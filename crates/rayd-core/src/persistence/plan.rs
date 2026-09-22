//! What one checkpoint archives (design D3): the requesting user's home,
//! the fixed ignore list applied first and the caller's `exclude` list
//! (validated like request paths, matched by whole components) applied
//! after it. The walk itself lives in the adapter; `should_skip` is the
//! only rule it asks.

use super::PERSIST_EXCLUDE_MAX;
use super::error::PersistenceError;
use crate::filesystem::{FsIdentity, PathRejection, TEMP_PREFIX};

/// Name components skipped at any depth.
pub const IGNORED_NAMES: [&str; 3] = [".cache", "__pycache__", ".ipynb_checkpoints"];
/// Home-relative paths skipped as a whole (each with everything below it).
pub const IGNORED_PATHS: [&str; 3] = [
    ".local/share/jupyter/runtime",
    ".ipython/profile_default/history.sqlite",
    ".ipython/profile_default/history.sqlite-journal",
];

/// The fixed D3 ignore list.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct IgnoreRules;

impl IgnoreRules {
    /// `relative` is the `/`-joined path of an entry below the home root.
    #[must_use]
    pub fn matches(self, relative: &str) -> bool {
        let last = relative.rsplit('/').next().unwrap_or(relative);
        if IGNORED_NAMES.contains(&last) || last.starts_with(TEMP_PREFIX) {
            return true;
        }
        IGNORED_PATHS
            .iter()
            .any(|ignored| has_component_prefix(relative, ignored))
    }
}

/// Why one `exclude` entry was refused; never carries the entry.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ExcludeRejection {
    Path(PathRejection),
    Absolute,
    TooLong,
}

impl std::fmt::Display for ExcludeRejection {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Path(rejection) => write!(f, "{rejection}"),
            Self::Absolute => f.write_str("is absolute"),
            Self::TooLong => f.write_str("exceeds 4096 bytes"),
        }
    }
}

pub const EXCLUDE_MAX_BYTES: usize = 4096;

/// The caller's `exclude` entries, normalised to `a/b` form.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ExcludeList {
    entries: Vec<String>,
}

impl ExcludeList {
    pub fn parse<S: AsRef<str>>(raw: &[S]) -> Result<Self, PersistenceError> {
        if raw.len() > PERSIST_EXCLUDE_MAX {
            return Err(PersistenceError::TooManyExcludes {
                max: PERSIST_EXCLUDE_MAX,
            });
        }
        let entries = raw
            .iter()
            .map(|entry| {
                normalise_exclude(entry.as_ref()).map_err(PersistenceError::InvalidExclude)
            })
            .collect::<Result<Vec<_>, _>>()?;
        Ok(Self { entries })
    }

    #[must_use]
    pub fn entries(&self) -> &[String] {
        &self.entries
    }

    #[must_use]
    pub fn matches(&self, relative: &str) -> bool {
        self.entries
            .iter()
            .any(|entry| has_component_prefix(relative, entry))
    }
}

fn normalise_exclude(raw: &str) -> Result<String, ExcludeRejection> {
    if raw.is_empty() {
        return Err(ExcludeRejection::Path(PathRejection::Empty));
    }
    if raw.contains('\0') {
        return Err(ExcludeRejection::Path(PathRejection::ContainsNul));
    }
    if raw.starts_with('/') {
        return Err(ExcludeRejection::Absolute);
    }
    if raw.len() > EXCLUDE_MAX_BYTES {
        return Err(ExcludeRejection::TooLong);
    }
    let mut components = Vec::new();
    for component in raw.split('/') {
        match component {
            "" | "." => {}
            ".." => return Err(ExcludeRejection::Path(PathRejection::ParentReference)),
            other => components.push(other),
        }
    }
    if components.is_empty() {
        return Err(ExcludeRejection::Path(PathRejection::Empty));
    }
    Ok(components.join("/"))
}

fn has_component_prefix(path: &str, prefix: &str) -> bool {
    path == prefix
        || path
            .strip_prefix(prefix)
            .is_some_and(|rest| rest.starts_with('/'))
}

/// One checkpoint or restore, resolved: whose home (identity and the
/// name the archive records as owner) and what to leave out.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ArchivePlan {
    pub identity: FsIdentity,
    pub username: String,
    pub excludes: ExcludeList,
}

impl ArchivePlan {
    #[must_use]
    pub fn new(identity: FsIdentity, username: String, excludes: ExcludeList) -> Self {
        Self {
            identity,
            username,
            excludes,
        }
    }

    /// The home root the archive is relative to.
    #[must_use]
    pub fn root(&self) -> &str {
        &self.identity.home
    }

    /// The single rule the walk asks per entry, on its home-relative path.
    #[must_use]
    pub fn should_skip(&self, relative: &str) -> bool {
        IgnoreRules.matches(relative) || self.excludes.matches(relative)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn plan(excludes: &[&str]) -> ArchivePlan {
        ArchivePlan::new(
            FsIdentity {
                uid: 1000,
                gid: 1000,
                home: "/home/user".to_owned(),
            },
            "user".to_owned(),
            ExcludeList::parse(excludes).unwrap(),
        )
    }

    #[test]
    fn ignore_list_matches_names_at_any_depth_and_fixed_paths() {
        let rules = IgnoreRules;
        assert!(rules.matches(".cache"));
        assert!(rules.matches("proj/.cache"));
        assert!(rules.matches("a/b/__pycache__"));
        assert!(rules.matches("nb/.ipynb_checkpoints"));
        assert!(rules.matches(".rayito-tmp-abc"));
        assert!(rules.matches("data/.rayito-tmp-xyz"));
        assert!(rules.matches(".local/share/jupyter/runtime"));
        assert!(rules.matches(".ipython/profile_default/history.sqlite"));
        assert!(rules.matches(".ipython/profile_default/history.sqlite-journal"));
        assert!(!rules.matches(".local/share/jupyter/runtime2"));
        assert!(!rules.matches(".local/share/jupyter"));
        assert!(!rules.matches("cache"));
        assert!(!rules.matches("data/blob.bin"));
    }

    #[test]
    fn excludes_match_whole_components() {
        let plan = plan(&["data/raw", "skipme", "./nested//deep/"]);
        assert!(plan.should_skip("data/raw"));
        assert!(plan.should_skip("data/raw/x.bin"));
        assert!(!plan.should_skip("data/raw2"));
        assert!(!plan.should_skip("data"));
        assert!(plan.should_skip("skipme/a"));
        assert!(plan.should_skip("nested/deep/file"));
        assert!(!plan.should_skip("nested"));
        assert_eq!(
            plan.excludes.entries(),
            ["data/raw", "skipme", "nested/deep"]
        );
        assert_eq!(plan.root(), "/home/user");
    }

    #[test]
    fn exclude_validation_follows_request_path_rules() {
        assert_eq!(
            ExcludeList::parse(&["../etc"]).unwrap_err(),
            PersistenceError::InvalidExclude(ExcludeRejection::Path(
                PathRejection::ParentReference
            ))
        );
        assert_eq!(
            ExcludeList::parse(&["/abs"]).unwrap_err(),
            PersistenceError::InvalidExclude(ExcludeRejection::Absolute)
        );
        assert_eq!(
            ExcludeList::parse(&[""]).unwrap_err(),
            PersistenceError::InvalidExclude(ExcludeRejection::Path(PathRejection::Empty))
        );
        assert_eq!(
            ExcludeList::parse(&["."]).unwrap_err(),
            PersistenceError::InvalidExclude(ExcludeRejection::Path(PathRejection::Empty))
        );
        assert_eq!(
            ExcludeList::parse(&["a\0b"]).unwrap_err(),
            PersistenceError::InvalidExclude(ExcludeRejection::Path(PathRejection::ContainsNul))
        );
        assert_eq!(
            ExcludeList::parse(&["a".repeat(4097)]).unwrap_err(),
            PersistenceError::InvalidExclude(ExcludeRejection::TooLong)
        );
        let too_many: Vec<String> = (0..65).map(|index| format!("d{index}")).collect();
        assert_eq!(
            ExcludeList::parse(&too_many).unwrap_err(),
            PersistenceError::TooManyExcludes { max: 64 }
        );
        assert!(ExcludeList::parse(&too_many[..64]).is_ok());
        assert_eq!(ExcludeRejection::Absolute.to_string(), "is absolute");
        assert_eq!(
            ExcludeRejection::Path(PathRejection::ParentReference).to_string(),
            "contains a parent reference"
        );
    }
}
