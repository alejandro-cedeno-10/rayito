//! The connection cap of `rayd`'s own listeners (`rayd_core::listeners`):
//! a listener only calls `accept` while it holds a free slot, and the
//! accepted connection carries that slot until it closes, so a connection
//! over the cap waits in the kernel's backlog instead of taking a
//! descriptor. A failed `accept` that is not about one connection
//! (`EMFILE`, `ENFILE`, `ENOBUFS`, `ENOMEM`) waits `ACCEPT_BACKOFF` before
//! the next attempt, so a server loop that retries on error (tonic's) can
//! never spin on it.
//!
//! The same semaphore-and-permit pattern bounds the local egress proxy
//! (`network::proxy`); this is its listener-side form, shared by the gRPC
//! and the hooks listeners.

use std::convert::Infallible;
use std::future::Future;
use std::io;
use std::pin::Pin;
use std::sync::Arc;
use std::task::{Context, Poll};
use std::time::Duration;

use rayd_core::listeners::{ACCEPT_BACKOFF, AcceptFailure};
use tokio::io::{AsyncRead, AsyncWrite, ReadBuf};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{OwnedSemaphorePermit, Semaphore};
use tokio_stream::Stream;
use tonic::transport::server::Connected;

use super::io_error_name;

/// Where connections come from: a TCP listener in production, an
/// in-memory source in the tests.
pub trait Accept: Send + 'static {
    type Io: Send + 'static;

    /// The next connection, or why `accept` failed.
    fn accept_connection(&mut self) -> impl Future<Output = io::Result<Self::Io>> + Send;
}

impl Accept for TcpListener {
    type Io = TcpStream;

    /// gRPC frames are small and latency-bound, so Nagle is off on every
    /// accepted socket, as `TcpIncoming::with_nodelay` did.
    async fn accept_connection(&mut self) -> io::Result<TcpStream> {
        let (stream, _) = self.accept().await?;
        let _ = stream.set_nodelay(true);
        Ok(stream)
    }
}

/// A listener that serves at most `max_connections` connections at once.
pub struct CappedListener<A> {
    acceptor: A,
    slots: Arc<Semaphore>,
    backoff: Duration,
    exhausted: bool,
}

impl<A: Accept> CappedListener<A> {
    #[must_use]
    pub fn new(acceptor: A, max_connections: usize) -> Self {
        Self {
            acceptor,
            slots: Arc::new(Semaphore::new(max_connections)),
            backoff: ACCEPT_BACKOFF,
            exhausted: false,
        }
    }

    /// Waits for a free slot, then for a connection. Never fails: a
    /// vanished connection is skipped and a resource failure is retried
    /// after the backoff, logged once per streak.
    pub async fn accept(&mut self) -> CappedStream<A::Io> {
        let permit = slot(self.slots.clone()).await;
        loop {
            match self.acceptor.accept_connection().await {
                Ok(io) => {
                    self.exhausted = false;
                    return CappedStream { io, _slot: permit };
                }
                Err(error) => self.on_accept_error(&error).await,
            }
        }
    }

    /// The listener as the connection stream tonic serves from.
    #[must_use]
    pub fn into_incoming(self) -> CappedIncoming<A> {
        CappedIncoming {
            state: IncomingState::Idle(self),
        }
    }

    async fn on_accept_error(&mut self, error: &io::Error) {
        if AcceptFailure::classify(error.kind()) == AcceptFailure::Connection {
            return;
        }
        if !self.exhausted {
            self.exhausted = true;
            tracing::warn!(
                reason = %io_error_name(error),
                backoff_ms = u64::try_from(self.backoff.as_millis()).unwrap_or(u64::MAX),
                "accept_exhausted"
            );
        }
        tokio::time::sleep(self.backoff).await;
    }
}

/// A free slot. The semaphore is never closed, so `acquire_owned` only
/// ever waits.
async fn slot(slots: Arc<Semaphore>) -> OwnedSemaphorePermit {
    match slots.acquire_owned().await {
        Ok(permit) => permit,
        Err(_) => std::future::pending().await,
    }
}

/// An accepted connection and the slot it holds until it is dropped.
pub struct CappedStream<Io> {
    io: Io,
    _slot: OwnedSemaphorePermit,
}

impl<Io> CappedStream<Io> {
    #[must_use]
    pub fn io(&self) -> &Io {
        &self.io
    }
}

impl<Io: AsyncRead + Unpin> AsyncRead for CappedStream<Io> {
    fn poll_read(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        Pin::new(&mut self.io).poll_read(cx, buf)
    }
}

impl<Io: AsyncWrite + Unpin> AsyncWrite for CappedStream<Io> {
    fn poll_write(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &[u8],
    ) -> Poll<io::Result<usize>> {
        Pin::new(&mut self.io).poll_write(cx, buf)
    }

    fn poll_flush(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.io).poll_flush(cx)
    }

    fn poll_shutdown(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.io).poll_shutdown(cx)
    }

    fn poll_write_vectored(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        bufs: &[io::IoSlice<'_>],
    ) -> Poll<io::Result<usize>> {
        Pin::new(&mut self.io).poll_write_vectored(cx, bufs)
    }

    fn is_write_vectored(&self) -> bool {
        self.io.is_write_vectored()
    }
}

impl<Io: Connected> Connected for CappedStream<Io> {
    type ConnectInfo = Io::ConnectInfo;

    fn connect_info(&self) -> Self::ConnectInfo {
        self.io.connect_info()
    }
}

type PendingAccept<A> =
    Pin<Box<dyn Future<Output = (CappedListener<A>, CappedStream<<A as Accept>::Io>)> + Send>>;

enum IncomingState<A: Accept> {
    Idle(CappedListener<A>),
    Accepting(PendingAccept<A>),
    Gone,
}

/// `CappedListener` as a `Stream` of connections. It never yields an
/// error (failures are handled inside `accept`) and never ends.
pub struct CappedIncoming<A: Accept> {
    state: IncomingState<A>,
}

/// Nothing inside is ever pinned in place: the listener moves in and out
/// of its boxed accept future, which is pinned on the heap.
impl<A: Accept> Unpin for CappedIncoming<A> {}

impl<A: Accept> Stream for CappedIncoming<A> {
    type Item = Result<CappedStream<A::Io>, Infallible>;

    fn poll_next(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        let mut pending = match std::mem::replace(&mut self.state, IncomingState::Gone) {
            IncomingState::Idle(mut listener) => Box::pin(async move {
                let stream = listener.accept().await;
                (listener, stream)
            }) as PendingAccept<A>,
            IncomingState::Accepting(pending) => pending,
            IncomingState::Gone => return Poll::Ready(None),
        };
        match pending.as_mut().poll(cx) {
            Poll::Ready((listener, stream)) => {
                self.state = IncomingState::Idle(listener);
                Poll::Ready(Some(Ok(stream)))
            }
            Poll::Pending => {
                self.state = IncomingState::Accepting(pending);
                Poll::Pending
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use std::collections::VecDeque;
    use std::sync::atomic::{AtomicUsize, Ordering};

    use tokio::io::DuplexStream;
    use tokio_stream::StreamExt;

    use super::*;

    /// Long enough for an accept that should not happen to show up.
    const NOT_ACCEPTED_WITHIN: Duration = Duration::from_millis(300);
    const ACCEPTED_WITHIN: Duration = Duration::from_secs(5);
    /// `EMFILE` on Linux and macOS (`errno(3)`).
    const EMFILE: i32 = 24;

    async fn loopback() -> (TcpListener, std::net::SocketAddr) {
        let listener = TcpListener::bind(("127.0.0.1", 0)).await.unwrap();
        let address = listener.local_addr().unwrap();
        (listener, address)
    }

    #[tokio::test]
    async fn a_connection_over_the_cap_waits_until_a_slot_frees() {
        let (listener, address) = loopback().await;
        let mut capped = CappedListener::new(listener, 2);
        let mut clients = Vec::new();
        for _ in 0..3 {
            clients.push(TcpStream::connect(address).await.unwrap());
        }
        let first = capped.accept().await;
        let _second = capped.accept().await;
        assert!(
            tokio::time::timeout(NOT_ACCEPTED_WITHIN, capped.accept())
                .await
                .is_err(),
            "a third connection was accepted with only two slots"
        );
        drop(first);
        tokio::time::timeout(ACCEPTED_WITHIN, capped.accept())
            .await
            .expect("the freed slot accepts the waiting connection");
    }

    #[tokio::test]
    async fn the_incoming_stream_respects_the_same_cap() {
        let (listener, address) = loopback().await;
        let mut incoming = CappedListener::new(listener, 1).into_incoming();
        let _clients = [
            TcpStream::connect(address).await.unwrap(),
            TcpStream::connect(address).await.unwrap(),
        ];
        let first = incoming.next().await.unwrap().unwrap();
        assert!(first.io().nodelay().unwrap());
        assert!(
            tokio::time::timeout(NOT_ACCEPTED_WITHIN, incoming.next())
                .await
                .is_err()
        );
        drop(first);
        let second = tokio::time::timeout(ACCEPTED_WITHIN, incoming.next())
            .await
            .unwrap();
        assert!(second.is_some());
    }

    /// Fails with the scripted errors, then hands out in-memory streams,
    /// counting every `accept` call.
    struct Scripted {
        failures: VecDeque<io::ErrorKind>,
        raw: VecDeque<i32>,
        calls: Arc<AtomicUsize>,
    }

    impl Accept for Scripted {
        type Io = DuplexStream;

        fn accept_connection(&mut self) -> impl Future<Output = io::Result<DuplexStream>> + Send {
            self.calls.fetch_add(1, Ordering::SeqCst);
            let next = if let Some(code) = self.raw.pop_front() {
                Err(io::Error::from_raw_os_error(code))
            } else if let Some(kind) = self.failures.pop_front() {
                Err(kind.into())
            } else {
                Ok(tokio::io::duplex(64).0)
            };
            std::future::ready(next)
        }
    }

    fn scripted(raw: &[i32], failures: &[io::ErrorKind]) -> (Scripted, Arc<AtomicUsize>) {
        let calls = Arc::new(AtomicUsize::new(0));
        let acceptor = Scripted {
            failures: failures.iter().copied().collect(),
            raw: raw.iter().copied().collect(),
            calls: calls.clone(),
        };
        (acceptor, calls)
    }

    #[tokio::test(start_paused = true)]
    async fn running_out_of_descriptors_backs_off_instead_of_spinning() {
        const STREAK: u32 = 5;
        let (acceptor, calls) = scripted(&[EMFILE; STREAK as usize], &[]);
        let mut capped = CappedListener::new(acceptor, 4);
        let started = tokio::time::Instant::now();
        let _stream = capped.accept().await;
        assert_eq!(calls.load(Ordering::SeqCst), STREAK as usize + 1);
        assert!(started.elapsed() >= ACCEPT_BACKOFF * STREAK);
    }

    #[tokio::test(start_paused = true)]
    async fn a_vanished_connection_is_skipped_without_waiting() {
        let (acceptor, calls) = scripted(
            &[],
            &[
                io::ErrorKind::ConnectionAborted,
                io::ErrorKind::ConnectionReset,
            ],
        );
        let mut capped = CappedListener::new(acceptor, 4);
        let started = tokio::time::Instant::now();
        let _stream = capped.accept().await;
        assert_eq!(calls.load(Ordering::SeqCst), 3);
        assert_eq!(started.elapsed(), Duration::ZERO);
    }
}
