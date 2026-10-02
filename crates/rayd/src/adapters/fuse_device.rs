//! Linux `FuseDevice` adapter (`m15-s3-mounts`, ADR-017): opens
//! `/dev/fuse` and performs the `mount(2)` the Linux FUSE kernel ABI
//! expects (`Documentation/filesystems/fuse.rst`: `fd`, `rootmode`,
//! `user_id`, `group_id`, `allow_other` as the mount data string), so a
//! later read/write under `mount_path` reaches the `mount-s3` daemon
//! (`adapters::mount_s3`) bound to the same descriptor. No AWS call and no
//! credential: this adapter only ever talks to the kernel.

use std::ffi::CString;
use std::fs::OpenOptions;
use std::os::fd::{IntoRawFd, RawFd};
use std::os::unix::fs::OpenOptionsExt;
use std::os::unix::process::CommandExt;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use rayd_core::s3_mount::{FuseDevice, MountErrorClass, S3Mount};

const FUSE_DEVICE_PATH: &str = "/dev/fuse";
/// `S_IFDIR` (kernel `stat.h`): the FUSE ABI's `rootmode` is the root
/// inode's file-type bits, and every mount here is a directory.
const FUSE_ROOTMODE_DIR: u32 = 0o040_000;
/// The guest's default user (`image/Dockerfile`'s `user`, uid 1000).
/// `user_id`/`group_id` in the FUSE mount data are an *access* gate, not an
/// ownership one — they decide which uid's requests the kernel lets reach
/// the mount at all; the inode ownership `stat` reports comes from
/// `mount-s3` itself (its own `--uid`/`--gid`, `adapters::mount_s3`), which
/// must be given the same ids for the two to agree. `allow_other` widens
/// the *kernel's* gate so rayd's own root-run readiness probe
/// (`probe_ready`, below) can reach the mount too, same as any uid other
/// than 1000; `mount-s3`'s own `--uid`/`--gid` is what still makes every
/// file appear owned by the guest user despite that.
pub const GUEST_USER_ID: u32 = 1000;
pub const GUEST_GROUP_ID: u32 = 1000;
/// `O_NONBLOCK` on `/dev/fuse`'s own fd (distinct from the mount's
/// behaviour): libfuse always opens it this way so a request read never
/// blocks the attach itself.
const FUSE_DEVICE_OPEN_FLAGS: i32 = libc::O_NONBLOCK;
/// Upper bound on one `stat` readiness probe: long enough for a local FUSE
/// round-trip once `mount-s3` is actually answering, short enough that a
/// daemon still stuck in its own IMDS/S3 startup call never blocks the
/// caller's own retry loop (`rayd::features::s3_mounts::await_ready`) for
/// more than this one attempt.
const PROBE_PROCESS_TIMEOUT: Duration = Duration::from_millis(300);
/// How often `probe_ready` polls the probe child for an exit, while it
/// waits.
const PROBE_POLL_INTERVAL: Duration = Duration::from_millis(10);
const STAT_BINARY: &str = "stat";

pub struct LinuxFuseDevice;

impl FuseDevice for LinuxFuseDevice {
    fn attach(&self, mount: &S3Mount) -> Result<RawFd, MountErrorClass> {
        std::fs::create_dir_all(&mount.mount_path)
            .map_err(|_io_error| MountErrorClass::NotFound)?;
        let device = OpenOptions::new()
            .read(true)
            .write(true)
            .custom_flags(FUSE_DEVICE_OPEN_FLAGS)
            .open(FUSE_DEVICE_PATH)
            .map_err(|_io_error| MountErrorClass::HelperMissing)?;
        let fd = device.into_raw_fd();
        let data = format!(
            "fd={fd},rootmode={FUSE_ROOTMODE_DIR:o},user_id={GUEST_USER_ID},group_id={GUEST_GROUP_ID},allow_other"
        );
        let result = raw_mount("mount-s3", &mount.mount_path, "fuse", &data);
        if let Err(class) = result {
            // SAFETY: `fd` came from `into_raw_fd()` above and nothing else
            // owns it yet; closing it on a failed mount prevents a leak.
            unsafe {
                libc::close(fd);
            }
            return Err(class);
        }
        Ok(fd)
    }

    fn detach(&self, mount_path: &str, fd: RawFd) -> Result<(), MountErrorClass> {
        let target = c_string(mount_path)?;
        // SAFETY: `target` is a valid, nul-terminated C string for the
        // duration of this call; `MNT_DETACH` lets the unmount succeed even
        // if a reader is still inside the mount, matching `/suspend`'s "no
        // extra step" contract (ADR-017: unmount never blocks a hook).
        let result = unsafe { libc::umount2(target.as_ptr(), libc::MNT_DETACH) };
        // SAFETY: `fd` is the descriptor `attach` returned for this exact
        // mount; `rayd::features::s3_mounts` owns it as `Option<RawFd>` and
        // only ever calls `detach` once per attach, taking the option so a
        // second call can never see the same number again.
        unsafe {
            libc::close(fd);
        }
        if result == 0 {
            Ok(())
        } else {
            Err(MountErrorClass::Timeout)
        }
    }

    fn probe_ready(&self, mount_path: &str) -> bool {
        // A real subprocess, not an in-process `stat(2)`: once `mount(2)`
        // succeeds the kernel will block any filesystem operation under
        // `mount_path` until `mount-s3` answers FUSE_INIT, which may never
        // happen (a dead or hung daemon); running the probe as its own
        // child lets a bounded wait `kill()` it instead of blocking this
        // call, or a blocking thread in `spawn_blocking`'s own pool,
        // forever. Runs as the guest's own uid (never root): `allow_other`
        // lets the kernel accept that, but exercising it here is also what
        // proves a *guest* `stat`/`open` would succeed, which is the thing
        // `sbx.mounts == "mounted"` is actually promising the caller.
        let mut child = match Command::new(STAT_BINARY)
            .arg(mount_path)
            .uid(GUEST_USER_ID)
            .gid(GUEST_GROUP_ID)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
        {
            Ok(child) => child,
            Err(_spawn_error) => return false,
        };
        let deadline = Instant::now() + PROBE_PROCESS_TIMEOUT;
        loop {
            match child.try_wait() {
                Ok(Some(status)) => return status.success(),
                Ok(None) if Instant::now() < deadline => {
                    std::thread::sleep(PROBE_POLL_INTERVAL);
                }
                Ok(None) => {
                    // Still hung past the deadline: kill and reap it so it
                    // never lingers blocked on the mount forever (the
                    // leak the pre-fix `/resume` probe had with
                    // `kill_on_drop(false)` — this probe is not even a
                    // tokio child, so the equivalent discipline is doing
                    // both steps by hand).
                    let _ = child.kill();
                    let _ = child.wait();
                    return false;
                }
                Err(_wait_error) => return false,
            }
        }
    }
}

fn raw_mount(source: &str, target: &str, fstype: &str, data: &str) -> Result<(), MountErrorClass> {
    let source = c_string(source)?;
    let target = c_string(target)?;
    let fstype = c_string(fstype)?;
    let data = c_string(data)?;
    // SAFETY: every pointer is a valid, nul-terminated C string owned by a
    // local `CString` kept alive until after the call; `flags = 0` (no
    // `MS_*` bit) is what a FUSE mount needs beyond the kernel ABI's own
    // `rootmode`/`user_id`/`group_id`/`allow_other` data string.
    let result = unsafe {
        libc::mount(
            source.as_ptr(),
            target.as_ptr(),
            fstype.as_ptr(),
            0,
            data.as_ptr().cast(),
        )
    };
    if result == 0 {
        Ok(())
    } else {
        Err(classify_mount_errno())
    }
}

fn classify_mount_errno() -> MountErrorClass {
    match nix::errno::Errno::last() {
        // Neither errno is an AWS/IAM decision: `mount(2)` itself only
        // ever denies for a kernel-side reason (missing `CAP_SYS_ADMIN`,
        // or the target already busy) — the execution role's own
        // permissions are decided later, inside `mount-s3`'s own process,
        // and surface through `adapters::mount_s3`'s exit classification
        // instead.
        nix::errno::Errno::ENOENT | nix::errno::Errno::ENOTDIR => MountErrorClass::NotFound,
        nix::errno::Errno::EACCES
        | nix::errno::Errno::EPERM
        | nix::errno::Errno::ENODEV
        | nix::errno::Errno::ENOSYS => MountErrorClass::HelperMissing,
        nix::errno::Errno::ETIMEDOUT => MountErrorClass::Timeout,
        _other => MountErrorClass::Network,
    }
}

fn c_string(value: &str) -> Result<CString, MountErrorClass> {
    CString::new(value).map_err(|_nul_error| MountErrorClass::NotFound)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn classify_mount_errno_maps_a_privilege_error_to_helper_missing_never_iam() {
        // `Errno::last()` reflects whatever the test process's last libc
        // call set; force a known value first so the mapping itself is
        // what gets exercised, not ambient process state. `EPERM`/`EACCES`
        // from `mount(2)` are never an IAM decision (see
        // `classify_mount_errno`'s own comment).
        nix::errno::Errno::EACCES.set();
        assert_eq!(classify_mount_errno(), MountErrorClass::HelperMissing);
        nix::errno::Errno::EPERM.set();
        assert_eq!(classify_mount_errno(), MountErrorClass::HelperMissing);
        nix::errno::Errno::ENOENT.set();
        assert_eq!(classify_mount_errno(), MountErrorClass::NotFound);
        nix::errno::Errno::ENODEV.set();
        assert_eq!(classify_mount_errno(), MountErrorClass::HelperMissing);
        nix::errno::Errno::ETIMEDOUT.set();
        assert_eq!(classify_mount_errno(), MountErrorClass::Timeout);
        nix::errno::Errno::ECONNRESET.set();
        assert_eq!(classify_mount_errno(), MountErrorClass::Network);
    }

    #[test]
    fn a_path_with_an_interior_nul_is_rejected_before_any_syscall() {
        assert_eq!(c_string("/mnt/da\0ta"), Err(MountErrorClass::NotFound));
    }

    #[test]
    fn probe_ready_is_false_for_a_path_with_an_interior_nul() {
        // `stat` itself rejects the argument; this just proves the probe
        // never panics on a value that can't reach the syscall layer.
        assert!(!LinuxFuseDevice.probe_ready("/mnt/da\0ta"));
    }
}
