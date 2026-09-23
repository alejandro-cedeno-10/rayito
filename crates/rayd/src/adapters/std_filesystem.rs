//! The `FileSystem` port over blocking `std::fs`, every call wrapped in the
//! requesting user's filesystem identity (design D2/D3): `realpath`,
//! `lstat`, `O_NOFOLLOW` reads, temp-file writes committed by `rename`,
//! `mkdir -p`, `rename(2)`, symlink-safe removal and the `statvfs` behind
//! the disk reserve; the export snapshot (`fstat` + `pread` on one
//! descriptor) and the `user.rayito.*` metadata (`fsetxattr` on the temp
//! file's descriptor, `llistxattr`/`lgetxattr` that never follow a
//! symlink, `m9-file-transfer` D17). Off Unix every call answers
//! `Unsupported` so the gRPC surface still routes.

#[cfg(unix)]
pub use unix::{StdFileSystem, TempWriteSink, io_error};
#[cfg(unix)]
pub type PlatformFileSystem = unix::StdFileSystem;

#[cfg(not(unix))]
pub use unsupported::UnsupportedFileSystem;
#[cfg(not(unix))]
pub type PlatformFileSystem = unsupported::UnsupportedFileSystem;

#[cfg(unix)]
mod unix {
    use std::fs::{self, DirBuilder, File, OpenOptions, Permissions};
    use std::io::{self, Read, Write};
    use std::os::unix::fs::{DirBuilderExt, FileExt, MetadataExt, OpenOptionsExt, PermissionsExt};
    use std::path::Path;

    use nix::errno::Errno;
    use nix::sys::statvfs::statvfs;
    use nix::unistd::{Gid, Uid, fchown};
    use rayd_core::filesystem::{
        DEFAULT_DIR_MODE, EntryKind, FileMetadata, FileSystem, FsIdentity, FsIoError, MODE_MASK,
        OpenedSnapshot, RawEntry, SnapshotFile, TEMP_PREFIX, WriteSink, join_canonical,
    };
    use tempfile::NamedTempFile;

    use super::xattr;
    use crate::adapters::IdentitySwitch;
    use crate::adapters::fs_identity::FsIdentityGuard;

    pub struct StdFileSystem {
        identity_switch: IdentitySwitch,
    }

    impl StdFileSystem {
        #[must_use]
        pub fn new(identity_switch: IdentitySwitch) -> Self {
            Self { identity_switch }
        }

        fn enter(&self, id: &FsIdentity) -> Result<FsIdentityGuard, FsIoError> {
            FsIdentityGuard::enter(self.identity_switch, id)
        }
    }

    impl FileSystem for StdFileSystem {
        fn canonicalize(&self, id: &FsIdentity, path: &str) -> Result<String, FsIoError> {
            let _guard = self.enter(id)?;
            fs::canonicalize(path)
                .map(|canonical| canonical.to_string_lossy().into_owned())
                .map_err(|error| io_error(&error))
        }

        fn lstat(&self, id: &FsIdentity, path: &str) -> Result<RawEntry, FsIoError> {
            let _guard = self.enter(id)?;
            let metadata = fs::symlink_metadata(path).map_err(|error| io_error(&error))?;
            Ok(raw_entry(entry_name(path), &metadata, Path::new(path)))
        }

        fn read_dir(&self, id: &FsIdentity, path: &str) -> Result<Vec<RawEntry>, FsIoError> {
            let _guard = self.enter(id)?;
            let entries = fs::read_dir(path).map_err(|error| io_error(&error))?;
            Ok(entries
                .filter_map(Result::ok)
                .filter_map(|entry| {
                    let metadata = entry.metadata().ok()?;
                    let name = entry.file_name().to_string_lossy().into_owned();
                    Some(raw_entry(name, &metadata, &entry.path()))
                })
                .collect())
        }

        /// A FIFO is refused without waiting for a writer (`open_regular`).
        fn open_read(
            &self,
            id: &FsIdentity,
            path: &str,
        ) -> Result<Box<dyn Read + Send>, FsIoError> {
            let _guard = self.enter(id)?;
            let (file, _) = open_regular(path)?;
            Ok(Box::new(file))
        }

        /// The same open as `open_read`; the entry is the `fstat` of that
        /// descriptor, so the export reads the inode it measured even if the
        /// name is replaced afterwards.
        fn open_snapshot(&self, id: &FsIdentity, path: &str) -> Result<OpenedSnapshot, FsIoError> {
            let _guard = self.enter(id)?;
            let (file, metadata) = open_regular(path)?;
            Ok(OpenedSnapshot {
                entry: raw_entry(entry_name(path), &metadata, Path::new(path)),
                file: Box::new(PositionedFile(file)),
            })
        }

        fn read_metadata(&self, id: &FsIdentity, path: &str) -> Result<FileMetadata, FsIoError> {
            let _guard = self.enter(id)?;
            xattr::read_metadata(path)
        }

        /// `f_bavail * f_frsize` of the deepest existing ancestor of the
        /// directory: what an unprivileged writer may still use, root's
        /// reserved blocks excluded.
        fn free_bytes(&self, id: &FsIdentity, canonical_dir: &str) -> Result<u64, FsIoError> {
            let _guard = self.enter(id)?;
            let existing = deepest_existing_ancestor(Path::new(canonical_dir));
            let stats = statvfs(existing).map_err(errno_error)?;
            Ok(widen(stats.blocks_available()).saturating_mul(widen(stats.fragment_size())))
        }

        fn begin_write(
            &self,
            id: &FsIdentity,
            dir: &str,
            mode: u32,
        ) -> Result<Box<dyn WriteSink>, FsIoError> {
            let _guard = self.enter(id)?;
            create_parents(Path::new(dir))?;
            let file = tempfile::Builder::new()
                .prefix(TEMP_PREFIX)
                .tempfile_in(dir)
                .map_err(|error| io_error(&error))?;
            Ok(Box::new(TempWriteSink {
                file,
                dir: dir.to_owned(),
                mode,
                identity_switch: self.identity_switch,
            }))
        }

        fn make_dir(&self, id: &FsIdentity, path: &str, mode: u32) -> Result<(), FsIoError> {
            let _guard = self.enter(id)?;
            if let Some(parent) = Path::new(path).parent() {
                create_parents(parent)?;
            }
            DirBuilder::new()
                .mode(mode)
                .create(path)
                .map_err(|error| io_error(&error))
        }

        fn rename(&self, id: &FsIdentity, from: &str, to: &str) -> Result<(), FsIoError> {
            let _guard = self.enter(id)?;
            fs::rename(from, to).map_err(|error| io_error(&error))
        }

        fn remove(
            &self,
            id: &FsIdentity,
            path: &str,
            kind: EntryKind,
            recursive: bool,
        ) -> Result<(), FsIoError> {
            let _guard = self.enter(id)?;
            let removed = match (kind, recursive) {
                (EntryKind::Directory, true) => fs::remove_dir_all(path),
                (EntryKind::Directory, false) => fs::remove_dir(path),
                _ => fs::remove_file(path),
            };
            removed.map_err(|error| io_error(&error))
        }
    }

    /// A `.rayito-tmp-*` file next to its destination. Dropping it without
    /// `commit` unlinks it (`NamedTempFile` semantics).
    pub struct TempWriteSink {
        file: NamedTempFile,
        dir: String,
        mode: u32,
        identity_switch: IdentitySwitch,
    }

    impl WriteSink for TempWriteSink {
        fn write_chunk(&mut self, bytes: &[u8]) -> Result<(), FsIoError> {
            self.file
                .as_file_mut()
                .write_all(bytes)
                .map_err(|error| io_error(&error))
        }

        /// On the descriptor with the agent's own rights, like `fchmod`: no
        /// path is resolved, so the set lands on this inode and appears with
        /// its content at the rename.
        fn set_metadata(&mut self, metadata: &FileMetadata) -> Result<(), FsIoError> {
            xattr::set_metadata(self.file.as_file(), metadata)
        }

        /// `fsync`, `fchmod` and `fchown` run on the descriptor with the
        /// agent's own rights (the temp file is already the user's); the
        /// `rename`, the directory `fsync` and the final `lstat` run under
        /// the user's identity like every other path operation.
        fn commit(
            self: Box<Self>,
            final_name: &str,
            id: &FsIdentity,
        ) -> Result<RawEntry, FsIoError> {
            let this = *self;
            let file = this.file.as_file();
            file.sync_all().map_err(|error| io_error(&error))?;
            file.set_permissions(Permissions::from_mode(this.mode))
                .map_err(|error| io_error(&error))?;
            if this.identity_switch == IdentitySwitch::Enforce {
                fchown(
                    file,
                    Some(Uid::from_raw(id.uid)),
                    Some(Gid::from_raw(id.gid)),
                )
                .map_err(errno_error)?;
            }
            let final_path = join_canonical(&this.dir, final_name);
            let _guard = FsIdentityGuard::enter(this.identity_switch, id)?;
            this.file
                .persist(&final_path)
                .map_err(|error| io_error(&error.error))?;
            sync_directory(&this.dir);
            let metadata = fs::symlink_metadata(&final_path).map_err(|error| io_error(&error))?;
            Ok(raw_entry(
                final_name.to_owned(),
                &metadata,
                Path::new(&final_path),
            ))
        }
    }

    /// `O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK`, regular files only.
    /// `O_NONBLOCK` is there for the open, not the reads: `open(2)` of a FIFO
    /// without a writer blocks forever otherwise, parking a pool thread;
    /// with it the FIFO opens at once and the regular-file check refuses
    /// it. Reads of a regular file ignore the flag.
    fn open_regular(path: &str) -> Result<(File, fs::Metadata), FsIoError> {
        let file = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_NONBLOCK)
            .open(path)
            .map_err(|error| io_error(&error))?;
        let metadata = file.metadata().map_err(|error| io_error(&error))?;
        let file_type = metadata.file_type();
        if file_type.is_dir() {
            return Err(FsIoError::IsADirectory);
        }
        if !file_type.is_file() {
            return Err(FsIoError::NotARegularFile);
        }
        Ok((file, metadata))
    }

    /// `pread` on an open descriptor: a retried part re-reads its own range
    /// and no shared cursor exists between reads.
    struct PositionedFile(File);

    impl SnapshotFile for PositionedFile {
        fn read_at(&self, buf: &mut [u8], offset: u64) -> Result<usize, FsIoError> {
            let mut filled = 0usize;
            while filled < buf.len() {
                let position = offset.saturating_add(u64::try_from(filled).unwrap_or(u64::MAX));
                match self.0.read_at(&mut buf[filled..], position) {
                    Ok(0) => break,
                    Ok(read) => filled += read,
                    Err(error) if error.kind() == io::ErrorKind::Interrupted => {}
                    Err(error) => return Err(io_error(&error)),
                }
            }
            Ok(filled)
        }
    }

    /// The rename is already durable in the file's own inode; a directory
    /// `fsync` that fails (an unreadable parent) must not undo a commit
    /// that happened.
    fn sync_directory(dir: &str) {
        if let Err(error) = File::open(dir).and_then(|handle| handle.sync_all()) {
            tracing::debug!(reason = %io_error(&error), "directory fsync skipped");
        }
    }

    /// `statvfs` field widths differ by target (`u32` on some, `u64` on the
    /// ARM64 musl build); a generic widening keeps both lint-clean.
    fn widen(value: impl Into<u64>) -> u64 {
        value.into()
    }

    /// The write may create parents: the filesystem to measure is the one
    /// holding the deepest ancestor that already exists (`/` at worst).
    fn deepest_existing_ancestor(dir: &Path) -> &Path {
        dir.ancestors()
            .find(|candidate| candidate.exists())
            .unwrap_or_else(|| Path::new("/"))
    }

    /// `mkdir -p` with `0o755`; an existing component that is not a
    /// directory surfaces as `EEXIST`/`ENOTDIR`, both `NotADirectory` here.
    fn create_parents(dir: &Path) -> Result<(), FsIoError> {
        DirBuilder::new()
            .recursive(true)
            .mode(DEFAULT_DIR_MODE)
            .create(dir)
            .map_err(|error| match io_error(&error) {
                FsIoError::AlreadyExists => FsIoError::NotADirectory,
                other => other,
            })
    }

    fn raw_entry(name: String, metadata: &fs::Metadata, path: &Path) -> RawEntry {
        let file_type = metadata.file_type();
        let kind = if file_type.is_symlink() {
            EntryKind::Symlink
        } else if file_type.is_dir() {
            EntryKind::Directory
        } else if file_type.is_file() {
            EntryKind::File
        } else {
            EntryKind::Other
        };
        let symlink_target = (kind == EntryKind::Symlink)
            .then(|| fs::read_link(path).ok())
            .flatten()
            .map(|target| target.to_string_lossy().into_owned());
        RawEntry {
            name,
            kind,
            size: metadata.len(),
            mode: metadata.mode() & MODE_MASK,
            uid: metadata.uid(),
            gid: metadata.gid(),
            modified_ms: metadata
                .mtime()
                .saturating_mul(1_000)
                .saturating_add(metadata.mtime_nsec() / 1_000_000),
            symlink_target,
        }
    }

    fn entry_name(path: &str) -> String {
        path.rsplit('/')
            .find(|component| !component.is_empty())
            .unwrap_or("/")
            .to_owned()
    }

    /// errno → port error; the name (`EIO`) is all that leaves the adapter.
    #[must_use]
    pub fn io_error(error: &io::Error) -> FsIoError {
        match error.raw_os_error().map(Errno::from_raw) {
            Some(Errno::ENOENT) => FsIoError::NotFound,
            Some(Errno::EACCES | Errno::EPERM) => FsIoError::PermissionDenied,
            Some(Errno::EEXIST) => FsIoError::AlreadyExists,
            Some(Errno::ENOTDIR) => FsIoError::NotADirectory,
            Some(Errno::EISDIR) => FsIoError::IsADirectory,
            Some(Errno::ENOTEMPTY) => FsIoError::NotEmpty,
            Some(Errno::ELOOP) => FsIoError::IsSymlink,
            Some(Errno::EXDEV) => FsIoError::CrossDevice,
            Some(Errno::ENOSPC) => FsIoError::NoSpace,
            Some(errno) => FsIoError::Other {
                errno: format!("{errno:?}"),
            },
            None => FsIoError::Other {
                errno: error.kind().to_string(),
            },
        }
    }

    fn errno_error(errno: Errno) -> FsIoError {
        io_error(&io::Error::from_raw_os_error(errno as i32))
    }

    #[cfg(test)]
    mod tests {
        use std::os::unix::fs::symlink;

        use super::*;

        fn identity() -> FsIdentity {
            FsIdentity {
                uid: nix::unistd::getuid().as_raw(),
                gid: nix::unistd::getgid().as_raw(),
                home: "/tmp".to_owned(),
            }
        }

        fn playground() -> (tempfile::TempDir, String) {
            let dir = tempfile::tempdir().unwrap();
            let root = fs::canonicalize(dir.path())
                .unwrap()
                .to_string_lossy()
                .into_owned();
            (dir, root)
        }

        fn temp_files(root: &str) -> usize {
            fs::read_dir(root)
                .unwrap()
                .filter_map(Result::ok)
                .filter(|entry| entry.file_name().to_string_lossy().starts_with(TEMP_PREFIX))
                .count()
        }

        #[test]
        fn free_bytes_reads_the_deepest_existing_ancestor() {
            let (_dir, root) = playground();
            let fs = StdFileSystem::new(IdentitySwitch::KeepCurrent);
            let existing = fs.free_bytes(&identity(), &root).unwrap();
            assert!(existing > 0);
            let missing = fs
                .free_bytes(&identity(), &format!("{root}/not/yet/created"))
                .unwrap();
            assert!(missing > 0);
        }

        #[test]
        fn write_commits_atomically_with_mode_and_leaves_no_temp() {
            let (_dir, root) = playground();
            let fs_adapter = StdFileSystem::new(IdentitySwitch::KeepCurrent);
            let id = identity();
            let nested = format!("{root}/a/b");
            let mut sink = fs_adapter.begin_write(&id, &nested, 0o600).unwrap();
            assert_eq!(temp_files(&nested), 1);
            sink.write_chunk(b"hola ").unwrap();
            sink.write_chunk(b"mundo").unwrap();
            let entry = sink.commit("out.txt", &id).unwrap();
            assert_eq!(entry.name, "out.txt");
            assert_eq!(entry.size, 10);
            assert_eq!(entry.mode, 0o600);
            assert_eq!(entry.kind, EntryKind::File);
            assert_eq!(temp_files(&nested), 0);
            assert_eq!(
                fs::read(format!("{nested}/out.txt")).unwrap(),
                b"hola mundo"
            );
            let abandoned = fs_adapter.begin_write(&id, &root, 0o644).unwrap();
            assert_eq!(temp_files(&root), 1);
            drop(abandoned);
            assert_eq!(temp_files(&root), 0);
        }

        #[test]
        fn commit_replaces_symlinks_but_refuses_directories() {
            let (_dir, root) = playground();
            let fs_adapter = StdFileSystem::new(IdentitySwitch::KeepCurrent);
            let id = identity();
            fs::write(format!("{root}/target.txt"), b"original").unwrap();
            symlink("target.txt", format!("{root}/link")).unwrap();
            let mut sink = fs_adapter.begin_write(&id, &root, 0o644).unwrap();
            sink.write_chunk(b"new").unwrap();
            sink.commit("link", &id).unwrap();
            assert!(
                !fs::symlink_metadata(format!("{root}/link"))
                    .unwrap()
                    .is_symlink()
            );
            assert_eq!(fs::read(format!("{root}/target.txt")).unwrap(), b"original");
            fs::create_dir(format!("{root}/sub")).unwrap();
            let sink = fs_adapter.begin_write(&id, &root, 0o644).unwrap();
            assert_eq!(
                sink.commit("sub", &id).unwrap_err(),
                FsIoError::IsADirectory
            );
            assert_eq!(temp_files(&root), 0);
        }

        #[test]
        fn open_read_refuses_symlinks_and_directories() {
            let (_dir, root) = playground();
            let fs_adapter = StdFileSystem::new(IdentitySwitch::KeepCurrent);
            let id = identity();
            fs::write(format!("{root}/f"), b"x").unwrap();
            symlink("f", format!("{root}/l")).unwrap();
            assert_eq!(
                fs_adapter.open_read(&id, &format!("{root}/l")).err(),
                Some(FsIoError::IsSymlink)
            );
            assert_eq!(
                fs_adapter.open_read(&id, &root).err(),
                Some(FsIoError::IsADirectory)
            );
            assert_eq!(
                fs_adapter.open_read(&id, &format!("{root}/nope")).err(),
                Some(FsIoError::NotFound)
            );
            let mut bytes = Vec::new();
            fs_adapter
                .open_read(&id, &format!("{root}/f"))
                .unwrap()
                .read_to_end(&mut bytes)
                .unwrap();
            assert_eq!(bytes, b"x");
        }

        /// A FIFO with no writer must be refused at once, not block the
        /// pool thread until a writer shows up.
        #[test]
        fn open_read_refuses_a_fifo_without_blocking() {
            let (_dir, root) = playground();
            let fs_adapter = StdFileSystem::new(IdentitySwitch::KeepCurrent);
            let id = identity();
            let fifo = format!("{root}/pipe");
            nix::unistd::mkfifo(fifo.as_str(), nix::sys::stat::Mode::S_IRWXU).unwrap();
            let (done_tx, done_rx) = std::sync::mpsc::channel();
            std::thread::spawn(move || {
                let _ = done_tx.send(fs_adapter.open_read(&id, &fifo).err());
            });
            let outcome = done_rx
                .recv_timeout(std::time::Duration::from_secs(5))
                .expect("open_read of a FIFO returned");
            assert_eq!(outcome, Some(FsIoError::NotARegularFile));
        }

        fn owner(value: &str) -> FileMetadata {
            FileMetadata::parse([("Owner", value)]).unwrap()
        }

        #[cfg(target_os = "linux")]
        #[test]
        fn metadata_set_before_commit_appears_with_the_content_and_an_overwrite_clears_it() {
            let (_dir, root) = playground();
            let fs_adapter = StdFileSystem::new(IdentitySwitch::KeepCurrent);
            let id = identity();
            let mut sink = fs_adapter.begin_write(&id, &root, 0o644).unwrap();
            sink.write_chunk(b"v1").unwrap();
            sink.set_metadata(&owner("alice")).unwrap();
            sink.commit("f", &id).unwrap();
            let path = format!("{root}/f");
            assert_eq!(
                fs_adapter.read_metadata(&id, &path).unwrap(),
                owner("alice")
            );
            let replaced = fs_adapter.begin_write(&id, &root, 0o644).unwrap();
            replaced.commit("f", &id).unwrap();
            assert!(fs_adapter.read_metadata(&id, &path).unwrap().is_empty());
            assert!(
                fs_adapter
                    .read_metadata(&id, &format!("{root}/missing"))
                    .is_err()
            );
        }

        #[cfg(target_os = "linux")]
        #[test]
        fn read_metadata_never_follows_a_symlink_and_skips_foreign_attributes() {
            let (_dir, root) = playground();
            let fs_adapter = StdFileSystem::new(IdentitySwitch::KeepCurrent);
            let id = identity();
            let mut sink = fs_adapter.begin_write(&id, &root, 0o644).unwrap();
            sink.set_metadata(&owner("alice")).unwrap();
            sink.commit("target", &id).unwrap();
            symlink("target", format!("{root}/link")).unwrap();
            assert!(
                fs_adapter
                    .read_metadata(&id, &format!("{root}/link"))
                    .unwrap()
                    .is_empty()
            );
            assert_eq!(
                fs_adapter
                    .read_metadata(&id, &format!("{root}/target"))
                    .unwrap(),
                owner("alice")
            );
        }

        /// An identity that may not search the directory reads an empty
        /// set instead of failing (needs root to switch identities).
        #[cfg(target_os = "linux")]
        #[test]
        fn unreadable_metadata_is_empty() {
            if !nix::unistd::getuid().is_root() {
                return;
            }
            let (_dir, root) = playground();
            let fs_adapter = StdFileSystem::new(IdentitySwitch::KeepCurrent);
            let mut sink = fs_adapter.begin_write(&identity(), &root, 0o644).unwrap();
            sink.set_metadata(&owner("alice")).unwrap();
            sink.commit("f", &identity()).unwrap();
            fs::set_permissions(&root, Permissions::from_mode(0o700)).unwrap();
            let stranger = FsIdentity {
                uid: 4_242,
                gid: 4_242,
                home: "/tmp".to_owned(),
            };
            let enforcing = StdFileSystem::new(IdentitySwitch::Enforce);
            assert!(
                enforcing
                    .read_metadata(&stranger, &format!("{root}/f"))
                    .unwrap()
                    .is_empty()
            );
        }

        #[test]
        fn open_snapshot_measures_the_descriptor_and_reads_by_offset() {
            let (_dir, root) = playground();
            let fs_adapter = StdFileSystem::new(IdentitySwitch::KeepCurrent);
            let id = identity();
            let path = format!("{root}/f");
            fs::write(&path, b"0123456789").unwrap();
            let opened = fs_adapter.open_snapshot(&id, &path).unwrap();
            assert_eq!(opened.entry.size, 10);
            assert_eq!(opened.entry.kind, EntryKind::File);
            fs::rename(&path, format!("{root}/moved")).unwrap();
            fs::write(&path, b"other").unwrap();
            let mut buffer = [0u8; 4];
            assert_eq!(opened.file.read_at(&mut buffer, 6).unwrap(), 4);
            assert_eq!(&buffer, b"6789");
            assert_eq!(opened.file.read_at(&mut buffer, 10).unwrap(), 0);
        }

        #[test]
        fn open_snapshot_refuses_symlinks_and_fifos_without_blocking() {
            let (_dir, root) = playground();
            let fs_adapter = StdFileSystem::new(IdentitySwitch::KeepCurrent);
            let id = identity();
            fs::write(format!("{root}/f"), b"x").unwrap();
            symlink("f", format!("{root}/l")).unwrap();
            assert_eq!(
                fs_adapter.open_snapshot(&id, &format!("{root}/l")).err(),
                Some(FsIoError::IsSymlink)
            );
            let fifo = format!("{root}/pipe");
            nix::unistd::mkfifo(fifo.as_str(), nix::sys::stat::Mode::S_IRWXU).unwrap();
            let (done_tx, done_rx) = std::sync::mpsc::channel();
            std::thread::spawn(move || {
                let _ = done_tx.send(fs_adapter.open_snapshot(&id, &fifo).err());
            });
            let outcome = done_rx
                .recv_timeout(std::time::Duration::from_secs(5))
                .expect("open_snapshot of a FIFO returned");
            assert_eq!(outcome, Some(FsIoError::NotARegularFile));
        }

        #[test]
        fn lstat_read_dir_make_dir_rename_and_remove() {
            let (_dir, root) = playground();
            let fs_adapter = StdFileSystem::new(IdentitySwitch::KeepCurrent);
            let id = identity();
            fs::write(format!("{root}/f"), b"abc").unwrap();
            symlink("f", format!("{root}/l")).unwrap();
            let link = fs_adapter.lstat(&id, &format!("{root}/l")).unwrap();
            assert_eq!(link.kind, EntryKind::Symlink);
            assert_eq!(link.symlink_target.as_deref(), Some("f"));
            let mut names: Vec<String> = fs_adapter
                .read_dir(&id, &root)
                .unwrap()
                .into_iter()
                .map(|entry| entry.name)
                .collect();
            names.sort();
            assert_eq!(names, vec!["f", "l"]);
            fs_adapter
                .make_dir(&id, &format!("{root}/d/e"), 0o755)
                .unwrap();
            assert_eq!(
                fs_adapter.make_dir(&id, &format!("{root}/d/e"), 0o755),
                Err(FsIoError::AlreadyExists)
            );
            assert_eq!(
                fs_adapter.make_dir(&id, &format!("{root}/f/x"), 0o755),
                Err(FsIoError::NotADirectory)
            );
            assert_eq!(
                fs_adapter.canonicalize(&id, &format!("{root}/f/x")),
                Err(FsIoError::NotADirectory)
            );
            assert_eq!(
                fs_adapter.rename(&id, &format!("{root}/f"), &format!("{root}/d")),
                Err(FsIoError::IsADirectory)
            );
            fs_adapter
                .rename(&id, &format!("{root}/f"), &format!("{root}/d/f"))
                .unwrap();
            assert_eq!(
                fs_adapter.remove(&id, &format!("{root}/d"), EntryKind::Directory, false),
                Err(FsIoError::NotEmpty)
            );
            fs_adapter
                .remove(&id, &format!("{root}/d"), EntryKind::Directory, true)
                .unwrap();
            fs_adapter
                .remove(&id, &format!("{root}/l"), EntryKind::Symlink, false)
                .unwrap();
            assert_eq!(fs_adapter.read_dir(&id, &root).unwrap().len(), 0);
        }
    }
}

/// `user.rayito.*` extended attributes through `libc`: the set is written
/// on a descriptor and read back with the `l*` calls, so no symlink is ever
/// followed. Values are capped at the domain's byte budget; a name listing
/// that changes between the size query and the read is read again once.
#[cfg(target_os = "linux")]
mod xattr {
    use std::ffi::CString;
    use std::fs::File;
    use std::io;
    use std::os::fd::AsRawFd;

    use nix::errno::Errno;
    use rayd_core::filesystem::{FileMetadata, FsIoError, METADATA_MAX_BYTES};

    use super::unix::io_error;

    pub fn set_metadata(file: &File, metadata: &FileMetadata) -> Result<(), FsIoError> {
        for (key, value) in metadata.iter() {
            let name = c_string(&FileMetadata::xattr_name(key))?;
            set_one(file, &name, value.as_bytes()).map_err(|error| set_error(&error))?;
        }
        Ok(())
    }

    /// A file without attributes, a filesystem without them or one the
    /// identity may not read answers an empty set.
    pub fn read_metadata(path: &str) -> Result<FileMetadata, FsIoError> {
        let path = c_string(path)?;
        let names = match list_names(&path) {
            Ok(names) => names,
            Err(error) if is_unreadable(&error) => return Ok(FileMetadata::default()),
            Err(error) => return Err(io_error(&error)),
        };
        let attributes = names
            .split(|byte| *byte == 0)
            .filter_map(|raw| std::str::from_utf8(raw).ok())
            .filter(|name| FileMetadata::is_metadata_attribute(name))
            .filter_map(|name| {
                let value = get_one(&path, &c_string(name).ok()?).ok()?;
                Some((name.to_owned(), value))
            });
        Ok(FileMetadata::from_xattrs(attributes))
    }

    fn is_unreadable(error: &io::Error) -> bool {
        matches!(
            error.raw_os_error().map(Errno::from_raw),
            Some(Errno::ENOTSUP | Errno::ENODATA | Errno::EACCES | Errno::EPERM)
        )
    }

    /// `ENOTSUP` is a filesystem without user attributes; `E2BIG` and
    /// `ENOSPC` are a set that does not fit the inode.
    fn set_error(error: &io::Error) -> FsIoError {
        match error.raw_os_error().map(Errno::from_raw) {
            Some(Errno::ENOTSUP) => FsIoError::Unsupported,
            Some(Errno::E2BIG | Errno::ENOSPC) => FsIoError::NoSpace,
            _ => io_error(error),
        }
    }

    fn c_string(value: &str) -> Result<CString, FsIoError> {
        CString::new(value).map_err(|_| FsIoError::Other {
            errno: "EINVAL".to_owned(),
        })
    }

    /// `fsetxattr(2)` with flags 0 (create or replace). Sound: `name` is a
    /// NUL-terminated string and `value` a live slice whose length is
    /// passed with it; the descriptor stays open for the whole call.
    fn set_one(file: &File, name: &CString, value: &[u8]) -> io::Result<()> {
        let result = unsafe {
            libc::fsetxattr(
                file.as_raw_fd(),
                name.as_ptr(),
                value.as_ptr().cast(),
                value.len(),
                0,
            )
        };
        if result == 0 {
            Ok(())
        } else {
            Err(io::Error::last_os_error())
        }
    }

    /// `llistxattr(2)`: a size query, then the read into a buffer of that
    /// size. Sound: the buffer outlives both calls and its exact length is
    /// passed; a null buffer with size 0 is the documented size query.
    fn list_names(path: &CString) -> io::Result<Vec<u8>> {
        for _ in 0..2 {
            let size = unsafe { libc::llistxattr(path.as_ptr(), std::ptr::null_mut(), 0) };
            let size = usize::try_from(size).map_err(|_| io::Error::last_os_error())?;
            let mut buffer = vec![0u8; size];
            let read = unsafe {
                libc::llistxattr(path.as_ptr(), buffer.as_mut_ptr().cast(), buffer.len())
            };
            match usize::try_from(read) {
                Ok(read) => {
                    buffer.truncate(read);
                    return Ok(buffer);
                }
                Err(_) if Errno::last() == Errno::ERANGE => {}
                Err(_) => return Err(io::Error::last_os_error()),
            }
        }
        Err(io::Error::from_raw_os_error(Errno::ERANGE as i32))
    }

    /// `lgetxattr(2)` into a buffer of the whole metadata budget: a value
    /// larger than that is not one the write rules could have stored.
    /// Sound for the same reasons as `list_names`.
    fn get_one(path: &CString, name: &CString) -> io::Result<Vec<u8>> {
        let mut buffer = vec![0u8; METADATA_MAX_BYTES];
        let read = unsafe {
            libc::lgetxattr(
                path.as_ptr(),
                name.as_ptr(),
                buffer.as_mut_ptr().cast(),
                buffer.len(),
            )
        };
        let read = usize::try_from(read).map_err(|_| io::Error::last_os_error())?;
        buffer.truncate(read);
        Ok(buffer)
    }
}

/// Unix hosts other than Linux (a developer's laptop) have no `user.*`
/// namespace with these signatures: writes refuse, reads are empty.
#[cfg(all(unix, not(target_os = "linux")))]
mod xattr {
    use std::fs::File;

    use rayd_core::filesystem::{FileMetadata, FsIoError};

    pub fn set_metadata(_file: &File, metadata: &FileMetadata) -> Result<(), FsIoError> {
        if metadata.is_empty() {
            Ok(())
        } else {
            Err(FsIoError::Unsupported)
        }
    }

    pub fn read_metadata(_path: &str) -> Result<FileMetadata, FsIoError> {
        Ok(FileMetadata::default())
    }
}

#[cfg(not(unix))]
mod unsupported {
    use std::io::Read;

    use rayd_core::filesystem::{
        EntryKind, FileMetadata, FileSystem, FsIdentity, FsIoError, OpenedSnapshot, RawEntry,
        WriteSink,
    };

    use crate::adapters::IdentitySwitch;

    #[derive(Debug, Default, Clone, Copy)]
    pub struct UnsupportedFileSystem;

    impl UnsupportedFileSystem {
        #[must_use]
        pub fn new(_identity_switch: IdentitySwitch) -> Self {
            Self
        }
    }

    impl FileSystem for UnsupportedFileSystem {
        fn canonicalize(&self, _id: &FsIdentity, _path: &str) -> Result<String, FsIoError> {
            Err(FsIoError::Unsupported)
        }

        fn lstat(&self, _id: &FsIdentity, _path: &str) -> Result<RawEntry, FsIoError> {
            Err(FsIoError::Unsupported)
        }

        fn read_dir(&self, _id: &FsIdentity, _path: &str) -> Result<Vec<RawEntry>, FsIoError> {
            Err(FsIoError::Unsupported)
        }

        fn open_read(
            &self,
            _id: &FsIdentity,
            _path: &str,
        ) -> Result<Box<dyn Read + Send>, FsIoError> {
            Err(FsIoError::Unsupported)
        }

        fn open_snapshot(
            &self,
            _id: &FsIdentity,
            _path: &str,
        ) -> Result<OpenedSnapshot, FsIoError> {
            Err(FsIoError::Unsupported)
        }

        fn read_metadata(&self, _id: &FsIdentity, _path: &str) -> Result<FileMetadata, FsIoError> {
            Err(FsIoError::Unsupported)
        }

        fn free_bytes(&self, _id: &FsIdentity, _canonical_dir: &str) -> Result<u64, FsIoError> {
            Err(FsIoError::Unsupported)
        }

        fn begin_write(
            &self,
            _id: &FsIdentity,
            _dir: &str,
            _mode: u32,
        ) -> Result<Box<dyn WriteSink>, FsIoError> {
            Err(FsIoError::Unsupported)
        }

        fn make_dir(&self, _id: &FsIdentity, _path: &str, _mode: u32) -> Result<(), FsIoError> {
            Err(FsIoError::Unsupported)
        }

        fn rename(&self, _id: &FsIdentity, _from: &str, _to: &str) -> Result<(), FsIoError> {
            Err(FsIoError::Unsupported)
        }

        fn remove(
            &self,
            _id: &FsIdentity,
            _path: &str,
            _kind: EntryKind,
            _recursive: bool,
        ) -> Result<(), FsIoError> {
            Err(FsIoError::Unsupported)
        }
    }
}
