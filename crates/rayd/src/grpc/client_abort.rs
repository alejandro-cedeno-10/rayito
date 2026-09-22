//! Tower layer that records whether a request body ended because the client
//! aborted it. tonic reports a `CANCEL` on a client stream to the handler as
//! a clean end of stream (`codec/decode.rs`), which would let a cancelled
//! `Write` commit a truncated file; the raw body is observed here, before
//! tonic decodes it, and the verdict travels in a request extension the
//! `Write` handler consults once its stream ends.

use std::pin::Pin;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::task::{Context, Poll};

use http_body::{Body, Frame, SizeHint};
use tonic::body::Body as TonicBody;
use tower::{Layer, Service};

/// Cloneable handle shared by the observed body and the handler.
#[derive(Debug, Clone, Default)]
pub struct ClientAbort(Arc<AtomicBool>);

impl ClientAbort {
    #[must_use]
    pub fn aborted(&self) -> bool {
        self.0.load(Ordering::SeqCst)
    }

    fn record(&self) {
        self.0.store(true, Ordering::SeqCst);
    }
}

#[derive(Debug, Clone, Copy, Default)]
pub struct ClientAbortLayer;

impl<S> Layer<S> for ClientAbortLayer {
    type Service = ClientAbortService<S>;

    fn layer(&self, inner: S) -> Self::Service {
        ClientAbortService { inner }
    }
}

#[derive(Debug, Clone)]
pub struct ClientAbortService<S> {
    inner: S,
}

impl<S> Service<http::Request<TonicBody>> for ClientAbortService<S>
where
    S: Service<http::Request<TonicBody>>,
{
    type Response = S::Response;
    type Error = S::Error;
    type Future = S::Future;

    fn poll_ready(&mut self, cx: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
        self.inner.poll_ready(cx)
    }

    fn call(&mut self, request: http::Request<TonicBody>) -> Self::Future {
        let flag = ClientAbort::default();
        let (mut parts, body) = request.into_parts();
        parts.extensions.insert(flag.clone());
        let observed = TonicBody::new(ObservedBody { inner: body, flag });
        self.inner.call(http::Request::from_parts(parts, observed))
    }
}

/// Passes every frame through and flips the flag on the first error.
pub struct ObservedBody<B> {
    inner: B,
    flag: ClientAbort,
}

impl<B> Body for ObservedBody<B>
where
    B: Body + Unpin,
{
    type Data = B::Data;
    type Error = B::Error;

    fn poll_frame(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Self::Data>, Self::Error>>> {
        let this = self.get_mut();
        let polled = Pin::new(&mut this.inner).poll_frame(cx);
        if let Poll::Ready(Some(Err(_))) = &polled {
            this.flag.record();
        }
        polled
    }

    fn is_end_stream(&self) -> bool {
        self.inner.is_end_stream()
    }

    fn size_hint(&self) -> SizeHint {
        self.inner.size_hint()
    }
}

#[cfg(test)]
mod tests {
    use bytes::Bytes;
    use http_body_util::BodyExt;
    use tonic::Status;

    use super::*;

    struct Failing {
        frames: Vec<Result<Bytes, Status>>,
    }

    impl Body for Failing {
        type Data = Bytes;
        type Error = Status;

        fn poll_frame(
            self: Pin<&mut Self>,
            _cx: &mut Context<'_>,
        ) -> Poll<Option<Result<Frame<Bytes>, Status>>> {
            let this = self.get_mut();
            if this.frames.is_empty() {
                return Poll::Ready(None);
            }
            Poll::Ready(Some(this.frames.remove(0).map(Frame::data)))
        }
    }

    #[tokio::test]
    async fn a_body_error_sets_the_flag_and_a_clean_end_does_not() {
        let flag = ClientAbort::default();
        let mut clean = ObservedBody {
            inner: Failing {
                frames: vec![Ok(Bytes::from_static(b"x"))],
            },
            flag: flag.clone(),
        };
        while let Some(frame) = clean.frame().await {
            frame.unwrap();
        }
        assert!(!flag.aborted());
        let mut broken = ObservedBody {
            inner: Failing {
                frames: vec![
                    Ok(Bytes::from_static(b"x")),
                    Err(Status::cancelled("reset")),
                ],
            },
            flag: flag.clone(),
        };
        assert!(broken.frame().await.unwrap().is_ok());
        assert!(broken.frame().await.unwrap().is_err());
        assert!(flag.aborted());
    }
}
