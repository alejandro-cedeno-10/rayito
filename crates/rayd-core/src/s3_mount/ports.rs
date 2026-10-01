//! What `rayd::features::s3_mounts` needs from the operating system. Both
//! ports are synchronous and return only [`MountErrorClass`]: the real
//! syscalls (`open("/dev/fuse")`, `mount(2)`, `fork`/`exec` of `mount-s3`)
//! live in the Linux adapters (`rayd::adapters::{fuse_device, mount_s3}`),
//! never here.

use std::os::fd::RawFd;

use super::error::MountErrorClass;
use super::spec::S3Mount;

/// Opens `/dev/fuse` and performs the `mount(2)` that attaches it at
/// `mount.mount_path`, creating the mountpoint directory first if it does
/// not exist. On success the returned descriptor is the one `FuseDaemon`
/// hands to `mount-s3` as `/dev/fd/<fd>`.
pub trait FuseDevice: Send + Sync {
    fn attach(&self, mount: &S3Mount) -> Result<RawFd, MountErrorClass>;

    /// `umount2(MNT_DETACH)` on `mount_path`, then closes the descriptor
    /// `attach` returned for it.
    fn detach(&self, mount_path: &str, fd: RawFd) -> Result<(), MountErrorClass>;
}

/// One running `mount-s3` process bound to an already-attached FUSE
/// descriptor; `pid` is a plain `libc` pid, never wrapped in a tokio
/// handle, so the domain stays free of an async runtime type.
pub trait FuseDaemon: Send + Sync {
    fn spawn(&self, fd: RawFd, mount: &S3Mount) -> Result<i32, MountErrorClass>;

    /// `true` while the pid is still running (a non-blocking `waitpid`);
    /// the restart logic in `rayd::features::s3_mounts` relaunches a dead
    /// daemon rather than ever calling this on a pid it did not spawn.
    fn is_alive(&self, pid: i32) -> bool;

    /// Best-effort `SIGTERM` then `SIGKILL`; never panics on an already
    /// dead pid.
    fn kill(&self, pid: i32);
}
