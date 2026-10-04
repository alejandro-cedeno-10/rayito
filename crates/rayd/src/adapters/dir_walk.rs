//! Opening a directory one component at a time with `O_PATH | O_NOFOLLOW`,
//! never following a symlink in any of them, so the directory a caller
//! acts on is exactly the one its path named when it was checked. `rayd`
//! runs as root (its filesystem identity lowered per call) while much of
//! what it touches — `/home/user`, `/tmp`, every mount root — is writable
//! by uid 1000, which can swap any component for a symlink between a
//! check and the use: a path handed to `open(2)`, `mkdir(2)` or `mount(2)`
//! is resolved again, and each component may then lead somewhere else.
//! Every step here is relative to the descriptor of the previous one, so
//! renaming or replacing an ancestor after it was opened cannot redirect
//! the rest of the walk; the caller then runs `openat`, `fstatat`,
//! `unlinkat`, `renameat` or `mkdirat` against the descriptor it gets
//! back (`mountpoint` for both mount features, `std_filesystem` for every
//! filesystem RPC).

use std::os::fd::{AsFd, OwnedFd};
use std::path::Path;

use nix::errno::Errno;
use nix::fcntl::{AtFlags, OFlag, openat};
use nix::sys::stat::{Mode, SFlag, fstatat, mkdirat};

/// Each walk step: `O_PATH` (no read permission and no FUSE request
/// needed, only search permission on the parent), `O_DIRECTORY` +
/// `O_NOFOLLOW` (a symlink fails with `ENOTDIR`/`ELOOP` instead of being
/// followed), `O_CLOEXEC` (never leaks into a child).
const DIR_STEP_FLAGS: OFlag = OFlag::O_PATH
    .union(OFlag::O_DIRECTORY)
    .union(OFlag::O_NOFOLLOW)
    .union(OFlag::O_CLOEXEC);

/// Why a walk stopped.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum WalkError {
    /// A component is a symlink: swapped in after the caller resolved the
    /// path, or a path that was never resolved.
    Symlink,
    /// A component exists and is neither a directory nor a symlink.
    NotADirectory,
    /// Any other `openat`/`mkdirat` failure (`ENOENT`, `EACCES`, `EINVAL`
    /// for an interior NUL...).
    Io(Errno),
}

/// What a missing component means for the walk.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum MissingDir {
    /// `Io(ENOENT)`.
    Fail,
    /// `mkdirat` with this mode (minus the umask), then the same
    /// `O_NOFOLLOW` open, so a swap between the two calls is still caught.
    Create(u32),
}

/// `relative` (components separated by `/`, empty ones ignored) opened
/// beneath `root`, where `root` itself is opened with the same flags (its
/// own last component is not followed either).
pub(crate) fn open_dir_beneath(
    root: &Path,
    relative: &str,
    missing: MissingDir,
) -> Result<OwnedFd, WalkError> {
    let start =
        openat(nix::fcntl::AT_FDCWD, root, DIR_STEP_FLAGS, Mode::empty()).map_err(WalkError::Io)?;
    walk_from(start, relative, missing)
}

/// Continues a walk from an already opened directory.
pub(crate) fn walk_from(
    start: OwnedFd,
    relative: &str,
    missing: MissingDir,
) -> Result<OwnedFd, WalkError> {
    let mut current = start;
    for component in relative.split('/').filter(|part| !part.is_empty()) {
        current = match (open_step(&current, component), missing) {
            (Err(WalkError::Io(Errno::ENOENT)), MissingDir::Create(mode)) => {
                make_dir_step(&current, component, mode)?;
                open_step(&current, component)?
            }
            (other, _) => other?,
        };
    }
    Ok(current)
}

/// One component of a walk, opened beneath `parent`.
pub(crate) fn open_step(parent: &OwnedFd, name: &str) -> Result<OwnedFd, WalkError> {
    openat(parent.as_fd(), name, DIR_STEP_FLAGS, Mode::empty()).map_err(|errno| match errno {
        Errno::ENOTDIR | Errno::ELOOP => classify_non_directory(parent, name),
        other => WalkError::Io(other),
    })
}

/// `EEXIST` is fine: a racing creator (or the sandbox) made it first, and
/// the `O_NOFOLLOW` open that follows decides whether it is usable.
fn make_dir_step(parent: &OwnedFd, name: &str, mode: u32) -> Result<(), WalkError> {
    match mkdirat(parent.as_fd(), name, Mode::from_bits_truncate(mode)) {
        Ok(()) | Err(Errno::EEXIST) => Ok(()),
        Err(errno) => Err(WalkError::Io(errno)),
    }
}

/// `O_DIRECTORY | O_NOFOLLOW` reports a symlink and a regular file alike;
/// an `lstat` of the entry tells them apart.
fn classify_non_directory(parent: &OwnedFd, name: &str) -> WalkError {
    match fstatat(parent.as_fd(), name, AtFlags::AT_SYMLINK_NOFOLLOW) {
        Ok(stat) if SFlag::from_bits_truncate(stat.st_mode) & SFlag::S_IFMT == SFlag::S_IFLNK => {
            WalkError::Symlink
        }
        _ => WalkError::NotADirectory,
    }
}

#[cfg(test)]
mod tests {
    use std::os::unix::fs::symlink;

    use super::*;

    #[test]
    fn a_walk_opens_nested_directories_and_creates_missing_ones() {
        let root = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(root.path().join("a/b")).unwrap();
        assert!(open_dir_beneath(root.path(), "/a/b", MissingDir::Fail).is_ok());
        assert_eq!(
            open_dir_beneath(root.path(), "a/c", MissingDir::Fail).err(),
            Some(WalkError::Io(Errno::ENOENT))
        );
        assert!(open_dir_beneath(root.path(), "a/c/d", MissingDir::Create(0o755)).is_ok());
        assert!(root.path().join("a/c/d").is_dir());
    }

    #[test]
    fn a_symlinked_component_is_reported_and_never_followed() {
        let root = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(root.path().join("target/inner")).unwrap();
        std::fs::create_dir_all(root.path().join("home")).unwrap();
        symlink(root.path().join("target"), root.path().join("home/link")).unwrap();
        for relative in ["home/link", "home/link/inner", "home/link/new"] {
            assert_eq!(
                open_dir_beneath(root.path(), relative, MissingDir::Create(0o755)).err(),
                Some(WalkError::Symlink),
                "{relative}"
            );
        }
        assert!(!root.path().join("target/new").exists());
    }

    #[test]
    fn a_regular_file_in_place_of_a_directory_is_not_a_directory() {
        let root = tempfile::tempdir().unwrap();
        std::fs::write(root.path().join("file"), b"").unwrap();
        assert_eq!(
            open_dir_beneath(root.path(), "file/x", MissingDir::Create(0o755)).err(),
            Some(WalkError::NotADirectory)
        );
    }
}
