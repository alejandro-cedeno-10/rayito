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
//! dedicated task the moment it is spawned, independently of
//! `adapters::{child_registry, orphan_reaper}` (reserved for processes
//! spawned through the generic `ProcessSpawner` port): a `mount-s3` pid is
//! never both tracked here and visible to a future orphan sweep, so there
//! is no double-`waitpid` hazard between the two.

use std::collections::HashSet;
use std::os::fd::RawFd;
use std::process::Stdio;
use std::sync::{Arc, Mutex, PoisonError};

use nix::sys::signal::Signal;
use nix::unistd::{Gid, Uid, setgid, setgroups, setuid};
use rayd_core::process::env::DEFAULT_PATH;
use rayd_core::s3_mount::{FuseDaemon, MountErrorClass, S3Mount};
// `tokio::process::Command::pre_exec` is an inherent method (unix-only), so
// no `CommandExt` trait import is needed to call it.
use tokio::process::Command;

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

/// Spawns `mount-s3` with `tokio::process` directly rather than through
/// `rayd_core::process::ProcessSpawner`: that port's `SpawnSpec` plans a
/// *sandboxed user's* command (output streaming, a server-side timeout, a
/// CPU budget), none of which apply to an internal, long-lived daemon with
/// a fixed, non-request-supplied identity.
pub struct TokioMountS3Daemon {
    region: String,
    /// `Arc` (rather than a bare `Mutex`) so the reap task spawned by
    /// `spawn()` below can hold its own clone of the same set without
    /// borrowing `self` past this call's lifetime.
    alive: Arc<Mutex<HashSet<i32>>>,
}

impl TokioMountS3Daemon {
    #[must_use]
    pub fn new(region: String) -> Self {
        Self {
            region,
            alive: Arc::new(Mutex::new(HashSet::new())),
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
            .arg(format!("/dev/fd/{MOUNT_FD_SLOT}"));
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
            .stderr(Stdio::null())
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
        let mut child = command.spawn().map_err(|_io_error| MountErrorClass::HelperMissing)?;
        let pid = i32::try_from(child.id().ok_or(MountErrorClass::HelperMissing)?)
            .map_err(|_overflow| MountErrorClass::HelperMissing)?;
        self.alive_set().insert(pid);
        let alive = Arc::clone(&self.alive);
        tokio::spawn(async move {
            // Reaps the exit status so a crashed or unmounted `mount-s3`
            // never lingers as a zombie; `is_alive` below is exactly this
            // set, so a dead daemon is observed the instant `wait()`
            // returns, not by polling `/proc`.
            let _ = child.wait().await;
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

    fn kill(&self, pid: i32) {
        if let Ok(pid_u32) = u32::try_from(pid) {
            signal_process_group(pid_u32, Signal::SIGTERM as i32);
        }
    }
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
}
