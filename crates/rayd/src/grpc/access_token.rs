//! Tower layer enforcing the access-token rule on every RPC but `Health`.
//! gRPC metadata travels as HTTP/2 headers, so the check runs on the raw
//! request before tonic decodes anything; a rejected call gets a gRPC
//! `UNAUTHENTICATED` status without touching the service. The rejected
//! request body is still read to its end (bounded) before answering: a
//! trailers-only response on a half-open HTTP/2 stream makes hyper reset the
//! stream, and the AWS proxy forwards that reset to the client as
//! `RST_STREAM(CANCEL)`, which grpc surfaces as `CANCELLED` instead of the
//! intended status (measured 2026-09-15, `AWS_API_NOTES.md` §16 Q29).

use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;
use std::task::{Context, Poll};
use std::time::Duration;

use base64::Engine;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use bytes::Buf;
use http::HeaderMap;
use http_body::Body;
use http_body_util::BodyExt;
use rayd_core::auth::{ACCESS_TOKEN_METADATA_KEY, AuthError, requires_access_token};
use rayd_core::session::SandboxSession;
use tonic::Status;
use tower::{Layer, Service};

/// A rejected unary or server-stream request carries one small message; a
/// client that keeps streaming past these bounds is answered anyway and the
/// resulting reset is its own problem.
pub const REJECTED_BODY_DRAIN_TIMEOUT: Duration = Duration::from_secs(2);
pub const REJECTED_BODY_DRAIN_MAX_BYTES: usize = 1 << 20;

#[derive(Clone)]
pub struct AccessTokenLayer {
    session: Arc<SandboxSession>,
}

impl AccessTokenLayer {
    #[must_use]
    pub fn new(session: Arc<SandboxSession>) -> Self {
        Self { session }
    }
}

impl<S> Layer<S> for AccessTokenLayer {
    type Service = AccessTokenService<S>;

    fn layer(&self, inner: S) -> Self::Service {
        AccessTokenService {
            inner,
            session: self.session.clone(),
        }
    }
}

#[derive(Clone)]
pub struct AccessTokenService<S> {
    inner: S,
    session: Arc<SandboxSession>,
}

impl<S, ReqBody, ResBody> Service<http::Request<ReqBody>> for AccessTokenService<S>
where
    S: Service<http::Request<ReqBody>, Response = http::Response<ResBody>> + Clone + Send + 'static,
    S::Future: Send + 'static,
    S::Error: Send + 'static,
    ReqBody: Body + Send + 'static,
    ReqBody::Data: Send,
    ReqBody::Error: Send,
    ResBody: Default + Send + 'static,
{
    type Response = S::Response;
    type Error = S::Error;
    type Future = Pin<Box<dyn Future<Output = Result<Self::Response, Self::Error>> + Send>>;

    fn poll_ready(&mut self, cx: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
        self.inner.poll_ready(cx)
    }

    fn call(&mut self, request: http::Request<ReqBody>) -> Self::Future {
        if let Err(status) = self.authorize(&request) {
            return Box::pin(async move {
                drain_rejected_body(request.into_body()).await;
                Ok(status.into_http())
            });
        }
        let ready_inner = self.inner.clone();
        let mut inner = std::mem::replace(&mut self.inner, ready_inner);
        Box::pin(async move { inner.call(request).await })
    }
}

impl<S> AccessTokenService<S> {
    /// The anonymous RPC never looks at the header: the SDK sends its token on
    /// every call, including the readiness probe, and a malformed one must not
    /// turn `Health` into `UNAUTHENTICATED`.
    fn authorize<B>(&self, request: &http::Request<B>) -> Result<(), Status> {
        let rpc = request.uri().path();
        if !requires_access_token(rpc) {
            return Ok(());
        }
        let presented = presented_secret(request.headers())?;
        self.session
            .authorize(rpc, presented.as_deref())
            .map_err(|error| rejected(rpc, &error))
    }
}

/// The metadata value is `base64url(secret)`; the domain compares the digest
/// of the decoded bytes. Padding is tolerated.
fn presented_secret(headers: &HeaderMap) -> Result<Option<Vec<u8>>, Status> {
    let Some(value) = headers.get(ACCESS_TOKEN_METADATA_KEY) else {
        return Ok(None);
    };
    let encoded = value
        .to_str()
        .map_err(|_| Status::unauthenticated("x-access-token is not printable ASCII"))?;
    let decoded = URL_SAFE_NO_PAD
        .decode(encoded.trim_end_matches('='))
        .map_err(|_| Status::unauthenticated("x-access-token is not base64url"))?;
    Ok(Some(decoded))
}

fn rejected(rpc: &str, error: &AuthError) -> Status {
    tracing::warn!(rpc, reason = %error, "rpc rejected");
    Status::unauthenticated(error.to_string())
}

async fn drain_rejected_body<B>(body: B)
where
    B: Body + Send,
{
    let drained =
        tokio::time::timeout(REJECTED_BODY_DRAIN_TIMEOUT, read_to_end_bounded(body)).await;
    if drained.is_err() {
        tracing::debug!(
            timeout_ms = REJECTED_BODY_DRAIN_TIMEOUT.as_millis(),
            "rejected request body still open; answering anyway"
        );
    }
}

async fn read_to_end_bounded<B>(body: B)
where
    B: Body + Send,
{
    let mut body = Box::pin(body);
    let mut seen = 0usize;
    while let Some(Ok(frame)) = body.frame().await {
        seen += frame.data_ref().map_or(0, Buf::remaining);
        if seen > REJECTED_BODY_DRAIN_MAX_BYTES {
            return;
        }
    }
}
