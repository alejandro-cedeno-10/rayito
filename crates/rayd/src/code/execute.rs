//! One client's view of an execution (design D9): the replay head, then
//! the live channel of its subscriber, ending after `End` or, when the
//! recorder gave this subscriber up for falling a full queue behind, after
//! a synthetic `OutputTruncated` pair. Dropping the originating `Execute` stream before
//! `End` interrupts the cell (the M4 cancel rule); `Reattach` subscribers
//! and streams closed by `/suspend` never do.

use std::collections::VecDeque;
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::task::{Context, Poll};

use rayd_core::code::{
    ContextId, ContextRegistry, ExecuteOutput, ExecutionErrorInfo, SyntheticError,
};
use tokio_stream::Stream;

use super::executions::{ExecuteReceiver, InterruptHandle};
use super::supervisor::lock;
use crate::lifecycle::SuspendAware;

/// Decrements the context's `in_flight` counter however the execution ends.
pub struct InFlightGuard {
    registry: Arc<Mutex<ContextRegistry>>,
    context_id: ContextId,
}

impl InFlightGuard {
    #[must_use]
    pub fn new(registry: Arc<Mutex<ContextRegistry>>, context_id: ContextId) -> Self {
        Self {
            registry,
            context_id,
        }
    }
}

impl Drop for InFlightGuard {
    fn drop(&mut self) {
        lock(&self.registry).end_execution(&self.context_id);
    }
}

pub struct ExecutionSubscriberStream {
    head: VecDeque<ExecuteOutput>,
    live: Option<ExecuteReceiver>,
    interrupt: Option<InterruptHandle>,
    detached: Arc<AtomicBool>,
    done: bool,
}

impl ExecutionSubscriberStream {
    /// `interrupt` is `Some` only for the originating `Execute` stream.
    #[must_use]
    pub fn new(
        head: Vec<ExecuteOutput>,
        live: Option<ExecuteReceiver>,
        interrupt: Option<InterruptHandle>,
    ) -> Self {
        let detached = live.as_ref().map_or_else(
            || Arc::new(AtomicBool::new(false)),
            ExecuteReceiver::detached_flag,
        );
        let done = head.is_empty() && live.is_none();
        Self {
            head: head.into(),
            live,
            interrupt,
            detached,
            done,
        }
    }

    fn take(&mut self, output: ExecuteOutput) -> Poll<Option<ExecuteOutput>> {
        if output.is_end() {
            self.done = true;
        }
        Poll::Ready(Some(output))
    }

    fn poll_live(&mut self, cx: &mut Context<'_>) -> Poll<Option<ExecuteOutput>> {
        let Some(live) = self.live.as_mut() else {
            self.done = true;
            return Poll::Ready(None);
        };
        match live.poll_recv(cx) {
            Poll::Ready(Some(output)) => self.take(output),
            Poll::Ready(None) if live.stalled() => {
                self.head.extend(truncated_pair(live.capacity()));
                self.live = None;
                match self.head.pop_front() {
                    Some(output) => self.take(output),
                    None => Poll::Ready(None),
                }
            }
            Poll::Ready(None) => {
                self.done = true;
                Poll::Ready(None)
            }
            Poll::Pending => Poll::Pending,
        }
    }
}

/// The per-subscriber truncation pair carries `seq` 0: it is not a counted
/// event of the execution (the ring keeps recording), so a `Reattach` with
/// the last counted `seq` picks up where this subscriber lost track.
fn truncated_pair(capacity: usize) -> [ExecuteOutput; 2] {
    [
        ExecuteOutput::Error {
            seq: 0,
            error: ExecutionErrorInfo::synthetic(
                SyntheticError::OutputTruncated,
                format!("client fell {capacity} events behind"),
            ),
        },
        ExecuteOutput::End {
            seq: 0,
            execution_count: 0,
        },
    ]
}

impl Stream for ExecutionSubscriberStream {
    type Item = ExecuteOutput;

    fn poll_next(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<ExecuteOutput>> {
        let this = self.get_mut();
        if let Some(output) = this.head.pop_front() {
            return this.take(output);
        }
        if this.done {
            return Poll::Ready(None);
        }
        this.poll_live(cx)
    }
}

impl SuspendAware for ExecutionSubscriberStream {
    fn on_suspend(&mut self) {
        self.detached.store(true, Ordering::Relaxed);
    }
}

/// An origin dropped before its `End` interrupts the cell unless it was
/// detached by `/suspend`; later events are still recorded in the ring.
impl Drop for ExecutionSubscriberStream {
    fn drop(&mut self) {
        if self.done || self.detached.load(Ordering::Relaxed) {
            return;
        }
        if let Some(interrupt) = &self.interrupt
            && tokio::runtime::Handle::try_current().is_ok()
        {
            interrupt.interrupt();
        }
    }
}
