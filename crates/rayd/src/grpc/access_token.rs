//! Tower layer enforcing the access-token rule on every RPC but `Health`.
//! gRPC metadata travels as HTTP/2 headers, so the check runs on the raw
//! request before tonic decodes anything; a rejected call gets a gRPC
//! `UNAUTHENTICATED` status without touching the service. The rejected
//! request body is still read to its end (bounded) before answering
//! (`reject::drain_rejected_body`).

use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;
use std::task::{Context, Poll};

use base64::Engine;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use http::HeaderMap;
use http_body::Body;
use rayd_core::auth::{ACCESS_TOKEN_METADATA_KEY, AuthError, requires_access_token};
use rayd_core::session::SandboxSession;
use tonic::Status;
use tower::{Layer, Service};

use super::reject::drain_rejected_body;

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
