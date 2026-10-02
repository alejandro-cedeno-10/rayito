//! `LifecycleEventSink` over the process's own stdout (M15,
//! `m15-events-webhooks`, ADR-020). `emit_line` never blocks the hook that
//! calls it: it only pushes onto a bounded channel
//! (`rayd_core::lifecycle_events::EVENT_QUEUE_CAPACITY`) that a single
//! background task drains, writing each line with one `writeln!` under the
//! stdout lock so two events can never interleave mid-line. `flush` puts a
//! barrier on the same channel: the drain task acknowledges it only after
//! writing every line queued ahead of it and flushing stdout, so a caller
//! about to freeze the VM (`/suspend`) or exit (`/terminate`) knows the
//! line is out.

use std::io::Write;

use rayd_core::lifecycle_events::{EVENT_QUEUE_CAPACITY, LifecycleEventSink, SinkFlush};
use tokio::sync::{mpsc, oneshot};

/// One slot in the queue. The channel is FIFO with a single consumer, so a
/// `Flush` is acknowledged only after every `Line` queued before it.
enum QueueItem {
    Line(String),
    Flush(oneshot::Sender<()>),
}

/// Spawns the drain task on the current Tokio runtime and hands back the
/// sink half. Dropping the returned `StdoutEventSink` closes the channel,
/// which lets the drain task exit once the queue empties.
#[must_use]
pub fn spawn() -> StdoutEventSink {
    let (tx, rx) = mpsc::channel::<QueueItem>(EVENT_QUEUE_CAPACITY);
    tokio::spawn(drain(rx, std::io::stdout));
    StdoutEventSink { tx }
}

/// `open` hands out the writer for each item (stdout in production, a
/// buffer in tests). Writing inline on this task is deliberate: stdout is a
/// pipe the image's `CloudWatch` Logs agent drains, so a write never blocks
/// noticeably, and a `spawn_blocking` per line would cost more than the
/// write itself.
async fn drain<W: Write, F: Fn() -> W>(mut rx: mpsc::Receiver<QueueItem>, open: F) {
    while let Some(item) = rx.recv().await {
        let mut out = open();
        match item {
            QueueItem::Line(line) => {
                let _ = writeln!(out, "{line}");
            }
            QueueItem::Flush(ack) => {
                let _ = out.flush();
                let _ = ack.send(());
            }
        }
    }
}

pub struct StdoutEventSink {
    tx: mpsc::Sender<QueueItem>,
}

impl LifecycleEventSink for StdoutEventSink {
    fn emit_line(&self, line: &str) -> bool {
        self.tx.try_send(QueueItem::Line(line.to_owned())).is_ok()
    }

    fn flush(&self) -> SinkFlush<'_> {
        Box::pin(async move {
            let (ack, acked) = oneshot::channel();
            // Waits for room behind a full queue rather than dropping the
            // barrier: the caller's own timeout is what bounds this.
            if self.tx.send(QueueItem::Flush(ack)).await.is_ok() {
                let _ = acked.await;
            }
        })
    }
}

#[cfg(test)]
mod tests {
    use std::sync::{Arc, Mutex};
    use std::time::Duration;

    use super::*;

    /// A writer appending into a shared buffer, standing in for stdout.
    #[derive(Clone, Default)]
    struct Shared(Arc<Mutex<Vec<u8>>>);

    impl Write for Shared {
        fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
            self.0.lock().unwrap().extend_from_slice(buf);
            Ok(buf.len())
        }

        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }

    fn sink_over(out: Shared) -> StdoutEventSink {
        let (tx, rx) = mpsc::channel::<QueueItem>(EVENT_QUEUE_CAPACITY);
        tokio::spawn(drain(rx, move || out.clone()));
        StdoutEventSink { tx }
    }

    #[tokio::test]
    async fn a_line_within_capacity_is_accepted() {
        let sink = spawn();
        assert!(sink.emit_line("rayito.event.v1 a b"));
    }

    #[tokio::test]
    async fn a_full_queue_drops_rather_than_blocks() {
        // A channel of capacity 1 whose single consumer never runs yet:
        // the second `try_send` must fail immediately, not await space.
        let (tx, _rx) = mpsc::channel::<QueueItem>(1);
        let sink = StdoutEventSink { tx };
        assert!(sink.emit_line("first"));
        assert!(!sink.emit_line("second"));
    }

    #[tokio::test]
    async fn flush_resolves_only_after_every_earlier_line_is_written() {
        let out = Shared::default();
        let sink = sink_over(out.clone());
        for n in 0..8 {
            assert!(sink.emit_line(&format!("line-{n}")));
        }
        sink.flush().await;
        let written = String::from_utf8(out.0.lock().unwrap().clone()).unwrap();
        assert_eq!(written.lines().count(), 8);
        assert!(written.ends_with("line-7\n"));
    }

    #[tokio::test(start_paused = true)]
    async fn flush_on_a_stuck_drain_never_resolves_so_the_caller_bounds_it() {
        // Nothing ever drains this channel.
        let (tx, _rx) = mpsc::channel::<QueueItem>(1);
        let sink = StdoutEventSink { tx };
        assert!(sink.emit_line("stuck"));
        let bound = Duration::from_millis(50);
        assert!(tokio::time::timeout(bound, sink.flush()).await.is_err());
    }
}
