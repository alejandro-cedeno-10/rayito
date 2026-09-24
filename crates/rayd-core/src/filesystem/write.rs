//! The `Write` client-stream as a state machine: a message with `path`
//! begins a file (committing the open one), a message without it appends,
//! and the end of the stream commits the last file. The bytes themselves
//! never enter the domain; only their length is checked here. The disk
//! reserve rule (`DISK_RESERVE_BYTES`) is applied by `FilesystemOps` when a
//! file begins.

use super::error::FilesystemError;
use super::metadata::FileMetadata;
use super::{DEFAULT_FILE_MODE, MAX_WRITE_CHUNK_BYTES, MODE_MASK};

/// Free space the destination filesystem must keep for `rayd`'s own needs
/// (`/run/rayito` sockets and connection files, the sidecar's stderr, the
/// write temporaries) before `Write` accepts another file.
pub const DISK_RESERVE_BYTES: u64 = 256 * 1024 * 1024;

/// The reserve rule on one `free_bytes` reading.
pub fn check_disk_reserve(free_bytes: u64) -> Result<(), FilesystemError> {
    if free_bytes < DISK_RESERVE_BYTES {
        return Err(FilesystemError::DiskReserve);
    }
    Ok(())
}

/// One `WriteRequest` without its bytes. `metadata` is the raw map as it
/// came; the session validates it.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct WriteMessage {
    pub path: Option<String>,
    pub user: Option<String>,
    pub mode: Option<u32>,
    pub metadata: Vec<(String, String)>,
    pub chunk_len: usize,
}

/// What the application layer does for one message, in order.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum WriteStep {
    Begin {
        path: String,
        user: Option<String>,
        mode: u32,
        metadata: FileMetadata,
    },
    Append {
        len: usize,
    },
    Commit,
}

#[derive(Debug, Default)]
pub struct WriteSession {
    open: bool,
    files: usize,
}

impl WriteSession {
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// Files begun so far, committed or not.
    #[must_use]
    pub fn files(&self) -> usize {
        self.files
    }

    pub fn accept(&mut self, message: WriteMessage) -> Result<Vec<WriteStep>, FilesystemError> {
        if message.chunk_len > MAX_WRITE_CHUNK_BYTES {
            return Err(FilesystemError::ChunkTooLarge {
                max: MAX_WRITE_CHUNK_BYTES,
            });
        }
        let mut steps = Vec::with_capacity(3);
        if let Some(path) = message.path {
            let mode = message.mode.unwrap_or(DEFAULT_FILE_MODE);
            if mode > MODE_MASK {
                return Err(FilesystemError::InvalidMode(mode));
            }
            let metadata = FileMetadata::parse(message.metadata)
                .map_err(|_| FilesystemError::InvalidMetadata)?;
            if self.open {
                steps.push(WriteStep::Commit);
            }
            self.open = true;
            self.files += 1;
            steps.push(WriteStep::Begin {
                path,
                user: message.user,
                mode,
                metadata,
            });
        } else {
            if !self.open {
                return Err(FilesystemError::MissingPath);
            }
            if message.user.is_some() {
                return Err(FilesystemError::UserWithoutPath);
            }
            if message.mode.is_some() {
                return Err(FilesystemError::ModeWithoutPath);
            }
            if !message.metadata.is_empty() {
                return Err(FilesystemError::MetadataWithoutPath);
            }
        }
        if message.chunk_len > 0 {
            steps.push(WriteStep::Append {
                len: message.chunk_len,
            });
        }
        Ok(steps)
    }

    /// End of the client stream: commits the open file; a stream that never
    /// began a file is an error.
    pub fn finish(&mut self) -> Result<Option<WriteStep>, FilesystemError> {
        if self.files == 0 {
            return Err(FilesystemError::NoFiles);
        }
        if self.open {
            self.open = false;
            return Ok(Some(WriteStep::Commit));
        }
        Ok(None)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn begin(path: &str, chunk_len: usize) -> WriteMessage {
        WriteMessage {
            path: Some(path.to_owned()),
            chunk_len,
            ..WriteMessage::default()
        }
    }

    fn append(chunk_len: usize) -> WriteMessage {
        WriteMessage {
            chunk_len,
            ..WriteMessage::default()
        }
    }

    #[test]
    fn disk_reserve_refuses_below_the_threshold_only() {
        assert_eq!(
            check_disk_reserve(10 * 1024 * 1024),
            Err(FilesystemError::DiskReserve)
        );
        assert_eq!(
            check_disk_reserve(DISK_RESERVE_BYTES - 1),
            Err(FilesystemError::DiskReserve)
        );
        assert_eq!(check_disk_reserve(DISK_RESERVE_BYTES), Ok(()));
        assert_eq!(check_disk_reserve(300 * 1024 * 1024), Ok(()));
    }

    #[test]
    fn one_file_begins_appends_and_commits_at_the_end() {
        let mut session = WriteSession::new();
        assert_eq!(
            session.accept(begin("/tmp/a", 10)).unwrap(),
            vec![
                WriteStep::Begin {
                    path: "/tmp/a".to_owned(),
                    user: None,
                    mode: 0o644,
                    metadata: FileMetadata::default(),
                },
                WriteStep::Append { len: 10 }
            ]
        );
        assert_eq!(
            session.accept(append(5)).unwrap(),
            vec![WriteStep::Append { len: 5 }]
        );
        assert_eq!(session.finish().unwrap(), Some(WriteStep::Commit));
        assert_eq!(session.finish().unwrap(), None);
        assert_eq!(session.files(), 1);
    }

    #[test]
    fn a_path_switch_commits_the_previous_file_first() {
        let mut session = WriteSession::new();
        session.accept(begin("/tmp/a", 1)).unwrap();
        let steps = session.accept(begin("/tmp/b", 0)).unwrap();
        assert_eq!(
            steps,
            vec![
                WriteStep::Commit,
                WriteStep::Begin {
                    path: "/tmp/b".to_owned(),
                    user: None,
                    mode: 0o644,
                    metadata: FileMetadata::default(),
                }
            ]
        );
        assert_eq!(session.files(), 2);
    }

    #[test]
    fn empty_first_chunk_only_begins() {
        let mut session = WriteSession::new();
        let steps = session.accept(begin("/tmp/empty", 0)).unwrap();
        assert_eq!(steps.len(), 1);
        assert!(matches!(steps[0], WriteStep::Begin { .. }));
    }

    #[test]
    fn first_message_without_path_is_refused() {
        let mut session = WriteSession::new();
        assert_eq!(session.accept(append(3)), Err(FilesystemError::MissingPath));
        assert_eq!(session.finish(), Err(FilesystemError::NoFiles));
    }

    #[test]
    fn oversized_chunks_are_refused_before_anything_else() {
        let mut session = WriteSession::new();
        assert_eq!(
            session.accept(begin("/tmp/a", MAX_WRITE_CHUNK_BYTES + 1)),
            Err(FilesystemError::ChunkTooLarge {
                max: MAX_WRITE_CHUNK_BYTES
            })
        );
        assert_eq!(session.files(), 0);
        assert!(
            session
                .accept(begin("/tmp/a", MAX_WRITE_CHUNK_BYTES))
                .is_ok()
        );
    }

    #[test]
    fn mode_defaults_and_is_validated() {
        let mut session = WriteSession::new();
        let mut message = begin("/tmp/a", 0);
        message.mode = Some(0o600);
        assert!(matches!(
            session.accept(message).unwrap()[0],
            WriteStep::Begin { mode: 0o600, .. }
        ));
        let mut bad = begin("/tmp/b", 0);
        bad.mode = Some(0o10000);
        assert_eq!(
            session.accept(bad),
            Err(FilesystemError::InvalidMode(0o10000))
        );
    }

    #[test]
    fn user_and_mode_need_a_path() {
        let mut session = WriteSession::new();
        let mut first = begin("/tmp/a", 0);
        first.user = Some("user".to_owned());
        assert!(matches!(
            &session.accept(first).unwrap()[0],
            WriteStep::Begin { user: Some(user), .. } if user == "user"
        ));
        let mut with_user = append(1);
        with_user.user = Some("root".to_owned());
        assert_eq!(
            session.accept(with_user),
            Err(FilesystemError::UserWithoutPath)
        );
        let mut with_mode = append(1);
        with_mode.mode = Some(0o600);
        assert_eq!(
            session.accept(with_mode),
            Err(FilesystemError::ModeWithoutPath)
        );
    }

    #[test]
    fn metadata_is_validated_with_the_file_start() {
        let mut session = WriteSession::new();
        let mut first = begin("/tmp/a", 0);
        first.metadata = vec![("Owner".to_owned(), "alice".to_owned())];
        let steps = session.accept(first).unwrap();
        let WriteStep::Begin { metadata, .. } = &steps[0] else {
            panic!("expected a begin step, got {steps:?}");
        };
        assert_eq!(
            metadata.iter().collect::<Vec<_>>(),
            vec![("owner", "alice")]
        );
        let mut bad = begin("/tmp/b", 0);
        bad.metadata = vec![("a b".to_owned(), "x".to_owned())];
        assert_eq!(session.accept(bad), Err(FilesystemError::InvalidMetadata));
    }

    #[test]
    fn metadata_needs_a_path() {
        let mut session = WriteSession::new();
        session.accept(begin("/tmp/a", 1)).unwrap();
        let mut with_metadata = append(1);
        with_metadata.metadata = vec![("k".to_owned(), "v".to_owned())];
        assert_eq!(
            session.accept(with_metadata),
            Err(FilesystemError::MetadataWithoutPath)
        );
    }
}
