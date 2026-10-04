//! The hooks listener's HTTP/1.1 server: the connection cap shared with
//! the gRPC listener (`adapters::CappedListener`), a deadline for each
//! request head and one request per connection, so neither an idle socket
//! nor a request head sent a byte at a time can keep a slot. Every
//! request carries its connection's peer address as
//! `ConnectInfo<SocketAddr>`, which the peer check (`guard_peers`) reads.
//!
//! `axum::serve` builds its hyper connection without a timer, which leaves
//! hyper's request-head timeout inert, and accepts without a cap; this
//! loop is the same serve with both in place.

use std::time::Duration;

use axum::Router;
use axum::extract::connect_info::ConnectInfo;
use hyper::body::Incoming;
use hyper::server::conn::http1;
use hyper_util::rt::{TokioIo, TokioTimer};
use rayd_core::listeners::{HOOKS_HEADER_READ_TIMEOUT, HOOKS_MAX_CONNECTIONS};
use tokio::net::{TcpListener, TcpStream};
use tokio::task::JoinSet;
use tokio_util::sync::CancellationToken;
use tower::ServiceExt;

use crate::adapters::{CappedListener, CappedStream};

/// The hooks listener's bounds (`rayd_core::listeners` in production,
/// smaller in the tests).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct HookServerSettings {
    pub max_connections: usize,
    pub header_read_timeout: Duration,
}

impl Default for HookServerSettings {
    fn default() -> Self {
        Self {
            max_connections: HOOKS_MAX_CONNECTIONS,
            header_read_timeout: HOOKS_HEADER_READ_TIMEOUT,
        }
    }
}

/// Serves `router` on `listener` until `shutdown` is cancelled, then lets
/// every open connection finish its response and returns.
pub async fn serve(
    listener: TcpListener,
    router: Router,
    settings: HookServerSettings,
    shutdown: CancellationToken,
) {
    let mut listener = CappedListener::new(listener, settings.max_connections);
    let mut connections = JoinSet::new();
    loop {
        tokio::select! {
            () = shutdown.cancelled() => break,
            stream = listener.accept() => {
                connections.spawn(serve_connection(
                    stream,
                    router.clone(),
                    settings.header_read_timeout,
                    shutdown.clone(),
                ));
            }
            Some(_) = connections.join_next(), if !connections.is_empty() => {}
        }
    }
    drop(listener);
    while connections.join_next().await.is_some() {}
}

/// One connection, one request: hyper closes it after the response
/// (`keep_alive(false)`), so the head deadline only ever applies to a
/// request that is really expected.
async fn serve_connection(
    stream: CappedStream<TcpStream>,
    router: Router,
    header_read_timeout: Duration,
    shutdown: CancellationToken,
) {
    let peer = stream.io().peer_addr().ok();
    let service = hyper::service::service_fn(move |mut request: hyper::Request<Incoming>| {
        if let Some(peer) = peer {
            request.extensions_mut().insert(ConnectInfo(peer));
        }
        router.clone().oneshot(request)
    });
    let mut builder = http1::Builder::new();
    builder
        .timer(TokioTimer::new())
        .header_read_timeout(header_read_timeout)
        .keep_alive(false);
    let connection = builder.serve_connection(TokioIo::new(stream), service);
    let mut connection = std::pin::pin!(connection);
    let result = tokio::select! {
        result = connection.as_mut() => result,
        () = shutdown.cancelled() => {
            connection.as_mut().graceful_shutdown();
            connection.await
        }
    };
    if let Err(error) = result {
        tracing::debug!(head_timeout = error.is_timeout(), "hook connection dropped");
    }
}
