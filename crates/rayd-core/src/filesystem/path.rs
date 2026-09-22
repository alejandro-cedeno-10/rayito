//! Request paths as pure `/`-separated strings (never `std::path`, whose
//! rules differ on the Windows host that runs these tests) and the deny list
//! that is applied to canonical paths, never to the request string.

use std::fmt;

use super::error::FilesystemError;

/// Trees the agent never touches on behalf of a request, whatever the
/// identity: kernel interfaces, the image's system files and the agent's
/// own runtime directory.
pub const DEFAULT_DENIED_PREFIXES: [&str; 7] = [
    "/proc",
    "/sys",
    "/dev",
    "/run/rayito",
    "/etc",
    "/usr",
    "/opt/rayito",
];
pub const RAYD_BINARY: &str = "/usr/local/bin/rayd";

/// Why a request path was refused before any syscall; never carries the
/// path itself.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PathRejection {
    Empty,
    ContainsNul,
    ParentReference,
}

impl fmt::Display for PathRejection {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Empty => "is empty",
            Self::ContainsNul => "contains a NUL byte",
            Self::ParentReference => "contains a parent reference",
        })
    }
}

/// An absolute, lexically normalised request path: no repeated `/`, no `.`
/// component, no trailing `/` (except the root) and no `..`.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct RequestPath(String);

impl RequestPath {
    /// A relative path is joined to `home` first (E2B behaviour). `..` is
    /// refused rather than resolved: lexical resolution is unsafe through
    /// symlinks and no client needs it.
    pub fn parse(raw: &str, home: &str) -> Result<Self, PathRejection> {
        if raw.is_empty() {
            return Err(PathRejection::Empty);
        }
        if raw.contains('\0') {
            return Err(PathRejection::ContainsNul);
        }
        let joined = if raw.starts_with('/') {
            raw.to_owned()
        } else {
            format!("{home}/{raw}")
        };
        let mut components = Vec::new();
        for component in joined.split('/') {
            match component {
                "" | "." => {}
                ".." => return Err(PathRejection::ParentReference),
                other => components.push(other),
            }
        }
        Ok(Self(format!("/{}", components.join("/"))))
    }

    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }

    #[must_use]
    pub fn is_root(&self) -> bool {
        self.0 == "/"
    }

    /// `None` for the root.
    #[must_use]
    pub fn parent(&self) -> Option<Self> {
        if self.is_root() {
            return None;
        }
        let cut = self.0.rfind('/').unwrap_or(0);
        Some(Self(if cut == 0 {
            "/".to_owned()
        } else {
            self.0[..cut].to_owned()
        }))
    }

    /// The final component, `/` for the root.
    #[must_use]
    pub fn name(&self) -> &str {
        if self.is_root() {
            return "/";
        }
        self.0.rsplit('/').next().unwrap_or("/")
    }

    /// Appends an already normalised relative name (`a` or `a/b`).
    #[must_use]
    pub fn join(&self, relative: &str) -> Self {
        Self(join_canonical(&self.0, relative))
    }
}

impl fmt::Display for RequestPath {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

/// `dir + "/" + name` without doubling the root's slash.
#[must_use]
pub fn join_canonical(dir: &str, name: &str) -> String {
    if dir == "/" {
        format!("/{name}")
    } else {
        format!("{dir}/{name}")
    }
}

/// `(parent, name)` of a canonical absolute path; the parent of a top-level
/// entry is `/`.
#[must_use]
pub fn split_canonical(canonical: &str) -> (&str, &str) {
    match canonical.rfind('/') {
        Some(0) => ("/", &canonical[1..]),
        Some(cut) => (&canonical[..cut], &canonical[cut + 1..]),
        None => ("/", canonical),
    }
}

/// Component-wise prefixes refused on canonical paths: `/etc` denies `/etc`
/// and `/etc/passwd` but not `/etcetera`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DenyList {
    prefixes: Vec<String>,
}

impl DenyList {
    /// The defaults, the agent's install path and `extra` entries (the
    /// canonical path of the running executable, in production).
    #[must_use]
    pub fn new(extra: impl IntoIterator<Item = String>) -> Self {
        let mut prefixes: Vec<String> = DEFAULT_DENIED_PREFIXES
            .iter()
            .map(|prefix| (*prefix).to_owned())
            .collect();
        prefixes.push(RAYD_BINARY.to_owned());
        prefixes.extend(
            extra
                .into_iter()
                .filter_map(|entry| RequestPath::parse(&entry, "/").ok())
                .map(|path| path.0),
        );
        Self { prefixes }
    }

    #[must_use]
    pub fn prefixes(&self) -> &[String] {
        &self.prefixes
    }

    #[must_use]
    pub fn is_denied(&self, canonical: &str) -> bool {
        self.prefixes
            .iter()
            .any(|prefix| has_component_prefix(canonical, prefix))
    }

    pub fn check(&self, canonical: &str) -> Result<(), FilesystemError> {
        if self.is_denied(canonical) {
            Err(FilesystemError::Denied)
        } else {
            Ok(())
        }
    }
}

impl Default for DenyList {
    fn default() -> Self {
        Self::new(Vec::new())
    }
}

fn has_component_prefix(path: &str, prefix: &str) -> bool {
    path == prefix
        || path
            .strip_prefix(prefix)
            .is_some_and(|rest| rest.starts_with('/'))
}

#[cfg(test)]
mod tests {
    use super::*;

    const HOME: &str = "/home/user";

    fn parse(raw: &str) -> Result<String, PathRejection> {
        RequestPath::parse(raw, HOME).map(|path| path.0)
    }

    #[test]
    fn absolute_paths_are_kept_and_normalised() {
        assert_eq!(parse("/tmp/a.txt").unwrap(), "/tmp/a.txt");
        assert_eq!(parse("//tmp///a//").unwrap(), "/tmp/a");
        assert_eq!(parse("/tmp/./a/.").unwrap(), "/tmp/a");
        assert_eq!(parse("/").unwrap(), "/");
        assert_eq!(parse("///").unwrap(), "/");
    }

    #[test]
    fn relative_paths_resolve_against_home() {
        assert_eq!(parse("data.csv").unwrap(), "/home/user/data.csv");
        assert_eq!(parse("./m3/rel.txt").unwrap(), "/home/user/m3/rel.txt");
        assert_eq!(parse(".").unwrap(), "/home/user");
        assert_eq!(
            RequestPath::parse("x", "").map(|path| path.0).unwrap(),
            "/x"
        );
    }

    #[test]
    fn empty_nul_and_parent_references_are_refused() {
        assert_eq!(parse(""), Err(PathRejection::Empty));
        assert_eq!(parse("/tmp/a\0b"), Err(PathRejection::ContainsNul));
        assert_eq!(parse("/tmp/../etc"), Err(PathRejection::ParentReference));
        assert_eq!(parse(".."), Err(PathRejection::ParentReference));
        assert_eq!(parse("a/../b"), Err(PathRejection::ParentReference));
        assert_eq!(
            PathRejection::ParentReference.to_string(),
            "contains a parent reference"
        );
    }

    #[test]
    fn name_parent_and_join() {
        let path = RequestPath::parse("/home/user/m3/big.bin", HOME).unwrap();
        assert_eq!(path.name(), "big.bin");
        assert_eq!(path.parent().unwrap().as_str(), "/home/user/m3");
        assert_eq!(path.join("x/y").as_str(), "/home/user/m3/big.bin/x/y");
        let top = RequestPath::parse("/tmp", HOME).unwrap();
        assert_eq!(top.parent().unwrap().as_str(), "/");
        let root = RequestPath::parse("/", HOME).unwrap();
        assert!(root.is_root());
        assert_eq!(root.name(), "/");
        assert_eq!(root.parent(), None);
        assert_eq!(root.join("etc").as_str(), "/etc");
        assert_eq!(split_canonical("/a/b"), ("/a", "b"));
        assert_eq!(split_canonical("/b"), ("/", "b"));
    }

    #[test]
    fn deny_list_matches_whole_components() {
        let deny = DenyList::default();
        assert!(deny.is_denied("/etc"));
        assert!(deny.is_denied("/etc/passwd"));
        assert!(deny.is_denied("/proc/1/mem"));
        assert!(deny.is_denied("/run/rayito/x"));
        assert!(deny.is_denied("/usr/local/bin/rayd"));
        assert!(!deny.is_denied("/etcetera"));
        assert!(!deny.is_denied("/run/user/1000"));
        assert!(!deny.is_denied("/home/user/etc"));
        assert!(!deny.is_denied("/"));
        assert_eq!(deny.check("/home/user"), Ok(()));
        assert_eq!(deny.check("/sys"), Err(FilesystemError::Denied));
    }

    #[test]
    fn extra_entries_are_normalised_and_appended() {
        let deny = DenyList::new(vec!["/work/target/debug//rayd/".to_owned(), String::new()]);
        assert!(deny.is_denied("/work/target/debug/rayd"));
        assert!(!deny.is_denied("/work/target/debug/rayd-core"));
        assert_eq!(deny.prefixes().len(), DEFAULT_DENIED_PREFIXES.len() + 2);
    }
}
