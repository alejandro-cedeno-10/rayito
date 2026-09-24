//! The `HomeArchiver` port over `tar` + `flate2` + `std::fs` (design
//! D3/D4), entered through `FsIdentityGuard` exactly like `StdFileSystem`
//! so every `open`, `read`, `lstat`, `mkdir` and `unpack` happens with the
//! user's rights. The walk is a sorted depth-first traversal that never
//! follows symlinked directories nor crosses to another filesystem; the
//! unpack accepts regular files, directories and symlinks only, refuses
//! absolute names and paths that leave the home, and drops
//! setuid/setgid/sticky bits. Errors and logs carry errno names and
//! counts, never a path or a name.

#[cfg(unix)]
pub use unix::TarHomeArchiver;
#[cfg(unix)]
pub type PlatformHomeArchiver = unix::TarHomeArchiver;

#[cfg(not(unix))]
pub use unsupported::UnsupportedHomeArchiver;
#[cfg(not(unix))]
pub type PlatformHomeArchiver = unsupported::UnsupportedHomeArchiver;

#[cfg(unix)]
mod unix {
    use std::fs::{self, File, Metadata};
    use std::io::{self, Read, Write};
    use std::os::unix::fs::MetadataExt;
    use std::path::{Path, PathBuf};
    use std::sync::Arc;

    use flate2::Compression;
    use flate2::read::GzDecoder;
    use flate2::write::GzEncoder;
    use rayd_core::filesystem::{FsIdentity, MODE_MASK};
    use rayd_core::persistence::{
        ArchiveError, ArchivePlan, ArchiveSink, ArchiveSummary, Counters, ExtractSummary,
        HomeArchiver,
    };
    use sha2::{Digest, Sha256};
    use tar::{Archive, Builder, EntryType, Header};

    use crate::adapters::fs_identity::FsIdentityGuard;
    use crate::adapters::std_filesystem::io_error;
    use crate::adapters::{IdentitySwitch, io_error_name};

    /// `tar::Entry::unpack_in` reports an escaping parent with this text.
    const OUTSIDE_DESTINATION: &str = "outside of destination";

    pub struct TarHomeArchiver {
        identity_switch: IdentitySwitch,
    }

    impl TarHomeArchiver {
        #[must_use]
        pub fn new(identity_switch: IdentitySwitch) -> Self {
            Self { identity_switch }
        }

        fn enter(&self, id: &FsIdentity) -> Result<FsIdentityGuard, ArchiveError> {
            FsIdentityGuard::enter(self.identity_switch, id)
                .map_err(|_| ArchiveError::PermissionDenied)
        }
    }

    impl HomeArchiver for TarHomeArchiver {
        fn count(&self, plan: &ArchivePlan) -> Result<ArchiveSummary, ArchiveError> {
            let _guard = self.enter(&plan.identity)?;
            let mut summary = ArchiveSummary::default();
            let walker = Walker::open(plan)?;
            walker.walk(&mut |entry| {
                summary.files += 1;
                if let Node::File { size, .. } = entry.node {
                    summary.bytes_read += size;
                }
                Ok(())
            })?;
            summary.skipped = walker.skipped();
            Ok(summary)
        }

        fn archive(
            &self,
            plan: &ArchivePlan,
            sink: Box<dyn ArchiveSink>,
            counters: Arc<Counters>,
        ) -> Result<ArchiveSummary, ArchiveError> {
            let _guard = self.enter(&plan.identity)?;
            let walker = Walker::open(plan)?;
            let hashing = HashingSink::new(sink);
            let encoder = GzEncoder::new(hashing, Compression::fast());
            let mut builder = Builder::new(encoder);
            builder.follow_symlinks(false);
            let mut appended = Appender {
                builder: &mut builder,
                plan,
                counters: &counters,
                files: 0,
                bytes_read: 0,
                unreadable: 0,
            };
            walker.walk(&mut |entry| appended.append(&entry))?;
            let (files, bytes_read, unreadable) =
                (appended.files, appended.bytes_read, appended.unreadable);
            let encoder = builder
                .into_inner()
                .map_err(|error| archive_write_error(&error))?;
            let hashing = encoder
                .finish()
                .map_err(|error| archive_write_error(&error))?;
            let (archive_bytes, sha256) = hashing
                .finish()
                .map_err(|error| archive_write_error(&error))?;
            Ok(ArchiveSummary {
                files,
                bytes_read,
                archive_bytes,
                sha256,
                skipped: walker.skipped() + unreadable,
            })
        }

        fn extract(
            &self,
            plan: &ArchivePlan,
            source: Box<dyn Read + Send>,
            counters: Arc<Counters>,
        ) -> Result<ExtractSummary, ArchiveError> {
            let _guard = self.enter(&plan.identity)?;
            let home = PathBuf::from(plan.root());
            let decoder = GzDecoder::new(HashingReader::new(source));
            let mut archive = Archive::new(decoder);
            archive.set_overwrite(true);
            archive.set_preserve_permissions(false);
            archive.set_preserve_ownerships(false);
            archive.set_preserve_mtime(true);
            archive.set_unpack_xattrs(false);
            let mut unpacked = Unpacker {
                home: &home,
                counters: &counters,
                files: 0,
                bytes_written: 0,
                skipped: 0,
            };
            for entry in archive
                .entries()
                .map_err(|error| extract_read_error(&error))?
            {
                let entry = entry.map_err(|error| extract_read_error(&error))?;
                unpacked.unpack(entry)?;
            }
            let (archive_bytes, sha256) = archive.into_inner().into_inner().finish()?;
            Ok(ExtractSummary {
                files: unpacked.files,
                bytes_written: unpacked.bytes_written,
                archive_bytes,
                sha256,
                skipped: unpacked.skipped,
            })
        }
    }

    /// What the walk found at one path, with the `lstat` facts the header
    /// needs; `relative` is the home-relative `a/b` path.
    struct WalkEntry<'a> {
        absolute: &'a Path,
        relative: &'a str,
        node: Node,
        mode: u32,
        mtime: u64,
    }

    enum Node {
        File { size: u64 },
        Directory,
        Symlink { target: PathBuf },
    }

    struct Walker<'a> {
        plan: &'a ArchivePlan,
        root: PathBuf,
        root_dev: u64,
        skipped: std::cell::Cell<u64>,
    }

    impl<'a> Walker<'a> {
        fn open(plan: &'a ArchivePlan) -> Result<Self, ArchiveError> {
            let root = PathBuf::from(plan.root());
            let metadata =
                fs::symlink_metadata(&root).map_err(|error| walk_error("lstat", &error))?;
            if !metadata.is_dir() {
                return Err(ArchiveError::Io {
                    operation: "lstat",
                    errno: "ENOTDIR".to_owned(),
                });
            }
            Ok(Self {
                plan,
                root_dev: metadata.dev(),
                root,
                skipped: std::cell::Cell::new(0),
            })
        }

        fn skipped(&self) -> u64 {
            self.skipped.get()
        }

        fn skip(&self) {
            self.skipped.set(self.skipped.get() + 1);
        }

        fn walk(
            &self,
            visit: &mut dyn FnMut(WalkEntry<'_>) -> Result<(), ArchiveError>,
        ) -> Result<(), ArchiveError> {
            self.walk_dir(&self.root, "", visit)
        }

        /// Entries sorted by the byte value of their names; a directory the
        /// user cannot read is skipped as a whole and counted once.
        fn walk_dir(
            &self,
            dir: &Path,
            relative_dir: &str,
            visit: &mut dyn FnMut(WalkEntry<'_>) -> Result<(), ArchiveError>,
        ) -> Result<(), ArchiveError> {
            let mut names: Vec<std::ffi::OsString> = match fs::read_dir(dir) {
                Ok(entries) => entries
                    .filter_map(Result::ok)
                    .map(|entry| entry.file_name())
                    .collect(),
                Err(error) if is_access_error(&error) => {
                    self.skip();
                    return Ok(());
                }
                Err(error) => return Err(walk_error("read_dir", &error)),
            };
            names.sort_unstable_by(|a, b| a.as_encoded_bytes().cmp(b.as_encoded_bytes()));
            for name in names {
                let absolute = dir.join(&name);
                let relative = join_relative(relative_dir, &name.to_string_lossy());
                if self.plan.should_skip(&relative) {
                    continue;
                }
                let Ok(metadata) = fs::symlink_metadata(&absolute) else {
                    self.skip();
                    continue;
                };
                if metadata.dev() != self.root_dev {
                    self.skip();
                    continue;
                }
                let Some(node) = node_of(&absolute, &metadata) else {
                    self.skip();
                    continue;
                };
                let is_dir = matches!(node, Node::Directory);
                visit(WalkEntry {
                    absolute: &absolute,
                    relative: &relative,
                    node,
                    mode: metadata.mode() & MODE_MASK,
                    mtime: u64::try_from(metadata.mtime()).unwrap_or(0),
                })?;
                if is_dir {
                    self.walk_dir(&absolute, &relative, visit)?;
                }
            }
            Ok(())
        }
    }

    fn node_of(absolute: &Path, metadata: &Metadata) -> Option<Node> {
        let file_type = metadata.file_type();
        if file_type.is_symlink() {
            let target = fs::read_link(absolute).ok()?;
            Some(Node::Symlink { target })
        } else if file_type.is_dir() {
            Some(Node::Directory)
        } else if file_type.is_file() {
            Some(Node::File {
                size: metadata.len(),
            })
        } else {
            None
        }
    }

    fn join_relative(dir: &str, name: &str) -> String {
        if dir.is_empty() {
            name.to_owned()
        } else {
            format!("{dir}/{name}")
        }
    }

    fn is_access_error(error: &io::Error) -> bool {
        matches!(
            error.kind(),
            io::ErrorKind::PermissionDenied | io::ErrorKind::NotFound
        )
    }

    struct Appender<'a, W: Write> {
        builder: &'a mut Builder<W>,
        plan: &'a ArchivePlan,
        counters: &'a Counters,
        files: u64,
        bytes_read: u64,
        unreadable: u64,
    }

    impl<W: Write> Appender<'_, W> {
        fn append(&mut self, entry: &WalkEntry<'_>) -> Result<(), ArchiveError> {
            let mut header = self.header(entry);
            match entry.node {
                Node::File { size } => {
                    let Ok(file) = File::open(entry.absolute) else {
                        self.unreadable += 1;
                        self.counters.add_skipped(1);
                        return Ok(());
                    };
                    header.set_entry_type(EntryType::Regular);
                    header.set_size(size);
                    let reader = CappedReader::new(file, size);
                    self.builder
                        .append_data(&mut header, entry.relative, reader)
                        .map_err(|error| archive_write_error(&error))?;
                    self.bytes_read += size;
                    self.counters.add_bytes_read(size);
                }
                Node::Directory => {
                    header.set_entry_type(EntryType::Directory);
                    header.set_size(0);
                    self.builder
                        .append_data(&mut header, entry.relative, io::empty())
                        .map_err(|error| archive_write_error(&error))?;
                }
                Node::Symlink { ref target } => {
                    header.set_entry_type(EntryType::Symlink);
                    header.set_size(0);
                    self.builder
                        .append_link(&mut header, entry.relative, target)
                        .map_err(|error| archive_write_error(&error))?;
                }
            }
            self.files += 1;
            self.counters.add_files_done(1);
            Ok(())
        }

        fn header(&self, entry: &WalkEntry<'_>) -> Header {
            let mut header = Header::new_gnu();
            header.set_mode(entry.mode);
            header.set_mtime(entry.mtime);
            header.set_uid(u64::from(self.plan.identity.uid));
            header.set_gid(u64::from(self.plan.identity.gid));
            let _ = header.set_username(&self.plan.username);
            let _ = header.set_groupname(&self.plan.username);
            header
        }
    }

    /// Reads exactly `remaining` bytes from a live file: a file that shrank
    /// is zero-padded, one that grew is truncated, so the header's size is
    /// always honoured and a changing file never aborts the checkpoint.
    struct CappedReader {
        inner: File,
        remaining: u64,
    }

    impl CappedReader {
        fn new(inner: File, size: u64) -> Self {
            Self {
                inner,
                remaining: size,
            }
        }
    }

    impl Read for CappedReader {
        fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
            if self.remaining == 0 {
                return Ok(0);
            }
            let limit = usize::try_from(self.remaining)
                .map_or(buf.len(), |remaining| remaining.min(buf.len()));
            let read = self.inner.read(&mut buf[..limit])?;
            if read == 0 {
                buf[..limit].fill(0);
                self.remaining -= limit as u64;
                return Ok(limit);
            }
            self.remaining -= read as u64;
            Ok(read)
        }
    }

    /// Counts and hashes the compressed bytes on their way to the sink.
    struct HashingSink {
        sink: Option<Box<dyn ArchiveSink>>,
        hasher: Sha256,
        bytes: u64,
    }

    impl HashingSink {
        fn new(sink: Box<dyn ArchiveSink>) -> Self {
            Self {
                sink: Some(sink),
                hasher: Sha256::new(),
                bytes: 0,
            }
        }

        fn finish(mut self) -> io::Result<(u64, String)> {
            if let Some(sink) = self.sink.take() {
                sink.finish()?;
            }
            Ok((self.bytes, hex(&self.hasher.finalize())))
        }
    }

    impl Write for HashingSink {
        fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
            let Some(sink) = self.sink.as_mut() else {
                return Err(io::Error::from(io::ErrorKind::BrokenPipe));
            };
            sink.write_all(buf)?;
            self.hasher.update(buf);
            self.bytes += buf.len() as u64;
            Ok(buf.len())
        }

        fn flush(&mut self) -> io::Result<()> {
            self.sink.as_mut().map_or(Ok(()), Write::flush)
        }
    }

    /// Counts and hashes the compressed bytes as the decoder pulls them.
    struct HashingReader {
        source: Box<dyn Read + Send>,
        hasher: Sha256,
        bytes: u64,
    }

    impl HashingReader {
        fn new(source: Box<dyn Read + Send>) -> Self {
            Self {
                source,
                hasher: Sha256::new(),
                bytes: 0,
            }
        }

        /// Drains what the decoder left unread (the gzip trailer) so the
        /// hash covers the whole object, then reports it.
        fn finish(mut self) -> Result<(u64, String), ArchiveError> {
            let mut rest = [0u8; 8192];
            loop {
                match self.read(&mut rest) {
                    Ok(0) => break,
                    Ok(_) => {}
                    Err(error) => return Err(extract_read_error(&error)),
                }
            }
            Ok((self.bytes, hex(&self.hasher.finalize())))
        }
    }

    impl Read for HashingReader {
        fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
            let read = self.source.read(buf)?;
            self.hasher.update(&buf[..read]);
            self.bytes += read as u64;
            Ok(read)
        }
    }

    struct Unpacker<'a> {
        home: &'a Path,
        counters: &'a Counters,
        files: u64,
        bytes_written: u64,
        skipped: u64,
    }

    impl Unpacker<'_> {
        fn unpack<R: Read>(&mut self, mut entry: tar::Entry<'_, R>) -> Result<(), ArchiveError> {
            let kind = entry.header().entry_type();
            if !matches!(
                kind,
                EntryType::Regular | EntryType::Directory | EntryType::Symlink
            ) {
                self.skipped += 1;
                self.counters.add_skipped(1);
                return Ok(());
            }
            if is_absolute_name(&entry)? {
                return Err(ArchiveError::EntryRefused);
            }
            let size = entry.header().size().unwrap_or(0);
            let unpacked = entry
                .unpack_in(self.home)
                .map_err(|error| unpack_error(&error))?;
            if !unpacked {
                return Err(ArchiveError::EntryRefused);
            }
            self.files += 1;
            self.counters.add_files_done(1);
            if kind == EntryType::Regular {
                self.bytes_written += size;
                self.counters.add_bytes_written(size);
            }
            Ok(())
        }
    }

    /// `unpack_in` silently re-roots an absolute name under the home; the
    /// spec refuses it instead (an archive `rayd` wrote never has one), so
    /// the name is inspected before the crate gets to relocate it.
    fn is_absolute_name<R: Read>(entry: &tar::Entry<'_, R>) -> Result<bool, ArchiveError> {
        let path = entry.path().map_err(|error| unpack_error(&error))?;
        Ok(path.is_absolute())
    }

    fn hex(bytes: &[u8]) -> String {
        use std::fmt::Write as _;
        bytes.iter().fold(String::new(), |mut acc, byte| {
            let _ = write!(acc, "{byte:02x}");
            acc
        })
    }

    fn walk_error(operation: &'static str, error: &io::Error) -> ArchiveError {
        match io_error(error) {
            rayd_core::filesystem::FsIoError::PermissionDenied => ArchiveError::PermissionDenied,
            _ => ArchiveError::Io {
                operation,
                errno: io_error_name(error),
            },
        }
    }

    /// A write that failed because the channel closed is a cancellation;
    /// anything else is an I/O failure of the walk or the compressor.
    fn archive_write_error(error: &io::Error) -> ArchiveError {
        if error.kind() == io::ErrorKind::BrokenPipe {
            return ArchiveError::Cancelled;
        }
        ArchiveError::Io {
            operation: "archive",
            errno: io_error_name(error),
        }
    }

    fn extract_read_error(error: &io::Error) -> ArchiveError {
        match error.kind() {
            io::ErrorKind::BrokenPipe => ArchiveError::Cancelled,
            _ => ArchiveError::Io {
                operation: "decode",
                errno: io_error_name(error),
            },
        }
    }

    fn unpack_error(error: &io::Error) -> ArchiveError {
        let rendered = error.to_string();
        if rendered.contains(OUTSIDE_DESTINATION) {
            return ArchiveError::EntryRefused;
        }
        let root = innermost(error);
        match io_error(root) {
            rayd_core::filesystem::FsIoError::NoSpace => ArchiveError::DiskFull,
            rayd_core::filesystem::FsIoError::PermissionDenied => ArchiveError::PermissionDenied,
            _ if root.kind() == io::ErrorKind::BrokenPipe => ArchiveError::Cancelled,
            _ => ArchiveError::Io {
                operation: "unpack",
                errno: io_error_name(root),
            },
        }
    }

    /// `tar` wraps the causing `io::Error` in `TarError`; the errno lives
    /// in the innermost one.
    fn innermost(error: &io::Error) -> &io::Error {
        let mut current: &io::Error = error;
        while let Some(inner) = current
            .get_ref()
            .and_then(|source| source.downcast_ref::<io::Error>())
        {
            current = inner;
        }
        current
    }

    #[cfg(test)]
    mod tests {
        use std::os::unix::fs::{PermissionsExt, symlink};

        use rayd_core::persistence::{ExcludeList, PART_BYTES};

        use super::*;

        fn identity(home: &str) -> FsIdentity {
            FsIdentity {
                uid: nix::unistd::getuid().as_raw(),
                gid: nix::unistd::getgid().as_raw(),
                home: home.to_owned(),
            }
        }

        fn plan(home: &str, excludes: &[&str]) -> ArchivePlan {
            ArchivePlan::new(
                identity(home),
                "user".to_owned(),
                ExcludeList::parse(excludes).unwrap(),
            )
        }

        fn playground() -> (tempfile::TempDir, String) {
            let dir = tempfile::tempdir().unwrap();
            let root = fs::canonicalize(dir.path())
                .unwrap()
                .to_string_lossy()
                .into_owned();
            (dir, root)
        }

        /// A sink that keeps everything in memory.
        struct MemorySink(Arc<std::sync::Mutex<(Vec<u8>, bool)>>);

        impl Write for MemorySink {
            fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
                self.0.lock().unwrap().0.extend_from_slice(buf);
                Ok(buf.len())
            }

            fn flush(&mut self) -> io::Result<()> {
                Ok(())
            }
        }

        impl ArchiveSink for MemorySink {
            fn finish(self: Box<Self>) -> io::Result<()> {
                self.0.lock().unwrap().1 = true;
                Ok(())
            }
        }

        struct ClosedSink;

        impl Write for ClosedSink {
            fn write(&mut self, _buf: &[u8]) -> io::Result<usize> {
                Err(io::Error::from(io::ErrorKind::BrokenPipe))
            }

            fn flush(&mut self) -> io::Result<()> {
                Ok(())
            }
        }

        impl ArchiveSink for ClosedSink {
            fn finish(self: Box<Self>) -> io::Result<()> {
                Err(io::Error::from(io::ErrorKind::BrokenPipe))
            }
        }

        fn checkpoint(root: &str, excludes: &[&str]) -> (ArchiveSummary, Vec<u8>) {
            let archiver = TarHomeArchiver::new(IdentitySwitch::KeepCurrent);
            let buffer = Arc::new(std::sync::Mutex::new((Vec::new(), false)));
            let summary = archiver
                .archive(
                    &plan(root, excludes),
                    Box::new(MemorySink(buffer.clone())),
                    Arc::new(Counters::new()),
                )
                .unwrap();
            let (bytes, finished) = std::mem::take(&mut *buffer.lock().unwrap());
            assert!(finished);
            (summary, bytes)
        }

        fn restore(root: &str, archive: &[u8]) -> Result<ExtractSummary, ArchiveError> {
            let archiver = TarHomeArchiver::new(IdentitySwitch::KeepCurrent);
            archiver.extract(
                &plan(root, &[]),
                Box::new(io::Cursor::new(archive.to_vec())),
                Arc::new(Counters::new()),
            )
        }

        fn seed_home(root: &str) {
            fs::create_dir_all(format!("{root}/data/raw")).unwrap();
            fs::create_dir_all(format!("{root}/proj/src")).unwrap();
            fs::create_dir_all(format!("{root}/.cache/pip")).unwrap();
            fs::create_dir_all(format!("{root}/skipme")).unwrap();
            fs::create_dir_all(format!("{root}/.local/share/jupyter/runtime")).unwrap();
            fs::write(format!("{root}/notes.txt"), b"hola").unwrap();
            fs::write(format!("{root}/data/blob.bin"), vec![7u8; 100_000]).unwrap();
            fs::write(format!("{root}/data/raw/x"), b"raw").unwrap();
            fs::write(format!("{root}/proj/src/a.py"), b"print()").unwrap();
            fs::write(format!("{root}/.cache/pip/c"), b"cache").unwrap();
            fs::write(format!("{root}/skipme/s"), b"skip").unwrap();
            fs::write(format!("{root}/.local/share/jupyter/runtime/k.json"), b"{}").unwrap();
            fs::write(format!("{root}/.rayito-tmp-1"), b"tmp").unwrap();
            fs::write(format!("{root}/run.sh"), b"#!/bin/sh\n").unwrap();
            fs::set_permissions(format!("{root}/run.sh"), fs::Permissions::from_mode(0o755))
                .unwrap();
            symlink("data/blob.bin", format!("{root}/link")).unwrap();
            symlink("/etc/hostname", format!("{root}/abs-link")).unwrap();
        }

        #[test]
        fn count_and_archive_agree_and_honour_ignore_and_exclude_lists() {
            let (_dir, root) = playground();
            seed_home(&root);
            let archiver = TarHomeArchiver::new(IdentitySwitch::KeepCurrent);
            let counted = archiver.count(&plan(&root, &["skipme"])).unwrap();
            let (summary, archive) = checkpoint(&root, &["skipme"]);
            assert_eq!(summary.files, counted.files);
            assert_eq!(summary.bytes_read, counted.bytes_read);
            assert_eq!(summary.bytes_read, 4 + 100_000 + 3 + 7 + 10);
            assert_eq!(summary.archive_bytes, archive.len() as u64);
            assert_eq!(summary.sha256.len(), 64);
            let names = entry_names(&archive);
            assert_eq!(
                names,
                vec![
                    ".local",
                    ".local/share",
                    ".local/share/jupyter",
                    "abs-link",
                    "data",
                    "data/blob.bin",
                    "data/raw",
                    "data/raw/x",
                    "link",
                    "notes.txt",
                    "proj",
                    "proj/src",
                    "proj/src/a.py",
                    "run.sh",
                ]
            );
            assert_eq!(summary.files, names.len() as u64);
        }

        fn entry_names(archive: &[u8]) -> Vec<String> {
            let mut names = Vec::new();
            let mut tar = Archive::new(GzDecoder::new(io::Cursor::new(archive)));
            for entry in tar.entries().unwrap() {
                let entry = entry.unwrap();
                names.push(entry.path().unwrap().to_string_lossy().into_owned());
            }
            names
        }

        #[test]
        fn round_trip_restores_contents_modes_and_symlinks_verbatim() {
            let (_src, source) = playground();
            seed_home(&source);
            let (summary, archive) = checkpoint(&source, &["skipme"]);
            let (_dst, destination) = playground();
            fs::write(format!("{destination}/notes.txt"), b"stale").unwrap();
            symlink("/etc/passwd", format!("{destination}/run.sh")).unwrap();
            let restored = restore(&destination, &archive).unwrap();
            assert_eq!(restored.files, summary.files);
            assert_eq!(restored.sha256, summary.sha256);
            assert_eq!(restored.archive_bytes, summary.archive_bytes);
            assert_eq!(restored.bytes_written, summary.bytes_read);
            assert_eq!(
                fs::read(format!("{destination}/notes.txt")).unwrap(),
                b"hola"
            );
            assert_eq!(
                fs::read(format!("{destination}/data/blob.bin")).unwrap(),
                vec![7u8; 100_000]
            );
            let run = fs::symlink_metadata(format!("{destination}/run.sh")).unwrap();
            assert!(run.is_file(), "an existing symlink at the path is replaced");
            assert_eq!(run.mode() & 0o777, 0o755);
            assert_eq!(
                fs::read_link(format!("{destination}/link")).unwrap(),
                PathBuf::from("data/blob.bin")
            );
            assert_eq!(
                fs::read_link(format!("{destination}/abs-link")).unwrap(),
                PathBuf::from("/etc/hostname")
            );
            assert!(!Path::new(&format!("{destination}/.cache")).exists());
            assert!(!Path::new(&format!("{destination}/skipme")).exists());
            assert!(!Path::new(&format!("{destination}/.rayito-tmp-1")).exists());
        }

        #[test]
        fn special_files_and_foreign_devices_are_skipped_not_fatal() {
            let (_dir, root) = playground();
            fs::write(format!("{root}/a"), b"a").unwrap();
            nix::unistd::mkfifo(
                Path::new(&format!("{root}/fifo")),
                nix::sys::stat::Mode::from_bits_truncate(0o644),
            )
            .unwrap();
            let (summary, archive) = checkpoint(&root, &[]);
            assert_eq!(summary.files, 1);
            assert_eq!(summary.skipped, 1);
            assert_eq!(entry_names(&archive), vec!["a"]);
        }

        #[test]
        fn unreadable_entries_are_skipped_and_counted() {
            if nix::unistd::geteuid().is_root() {
                return;
            }
            let (_dir, root) = playground();
            fs::write(format!("{root}/open"), b"o").unwrap();
            fs::write(format!("{root}/closed"), b"c").unwrap();
            fs::set_permissions(format!("{root}/closed"), fs::Permissions::from_mode(0o000))
                .unwrap();
            fs::create_dir(format!("{root}/vault")).unwrap();
            fs::write(format!("{root}/vault/secret"), b"s").unwrap();
            fs::set_permissions(format!("{root}/vault"), fs::Permissions::from_mode(0o000))
                .unwrap();
            let (summary, archive) = checkpoint(&root, &[]);
            fs::set_permissions(format!("{root}/vault"), fs::Permissions::from_mode(0o755))
                .unwrap();
            assert_eq!(entry_names(&archive), vec!["open", "vault"]);
            assert_eq!(summary.files, 2);
            assert_eq!(summary.skipped, 2);
        }

        #[test]
        fn a_file_that_shrinks_while_read_is_padded_to_its_header_size() {
            let (_dir, root) = playground();
            let path = format!("{root}/live");
            fs::write(&path, vec![1u8; 10]).unwrap();
            let file = File::open(&path).unwrap();
            let mut reader = CappedReader::new(file, 16);
            let mut out = Vec::new();
            reader.read_to_end(&mut out).unwrap();
            assert_eq!(out.len(), 16);
            assert_eq!(&out[..10], &[1u8; 10]);
            assert_eq!(&out[10..], &[0u8; 6]);
            let file = File::open(&path).unwrap();
            let mut reader = CappedReader::new(file, 4);
            let mut out = Vec::new();
            reader.read_to_end(&mut out).unwrap();
            assert_eq!(out, vec![1u8; 4]);
        }

        #[test]
        fn a_closed_sink_is_a_cancellation() {
            let (_dir, root) = playground();
            fs::write(format!("{root}/a"), vec![0u8; 4096]).unwrap();
            let archiver = TarHomeArchiver::new(IdentitySwitch::KeepCurrent);
            let error = archiver
                .archive(
                    &plan(&root, &[]),
                    Box::new(ClosedSink),
                    Arc::new(Counters::new()),
                )
                .unwrap_err();
            assert_eq!(error, ArchiveError::Cancelled);
        }

        /// Builds an archive the `tar` crate itself would refuse to write
        /// (`..` and absolute names go into the raw header bytes), the way a
        /// hostile producer would.
        fn crafted(entries: &[(&str, EntryType, &[u8], Option<&str>)]) -> Vec<u8> {
            let mut builder = Builder::new(GzEncoder::new(Vec::new(), Compression::fast()));
            for (path, kind, data, link) in entries {
                let mut header = Header::new_gnu();
                header.set_entry_type(*kind);
                header.set_mode(if *kind == EntryType::Directory {
                    0o755
                } else {
                    0o644
                });
                header.set_size(data.len() as u64);
                if let Some(target) = link {
                    header.set_link_name(target).unwrap();
                }
                let name = header.as_old_mut().name.as_mut();
                name[..path.len()].copy_from_slice(path.as_bytes());
                header.set_cksum();
                builder.append(&header, *data).unwrap();
            }
            builder.into_inner().unwrap().finish().unwrap()
        }

        #[test]
        fn crafted_parent_references_and_absolute_paths_are_refused() {
            let (_dir, root) = playground();
            let escape = crafted(&[("../escape", EntryType::Regular, b"x", None)]);
            assert_eq!(
                restore(&root, &escape).unwrap_err(),
                ArchiveError::EntryRefused
            );
            assert!(!Path::new(&format!("{root}/../escape")).exists());
            let absolute =
                crafted(&[("/tmp/rayito-absolute-probe", EntryType::Regular, b"x", None)]);
            assert_eq!(
                restore(&root, &absolute).unwrap_err(),
                ArchiveError::EntryRefused
            );
            assert!(!Path::new(&format!("{root}/tmp/rayito-absolute-probe")).exists());
            assert!(!Path::new("/tmp/rayito-absolute-probe").exists());
        }

        #[test]
        fn crafted_symlinked_parent_that_escapes_is_refused() {
            let (_outside, outside) = playground();
            let (_dir, root) = playground();
            let archive = crafted(&[
                ("out", EntryType::Symlink, b"", Some(outside.as_str())),
                ("out/planted", EntryType::Regular, b"x", None),
            ]);
            assert_eq!(
                restore(&root, &archive).unwrap_err(),
                ArchiveError::EntryRefused
            );
            assert!(!Path::new(&format!("{outside}/planted")).exists());
        }

        #[test]
        fn device_fifo_and_hard_link_entries_are_skipped_and_setuid_is_masked() {
            let (_dir, root) = playground();
            let mut builder = Builder::new(GzEncoder::new(Vec::new(), Compression::fast()));
            let mut header = Header::new_gnu();
            header.set_entry_type(EntryType::Regular);
            header.set_mode(0o4755);
            header.set_size(2);
            builder
                .append_data(&mut header, "suid", &b"hi"[..])
                .unwrap();
            let mut device = Header::new_gnu();
            device.set_entry_type(EntryType::Char);
            device.set_mode(0o600);
            device.set_size(0);
            device.set_device_major(1).unwrap();
            device.set_device_minor(3).unwrap();
            builder
                .append_data(&mut device, "null", io::empty())
                .unwrap();
            let mut fifo = Header::new_gnu();
            fifo.set_entry_type(EntryType::Fifo);
            fifo.set_mode(0o600);
            fifo.set_size(0);
            builder.append_data(&mut fifo, "pipe", io::empty()).unwrap();
            let mut hard = Header::new_gnu();
            hard.set_entry_type(EntryType::Link);
            hard.set_size(0);
            builder.append_link(&mut hard, "suid2", "suid").unwrap();
            let archive = builder.into_inner().unwrap().finish().unwrap();
            let summary = restore(&root, &archive).unwrap();
            assert_eq!(summary.files, 1);
            assert_eq!(summary.skipped, 3);
            let suid = fs::symlink_metadata(format!("{root}/suid")).unwrap();
            assert_eq!(suid.mode() & 0o7777, 0o755);
            assert!(!Path::new(&format!("{root}/null")).exists());
            assert!(!Path::new(&format!("{root}/pipe")).exists());
            assert!(!Path::new(&format!("{root}/suid2")).exists());
        }

        #[test]
        fn restore_merges_directories_and_overwrites_files() {
            let (_dir, root) = playground();
            fs::create_dir(format!("{root}/d")).unwrap();
            fs::write(format!("{root}/d/keep"), b"k").unwrap();
            fs::write(format!("{root}/d/old"), b"old").unwrap();
            let archive = crafted(&[
                ("d", EntryType::Directory, b"", None),
                ("d/old", EntryType::Regular, b"new", None),
            ]);
            restore(&root, &archive).unwrap();
            assert_eq!(fs::read(format!("{root}/d/keep")).unwrap(), b"k");
            assert_eq!(fs::read(format!("{root}/d/old")).unwrap(), b"new");
        }

        #[test]
        fn a_truncated_archive_fails_the_extract() {
            let (_src, source) = playground();
            fs::write(format!("{source}/a"), vec![5u8; PART_BYTES / 64]).unwrap();
            let (_, archive) = checkpoint(&source, &[]);
            let (_dst, destination) = playground();
            let truncated = &archive[..archive.len() / 2];
            assert!(matches!(
                restore(&destination, truncated).unwrap_err(),
                ArchiveError::Io { .. }
            ));
        }
    }
}

#[cfg(not(unix))]
mod unsupported {
    use std::io::Read;
    use std::sync::Arc;

    use rayd_core::persistence::{
        ArchiveError, ArchivePlan, ArchiveSink, ArchiveSummary, Counters, ExtractSummary,
        HomeArchiver,
    };

    use crate::adapters::IdentitySwitch;

    #[derive(Default)]
    pub struct UnsupportedHomeArchiver;

    impl UnsupportedHomeArchiver {
        #[must_use]
        pub fn new(_identity_switch: IdentitySwitch) -> Self {
            Self
        }
    }

    impl HomeArchiver for UnsupportedHomeArchiver {
        fn count(&self, _plan: &ArchivePlan) -> Result<ArchiveSummary, ArchiveError> {
            Err(ArchiveError::Unsupported)
        }

        fn archive(
            &self,
            _plan: &ArchivePlan,
            _sink: Box<dyn ArchiveSink>,
            _counters: Arc<Counters>,
        ) -> Result<ArchiveSummary, ArchiveError> {
            Err(ArchiveError::Unsupported)
        }

        fn extract(
            &self,
            _plan: &ArchivePlan,
            _source: Box<dyn Read + Send>,
            _counters: Arc<Counters>,
        ) -> Result<ExtractSummary, ArchiveError> {
            Err(ArchiveError::Unsupported)
        }
    }
}
