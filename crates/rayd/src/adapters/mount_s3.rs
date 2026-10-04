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
//! dedicated task the moment it is spawned, and spawns them through the
//! process-wide `ChildRegistry`, so PID 1's orphan reaper
//! (`adapters::orphan_reaper`, reserved for re-parented zombies) never wins
//! the race to `waitpid` a `mount-s3` this task has not reaped yet.

use std::collections::{HashMap, HashSet};
use std::os::fd::RawFd;
use std::process::Stdio;
use std::sync::{Arc, Mutex, PoisonError};

use nix::sys::signal::Signal;
use rayd_core::process::env::DEFAULT_PATH;
use rayd_core::s3_mount::{FuseDaemon, MountErrorClass, S3Mount};
// `tokio::process::Command::pre_exec` is an inherent method (unix-only), so
// no `CommandExt` trait import is needed to call it.
use tokio::io::{AsyncRead, AsyncReadExt};
use tokio::process::Command;

use super::child_registry::ChildRegistry;
use super::exec_posture::ExecPosture;
use super::mountpoint::{GUEST_GROUP_ID, GUEST_USER_ID};
use super::sidecar_process::signal_process_group;

/// Mountpoint for Amazon S3 1.24.0, installed by `image/Dockerfile` from
/// AWS's own pinned RPM (AL2023's repos stop at 1.22.3, Q80) on `PATH`
/// (72 677 112 B installed, `AWS_API_NOTES.md` Q100).
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
/// How much of `mount-s3`'s stderr `classify_exit` looks at: the *last*
/// bytes it wrote (its final error is what explains the exit), kept in a
/// bounded ring by `drain_stderr_tail`. Only the *class* it maps to ever
/// crosses the wire or a log line (`MountErrorClass` is the closed set
/// that does) — the bytes themselves are discarded the moment
/// classification is done.
pub(crate) const STDERR_TAIL_BYTES: usize = 4096;
/// One `read` of the stderr pipe while draining it.
const STDERR_READ_CHUNK_BYTES: usize = 1024;

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
    /// Filled by the reap task the instant a pid leaves `alive`; read
    /// (and removed) exactly once by `exit_class`.
    exited: Arc<Mutex<HashMap<i32, MountErrorClass>>>,
}

impl TokioMountS3Daemon {
    #[must_use]
    pub fn new(region: String) -> Self {
        Self {
            region,
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
            .args(daemon_args(mount))
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
        let posture =
            ExecPosture::as_user(MOUNT_USER_UID, MOUNT_USER_GID).inheriting_up_to(MOUNT_FD_SLOT);
        // SAFETY: runs in the forked child between `fork` and `exec`.
        // `dup2`/`close` only touch `fd`, computed in the parent before
        // `fork`; `ExecPosture::apply` then drops to the fixed
        // `MOUNT_USER_{UID,GID}`, marks every descriptor above
        // `MOUNT_FD_SLOT` close-on-exec (an inheritable PTY master from a
        // concurrent `openpty` never reaches `mount-s3`) and resets every
        // signal disposition, all on values computed before the fork. No
        // allocation, no lock.
        unsafe {
            command.pre_exec(move || {
                if libc::dup2(fd, MOUNT_FD_SLOT) == -1 {
                    return Err(std::io::Error::last_os_error());
                }
                if fd != MOUNT_FD_SLOT {
                    libc::close(fd);
                }
                posture.apply()
            });
        }
        let mut child = ChildRegistry::process()
            .spawn(&mut command)
            .map_err(|_io_error| MountErrorClass::HelperMissing)?;
        let pid = i32::try_from(child.id().ok_or(MountErrorClass::HelperMissing)?)
            .map_err(|_overflow| MountErrorClass::HelperMissing)?;
        let mut stderr = child.stderr.take();
        self.alive_set().insert(pid);
        let alive = Arc::clone(&self.alive);
        let exited = Arc::clone(&self.exited);
        tokio::spawn(async move {
            // Drains stderr for the daemon's whole life, concurrently with
            // `wait()`: a long-running daemon that keeps logging must never
            // fill the pipe (~64 KiB) and block on `write(2)`, which would
            // hang every FUSE request under the mount. Only the last
            // `STDERR_TAIL_BYTES` are kept, so a chatty daemon does not
            // grow this task's own memory either.
            let read_stderr = async {
                match stderr.as_mut() {
                    Some(pipe) => drain_stderr_tail(pipe).await,
                    None => Vec::new(),
                }
            };
            let (status, tail) = tokio::join!(child.wait(), read_stderr);
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

/// Reads `pipe` to EOF, keeping only its last `STDERR_TAIL_BYTES`.
pub(crate) async fn drain_stderr_tail<R: AsyncRead + Unpin>(pipe: &mut R) -> Vec<u8> {
    let mut tail = Vec::with_capacity(STDERR_TAIL_BYTES);
    let mut chunk = [0u8; STDERR_READ_CHUNK_BYTES];
    loop {
        match pipe.read(&mut chunk).await {
            Ok(0) | Err(_) => return tail,
            Ok(read) => keep_tail(&mut tail, &chunk[..read], STDERR_TAIL_BYTES),
        }
    }
}

/// Appends `chunk` to `tail`, then drops the oldest bytes beyond `cap`.
fn keep_tail(tail: &mut Vec<u8>, chunk: &[u8], cap: usize) {
    tail.extend_from_slice(chunk);
    let excess = tail.len().saturating_sub(cap);
    if excess > 0 {
        tail.drain(..excess);
    }
}

/// `mount-s3`'s argv for `mount` (everything after the binary name).
/// `--allow-other` is what lets the guest (uid 1000) use the mount at all:
/// the daemon runs as `rayito-mount` (uid 990), and without the flag
/// Mountpoint's own FUSE session only answers requests from its owner, so
/// every `stat`/`open` from uid 1000 gets `EACCES` even though the kernel
/// mount already carries `allow_other` (`fuse_device::attach`) — measured
/// on AWS, `AWS_API_NOTES.md` Q101: every mount stayed `pending` until the
/// readiness probe timed out. `--uid`/`--gid` keep file ownership on the
/// guest's user.
fn daemon_args(mount: &S3Mount) -> Vec<String> {
    let mut args = vec![
        "--foreground".to_owned(),
        mount.bucket.clone(),
        format!("/dev/fd/{MOUNT_FD_SLOT}"),
        "--allow-other".to_owned(),
        "--uid".to_owned(),
        GUEST_USER_ID.to_string(),
        "--gid".to_owned(),
        GUEST_GROUP_ID.to_string(),
    ];
    if !mount.prefix.is_empty() {
        args.extend(["--prefix".to_owned(), mount.prefix.clone()]);
    }
    if mount.read_only {
        args.push("--read-only".to_owned());
    } else {
        if mount.allow_overwrite {
            args.push("--allow-overwrite".to_owned());
        }
        if mount.allow_delete {
            args.push("--allow-delete".to_owned());
        }
    }
    args
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

#[cfg(test)]
mod tests {
    use super::*;

    fn mount_for(read_only: bool, prefix: &str) -> S3Mount {
        S3Mount {
            mount_path: "/mnt/data".to_owned(),
            bucket: "team-data".to_owned(),
            prefix: prefix.to_owned(),
            read_only,
            allow_overwrite: !read_only,
            allow_delete: !read_only,
        }
    }

    #[test]
    fn daemon_args_always_let_the_guest_user_use_the_mount() {
        let args = daemon_args(&mount_for(true, ""));
        assert_eq!(
            args,
            [
                "--foreground",
                "team-data",
                "/dev/fd/3",
                "--allow-other",
                "--uid",
                "1000",
                "--gid",
                "1000",
                "--read-only",
            ]
        );
    }

    #[test]
    fn daemon_args_carry_the_prefix_and_write_flags_only_when_writable() {
        let args = daemon_args(&mount_for(false, "runs/42/"));
        assert!(args.windows(2).any(|pair| pair == ["--prefix", "runs/42/"]));
        assert!(args.iter().any(|arg| arg == "--allow-overwrite"));
        assert!(args.iter().any(|arg| arg == "--allow-delete"));
        assert!(!args.iter().any(|arg| arg == "--read-only"));
    }

    #[test]
    fn classify_exit_reads_the_closed_classes_from_known_stderr_phrases() {
        let ok_status = Some(std::os::unix::process::ExitStatusExt::from_raw(0));
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
    fn keep_tail_keeps_only_the_most_recent_bytes() {
        let mut tail = Vec::new();
        keep_tail(&mut tail, b"abcdef", 4);
        assert_eq!(tail, b"cdef");
        keep_tail(&mut tail, b"gh", 4);
        assert_eq!(tail, b"efgh");
        keep_tail(&mut tail, b"", 4);
        assert_eq!(tail, b"efgh");
    }

    #[tokio::test]
    async fn the_drain_reads_past_the_tail_bound_and_keeps_the_final_error() {
        // A daemon that logs far more than the pipe buffer before failing:
        // every byte is consumed (nothing left to block a writer) and the
        // classified tail is the *last* message, not the first warnings.
        let mut noise = vec![b'w'; 256 * 1024];
        noise.extend_from_slice(b"\nError: Access Denied");
        let mut pipe: &[u8] = &noise;
        let tail = drain_stderr_tail(&mut pipe).await;
        assert!(pipe.is_empty());
        assert_eq!(tail.len(), STDERR_TAIL_BYTES);
        assert_eq!(classify_exit(None, &tail), MountErrorClass::IamDenied);
    }

    #[test]
    fn classify_exit_falls_back_to_network_with_no_status() {
        assert_eq!(classify_exit(None, b""), MountErrorClass::Network);
    }
}
