//! Mount-path shape, shared by every feature that mounts something into
//! the guest filesystem (`s3_mount` today; `efs_volumes` reuses it in its
//! own change). Mirrors the SDK-side rule (`_mount_path.py` /
//! `mount-path.ts`) so a non-SDK or buggy `ConfigureSandbox` caller cannot
//! reach `rayd`'s own `mount(2)`/`create_dir_all` with a path the SDK
//! would have already rejected — `rayd` never trusts the client to have
//! run that check itself (the whole point of re-validating here, §9.2).
//!
//! Pure string checks only: `rayd` never touches the guest filesystem to
//! decide whether a path is *allowed* (only `FuseDevice::attach`'s own
//! `create_dir_all` touches it, afterwards).

/// A sandbox has at most this many mount points between `mounts=` and
/// `volumes=` combined — mirrors the SDK's own `_mount_path.MAX_MOUNTS`.
pub const MAX_MOUNTS: usize = 4;
/// Every mount path must start with one of these; mirrors the SDK's own
/// `_mount_path.ALLOWED_ROOTS`.
pub const ALLOWED_ROOTS: [&str; 2] = ["/mnt/", "/home/user/"];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MountPathError {
    /// Not absolute, not canonical (`.`, `..`, `//` or a trailing `/`), or
    /// outside `ALLOWED_ROOTS`.
    InvalidShape,
    /// The same path named twice in one request, or nested under/over
    /// another mount in it.
    Overlapping,
    /// More than `MAX_MOUNTS` paths in one request.
    TooMany,
}

/// Checks every path's shape, then that none of them overlap each other,
/// in the order they were given — the first offending path is the one a
/// caller should report. Does not check for a path already mounted by a
/// *different* `Configure` call (each feature's own `apply()` does that
/// against its current state); this only rules out what one request alone
/// can never make valid.
pub fn validate_mount_paths<'a>(paths: &[&'a str]) -> Result<(), (&'a str, MountPathError)> {
    if paths.len() > MAX_MOUNTS {
        // No single path is "the" offender; report the first so the
        // caller still has something concrete to point at.
        return Err((paths[0], MountPathError::TooMany));
    }
    for (index, path) in paths.iter().enumerate() {
        if let Err(error) = validate_one(path) {
            return Err((path, error));
        }
        for other in &paths[..index] {
            if overlaps(path, other) {
                return Err((path, MountPathError::Overlapping));
            }
        }
    }
    Ok(())
}

fn validate_one(path: &str) -> Result<(), MountPathError> {
    if !path.starts_with('/') {
        return Err(MountPathError::InvalidShape);
    }
    if !is_canonical(path) {
        return Err(MountPathError::InvalidShape);
    }
    if !ALLOWED_ROOTS
        .iter()
        .any(|root| path.starts_with(root) && path != root.trim_end_matches('/'))
    {
        return Err(MountPathError::InvalidShape);
    }
    Ok(())
}

/// No `.`/`..` segment, no empty segment (`//`), no trailing `/` — the
/// same shape `posixpath.normpath(path) == path` enforces SDK-side.
fn is_canonical(path: &str) -> bool {
    if path.len() > 1 && path.ends_with('/') {
        return false;
    }
    path.split('/')
        .all(|segment| !matches!(segment, "." | ".."))
        && !path.contains("//")
}

fn overlaps(first: &str, second: &str) -> bool {
    let first_dir = format!("{first}/");
    let second_dir = format!("{second}/");
    first == second || first_dir.starts_with(&second_dir) || second_dir.starts_with(&first_dir)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_absolute_canonical_path_under_an_allowed_root_passes() {
        assert_eq!(validate_mount_paths(&["/mnt/data"]), Ok(()));
        assert_eq!(validate_mount_paths(&["/home/user/data"]), Ok(()));
    }

    #[test]
    fn a_relative_path_is_rejected() {
        assert_eq!(
            validate_mount_paths(&["mnt/data"]),
            Err(("mnt/data", MountPathError::InvalidShape))
        );
    }

    #[test]
    fn a_path_outside_the_allowed_roots_is_rejected() {
        assert_eq!(
            validate_mount_paths(&["/etc/cron.d"]),
            Err(("/etc/cron.d", MountPathError::InvalidShape))
        );
        assert_eq!(
            validate_mount_paths(&["/"]),
            Err(("/", MountPathError::InvalidShape))
        );
        assert_eq!(
            validate_mount_paths(&["/mnt"]),
            Err(("/mnt", MountPathError::InvalidShape))
        );
    }

    #[test]
    fn dot_dot_and_double_slash_and_trailing_slash_are_rejected() {
        for path in ["/mnt/../etc", "/mnt//data", "/mnt/data/", "/mnt/./data"] {
            assert_eq!(
                validate_mount_paths(&[path]),
                Err((path, MountPathError::InvalidShape)),
                "{path}"
            );
        }
    }

    #[test]
    fn an_exact_duplicate_overlaps() {
        assert_eq!(
            validate_mount_paths(&["/mnt/data", "/mnt/data"]),
            Err(("/mnt/data", MountPathError::Overlapping))
        );
    }

    #[test]
    fn a_nested_path_overlaps_its_ancestor() {
        assert_eq!(
            validate_mount_paths(&["/mnt/data", "/mnt/data/sub"]),
            Err(("/mnt/data/sub", MountPathError::Overlapping))
        );
    }

    #[test]
    fn distinct_sibling_paths_do_not_overlap() {
        assert_eq!(validate_mount_paths(&["/mnt/a", "/mnt/b"]), Ok(()));
    }

    #[test]
    fn more_than_max_mounts_is_rejected() {
        let paths = ["/mnt/a", "/mnt/b", "/mnt/c", "/mnt/d", "/mnt/e"];
        assert_eq!(
            validate_mount_paths(&paths),
            Err(("/mnt/a", MountPathError::TooMany))
        );
    }
}
