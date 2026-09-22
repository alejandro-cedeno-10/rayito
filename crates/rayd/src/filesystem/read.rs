//! `Read` (design D5): the file is opened under the user's identity on the
//! pool, then one blocking reader fills 256 KiB chunks into a four-deep
//! channel; HTTP/2 flow control plus the channel bound the memory per
//! stream, and a client that cancels drops the receiver, which stops the
//! reader on its next send.

use std::io::{self, Read};

use rayd_core::filesystem::{FilesystemError, READ_CHUNK_BYTES};
use tokio::sync::mpsc;
use tokio_stream::wrappers::ReceiverStream;

use super::manager::FilesystemManager;
use crate::adapters::io_error_name;
use crate::lifecycle::SuspendAware;

pub const READ_PIPELINE_DEPTH: usize = 4;

pub type ReadStream = ReceiverStream<Result<Vec<u8>, FilesystemError>>;

/// Dropping the receiver stops the reader on its next send and closes the
/// file; the watch pump ends the same way and removes its inotify watches.
impl<T> SuspendAware for ReceiverStream<T> {}

impl FilesystemManager {
    pub async fn read(
        &self,
        user: Option<String>,
        path: String,
    ) -> Result<ReadStream, FilesystemError> {
        self.stream_gate()?;
        let id = self.identity(user.as_deref())?;
        let reader = self
            .blocking(move |ops| ops.prepare_read(&id, &path))
            .await?;
        Ok(spawn_reader(reader))
    }
}

#[must_use]
pub fn spawn_reader(reader: Box<dyn Read + Send>) -> ReadStream {
    let (sender, receiver) = mpsc::channel(READ_PIPELINE_DEPTH);
    tokio::task::spawn_blocking(move || pump(reader, &sender));
    ReceiverStream::new(receiver)
}

fn pump(mut reader: Box<dyn Read + Send>, sender: &mpsc::Sender<Result<Vec<u8>, FilesystemError>>) {
    let mut chunks = 0usize;
    let mut bytes = 0usize;
    loop {
        let chunk = match fill_chunk(reader.as_mut()) {
            Ok(chunk) if chunk.is_empty() => break,
            Ok(chunk) => chunk,
            Err(error) => {
                let failure = FilesystemError::Io {
                    operation: "read",
                    errno: io_error_name(&error),
                };
                let _ = sender.blocking_send(Err(failure));
                return;
            }
        };
        chunks += 1;
        bytes += chunk.len();
        if sender.blocking_send(Ok(chunk)).is_err() {
            tracing::debug!(rpc = "Read", chunks, bytes, "client stopped reading");
            return;
        }
    }
    tracing::debug!(rpc = "Read", chunks, bytes, "read completed");
}

/// Short reads are looped so every chunk but the last is exactly
/// `READ_CHUNK_BYTES`.
fn fill_chunk(reader: &mut dyn Read) -> io::Result<Vec<u8>> {
    let mut buffer = vec![0u8; READ_CHUNK_BYTES];
    let mut filled = 0usize;
    while filled < READ_CHUNK_BYTES {
        match reader.read(&mut buffer[filled..]) {
            Ok(0) => break,
            Ok(read) => filled += read,
            Err(error) if error.kind() == io::ErrorKind::Interrupted => {}
            Err(error) => return Err(error),
        }
    }
    buffer.truncate(filled);
    Ok(buffer)
}

#[cfg(test)]
mod tests {
    use std::io::Cursor;

    use tokio_stream::StreamExt;

    use super::*;

    struct ShortReads {
        inner: Cursor<Vec<u8>>,
    }

    impl Read for ShortReads {
        fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
            let limit = buffer.len().min(1_000);
            self.inner.read(&mut buffer[..limit])
        }
    }

    struct Failing;

    impl Read for Failing {
        fn read(&mut self, _buffer: &mut [u8]) -> io::Result<usize> {
            Err(io::Error::other("boom"))
        }
    }

    #[tokio::test]
    async fn chunks_are_full_size_except_the_last() {
        let bytes = vec![3u8; 600 * 1024];
        let stream = spawn_reader(Box::new(ShortReads {
            inner: Cursor::new(bytes.clone()),
        }));
        let chunks: Vec<Vec<u8>> = stream.map(Result::unwrap).collect().await;
        let sizes: Vec<usize> = chunks.iter().map(Vec::len).collect();
        assert_eq!(sizes, vec![262_144, 262_144, 90_112]);
        assert_eq!(chunks.concat(), bytes);
    }

    #[tokio::test]
    async fn empty_files_produce_no_chunks() {
        let stream = spawn_reader(Box::new(Cursor::new(Vec::new())));
        let chunks: Vec<_> = stream.collect().await;
        assert!(chunks.is_empty());
    }

    #[tokio::test]
    async fn read_errors_end_the_stream_with_a_trailing_error() {
        let mut stream = spawn_reader(Box::new(Failing));
        let first = stream.next().await.unwrap().unwrap_err();
        assert!(matches!(
            first,
            FilesystemError::Io {
                operation: "read",
                ..
            }
        ));
        assert!(stream.next().await.is_none());
    }
}
