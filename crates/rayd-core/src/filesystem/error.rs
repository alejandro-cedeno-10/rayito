//! Domain errors of the filesystem module. Their `Display` strings are what
//! the gRPC adapter sends to clients and writes to logs, so none of them
//! ever quotes a path, an entry name, a symlink target or file bytes.

use thiserror::Error;

use super::path::PathRejection;
use super::ports::{FsIoError, WatchError};
use crate::lifecycle::HookPhase;

#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum FilesystemError {
    #[error("path {0}")]
    InvalidPath(PathRejection),
    #[error("path is denied by policy")]
    Denied,
    #[error("no such file or directory")]
    NotFound,
    #[error("directory already exists")]
    AlreadyExists,
    #[error("not a directory")]
    NotADirectory,
    #[error("path is a directory")]
    IsADirectory,
    #[error("path is not a regular file")]
    NotARegularFile,
    #[error("path is a symlink")]
    IsSymlink,
    #[error("directory not empty; use recursive")]
    NotEmpty,
    #[error("destination conflicts with an existing entry")]
    DestinationConflict,
    #[error("cross-device move")]
    CrossDevice,
    #[error("permission denied")]
    PermissionDenied,
    #[error("chunk exceeds {max} bytes")]
    ChunkTooLarge { max: usize },
    #[error("mode {0:o} is outside 0..=7777")]
    InvalidMode(u32),
    #[error("first message of a file must carry path")]
    MissingPath,
    #[error("user may only be set on a message with path")]
    UserWithoutPath,
    #[error("mode may only be set on a message with path")]
    ModeWithoutPath,
    #[error("stream carried no files")]
    NoFiles,
    #[error("listing exceeds {max} entries; reduce depth")]
    TooManyEntries { max: usize },
    #[error("max {max} live watches")]
    TooManyWatches { max: usize },
    #[error("inotify watch limit reached")]
    WatchLimitReached,
    #[error("watch queue overflowed; re-open the watch")]
    WatchOverflow,
    #[error("watched directory removed")]
    WatchRootGone,
    /// Fewer than `DISK_RESERVE_BYTES` free before a file's temporary is
    /// created; the message is the status detail the SDK keys on.
    #[error("disk_reserve")]
    DiskReserve,
    /// `ENOSPC` from a write or a commit; same status, other detail.
    #[error("disk_full")]
    DiskFull,
    #[error("running as root is not allowed by this image")]
    RootNotAllowed,
    #[error(
        "only unprivileged accounts of this image may touch files (uid and gid >= 1000, never in group 0)"
    )]
    PrivilegedAccount,
    #[error("unknown user")]
    UnknownUser,
    #[error("user lookup failed: {0}")]
    UserLookupFailed(String),
    #[error("{phase}")]
    NotAcceptingStreams { phase: HookPhase },
    #[error("filesystem operations are not supported on this platform")]
    Unsupported,
    #[error("{operation} failed: {errno}")]
    Io {
        operation: &'static str,
        errno: String,
    },
}

impl FilesystemError {
    /// The default port-to-domain mapping; callers override the variants
    /// whose meaning depends on the operation (a `NotADirectory` from
    /// `realpath` is `NotFound` for `Stat`, a rename conflict is
    /// `DestinationConflict`).
    #[must_use]
    pub fn from_io(operation: &'static str, error: FsIoError) -> Self {
        match error {
            FsIoError::NotFound => Self::NotFound,
            FsIoError::PermissionDenied => Self::PermissionDenied,
            FsIoError::AlreadyExists => Self::AlreadyExists,
            FsIoError::NotADirectory => Self::NotADirectory,
            FsIoError::IsADirectory => Self::IsADirectory,
            FsIoError::NotEmpty => Self::NotEmpty,
            FsIoError::NotARegularFile => Self::NotARegularFile,
            FsIoError::IsSymlink => Self::IsSymlink,
            FsIoError::CrossDevice => Self::CrossDevice,
            FsIoError::NoSpace => Self::DiskFull,
            FsIoError::Unsupported => Self::Unsupported,
            FsIoError::Other { errno } => Self::Io { operation, errno },
        }
    }

    #[must_use]
    pub fn from_watch(error: WatchError) -> Self {
        match error {
            WatchError::NotFound => Self::NotFound,
            WatchError::NotADirectory => Self::NotADirectory,
            WatchError::PermissionDenied => Self::PermissionDenied,
            WatchError::LimitReached => Self::WatchLimitReached,
            WatchError::Unsupported => Self::Unsupported,
            WatchError::Other(errno) => Self::Io {
                operation: "watch",
                errno,
            },
        }
    }
}

impl From<PathRejection> for FilesystemError {
    fn from(rejection: PathRejection) -> Self {
        Self::InvalidPath(rejection)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn messages_never_quote_input() {
        assert_eq!(
            FilesystemError::InvalidPath(PathRejection::ParentReference).to_string(),
            "path contains a parent reference"
        );
        assert_eq!(
            FilesystemError::Denied.to_string(),
            "path is denied by policy"
        );
        assert_eq!(
            FilesystemError::ChunkTooLarge { max: 1_048_576 }.to_string(),
            "chunk exceeds 1048576 bytes"
        );
        assert_eq!(
            FilesystemError::InvalidMode(0o10000).to_string(),
            "mode 10000 is outside 0..=7777"
        );
        assert_eq!(
            FilesystemError::TooManyEntries { max: 10_000 }.to_string(),
            "listing exceeds 10000 entries; reduce depth"
        );
        assert_eq!(
            FilesystemError::NotAcceptingStreams {
                phase: HookPhase::Suspending
            }
            .to_string(),
            "suspending"
        );
        assert_eq!(
            FilesystemError::Io {
                operation: "read",
                errno: "EIO".to_owned()
            }
            .to_string(),
            "read failed: EIO"
        );
    }

    #[test]
    fn port_errors_map_to_domain_errors() {
        assert_eq!(
            FilesystemError::from_io("lstat", FsIoError::NotFound),
            FilesystemError::NotFound
        );
        assert_eq!(
            FilesystemError::from_io(
                "open",
                FsIoError::Other {
                    errno: "EIO".to_owned()
                }
            ),
            FilesystemError::Io {
                operation: "open",
                errno: "EIO".to_owned()
            }
        );
        assert_eq!(
            FilesystemError::from_watch(WatchError::LimitReached),
            FilesystemError::WatchLimitReached
        );
        assert_eq!(
            FilesystemError::from_io("write", FsIoError::NoSpace),
            FilesystemError::DiskFull
        );
        assert_eq!(FilesystemError::DiskFull.to_string(), "disk_full");
        assert_eq!(FilesystemError::DiskReserve.to_string(), "disk_reserve");
    }
}
