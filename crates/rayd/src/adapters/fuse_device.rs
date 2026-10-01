//! Linux `FuseDevice` adapter (`m15-s3-mounts`, ADR-017): opens
//! `/dev/fuse` and performs the `mount(2)` the Linux FUSE kernel ABI
//! expects (`Documentation/filesystems/fuse.rst`: `fd`, `rootmode`,
//! `user_id`, `group_id` as the mount data string), so a later read/write
//! under `mount_path` reaches the `mount-s3` daemon
//! (`adapters::mount_s3`) bound to the same descriptor. No AWS call and no
//! credential: this adapter only ever talks to the kernel.

use std::ffi::CString;
use std::fs::OpenOptions;
use std::os::fd::{IntoRawFd, RawFd};
use std::os::unix::fs::OpenOptionsExt;

use rayd_core::s3_mount::{FuseDevice, MountErrorClass, S3Mount};

const FUSE_DEVICE_PATH: &str = "/dev/fuse";
/// `S_IFDIR` (kernel `stat.h`): the FUSE ABI's `rootmode` is the root
/// inode's file-type bits, and every mount here is a directory.
const FUSE_ROOTMODE_DIR: u32 = 0o040_000;
/// The guest's default user (`image/Dockerfile`'s `user`, uid 1000):
/// `user_id`/`group_id` in the FUSE mount data make the kernel report that
/// owner for every inode under the mount, so `user`'s own file operations
/// (`stat`, `open`) succeed without `allow_other`'s wider exposure.
const GUEST_USER_ID: u32 = 1000;
const GUEST_GROUP_ID: u32 = 1000;
/// `O_NONBLOCK` on `/dev/fuse`'s own fd (distinct from the mount's
/// behaviour): libfuse always opens it this way so a request read never
/// blocks the attach itself.
const FUSE_DEVICE_OPEN_FLAGS: i32 = libc::O_NONBLOCK;

pub struct LinuxFuseDevice;

impl FuseDevice for LinuxFuseDevice {
    fn attach(&self, mount: &S3Mount) -> Result<RawFd, MountErrorClass> {
        std::fs::create_dir_all(&mount.mount_path).map_err(|_io_error| MountErrorClass::NotFound)?;
        let device = OpenOptions::new()
            .read(true)
            .write(true)
            .custom_flags(FUSE_DEVICE_OPEN_FLAGS)
            .open(FUSE_DEVICE_PATH)
            .map_err(|_io_error| MountErrorClass::HelperMissing)?;
        let fd = device.into_raw_fd();
        let data = format!(
            "fd={fd},rootmode={FUSE_ROOTMODE_DIR:o},user_id={GUEST_USER_ID},group_id={GUEST_GROUP_ID}"
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
        // extra step" contract (§7.2: unmount never blocks a hook).
        let result = unsafe { libc::umount2(target.as_ptr(), libc::MNT_DETACH) };
        // SAFETY: `fd` is the descriptor `attach` returned for this exact
        // mount; the caller never calls `detach` twice for the same one.
        unsafe {
            libc::close(fd);
        }
        if result == 0 {
            Ok(())
        } else {
            Err(MountErrorClass::Timeout)
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
    // `rootmode`/`user_id`/`group_id` data string.
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
        nix::errno::Errno::EACCES | nix::errno::Errno::EPERM => MountErrorClass::IamDenied,
        nix::errno::Errno::ENOENT | nix::errno::Errno::ENOTDIR => MountErrorClass::NotFound,
        nix::errno::Errno::ENODEV | nix::errno::Errno::ENOSYS => MountErrorClass::HelperMissing,
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
    fn classify_mount_errno_maps_permission_errors_to_iam_denied() {
        // `Errno::last()` reflects whatever the test process's last libc
        // call set; force a known value first so the mapping itself is
        // what gets exercised, not ambient process state.
        nix::errno::Errno::EACCES.set();
        assert_eq!(classify_mount_errno(), MountErrorClass::IamDenied);
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
}
