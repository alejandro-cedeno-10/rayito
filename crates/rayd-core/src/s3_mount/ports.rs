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
/// not exist. Never follows a symlink in any component of the path (uid
/// 1000 owns `/home/user` and could plant one): such a path is
/// `MountErrorClass::InvalidPath`. On success the returned descriptor is the one `FuseDaemon`
/// hands to `mount-s3` as `/dev/fd/<fd>`.
pub trait FuseDevice: Send + Sync {
    fn attach(&self, mount: &S3Mount) -> Result<RawFd, MountErrorClass>;

    /// `umount2(MNT_DETACH | UMOUNT_NOFOLLOW)` on `mount_path` (ancestors
    /// resolved without following symlinks, like `attach`), then closes the descriptor
    /// `attach` returned for it. Idempotent from the caller's point of
    /// view only in the sense that calling it twice for the same `fd` is a
    /// bug the caller must never commit — ownership of `fd` passes here,
    /// so the caller drops its own copy of the number the moment this
    /// returns, successfully or not.
    fn detach(&self, mount_path: &str, fd: RawFd) -> Result<(), MountErrorClass>;

    /// Bounded check of whether `mount_path` is answering filesystem
    /// requests yet (a `stat` of its root as the guest user, run as its
    /// own short-lived, killable child so a FUSE connection with no
    /// daemon behind it can never block this call itself). Called in a
    /// loop by `rayd::features::s3_mounts` while a mount is `Pending`.
    fn probe_ready(&self, mount_path: &str) -> bool;
}

/// One running `mount-s3` process bound to an already-attached FUSE
/// descriptor; `pid` is a plain `libc` pid, never wrapped in a tokio
/// handle, so the domain stays free of an async runtime type.
pub trait FuseDaemon: Send + Sync {
    fn spawn(&self, fd: RawFd, mount: &S3Mount) -> Result<i32, MountErrorClass>;

    /// `true` while the pid is still running (a non-blocking check); the
    /// restart logic in `rayd::features::s3_mounts` relaunches a dead
    /// daemon rather than ever calling this on a pid it did not spawn.
    fn is_alive(&self, pid: i32) -> bool;

    /// Once `is_alive` has turned `false` for a `pid` this adapter spawned,
    /// the classified reason it exited — from its exit status and a
    /// stderr tail that is read here and never logged or returned as
    /// text, only as this closed class. Consumes the record: the caller
    /// asks exactly once per death, right after observing `is_alive` turn
    /// `false`, so a second call for the same `pid` is never made and an
    /// adapter is free to free the record's memory on read. Never called
    /// while `is_alive(pid)` is still `true`.
    fn exit_class(&self, pid: i32) -> MountErrorClass;

    /// Best-effort `SIGTERM` to the process group; never panics on an
    /// already dead pid.
    fn kill(&self, pid: i32);
}
