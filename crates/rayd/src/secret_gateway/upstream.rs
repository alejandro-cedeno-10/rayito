//! The HTTPS client every gateway route forwards through (ADR-023): HTTP/1.1
//! (a loopback proxy in front of one fixed upstream API gets nothing from
//! h2 multiplexing that a pooled connection does not already give it),
//! `FilteringResolver` so a route's upstream can never resolve to a
//! loopback, link-local or IMDS address
//! (`rayd_core::transfer::is_forbidden_address`, the same predicate
//! `adapters::signed_http` and the local proxy use), the OS trust store,
//! and bounded connect/idle timeouts. Both the request and the response
//! body stream through unbuffered (no `Vec<u8>` ever holds a whole body),
//! so a chunked upload or an SSE response passes through as it arrives.

use std::error::Error as StdError;
use std::fmt;
use std::future::Future;
use std::io;
use std::net::{IpAddr, SocketAddr};
use std::pin::Pin;
use std::task::{Context, Poll};
use std::time::Duration;

use axum::body::Body;
use http::{Request, Response, Uri};
use hyper::body::Incoming;
use hyper_rustls::{ConfigBuilderExt, HttpsConnector, HttpsConnectorBuilder};
use hyper_util::client::legacy::Client;
use hyper_util::client::legacy::connect::HttpConnector;
use hyper_util::client::legacy::connect::dns::{GaiResolver, Name};
use hyper_util::rt::{TokioExecutor, TokioTimer};
use rayd_core::transfer::is_forbidden_address;
use rustls::ClientConfig;
use tower::Service;

/// No upstream connection attempt waits longer than this; well above a
/// loopback-to-internet handshake, short enough that a stuck upstream
/// never pins a sandbox command for long.
pub const CONNECT_TIMEOUT: Duration = Duration::from_secs(10);
/// No upstream request waits longer than this for its response *head*
/// (status line and headers) once connected; separate from
/// `CONNECT_TIMEOUT` because a slow TLS handshake and an upstream that
/// accepted the connection but never answers at all are different
/// failures, and without this bound the latter pins the forwarding task
/// (and the sandbox command behind it) forever. 10 minutes: generous enough
/// for a long-running LLM completion request to still be waiting on its
/// first response byte, short enough that a genuinely stuck upstream is
/// eventually reported rather than hung on indefinitely. An SSE response's
/// *body*, once it starts, is unaffected (this only bounds the wait for the
/// head; the open-ended stream after it is the caller's own concern, same
/// as before this constant existed).
pub const RESPONSE_HEAD_TIMEOUT: Duration = Duration::from_secs(600);
/// How long the pool keeps an idle connection to one upstream before
/// closing it.
const POOL_IDLE_TIMEOUT: Duration = Duration::from_secs(30);
/// At most this many idle connections per upstream host: one gateway route
/// talks to exactly one host, so a handful covers bursty concurrent calls
/// without the pool growing unbounded.
const POOL_MAX_IDLE_PER_HOST: usize = 4;

/// The native trust store could not be loaded.
#[derive(Debug)]
pub struct UpstreamInitError(io::Error);

impl fmt::Display for UpstreamInitError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "trust store unavailable: {}", self.0.kind())
    }
}

impl StdError for UpstreamInitError {}

/// Why forwarding a request never reached (or never finished) a response;
/// mirrors `rayd_core::secret_gateway::GatewayErrorClass`'s upstream
/// variants.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ForwardError {
    Unreachable,
    Timeout,
    /// The connection was established but the request still failed: a TLS
    /// error, a malformed or truncated response, or the connection closing
    /// mid-response. Distinct from `Timeout` (which only ever means
    /// `CONNECT_TIMEOUT` or `RESPONSE_HEAD_TIMEOUT` actually elapsed) so
    /// `GatewayStatus.last_error_class` does not call an upstream that
    /// answered badly, or not at all, "timed out".
    Upstream,
}

type BoxError = Box<dyn StdError + Send + Sync>;
type Connector = HttpsConnector<HttpConnector<FilteringResolver>>;

/// A resolver that keeps only the addresses `is_forbidden_address` allows,
/// so `GatewayRoute::upstream` can never be pointed at the guest's own
/// loopback, a link-local address or IMDS by a route the SDK built from
/// user input.
#[derive(Clone)]
struct FilteringResolver(GaiResolver);

impl Default for FilteringResolver {
    fn default() -> Self {
        Self(GaiResolver::new())
    }
}

/// Every address a name resolved to was one `rayd` refuses to reach.
#[derive(Debug, Clone, Copy)]
struct ForbiddenAddressError;

impl fmt::Display for ForbiddenAddressError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("forbidden_address")
    }
}

impl StdError for ForbiddenAddressError {}

type ResolveFuture =
    Pin<Box<dyn Future<Output = Result<std::vec::IntoIter<SocketAddr>, BoxError>> + Send>>;

impl Service<Name> for FilteringResolver {
    type Response = std::vec::IntoIter<SocketAddr>;
    type Error = BoxError;
    type Future = ResolveFuture;

    fn poll_ready(&mut self, cx: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
        self.0
            .poll_ready(cx)
            .map_err(|error| Box::new(error) as BoxError)
    }

    fn call(&mut self, name: Name) -> Self::Future {
        let resolving = self.0.call(name);
        Box::pin(async move {
            let allowed: Vec<SocketAddr> = resolving
                .await
                .map_err(|error| Box::new(error) as BoxError)?
                .filter(|address| !is_forbidden_address(address.ip()))
                .collect();
            if allowed.is_empty() {
                Err(Box::new(ForbiddenAddressError) as BoxError)
            } else {
                Ok(allowed.into_iter())
            }
        })
    }
}

/// The client every `GatewayRoute`'s listener shares: one pooled
/// connection set per upstream host, built once by `features::secret_gateway`.
pub struct GatewayUpstream {
    client: Client<Connector, Body>,
}

impl GatewayUpstream {
    /// OS trust store, TLS 1.2+, HTTPS only, the address filter above.
    ///
    /// # Errors
    /// `UpstreamInitError` when the OS trust store cannot be loaded.
    pub fn new() -> Result<Self, UpstreamInitError> {
        let config = ClientConfig::builder_with_provider(std::sync::Arc::new(
            rustls::crypto::aws_lc_rs::default_provider(),
        ))
        .with_safe_default_protocol_versions()
        .map_err(|error| UpstreamInitError(io::Error::other(error)))?
        .with_native_roots()
        .map_err(UpstreamInitError)?
        .with_no_client_auth();
        let mut http = HttpConnector::new_with_resolver(FilteringResolver::default());
        http.enforce_http(false);
        http.set_connect_timeout(Some(CONNECT_TIMEOUT));
        http.set_nodelay(true);
        let connector = HttpsConnectorBuilder::new()
            .with_tls_config(config)
            .https_only()
            .enable_http1()
            .wrap_connector(http);
        let client = Client::builder(TokioExecutor::new())
            .pool_idle_timeout(POOL_IDLE_TIMEOUT)
            .pool_max_idle_per_host(POOL_MAX_IDLE_PER_HOST)
            .pool_timer(TokioTimer::new())
            .build(connector);
        Ok(Self { client })
    }

    /// Sends `request` and hands back the response head plus its still-open
    /// body (`Incoming`): the caller streams it on, nothing is buffered
    /// here. `ForwardError::Timeout` only ever covers the connection
    /// attempt (`CONNECT_TIMEOUT`); a connection that opens but then stalls
    /// mid-response is the caller's own idle timeout to enforce, because
    /// only the caller knows whether the stall is an SSE keep-alive gap or
    /// a truly stuck upstream.
    ///
    /// # Errors
    /// `ForwardError::Unreachable` when `request`'s URI is an IP-literal
    /// `is_forbidden_address` refuses (checked here because the connector
    /// dials an IP literal directly, without ever asking
    /// `FilteringResolver`) or the connection could not be established
    /// (including a resolved address the resolver refused);
    /// `ForwardError::Timeout` when the connection attempt or the wait for
    /// the response head timed out (`CONNECT_TIMEOUT`,
    /// `RESPONSE_HEAD_TIMEOUT`); `ForwardError::Upstream` for every other
    /// failure once connected (a TLS or framing error, a connection closed
    /// mid-response).
    pub async fn send(&self, request: Request<Body>) -> Result<Response<Incoming>, ForwardError> {
        if literal_host_forbidden(request.uri()) {
            return Err(ForwardError::Unreachable);
        }
        match tokio::time::timeout(RESPONSE_HEAD_TIMEOUT, self.client.request(request)).await {
            Ok(Ok(response)) => Ok(response),
            Ok(Err(error)) => Err(classify(&error)),
            Err(_elapsed) => Err(ForwardError::Timeout),
        }
    }
}

/// `true` when `uri`'s host is an IP literal `is_forbidden_address`
/// refuses. A hostname is left to `FilteringResolver`: this only covers
/// the literal case the connector would otherwise dial unchecked (mirrors
/// `adapters::signed_http::HyperSignedHttp::check_literal_host`).
fn literal_host_forbidden(uri: &Uri) -> bool {
    uri.host()
        .map(|host| host.trim_start_matches('[').trim_end_matches(']'))
        .and_then(|host| host.parse::<IpAddr>().ok())
        .is_some_and(is_forbidden_address)
}

/// `hyper_util::client::legacy::Error::is_connect()` is only ever `true`
/// for a failed connection attempt; every other error (TLS, framing, a
/// connection reset mid-response) reached this far *after* connecting, so
/// the fallback below must never call it `Timeout` — that class is reserved
/// for the two cases `send` already covers explicitly: a genuine I/O
/// timeout wrapped somewhere in the error chain, or `RESPONSE_HEAD_TIMEOUT`
/// itself elapsing (handled in `send`, never reaching this function).
fn classify(error: &hyper_util::client::legacy::Error) -> ForwardError {
    let mut source: Option<&(dyn StdError + 'static)> = Some(error);
    while let Some(current) = source {
        if current.downcast_ref::<ForbiddenAddressError>().is_some() {
            return ForwardError::Unreachable;
        }
        if current
            .downcast_ref::<io::Error>()
            .is_some_and(|io_error| io_error.kind() == io::ErrorKind::TimedOut)
        {
            return ForwardError::Timeout;
        }
        source = current.source();
    }
    if error.is_connect() {
        ForwardError::Unreachable
    } else {
        ForwardError::Upstream
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_literal_imds_or_loopback_host_is_forbidden() {
        for uri in [
            "https://169.254.169.254/latest/meta-data/",
            "https://127.0.0.1/",
            "https://[::1]/",
        ] {
            assert!(
                literal_host_forbidden(&uri.parse().unwrap()),
                "{uri} should be forbidden"
            );
        }
        assert!(!literal_host_forbidden(
            &"https://93.184.216.34/".parse().unwrap()
        ));
    }

    #[test]
    fn a_hostname_is_left_to_the_resolver() {
        // Not an IP literal: `literal_host_forbidden` never resolves it, so
        // it is never forbidden at this layer (`FilteringResolver` is the
        // one that checks a resolved name's addresses).
        assert!(!literal_host_forbidden(
            &"https://api.anthropic.com/".parse().unwrap()
        ));
    }

    /// No network attempt here: `send` rejects an IP-literal forbidden host
    /// before ever reaching the connector, so this is deterministic in any
    /// environment (unlike actually dialing 169.254.169.254, whose real
    /// behaviour — refused, filtered or a genuine IMDS reply — depends on
    /// where the test runs).
    #[tokio::test]
    async fn sending_to_a_literal_forbidden_address_never_connects() {
        let upstream = GatewayUpstream::new().unwrap();
        let request = Request::builder()
            .uri("https://169.254.169.254/latest/meta-data/")
            .body(Body::empty())
            .unwrap();
        let error = upstream.send(request).await.unwrap_err();
        assert_eq!(error, ForwardError::Unreachable);
    }

    /// A connection the server accepts and then closes before any response
    /// bytes arrive fails *after* `Connect` already succeeded — the actual
    /// shape of the regression this finding closes (a TLS or framing error,
    /// or the connection resetting mid-response). Built against a plain
    /// `HttpConnector` (not `GatewayUpstream`'s HTTPS one): `is_connect()`
    /// is true for the whole TLS handshake too, so an HTTPS target cannot
    /// reach this branch without a certificate the client's native trust
    /// store accepts; `classify` itself takes no connector type, so this
    /// still exercises exactly the function `send` calls.
    #[tokio::test]
    async fn a_connection_closed_after_connecting_is_classified_as_upstream_not_timeout() {
        use hyper_util::client::legacy::Client;
        use hyper_util::client::legacy::connect::HttpConnector;
        use tokio::net::TcpListener;

        let listener = TcpListener::bind(("127.0.0.1", 0)).await.unwrap();
        let port = listener.local_addr().unwrap().port();
        tokio::spawn(async move {
            if let Ok((socket, _)) = listener.accept().await {
                drop(socket);
            }
        });
        let client: Client<HttpConnector, Body> =
            Client::builder(TokioExecutor::new()).build(HttpConnector::new());
        let request = Request::builder()
            .uri(format!("http://127.0.0.1:{port}/"))
            .body(Body::empty())
            .unwrap();
        let error = client.request(request).await.unwrap_err();
        assert!(
            !error.is_connect(),
            "the TCP connect itself must have succeeded"
        );
        assert_eq!(classify(&error), ForwardError::Upstream);
    }
}
