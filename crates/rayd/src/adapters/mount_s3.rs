//! `mount-s3` daemon adapter (`m15-s3-mounts`, ADR-017): launches
//! `mount-s3 --foreground <bucket> /dev/fd/<N> [...]` bound to the FUSE
//! descriptor `adapters::fuse_device::LinuxFuseDevice::attach` returned,
//! as the dedicated `rayito-mount` user (uid 990, `image/Dockerfile`), with
//! an environment rebuilt from scratch holding only `AWS_REGION` and
//! `PATH` — never a credential in argv or env (SEC-3): `mount-s3` resolves
//! the execution role straight from IMDS itself, in its own process, which
//! is reachable from uid 990 because the M6 IMDS blackhole only covers
//! `uidrange 1000-65535` (ADR-012).
//!
//! This adapter reaps its own children by `.wait()`-ing each one on a
//! dedicated task the moment it is spawned, *and* registers the pid in the
//! shared `ChildRegistry` for that same window: `adapters::orphan_reaper`
//! (reserved for a re-parented zombie, never a pid rayd is still waiting on
//! itself) must never win the race to `waitpid` a `mount-s3` this task has
//! not yet reaped — registering closes that window instead of relying on
//! `orphan_reaper` not being wired into the PID-1 loop yet (`features::mod`
//! `FeatureContext`'s own non-blocking follow-up).

use std::collections::{HashMap, HashSet};
use std::os::fd::RawFd;
use std::process::Stdio;
use std::sync::{Arc, Mutex, PoisonError};

use nix::sys::signal::Signal;
use nix::unistd::{Gid, Uid, setgid, setgroups, setuid};
use rayd_core::process::env::DEFAULT_PATH;
use rayd_core::s3_mount::{FuseDaemon, MountErrorClass, S3Mount};
// `tokio::process::Command::pre_exec` is an inherent method (unix-only), so
// no `CommandExt` trait import is needed to call it.
use tokio::io::AsyncReadExt;
use tokio::process::Command;

use super::child_registry::ChildRegistry;
use super::fuse_device::{GUEST_GROUP_ID, GUEST_USER_ID};
use super::sidecar_process::signal_process_group;

/// AL2023 `mount-s3`/`fuse` packages (Q80 of the out-of-scope research,
/// +22.4 MB measured on the image build); `image/Dockerfile` installs the
/// binary on `PATH`.
pub const MOUNT_S3_BINARY: &str = "mount-s3";
/// `rayito-mount`, created by `image/Dockerfile`. Chosen below
/// `rayd_core::process::identity::MIN_UNPRIVILEGED_ID` (1000) on purpose:
/// the M6 IMDS blackhole (`ip rule add uidrange 1000-65535 lookup 100`)
/// does not cover a system account, so this uid keeps direct IMDS access
/// for `mount-s3`'s own credential resolution while uid 1000 (the
/// sandboxed user) stays blocked.
pub const MOUNT_USER_UID: u32 = 990;
pub const MOUNT_USER_GID: u32 = 990;
/// First descriptor above stdio: `pre_exec` `dup2`s the attached FUSE
/// descriptor here before `exec`, so the `/dev/fd/<N>` argument is always
/// this fixed number regardless of what `attach()` happened to return.
const MOUNT_FD_SLOT: RawFd = 3;
/// How much of `mount-s3`'s stderr `classify_exit` looks at; only the
/// *class* it maps to ever crosses the wire or a log line (`MountErrorClass`
/// is the closed set that does) — the bytes themselves are read here and
/// discarded the moment classification is done.
const STDERR_TAIL_BYTES: usize = 4096;

/// Spawns `mount-s3` with `tokio::process` directly rather than through
/// `rayd_core::process::ProcessSpawner`: that port's `SpawnSpec` plans a
/// *sandboxed user's* command (output streaming, a server-side timeout, a
/// CPU budget), none of which apply to an internal, long-lived daemon with
/// a fixed, non-request-supplied identity.
pub struct TokioMountS3Daemon {
    region: String,
    registry: Arc<ChildRegistry>,
    /// `Arc` (rather than a bare `Mutex`) so the reap task spawned by
    /// `spawn()` below can hold its own clone of the same set without
    /// borrowing `self` past this call's lifetime.
    alive: Arc<Mutex<HashSet<i32>>>,
    /// Filled by the reap task the instant a pid leaves `alive`; read
    /// (and removed) exactly once by `exit_class`.
    exited: Arc<Mutex<HashMap<i32, MountErrorClass>>>,
}

impl TokioMountS3Daemon {
    #[must_use]
    pub fn new(region: String, registry: Arc<ChildRegistry>) -> Self {
        Self {
            region,
            registry,
            alive: Arc::new(Mutex::new(HashSet::new())),
            exited: Arc::new(Mutex::new(HashMap::new())),
        }
    }

    fn alive_set(&self) -> std::sync::MutexGuard<'_, HashSet<i32>> {
        self.alive.lock().unwrap_or_else(PoisonError::into_inner)
    }
}

impl FuseDaemon for TokioMountS3Daemon {
    fn spawn(&self, fd: RawFd, mount: &S3Mount) -> Result<i32, MountErrorClass> {
        let mut command = Command::new(MOUNT_S3_BINARY);
        command
            .arg("--foreground")
            .arg(&mount.bucket)
            .arg(format!("/dev/fd/{MOUNT_FD_SLOT}"))
            .arg("--uid")
            .arg(GUEST_USER_ID.to_string())
            .arg("--gid")
            .arg(GUEST_GROUP_ID.to_string());
        if !mount.prefix.is_empty() {
            command.arg("--prefix").arg(&mount.prefix);
        }
        if mount.read_only {
            command.arg("--read-only");
        } else {
            if mount.allow_overwrite {
                command.arg("--allow-overwrite");
            }
            if mount.allow_delete {
                command.arg("--allow-delete");
            }
        }
        command
            .env_clear()
            .env("AWS_REGION", &self.region)
            .env("PATH", DEFAULT_PATH)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            // Piped, not `null`: `classify_exit` reads a bounded tail to
            // tell an IAM denial from a missing bucket from a network
            // failure. Never logged, never returned as text (SEC-3-style
            // residual: only the closed `MountErrorClass` crosses out of
            // this module).
            .stderr(Stdio::piped())
            .process_group(0)
            .kill_on_drop(false);
        // SAFETY: runs in the forked child between `fork` and `exec`.
        // `dup2`/`close` only touch `fd`, computed in the parent before
        // `fork`; `setgroups`/`setgid`/`setuid` only touch the fixed
        // `MOUNT_USER_{UID,GID}` constants. No allocation, no lock.
        unsafe {
            command.pre_exec(move || {
                if libc::dup2(fd, MOUNT_FD_SLOT) == -1 {
                    return Err(std::io::Error::last_os_error());
                }
                if fd != MOUNT_FD_SLOT {
                    libc::close(fd);
                }
                setgroups(&[Gid::from_raw(MOUNT_USER_GID)]).map_err(io_error)?;
                setgid(Gid::from_raw(MOUNT_USER_GID)).map_err(io_error)?;
                setuid(Uid::from_raw(MOUNT_USER_UID)).map_err(io_error)?;
                Ok(())
            });
        }
        let mut child = command
            .spawn()
            .map_err(|_io_error| MountErrorClass::HelperMissing)?;
        let pid = i32::try_from(child.id().ok_or(MountErrorClass::HelperMissing)?)
            .map_err(|_overflow| MountErrorClass::HelperMissing)?;
        let mut stderr = child.stderr.take();
        self.alive_set().insert(pid);
        self.registry.register(pid);
        let alive = Arc::clone(&self.alive);
        let exited = Arc::clone(&self.exited);
        let registry = Arc::clone(&self.registry);
        tokio::spawn(async move {
            // Reads concurrently with `wait()` so a full stderr pipe can
            // never deadlock the exit; bounded to `STDERR_TAIL_BYTES` (a
            // chatty daemon does not grow this task's own memory).
            let read_stderr = async {
                let mut buffer = Vec::new();
                if let Some(pipe) = stderr.as_mut() {
                    let _ = pipe
                        .take(STDERR_TAIL_BYTES as u64)
                        .read_to_end(&mut buffer)
                        .await;
                }
                buffer
            };
            let (status, tail) = tokio::join!(child.wait(), read_stderr);
            registry.unregister(pid);
            let class = classify_exit(status.as_ref().ok(), &tail);
            exited
                .lock()
                .unwrap_or_else(PoisonError::into_inner)
                .insert(pid, class);
            alive
                .lock()
                .unwrap_or_else(PoisonError::into_inner)
                .remove(&pid);
        });
        Ok(pid)
    }

    fn is_alive(&self, pid: i32) -> bool {
        self.alive_set().contains(&pid)
    }

    fn exit_class(&self, pid: i32) -> MountErrorClass {
        self.exited
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .remove(&pid)
            // Defensive only: `exit_class` is documented as never called
            // while `is_alive` is still true, so this never actually
            // triggers once the reap task above has run.
            .unwrap_or(MountErrorClass::Network)
    }

    fn kill(&self, pid: i32) {
        if let Ok(pid_u32) = u32::try_from(pid) {
            signal_process_group(pid_u32, Signal::SIGTERM as i32);
        }
    }
}

/// Best-effort classification of why `mount-s3 --foreground` exited before
/// (or instead of) becoming ready: a closed, small heuristic over its own
/// documented failure modes, never the raw text. Exiting cleanly
/// (`status == 0`) is itself still a failure here — a foreground mount
/// daemon is never supposed to exit on its own while the mount is wanted.
fn classify_exit(status: Option<&std::process::ExitStatus>, stderr_tail: &[u8]) -> MountErrorClass {
    let text = String::from_utf8_lossy(stderr_tail).to_lowercase();
    if text.contains("access denied") || text.contains("forbidden") || text.contains("403") {
        return MountErrorClass::IamDenied;
    }
    if text.contains("nosuchbucket")
        || text.contains("no such bucket")
        || text.contains("not found")
        || text.contains("does not exist")
        || text.contains("404")
    {
        return MountErrorClass::NotFound;
    }
    if text.contains("timed out") || text.contains("timeout") {
        return MountErrorClass::Timeout;
    }
    if status.is_none() {
        // `child.wait()` itself failed (already reaped elsewhere, or a
        // platform error): nothing to classify from the exit code.
        return MountErrorClass::Network;
    }
    MountErrorClass::Network
}

fn io_error(error: nix::Error) -> std::io::Error {
    std::io::Error::from_raw_os_error(error as i32)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn io_error_round_trips_the_errno_value() {
        let error = io_error(nix::Error::EACCES);
        assert_eq!(error.raw_os_error(), Some(nix::Error::EACCES as i32));
    }

    #[test]
    fn classify_exit_reads_the_closed_classes_from_known_stderr_phrases() {
        let ok_status = std::process::Command::new("true").status().ok();
        assert_eq!(
            classify_exit(ok_status.as_ref(), b"Access Denied by bucket policy"),
            MountErrorClass::IamDenied
        );
        assert_eq!(
            classify_exit(
                ok_status.as_ref(),
                b"NoSuchBucket: the bucket does not exist"
            ),
            MountErrorClass::NotFound
        );
        assert_eq!(
            classify_exit(ok_status.as_ref(), b"request timed out"),
            MountErrorClass::Timeout
        );
        assert_eq!(
            classify_exit(ok_status.as_ref(), b"connection reset"),
            MountErrorClass::Network
        );
    }

    #[test]
    fn classify_exit_falls_back_to_network_with_no_status() {
        assert_eq!(classify_exit(None, b""), MountErrorClass::Network);
    }
}
