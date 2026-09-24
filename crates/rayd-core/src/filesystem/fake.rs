//! In-memory `FileSystem` for the host tests: a tree of nodes keyed by
//! canonical path, symlinks resolved by `canonicalize`, a per-path
//! "unreadable" flag that answers `PermissionDenied`, per-file metadata
//! that a commit replaces as a whole, and temp-file accounting so tests
//! can assert nothing leaks on error.

use std::collections::{BTreeMap, BTreeSet};
use std::io::{Cursor, Read};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};

use super::entry::{EntryKind, RawEntry};
use super::identity::FsIdentity;
use super::metadata::FileMetadata;
use super::path::{join_canonical, split_canonical};
use super::ports::{FileSystem, FsIoError, NameResolver, OpenedSnapshot, SnapshotFile, WriteSink};

/// The unprivileged sandbox user of the image.
pub fn user() -> FsIdentity {
    FsIdentity {
        uid: 1000,
        gid: 1000,
        home: "/home/user".to_owned(),
    }
}

const MAX_SYMLINK_HOPS: usize = 40;

/// Owner and group as their numbers, like an image without those accounts.
pub struct NumericNames;

impl NameResolver for NumericNames {
    fn user_name(&self, _uid: u32) -> Option<String> {
        None
    }

    fn group_name(&self, _gid: u32) -> Option<String> {
        None
    }
}

#[derive(Debug, Clone)]
struct Node {
    kind: EntryKind,
    bytes: Vec<u8>,
    mode: u32,
    uid: u32,
    gid: u32,
    target: Option<String>,
    metadata: FileMetadata,
}

impl Node {
    fn dir(mode: u32, id: &FsIdentity) -> Self {
        Self {
            kind: EntryKind::Directory,
            bytes: Vec::new(),
            mode,
            uid: id.uid,
            gid: id.gid,
            target: None,
            metadata: FileMetadata::default(),
        }
    }
}

/// Plenty by default: the reserve rule only trips when a test lowers it.
const DEFAULT_FAKE_FREE_BYTES: u64 = 64 * 1024 * 1024 * 1024;

#[derive(Debug)]
struct Tree {
    nodes: BTreeMap<String, Node>,
    unreadable: BTreeSet<String>,
    open_temps: usize,
    free_bytes: u64,
}

impl Default for Tree {
    fn default() -> Self {
        Self {
            nodes: BTreeMap::new(),
            unreadable: BTreeSet::new(),
            open_temps: 0,
            free_bytes: DEFAULT_FAKE_FREE_BYTES,
        }
    }
}

impl Tree {
    fn children_of(&self, dir: &str) -> Vec<(&String, &Node)> {
        let prefix = if dir == "/" {
            "/".to_owned()
        } else {
            format!("{dir}/")
        };
        self.nodes
            .range(prefix.clone()..)
            .take_while(|(path, _)| path.starts_with(&prefix))
            .filter(|(path, _)| path.len() > prefix.len())
            .filter(|(path, _)| !path[prefix.len()..].contains('/'))
            .collect()
    }

    fn has_children(&self, dir: &str) -> bool {
        !self.children_of(dir).is_empty()
    }

    /// `lstat` needs search permission on the parent, not read permission
    /// on the entry itself, so the unreadable flag does not apply here.
    fn raw_entry(&self, path: &str) -> Result<RawEntry, FsIoError> {
        let node = self.nodes.get(path).ok_or(FsIoError::NotFound)?;
        let (_, name) = split_canonical(path);
        Ok(RawEntry {
            name: if path == "/" {
                "/".to_owned()
            } else {
                name.to_owned()
            },
            kind: node.kind,
            size: node.bytes.len() as u64,
            mode: node.mode,
            uid: node.uid,
            gid: node.gid,
            modified_ms: 1_700_000_000_000,
            symlink_target: node.target.clone(),
        })
    }

    fn resolve(&self, path: &str, hops: usize) -> Result<String, FsIoError> {
        if hops > MAX_SYMLINK_HOPS {
            return Err(FsIoError::Other {
                errno: "ELOOP".to_owned(),
            });
        }
        let components: Vec<&str> = path.split('/').filter(|c| !c.is_empty()).collect();
        let mut current = "/".to_owned();
        for (index, component) in components.iter().enumerate() {
            if self.unreadable.contains(&current) {
                return Err(FsIoError::PermissionDenied);
            }
            let candidate = join_canonical(&current, component);
            let node = self.nodes.get(&candidate).ok_or(FsIoError::NotFound)?;
            match node.kind {
                EntryKind::Symlink => {
                    let target = node.target.clone().unwrap_or_default();
                    let resolved_target = if target.starts_with('/') {
                        target
                    } else {
                        join_canonical(&current, &target)
                    };
                    let rest = components[index + 1..].join("/");
                    let remaining = if rest.is_empty() {
                        resolved_target
                    } else {
                        join_canonical(&resolved_target, &rest)
                    };
                    return self.resolve(&remaining, hops + 1);
                }
                EntryKind::Directory => current = candidate,
                EntryKind::File | EntryKind::Other => {
                    if index + 1 == components.len() {
                        current = candidate;
                    } else {
                        return Err(FsIoError::NotADirectory);
                    }
                }
            }
        }
        Ok(current)
    }

    fn create_parents(&mut self, dir: &str, id: &FsIdentity) -> Result<(), FsIoError> {
        let mut current = "/".to_owned();
        for component in dir.split('/').filter(|c| !c.is_empty()) {
            let candidate = join_canonical(&current, component);
            match self.nodes.get(&candidate) {
                Some(node) if node.kind == EntryKind::Directory => {}
                Some(_) => return Err(FsIoError::NotADirectory),
                None => {
                    self.nodes.insert(candidate.clone(), Node::dir(0o755, id));
                }
            }
            current = candidate;
        }
        Ok(())
    }

    fn move_subtree(&mut self, from: &str, to: &str) {
        let prefix = format!("{from}/");
        let moved: Vec<(String, Node)> = self
            .nodes
            .iter()
            .filter(|(path, _)| *path == from || path.starts_with(&prefix))
            .map(|(path, node)| (path.clone(), node.clone()))
            .collect();
        for (path, _) in &moved {
            self.nodes.remove(path);
        }
        for (path, node) in moved {
            let new_path = format!("{to}{}", &path[from.len()..]);
            self.nodes.insert(new_path, node);
        }
    }

    fn remove_subtree(&mut self, path: &str) {
        let prefix = format!("{path}/");
        self.nodes
            .retain(|existing, _| existing != path && !existing.starts_with(&prefix));
    }
}

#[derive(Debug, Default)]
pub struct FakeFileSystem {
    tree: Arc<Mutex<Tree>>,
}

impl FakeFileSystem {
    pub fn new() -> Self {
        let fs = Self::default();
        fs.tree().nodes.insert(
            "/".to_owned(),
            Node::dir(
                0o755,
                &FsIdentity {
                    uid: 0,
                    gid: 0,
                    home: String::new(),
                },
            ),
        );
        fs
    }

    pub fn add_dir(&self, path: &str) {
        self.insert_with_parents(path, Node::dir(0o755, &user()));
    }

    pub fn add_file(&self, path: &str, bytes: &[u8]) {
        self.insert_with_parents(
            path,
            Node {
                kind: EntryKind::File,
                bytes: bytes.to_vec(),
                mode: 0o644,
                uid: user().uid,
                gid: user().gid,
                target: None,
                metadata: FileMetadata::default(),
            },
        );
    }

    pub fn add_symlink(&self, path: &str, target: &str) {
        self.insert_with_parents(
            path,
            Node {
                kind: EntryKind::Symlink,
                bytes: Vec::new(),
                mode: 0o777,
                uid: user().uid,
                gid: user().gid,
                target: Some(target.to_owned()),
                metadata: FileMetadata::default(),
            },
        );
    }

    pub fn add_other(&self, path: &str) {
        self.insert_with_parents(
            path,
            Node {
                kind: EntryKind::Other,
                bytes: Vec::new(),
                mode: 0o644,
                uid: user().uid,
                gid: user().gid,
                target: None,
                metadata: FileMetadata::default(),
            },
        );
    }

    pub fn set_unreadable(&self, path: &str) {
        self.tree().unreadable.insert(path.to_owned());
    }

    pub fn exists(&self, path: &str) -> bool {
        self.tree().nodes.contains_key(path)
    }

    pub fn is_dir(&self, path: &str) -> bool {
        self.tree()
            .nodes
            .get(path)
            .is_some_and(|node| node.kind == EntryKind::Directory)
    }

    pub fn is_symlink(&self, path: &str) -> bool {
        self.tree()
            .nodes
            .get(path)
            .is_some_and(|node| node.kind == EntryKind::Symlink)
    }

    pub fn contents(&self, path: &str) -> Option<Vec<u8>> {
        self.tree()
            .nodes
            .get(path)
            .filter(|node| node.kind == EntryKind::File)
            .map(|node| node.bytes.clone())
    }

    /// Temp files begun and neither committed nor dropped.
    pub fn open_temps(&self) -> usize {
        self.tree().open_temps
    }

    /// Replaces the metadata a node carries, as a sandbox process could.
    pub fn set_metadata(&self, path: &str, metadata: FileMetadata) {
        if let Some(node) = self.tree().nodes.get_mut(path) {
            node.metadata = metadata;
        }
    }

    pub fn metadata(&self, path: &str) -> Option<FileMetadata> {
        self.tree()
            .nodes
            .get(path)
            .map(|node| node.metadata.clone())
    }

    /// Truncates a file in place, as a writer racing an export would.
    pub fn truncate(&self, path: &str, len: usize) {
        if let Some(node) = self.tree().nodes.get_mut(path) {
            node.bytes.truncate(len);
        }
    }

    /// What `free_bytes` answers for every directory from now on.
    pub fn set_free_bytes(&self, free: u64) {
        self.tree().free_bytes = free;
    }

    fn insert_with_parents(&self, path: &str, node: Node) {
        let mut tree = self.tree();
        let (dir, _) = split_canonical(path);
        tree.create_parents(dir, &user())
            .unwrap_or_else(|error| panic!("fixture parent {dir}: {error}"));
        tree.nodes.insert(path.to_owned(), node);
    }

    fn tree(&self) -> MutexGuard<'_, Tree> {
        self.tree.lock().unwrap_or_else(PoisonError::into_inner)
    }
}

impl FileSystem for FakeFileSystem {
    fn canonicalize(&self, _id: &FsIdentity, path: &str) -> Result<String, FsIoError> {
        self.tree().resolve(path, 0)
    }

    fn lstat(&self, _id: &FsIdentity, path: &str) -> Result<RawEntry, FsIoError> {
        self.tree().raw_entry(path)
    }

    fn read_dir(&self, _id: &FsIdentity, path: &str) -> Result<Vec<RawEntry>, FsIoError> {
        let tree = self.tree();
        if tree.unreadable.contains(path) {
            return Err(FsIoError::PermissionDenied);
        }
        match tree.nodes.get(path) {
            None => return Err(FsIoError::NotFound),
            Some(node) if node.kind != EntryKind::Directory => {
                return Err(FsIoError::NotADirectory);
            }
            Some(_) => {}
        }
        Ok(tree
            .children_of(path)
            .into_iter()
            .filter_map(|(child, _)| tree.raw_entry(child).ok())
            .collect())
    }

    fn open_read(&self, _id: &FsIdentity, path: &str) -> Result<Box<dyn Read + Send>, FsIoError> {
        let tree = self.tree();
        if tree.unreadable.contains(path) {
            return Err(FsIoError::PermissionDenied);
        }
        let node = tree.nodes.get(path).ok_or(FsIoError::NotFound)?;
        match node.kind {
            EntryKind::File => Ok(Box::new(Cursor::new(node.bytes.clone()))),
            EntryKind::Directory => Err(FsIoError::IsADirectory),
            EntryKind::Symlink => Err(FsIoError::IsSymlink),
            EntryKind::Other => Err(FsIoError::NotARegularFile),
        }
    }

    fn open_snapshot(&self, id: &FsIdentity, path: &str) -> Result<OpenedSnapshot, FsIoError> {
        self.open_read(id, path)?;
        let entry = self.tree().raw_entry(path)?;
        Ok(OpenedSnapshot {
            file: Box::new(FakeSnapshot {
                tree: self.tree.clone(),
                path: path.to_owned(),
            }),
            entry,
        })
    }

    fn read_metadata(&self, _id: &FsIdentity, path: &str) -> Result<FileMetadata, FsIoError> {
        Ok(self
            .tree()
            .nodes
            .get(path)
            .map(|node| node.metadata.clone())
            .unwrap_or_default())
    }

    fn free_bytes(&self, _id: &FsIdentity, _canonical_dir: &str) -> Result<u64, FsIoError> {
        Ok(self.tree().free_bytes)
    }

    fn begin_write(
        &self,
        id: &FsIdentity,
        dir: &str,
        mode: u32,
    ) -> Result<Box<dyn WriteSink>, FsIoError> {
        let mut tree = self.tree();
        if tree.unreadable.contains(dir) {
            return Err(FsIoError::PermissionDenied);
        }
        tree.create_parents(dir, id)?;
        tree.open_temps += 1;
        Ok(Box::new(FakeSink {
            tree: self.tree.clone(),
            dir: dir.to_owned(),
            mode,
            buffer: Vec::new(),
            metadata: FileMetadata::default(),
        }))
    }

    fn make_dir(&self, id: &FsIdentity, path: &str, mode: u32) -> Result<(), FsIoError> {
        let mut tree = self.tree();
        let (dir, _) = split_canonical(path);
        tree.create_parents(dir, id)?;
        if tree.nodes.contains_key(path) {
            return Err(FsIoError::AlreadyExists);
        }
        tree.nodes.insert(path.to_owned(), Node::dir(mode, id));
        Ok(())
    }

    fn rename(&self, _id: &FsIdentity, from: &str, to: &str) -> Result<(), FsIoError> {
        let mut tree = self.tree();
        let source_kind = tree.nodes.get(from).ok_or(FsIoError::NotFound)?.kind;
        let (to_dir, _) = split_canonical(to);
        match tree.nodes.get(to_dir) {
            None => return Err(FsIoError::NotFound),
            Some(node) if node.kind != EntryKind::Directory => {
                return Err(FsIoError::NotADirectory);
            }
            Some(_) => {}
        }
        let source_is_dir = source_kind == EntryKind::Directory;
        match tree
            .nodes
            .get(to)
            .map(|node| node.kind == EntryKind::Directory)
        {
            None => {}
            Some(true) if source_is_dir => {
                if tree.has_children(to) {
                    return Err(FsIoError::NotEmpty);
                }
                tree.nodes.remove(to);
            }
            Some(true) => return Err(FsIoError::IsADirectory),
            Some(false) if source_is_dir => return Err(FsIoError::NotADirectory),
            Some(false) => {
                tree.nodes.remove(to);
            }
        }
        tree.move_subtree(from, to);
        Ok(())
    }

    fn remove(
        &self,
        _id: &FsIdentity,
        path: &str,
        kind: EntryKind,
        recursive: bool,
    ) -> Result<(), FsIoError> {
        let mut tree = self.tree();
        if !tree.nodes.contains_key(path) {
            return Err(FsIoError::NotFound);
        }
        if kind == EntryKind::Directory {
            if tree.has_children(path) && !recursive {
                return Err(FsIoError::NotEmpty);
            }
            tree.remove_subtree(path);
        } else {
            tree.nodes.remove(path);
        }
        Ok(())
    }
}

/// Reads the node's current bytes on every call, so a truncation after
/// the open is visible like it is through a real descriptor.
struct FakeSnapshot {
    tree: Arc<Mutex<Tree>>,
    path: String,
}

impl SnapshotFile for FakeSnapshot {
    fn read_at(&self, buf: &mut [u8], offset: u64) -> Result<usize, FsIoError> {
        let tree = self.tree.lock().unwrap_or_else(PoisonError::into_inner);
        let bytes = tree
            .nodes
            .get(&self.path)
            .map(|node| node.bytes.as_slice())
            .unwrap_or_default();
        let start = usize::try_from(offset)
            .unwrap_or(usize::MAX)
            .min(bytes.len());
        let len = buf.len().min(bytes.len() - start);
        buf[..len].copy_from_slice(&bytes[start..start + len]);
        Ok(len)
    }
}

struct FakeSink {
    tree: Arc<Mutex<Tree>>,
    dir: String,
    mode: u32,
    buffer: Vec<u8>,
    metadata: FileMetadata,
}

impl WriteSink for FakeSink {
    fn write_chunk(&mut self, bytes: &[u8]) -> Result<(), FsIoError> {
        self.buffer.extend_from_slice(bytes);
        Ok(())
    }

    fn set_metadata(&mut self, metadata: &FileMetadata) -> Result<(), FsIoError> {
        self.metadata = metadata.clone();
        Ok(())
    }

    fn commit(self: Box<Self>, final_name: &str, id: &FsIdentity) -> Result<RawEntry, FsIoError> {
        let final_path = join_canonical(&self.dir, final_name);
        let mut tree = self.tree.lock().unwrap_or_else(PoisonError::into_inner);
        if tree
            .nodes
            .get(&final_path)
            .is_some_and(|node| node.kind == EntryKind::Directory)
        {
            return Err(FsIoError::IsADirectory);
        }
        tree.nodes.insert(
            final_path.clone(),
            Node {
                kind: EntryKind::File,
                bytes: self.buffer.clone(),
                mode: self.mode,
                uid: id.uid,
                gid: id.gid,
                target: None,
                metadata: self.metadata.clone(),
            },
        );
        tree.raw_entry(&final_path)
    }
}

impl Drop for FakeSink {
    fn drop(&mut self) {
        let mut tree = self.tree.lock().unwrap_or_else(PoisonError::into_inner);
        tree.open_temps = tree.open_temps.saturating_sub(1);
    }
}
