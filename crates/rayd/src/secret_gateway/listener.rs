//! One loopback HTTP listener per gateway route (ADR-023, T24): a `/suspend`
//! never has to wait on this module (no participant — see
//! `features::secret_gateway`'s module doc for why), and nothing here ever
//! logs a header name or value. `GatewayRuntime::apply` is the only way a
//! route starts or stops, matching `SecretGatewayConfig`'s own "a present
//! section is the complete desired state" semantics — but a route whose
//! *name* is present both before and after a reapply keeps its listener and
//! port: only its `RouteState` (vault values, allow rules, rate) swaps,
//! behind a `SwappableState` `forward` reloads once per request. Every process
//! already talking to that port (the documented way to rotate a secret,
//! `pasarela-de-secretos.md`) survives the rotation instead of getting
//! connection refused. A route whose name is *not* in the new spec is torn
//! down with a graceful shutdown (`ROUTE_SHUTDOWN_GRACE`): its listener
//! stops accepting, an idle keep-alive connection is closed immediately and
//! an in-flight request is allowed to finish, so the credential a removed
//! or rotated route stops serving really does stop being reachable on the
//! next request — not just for a connection opened after the `Configure`
//! call.

use std::collections::BTreeMap;
use std::net::Ipv4Addr;
use std::sync::{Arc, Mutex, PoisonError, RwLock};
use std::time::{Duration, Instant};

use axum::Router;
use axum::body::Body;
use axum::extract::{Request, State};
use axum::response::Response;
use http::{HeaderName, HeaderValue, StatusCode};
use rayd_core::secret_gateway::header_template::must_drop;
use rayd_core::secret_gateway::{
    Decision, GatewayErrorClass, GatewayRoute, GatewaySpec, TokenBucket,
};
use tokio::net::TcpListener;
use tokio::task::JoinHandle;
use tokio_util::sync::CancellationToken;

use super::upstream::{ForwardError, GatewayUpstream};

/// How long `GatewayRuntime::apply` waits for a torn-down route's graceful
/// shutdown before moving on without it: bounds one `Configure` call
/// against a single slow in-flight request on the route being replaced or
/// removed. The task itself is never aborted after this elapses (dropping
/// its `JoinHandle` only detaches it, `Drop for GatewayRuntime`'s own
/// comment), so a request already in flight still finishes; `apply` simply
/// stops waiting for it.
const ROUTE_SHUTDOWN_GRACE: Duration = Duration::from_secs(2);

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
        *self
            .last_error
            .lock()
            .unwrap_or_else(PoisonError::into_inner) = Some(class);
    }
}

/// A route's current config, replaced in place by `GatewayRuntime::apply`
/// on a reapply that keeps the route's name. The lock is held only to
/// clone or replace the inner `Arc` (never across an `.await`), so a
/// request never waits on another request, only on a concurrent swap.
#[derive(Clone)]
struct SwappableState(Arc<RwLock<Arc<RouteState>>>);

impl SwappableState {
    fn new(state: RouteState) -> Self {
        Self(Arc::new(RwLock::new(Arc::new(state))))
    }

    /// The `RouteState` current right now; a later `store` never changes
    /// what this returned `Arc` points at.
    fn load(&self) -> Arc<RouteState> {
        self.0
            .read()
            .unwrap_or_else(PoisonError::into_inner)
            .clone()
    }

    fn store(&self, state: RouteState) {
        *self.0.write().unwrap_or_else(PoisonError::into_inner) = Arc::new(state);
    }
}

/// One running route: its real loopback port, the task serving it and its
/// current, swappable config. `GatewayRuntime::apply` reuses this whole
/// struct (never rebinding the listener) for any route name present in both
/// the previous and the new spec — only `state` is replaced, atomically,
/// by `SwappableState::store`; `forward` reloads it once per request
/// (`SwappableState::load`), so an in-flight request always finishes against the
/// `RouteState` it started with while the next one already sees the swap.
struct RouteHandle {
    name: String,
    port: u16,
    shutdown: CancellationToken,
    serve: JoinHandle<()>,
    state: SwappableState,
}

impl RouteHandle {
    /// Cancels the shared token (axum's graceful shutdown then closes any
    /// idle connection immediately and lets an in-flight request finish)
    /// and waits up to `ROUTE_SHUTDOWN_GRACE` for the serving task to end;
    /// past that it stops waiting without aborting the task, so one slow
    /// request on a route being torn down can never block the `Configure`
    /// call past a bounded margin.
    async fn shutdown(self) {
        self.shutdown.cancel();
        let _ = tokio::time::timeout(ROUTE_SHUTDOWN_GRACE, self.serve).await;
    }
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

    /// Reconciles the running set against `spec`, matching
    /// `SecretGatewayConfig`'s replace-whole-state semantics for *which
    /// routes exist* while keeping each still-present route's listener,
    /// port and in-flight connections alive: a route whose name is in both
    /// the running set and `spec` only gets its `RouteState` swapped
    /// (`RouteHandle::state`, a `SwappableState`); a route whose name is new gets
    /// a fresh listener; a route whose name is gone is shut down
    /// gracefully (`RouteHandle::shutdown`).
    ///
    /// Binding a new route's listener is the only step that can fail. On
    /// failure every route this call already bound is torn down and the
    /// routes untouched so far (kept-alive and not-yet-removed ones) are
    /// restored exactly as they were — a bad `Configure` call never
    /// disturbs a route it did not even ask to change, and never leaves a
    /// listener nothing can reach from `ConfigureStatus` again.
    ///
    /// # Errors
    /// `ListenError` when the OS refuses a loopback bind for one of
    /// `spec`'s new routes.
    pub async fn apply(&self, spec: GatewaySpec) -> Result<(), ListenError> {
        let mut previous: BTreeMap<String, RouteHandle> = {
            let mut guard = self.routes.lock().unwrap_or_else(PoisonError::into_inner);
            std::mem::take(&mut *guard)
                .into_iter()
                .map(|handle| (handle.name.clone(), handle))
                .collect()
        };
        let mut reused: Vec<(RouteHandle, GatewayRoute)> = Vec::new();
        let mut started: Vec<RouteHandle> = Vec::new();
        for route in spec.into_routes() {
            if let Some(handle) = previous.remove(route.name()) {
                reused.push((handle, route));
            } else {
                match self.start(route).await {
                    Ok(handle) => started.push(handle),
                    Err(error) => {
                        for handle in started {
                            handle.shutdown().await;
                        }
                        // Put back every route this call touched, reused or
                        // not and none of them mutated yet: a failed bind
                        // must never disturb a route it did not even ask to
                        // change.
                        let mut restored = previous;
                        for (handle, _route) in reused {
                            restored.insert(handle.name.clone(), handle);
                        }
                        *self.routes.lock().unwrap_or_else(PoisonError::into_inner) =
                            restored.into_values().collect();
                        return Err(error);
                    }
                }
            }
        }
        let mut next = started;
        next.reserve(reused.len());
        for (handle, route) in reused {
            let state = self.build_state(route);
            handle.state.store(state);
            next.push(handle);
        }
        // Whatever is left in `previous` is a route `spec` dropped.
        for (_, handle) in previous {
            handle.shutdown().await;
        }
        *self.routes.lock().unwrap_or_else(PoisonError::into_inner) = next;
        Ok(())
    }

    fn build_state(&self, route: GatewayRoute) -> RouteState {
        let bucket = TokenBucket::full(&route, 0);
        RouteState {
            route,
            bucket: Mutex::new(bucket),
            base: Instant::now(),
            client: self.client.clone(),
            last_error: Mutex::new(None),
        }
    }

    async fn start(&self, route: GatewayRoute) -> Result<RouteHandle, ListenError> {
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0))
            .await
            .map_err(|_| ListenError)?;
        let port = listener.local_addr().map_err(|_| ListenError)?.port();
        let name = route.name().to_owned();
        let state = SwappableState::new(self.build_state(route));
        let app: Router = Router::new().fallback(forward).with_state(state.clone());
        let shutdown = CancellationToken::new();
        let signal = shutdown.clone().cancelled_owned();
        let serve = tokio::spawn(async move {
            let _ = axum::serve(listener, app)
                .with_graceful_shutdown(signal)
                .await;
        });
        Ok(RouteHandle {
            name,
            port,
            shutdown,
            serve,
            state,
        })
    }

    #[must_use]
    pub fn status(&self) -> Vec<RouteStatus> {
        self.routes
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .iter()
            .map(|handle| {
                let state = handle.state.load();
                RouteStatus {
                    name: handle.name.clone(),
                    port: handle.port,
                    last_error_class: *state
                        .last_error
                        .lock()
                        .unwrap_or_else(PoisonError::into_inner),
                }
            })
            .collect()
    }
}

impl Drop for GatewayRuntime {
    fn drop(&mut self) {
        // A full teardown (the whole `GatewayRuntime`, not just one route
        // going away under `apply`) never needs to wait for an in-flight
        // request: the MicroVM itself is going away with it, same as any
        // other connection the freeze cuts.
        for handle in self
            .routes
            .get_mut()
            .unwrap_or_else(PoisonError::into_inner)
            .drain(..)
        {
            handle.shutdown.cancel();
            handle.serve.abort();
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
/// Loads the route's current `RouteState` once, up front
/// (`SwappableState::load`): a `Configure` reapply that swaps it mid-request
/// never changes which vaulted value or allowlist this one request sees,
/// and the next request picks up the swap on its own load.
async fn forward(State(state): State<SwappableState>, request: Request) -> Response {
    let state = state.load();
    let method = request.method().clone();
    let path = request.uri().path().to_owned();
    let path_and_query = request
        .uri()
        .path_and_query()
        .map_or_else(|| path.clone(), |value| value.as_str().to_owned());
    let now_ms = u64::try_from(state.base.elapsed().as_millis()).unwrap_or(u64::MAX);
    let decision = {
        let mut bucket = state.bucket.lock().unwrap_or_else(PoisonError::into_inner);
        let (next, decision) = rayd_core::secret_gateway::evaluate(
            &state.route,
            method.as_str(),
            &path,
            *bucket,
            now_ms,
        );
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
        Err(ForwardError::Upstream) => {
            state.record_error(GatewayErrorClass::UpstreamError.as_str());
            empty_response(deny_status(GatewayErrorClass::UpstreamError))
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

#[cfg(test)]
mod tests {
    use std::time::Duration;

    use rayd_core::secret_gateway::vault::SecretValue;
    use rayd_core::secret_gateway::{GatewaySpec, RawRoute};
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpStream;

    use super::*;

    /// No test here ever waits on a real upstream: every request is to a
    /// path `allow` never lists, denied by the method/path check before
    /// `GatewayUpstream::send` is ever called.
    fn spec(route_name: &str, header_value: &str) -> GatewaySpec {
        GatewaySpec::parse(vec![RawRoute {
            name: route_name.to_owned(),
            upstream: "https://example.com".to_owned(),
            headers: vec![(
                "x-api-key".to_owned(),
                SecretValue::new(header_value.to_owned()),
            )],
            allow: vec![("GET".to_owned(), "/v1/messages".to_owned())],
            rate_per_minute: 60,
        }])
        .unwrap()
    }

    fn runtime() -> GatewayRuntime {
        GatewayRuntime::new(Arc::new(GatewayUpstream::new().unwrap()))
    }

    /// Bound for every read in these tests: no response here ever waits on
    /// a real upstream, so anything slower than this is a hang.
    const READ_TIMEOUT: Duration = Duration::from_secs(5);

    async fn send_request(stream: &mut TcpStream, path: &str) -> Vec<u8> {
        let request =
            format!("GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: keep-alive\r\n\r\n");
        stream.write_all(request.as_bytes()).await.unwrap();
        let mut buf = vec![0_u8; 256];
        let read = tokio::time::timeout(READ_TIMEOUT, stream.read(&mut buf))
            .await
            .expect("the request gets an immediate response")
            .unwrap();
        buf.truncate(read);
        buf
    }

    async fn send_denied_request(stream: &mut TcpStream) -> Vec<u8> {
        send_request(stream, "/denied").await
    }

    /// A route whose upstream is the loopback IP literal: `GatewayUpstream`
    /// refuses it before dialing (`ForwardError::Unreachable`, 502), so an
    /// *allowed* request still gets an immediate answer distinct from a
    /// *denied* one (403), with no network involved.
    fn loopback_spec(allowed_path: &str) -> GatewaySpec {
        GatewaySpec::parse(vec![RawRoute {
            name: "a".to_owned(),
            upstream: "https://127.0.0.1".to_owned(),
            headers: vec![],
            allow: vec![("GET".to_owned(), allowed_path.to_owned())],
            rate_per_minute: 60,
        }])
        .unwrap()
    }

    #[tokio::test]
    async fn a_rotation_reaches_an_already_open_keepalive_connection() {
        let runtime = runtime();
        runtime.apply(loopback_spec("/x")).await.unwrap();
        let port = runtime.status()[0].port;
        let mut stream = TcpStream::connect(("127.0.0.1", port)).await.unwrap();
        let allowed = send_request(&mut stream, "/x").await;
        assert!(
            allowed.starts_with(b"HTTP/1.1 502"),
            "unexpected first response: {}",
            String::from_utf8_lossy(&allowed)
        );

        // Same route name, a different allowlist: the very next request on
        // the *same* connection must already be judged by the new state,
        // never the one the connection was opened under.
        runtime.apply(loopback_spec("/y")).await.unwrap();
        assert_eq!(runtime.status()[0].port, port);
        let denied = send_request(&mut stream, "/x").await;
        assert!(
            denied.starts_with(b"HTTP/1.1 403"),
            "unexpected response after the rotation: {}",
            String::from_utf8_lossy(&denied)
        );
    }

    #[tokio::test]
    async fn reapplying_with_the_same_route_name_keeps_the_port() {
        let runtime = runtime();
        runtime.apply(spec("a", "sk-1")).await.unwrap();
        let first_port = runtime.status()[0].port;

        // A different header value (a rotated secret): same route name, so
        // `apply` must swap the existing listener's `RouteState` in place
        // rather than rebinding a new port (ADR-023, the finding this test
        // guards: a rotation must not be a client-visible outage).
        runtime.apply(spec("a", "sk-2")).await.unwrap();
        let second_port = runtime.status()[0].port;

        assert_eq!(first_port, second_port);
    }

    #[tokio::test]
    async fn a_route_absent_from_a_reapply_is_torn_down_while_others_keep_running() {
        let runtime = runtime();
        let two_routes = GatewaySpec::parse(vec![
            RawRoute {
                name: "a".to_owned(),
                upstream: "https://example.com".to_owned(),
                headers: vec![],
                allow: vec![("GET".to_owned(), "/x".to_owned())],
                rate_per_minute: 60,
            },
            RawRoute {
                name: "b".to_owned(),
                upstream: "https://example.com".to_owned(),
                headers: vec![],
                allow: vec![("GET".to_owned(), "/x".to_owned())],
                rate_per_minute: 60,
            },
        ])
        .unwrap();
        runtime.apply(two_routes).await.unwrap();
        let port_b_before = runtime
            .status()
            .into_iter()
            .find(|status| status.name == "b")
            .unwrap()
            .port;

        runtime.apply(spec("b", "sk-1")).await.unwrap();

        let status = runtime.status();
        assert_eq!(status.len(), 1);
        assert_eq!(status[0].name, "b");
        assert_eq!(status[0].port, port_b_before, "route b must keep its port");
    }

    #[tokio::test]
    async fn tearing_down_a_route_closes_an_idle_keepalive_connection() {
        let runtime = runtime();
        runtime.apply(spec("a", "sk-1")).await.unwrap();
        let port = runtime.status()[0].port;

        let mut stream = TcpStream::connect(("127.0.0.1", port)).await.unwrap();
        let response = send_denied_request(&mut stream).await;
        assert!(
            response.starts_with(b"HTTP/1.1 403"),
            "unexpected first response: {}",
            String::from_utf8_lossy(&response)
        );

        // Replacing the whole spec with one that no longer names "a" tears
        // it down; `apply` waits (up to `ROUTE_SHUTDOWN_GRACE`) for the
        // graceful shutdown, so by the time it returns the still-open,
        // idle keep-alive connection above must already be closed.
        runtime
            .apply(GatewaySpec::parse(vec![]).unwrap())
            .await
            .unwrap();

        let mut buf = [0_u8; 16];
        let read = tokio::time::timeout(READ_TIMEOUT, stream.read(&mut buf))
            .await
            .expect("the server must close the idle connection, not hang");
        assert_eq!(
            read.unwrap(),
            0,
            "the connection must be closed (EOF), not still answering"
        );
    }
}
