//! One loopback HTTP listener per gateway route (ADR-023, T24): a `/suspend`
//! never has to wait on this module (no participant — see
//! `features::secret_gateway`'s module doc for why), and nothing here ever
//! logs a header name or value. `GatewayRuntime::apply` is the only way a
//! route starts or stops: it always replaces the whole set, matching
//! `SecretGatewayConfig`'s own "a present section is the complete desired
//! state" semantics.

use std::net::Ipv4Addr;
use std::sync::{Arc, Mutex, PoisonError};
use std::time::Instant;

use axum::Router;
use axum::body::Body;
use axum::extract::{Request, State};
use axum::response::Response;
use http::{HeaderName, HeaderValue, StatusCode};
use rayd_core::secret_gateway::header_template::must_drop;
use rayd_core::secret_gateway::{Decision, GatewayErrorClass, GatewayRoute, GatewaySpec, TokenBucket};
use tokio::net::TcpListener;
use tokio::task::JoinHandle;

use super::upstream::{ForwardError, GatewayUpstream};

/// A route whose body or headers could not be turned into an outbound
/// request at all (never an upstream failure): an injected header value
/// that is not valid ASCII header bytes despite passing `header_safe`
/// (control-character check only), or a request line too long for `http`
/// to represent.
const BUILD_FAILED_STATUS: StatusCode = StatusCode::BAD_GATEWAY;

struct RouteState {
    route: GatewayRoute,
    bucket: Mutex<TokenBucket>,
    base: Instant,
    client: Arc<GatewayUpstream>,
    last_error: Mutex<Option<&'static str>>,
}

impl RouteState {
    fn record_error(&self, class: &'static str) {
        *self.last_error.lock().unwrap_or_else(PoisonError::into_inner) = Some(class);
    }
}

/// One running route: its real loopback port and the task serving it.
/// Dropping (aborting) `accept` is the only teardown a route needs — no
/// in-flight state survives a `Configure` call that drops a route.
struct RouteHandle {
    name: String,
    port: u16,
    accept: JoinHandle<()>,
    state: Arc<RouteState>,
}

/// A route's state at the moment `GatewayRuntime::status` was asked, for
/// `SecretGatewayStatus`.
pub struct RouteStatus {
    pub name: String,
    pub port: u16,
    pub last_error_class: Option<&'static str>,
}

/// Why `GatewayRuntime::apply` could not start the new routes; the spec
/// itself was already valid (`GatewaySpec::parse` succeeded) by the time
/// this runs, so the only way left to fail is the OS refusing a loopback
/// bind.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ListenError;

impl ListenError {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        "listen_failed"
    }
}

/// Holds every currently-running route; shared by the `ConfigurableFeature`
/// slot and torn down only by replacing it wholesale.
pub struct GatewayRuntime {
    client: Arc<GatewayUpstream>,
    routes: Mutex<Vec<RouteHandle>>,
}

impl GatewayRuntime {
    #[must_use]
    pub fn new(client: Arc<GatewayUpstream>) -> Self {
        Self {
            client,
            routes: Mutex::new(Vec::new()),
        }
    }

    /// Stops every route this runtime was serving and starts exactly the
    /// routes in `spec`, matching `SecretGatewayConfig`'s replace-whole-state
    /// semantics. Routes from the previous call always stop even when
    /// starting a new one fails partway, so a bad `Configure` call never
    /// leaves a stale, unreachable-from-`ConfigureStatus` listener running.
    ///
    /// # Errors
    /// `ListenError` when the OS refuses a loopback bind for one of
    /// `spec`'s routes; every route this call already started is stopped
    /// again before the error is returned.
    pub async fn apply(&self, spec: GatewaySpec) -> Result<(), ListenError> {
        let previous = {
            let mut guard = self.routes.lock().unwrap_or_else(PoisonError::into_inner);
            std::mem::take(&mut *guard)
        };
        for handle in previous {
            handle.accept.abort();
        }
        let mut started = Vec::with_capacity(spec.routes().len());
        for route in spec.into_routes() {
            match self.start(route).await {
                Ok(handle) => started.push(handle),
                Err(error) => {
                    // A `JoinHandle` dropped without `abort()` just detaches
                    // (the task keeps running): every route started so far
                    // in this call must be stopped explicitly before
                    // propagating the error, or a bad `Configure` call would
                    // leak a live listener nothing can ever reach again.
                    for handle in started {
                        handle.accept.abort();
                    }
                    return Err(error);
                }
            }
        }
        *self.routes.lock().unwrap_or_else(PoisonError::into_inner) = started;
        Ok(())
    }

    async fn start(&self, route: GatewayRoute) -> Result<RouteHandle, ListenError> {
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0))
            .await
            .map_err(|_| ListenError)?;
        let port = listener.local_addr().map_err(|_| ListenError)?.port();
        let name = route.name().to_owned();
        let bucket = TokenBucket::full(&route, 0);
        let state = Arc::new(RouteState {
            route,
            bucket: Mutex::new(bucket),
            base: Instant::now(),
            client: self.client.clone(),
            last_error: Mutex::new(None),
        });
        let app: Router = Router::new().fallback(forward).with_state(state.clone());
        let accept = tokio::spawn(async move {
            let _ = axum::serve(listener, app).await;
        });
        Ok(RouteHandle {
            name,
            port,
            accept,
            state,
        })
    }

    #[must_use]
    pub fn status(&self) -> Vec<RouteStatus> {
        self.routes
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .iter()
            .map(|handle| RouteStatus {
                name: handle.name.clone(),
                port: handle.port,
                last_error_class: *handle
                    .state
                    .last_error
                    .lock()
                    .unwrap_or_else(PoisonError::into_inner),
            })
            .collect()
    }
}

impl Drop for GatewayRuntime {
    fn drop(&mut self) {
        for handle in self.routes.get_mut().unwrap_or_else(PoisonError::into_inner).drain(..) {
            handle.accept.abort();
        }
    }
}

fn deny_status(class: GatewayErrorClass) -> StatusCode {
    match class {
        GatewayErrorClass::NotAllowed => StatusCode::FORBIDDEN,
        GatewayErrorClass::RateLimited => StatusCode::TOO_MANY_REQUESTS,
        GatewayErrorClass::UpstreamUnreachable | GatewayErrorClass::UpstreamError => {
            StatusCode::BAD_GATEWAY
        }
        GatewayErrorClass::UpstreamTimeout => StatusCode::GATEWAY_TIMEOUT,
    }
}

fn empty_response(status: StatusCode) -> Response {
    Response::builder()
        .status(status)
        .body(Body::empty())
        .unwrap_or_else(|_| Response::new(Body::empty()))
}

/// The one handler every route's router falls back to for any method and
/// path: decide, then forward. Never logs the method, path or any header.
async fn forward(State(state): State<Arc<RouteState>>, request: Request) -> Response {
    let method = request.method().clone();
    let path = request.uri().path().to_owned();
    let path_and_query = request
        .uri()
        .path_and_query()
        .map_or_else(|| path.clone(), |value| value.as_str().to_owned());
    let now_ms = u64::try_from(state.base.elapsed().as_millis()).unwrap_or(u64::MAX);
    let decision = {
        let mut bucket = state.bucket.lock().unwrap_or_else(PoisonError::into_inner);
        let (next, decision) =
            rayd_core::secret_gateway::evaluate(&state.route, method.as_str(), &path, *bucket, now_ms);
        *bucket = next;
        decision
    };
    let Decision::Allow = decision else {
        let Decision::Deny(class) = decision else {
            unreachable!("evaluate only ever returns Allow or Deny")
        };
        state.record_error(class.as_str());
        return empty_response(deny_status(class));
    };
    let Some(outbound) = build_outbound(&state, method, &path_and_query, request) else {
        state.record_error(GatewayErrorClass::UpstreamError.as_str());
        return empty_response(BUILD_FAILED_STATUS);
    };
    match state.client.send(outbound).await {
        Ok(response) => into_axum_response(response),
        Err(ForwardError::Unreachable) => {
            state.record_error(GatewayErrorClass::UpstreamUnreachable.as_str());
            empty_response(deny_status(GatewayErrorClass::UpstreamUnreachable))
        }
        Err(ForwardError::Timeout) => {
            state.record_error(GatewayErrorClass::UpstreamTimeout.as_str());
            empty_response(deny_status(GatewayErrorClass::UpstreamTimeout))
        }
    }
}

/// Builds the complete outbound request: method and URI, inbound headers
/// with whatever `must_drop` names stripped, the route's vaulted values
/// injected, and `request`'s own body streamed through unbuffered. `None`
/// means a vaulted value could not become a valid header byte sequence, or
/// the upstream URI could not be built — both checked once already at
/// `Configure` time in the common case, so this is a last-resort guard,
/// never the normal path.
fn build_outbound(
    state: &RouteState,
    method: http::Method,
    path_and_query: &str,
    request: Request,
) -> Option<http::Request<Body>> {
    let uri = format!("{}{path_and_query}", state.route.upstream());
    let mut builder = http::Request::builder().method(method).uri(uri);
    for (name, value) in request.headers() {
        let lower = name.as_str().to_ascii_lowercase();
        if !must_drop(&state.route, &lower) {
            builder = builder.header(name, value);
        }
    }
    for name in state.route.header_names() {
        let header_name = HeaderName::from_bytes(name.as_bytes()).ok()?;
        let header_value = state.route.header_value(name)?;
        let value = HeaderValue::from_str(header_value.expose()).ok()?;
        builder = builder.header(header_name, value);
    }
    builder.body(request.into_body()).ok()
}

fn into_axum_response(response: http::Response<hyper::body::Incoming>) -> Response {
    let (mut parts, incoming) = response.into_parts();
    for name in rayd_core::secret_gateway::header_template::HOP_BY_HOP_HEADERS {
        parts.headers.remove(name);
    }
    Response::from_parts(parts, Body::new(incoming))
}
