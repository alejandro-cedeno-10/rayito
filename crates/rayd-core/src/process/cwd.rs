//! Working-directory precedence and the checks that need no filesystem:
//! whether the directory exists is the adapter's job (`is_dir` right before
//! spawning).

use super::error::{CwdRejection, ProcessError};

/// Request `ProcessConfig.cwd`, else the `workdir` default of the `/run`
/// payload, else the user's home. Empty strings count as absent.
#[must_use]
pub fn resolve_cwd(request: Option<&str>, workdir_default: Option<&str>, home: &str) -> String {
    non_empty(request)
        .or_else(|| non_empty(workdir_default))
        .unwrap_or(home)
        .to_owned()
}

/// Sandbox paths are Linux paths whatever the host running the tests, so
/// "absolute" means a leading slash rather than `Path::is_absolute`.
pub fn validate_cwd(path: &str) -> Result<(), ProcessError> {
    if path.contains('\0') {
        return Err(ProcessError::InvalidCwd(CwdRejection::ContainsNul));
    }
    if !path.starts_with('/') {
        return Err(ProcessError::InvalidCwd(CwdRejection::NotAbsolute));
    }
    Ok(())
}

fn non_empty(value: Option<&str>) -> Option<&str> {
    value.filter(|path| !path.is_empty())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn request_wins_over_workdir_default_over_home() {
        assert_eq!(
            resolve_cwd(Some("/tmp"), Some("/srv"), "/home/user"),
            "/tmp"
        );
        assert_eq!(resolve_cwd(None, Some("/srv"), "/home/user"), "/srv");
        assert_eq!(resolve_cwd(None, None, "/home/user"), "/home/user");
        assert_eq!(resolve_cwd(Some(""), Some(""), "/home/user"), "/home/user");
    }

    #[test]
    fn relative_and_nul_paths_are_rejected() {
        assert_eq!(validate_cwd("/tmp"), Ok(()));
        assert_eq!(
            validate_cwd("tmp"),
            Err(ProcessError::InvalidCwd(CwdRejection::NotAbsolute))
        );
        assert_eq!(
            validate_cwd("/tmp\0x"),
            Err(ProcessError::InvalidCwd(CwdRejection::ContainsNul))
        );
        assert_eq!(
            validate_cwd(""),
            Err(ProcessError::InvalidCwd(CwdRejection::NotAbsolute))
        );
    }
}
