//! What the filesystem domain needs from the operating system: blocking
//! file operations issued under a filesystem identity, an atomic write
//! sink, an inotify-style watcher and the user database for owner and
//! group names. Synchronous functions with plain data; `std::io::Read` is
//! the only I/O type that crosses the port.

use std::io::Read;

use thiserror::Error;

use super::entry::{EntryKind, RawEntry};
use super::events::RawWatchEvent;
use super::identity::FsIdentity;
use super::metadata::FileMetadata;

/// Adapter failures carry an errno *name* (`ENOENT`) at most, never a path.
#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum FsIoError {
    #[error("no such file or directory")]
    NotFound,
    #[error("permission denied")]
    PermissionDenied,
    #[error("already exists")]
    AlreadyExists,
    #[error("not a directory")]
    NotADirectory,
    #[error("is a directory")]
    IsADirectory,
    #[error("directory not empty")]
    NotEmpty,
    #[error("not a regular file")]
    NotARegularFile,
    #[error("is a symlink")]
    IsSymlink,
    #[error("cross-device")]
    CrossDevice,
    #[error("no space left on device")]
    NoSpace,
    #[error("filesystem operations are not supported on this platform")]
    Unsupported,
    #[error("{errno}")]
    Other { errno: String },
}

#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum WatchError {
    #[error("no such directory")]
    NotFound,
    #[error("not a directory")]
    NotADirectory,
    #[error("permission denied")]
    PermissionDenied,
    #[error("inotify watch limit reached")]
    LimitReached,
    #[error("directory watching is not supported on this platform")]
    Unsupported,
    #[error("{0}")]
    Other(String),
}

/// Every call runs with `id` as the filesystem identity (the adapter sets
/// it per thread) so the kernel, not the domain, enforces permissions.
/// Paths are canonical absolute paths produced by the domain.
pub trait FileSystem: Send + Sync {
    /// `realpath`: fails with `NotFound` for a missing component and
    /// `NotADirectory` for a component that is a file.
    fn canonicalize(&self, id: &FsIdentity, path: &str) -> Result<String, FsIoError>;

    fn lstat(&self, id: &FsIdentity, path: &str) -> Result<RawEntry, FsIoError>;

    /// `lstat` of every child, unsorted; a child whose `lstat` fails between
    /// the directory read and the stat (a race with the sandbox) is skipped.
    fn read_dir(&self, id: &FsIdentity, path: &str) -> Result<Vec<RawEntry>, FsIoError>;

    /// `O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK`, regular files only:
    /// a symlink answers `IsSymlink`, a directory `IsADirectory`, anything
    /// else `NotARegularFile` (a FIFO is refused without waiting for a
    /// writer).
    fn open_read(&self, id: &FsIdentity, path: &str) -> Result<Box<dyn Read + Send>, FsIoError>;

    /// The same open as `open_read` plus an `fstat` of the descriptor: the
    /// export reads exactly the file measured here, by offset, even if the
    /// name is replaced afterwards.
    fn open_snapshot(&self, id: &FsIdentity, path: &str) -> Result<OpenedSnapshot, FsIoError>;

    /// `llistxattr` + `lgetxattr` of every `user.rayito.*` attribute, never
    /// following a symlink. A filesystem without xattrs, a file without
    /// them or one the identity may not read answers an empty set.
    fn read_metadata(&self, id: &FsIdentity, path: &str) -> Result<FileMetadata, FsIoError>;

    /// Bytes a non-root user may still write on the filesystem holding
    /// `canonical_dir` (`statvfs`: `f_bavail * f_frsize`). The directory
    /// may not exist yet: the adapter walks up to the deepest existing
    /// ancestor.
    fn free_bytes(&self, id: &FsIdentity, canonical_dir: &str) -> Result<u64, FsIoError>;

    /// Creates the missing parents of `dir` (mode `0o755`) and a temp file
    /// inside it; the sink commits by renaming over the final name.
    fn begin_write(
        &self,
        id: &FsIdentity,
        dir: &str,
        mode: u32,
    ) -> Result<Box<dyn WriteSink>, FsIoError>;

    /// `mkdir -p` with `0o755` parents and `mode` for the final component;
    /// `AlreadyExists` only when the final component itself exists (of any
    /// kind), `NotADirectory` when a parent component is not a directory.
    fn make_dir(&self, id: &FsIdentity, path: &str, mode: u32) -> Result<(), FsIoError>;

    /// `rename(2)`: `AlreadyExists`, `NotEmpty`, `IsADirectory` and
    /// `NotADirectory` describe a conflicting destination.
    fn rename(&self, id: &FsIdentity, from: &str, to: &str) -> Result<(), FsIoError>;

    /// `unlink` for anything but a directory; `rmdir` (`NotEmpty` when it
    /// is not) or a symlink-safe recursive removal for directories.
    fn remove(
        &self,
        id: &FsIdentity,
        path: &str,
        kind: EntryKind,
        recursive: bool,
    ) -> Result<(), FsIoError>;
}

/// An open temp file. Dropping it without `commit` unlinks the temp file
/// and leaves the destination untouched.
pub trait WriteSink: Send {
    fn write_chunk(&mut self, bytes: &[u8]) -> Result<(), FsIoError>;

    /// `fsetxattr` of every key on the temp file's descriptor, before
    /// `commit`: no path-based call, and the set appears with the content.
    /// `Unsupported` when the filesystem has no user xattrs, `NoSpace` when
    /// the set does not fit.
    fn set_metadata(&mut self, metadata: &FileMetadata) -> Result<(), FsIoError>;

    /// `fsync`, `fchmod`, `fchown` to `id`, `rename` over `final_name` in the
    /// sink's directory, `fsync` of the directory, then `lstat` of the
    /// result. A destination that is a directory answers `IsADirectory`.
    fn commit(self: Box<Self>, final_name: &str, id: &FsIdentity) -> Result<RawEntry, FsIoError>;
}

/// A regular file opened for an export, read by offset (`pread`) so a
/// retried part re-reads its own range.
pub trait SnapshotFile: Send + Sync {
    /// Fewer bytes than `buf` holds only at the end of the file; `Ok(0)` at
    /// or past it.
    fn read_at(&self, buf: &mut [u8], offset: u64) -> Result<usize, FsIoError>;
}

/// What `open_snapshot` hands back: the open file and its `fstat`.
pub struct OpenedSnapshot {
    pub file: Box<dyn SnapshotFile>,
    pub entry: RawEntry,
}

/// Installs one non-recursive watch on `canonical_root` and one on each of
/// `subdirectories` (the readable, non-denied directories the domain walked
/// under `id`), and delivers raw events to `sink` from the watcher's own
/// thread; the sink must never block. Every watch is installed under `id`:
/// the adapter enters the identity before creating the backend, so the
/// backend's own thread inherits it and the kernel refuses what `id` may
/// not read. The root must succeed; a subdirectory that vanished or became
/// unreadable since the walk is skipped.
pub trait Watcher: Send + Sync {
    fn watch(
        &self,
        id: &FsIdentity,
        canonical_root: &str,
        subdirectories: &[String],
        sink: Box<dyn Fn(RawWatchEvent) + Send + Sync>,
    ) -> Result<Box<dyn WatchSubscription>, WatchError>;
}

/// Dropping the subscription removes every watch it holds.
pub trait WatchSubscription: Send + Sync {
    /// One more non-recursive watch, for a directory that appeared inside a
    /// recursive watch after the caller checked it under `id`.
    fn add_directory(&self, id: &FsIdentity, canonical_dir: &str) -> Result<(), WatchError>;
}

pub trait NameResolver: Send + Sync {
    fn user_name(&self, uid: u32) -> Option<String>;
    fn group_name(&self, gid: u32) -> Option<String>;
}
