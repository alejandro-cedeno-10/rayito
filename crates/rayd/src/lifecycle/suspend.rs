//! The `/suspend` broadcast (design D7). Every server stream subscribes when
//! it opens; the hook broadcasts once per accepted `/suspend`, waits a
//! short grace for the subscribers to close, and answers. Each stream kind
//! closes in the form its schema allows: an in-stream terminal message
//! (`EndEvent`/`PtyExited` with status `suspending`) or a trailing
//! `UNAVAILABLE` status. Streams opened after a resume subscribe to the
//! next generation, so the following `/suspend` closes them too.

use std::pin::Pin;
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::task::{Context, Poll};
use std::time::Duration;

use tokio_stream::Stream;
use tokio_util::sync::{CancellationToken, WaitForCancellationFutureOwned};
use tonic::Status;

/// The status message every stream-gate refusal and every suspend close
/// carries; the SDK classifies it (`is_phase_gate`).
pub const SUSPENDING_MESSAGE: &str = "suspending";
const CLOSE_POLL_INTERVAL: Duration = Duration::from_millis(5);

#[derive(Debug)]
pub struct SuspendSignal {
    current: Mutex<CancellationToken>,
    generation: AtomicU64,
    open_streams: Arc<AtomicUsize>,
}

impl Default for SuspendSignal {
    fn default() -> Self {
        Self::new()
    }
}

impl SuspendSignal {
    #[must_use]
    pub fn new() -> Self {
        Self {
            current: Mutex::new(CancellationToken::new()),
            generation: AtomicU64::new(0),
            open_streams: Arc::new(AtomicUsize::new(0)),
        }
    }

    /// Closes every stream subscribed since the previous broadcast and
    /// starts the generation the next streams will subscribe to.
    pub fn broadcast(&self, suspend_generation: u64) {
        let previous = std::mem::replace(&mut *self.current(), CancellationToken::new());
        self.generation.store(suspend_generation, Ordering::SeqCst);
        previous.cancel();
    }

    #[must_use]
    pub fn subscribe(&self) -> SuspendWatch {
        let token = self.current().clone();
        SuspendWatch {
            cancelled: Box::pin(token.cancelled_owned()),
            guard: Some(OpenStreamGuard::new(self.open_streams.clone())),
        }
    }

    #[must_use]
    pub fn generation(&self) -> u64 {
        self.generation.load(Ordering::SeqCst)
    }

    #[must_use]
    pub fn open_streams(&self) -> usize {
        self.open_streams.load(Ordering::SeqCst)
    }

    /// Waits up to `grace` for every open stream to close; returns how many
    /// are still open (stalled clients tonic is not polling).
    pub async fn wait_streams_closed(&self, grace: Duration) -> usize {
        let deadline = tokio::time::Instant::now() + grace;
        loop {
            let open = self.open_streams();
            if open == 0 || tokio::time::Instant::now() >= deadline {
                return open;
            }
            tokio::time::sleep(CLOSE_POLL_INTERVAL).await;
        }
    }

    fn current(&self) -> MutexGuard<'_, CancellationToken> {
        self.current.lock().unwrap_or_else(PoisonError::into_inner)
    }
}

/// Counted while alive; released as soon as the stream has produced its
/// close (or dropped), which is what `wait_streams_closed` observes.
struct OpenStreamGuard {
    counter: Arc<AtomicUsize>,
}

impl OpenStreamGuard {
    fn new(counter: Arc<AtomicUsize>) -> Self {
        counter.fetch_add(1, Ordering::SeqCst);
        Self { counter }
    }
}

impl Drop for OpenStreamGuard {
    fn drop(&mut self) {
        self.counter.fetch_sub(1, Ordering::SeqCst);
    }
}

/// One stream's view of the broadcast: a future that completes when its
/// generation is suspended, plus the open-stream slot.
pub struct SuspendWatch {
    cancelled: Pin<Box<WaitForCancellationFutureOwned>>,
    guard: Option<OpenStreamGuard>,
}

impl SuspendWatch {
    /// Registers the waker, so an idle stream is polled again when the
    /// broadcast happens.
    pub fn poll_suspended(&mut self, cx: &mut Context<'_>) -> bool {
        self.cancelled.as_mut().poll(cx).is_ready()
    }

    pub async fn suspended(&mut self) {
        self.cancelled.as_mut().await;
    }

    /// Frees the open-stream slot before the stream object itself goes
    /// away (tonic keeps it until the close has been written).
    pub fn release(&mut self) {
        self.guard = None;
    }
}

/// How a stream ends on suspend: with one last in-stream message, or with
/// a trailing `UNAVAILABLE suspending` status.
pub enum SuspendClose<T> {
    Terminal(T),
    Status,
}

/// Called on the inner stream right before it is dropped by the suspend
/// close, for streams whose drop has side effects (an `Execute` origin
/// interrupts its cell).
pub trait SuspendAware {
    fn on_suspend(&mut self) {}
}

#[must_use]
pub fn suspending_status() -> Status {
    Status::unavailable(SUSPENDING_MESSAGE)
}

/// Wraps a domain stream into a gRPC response stream that closes on
/// suspend. `convert` maps the domain items; `close` picks the close form.
pub struct SuspendableStream<S, C, F> {
    inner: Option<S>,
    convert: C,
    close: F,
    watch: SuspendWatch,
    finished: bool,
}

impl<S, C, F, T> SuspendableStream<S, C, F>
where
    S: Stream + SuspendAware + Unpin,
    C: Fn(S::Item) -> Result<T, Status>,
    F: Fn() -> SuspendClose<T>,
{
    pub fn new(inner: S, watch: SuspendWatch, convert: C, close: F) -> Self {
        Self {
            inner: Some(inner),
            convert,
            close,
            watch,
            finished: false,
        }
    }

    fn close_now(&mut self) -> Poll<Option<Result<T, Status>>> {
        if let Some(inner) = self.inner.as_mut() {
            inner.on_suspend();
        }
        self.inner = None;
        self.watch.release();
        self.finished = true;
        match (self.close)() {
            SuspendClose::Terminal(item) => Poll::Ready(Some(Ok(item))),
            SuspendClose::Status => Poll::Ready(Some(Err(suspending_status()))),
        }
    }
}

impl<S, C, F, T> Stream for SuspendableStream<S, C, F>
where
    S: Stream + SuspendAware + Unpin,
    C: Fn(S::Item) -> Result<T, Status> + Unpin,
    F: Fn() -> SuspendClose<T> + Unpin,
{
    type Item = Result<T, Status>;

    fn poll_next(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        let this = self.get_mut();
        if this.finished {
            return Poll::Ready(None);
        }
        if this.watch.poll_suspended(cx) {
            return this.close_now();
        }
        let Some(inner) = this.inner.as_mut() else {
            this.finished = true;
            return Poll::Ready(None);
        };
        match Pin::new(inner).poll_next(cx) {
            Poll::Ready(Some(item)) => Poll::Ready(Some((this.convert)(item))),
            Poll::Ready(None) => {
                this.inner = None;
                this.watch.release();
                this.finished = true;
                Poll::Ready(None)
            }
            Poll::Pending => Poll::Pending,
        }
    }
}

#[cfg(test)]
mod tests {
    use std::sync::atomic::AtomicBool;

    use tokio::sync::mpsc;
    use tokio_stream::StreamExt;
    use tokio_stream::wrappers::ReceiverStream;
    use tonic::Code;

    use super::*;

    struct Inner {
        items: ReceiverStream<u32>,
        suspended: Arc<AtomicBool>,
        dropped: Arc<AtomicBool>,
    }

    impl Stream for Inner {
        type Item = u32;

        fn poll_next(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<u32>> {
            Pin::new(&mut self.get_mut().items).poll_next(cx)
        }
    }

    impl SuspendAware for Inner {
        fn on_suspend(&mut self) {
            assert!(!self.dropped.load(Ordering::SeqCst));
            self.suspended.store(true, Ordering::SeqCst);
        }
    }

    impl Drop for Inner {
        fn drop(&mut self) {
            self.dropped.store(true, Ordering::SeqCst);
        }
    }

    struct Fixture {
        sender: mpsc::Sender<u32>,
        suspended: Arc<AtomicBool>,
        dropped: Arc<AtomicBool>,
    }

    fn fixture() -> (Fixture, Inner) {
        let (sender, receiver) = mpsc::channel(8);
        let suspended = Arc::new(AtomicBool::new(false));
        let dropped = Arc::new(AtomicBool::new(false));
        (
            Fixture {
                sender,
                suspended: suspended.clone(),
                dropped: dropped.clone(),
            },
            Inner {
                items: ReceiverStream::new(receiver),
                suspended,
                dropped,
            },
        )
    }

    #[allow(clippy::unnecessary_wraps)]
    fn convert(item: u32) -> Result<u32, Status> {
        Ok(item * 10)
    }

    #[tokio::test(start_paused = true)]
    async fn terminal_close_yields_the_item_then_ends() {
        let signal = SuspendSignal::new();
        let (fixture, inner) = fixture();
        let mut stream = SuspendableStream::new(inner, signal.subscribe(), convert, || {
            SuspendClose::Terminal(999)
        });
        fixture.sender.send(1).await.unwrap();
        assert_eq!(stream.next().await.unwrap().unwrap(), 10);
        assert_eq!(signal.open_streams(), 1);
        let pending = tokio::spawn(async move {
            let item = stream.next().await;
            (item, stream)
        });
        tokio::time::sleep(Duration::from_millis(10)).await;
        signal.broadcast(1);
        let (item, mut stream) = pending.await.unwrap();
        assert_eq!(item.unwrap().unwrap(), 999);
        assert!(fixture.suspended.load(Ordering::SeqCst));
        assert!(fixture.dropped.load(Ordering::SeqCst));
        assert_eq!(
            signal.open_streams(),
            0,
            "released before the object is dropped"
        );
        assert!(stream.next().await.is_none());
        assert!(stream.next().await.is_none());
        assert_eq!(signal.generation(), 1);
    }

    #[tokio::test(start_paused = true)]
    async fn status_close_yields_unavailable_suspending() {
        let signal = SuspendSignal::new();
        let (fixture, inner) = fixture();
        let mut stream = SuspendableStream::new(inner, signal.subscribe(), convert, || {
            SuspendClose::<u32>::Status
        });
        signal.broadcast(1);
        let status = stream.next().await.unwrap().unwrap_err();
        assert_eq!(status.code(), Code::Unavailable);
        assert_eq!(status.message(), "suspending");
        assert!(stream.next().await.is_none());
        assert!(fixture.suspended.load(Ordering::SeqCst));
        drop(fixture);
    }

    #[tokio::test(start_paused = true)]
    async fn without_a_broadcast_the_stream_is_transparent_and_releases_on_end() {
        let signal = SuspendSignal::new();
        let (fixture, inner) = fixture();
        let mut stream = SuspendableStream::new(inner, signal.subscribe(), convert, || {
            SuspendClose::<u32>::Status
        });
        fixture.sender.send(2).await.unwrap();
        fixture.sender.send(3).await.unwrap();
        assert_eq!(stream.next().await.unwrap().unwrap(), 20);
        assert_eq!(stream.next().await.unwrap().unwrap(), 30);
        assert_eq!(signal.open_streams(), 1);
        drop(fixture.sender);
        assert!(stream.next().await.is_none());
        assert_eq!(signal.open_streams(), 0);
        assert!(!fixture.suspended.load(Ordering::SeqCst));
    }

    #[tokio::test(start_paused = true)]
    async fn dropping_an_open_stream_frees_its_slot_and_a_later_generation_closes_new_streams() {
        let signal = SuspendSignal::new();
        let (_first, inner) = fixture();
        let stream = SuspendableStream::new(inner, signal.subscribe(), convert, || {
            SuspendClose::<u32>::Status
        });
        assert_eq!(signal.open_streams(), 1);
        drop(stream);
        assert_eq!(signal.open_streams(), 0);
        signal.broadcast(1);
        let (_second, inner) = fixture();
        let mut later = SuspendableStream::new(inner, signal.subscribe(), convert, || {
            SuspendClose::<u32>::Status
        });
        let poll = tokio::time::timeout(Duration::from_millis(50), later.next()).await;
        assert!(
            poll.is_err(),
            "the first broadcast must not touch a stream of the next generation"
        );
        signal.broadcast(2);
        assert_eq!(
            later.next().await.unwrap().unwrap_err().code(),
            Code::Unavailable
        );
    }

    #[tokio::test(start_paused = true)]
    async fn wait_streams_closed_reports_what_is_still_open() {
        let signal = Arc::new(SuspendSignal::new());
        let (_fixture, inner) = fixture();
        let mut stream = SuspendableStream::new(inner, signal.subscribe(), convert, || {
            SuspendClose::<u32>::Status
        });
        let (_unpolled, unpolled_inner) = fixture();
        let _never_polled =
            SuspendableStream::new(unpolled_inner, signal.subscribe(), convert, || {
                SuspendClose::<u32>::Status
            });
        let reader = tokio::spawn(async move { stream.next().await.map(|item| item.is_err()) });
        tokio::time::sleep(Duration::from_millis(10)).await;
        signal.broadcast(1);
        let still_open = signal.wait_streams_closed(Duration::from_millis(500)).await;
        assert_eq!(still_open, 1);
        assert_eq!(reader.await.unwrap(), Some(true));
    }
}
