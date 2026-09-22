//! Emits a keepalive item whenever the wrapped stream stays silent for the
//! interval, so an idle process stream keeps crossing the proxy (idle policy
//! counts bytes, M0 Q15) and never looks dead to the client.

use std::pin::Pin;
use std::task::{Context, Poll};
use std::time::Duration;

use tokio::time::{Instant, Sleep};
use tokio_stream::Stream;

pub const DEFAULT_KEEPALIVE_INTERVAL: Duration = Duration::from_secs(30);

pub struct KeepAliveStream<S, F> {
    inner: Pin<Box<S>>,
    sleep: Pin<Box<Sleep>>,
    interval: Duration,
    keepalive: F,
    finished: bool,
}

impl<S, F, T> KeepAliveStream<S, F>
where
    S: Stream<Item = T>,
    F: Fn() -> T,
{
    pub fn new(inner: S, interval: Duration, keepalive: F) -> Self {
        Self {
            inner: Box::pin(inner),
            sleep: Box::pin(tokio::time::sleep(interval)),
            interval,
            keepalive,
            finished: false,
        }
    }

    fn reset_timer(&mut self) {
        let deadline = Instant::now() + self.interval;
        self.sleep.as_mut().reset(deadline);
    }
}

impl<S, F, T> Stream for KeepAliveStream<S, F>
where
    S: Stream<Item = T>,
    F: Fn() -> T + Unpin,
{
    type Item = T;

    fn poll_next(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<T>> {
        let this = self.get_mut();
        if this.finished {
            return Poll::Ready(None);
        }
        match this.inner.as_mut().poll_next(cx) {
            Poll::Ready(Some(item)) => {
                this.reset_timer();
                Poll::Ready(Some(item))
            }
            Poll::Ready(None) => {
                this.finished = true;
                Poll::Ready(None)
            }
            Poll::Pending => match this.sleep.as_mut().poll(cx) {
                Poll::Ready(()) => {
                    this.reset_timer();
                    Poll::Ready(Some((this.keepalive)()))
                }
                Poll::Pending => Poll::Pending,
            },
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::sync::mpsc;
    use tokio_stream::StreamExt;
    use tokio_stream::wrappers::ReceiverStream;

    #[tokio::test]
    async fn silence_produces_keepalives_and_items_reset_the_timer() {
        let (sender, receiver) = mpsc::channel::<&str>(4);
        let mut stream = KeepAliveStream::new(
            ReceiverStream::new(receiver),
            Duration::from_millis(50),
            || "keepalive",
        );
        sender.send("a").await.unwrap();
        assert_eq!(stream.next().await, Some("a"));
        assert_eq!(stream.next().await, Some("keepalive"));
        assert_eq!(stream.next().await, Some("keepalive"));
        sender.send("b").await.unwrap();
        assert_eq!(stream.next().await, Some("b"));
        drop(sender);
        assert_eq!(stream.next().await, None);
        assert_eq!(stream.next().await, None);
    }
}
