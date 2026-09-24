//! What `Stat`, `ListDir`, `MakeDir`, `Move` and `Write` report: a raw
//! `lstat` result from the port turned into the wire entry with the
//! `ls -l` permission string and owner/group names.

use std::collections::HashMap;

use super::MODE_MASK;
use super::metadata::FileMetadata;
use super::path::RequestPath;
use super::ports::NameResolver;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EntryKind {
    File,
    Directory,
    Symlink,
    /// FIFO, socket or device: reported as `FILE_TYPE_UNSPECIFIED`.
    Other,
}

/// One `lstat`, as the adapter sees it. `mode` carries at least the
/// permission bits; `symlink_target` is `readlink` verbatim for symlinks.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RawEntry {
    pub name: String,
    pub kind: EntryKind,
    pub size: u64,
    pub mode: u32,
    pub uid: u32,
    pub gid: u32,
    pub modified_ms: i64,
    pub symlink_target: Option<String>,
}

/// The wire `EntryInfo`. `path` is the normalised request path, never the
/// symlink-resolved one; `metadata` is empty unless the operation read or
/// wrote it (`Stat`, `ListDir`, `Write`, a transfer).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Entry {
    pub name: String,
    pub kind: EntryKind,
    pub path: String,
    pub size: u64,
    pub mode: u32,
    pub permissions: String,
    pub owner: String,
    pub group: String,
    pub modified_ms: i64,
    pub symlink_target: Option<String>,
    pub metadata: FileMetadata,
}

/// The 10-character `ls -l` form: type letter plus three `rwx` triplets
/// with the setuid, setgid and sticky bits folded in.
#[must_use]
pub fn permissions_string(kind: EntryKind, mode: u32) -> String {
    let type_letter = match kind {
        EntryKind::File => '-',
        EntryKind::Directory => 'd',
        EntryKind::Symlink => 'l',
        EntryKind::Other => '?',
    };
    let mut out = String::with_capacity(10);
    out.push(type_letter);
    push_triplet(&mut out, mode >> 6, mode & 0o4000 != 0, 's');
    push_triplet(&mut out, mode >> 3, mode & 0o2000 != 0, 's');
    push_triplet(&mut out, mode, mode & 0o1000 != 0, 't');
    out
}

fn push_triplet(out: &mut String, bits: u32, special: bool, special_letter: char) {
    out.push(if bits & 0o4 != 0 { 'r' } else { '-' });
    out.push(if bits & 0o2 != 0 { 'w' } else { '-' });
    let executable = bits & 0o1 != 0;
    out.push(match (executable, special) {
        (true, true) => special_letter,
        (false, true) => special_letter.to_ascii_uppercase(),
        (true, false) => 'x',
        (false, false) => '-',
    });
}

/// Memoises the user database so a large listing costs one lookup per
/// distinct uid and gid; unknown ids are reported as their number.
pub struct NameCache<'a> {
    resolver: &'a dyn NameResolver,
    users: HashMap<u32, String>,
    groups: HashMap<u32, String>,
}

impl<'a> NameCache<'a> {
    #[must_use]
    pub fn new(resolver: &'a dyn NameResolver) -> Self {
        Self {
            resolver,
            users: HashMap::new(),
            groups: HashMap::new(),
        }
    }

    pub fn user(&mut self, uid: u32) -> String {
        self.users
            .entry(uid)
            .or_insert_with(|| {
                self.resolver
                    .user_name(uid)
                    .unwrap_or_else(|| uid.to_string())
            })
            .clone()
    }

    pub fn group(&mut self, gid: u32) -> String {
        self.groups
            .entry(gid)
            .or_insert_with(|| {
                self.resolver
                    .group_name(gid)
                    .unwrap_or_else(|| gid.to_string())
            })
            .clone()
    }
}

pub fn build_entry(raw: RawEntry, path: &RequestPath, names: &mut NameCache<'_>) -> Entry {
    let mode = raw.mode & MODE_MASK;
    Entry {
        name: path.name().to_owned(),
        kind: raw.kind,
        path: path.as_str().to_owned(),
        size: raw.size,
        mode,
        permissions: permissions_string(raw.kind, mode),
        owner: names.user(raw.uid),
        group: names.group(raw.gid),
        modified_ms: raw.modified_ms,
        symlink_target: raw.symlink_target,
        metadata: FileMetadata::default(),
    }
}

#[cfg(test)]
mod tests {
    use std::sync::atomic::{AtomicUsize, Ordering};

    use super::*;

    #[test]
    fn permission_strings_follow_ls() {
        assert_eq!(permissions_string(EntryKind::File, 0o644), "-rw-r--r--");
        assert_eq!(
            permissions_string(EntryKind::Directory, 0o755),
            "drwxr-xr-x"
        );
        assert_eq!(permissions_string(EntryKind::Symlink, 0o777), "lrwxrwxrwx");
        assert_eq!(permissions_string(EntryKind::Other, 0o600), "?rw-------");
        assert_eq!(permissions_string(EntryKind::File, 0o4755), "-rwsr-xr-x");
        assert_eq!(permissions_string(EntryKind::File, 0o4644), "-rwSr--r--");
        assert_eq!(
            permissions_string(EntryKind::Directory, 0o2775),
            "drwxrwsr-x"
        );
        assert_eq!(
            permissions_string(EntryKind::Directory, 0o1777),
            "drwxrwxrwt"
        );
        assert_eq!(
            permissions_string(EntryKind::Directory, 0o1776),
            "drwxrwxrwT"
        );
    }

    struct CountingResolver {
        calls: AtomicUsize,
    }

    impl NameResolver for CountingResolver {
        fn user_name(&self, uid: u32) -> Option<String> {
            self.calls.fetch_add(1, Ordering::SeqCst);
            (uid == 1000).then(|| "user".to_owned())
        }

        fn group_name(&self, gid: u32) -> Option<String> {
            self.calls.fetch_add(1, Ordering::SeqCst);
            (gid == 1000).then(|| "user".to_owned())
        }
    }

    fn raw(uid: u32, gid: u32) -> RawEntry {
        RawEntry {
            name: "big.bin".to_owned(),
            kind: EntryKind::File,
            size: 8_000_000,
            mode: 0o100_644,
            uid,
            gid,
            modified_ms: 1_700_000_000_000,
            symlink_target: None,
        }
    }

    #[test]
    fn build_entry_resolves_names_and_masks_the_mode() {
        let resolver = CountingResolver {
            calls: AtomicUsize::new(0),
        };
        let mut names = NameCache::new(&resolver);
        let path = RequestPath::parse("/home/user/m3/big.bin", "/home/user").unwrap();
        let entry = build_entry(raw(1000, 1000), &path, &mut names);
        assert_eq!(entry.name, "big.bin");
        assert_eq!(entry.path, "/home/user/m3/big.bin");
        assert_eq!(entry.mode, 0o644);
        assert_eq!(entry.permissions, "-rw-r--r--");
        assert_eq!(entry.owner, "user");
        assert_eq!(entry.group, "user");
        assert_eq!(entry.size, 8_000_000);
        assert_eq!(entry.symlink_target, None);
        let unknown = build_entry(raw(4242, 4243), &path, &mut names);
        assert_eq!(unknown.owner, "4242");
        assert_eq!(unknown.group, "4243");
        build_entry(raw(1000, 1000), &path, &mut names);
        build_entry(raw(4242, 4243), &path, &mut names);
        assert_eq!(
            resolver.calls.load(Ordering::SeqCst),
            4,
            "one lookup per distinct id"
        );
    }

    #[test]
    fn root_entry_is_named_slash() {
        let resolver = CountingResolver {
            calls: AtomicUsize::new(0),
        };
        let mut names = NameCache::new(&resolver);
        let root = RequestPath::parse("/", "/home/user").unwrap();
        let mut raw = raw(0, 0);
        raw.kind = EntryKind::Directory;
        let entry = build_entry(raw, &root, &mut names);
        assert_eq!(entry.name, "/");
        assert_eq!(entry.path, "/");
        assert_eq!(entry.owner, "0");
    }
}
