//! `/run` over a real connection, the way `main` serves the hooks
//! (`into_make_service_with_connect_info::<HookPeer>()`), with a scripted
//! `SocketOwners` so the test does not depend on which uid runs it: the
//! RAYD-08 race, where a sandbox process (a template's `start_cmd`, thawed
//! with the snapshot before the platform's `/run` arrives) posts a
//! well-formed `/run` first.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::HashSet;
use std::future::IntoFuture;
use std::net::SocketAddr;
use std::sync::{Arc, Mutex};

use rayd::adapters::{ImdsState, OsRandomSource};
use rayd::code::CodeManager;
use rayd::hooks::{HookPeer, HookReply, HookServices, hook_path};
use rayd::lifecycle::{SuspendSignal, TimeoutWatcher};
use rayd::network::NetworkManager;
use rayd_core::clock::SystemClock;
use rayd_core::hook_origin::SocketOwners;
use rayd_core::lifecycle::Hook;
use rayd_core::session::SandboxSession;
use sha2::{Digest, Sha256};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpSocket};
use tokio_util::sync::CancellationToken;

const GENUINE_SECRET: &[u8] = b"platform-secret";
const FORGED_SECRET: &[u8] = b"start-cmd-secret";
const PROTECTED_RPC: &str = "/rayito.v1.ProcessService/List";
/// The image's sandbox user and one of the platform agent's uids (Q48).
const SANDBOX_UID: u32 = 1000;
const PLATFORM_UID: u32 = 993;

/// Answers `SANDBOX_UID` for the client ports registered as sandbox
/// processes and `PLATFORM_UID` for every other one, and records the
/// listener end it was asked about.
#[derive(Default)]
struct ScriptedOwners {
    sandbox_ports: Mutex<HashSet<u16>>,
    listener_ends: Mutex<Vec<SocketAddr>>,
}

impl SocketOwners for ScriptedOwners {
    fn owner_uid(&self, peer: SocketAddr, local: SocketAddr) -> Option<u32> {
        self.listener_ends.lock().unwrap().push(local);
        if self.sandbox_ports.lock().unwrap().contains(&peer.port()) {
            Some(SANDBOX_UID)
        } else {
            Some(PLATFORM_UID)
        }
    }
}

fn run_envelope(secret: &[u8]) -> String {
    use std::fmt::Write as _;
    let digest = Sha256::digest(secret)
        .iter()
        .fold(String::new(), |mut hex, byte| {
            write!(hex, "{byte:02x}").unwrap();
            hex
        });
    let payload = format!("{{\"v\":1,\"token_sha256\":\"{digest}\"}}");
    serde_json::json!({ "microvmId": "mvm-origin", "runHookPayload": payload }).to_string()
}

/// A raw HTTP/1.1 `/run` from a socket bound before it connects, so the
/// caller's port is known (and registered) before the hook sees it.
async fn post_run(
    hooks: SocketAddr,
    owners: &ScriptedOwners,
    sandbox: bool,
    body: &str,
) -> HookReply {
    let socket = TcpSocket::new_v4().unwrap();
    socket.bind("127.0.0.1:0".parse().unwrap()).unwrap();
    if sandbox {
        owners
            .sandbox_ports
            .lock()
            .unwrap()
            .insert(socket.local_addr().unwrap().port());
    }
    let mut stream = socket.connect(hooks).await.unwrap();
    let request = format!(
        "POST {} HTTP/1.1\r\nHost: localhost:9000\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        hook_path(Hook::Run),
        body.len()
    );
    stream.write_all(request.as_bytes()).await.unwrap();
    let mut response = String::new();
    stream.read_to_string(&mut response).await.unwrap();
    assert!(response.starts_with("HTTP/1.1 200"), "{response}");
    let (_, json) = response.split_once("\r\n\r\n").unwrap();
    serde_json::from_str(json).unwrap()
}

#[tokio::test]
async fn a_run_from_a_sandbox_socket_never_claims_the_boot_and_the_genuine_run_still_installs() {
    let session = Arc::new(SandboxSession::new(Arc::new(SystemClock::new()), "test"));
    let code = CodeManager::disabled(session.clone(), Arc::new(OsRandomSource));
    let owners = Arc::new(ScriptedOwners::default());
    let shutdown = CancellationToken::new();
    let router = rayd::hooks::router_with(HookServices {
        session: session.clone(),
        code,
        suspend: Arc::new(SuspendSignal::new()),
        shutdown: shutdown.clone(),
        imds: Arc::new(ImdsState::default()),
        user_probe: None,
        timeout: TimeoutWatcher::detached(),
        network: NetworkManager::unavailable(session.clone()),
        participants: Vec::new(),
        socket_owners: owners.clone(),
    });
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let hooks = listener.local_addr().unwrap();
    tokio::spawn(
        axum::serve(
            listener,
            router.into_make_service_with_connect_info::<HookPeer>(),
        )
        .with_graceful_shutdown(shutdown.clone().cancelled_owned())
        .into_future(),
    );

    let forged = post_run(hooks, &owners, true, &run_envelope(FORGED_SECRET)).await;
    assert_eq!(forged.outcome, "sandbox_origin");
    assert_eq!(session.hook_anomalies(), 1);
    assert!(
        session
            .authorize(PROTECTED_RPC, Some(FORGED_SECRET))
            .is_err()
    );

    let genuine = post_run(hooks, &owners, false, &run_envelope(GENUINE_SECRET)).await;
    assert_eq!(genuine.outcome, "installed");
    assert_eq!(
        session.authorize(PROTECTED_RPC, Some(GENUINE_SECRET)),
        Ok(())
    );

    let late = post_run(hooks, &owners, true, &run_envelope(FORGED_SECRET)).await;
    assert_eq!(late.outcome, "sandbox_origin");
    assert_eq!(
        session.authorize(PROTECTED_RPC, Some(GENUINE_SECRET)),
        Ok(())
    );
    assert!(
        owners
            .listener_ends
            .lock()
            .unwrap()
            .iter()
            .all(|local| *local == hooks),
        "the owner lookup gets the listener's own end of each connection"
    );
    shutdown.cancel();
}
