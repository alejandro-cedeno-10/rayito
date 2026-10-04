//! Linux `FuseDevice` adapter (`m15-s3-mounts`, ADR-017): opens
//! `/dev/fuse` and performs the `mount(2)` the Linux FUSE kernel ABI
//! expects (`Documentation/filesystems/fuse.rst`: `fd`, `rootmode`,
//! `user_id`, `group_id`, `allow_other` as the mount data string), so a
//! later read/write under `mount_path` reaches the `mount-s3` daemon
//! (`adapters::mount_s3`) bound to the same descriptor. No AWS call and no
//! credential: this adapter only ever talks to the kernel.
//!
//! The mountpoint itself is walked and targeted without following any
//! symlink (`adapters::mountpoint`, shared with the EFS adapter): uid 1000
//! owns every allowed mount root under `/home/user` and could otherwise swap
//! a component for a link to a system directory.

use std::fs::OpenOptions;
use std::os::fd::{IntoRawFd, RawFd};
use std::os::unix::fs::OpenOptionsExt;
use std::path::Path;
use std::time::Duration;

use rayd_core::s3_mount::{FuseDevice, MountErrorClass, S3Mount};

use super::mountpoint::{
    FILESYSTEM_ROOT, MountpointError, StatProbe, c_string, open_dir_no_symlinks, proc_fd_path,
    stat_as_guest, unmount_no_symlinks,
};
use super::mountpoint::{GUEST_GROUP_ID, GUEST_USER_ID};

const FUSE_DEVICE_PATH: &str = "/dev/fuse";
/// `S_IFDIR` (kernel `stat.h`): the FUSE ABI's `rootmode` is the root
/// inode's file-type bits, and every mount here is a directory.
const FUSE_ROOTMODE_DIR: u32 = 0o040_000;
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

/// `user_id`/`group_id` in the FUSE mount data (`mountpoint::GUEST_USER_ID`/
/// `GUEST_GROUP_ID`) are an *access* gate, not an ownership one — they
/// decide which uid's requests the kernel lets reach the mount at all; the
/// inode ownership `stat` reports comes from `mount-s3` itself (its own
/// `--uid`/`--gid`, `adapters::mount_s3`), which must be given the same ids
/// for the two to agree. `allow_other` widens the *kernel's* gate so any
/// uid other than 1000 can reach the mount too; `mount-s3`'s own
/// `--uid`/`--gid` is what still makes every file appear owned by the guest
/// user despite that.
pub struct LinuxFuseDevice;

impl FuseDevice for LinuxFuseDevice {
    fn attach(&self, mount: &S3Mount) -> Result<RawFd, MountErrorClass> {
        let mountpoint = open_dir_no_symlinks(Path::new(FILESYSTEM_ROOT), &mount.mount_path, true)
            .map_err(walk_class)?;
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
        let target = proc_fd_path(&mountpoint, None);
        let result = raw_mount("mount-s3", &target, "fuse", &data);
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
        let result = unmount_no_symlinks(mount_path).map_err(unmount_class);
        // SAFETY: `fd` is the descriptor `attach` returned for this exact
        // mount; `rayd::features::s3_mounts` owns it as `Option<RawFd>` and
        // only ever calls `detach` once per attach, taking the option so a
        // second call can never see the same number again.
        unsafe {
            libc::close(fd);
        }
        result
    }

    fn probe_ready(&self, mount_path: &str) -> bool {
        // A real subprocess (`mountpoint::stat_as_guest`), never an
        // in-process `stat(2)`: once `mount(2)` succeeds the kernel blocks
        // any filesystem operation under `mount_path` until `mount-s3`
        // answers FUSE_INIT, which may never happen. Running as the guest's
        // own uid is also what proves a *guest* `stat`/`open` would
        // succeed, which is what `sbx.mounts == "mounted"` promises.
        stat_as_guest(mount_path, PROBE_PROCESS_TIMEOUT) == StatProbe::Answered
    }
}

/// The walk's refusal as the s3-mounts error class (`InvalidPath` for a
/// symlink or a non-directory, `NotFound` otherwise).
fn walk_class(error: MountpointError) -> MountErrorClass {
    match error {
        MountpointError::InvalidPath => MountErrorClass::InvalidPath,
        MountpointError::NotFound | MountpointError::Syscall(_) => MountErrorClass::NotFound,
    }
}

/// A failed `umount2` keeps the class it always had here (`Timeout`).
fn unmount_class(error: MountpointError) -> MountErrorClass {
    match error {
        MountpointError::Syscall(_) => MountErrorClass::Timeout,
        walk => walk_class(walk),
    }
}

fn raw_mount(source: &str, target: &str, fstype: &str, data: &str) -> Result<(), MountErrorClass> {
    let source = c_string(source).map_err(walk_class)?;
    let target = c_string(target).map_err(walk_class)?;
    let fstype = c_string(fstype).map_err(walk_class)?;
    let data = c_string(data).map_err(walk_class)?;
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
    fn a_symlinked_or_missing_mountpoint_keeps_its_s3_class() {
        assert_eq!(
            walk_class(MountpointError::InvalidPath),
            MountErrorClass::InvalidPath
        );
        assert_eq!(
            walk_class(MountpointError::NotFound),
            MountErrorClass::NotFound
        );
        assert_eq!(
            unmount_class(MountpointError::Syscall(nix::errno::Errno::EBUSY)),
            MountErrorClass::Timeout
        );
    }

    #[test]
    fn probe_ready_is_false_for_a_path_with_an_interior_nul() {
        // `stat` itself rejects the argument; this just proves the probe
        // never panics on a value that can't reach the syscall layer.
        assert!(!LinuxFuseDevice.probe_ready("/mnt/da\0ta"));
    }
}
