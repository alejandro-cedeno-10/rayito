//! Tower layer of the logical deadline (ADR-011): past the deadline, and
//! during a resume grace, every RPC but `Health` and `SetTimeout` is
//! answered `FAILED_PRECONDITION sandbox_timeout` before tonic decodes it.
//! It runs inside the access-token layer, so an unauthenticated caller gets
//! `UNAUTHENTICATED` and never learns the phase, and it drains the request
//! body first like every rejection (`reject`). Never `UNAVAILABLE`: that
//! would start the SDK's reconnect loop.

use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;
use std::task::{Context, Poll};

use http_body::Body;
use rayd_core::sandbox_timeout::SANDBOX_TIMEOUT_CODE;
use rayd_core::session::SandboxSession;
use tonic::Status;
use tower::{Layer, Service};

use super::reject::drain_rejected_body;

#[derive(Clone)]
pub struct SandboxTimeoutGateLayer {
    session: Arc<SandboxSession>,
}

impl SandboxTimeoutGateLayer {
    #[must_use]
    pub fn new(session: Arc<SandboxSession>) -> Self {
        Self { session }
    }
}

impl<S> Layer<S> for SandboxTimeoutGateLayer {
    type Service = SandboxTimeoutGate<S>;

    fn layer(&self, inner: S) -> Self::Service {
        SandboxTimeoutGate {
            inner,
            session: self.session.clone(),
        }
    }
}

#[derive(Clone)]
pub struct SandboxTimeoutGate<S> {
    inner: S,
    session: Arc<SandboxSession>,
}

impl<S, ReqBody, ResBody> Service<http::Request<ReqBody>> for SandboxTimeoutGate<S>
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
        if !self.session.admits_rpc(request.uri().path()) {
            tracing::debug!(rpc = request.uri().path(), "rpc refused past the deadline");
            return Box::pin(async move {
                drain_rejected_body(request.into_body()).await;
                Ok(Status::failed_precondition(SANDBOX_TIMEOUT_CODE).into_http())
            });
        }
        let ready_inner = self.inner.clone();
        let mut inner = std::mem::replace(&mut self.inner, ready_inner);
        Box::pin(async move { inner.call(request).await })
    }
}

#[cfg(test)]
mod tests {
    use std::convert::Infallible;
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::time::{Duration, SystemTime, UNIX_EPOCH};

    use bytes::Bytes;
    use http_body_util::Empty;
    use rayd_core::auth::ANONYMOUS_RPC_PATH;
    use rayd_core::clock::Clock;
    use rayd_core::sandbox_timeout::{DeadlineAction, SET_TIMEOUT_RPC_PATH};
    use rayd_core::session::RunHookInput;
    use tonic::Code;
    use tower::ServiceExt;

    use super::*;

    const LIST: &str = "/rayito.v1.ProcessService/List";
    const SERVED: &str = "x-served";

    #[derive(Default)]
    struct StepClock {
        ms: AtomicU64,
    }

    impl Clock for StepClock {
        fn monotonic(&self) -> Duration {
            Duration::from_millis(self.ms.load(Ordering::SeqCst))
        }

        fn wall(&self) -> SystemTime {
            UNIX_EPOCH + Duration::from_secs(1_700_000_000) + self.monotonic()
        }
    }

    fn pause_session(clock: Arc<StepClock>) -> Arc<SandboxSession> {
        let session = Arc::new(SandboxSession::new(clock, "test"));
        let payload = "{\"v\":1,\"token_sha256\":\"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\",\"lifecycle\":{\"auto_resume\":false,\"cap_s\":900,\"on_timeout\":\"pause\",\"timeout_s\":60}}";
        session.run(RunHookInput {
            sandbox_id: Some("mvm-test"),
            payload: Some(payload),
        });
        session
    }

    async fn call(session: &Arc<SandboxSession>, path: &str) -> http::Response<String> {
        let inner = tower::service_fn(|_request: http::Request<Empty<Bytes>>| async {
            let mut response = http::Response::new(String::new());
            response
                .headers_mut()
                .insert(SERVED, http::HeaderValue::from_static("1"));
            Ok::<_, Infallible>(response)
        });
        let gate = SandboxTimeoutGateLayer::new(session.clone()).layer(inner);
        let request = http::Request::builder()
            .uri(path)
            .body(Empty::<Bytes>::new())
            .unwrap();
        gate.oneshot(request).await.unwrap()
    }

    fn served(response: &http::Response<String>) -> bool {
        response.headers().contains_key(SERVED)
    }

    #[tokio::test]
    async fn only_health_and_set_timeout_pass_an_expired_sandbox() {
        let clock = Arc::new(StepClock::default());
        let session = pause_session(clock.clone());
        assert!(served(&call(&session, LIST).await));
        clock.ms.store(61_000, Ordering::SeqCst);
        assert_eq!(session.tick_timeout(false), Some(DeadlineAction::Expire));
        let refused = call(&session, LIST).await;
        assert!(!served(&refused));
        let status = Status::from_header_map(refused.headers()).unwrap();
        assert_eq!(status.code(), Code::FailedPrecondition);
        assert_eq!(status.message(), "sandbox_timeout");
        assert!(served(&call(&session, ANONYMOUS_RPC_PATH).await));
        assert!(served(&call(&session, SET_TIMEOUT_RPC_PATH).await));
    }
}
