//! `ListDir`: depth-first pre-order over the port, one directory's entries
//! sorted by name bytes, never descending into a symlink or a denied
//! directory, bounded by an entry cap so a deep tree cannot exhaust memory.

use super::MAX_LIST_ENTRIES;
use super::entry::{Entry, EntryKind, NameCache, RawEntry, build_entry};
use super::error::FilesystemError;
use super::identity::FsIdentity;
use super::path::{DenyList, RequestPath, join_canonical};
use super::ports::{FileSystem, FsIoError};

/// `0` means `1`: the proto reserves it for "the directory itself".
#[must_use]
pub fn effective_depth(depth: u32) -> u32 {
    depth.max(1)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ListingLimits {
    pub max_entries: usize,
}

impl Default for ListingLimits {
    fn default() -> Self {
        Self {
            max_entries: MAX_LIST_ENTRIES,
        }
    }
}

/// What one listing walks: the request root (for the reported paths), its
/// canonical form (for the syscalls and the deny check), the requested
/// depth and the cap.
#[derive(Debug, Clone, Copy)]
pub struct ListingRequest<'a> {
    pub root: &'a RequestPath,
    pub canonical_root: &'a str,
    pub depth: u32,
    pub limits: ListingLimits,
}

/// An entry at relative depth `d` (1 = direct child) is included iff
/// `d <= depth`. The root's own `read_dir` failure is the caller's error; a
/// subdirectory that vanishes or is unreadable mid-walk is listed but not
/// descended.
pub fn walk_listing(
    fs: &dyn FileSystem,
    id: &FsIdentity,
    deny: &DenyList,
    request: ListingRequest<'_>,
    names: &mut NameCache<'_>,
) -> Result<Vec<Entry>, FilesystemError> {
    let children = fs
        .read_dir(id, request.canonical_root)
        .map_err(|error| FilesystemError::from_io("read_dir", error))?;
    let mut walk = Walk {
        fs,
        id,
        deny,
        limits: request.limits,
        names,
        entries: Vec::new(),
    };
    walk.push_children(
        children,
        request.root,
        request.canonical_root,
        effective_depth(request.depth),
    )?;
    Ok(walk.entries)
}

struct Walk<'a, 'n> {
    fs: &'a dyn FileSystem,
    id: &'a FsIdentity,
    deny: &'a DenyList,
    limits: ListingLimits,
    names: &'a mut NameCache<'n>,
    entries: Vec<Entry>,
}

impl Walk<'_, '_> {
    fn push_children(
        &mut self,
        mut children: Vec<RawEntry>,
        request_dir: &RequestPath,
        canonical_dir: &str,
        remaining: u32,
    ) -> Result<(), FilesystemError> {
        children.sort_unstable_by(|left, right| left.name.cmp(&right.name));
        for child in children {
            if self.entries.len() >= self.limits.max_entries {
                return Err(FilesystemError::TooManyEntries {
                    max: self.limits.max_entries,
                });
            }
            let child_request = request_dir.join(&child.name);
            let child_canonical = join_canonical(canonical_dir, &child.name);
            let descend = child.kind == EntryKind::Directory
                && remaining > 1
                && !self.deny.is_denied(&child_canonical);
            let mut entry = build_entry(child, &child_request, self.names);
            entry.metadata = self
                .fs
                .read_metadata(self.id, &child_canonical)
                .unwrap_or_default();
            self.entries.push(entry);
            if descend {
                self.descend(&child_request, &child_canonical, remaining - 1)?;
            }
        }
        Ok(())
    }

    fn descend(
        &mut self,
        request_dir: &RequestPath,
        canonical_dir: &str,
        remaining: u32,
    ) -> Result<(), FilesystemError> {
        match self.fs.read_dir(self.id, canonical_dir) {
            Ok(children) => self.push_children(children, request_dir, canonical_dir, remaining),
            Err(FsIoError::NotFound | FsIoError::NotADirectory | FsIoError::PermissionDenied) => {
                Ok(())
            }
            Err(other) => Err(FilesystemError::from_io("read_dir", other)),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::filesystem::fake::{FakeFileSystem, NumericNames, user};

    fn tree() -> FakeFileSystem {
        let fs = FakeFileSystem::new();
        fs.add_dir("/home/user/m3");
        fs.add_file("/home/user/m3/big.bin", &[0; 10]);
        fs.add_file("/home/user/m3/hola.txt", b"hola");
        fs.add_dir("/home/user/m3/many");
        fs.add_file("/home/user/m3/many/f01.txt", b"1");
        fs.add_file("/home/user/m3/many/f00.txt", b"0");
        fs.add_dir("/home/user/m3/many/deep");
        fs.add_file("/home/user/m3/many/deep/x", b"x");
        fs.add_dir("/etc");
        fs.add_file("/etc/passwd", b"root:x:0:0");
        fs.add_symlink("/home/user/m3/sys", "/etc");
        fs.add_symlink("/home/user/m3/zeta", "big.bin");
        fs
    }

    fn list(
        fs: &FakeFileSystem,
        depth: u32,
        max_entries: usize,
    ) -> Result<Vec<String>, FilesystemError> {
        let root = RequestPath::parse("/home/user/m3", "/home/user").unwrap();
        let deny = DenyList::default();
        let mut names = NameCache::new(&NumericNames);
        walk_listing(
            fs,
            &user(),
            &deny,
            ListingRequest {
                root: &root,
                canonical_root: "/home/user/m3",
                depth,
                limits: ListingLimits { max_entries },
            },
            &mut names,
        )
        .map(|entries| entries.into_iter().map(|entry| entry.path).collect())
    }

    #[test]
    fn depth_zero_and_one_list_direct_children_sorted() {
        let fs = tree();
        let expected = vec![
            "/home/user/m3/big.bin",
            "/home/user/m3/hola.txt",
            "/home/user/m3/many",
            "/home/user/m3/sys",
            "/home/user/m3/zeta",
        ];
        assert_eq!(list(&fs, 0, 100).unwrap(), expected);
        assert_eq!(list(&fs, 1, 100).unwrap(), expected);
    }

    #[test]
    fn deeper_levels_follow_pre_order_without_symlinks() {
        let fs = tree();
        assert_eq!(
            list(&fs, 2, 100).unwrap(),
            vec![
                "/home/user/m3/big.bin",
                "/home/user/m3/hola.txt",
                "/home/user/m3/many",
                "/home/user/m3/many/deep",
                "/home/user/m3/many/f00.txt",
                "/home/user/m3/many/f01.txt",
                "/home/user/m3/sys",
                "/home/user/m3/zeta",
            ]
        );
        let three = list(&fs, 3, 100).unwrap();
        assert!(three.contains(&"/home/user/m3/many/deep/x".to_owned()));
        assert!(!three.iter().any(|path| path.contains("passwd")));
    }

    #[test]
    fn symlinked_directory_is_reported_as_a_symlink() {
        let fs = tree();
        let root = RequestPath::parse("/home/user/m3", "/home/user").unwrap();
        let mut names = NameCache::new(&NumericNames);
        let entries = walk_listing(
            &fs,
            &user(),
            &DenyList::default(),
            ListingRequest {
                root: &root,
                canonical_root: "/home/user/m3",
                depth: 5,
                limits: ListingLimits::default(),
            },
            &mut names,
        )
        .unwrap();
        let sys = entries.iter().find(|entry| entry.name == "sys").unwrap();
        assert_eq!(sys.kind, EntryKind::Symlink);
        assert_eq!(sys.symlink_target.as_deref(), Some("/etc"));
        assert_eq!(sys.permissions, "lrwxrwxrwx");
    }

    #[test]
    fn denied_subtrees_are_named_but_not_descended() {
        let fs = FakeFileSystem::new();
        fs.add_dir("/etc");
        fs.add_file("/etc/passwd", b"x");
        fs.add_dir("/home");
        let root = RequestPath::parse("/", "/home/user").unwrap();
        let mut names = NameCache::new(&NumericNames);
        let entries = walk_listing(
            &fs,
            &user(),
            &DenyList::default(),
            ListingRequest {
                root: &root,
                canonical_root: "/",
                depth: 3,
                limits: ListingLimits::default(),
            },
            &mut names,
        )
        .unwrap();
        let paths: Vec<&str> = entries.iter().map(|entry| entry.path.as_str()).collect();
        assert_eq!(paths, vec!["/etc", "/home"]);
    }

    #[test]
    fn entry_cap_fails_the_whole_listing() {
        let fs = tree();
        assert_eq!(
            list(&fs, 2, 3),
            Err(FilesystemError::TooManyEntries { max: 3 })
        );
        assert_eq!(list(&fs, 1, 5).unwrap().len(), 5);
    }

    #[test]
    fn unreadable_subdirectory_is_listed_but_skipped() {
        let fs = tree();
        fs.set_unreadable("/home/user/m3/many");
        let two = list(&fs, 2, 100).unwrap();
        assert!(two.contains(&"/home/user/m3/many".to_owned()));
        assert!(
            !two.iter()
                .any(|path| path.starts_with("/home/user/m3/many/"))
        );
    }

    #[test]
    fn unreadable_root_is_the_callers_error() {
        let fs = tree();
        fs.set_unreadable("/home/user/m3");
        assert_eq!(list(&fs, 1, 100), Err(FilesystemError::PermissionDenied));
    }

    #[test]
    fn effective_depth_floors_at_one() {
        assert_eq!(effective_depth(0), 1);
        assert_eq!(effective_depth(1), 1);
        assert_eq!(effective_depth(7), 7);
    }
}
