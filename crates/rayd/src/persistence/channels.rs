//! The two bounded channels that couple a blocking archive thread to the
//! async store side (design D7). Upload: `PartWriter` (an `ArchiveSink`
//! filling 8 MiB parts) → `ChannelParts` (a `PartSource`). Download:
//! `ChannelSink` (a `ChunkSink`) → `ChunkReader` (a `Read`). An explicit
//! end frame separates a complete transfer from a producer that died:
//! a channel closed without it is an interruption, never a clean end.

use std::collections::VecDeque;
use std::io::{self, Read, Write};

use bytes::{Bytes, BytesMut};
use rayd_core::persistence::{
    ArchiveSink, ChunkSink, PART_BYTES, PartSource, SinkClosed, StoreError, StoreErrorKind,
};
use tokio::sync::mpsc;

/// Parts in flight between the archive thread and the uploader: with the
/// uploader's own peek-ahead this bounds the pipeline at three parts.
pub const PART_CHANNEL_CAPACITY: usize = 1;
/// 1 MiB download chunks in flight before the unpack thread.
pub const CHUNK_CHANNEL_CAPACITY: usize = 8;

pub enum Frame {
    Data(Bytes),
    End,
}

/// Fills `part_bytes` parts from the archive thread's writes.
pub struct PartWriter {
    sender: mpsc::Sender<Frame>,
    buffer: BytesMut,
    part_bytes: usize,
}

pub struct ChannelParts {
    receiver: mpsc::Receiver<Frame>,
    ended: bool,
}

/// One upload channel; `part_bytes` is `PART_BYTES` in production and
/// small in tests.
#[must_use]
pub fn part_channel(part_bytes: usize) -> (Box<dyn ArchiveSink>, ChannelParts) {
    let (sender, receiver) = mpsc::channel(PART_CHANNEL_CAPACITY);
    (
        Box::new(PartWriter {
            sender,
            buffer: BytesMut::with_capacity(part_bytes.min(PART_BYTES)),
            part_bytes,
        }),
        ChannelParts {
            receiver,
            ended: false,
        },
    )
}

impl PartWriter {
    fn send(&mut self, frame: Frame) -> io::Result<()> {
        self.sender
            .blocking_send(frame)
            .map_err(|_| io::Error::from(io::ErrorKind::BrokenPipe))
    }

    fn flush_full_parts(&mut self) -> io::Result<()> {
        while self.buffer.len() >= self.part_bytes {
            let part = self.buffer.split_to(self.part_bytes).freeze();
            self.send(Frame::Data(part))?;
        }
        Ok(())
    }
}

impl Write for PartWriter {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        self.buffer.extend_from_slice(buf);
        self.flush_full_parts()?;
        Ok(buf.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

impl ArchiveSink for PartWriter {
    fn finish(mut self: Box<Self>) -> io::Result<()> {
        self.flush_full_parts()?;
        if !self.buffer.is_empty() {
            let last = self.buffer.split().freeze();
            self.send(Frame::Data(last))?;
        }
        self.send(Frame::End)
    }
}

impl PartSource for ChannelParts {
    async fn next_part(&mut self) -> Result<Option<Bytes>, StoreError> {
        if self.ended {
            return Ok(None);
        }
        match self.receiver.recv().await {
            Some(Frame::Data(part)) => Ok(Some(part)),
            Some(Frame::End) => {
                self.ended = true;
                Ok(None)
            }
            None => Err(StoreError::new(StoreErrorKind::Interrupted)),
        }
    }
}

pub struct ChannelSink {
    sender: mpsc::Sender<Frame>,
}

pub struct ChunkReader {
    receiver: mpsc::Receiver<Frame>,
    pending: VecDeque<Bytes>,
    ended: bool,
}

#[must_use]
pub fn chunk_channel() -> (ChannelSink, Box<dyn Read + Send>) {
    let (sender, receiver) = mpsc::channel(CHUNK_CHANNEL_CAPACITY);
    (
        ChannelSink { sender },
        Box::new(ChunkReader {
            receiver,
            pending: VecDeque::new(),
            ended: false,
        }),
    )
}

impl ChunkSink for ChannelSink {
    async fn push(&mut self, chunk: Bytes) -> Result<(), SinkClosed> {
        self.sender
            .send(Frame::Data(chunk))
            .await
            .map_err(|_| SinkClosed)
    }

    async fn finish(self) {
        let _ = self.sender.send(Frame::End).await;
    }
}

impl Read for ChunkReader {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        while self.pending.is_empty() {
            if self.ended {
                return Ok(0);
            }
            match self.receiver.blocking_recv() {
                Some(Frame::Data(chunk)) if chunk.is_empty() => {}
                Some(Frame::Data(chunk)) => self.pending.push_back(chunk),
                Some(Frame::End) => self.ended = true,
                None => return Err(io::Error::from(io::ErrorKind::BrokenPipe)),
            }
        }
        let Some(front) = self.pending.front_mut() else {
            return Ok(0);
        };
        let n = buf.len().min(front.len());
        buf[..n].copy_from_slice(&front[..n]);
        let _ = front.split_to(n);
        if front.is_empty() {
            self.pending.pop_front();
        }
        Ok(n)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn parts_are_cut_at_the_part_size_and_the_end_frame_closes_cleanly() {
        let (mut sink, mut parts) = part_channel(4);
        let producer = tokio::task::spawn_blocking(move || {
            sink.write_all(b"abcdefghij").unwrap();
            sink.finish().unwrap();
        });
        let mut got = Vec::new();
        while let Some(part) = parts.next_part().await.unwrap() {
            got.push(part.to_vec());
        }
        assert_eq!(
            got,
            vec![b"abcd".to_vec(), b"efgh".to_vec(), b"ij".to_vec()]
        );
        assert_eq!(parts.next_part().await.unwrap(), None);
        producer.await.unwrap();
    }

    #[tokio::test]
    async fn a_producer_that_dies_is_an_interruption() {
        let (mut sink, mut parts) = part_channel(4);
        let producer = tokio::task::spawn_blocking(move || {
            sink.write_all(b"abcd").unwrap();
            drop(sink);
        });
        assert_eq!(parts.next_part().await.unwrap().unwrap().to_vec(), b"abcd");
        assert_eq!(
            parts.next_part().await.unwrap_err().kind,
            StoreErrorKind::Interrupted
        );
        producer.await.unwrap();
    }

    #[tokio::test]
    async fn a_dropped_receiver_breaks_the_writer_pipe() {
        let (mut sink, parts) = part_channel(4);
        drop(parts);
        let producer = tokio::task::spawn_blocking(move || {
            sink.write_all(b"abcdefgh").map_err(|error| error.kind())
        });
        assert_eq!(producer.await.unwrap(), Err(io::ErrorKind::BrokenPipe));
    }

    #[tokio::test]
    async fn chunks_reach_the_reader_in_order_and_end_cleanly() {
        let (mut sink, mut reader) = chunk_channel();
        let consumer = tokio::task::spawn_blocking(move || {
            let mut out = Vec::new();
            reader.read_to_end(&mut out).map(|_| out)
        });
        sink.push(Bytes::from_static(b"hello ")).await.unwrap();
        sink.push(Bytes::from_static(b"")).await.unwrap();
        sink.push(Bytes::from_static(b"world")).await.unwrap();
        sink.finish().await;
        assert_eq!(consumer.await.unwrap().unwrap(), b"hello world");
    }

    #[tokio::test]
    async fn a_sink_dropped_without_end_is_a_broken_pipe_for_the_reader() {
        let (mut sink, mut reader) = chunk_channel();
        sink.push(Bytes::from_static(b"x")).await.unwrap();
        drop(sink);
        let consumer = tokio::task::spawn_blocking(move || {
            let mut out = Vec::new();
            reader.read_to_end(&mut out).map_err(|error| error.kind())
        });
        assert_eq!(consumer.await.unwrap(), Err(io::ErrorKind::BrokenPipe));
    }

    #[tokio::test]
    async fn a_dropped_reader_closes_the_sink() {
        let (mut sink, reader) = chunk_channel();
        drop(reader);
        assert_eq!(sink.push(Bytes::from_static(b"x")).await, Err(SinkClosed));
    }
}
