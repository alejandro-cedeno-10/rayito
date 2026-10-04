//! The `FileSystem` port over blocking descriptor-based syscalls, every
//! call wrapped in the requesting user's filesystem identity (design
//! D2/D3). `canonicalize` is the domain's `realpath`; every other call
//! opens the parent of its canonical path with `dir_walk` (one component
//! at a time, `O_PATH | O_NOFOLLOW`, refusing a symlink and a directory on
//! `proc`/`sysfs`/`devpts` as `Redirected`) and reaches the final
//! component with an `*at` call relative to that descriptor: `fstatat`,
//! `O_NOFOLLOW` reads, temp-file writes created with `O_EXCL` and
//! committed by `renameat`, `mkdirat`, `renameat`, and a removal that
//! empties each directory through its own descriptor. So the deny list the
//! domain checked on the path string still describes the object each call
//! acts on (`sec-rayd-agent-hardening`, RAYD-01). The `statvfs` behind the
//! disk reserve, the export snapshot (`fstat` + `pread` on one descriptor)
//! and the `user.rayito.*` metadata (`fsetxattr` on the temp file's
//! descriptor, reads through the entry's own `O_PATH` descriptor,
//! `m9-file-transfer` D17) work the same way. Off Unix every call answers
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
    use std::ffi::{CStr, CString};
    use std::fmt::Write as _;
    use std::fs::{File, Permissions};
    use std::io::{self, Read, Write};
    use std::os::fd::{AsFd, OwnedFd};
    use std::os::unix::fs::{FileExt, PermissionsExt};
    use std::path::Path;

    use nix::dir::Dir;
    use nix::errno::Errno;
    use nix::fcntl::{AtFlags, OFlag, openat, readlinkat, renameat};
    use nix::sys::stat::{FileStat, Mode, SFlag, fstat, fstatat, mkdirat};
    use nix::sys::statvfs::fstatvfs;
    use nix::unistd::{Gid, Uid, UnlinkatFlags, fchown, unlinkat};
    use rayd_core::code::RandomSource;
    use rayd_core::filesystem::{
        DEFAULT_DIR_MODE, EntryKind, FileMetadata, FileSystem, FsIdentity, FsIoError, MODE_MASK,
        OpenedSnapshot, RawEntry, SnapshotFile, TEMP_PREFIX, WriteSink,
    };

    use super::xattr;
    use crate::adapters::IdentitySwitch;
    use crate::adapters::dir_walk::{MissingDir, WalkError, open_dir_beneath, open_step};
    use crate::adapters::fs_identity::FsIdentityGuard;
    use crate::adapters::random::OsRandomSource;

    /// Where every walk starts: the domain hands over canonical absolute
    /// paths.
    const FILESYSTEM_ROOT: &str = "/";
    /// The final component of the root itself, for the calls that act on
    /// `/` (`Stat`, `ListDir` of the root).
    const CURRENT_DIR: &str = ".";
    /// `O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK`, regular files only.
    /// `O_NONBLOCK` is there for the open, not the reads: `open(2)` of a FIFO
    /// without a writer blocks forever otherwise, parking a pool thread;
    /// with it the FIFO opens at once and the regular-file check refuses
    /// it. Reads of a regular file ignore the flag.
    const READ_FLAGS: OFlag = OFlag::O_RDONLY
        .union(OFlag::O_NOFOLLOW)
        .union(OFlag::O_CLOEXEC)
        .union(OFlag::O_NONBLOCK);
    /// A directory opened to read its entries or to `fsync` it.
    const LIST_FLAGS: OFlag = OFlag::O_RDONLY
        .union(OFlag::O_DIRECTORY)
        .union(OFlag::O_NOFOLLOW)
        .union(OFlag::O_CLOEXEC);
    /// The temp file of a write: created, never reused, never a symlink.
    const TEMP_FLAGS: OFlag = OFlag::O_RDWR
        .union(OFlag::O_CREAT)
        .union(OFlag::O_EXCL)
        .union(OFlag::O_NOFOLLOW)
        .union(OFlag::O_CLOEXEC);
    /// Mode of the temp file until the commit's `fchmod`: owner-only, as
    /// `tempfile` creates it.
    const TEMP_FILE_MODE: u32 = 0o600;
    /// Random bytes behind a temp name (12 hex characters), and how many
    /// names are tried before giving up on `EEXIST`.
    const TEMP_SUFFIX_BYTES: usize = 6;
    const TEMP_NAME_ATTEMPTS: usize = 8;

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

    /// Every call below acts on descriptors (design D2/D3, `sec-rayd-agent-hardening`):
    /// the parent of a canonical path is opened by `dir_walk`, one
    /// component at a time without following a symlink, and checked
    /// against the kernel filesystems the deny list keeps out; the final
    /// component is then reached with an `*at` call relative to it. So the
    /// object a call touches is the one the domain's deny check saw, even
    /// when the sandbox swaps a component in between (RAYD-01).
    impl FileSystem for StdFileSystem {
        /// The domain's resolution step: the deny check runs on this
        /// string, and nothing else here ever follows a symlink.
        fn canonicalize(&self, id: &FsIdentity, path: &str) -> Result<String, FsIoError> {
            let _guard = self.enter(id)?;
            std::fs::canonicalize(path)
                .map(|canonical| canonical.to_string_lossy().into_owned())
                .map_err(|error| io_error(&error))
        }

        fn lstat(&self, id: &FsIdentity, path: &str) -> Result<RawEntry, FsIoError> {
            let _guard = self.enter(id)?;
            let (parent, name) = open_parent(path)?;
            entry_at(&parent, name, entry_name(path))
        }

        fn read_dir(&self, id: &FsIdentity, path: &str) -> Result<Vec<RawEntry>, FsIoError> {
            let _guard = self.enter(id)?;
            let dir = open_directory(path, MissingDir::Fail)?;
            Ok(child_names(&dir)?
                .into_iter()
                .filter_map(|name| {
                    let shown = name.to_string_lossy().into_owned();
                    entry_at(&dir, name.as_c_str(), shown).ok()
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
            let (parent, name) = open_parent(path)?;
            let (file, _) = open_regular(&parent, name)?;
            Ok(Box::new(file))
        }

        /// The same open as `open_read`; the entry is the `fstat` of that
        /// descriptor, so the export reads the inode it measured even if the
        /// name is replaced afterwards.
        fn open_snapshot(&self, id: &FsIdentity, path: &str) -> Result<OpenedSnapshot, FsIoError> {
            let _guard = self.enter(id)?;
            let (parent, name) = open_parent(path)?;
            let (file, stat) = open_regular(&parent, name)?;
            Ok(OpenedSnapshot {
                entry: raw_entry(entry_name(path), &stat, None),
                file: Box::new(PositionedFile(file)),
            })
        }

        /// A parent the identity may not search reads an empty set, like an
        /// entry it may not read.
        fn read_metadata(&self, id: &FsIdentity, path: &str) -> Result<FileMetadata, FsIoError> {
            let _guard = self.enter(id)?;
            match open_parent(path) {
                Ok((parent, name)) => xattr::read_metadata_at(&parent, name),
                Err(FsIoError::PermissionDenied) => Ok(FileMetadata::default()),
                Err(error) => Err(error),
            }
        }

        /// `f_bavail * f_frsize` of the deepest existing ancestor of the
        /// directory: what an unprivileged writer may still use, root's
        /// reserved blocks excluded.
        fn free_bytes(&self, id: &FsIdentity, canonical_dir: &str) -> Result<u64, FsIoError> {
            let _guard = self.enter(id)?;
            let existing = deepest_existing_directory(canonical_dir)?;
            let stats = fstatvfs(existing.as_fd()).map_err(errno_error)?;
            Ok(widen(stats.blocks_available()).saturating_mul(widen(stats.fragment_size())))
        }

        fn begin_write(
            &self,
            id: &FsIdentity,
            dir: &str,
            mode: u32,
        ) -> Result<Box<dyn WriteSink>, FsIoError> {
            let _guard = self.enter(id)?;
            let dir = open_directory(dir, MissingDir::Create(DEFAULT_DIR_MODE))?;
            let (file, temp_name) = create_temp_file(&dir)?;
            Ok(Box::new(TempWriteSink {
                file,
                dir,
                temp_name,
                committed: false,
                mode,
                identity_switch: self.identity_switch,
            }))
        }

        fn make_dir(&self, id: &FsIdentity, path: &str, mode: u32) -> Result<(), FsIoError> {
            let _guard = self.enter(id)?;
            let (dir, name) = split_parent(path);
            let parent = open_directory(dir, MissingDir::Create(DEFAULT_DIR_MODE))?;
            mkdirat(parent.as_fd(), name, Mode::from_bits_truncate(mode)).map_err(errno_error)
        }

        fn rename(&self, id: &FsIdentity, from: &str, to: &str) -> Result<(), FsIoError> {
            let _guard = self.enter(id)?;
            let (from_parent, from_name) = open_parent(from)?;
            let (to_parent, to_name) = open_parent(to)?;
            renameat(from_parent.as_fd(), from_name, to_parent.as_fd(), to_name)
                .map_err(errno_error)
        }

        fn remove(
            &self,
            id: &FsIdentity,
            path: &str,
            kind: EntryKind,
            recursive: bool,
        ) -> Result<(), FsIoError> {
            let _guard = self.enter(id)?;
            let (parent, name) = open_parent(path)?;
            match (kind, recursive) {
                (EntryKind::Directory, true) => remove_tree(&parent, name),
                (EntryKind::Directory, false) => {
                    unlinkat(parent.as_fd(), name, UnlinkatFlags::RemoveDir).map_err(errno_error)
                }
                _ => {
                    unlinkat(parent.as_fd(), name, UnlinkatFlags::NoRemoveDir).map_err(errno_error)
                }
            }
        }
    }

    /// A `.rayito-tmp-*` file next to its destination, held by descriptor
    /// together with its directory. Dropping it without `commit` unlinks it
    /// from that same directory.
    pub struct TempWriteSink {
        file: File,
        dir: OwnedFd,
        temp_name: String,
        committed: bool,
        mode: u32,
        identity_switch: IdentitySwitch,
    }

    impl WriteSink for TempWriteSink {
        fn write_chunk(&mut self, bytes: &[u8]) -> Result<(), FsIoError> {
            self.file.write_all(bytes).map_err(|error| io_error(&error))
        }

        /// On the descriptor with the agent's own rights, like `fchmod`: no
        /// path is resolved, so the set lands on this inode and appears with
        /// its content at the rename.
        fn set_metadata(&mut self, metadata: &FileMetadata) -> Result<(), FsIoError> {
            xattr::set_metadata(&self.file, metadata)
        }

        /// `fsync`, `fchmod` and `fchown` run on the descriptor with the
        /// agent's own rights (the temp file is already the user's); the
        /// `renameat` inside the sink's directory, the directory `fsync`
        /// and the final `fstatat` run under the user's identity like every
        /// other operation.
        fn commit(
            self: Box<Self>,
            final_name: &str,
            id: &FsIdentity,
        ) -> Result<RawEntry, FsIoError> {
            let mut this = *self;
            this.file.sync_all().map_err(|error| io_error(&error))?;
            this.file
                .set_permissions(Permissions::from_mode(this.mode))
                .map_err(|error| io_error(&error))?;
            if this.identity_switch == IdentitySwitch::Enforce {
                fchown(
                    &this.file,
                    Some(Uid::from_raw(id.uid)),
                    Some(Gid::from_raw(id.gid)),
                )
                .map_err(errno_error)?;
            }
            let _guard = FsIdentityGuard::enter(this.identity_switch, id)?;
            renameat(
                this.dir.as_fd(),
                this.temp_name.as_str(),
                this.dir.as_fd(),
                final_name,
            )
            .map_err(errno_error)?;
            this.committed = true;
            sync_directory(&this.dir);
            entry_at(&this.dir, final_name, final_name.to_owned())
        }
    }

    impl Drop for TempWriteSink {
        /// Relative to the sink's own directory descriptor and by the name
        /// this sink created, so it can only ever remove its own temp file.
        fn drop(&mut self) {
            if !self.committed {
                let _ = unlinkat(
                    self.dir.as_fd(),
                    self.temp_name.as_str(),
                    UnlinkatFlags::NoRemoveDir,
                );
            }
        }
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

    /// `(parent, name)` of a canonical path; the root is its own parent,
    /// named `.`.
    fn split_parent(path: &str) -> (&str, &str) {
        match path.rfind('/') {
            Some(_) if path == FILESYSTEM_ROOT => (FILESYSTEM_ROOT, CURRENT_DIR),
            Some(0) => (FILESYSTEM_ROOT, &path[1..]),
            Some(cut) => (&path[..cut], &path[cut + 1..]),
            None => (FILESYSTEM_ROOT, path),
        }
    }

    /// The parent directory of `path`, opened without following any
    /// component, and the final name to reach from it.
    fn open_parent(path: &str) -> Result<(OwnedFd, &str), FsIoError> {
        let (dir, name) = split_parent(path);
        Ok((open_directory(dir, MissingDir::Fail)?, name))
    }

    /// Every component of `path`, the last included, opened by `dir_walk`;
    /// the result is checked against the kernel filesystems.
    fn open_directory(path: &str, missing: MissingDir) -> Result<OwnedFd, FsIoError> {
        let dir =
            open_dir_beneath(Path::new(FILESYSTEM_ROOT), path, missing).map_err(walk_error)?;
        refuse_kernel_filesystem(&dir)?;
        Ok(dir)
    }

    /// The deepest directory of `path` that exists, for `statvfs`: the walk
    /// stops at the first missing component instead of failing.
    fn deepest_existing_directory(path: &str) -> Result<OwnedFd, FsIoError> {
        let mut current = root_directory()?;
        for component in path.split('/').filter(|part| !part.is_empty()) {
            match open_step(&current, component) {
                Ok(next) => current = next,
                Err(WalkError::Io(Errno::ENOENT)) => break,
                Err(error) => return Err(walk_error(error)),
            }
        }
        refuse_kernel_filesystem(&current)?;
        Ok(current)
    }

    fn root_directory() -> Result<OwnedFd, FsIoError> {
        open_dir_beneath(Path::new(FILESYSTEM_ROOT), "", MissingDir::Fail).map_err(walk_error)
    }

    /// Defence in depth behind the walk: a directory on `proc`, `sysfs` or
    /// `devpts` is never one a filesystem RPC may act in, whatever path led
    /// there (the deny list covers their usual mountpoints; this covers any
    /// other).
    #[cfg(target_os = "linux")]
    fn refuse_kernel_filesystem(dir: &OwnedFd) -> Result<(), FsIoError> {
        use nix::sys::statfs::{DEVPTS_SUPER_MAGIC, PROC_SUPER_MAGIC, SYSFS_MAGIC, fstatfs};
        let kind = fstatfs(dir.as_fd()).map_err(errno_error)?.filesystem_type();
        if [PROC_SUPER_MAGIC, SYSFS_MAGIC, DEVPTS_SUPER_MAGIC].contains(&kind) {
            return Err(FsIoError::Redirected);
        }
        Ok(())
    }

    /// No kernel filesystems with these magic numbers off Linux.
    #[cfg(not(target_os = "linux"))]
    fn refuse_kernel_filesystem(_dir: &OwnedFd) -> Result<(), FsIoError> {
        Ok(())
    }

    /// `lstat` of `name` inside `parent`, with the symlink target read the
    /// same way.
    fn entry_at<P: ?Sized + nix::NixPath>(
        parent: &OwnedFd,
        name: &P,
        shown: String,
    ) -> Result<RawEntry, FsIoError> {
        let stat =
            fstatat(parent.as_fd(), name, AtFlags::AT_SYMLINK_NOFOLLOW).map_err(errno_error)?;
        let symlink_target = (kind_of(&stat) == EntryKind::Symlink)
            .then(|| readlinkat(parent.as_fd(), name).ok())
            .flatten()
            .map(|target| target.to_string_lossy().into_owned());
        Ok(raw_entry(shown, &stat, symlink_target))
    }

    /// Every entry name of a directory but `.` and `..`, read through a
    /// fresh descriptor so `dir` stays usable for the `*at` calls.
    fn child_names(dir: &OwnedFd) -> Result<Vec<CString>, FsIoError> {
        let listing =
            openat(dir.as_fd(), CURRENT_DIR, LIST_FLAGS, Mode::empty()).map_err(errno_error)?;
        let mut entries = Dir::from_fd(listing).map_err(errno_error)?;
        Ok(entries
            .iter()
            .filter_map(Result::ok)
            .map(|entry| entry.file_name().to_owned())
            .filter(|name| !is_dot_entry(name))
            .collect())
    }

    fn is_dot_entry(name: &CStr) -> bool {
        matches!(name.to_bytes(), b"." | b"..")
    }

    /// `name` inside `parent` with `READ_FLAGS`, regular files only.
    fn open_regular(parent: &OwnedFd, name: &str) -> Result<(File, FileStat), FsIoError> {
        let fd = openat(parent.as_fd(), name, READ_FLAGS, Mode::empty()).map_err(errno_error)?;
        let stat = fstat(fd.as_fd()).map_err(errno_error)?;
        match kind_of(&stat) {
            EntryKind::File => Ok((File::from(fd), stat)),
            EntryKind::Directory => Err(FsIoError::IsADirectory),
            EntryKind::Symlink | EntryKind::Other => Err(FsIoError::NotARegularFile),
        }
    }

    /// `O_CREAT | O_EXCL | O_NOFOLLOW` inside the directory descriptor,
    /// under a random name; a name that already exists is retried.
    fn create_temp_file(dir: &OwnedFd) -> Result<(File, String), FsIoError> {
        for _ in 0..TEMP_NAME_ATTEMPTS {
            let name = temp_name()?;
            match openat(
                dir.as_fd(),
                name.as_str(),
                TEMP_FLAGS,
                Mode::from_bits_truncate(TEMP_FILE_MODE),
            ) {
                Ok(fd) => return Ok((File::from(fd), name)),
                Err(Errno::EEXIST) => {}
                Err(errno) => return Err(errno_error(errno)),
            }
        }
        Err(FsIoError::AlreadyExists)
    }

    fn temp_name() -> Result<String, FsIoError> {
        let mut suffix = [0u8; TEMP_SUFFIX_BYTES];
        OsRandomSource
            .fill(&mut suffix)
            .map_err(|_| FsIoError::Other {
                errno: "EIO".to_owned(),
            })?;
        Ok(suffix
            .iter()
            .fold(TEMP_PREFIX.to_owned(), |mut name, byte| {
                let _ = write!(name, "{byte:02x}");
                name
            }))
    }

    /// The rename is already durable in the file's own inode; a directory
    /// `fsync` that fails (an unreadable parent) must not undo a commit
    /// that happened.
    fn sync_directory(dir: &OwnedFd) {
        let synced = openat(dir.as_fd(), CURRENT_DIR, LIST_FLAGS, Mode::empty())
            .map_err(|errno| io::Error::from_raw_os_error(errno as i32))
            .and_then(|fd| File::from(fd).sync_all());
        if let Err(error) = synced {
            tracing::debug!(reason = %io_error(&error), "directory fsync skipped");
        }
    }

    /// One directory being emptied by `remove_tree`: its descriptor, its
    /// name inside its parent and the children still to remove.
    struct RemovalFrame {
        dir: OwnedFd,
        name: CString,
        pending: Vec<CString>,
    }

    /// What opening a child as a directory to empty it found.
    enum Opened {
        Directory(RemovalFrame),
        /// A file, a symlink (never followed) or anything else: unlinked
        /// as it is.
        NotADirectory,
        /// Gone between the listing and the open (a race with the sandbox).
        Gone,
    }

    /// Recursive removal relative to descriptors, depth first, without
    /// recursion on the stack and without following a symlink at any
    /// depth: each directory is opened `O_NOFOLLOW` beneath its parent's
    /// descriptor, emptied, then removed with `unlinkat(AT_REMOVEDIR)` from
    /// that parent.
    fn remove_tree(parent: &OwnedFd, name: &str) -> Result<(), FsIoError> {
        let name = CString::new(name).map_err(|_| FsIoError::Other {
            errno: "EINVAL".to_owned(),
        })?;
        let mut stack = match open_for_removal(parent, name.clone())? {
            Opened::Directory(frame) => vec![frame],
            Opened::NotADirectory => return Err(FsIoError::NotADirectory),
            Opened::Gone => return Err(FsIoError::NotFound),
        };
        while let Some(top) = stack.last_mut() {
            let Some(child) = top.pending.pop() else {
                let finished = stack.pop();
                let parent_dir = stack.last().map_or(parent, |frame| &frame.dir);
                if let Some(finished) = finished {
                    unlinkat(
                        parent_dir.as_fd(),
                        finished.name.as_c_str(),
                        UnlinkatFlags::RemoveDir,
                    )
                    .map_err(errno_error)?;
                }
                continue;
            };
            match open_for_removal(&top.dir, child.clone())? {
                Opened::Directory(frame) => stack.push(frame),
                Opened::NotADirectory => {
                    match unlinkat(
                        top.dir.as_fd(),
                        child.as_c_str(),
                        UnlinkatFlags::NoRemoveDir,
                    ) {
                        Ok(()) | Err(Errno::ENOENT) => {}
                        Err(errno) => return Err(errno_error(errno)),
                    }
                }
                Opened::Gone => {}
            }
        }
        Ok(())
    }

    fn open_for_removal(parent: &OwnedFd, name: CString) -> Result<Opened, FsIoError> {
        let dir = match openat(parent.as_fd(), name.as_c_str(), LIST_FLAGS, Mode::empty()) {
            Ok(dir) => dir,
            Err(Errno::ENOTDIR | Errno::ELOOP) => return Ok(Opened::NotADirectory),
            Err(Errno::ENOENT) => return Ok(Opened::Gone),
            Err(errno) => return Err(errno_error(errno)),
        };
        let pending = child_names(&dir)?;
        Ok(Opened::Directory(RemovalFrame { dir, name, pending }))
    }

    /// `statvfs` field widths differ by target (`u32` on some, `u64` on the
    /// ARM64 musl build); a generic widening keeps both lint-clean.
    fn widen(value: impl Into<u64>) -> u64 {
        value.into()
    }

    fn kind_of(stat: &FileStat) -> EntryKind {
        let format = SFlag::from_bits_truncate(stat.st_mode) & SFlag::S_IFMT;
        if format == SFlag::S_IFLNK {
            EntryKind::Symlink
        } else if format == SFlag::S_IFDIR {
            EntryKind::Directory
        } else if format == SFlag::S_IFREG {
            EntryKind::File
        } else {
            EntryKind::Other
        }
    }

    fn raw_entry(name: String, stat: &FileStat, symlink_target: Option<String>) -> RawEntry {
        RawEntry {
            name,
            kind: kind_of(stat),
            size: u64::try_from(stat.st_size).unwrap_or(0),
            mode: stat.st_mode & MODE_MASK,
            uid: stat.st_uid,
            gid: stat.st_gid,
            modified_ms: stat
                .st_mtime
                .saturating_mul(1_000)
                .saturating_add(stat.st_mtime_nsec / 1_000_000),
            symlink_target,
        }
    }

    fn entry_name(path: &str) -> String {
        path.rsplit('/')
            .find(|component| !component.is_empty())
            .unwrap_or(FILESYSTEM_ROOT)
            .to_owned()
    }

    fn walk_error(error: WalkError) -> FsIoError {
        match error {
            WalkError::Symlink => FsIoError::Redirected,
            WalkError::NotADirectory => FsIoError::NotADirectory,
            WalkError::Io(errno) => errno_error(errno),
        }
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
        use std::fs;
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

        /// RAYD-01 at the adapter: the domain resolved `<root>/a/<name>`
        /// while `a` was a directory and deny-checked that string; the
        /// sandbox then swapped `a` for a symlink into a tree it must not
        /// reach. No call follows it: each answers `Redirected` and the
        /// tree behind the link is untouched.
        #[test]
        fn an_intermediate_symlink_swapped_in_after_resolution_is_never_followed() {
            let (_dir, root) = playground();
            let fs_adapter = StdFileSystem::new(IdentitySwitch::KeepCurrent);
            let id = identity();
            let denied = format!("{root}/denied");
            fs::create_dir(&denied).unwrap();
            fs::write(format!("{denied}/secret"), b"secret").unwrap();
            fs::create_dir(format!("{denied}/sub")).unwrap();
            symlink(&denied, format!("{root}/a")).unwrap();
            let through = |name: &str| format!("{root}/a/{name}");
            let redirected = Some(FsIoError::Redirected);
            assert_eq!(
                fs_adapter.open_read(&id, &through("secret")).err(),
                redirected
            );
            assert_eq!(
                fs_adapter.open_snapshot(&id, &through("secret")).err(),
                redirected
            );
            assert_eq!(fs_adapter.lstat(&id, &through("secret")).err(), redirected);
            assert_eq!(
                fs_adapter.read_metadata(&id, &through("secret")).err(),
                redirected
            );
            assert_eq!(fs_adapter.read_dir(&id, &through("sub")).err(), redirected);
            assert_eq!(
                fs_adapter.read_dir(&id, &format!("{root}/a")).err(),
                redirected
            );
            assert_eq!(
                fs_adapter.free_bytes(&id, &through("sub")).err(),
                redirected
            );
            assert_eq!(
                fs_adapter.begin_write(&id, &through("sub"), 0o644).err(),
                redirected
            );
            assert_eq!(
                fs_adapter.make_dir(&id, &through("new"), 0o755).err(),
                redirected
            );
            assert_eq!(
                fs_adapter
                    .rename(&id, &through("secret"), &format!("{root}/stolen"))
                    .err(),
                redirected
            );
            assert_eq!(
                fs_adapter
                    .remove(&id, &through("secret"), EntryKind::File, false)
                    .err(),
                redirected
            );
            assert_eq!(
                fs_adapter
                    .remove(&id, &through("sub"), EntryKind::Directory, true)
                    .err(),
                redirected
            );
            assert_eq!(fs::read(format!("{denied}/secret")).unwrap(), b"secret");
            assert!(fs::symlink_metadata(format!("{denied}/new")).is_err());
            assert!(fs::symlink_metadata(format!("{root}/stolen")).is_err());
            assert_eq!(temp_files(&format!("{denied}/sub")), 0);
            assert_eq!(
                fs_adapter.lstat(&id, &format!("{root}/a")).unwrap().kind,
                EntryKind::Symlink,
                "the link itself is an entry like any other"
            );
            fs_adapter
                .remove(&id, &format!("{root}/a"), EntryKind::Symlink, false)
                .unwrap();
            assert!(fs::symlink_metadata(format!("{denied}/secret")).is_ok());
        }

        /// A recursive removal empties every level through descriptors:
        /// a symlink at any depth is unlinked, never followed.
        #[test]
        fn recursive_removal_never_follows_a_symlink_at_any_depth() {
            let (_dir, root) = playground();
            let fs_adapter = StdFileSystem::new(IdentitySwitch::KeepCurrent);
            let id = identity();
            fs::create_dir_all(format!("{root}/outside/keep")).unwrap();
            fs::write(format!("{root}/outside/keep/file"), b"x").unwrap();
            fs::create_dir_all(format!("{root}/tree/a/b/c")).unwrap();
            fs::write(format!("{root}/tree/a/b/c/leaf"), b"y").unwrap();
            fs::write(format!("{root}/tree/top"), b"z").unwrap();
            symlink(format!("{root}/outside"), format!("{root}/tree/a/b/escape")).unwrap();
            symlink(format!("{root}/outside/keep"), format!("{root}/tree/link")).unwrap();
            fs_adapter
                .remove(&id, &format!("{root}/tree"), EntryKind::Directory, true)
                .unwrap();
            assert!(fs::symlink_metadata(format!("{root}/tree")).is_err());
            assert_eq!(fs::read(format!("{root}/outside/keep/file")).unwrap(), b"x");
        }

        /// The defence behind the walk: a directory on `proc` or `sysfs`
        /// is refused whatever path reached it.
        #[cfg(target_os = "linux")]
        #[test]
        fn kernel_filesystems_are_refused_by_descriptor() {
            let fs_adapter = StdFileSystem::new(IdentitySwitch::KeepCurrent);
            let id = identity();
            for path in ["/proc", "/sys"] {
                assert_eq!(
                    fs_adapter.read_dir(&id, path).err(),
                    Some(FsIoError::Redirected),
                    "{path}"
                );
            }
            assert_eq!(
                fs_adapter.lstat(&id, "/proc/version").err(),
                Some(FsIoError::Redirected)
            );
            assert_eq!(
                fs_adapter.open_read(&id, "/proc/self/status").err(),
                Some(FsIoError::Redirected)
            );
        }
    }
}

/// `user.rayito.*` extended attributes through `libc`: the set is written
/// on a descriptor and read back through the descriptor of the entry
/// itself (`O_PATH | O_NOFOLLOW` beneath its parent's), so no symlink is
/// ever followed. Values are capped at the domain's byte budget; a name
/// listing that changes between the size query and the read is read again
/// once.
#[cfg(target_os = "linux")]
mod xattr {
    use std::ffi::CString;
    use std::fs::File;
    use std::io;
    use std::os::fd::{AsFd, AsRawFd, OwnedFd};

    use nix::errno::Errno;
    use nix::fcntl::{OFlag, openat};
    use nix::sys::stat::{Mode, SFlag, fstat};
    use rayd_core::filesystem::{FileMetadata, FsIoError, METADATA_MAX_BYTES};

    use super::unix::io_error;

    /// The entry whose attributes are read: `O_PATH` (the file itself is
    /// not opened, so no FUSE open and no read permission for this step),
    /// `O_NOFOLLOW` (a symlink is the entry, never its target).
    const ENTRY_FLAGS: OFlag = OFlag::O_PATH
        .union(OFlag::O_NOFOLLOW)
        .union(OFlag::O_CLOEXEC);
    /// `proc(5)`'s per-descriptor magic links: `listxattr`/`getxattr` of
    /// `/proc/self/fd/<n>` act on exactly the inode `<n>` names (an
    /// `O_PATH` descriptor answers `EBADF` to `flistxattr`).
    const PROC_SELF_FD: &str = "/proc/self/fd";

    pub fn set_metadata(file: &File, metadata: &FileMetadata) -> Result<(), FsIoError> {
        for (key, value) in metadata.iter() {
            let name = c_string(&FileMetadata::xattr_name(key))?;
            set_one(file, &name, value.as_bytes()).map_err(|error| set_error(&error))?;
        }
        Ok(())
    }

    /// `user.*` attributes exist only on regular files and directories: a
    /// symlink or any other kind, a file without attributes, a filesystem
    /// without them or one the identity may not read answers an empty set;
    /// a missing entry is `NotFound`.
    pub fn read_metadata_at(parent: &OwnedFd, name: &str) -> Result<FileMetadata, FsIoError> {
        let entry = match openat(parent.as_fd(), name, ENTRY_FLAGS, Mode::empty()) {
            Ok(entry) => entry,
            Err(Errno::EACCES | Errno::EPERM) => return Ok(FileMetadata::default()),
            Err(errno) => return Err(io_error(&io::Error::from_raw_os_error(errno as i32))),
        };
        let stat = fstat(entry.as_fd())
            .map_err(|errno| io_error(&io::Error::from_raw_os_error(errno as i32)))?;
        let format = SFlag::from_bits_truncate(stat.st_mode) & SFlag::S_IFMT;
        if format != SFlag::S_IFREG && format != SFlag::S_IFDIR {
            return Ok(FileMetadata::default());
        }
        let path = c_string(&format!("{PROC_SELF_FD}/{}", entry.as_raw_fd()))?;
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

    /// `ENOENT` here is a `/proc` that is not mounted: the entry itself
    /// was just opened.
    fn is_unreadable(error: &io::Error) -> bool {
        matches!(
            error.raw_os_error().map(Errno::from_raw),
            Some(Errno::ENOTSUP | Errno::ENODATA | Errno::EACCES | Errno::EPERM | Errno::ENOENT)
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

    /// `listxattr(2)` of a `/proc/self/fd/<n>` magic link (followed to the
    /// inode it names, never further: that inode is a regular file or a
    /// directory): a size query, then the read into a buffer of that size.
    /// Sound: the buffer outlives both calls and its exact length is
    /// passed; a null buffer with size 0 is the documented size query.
    fn list_names(path: &CString) -> io::Result<Vec<u8>> {
        for _ in 0..2 {
            let size = unsafe { libc::listxattr(path.as_ptr(), std::ptr::null_mut(), 0) };
            let size = usize::try_from(size).map_err(|_| io::Error::last_os_error())?;
            let mut buffer = vec![0u8; size];
            let read =
                unsafe { libc::listxattr(path.as_ptr(), buffer.as_mut_ptr().cast(), buffer.len()) };
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

    /// `getxattr(2)` of the same magic link into a buffer of the whole
    /// metadata budget: a value larger than that is not one the write rules
    /// could have stored. Sound for the same reasons as `list_names`.
    fn get_one(path: &CString, name: &CString) -> io::Result<Vec<u8>> {
        let mut buffer = vec![0u8; METADATA_MAX_BYTES];
        let read = unsafe {
            libc::getxattr(
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

    pub fn read_metadata_at(
        _parent: &std::os::fd::OwnedFd,
        _name: &str,
    ) -> Result<FileMetadata, FsIoError> {
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
