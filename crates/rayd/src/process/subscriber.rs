//! One subscriber of a process stream: a bounded channel that gives the pump
//! real backpressure, a stall rule that bounds how long a stuck client can
//! hold the pump, and the stream wrapper that turns a detach into the
//! `output_truncated` terminal event (design D7).

use std::collections::VecDeque;
use std::pin::Pin;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::task::{Context, Poll};
use std::time::Duration;

use rayd_core::process::{ProcessEnd, ProcessEvent, SubscriberSlot};
use tokio::sync::mpsc;
use tokio::sync::mpsc::error::SendTimeoutError;
use tokio_stream::Stream;

use crate::lifecycle::SuspendAware;

pub const SUBSCRIBER_CHANNEL_CAPACITY: usize = 64;
pub const DEFAULT_STALL_TIMEOUT: Duration = Duration::from_secs(30);

#[derive(Debug, Default)]
struct SubscriberState {
    last_seq: AtomicU64,
    detached: AtomicBool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Delivery {
    Delivered,
    /// The channel stayed full for the stall timeout: the subscriber is
    /// marked detached and must be dropped from the registry.
    Stalled,
    /// The receiver is gone (client cancelled or connection reset).
    Closed,
}

/// The sending half stored in the registry; cloning it is cheap and every
/// clone shares the subscriber's `last_seq`/`detached` state.
#[derive(Clone)]
pub struct SubscriberSink {
    sender: mpsc::Sender<ProcessEvent>,
    state: Arc<SubscriberState>,
    stall_timeout: Duration,
}

/// The receiving half, consumed by exactly one `SubscriberStream`.
pub struct SubscriberReceiver {
    receiver: mpsc::Receiver<ProcessEvent>,
    state: Arc<SubscriberState>,
    stall_timeout: Duration,
}

impl SubscriberSink {
    #[must_use]
    pub fn open(stall_timeout: Duration) -> (Self, SubscriberReceiver) {
        let (sender, receiver) = mpsc::channel(SUBSCRIBER_CHANNEL_CAPACITY);
        let state = Arc::new(SubscriberState::default());
        (
            Self {
                sender,
                state: state.clone(),
                stall_timeout,
            },
            SubscriberReceiver {
                receiver,
                state,
                stall_timeout,
            },
        )
    }

    /// Blocks while the channel is full (backpressure) up to the stall
    /// timeout, after which the subscriber is considered stuck.
    pub async fn deliver(&self, event: ProcessEvent) -> Delivery {
        let seq = match &event {
            ProcessEvent::Output(output) => Some(output.seq),
            ProcessEvent::Started { .. } | ProcessEvent::Ended(_) => None,
        };
        match self.sender.send_timeout(event, self.stall_timeout).await {
            Ok(()) => {
                if let Some(seq) = seq {
                    self.state.last_seq.store(seq, Ordering::Relaxed);
                }
                Delivery::Delivered
            }
            Err(SendTimeoutError::Timeout(_)) => {
                self.state.detached.store(true, Ordering::Relaxed);
                Delivery::Stalled
            }
            Err(SendTimeoutError::Closed(_)) => Delivery::Closed,
        }
    }

    #[must_use]
    pub fn last_seq(&self) -> u64 {
        self.state.last_seq.load(Ordering::Relaxed)
    }
}

/// Dropping the `SubscriberStream` drops the receiver, which tokio reports
/// through `is_closed` without any event having to be sent.
impl SubscriberSlot for SubscriberSink {
    fn is_open(&self) -> bool {
        !self.sender.is_closed()
    }
}

/// Yields the replay head first (never subject to the stall rule), then the
/// live channel, and ends after the first terminal event or, when the
/// subscriber was detached for stalling, after a synthetic
/// `output_truncated` end.
pub struct SubscriberStream {
    head: VecDeque<ProcessEvent>,
    live: Option<SubscriberReceiver>,
    finished: bool,
}

impl SubscriberStream {
    #[must_use]
    pub fn new(head: Vec<ProcessEvent>, live: Option<SubscriberReceiver>) -> Self {
        Self {
            head: head.into(),
            live,
            finished: false,
        }
    }

    fn finish_with(&mut self, event: ProcessEvent) -> Poll<Option<ProcessEvent>> {
        if matches!(event, ProcessEvent::Ended(_)) {
            self.finished = true;
        }
        Poll::Ready(Some(event))
    }

    fn poll_live(&mut self, cx: &mut Context<'_>) -> Poll<Option<ProcessEvent>> {
        let Some(live) = self.live.as_mut() else {
            self.finished = true;
            return Poll::Ready(None);
        };
        match live.receiver.poll_recv(cx) {
            Poll::Ready(Some(event)) => self.finish_with(event),
            Poll::Ready(None) => {
                self.finished = true;
                if live.state.detached.load(Ordering::Relaxed) {
                    let last_seq = live.state.last_seq.load(Ordering::Relaxed);
                    Poll::Ready(Some(ProcessEvent::Ended(ProcessEnd::output_truncated(
                        last_seq,
                        live.stall_timeout,
                    ))))
                } else {
                    Poll::Ready(None)
                }
            }
            Poll::Pending => Poll::Pending,
        }
    }
}

/// Dropping the stream is enough: the registry reclaims the slot through
/// `is_open` and the pump detaches it on the next delivery.
impl SuspendAware for SubscriberStream {}

impl Stream for SubscriberStream {
    type Item = ProcessEvent;

    fn poll_next(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<ProcessEvent>> {
        let this = self.get_mut();
        if this.finished {
            return Poll::Ready(None);
        }
        if let Some(event) = this.head.pop_front() {
            return this.finish_with(event);
        }
        this.poll_live(cx)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use bytes::Bytes;
    use rayd_core::process::{EndStatus, OutputEvent, OutputStream, Pid};
    use tokio_stream::StreamExt;

    fn output(seq: u64) -> ProcessEvent {
        ProcessEvent::Output(OutputEvent {
            seq,
            stream: OutputStream::Stdout,
            bytes: Bytes::from_static(b"x"),
        })
    }

    #[tokio::test]
    async fn head_is_delivered_before_live_events_and_end_finishes() {
        let (sink, receiver) = SubscriberSink::open(Duration::from_secs(1));
        let mut stream = SubscriberStream::new(
            vec![ProcessEvent::Started { pid: Pid(1) }, output(1)],
            Some(receiver),
        );
        assert_eq!(sink.deliver(output(2)).await, Delivery::Delivered);
        assert_eq!(
            sink.deliver(ProcessEvent::Ended(ProcessEnd::exited(0)))
                .await,
            Delivery::Delivered
        );
        assert_eq!(sink.last_seq(), 2);
        assert_eq!(
            stream.next().await,
            Some(ProcessEvent::Started { pid: Pid(1) })
        );
        assert_eq!(stream.next().await, Some(output(1)));
        assert_eq!(stream.next().await, Some(output(2)));
        assert!(matches!(
            stream.next().await,
            Some(ProcessEvent::Ended(end)) if end.status == EndStatus::Exited
        ));
        assert_eq!(stream.next().await, None);
    }

    #[tokio::test]
    async fn stalled_subscriber_ends_with_output_truncated_after_the_buffered_events() {
        let (sink, receiver) = SubscriberSink::open(Duration::from_millis(50));
        let mut stream = SubscriberStream::new(vec![], Some(receiver));
        for seq in 1..=SUBSCRIBER_CHANNEL_CAPACITY as u64 {
            assert_eq!(sink.deliver(output(seq)).await, Delivery::Delivered);
        }
        assert_eq!(sink.deliver(output(65)).await, Delivery::Stalled);
        drop(sink);
        let mut delivered = 0;
        loop {
            match stream.next().await {
                Some(ProcessEvent::Output(_)) => delivered += 1,
                Some(ProcessEvent::Ended(end)) => {
                    assert_eq!(end.status, EndStatus::OutputTruncated);
                    assert_eq!(
                        end.error.unwrap().message,
                        "subscriber stalled for 0 s at seq 64"
                    );
                    break;
                }
                other => panic!("unexpected {other:?}"),
            }
        }
        assert_eq!(delivered, SUBSCRIBER_CHANNEL_CAPACITY);
        assert_eq!(stream.next().await, None);
    }

    #[tokio::test]
    async fn dropped_receiver_reports_closed_and_a_silent_close_ends_the_stream() {
        let (sink, receiver) = SubscriberSink::open(Duration::from_secs(1));
        assert!(sink.is_open());
        drop(receiver);
        assert!(!sink.is_open());
        assert_eq!(sink.deliver(output(1)).await, Delivery::Closed);
        let (sink, receiver) = SubscriberSink::open(Duration::from_secs(1));
        let mut stream = SubscriberStream::new(vec![], Some(receiver));
        drop(sink);
        assert_eq!(stream.next().await, None);
    }

    #[tokio::test]
    async fn a_head_only_stream_replays_the_end_without_a_channel() {
        let mut stream = SubscriberStream::new(
            vec![
                ProcessEvent::Started { pid: Pid(7) },
                ProcessEvent::Ended(ProcessEnd::exited(3)),
            ],
            None,
        );
        assert_eq!(
            stream.next().await,
            Some(ProcessEvent::Started { pid: Pid(7) })
        );
        assert!(matches!(stream.next().await, Some(ProcessEvent::Ended(_))));
        assert_eq!(stream.next().await, None);
    }
}
