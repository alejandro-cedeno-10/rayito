//! `WatchTransfer`'s domain stream (design D8): the current snapshot
//! first, then a full snapshot on every phase change and on progress
//! (sampled at most once per interval), ending right after a terminal one.
//! A task follows the transfer's `watch` channel; dropping the stream (the
//! client left, `/suspend` closed it) stops the task on its next send.

use std::time::Duration;

use rayd_core::transfer::{TransferPhase, TransferSnapshot};
use tokio::sync::{mpsc, watch};
use tokio::time::Instant;
use tokio_stream::wrappers::ReceiverStream;

pub type TransferWatch = ReceiverStream<TransferSnapshot>;

const WATCH_QUEUE: usize = 4;

#[must_use]
pub fn watch_snapshots(
    receiver: watch::Receiver<TransferSnapshot>,
    progress_interval: Duration,
) -> TransferWatch {
    let (sender, stream) = mpsc::channel(WATCH_QUEUE);
    tokio::spawn(follow(receiver, sender, progress_interval));
    ReceiverStream::new(stream)
}

async fn follow(
    mut receiver: watch::Receiver<TransferSnapshot>,
    sender: mpsc::Sender<TransferSnapshot>,
    progress_interval: Duration,
) {
    let mut last: Option<(TransferPhase, u64)> = None;
    let mut next_progress = Instant::now();
    loop {
        let snapshot = receiver.borrow_and_update().clone();
        let phase_changed = last.is_none_or(|(phase, _)| phase != snapshot.phase);
        let progressed = last.is_some_and(|(_, bytes)| bytes != snapshot.bytes_done);
        if phase_changed || (progressed && Instant::now() >= next_progress) {
            last = Some((snapshot.phase, snapshot.bytes_done));
            next_progress = Instant::now() + progress_interval;
            let terminal = snapshot.phase.is_terminal();
            if sender.send(snapshot).await.is_err() || terminal {
                return;
            }
        }
        let changed = if progressed && Instant::now() < next_progress {
            tokio::select! {
                changed = receiver.changed() => changed,
                () = tokio::time::sleep_until(next_progress) => Ok(()),
            }
        } else {
            receiver.changed().await
        };
        if changed.is_err() && !receiver.borrow().phase.is_terminal() {
            return;
        }
    }
}
