//! The hooks listener's bounds over real connections (`rayd::hooks::serve`,
//! `SECURITY.md` T7): sandbox processes reach `:9000` on loopback without a
//! token, so neither idle sockets, a request head that never ends nor a
//! kept-alive connection may hold `rayd`'s descriptors, and the platform's
//! `/terminate` still gets through once a slot frees.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use rayd::adapters::OsRandomSource;
use rayd::code::CodeManager;
use rayd::hooks::{HookServerSettings, hook_path};
use rayd::lifecycle::SuspendSignal;
use rayd_core::clock::SystemClock;
use rayd_core::lifecycle::Hook;
use rayd_core::session::SandboxSession;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio_util::sync::CancellationToken;

/// Long enough for a response that should not come to show up.
const NOT_SERVED_WITHIN: Duration = Duration::from_millis(500);
const SERVED_WITHIN: Duration = Duration::from_secs(5);
/// A head deadline no test reaches unless it means to.
const NEVER: Duration = Duration::from_secs(60);
const SHORT_HEAD_DEADLINE: Duration = Duration::from_millis(300);

async fn start(settings: HookServerSettings) -> (SocketAddr, CancellationToken) {
    let session = Arc::new(SandboxSession::new(Arc::new(SystemClock::new()), "test"));
    let code = CodeManager::disabled(session.clone(), Arc::new(OsRandomSource));
    let shutdown = CancellationToken::new();
    let router = rayd::hooks::router(
        session,
        code,
        Arc::new(SuspendSignal::new()),
        shutdown.clone(),
    );
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    tokio::spawn(rayd::hooks::serve(
        listener,
        router,
        settings,
        shutdown.clone(),
    ));
    (address, shutdown)
}

fn request(hook: Hook) -> String {
    format!(
        "POST {} HTTP/1.1\r\nHost: localhost:9000\r\nContent-Length: 0\r\n\r\n",
        hook_path(hook)
    )
}

/// Sends a whole hook request and reads until the server closes.
async fn post(hooks: SocketAddr, hook: Hook) -> String {
    let mut stream = TcpStream::connect(hooks).await.unwrap();
    stream.write_all(request(hook).as_bytes()).await.unwrap();
    let mut response = String::new();
    stream.read_to_string(&mut response).await.unwrap();
    response
}

#[tokio::test]
async fn idle_connections_beyond_the_cap_wait_and_terminate_is_served_once_one_closes() {
    let (hooks, _shutdown) = start(HookServerSettings {
        max_connections: 2,
        header_read_timeout: NEVER,
    })
    .await;
    let first_idle = TcpStream::connect(hooks).await.unwrap();
    let _second_idle = TcpStream::connect(hooks).await.unwrap();
    let terminate = tokio::spawn(post(hooks, Hook::Terminate));
    tokio::time::sleep(NOT_SERVED_WITHIN).await;
    assert!(
        !terminate.is_finished(),
        "a third connection was served while two idle ones held both slots"
    );
    drop(first_idle);
    let response = tokio::time::timeout(SERVED_WITHIN, terminate)
        .await
        .expect("the freed slot serves the waiting /terminate")
        .unwrap();
    assert!(response.starts_with("HTTP/1.1 200"), "{response}");
}

#[tokio::test]
async fn a_request_head_that_never_ends_loses_its_slot_at_the_deadline() {
    let (hooks, _shutdown) = start(HookServerSettings {
        max_connections: 1,
        header_read_timeout: SHORT_HEAD_DEADLINE,
    })
    .await;
    let mut slow = TcpStream::connect(hooks).await.unwrap();
    let partial = request(Hook::Ready);
    slow.write_all(&partial.as_bytes()[..partial.len() / 2])
        .await
        .unwrap();
    let ready = tokio::time::timeout(SERVED_WITHIN, post(hooks, Hook::Ready))
        .await
        .expect("the slot of the unfinished head is freed at its deadline");
    assert!(ready.starts_with("HTTP/1.1 "), "{ready}");
    let mut rest = Vec::new();
    let closed = tokio::time::timeout(SERVED_WITHIN, slow.read_to_end(&mut rest)).await;
    assert!(closed.is_ok(), "the unfinished head's connection is closed");
}

#[tokio::test]
async fn a_connection_is_closed_after_its_one_response() {
    let (hooks, _shutdown) = start(HookServerSettings {
        max_connections: 1,
        header_read_timeout: NEVER,
    })
    .await;
    let first = tokio::time::timeout(SERVED_WITHIN, post(hooks, Hook::Ready))
        .await
        .expect("the server closes the connection after the response");
    assert!(
        first.to_ascii_lowercase().contains("connection: close"),
        "{first}"
    );
    let second = tokio::time::timeout(SERVED_WITHIN, post(hooks, Hook::Ready))
        .await
        .expect("the only slot is free again");
    assert!(second.starts_with("HTTP/1.1 "), "{second}");
}
