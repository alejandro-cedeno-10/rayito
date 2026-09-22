//! `Write` (design D6): the client stream drives the core `WriteSession`;
//! each step (`Begin`, `Append`, `Commit`) is one blocking call on the pool.
//! Every committed file stays committed; the open temp file is unlinked by
//! its sink's `Drop` on any error or cancellation; before an error is
//! answered the remaining client messages are drained (bounded) so the
//! proxy delivers the status instead of a stream reset.

use std::time::{Duration, Instant};

use rayd_core::filesystem::{
    Entry, FilesystemError, FsIdentity, NameCache, RequestPath, WriteMessage, WriteSession,
    WriteSink, WriteStep, build_entry,
};
use tokio_stream::{Stream, StreamExt};

use super::manager::{FilesystemManager, join_error};

/// One `WriteRequest`: the domain view of its fields plus the bytes.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct WriteMessageWithChunk {
    pub message: WriteMessage,
    pub chunk: Vec<u8>,
}

/// Either a domain refusal (answered as a gRPC status after the drain),
/// the client's own stream failure returned as it came, or a stream that
/// looked complete but whose transport reported an abort.
#[derive(Debug)]
pub enum WriteFailure<E> {
    Filesystem(FilesystemError),
    Client(E),
    Aborted,
}

impl<E> From<FilesystemError> for WriteFailure<E> {
    fn from(error: FilesystemError) -> Self {
        Self::Filesystem(error)
    }
}

struct OpenFile {
    sink: Box<dyn WriteSink>,
    path: RequestPath,
    name: String,
    identity: FsIdentity,
}

impl FilesystemManager {
    /// `client_aborted` is consulted once the stream ends: the transport may
    /// present a cancelled upload as a clean end (tonic does), and a
    /// cancelled upload must never be committed.
    pub async fn write<S, E, A>(
        &self,
        messages: &mut S,
        client_aborted: A,
    ) -> Result<Vec<Entry>, WriteFailure<E>>
    where
        S: Stream<Item = Result<WriteMessageWithChunk, E>> + Unpin,
        A: Fn() -> bool,
    {
        let started = Instant::now();
        let outcome = self.drive_write(messages, client_aborted).await;
        if let Ok(entries) = &outcome {
            tracing::info!(
                rpc = "Write",
                files = entries.len(),
                duration_ms = started.elapsed().as_millis(),
                "write completed"
            );
        } else {
            let settings = self.settings();
            drain_after_error(
                messages,
                settings.write_error_drain_bytes,
                settings.write_error_drain_timeout,
            )
            .await;
        }
        outcome
    }

    async fn drive_write<S, E, A>(
        &self,
        messages: &mut S,
        client_aborted: A,
    ) -> Result<Vec<Entry>, WriteFailure<E>>
    where
        S: Stream<Item = Result<WriteMessageWithChunk, E>> + Unpin,
        A: Fn() -> bool,
    {
        self.stream_gate()?;
        let mut session = WriteSession::new();
        let mut open: Option<OpenFile> = None;
        let mut entries = Vec::new();
        while let Some(item) = messages.next().await {
            let item = item.map_err(WriteFailure::Client)?;
            let steps = session.accept(item.message)?;
            let mut chunk = Some(item.chunk);
            for step in steps {
                self.apply_write_step(step, &mut open, &mut entries, &mut chunk)
                    .await?;
            }
        }
        if client_aborted() {
            return Err(WriteFailure::Aborted);
        }
        if let Some(step) = session.finish()? {
            self.apply_write_step(step, &mut open, &mut entries, &mut None)
                .await?;
        }
        Ok(entries)
    }

    async fn apply_write_step(
        &self,
        step: WriteStep,
        open: &mut Option<OpenFile>,
        entries: &mut Vec<Entry>,
        chunk: &mut Option<Vec<u8>>,
    ) -> Result<(), FilesystemError> {
        match step {
            WriteStep::Begin { path, user, mode } => {
                let identity = self.identity(user.as_deref())?;
                let id = identity.clone();
                let target = self
                    .blocking(move |ops| ops.write_target(&id, &path, mode))
                    .await?;
                *open = Some(OpenFile {
                    sink: target.sink,
                    path: target.path,
                    name: target.name,
                    identity,
                });
            }
            WriteStep::Append { .. } => {
                let bytes = chunk.take().unwrap_or_default();
                let file = open.take().ok_or(FilesystemError::MissingPath)?;
                *open = Some(append_chunk(file, bytes).await?);
            }
            WriteStep::Commit => {
                let file = open.take().ok_or(FilesystemError::MissingPath)?;
                entries.push(self.commit_file(file).await?);
            }
        }
        Ok(())
    }

    async fn commit_file(&self, file: OpenFile) -> Result<Entry, FilesystemError> {
        let names = self.names();
        tokio::task::spawn_blocking(move || {
            let raw = file
                .sink
                .commit(&file.name, &file.identity)
                .map_err(|error| FilesystemError::from_io("commit", error))?;
            let mut cache = NameCache::new(names.as_ref());
            Ok(build_entry(raw, &file.path, &mut cache))
        })
        .await
        .unwrap_or_else(|error| Err(join_error(&error)))
    }
}

/// The write itself needs no identity (the descriptor is already open), so
/// it is a plain blocking call; a failure drops the file and its temp.
async fn append_chunk(mut file: OpenFile, bytes: Vec<u8>) -> Result<OpenFile, FilesystemError> {
    tokio::task::spawn_blocking(move || {
        file.sink
            .write_chunk(&bytes)
            .map_err(|error| FilesystemError::from_io("write", error))?;
        Ok(file)
    })
    .await
    .unwrap_or_else(|error| Err(join_error(&error)))
}

/// Keeps consuming and discarding the client's messages for at most
/// `max_bytes` or `timeout`, whichever comes first.
pub async fn drain_after_error<S, E>(messages: &mut S, max_bytes: usize, timeout: Duration)
where
    S: Stream<Item = Result<WriteMessageWithChunk, E>> + Unpin,
{
    let drained = tokio::time::timeout(timeout, async {
        let mut seen = 0usize;
        while let Some(Ok(item)) = messages.next().await {
            seen += item.chunk.len();
            if seen > max_bytes {
                break;
            }
        }
    })
    .await;
    if drained.is_err() {
        tracing::debug!(
            rpc = "Write",
            timeout_ms = timeout.as_millis(),
            "client still sending after the error; answering anyway"
        );
    }
}

#[cfg(test)]
mod tests {
    use tokio_stream::iter;

    use super::*;

    fn message(len: usize) -> Result<WriteMessageWithChunk, ()> {
        let chunk = WriteMessageWithChunk {
            message: WriteMessage::default(),
            chunk: vec![0; len],
        };
        if len == usize::MAX {
            Err(())
        } else {
            Ok(chunk)
        }
    }

    #[tokio::test]
    async fn drain_stops_at_the_byte_bound() {
        let mut messages = iter(vec![message(600_000), message(600_000), message(1)]);
        drain_after_error(&mut messages, 1_000_000, Duration::from_secs(1)).await;
        assert!(
            messages.next().await.is_some(),
            "the third message was not consumed"
        );
    }

    #[tokio::test]
    async fn drain_consumes_a_short_stream_to_its_end() {
        let mut messages = iter(vec![message(1), Err(())]);
        drain_after_error(&mut messages, 1_000_000, Duration::from_secs(1)).await;
        assert!(messages.next().await.is_none());
    }
}
