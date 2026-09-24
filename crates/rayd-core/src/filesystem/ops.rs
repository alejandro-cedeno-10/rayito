//! One RPC = parse the request path, resolve its canonical form under the
//! requesting identity, check the deny list, then call the port. Every rule
//! of the filesystem design runs here, so the host tests exercise all of it
//! against the in-memory fake.

use std::collections::VecDeque;
use std::io::Read;

use super::entry::{Entry, EntryKind, NameCache, RawEntry};
use super::error::FilesystemError;
use super::identity::FsIdentity;
use super::listing::{ListingLimits, ListingRequest, walk_listing};
use super::path::{DenyList, RequestPath, join_canonical, split_canonical};
use super::ports::{FileSystem, FsIoError, NameResolver, SnapshotFile, WriteSink};
use super::write::check_disk_reserve;
use super::{DEFAULT_DIR_MODE, MAX_WATCH_DIRECTORIES};

pub struct FilesystemOps<'a> {
    fs: &'a dyn FileSystem,
    deny: &'a DenyList,
    names: &'a dyn NameResolver,
}

/// An open temp file plus what the commit needs: the final name inside
/// the sink's directory and the request path to report.
pub struct WriteTarget {
    pub sink: Box<dyn WriteSink>,
    pub path: RequestPath,
    pub name: String,
}

/// Where an import lands, resolved like a `Write` destination: the request
/// path to report, its would-be canonical form (deny-checked) and the
/// directory and final name the temp file and the rename use.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ImportDestination {
    pub path: RequestPath,
    pub canonical: String,
    pub dir: String,
    pub name: String,
}

/// The source of an export: the open file, its entry as measured by
/// `fstat` when it was opened, and the request path.
pub struct ExportSource {
    pub file: Box<dyn SnapshotFile>,
    pub entry: Entry,
    pub path: RequestPath,
}

/// A watch root plus, for a recursive watch, every directory below it the
/// identity may read and the deny list allows; symlinks are never followed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WatchTarget {
    pub path: RequestPath,
    pub canonical_root: String,
    pub subdirectories: Vec<String>,
}

impl<'a> FilesystemOps<'a> {
    #[must_use]
    pub fn new(fs: &'a dyn FileSystem, deny: &'a DenyList, names: &'a dyn NameResolver) -> Self {
        Self { fs, deny, names }
    }

    /// The entry carries the file's metadata; an unreadable set is empty,
    /// never an error.
    pub fn stat(&self, id: &FsIdentity, raw: &str) -> Result<Entry, FilesystemError> {
        let path = RequestPath::parse(raw, &id.home)?;
        let canonical = self.canonical_existing(id, &path)?;
        let entry = self.fs.lstat(id, &canonical).map_err(lookup_error)?;
        let mut entry = self.entry(entry, &path);
        entry.metadata = self.fs.read_metadata(id, &canonical).unwrap_or_default();
        Ok(entry)
    }

    pub fn list_dir(
        &self,
        id: &FsIdentity,
        raw: &str,
        depth: u32,
        limits: ListingLimits,
    ) -> Result<Vec<Entry>, FilesystemError> {
        let path = RequestPath::parse(raw, &id.home)?;
        let canonical = self.canonical_directory(id, &path)?;
        let mut names = NameCache::new(self.names);
        walk_listing(
            self.fs,
            id,
            self.deny,
            ListingRequest {
                root: &path,
                canonical_root: &canonical,
                depth,
                limits,
            },
            &mut names,
        )
    }

    pub fn make_dir(&self, id: &FsIdentity, raw: &str) -> Result<Entry, FilesystemError> {
        let path = RequestPath::parse(raw, &id.home)?;
        if path.is_root() {
            return Err(FilesystemError::AlreadyExists);
        }
        let canonical = self.canonical_creating(id, &path)?;
        match self.fs.make_dir(id, &canonical, DEFAULT_DIR_MODE) {
            Ok(()) => {}
            Err(FsIoError::AlreadyExists) => {
                return Err(self.existing_kind_error(id, &canonical));
            }
            Err(error) => return Err(FilesystemError::from_io("mkdir", error)),
        }
        let raw = self
            .fs
            .lstat(id, &canonical)
            .map_err(|error| FilesystemError::from_io("lstat", error))?;
        Ok(self.entry(raw, &path))
    }

    pub fn rename(
        &self,
        id: &FsIdentity,
        from_raw: &str,
        to_raw: &str,
    ) -> Result<Entry, FilesystemError> {
        let from = RequestPath::parse(from_raw, &id.home)?;
        let to = RequestPath::parse(to_raw, &id.home)?;
        if from.is_root() || to.is_root() {
            return Err(FilesystemError::Denied);
        }
        let from_canonical = self.canonical_existing(id, &from)?;
        let to_canonical = self.canonical_existing(id, &to)?;
        self.fs.lstat(id, &from_canonical).map_err(lookup_error)?;
        self.fs
            .rename(id, &from_canonical, &to_canonical)
            .map_err(rename_error)?;
        let raw = self
            .fs
            .lstat(id, &to_canonical)
            .map_err(|error| FilesystemError::from_io("lstat", error))?;
        Ok(self.entry(raw, &to))
    }

    pub fn remove(
        &self,
        id: &FsIdentity,
        raw: &str,
        recursive: bool,
    ) -> Result<(), FilesystemError> {
        let path = RequestPath::parse(raw, &id.home)?;
        if path.is_root() {
            return Err(FilesystemError::Denied);
        }
        let canonical = self.canonical_existing(id, &path)?;
        let entry = self.fs.lstat(id, &canonical).map_err(lookup_error)?;
        self.fs
            .remove(id, &canonical, entry.kind, recursive)
            .map_err(|error| FilesystemError::from_io("remove", error))
    }

    pub fn prepare_read(
        &self,
        id: &FsIdentity,
        raw: &str,
    ) -> Result<Box<dyn Read + Send>, FilesystemError> {
        let path = RequestPath::parse(raw, &id.home)?;
        let canonical = self.canonical_existing(id, &path)?;
        self.fs.open_read(id, &canonical).map_err(lookup_error)
    }

    /// Resolves the destination (parents may not exist yet), checks the
    /// deny list on the would-be canonical path, then the disk reserve of
    /// the destination filesystem, and only then opens the temp file.
    pub fn write_target(
        &self,
        id: &FsIdentity,
        raw: &str,
        mode: u32,
    ) -> Result<WriteTarget, FilesystemError> {
        let path = RequestPath::parse(raw, &id.home)?;
        if path.is_root() {
            return Err(FilesystemError::IsADirectory);
        }
        let canonical = self.canonical_creating(id, &path)?;
        let (dir, name) = split_canonical(&canonical);
        let free = self
            .fs
            .free_bytes(id, dir)
            .map_err(|error| FilesystemError::from_io("statvfs", error))?;
        check_disk_reserve(free)?;
        let sink = self
            .fs
            .begin_write(id, dir, mode)
            .map_err(|error| FilesystemError::from_io("begin_write", error))?;
        Ok(WriteTarget {
            sink,
            name: name.to_owned(),
            path,
        })
    }

    /// Resolves an import destination the way `write_target` does (parents
    /// may not exist yet, deny list on the would-be canonical path) and
    /// refuses an existing directory as the final component. Called when
    /// the import is accepted and again right before its temp file is
    /// created, so a symlink swapped in meanwhile is seen.
    pub fn import_destination(
        &self,
        id: &FsIdentity,
        raw: &str,
    ) -> Result<ImportDestination, FilesystemError> {
        let path = RequestPath::parse(raw, &id.home)?;
        if path.is_root() {
            return Err(FilesystemError::IsADirectory);
        }
        let canonical = self.canonical_creating(id, &path)?;
        match self.fs.lstat(id, &canonical) {
            Ok(existing) if existing.kind == EntryKind::Directory => {
                return Err(FilesystemError::IsADirectory);
            }
            Ok(_) | Err(FsIoError::NotFound | FsIoError::NotADirectory) => {}
            Err(error) => return Err(FilesystemError::from_io("lstat", error)),
        }
        let (dir, name) = split_canonical(&canonical);
        Ok(ImportDestination {
            dir: dir.to_owned(),
            name: name.to_owned(),
            path,
            canonical,
        })
    }

    /// Free bytes of the filesystem an import destination lands on.
    pub fn free_bytes(&self, id: &FsIdentity, dir: &str) -> Result<u64, FilesystemError> {
        self.fs
            .free_bytes(id, dir)
            .map_err(|error| FilesystemError::from_io("statvfs", error))
    }

    /// The temp file of an import, opened after its admit checks passed.
    pub fn begin_import(
        &self,
        id: &FsIdentity,
        destination: &ImportDestination,
        mode: u32,
    ) -> Result<Box<dyn WriteSink>, FilesystemError> {
        self.fs
            .begin_write(id, &destination.dir, mode)
            .map_err(|error| FilesystemError::from_io("begin_write", error))
    }

    /// Opens the export source under `id`: regular files only (a symlink,
    /// a directory or a FIFO is refused without following or blocking),
    /// measured by `fstat` on the open descriptor.
    pub fn export_source(
        &self,
        id: &FsIdentity,
        raw: &str,
    ) -> Result<ExportSource, FilesystemError> {
        let path = RequestPath::parse(raw, &id.home)?;
        let canonical = self.canonical_existing(id, &path)?;
        let opened = self
            .fs
            .open_snapshot(id, &canonical)
            .map_err(lookup_error)?;
        let entry = self.entry(opened.entry, &path);
        Ok(ExportSource {
            file: opened.file,
            entry,
            path,
        })
    }

    /// The would-be canonical target of a request path under `id`, for the
    /// read-after-upload barrier; `None` whenever the path could not be
    /// resolved or is denied (the operation itself then reports why).
    #[must_use]
    pub fn barrier_target(&self, id: &FsIdentity, raw: &str) -> Option<(RequestPath, String)> {
        let path = RequestPath::parse(raw, &id.home).ok()?;
        let canonical = self.canonical_creating(id, &path).ok()?;
        Some((path, canonical))
    }

    /// The watch root must be a readable directory under the requesting
    /// identity. A recursive watch walks the tree here, under that
    /// identity, so a subdirectory the user cannot read or that the deny
    /// list covers is simply not watched; a tree over
    /// `MAX_WATCH_DIRECTORIES` is refused as `WatchLimitReached`.
    pub fn prepare_watch(
        &self,
        id: &FsIdentity,
        raw: &str,
        recursive: bool,
    ) -> Result<WatchTarget, FilesystemError> {
        let path = RequestPath::parse(raw, &id.home)?;
        let canonical_root = self.canonical_directory(id, &path)?;
        let children = self
            .fs
            .read_dir(id, &canonical_root)
            .map_err(|error| FilesystemError::from_io("read_dir", error))?;
        let subdirectories = if recursive {
            self.watchable_subdirectories(id, &canonical_root, children)?
        } else {
            Vec::new()
        };
        Ok(WatchTarget {
            path,
            canonical_root,
            subdirectories,
        })
    }

    /// Whether a directory that appeared inside a recursive watch may be
    /// followed: not denied, a directory itself (never a symlink to one)
    /// and readable under `id`.
    #[must_use]
    pub fn is_watchable_directory(&self, id: &FsIdentity, canonical: &str) -> bool {
        if self.deny.is_denied(canonical) {
            return false;
        }
        match self.fs.lstat(id, canonical) {
            Ok(entry) if entry.kind == EntryKind::Directory => {
                self.fs.read_dir(id, canonical).is_ok()
            }
            _ => false,
        }
    }

    /// Breadth-first from the root's children: a directory entry (not a
    /// symlink) that is not denied and whose `read_dir` succeeds under
    /// `id` is watched and descended; anything else is skipped.
    fn watchable_subdirectories(
        &self,
        id: &FsIdentity,
        canonical_root: &str,
        root_children: Vec<RawEntry>,
    ) -> Result<Vec<String>, FilesystemError> {
        let mut subdirectories = Vec::new();
        let mut pending = VecDeque::from([(canonical_root.to_owned(), root_children)]);
        while let Some((dir, children)) = pending.pop_front() {
            for child in children
                .into_iter()
                .filter(|child| child.kind == EntryKind::Directory)
            {
                let canonical = join_canonical(&dir, &child.name);
                if self.deny.is_denied(&canonical) {
                    continue;
                }
                let Ok(grandchildren) = self.fs.read_dir(id, &canonical) else {
                    continue;
                };
                if subdirectories.len() + 1 >= MAX_WATCH_DIRECTORIES {
                    return Err(FilesystemError::WatchLimitReached);
                }
                subdirectories.push(canonical.clone());
                pending.push_back((canonical, grandchildren));
            }
        }
        Ok(subdirectories)
    }

    /// `lstat` of `name` inside a watched directory, for `include_entry`.
    pub fn entry_in(
        &self,
        id: &FsIdentity,
        root: &RequestPath,
        canonical_root: &str,
        name: &str,
    ) -> Result<Entry, FilesystemError> {
        let raw = self
            .fs
            .lstat(id, &join_canonical(canonical_root, name))
            .map_err(|error| FilesystemError::from_io("lstat", error))?;
        Ok(self.entry(raw, &root.join(name)))
    }

    /// `realpath(parent) + "/" + name`, deny-checked (the request path is
    /// checked lexically first, so a denied tree is refused even when it
    /// does not exist). A missing or non-directory component of the parent
    /// is `NotFound`.
    fn canonical_existing(
        &self,
        id: &FsIdentity,
        path: &RequestPath,
    ) -> Result<String, FilesystemError> {
        self.deny.check(path.as_str())?;
        let Some(parent) = path.parent() else {
            return Ok("/".to_owned());
        };
        let canonical_parent = self
            .fs
            .canonicalize(id, parent.as_str())
            .map_err(realpath_error)?;
        let canonical = join_canonical(&canonical_parent, path.name());
        self.deny.check(&canonical)?;
        Ok(canonical)
    }

    /// `realpath(path)` itself, for roots that are followed on purpose
    /// (`ListDir`, `WatchDir`); must be a directory.
    fn canonical_directory(
        &self,
        id: &FsIdentity,
        path: &RequestPath,
    ) -> Result<String, FilesystemError> {
        self.deny.check(path.as_str())?;
        let canonical = self
            .fs
            .canonicalize(id, path.as_str())
            .map_err(realpath_error)?;
        self.deny.check(&canonical)?;
        let root = self
            .fs
            .lstat(id, &canonical)
            .map_err(|error| FilesystemError::from_io("lstat", error))?;
        if root.kind != EntryKind::Directory {
            return Err(FilesystemError::NotADirectory);
        }
        Ok(canonical)
    }

    /// The deepest existing ancestor canonicalised, the missing components
    /// appended lexically, deny-checked before anything is created.
    fn canonical_creating(
        &self,
        id: &FsIdentity,
        path: &RequestPath,
    ) -> Result<String, FilesystemError> {
        self.deny.check(path.as_str())?;
        let mut missing = vec![path.name().to_owned()];
        let mut cursor = path.parent();
        let base = loop {
            let Some(dir) = cursor else {
                break "/".to_owned();
            };
            match self.fs.canonicalize(id, dir.as_str()) {
                Ok(canonical) => break canonical,
                Err(FsIoError::NotFound) => {
                    missing.push(dir.name().to_owned());
                    cursor = dir.parent();
                }
                Err(error) => return Err(FilesystemError::from_io("realpath", error)),
            }
        };
        let canonical = missing
            .iter()
            .rev()
            .fold(base, |acc, component| join_canonical(&acc, component));
        self.deny.check(&canonical)?;
        Ok(canonical)
    }

    fn existing_kind_error(&self, id: &FsIdentity, canonical: &str) -> FilesystemError {
        match self.fs.lstat(id, canonical) {
            Ok(existing) if existing.kind == EntryKind::Directory => FilesystemError::AlreadyExists,
            Ok(_) => FilesystemError::NotADirectory,
            Err(error) => FilesystemError::from_io("lstat", error),
        }
    }

    fn entry(&self, raw: RawEntry, path: &RequestPath) -> Entry {
        let mut names = NameCache::new(self.names);
        super::entry::build_entry(raw, path, &mut names)
    }
}

/// `lstat`/`open` of `<file>/<name>` fails with `ENOTDIR`, which for a
/// lookup means "no such entry", not "bad argument".
fn lookup_error(error: FsIoError) -> FilesystemError {
    match error {
        FsIoError::NotADirectory => FilesystemError::NotFound,
        other => FilesystemError::from_io("lookup", other),
    }
}

/// `set_metadata` failures: a filesystem without user xattrs and a set that
/// does not fit have their own details; the rest is the default mapping.
#[must_use]
pub fn metadata_error(error: FsIoError) -> FilesystemError {
    match error {
        FsIoError::Unsupported => FilesystemError::MetadataUnsupported,
        FsIoError::NoSpace => FilesystemError::MetadataTooLarge,
        other => FilesystemError::from_io("fsetxattr", other),
    }
}

fn realpath_error(error: FsIoError) -> FilesystemError {
    match error {
        FsIoError::NotFound | FsIoError::NotADirectory => FilesystemError::NotFound,
        other => FilesystemError::from_io("realpath", other),
    }
}

fn rename_error(error: FsIoError) -> FilesystemError {
    match error {
        FsIoError::AlreadyExists
        | FsIoError::NotEmpty
        | FsIoError::IsADirectory
        | FsIoError::NotADirectory => FilesystemError::DestinationConflict,
        FsIoError::CrossDevice => FilesystemError::CrossDevice,
        other => FilesystemError::from_io("rename", other),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::filesystem::fake::{FakeFileSystem, NumericNames, user};
    use crate::filesystem::path::PathRejection;

    struct Fixture {
        fs: FakeFileSystem,
        deny: DenyList,
    }

    impl Fixture {
        fn new() -> Self {
            let fs = FakeFileSystem::new();
            fs.add_dir("/home/user/m3");
            fs.add_file("/home/user/m3/big.bin", &[7; 600]);
            fs.add_dir("/home/user/m3/many");
            fs.add_file("/home/user/m3/many/f00.txt", b"0");
            fs.add_symlink("/home/user/m3/link", "big.bin");
            fs.add_dir("/etc");
            fs.add_file("/etc/passwd", b"root:x:0:0");
            fs.add_symlink("/home/user/m3/sys", "/etc");
            fs.add_other("/home/user/m3/fifo");
            Self {
                fs,
                deny: DenyList::default(),
            }
        }

        fn ops(&self) -> FilesystemOps<'_> {
            FilesystemOps::new(&self.fs, &self.deny, &NumericNames)
        }
    }

    #[test]
    fn stat_reports_files_directories_and_symlinks() {
        let fixture = Fixture::new();
        let ops = fixture.ops();
        let file = ops.stat(&user(), "/home/user/m3/big.bin").unwrap();
        assert_eq!(file.kind, EntryKind::File);
        assert_eq!(file.size, 600);
        assert_eq!(file.path, "/home/user/m3/big.bin");
        let relative = ops.stat(&user(), "m3/big.bin").unwrap();
        assert_eq!(relative.path, "/home/user/m3/big.bin");
        let dir = ops.stat(&user(), "/home/user/m3/many/").unwrap();
        assert_eq!(dir.kind, EntryKind::Directory);
        assert_eq!(dir.permissions, "drwxr-xr-x");
        let link = ops.stat(&user(), "/home/user/m3/link").unwrap();
        assert_eq!(link.kind, EntryKind::Symlink);
        assert_eq!(link.symlink_target.as_deref(), Some("big.bin"));
        let root = ops.stat(&user(), "/").unwrap();
        assert_eq!(root.name, "/");
        assert_eq!(
            ops.stat(&user(), "/home/user/m3/missing"),
            Err(FilesystemError::NotFound)
        );
        assert_eq!(
            ops.stat(&user(), "/home/user/m3/big.bin/inner"),
            Err(FilesystemError::NotFound)
        );
        assert_eq!(
            ops.stat(&user(), "/home/user/m3/../../etc/passwd"),
            Err(FilesystemError::InvalidPath(PathRejection::ParentReference))
        );
    }

    #[test]
    fn deny_list_applies_to_the_canonical_parent() {
        let fixture = Fixture::new();
        let ops = fixture.ops();
        assert_eq!(
            ops.prepare_read(&user(), "/etc/passwd").err(),
            Some(FilesystemError::Denied)
        );
        assert_eq!(
            ops.prepare_read(&user(), "/home/user/m3/sys/passwd").err(),
            Some(FilesystemError::Denied)
        );
        assert_eq!(
            ops.stat(&user(), "/home/user/m3/sys/passwd"),
            Err(FilesystemError::Denied)
        );
        assert_eq!(
            ops.stat(&user(), "/home/user/m3/sys").map(|e| e.kind),
            Ok(EntryKind::Symlink)
        );
        assert_eq!(
            ops.list_dir(&user(), "/home/user/m3/sys", 1, ListingLimits::default()),
            Err(FilesystemError::Denied)
        );
        assert_eq!(
            ops.list_dir(&user(), "/proc", 1, ListingLimits::default()),
            Err(FilesystemError::Denied)
        );
        assert!(
            ops.list_dir(&user(), "/", 1, ListingLimits::default())
                .unwrap()
                .iter()
                .any(|entry| entry.name == "etc")
        );
        assert_eq!(
            ops.write_target(&user(), "/usr/local/bin/rayd", 0o644)
                .err(),
            Some(FilesystemError::Denied)
        );
        assert_eq!(
            ops.make_dir(&user(), "/etc/new").err(),
            Some(FilesystemError::Denied)
        );
        assert_eq!(
            ops.remove(&user(), "/etc/passwd", false),
            Err(FilesystemError::Denied)
        );
        assert_eq!(ops.remove(&user(), "/", true), Err(FilesystemError::Denied));
    }

    #[test]
    fn read_refuses_directories_symlinks_and_other_kinds() {
        let fixture = Fixture::new();
        let ops = fixture.ops();
        let mut bytes = Vec::new();
        ops.prepare_read(&user(), "/home/user/m3/big.bin")
            .unwrap()
            .read_to_end(&mut bytes)
            .unwrap();
        assert_eq!(bytes.len(), 600);
        assert_eq!(
            ops.prepare_read(&user(), "/home/user/m3").err(),
            Some(FilesystemError::IsADirectory)
        );
        assert_eq!(
            ops.prepare_read(&user(), "/home/user/m3/link").err(),
            Some(FilesystemError::IsSymlink)
        );
        assert_eq!(
            ops.prepare_read(&user(), "/home/user/m3/fifo").err(),
            Some(FilesystemError::NotARegularFile)
        );
        assert_eq!(
            ops.prepare_read(&user(), "/home/user/m3/nope").err(),
            Some(FilesystemError::NotFound)
        );
        fixture.fs.set_unreadable("/home/user/m3/big.bin");
        assert_eq!(
            ops.prepare_read(&user(), "/home/user/m3/big.bin").err(),
            Some(FilesystemError::PermissionDenied)
        );
    }

    #[test]
    fn list_dir_rejects_non_directories_and_missing_roots() {
        let fixture = Fixture::new();
        let ops = fixture.ops();
        assert_eq!(
            ops.list_dir(
                &user(),
                "/home/user/m3/big.bin",
                1,
                ListingLimits::default()
            ),
            Err(FilesystemError::NotADirectory)
        );
        assert_eq!(
            ops.list_dir(&user(), "/home/user/m3/nope", 1, ListingLimits::default()),
            Err(FilesystemError::NotFound)
        );
        let names: Vec<String> = ops
            .list_dir(&user(), "/home/user/m3", 2, ListingLimits::default())
            .unwrap()
            .into_iter()
            .map(|entry| entry.path)
            .collect();
        assert_eq!(
            names,
            vec![
                "/home/user/m3/big.bin",
                "/home/user/m3/fifo",
                "/home/user/m3/link",
                "/home/user/m3/many",
                "/home/user/m3/many/f00.txt",
                "/home/user/m3/sys",
            ]
        );
    }

    #[test]
    fn make_dir_creates_parents_and_distinguishes_existing_kinds() {
        let fixture = Fixture::new();
        let ops = fixture.ops();
        let created = ops.make_dir(&user(), "/home/user/m3/a/b/c").unwrap();
        assert_eq!(created.kind, EntryKind::Directory);
        assert_eq!(created.path, "/home/user/m3/a/b/c");
        assert_eq!(created.name, "c");
        assert_eq!(created.owner, "1000");
        assert!(fixture.fs.is_dir("/home/user/m3/a/b"));
        assert_eq!(
            ops.make_dir(&user(), "/home/user/m3/a/b/c").err(),
            Some(FilesystemError::AlreadyExists)
        );
        assert_eq!(
            ops.make_dir(&user(), "/home/user/m3/big.bin").err(),
            Some(FilesystemError::NotADirectory)
        );
        assert_eq!(
            ops.make_dir(&user(), "/home/user/m3/link").err(),
            Some(FilesystemError::NotADirectory)
        );
        assert_eq!(
            ops.make_dir(&user(), "/home/user/m3/big.bin/sub").err(),
            Some(FilesystemError::NotADirectory)
        );
        assert_eq!(
            ops.make_dir(&user(), "/").err(),
            Some(FilesystemError::AlreadyExists)
        );
    }

    #[test]
    fn rename_moves_and_reports_conflicts() {
        let fixture = Fixture::new();
        let ops = fixture.ops();
        let moved = ops
            .rename(
                &user(),
                "/home/user/m3/big.bin",
                "/home/user/m3/many/big.bin",
            )
            .unwrap();
        assert_eq!(moved.path, "/home/user/m3/many/big.bin");
        assert_eq!(moved.size, 600);
        assert!(!fixture.fs.exists("/home/user/m3/big.bin"));
        assert_eq!(
            ops.rename(&user(), "/home/user/m3/missing", "/home/user/m3/x"),
            Err(FilesystemError::NotFound)
        );
        assert_eq!(
            ops.rename(
                &user(),
                "/home/user/m3/many/f00.txt",
                "/home/user/m3/nodir/x"
            ),
            Err(FilesystemError::NotFound)
        );
        assert_eq!(
            ops.rename(&user(), "/home/user/m3/many/f00.txt", "/home/user/m3/many"),
            Err(FilesystemError::DestinationConflict)
        );
        fixture.fs.add_dir("/home/user/m3/empty");
        assert_eq!(
            ops.rename(&user(), "/home/user/m3/many", "/home/user/m3/empty")
                .map(|entry| entry.path),
            Ok("/home/user/m3/empty".to_owned())
        );
        assert_eq!(
            ops.rename(&user(), "/home/user/m3/empty", "/etc/many"),
            Err(FilesystemError::Denied)
        );
        assert_eq!(
            ops.rename(&user(), "/", "/home/user/x"),
            Err(FilesystemError::Denied)
        );
    }

    #[test]
    fn remove_honours_the_recursive_flag_and_symlinks() {
        let fixture = Fixture::new();
        let ops = fixture.ops();
        assert_eq!(
            ops.remove(&user(), "/home/user/m3/many", false),
            Err(FilesystemError::NotEmpty)
        );
        assert!(fixture.fs.exists("/home/user/m3/many/f00.txt"));
        ops.remove(&user(), "/home/user/m3/many", true).unwrap();
        assert!(!fixture.fs.exists("/home/user/m3/many"));
        assert_eq!(
            ops.remove(&user(), "/home/user/m3/many", true),
            Err(FilesystemError::NotFound)
        );
        ops.remove(&user(), "/home/user/m3/sys", true).unwrap();
        assert!(fixture.fs.exists("/etc/passwd"), "only the link went");
        ops.remove(&user(), "/home/user/m3/link", false).unwrap();
        assert!(fixture.fs.exists("/home/user/m3/big.bin"));
        fixture.fs.add_dir("/home/user/m3/empty");
        ops.remove(&user(), "/home/user/m3/empty", false).unwrap();
    }

    #[test]
    fn write_target_refuses_below_the_disk_reserve_before_any_temporary() {
        let fixture = Fixture::new();
        let ops = fixture.ops();
        fixture.fs.set_free_bytes(10 * 1024 * 1024);
        assert_eq!(
            ops.write_target(&user(), "/home/user/m3/reserved.txt", 0o644)
                .err(),
            Some(FilesystemError::DiskReserve)
        );
        assert_eq!(fixture.fs.open_temps(), 0);
        assert!(!fixture.fs.exists("/home/user/m3/reserved.txt"));
        fixture.fs.set_free_bytes(300 * 1024 * 1024);
        let target = ops
            .write_target(&user(), "/home/user/m3/reserved.txt", 0o644)
            .unwrap();
        target.sink.commit("reserved.txt", &user()).unwrap();
        assert!(fixture.fs.exists("/home/user/m3/reserved.txt"));
    }

    #[test]
    fn write_target_creates_parents_and_refuses_bad_destinations() {
        let fixture = Fixture::new();
        let ops = fixture.ops();
        let target = ops
            .write_target(&user(), "/home/user/m3/new/deep/file.txt", 0o600)
            .unwrap();
        assert_eq!(target.name, "file.txt");
        assert_eq!(target.path.as_str(), "/home/user/m3/new/deep/file.txt");
        assert!(fixture.fs.is_dir("/home/user/m3/new/deep"));
        assert_eq!(fixture.fs.open_temps(), 1);
        let mut sink = target.sink;
        sink.write_chunk(b"hola").unwrap();
        let raw = sink.commit("file.txt", &user()).unwrap();
        assert_eq!(raw.size, 4);
        assert_eq!(raw.mode & 0o7777, 0o600);
        assert_eq!(fixture.fs.open_temps(), 0);
        assert_eq!(
            fixture.fs.contents("/home/user/m3/new/deep/file.txt"),
            Some(b"hola".to_vec())
        );
        let dropped = ops
            .write_target(&user(), "/home/user/m3/abandoned", 0o644)
            .unwrap();
        drop(dropped);
        assert_eq!(fixture.fs.open_temps(), 0);
        assert!(!fixture.fs.exists("/home/user/m3/abandoned"));
        assert_eq!(
            ops.write_target(&user(), "/home/user/m3/big.bin/x", 0o644)
                .err(),
            Some(FilesystemError::NotADirectory)
        );
        assert_eq!(
            ops.write_target(&user(), "/", 0o644).err(),
            Some(FilesystemError::IsADirectory)
        );
        let onto_dir = ops
            .write_target(&user(), "/home/user/m3/many", 0o644)
            .unwrap();
        assert_eq!(
            onto_dir.sink.commit("many", &user()).err(),
            Some(FsIoError::IsADirectory)
        );
        let onto_link = ops
            .write_target(&user(), "/home/user/m3/link", 0o644)
            .unwrap();
        onto_link.sink.commit("link", &user()).unwrap();
        assert!(!fixture.fs.is_symlink("/home/user/m3/link"));
        assert_eq!(
            fixture
                .fs
                .contents("/home/user/m3/big.bin")
                .map(|b| b.len()),
            Some(600)
        );
    }

    #[test]
    fn prepare_watch_needs_a_readable_directory() {
        let fixture = Fixture::new();
        let ops = fixture.ops();
        let target = ops.prepare_watch(&user(), "m3/many", false).unwrap();
        assert_eq!(target.canonical_root, "/home/user/m3/many");
        assert_eq!(target.path.as_str(), "/home/user/m3/many");
        assert!(target.subdirectories.is_empty());
        fixture
            .fs
            .add_symlink("/home/user/m3/alias", "/home/user/m3/many");
        assert_eq!(
            ops.prepare_watch(&user(), "/home/user/m3/alias", false)
                .unwrap()
                .canonical_root,
            "/home/user/m3/many"
        );
        assert_eq!(
            ops.prepare_watch(&user(), "/home/user/m3/big.bin", false),
            Err(FilesystemError::NotADirectory)
        );
        assert_eq!(
            ops.prepare_watch(&user(), "/home/user/m3/nope", false),
            Err(FilesystemError::NotFound)
        );
        assert_eq!(
            ops.prepare_watch(&user(), "/home/user/m3/sys", false),
            Err(FilesystemError::Denied)
        );
        fixture.fs.set_unreadable("/home/user/m3/many");
        assert_eq!(
            ops.prepare_watch(&user(), "/home/user/m3/many", true),
            Err(FilesystemError::PermissionDenied)
        );
        let root = RequestPath::parse("/home/user/m3", "/home/user").unwrap();
        let entry = ops
            .entry_in(&user(), &root, "/home/user/m3", "big.bin")
            .unwrap();
        assert_eq!(entry.path, "/home/user/m3/big.bin");
        assert_eq!(entry.name, "big.bin");
    }

    /// The recursive walk runs under the identity: unreadable and denied
    /// directories and symlinks to directories are left out, so no watch
    /// ever streams names the user could not list.
    #[test]
    fn recursive_watch_walks_only_readable_allowed_real_directories() {
        let fixture = Fixture::new();
        fixture.fs.add_dir("/home/user/m3/many/deep/deeper");
        fixture.fs.add_dir("/home/user/m3/private");
        fixture.fs.set_unreadable("/home/user/m3/private");
        fixture.fs.add_dir("/home/user/m3/private/inner");
        fixture
            .fs
            .add_symlink("/home/user/m3/loop", "/home/user/m3");
        let ops = fixture.ops();
        let target = ops.prepare_watch(&user(), "/home/user/m3", true).unwrap();
        assert_eq!(
            target.subdirectories,
            vec![
                "/home/user/m3/many",
                "/home/user/m3/many/deep",
                "/home/user/m3/many/deep/deeper",
            ]
        );
        let from_root = ops.prepare_watch(&user(), "/", true).unwrap();
        assert!(
            !from_root
                .subdirectories
                .iter()
                .any(|dir| dir.starts_with("/etc")),
            "{:?}",
            from_root.subdirectories
        );
        assert!(
            from_root
                .subdirectories
                .contains(&"/home/user/m3/many".to_owned())
        );
    }

    #[test]
    fn recursive_watch_refuses_trees_over_the_directory_cap() {
        let fixture = Fixture::new();
        for index in 0..MAX_WATCH_DIRECTORIES {
            fixture.fs.add_dir(&format!("/home/user/m3/many/d{index}"));
        }
        let ops = fixture.ops();
        assert_eq!(
            ops.prepare_watch(&user(), "/home/user/m3/many", true),
            Err(FilesystemError::WatchLimitReached)
        );
    }

    #[test]
    fn new_directories_are_followed_only_when_readable_real_and_allowed() {
        let fixture = Fixture::new();
        fixture.fs.add_dir("/home/user/m3/private");
        fixture.fs.set_unreadable("/home/user/m3/private");
        let ops = fixture.ops();
        assert!(ops.is_watchable_directory(&user(), "/home/user/m3/many"));
        assert!(!ops.is_watchable_directory(&user(), "/home/user/m3/private"));
        assert!(!ops.is_watchable_directory(&user(), "/home/user/m3/sys"));
        assert!(!ops.is_watchable_directory(&user(), "/home/user/m3/big.bin"));
        assert!(!ops.is_watchable_directory(&user(), "/home/user/m3/nope"));
        assert!(!ops.is_watchable_directory(&user(), "/etc"));
    }

    #[test]
    fn stat_and_listings_carry_the_metadata_the_filesystem_holds() {
        let fixture = Fixture::new();
        fixture.fs.set_metadata(
            "/home/user/m3/big.bin",
            crate::filesystem::FileMetadata::parse([("owner", "alice")]).unwrap(),
        );
        let ops = fixture.ops();
        let entry = ops.stat(&user(), "/home/user/m3/big.bin").unwrap();
        assert_eq!(
            entry.metadata.iter().collect::<Vec<_>>(),
            vec![("owner", "alice")]
        );
        assert!(
            ops.stat(&user(), "/home/user/m3/many")
                .unwrap()
                .metadata
                .is_empty()
        );
        let listed = ops
            .list_dir(&user(), "/home/user/m3", 1, ListingLimits::default())
            .unwrap();
        let big = listed.iter().find(|entry| entry.name == "big.bin").unwrap();
        assert_eq!(big.metadata.len(), 1);
    }

    #[test]
    fn metadata_errors_have_their_own_details() {
        assert_eq!(
            metadata_error(FsIoError::Unsupported),
            FilesystemError::MetadataUnsupported
        );
        assert_eq!(
            metadata_error(FsIoError::NoSpace),
            FilesystemError::MetadataTooLarge
        );
        assert_eq!(
            metadata_error(FsIoError::PermissionDenied),
            FilesystemError::PermissionDenied
        );
    }

    #[test]
    fn import_destinations_resolve_like_writes_and_refuse_directories() {
        let fixture = Fixture::new();
        let ops = fixture.ops();
        let destination = ops
            .import_destination(&user(), "m3/new/deep/file.bin")
            .unwrap();
        assert_eq!(destination.canonical, "/home/user/m3/new/deep/file.bin");
        assert_eq!(destination.dir, "/home/user/m3/new/deep");
        assert_eq!(destination.name, "file.bin");
        assert_eq!(destination.path.as_str(), "/home/user/m3/new/deep/file.bin");
        assert!(
            !fixture.fs.exists("/home/user/m3/new"),
            "nothing is created"
        );
        assert_eq!(
            ops.import_destination(&user(), "/home/user/m3/many"),
            Err(FilesystemError::IsADirectory)
        );
        assert_eq!(
            ops.import_destination(&user(), "/home/user/m3/sys/passwd"),
            Err(FilesystemError::Denied)
        );
        assert_eq!(
            ops.import_destination(&user(), "/"),
            Err(FilesystemError::IsADirectory)
        );
        let via_link = ops
            .import_destination(&user(), "/home/user/m3/link")
            .unwrap();
        assert_eq!(via_link.canonical, "/home/user/m3/link");
    }

    #[test]
    fn export_sources_are_regular_files_measured_when_opened() {
        let fixture = Fixture::new();
        let ops = fixture.ops();
        let source = ops.export_source(&user(), "/home/user/m3/big.bin").unwrap();
        assert_eq!(source.entry.size, 600);
        assert_eq!(source.path.as_str(), "/home/user/m3/big.bin");
        let mut buffer = [0u8; 16];
        assert_eq!(source.file.read_at(&mut buffer, 590).unwrap(), 10);
        assert_eq!(source.file.read_at(&mut buffer, 600).unwrap(), 0);
        assert_eq!(
            ops.export_source(&user(), "/home/user/m3/link")
                .err()
                .map(|e| e.to_string()),
            Some(FilesystemError::IsSymlink.to_string())
        );
        assert!(matches!(
            ops.export_source(&user(), "/home/user/m3/fifo"),
            Err(FilesystemError::NotARegularFile)
        ));
        assert!(matches!(
            ops.export_source(&user(), "/home/user/m3/many"),
            Err(FilesystemError::IsADirectory)
        ));
        assert!(matches!(
            ops.export_source(&user(), "/home/user/m3/nope"),
            Err(FilesystemError::NotFound)
        ));
        assert!(matches!(
            ops.export_source(&user(), "/etc/passwd"),
            Err(FilesystemError::Denied)
        ));
    }

    #[test]
    fn an_import_sink_commits_its_metadata_and_an_overwrite_without_clears_it() {
        let fixture = Fixture::new();
        let ops = fixture.ops();
        let destination = ops.import_destination(&user(), "m3/new.bin").unwrap();
        let mut sink = ops.begin_import(&user(), &destination, 0o600).unwrap();
        sink.write_chunk(b"abc").unwrap();
        let owner = crate::filesystem::FileMetadata::parse([("Owner", "alice")]).unwrap();
        sink.set_metadata(&owner).unwrap();
        sink.commit(&destination.name, &user()).unwrap();
        assert_eq!(fixture.fs.metadata("/home/user/m3/new.bin"), Some(owner));
        let again = ops.begin_import(&user(), &destination, 0o600).unwrap();
        again.commit(&destination.name, &user()).unwrap();
        assert_eq!(
            fixture.fs.metadata("/home/user/m3/new.bin"),
            Some(crate::filesystem::FileMetadata::default())
        );
        assert_eq!(fixture.fs.open_temps(), 0);
    }

    #[test]
    fn an_export_source_reads_the_file_as_it_is_now() {
        let fixture = Fixture::new();
        let ops = fixture.ops();
        let source = ops.export_source(&user(), "/home/user/m3/big.bin").unwrap();
        fixture.fs.truncate("/home/user/m3/big.bin", 100);
        let mut buffer = [0u8; 64];
        assert_eq!(source.file.read_at(&mut buffer, 64).unwrap(), 36);
        assert_eq!(source.file.read_at(&mut buffer, 100).unwrap(), 0);
        assert_eq!(source.entry.size, 600, "the entry is the size at open");
    }

    #[test]
    fn barrier_targets_match_import_destinations() {
        let fixture = Fixture::new();
        let ops = fixture.ops();
        let (path, canonical) = ops.barrier_target(&user(), "m3/new/file.bin").unwrap();
        let destination = ops.import_destination(&user(), "m3/new/file.bin").unwrap();
        assert_eq!(path, destination.path);
        assert_eq!(canonical, destination.canonical);
        assert_eq!(ops.barrier_target(&user(), "/etc/passwd"), None);
        assert_eq!(ops.barrier_target(&user(), "a/../b"), None);
    }
}
