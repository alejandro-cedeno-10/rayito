//! `LifecycleEventSink` over the process's own stdout (M15,
//! `m15-events-webhooks`, ADR-020). `emit_line` never blocks the hook that
//! calls it: it only pushes onto a bounded channel
//! (`rayd_core::lifecycle_events::EVENT_QUEUE_CAPACITY`) that a single
//! background task drains, writing each line with one `writeln!` under the
//! stdout lock so two events can never interleave mid-line.

use rayd_core::lifecycle_events::{EVENT_QUEUE_CAPACITY, LifecycleEventSink};
use tokio::sync::mpsc;

/// Spawns the drain task and hands back the sink half. Dropping the
/// returned `StdoutEventSink` closes the channel, which lets the drain task
/// exit once the queue empties — `rayd` never does this in practice (the
/// sink lives for the process), but tests do.
#[must_use]
pub fn spawn() -> StdoutEventSink {
    let (tx, mut rx) = mpsc::channel::<String>(EVENT_QUEUE_CAPACITY);
    tokio::spawn(async move {
        use std::io::Write;
        while let Some(line) = rx.recv().await {
            // A synchronous `writeln!` under the stdout lock: one write
            // syscall for the whole line, so two events can never
            // interleave mid-line even with another thread also writing to
            // stdout (tracing's own output). This never blocks noticeably
            // (stdout is a pipe the image's CloudWatch Logs agent drains),
            // so doing it inline on this task is deliberate — spawning a
            // `spawn_blocking` per line would cost more than the write
            // itself.
            let mut stdout = std::io::stdout().lock();
            let _ = writeln!(stdout, "{line}");
        }
    });
    StdoutEventSink { tx }
}

pub struct StdoutEventSink {
    tx: mpsc::Sender<String>,
}

impl LifecycleEventSink for StdoutEventSink {
    fn emit_line(&self, line: &str) -> bool {
        self.tx.try_send(line.to_owned()).is_ok()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn a_line_within_capacity_is_accepted() {
        let sink = spawn();
        assert!(sink.emit_line("rayito.event.v1 a b"));
    }

    #[tokio::test]
    async fn a_full_queue_drops_rather_than_blocks() {
        // A channel of capacity 1 whose single consumer never runs yet:
        // the second `try_send` must fail immediately, not await space.
        let (tx, _rx) = mpsc::channel::<String>(1);
        let sink = StdoutEventSink { tx };
        assert!(sink.emit_line("first"));
        assert!(!sink.emit_line("second"));
    }
}
