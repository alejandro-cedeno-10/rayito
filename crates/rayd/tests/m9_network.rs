//! `NetworkService` through the real tonic router (ADR-012, design D11,
//! D18): on an image without `CAP_NET_ADMIN`, `/run` with
//! `network.enforce` still answers 200 and `Health.egress_enforcement` is
//! `NONE`; an enforcing `UpdateNetwork` is `FAILED_PRECONDITION` naming
//! `rayito-base-caps`, an unrestricted one is accepted, malformed entries
//! are `INVALID_ARGUMENT` by list and index, `GetNetwork` never echoes the
//! proxy, both RPCs follow the phase gate, and `Health` carries what the
//! egress manager publishes. Runs on any host.
//!
//! Integration tests are test code, but clippy's `allow-unwrap-in-tests` only
//! recognises `#[test]` functions and `#[cfg(test)]` modules.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;

use axum::Router;
use axum::body::Body;
use base64::Engine;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use http::{Request, StatusCode};
use rayd::adapters::{OsRandomSource, PlatformMetricsProbe, detect_spawn_platform};
use rayd::code::CodeManager;
use rayd::filesystem::{FilesystemSettings, platform_filesystem_manager};
use rayd::grpc::Services;
use rayd::hooks::{HookServices, hook_path};
use rayd::lifecycle::{SuspendSignal, TimeoutWatcher};
use rayd::network::NetworkManager;
use rayd::process::{ManagerSettings, platform_manager, shared_registry};
use rayd::pty::{PtySettings, platform_pty_manager};
use rayd_core::clock::SystemClock;
use rayd_core::filesystem::DenyList;
use rayd_core::lifecycle::Hook;
use rayd_core::network::EgressEnforcement as DomainEnforcement;
use rayd_core::process::{RegistryLimits, UserPolicy};
use rayd_core::session::SandboxSession;
use rayito_proto::v1::health_service_client::HealthServiceClient;
use rayito_proto::v1::network_service_client::NetworkServiceClient;
use rayito_proto::v1::{
    EgressEnforcement, EgressProxy, GetNetworkRequest, HealthRequest, NetworkPolicy, NetworkState,
    UpdateNetworkRequest,
};
use sha2::{Digest, Sha256};
use tokio::net::TcpListener;
use tokio_util::sync::CancellationToken;
use tonic::metadata::MetadataValue;
use tonic::transport::Channel;
use tonic::transport::server::TcpIncoming;
use tonic::{Code, Status};
use tower::ServiceExt;

const SECRET: &[u8] = b"m9-network-secret";

struct Harness {
    channel: Channel,
    hooks: Router,
    session: Arc<SandboxSession>,
    _shutdown: CancellationToken,
}

async fn harness() -> Harness {
    let session = Arc::new(SandboxSession::new(Arc::new(SystemClock::new()), "test"));
    let platform = detect_spawn_platform();
    let files = platform_filesystem_manager(
        session.clone(),
        &platform,
        UserPolicy::default(),
        DenyList::default(),
        FilesystemSettings::default(),
    );
    let registry = shared_registry(RegistryLimits::default());
    let ptys = platform_pty_manager(
        session.clone(),
        &platform,
        UserPolicy::default(),
        registry.clone(),
        PtySettings::default(),
    );
    let processes = platform_manager(
        session.clone(),
        platform,
        UserPolicy::default(),
        registry,
        ManagerSettings::default(),
    );
    let code = CodeManager::disabled(session.clone(), Arc::new(OsRandomSource));
    let suspend = Arc::new(SuspendSignal::new());
    let network = NetworkManager::unavailable(session.clone());
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let shutdown = CancellationToken::new();
    let grpc = rayd::grpc::router(Services {
        session: session.clone(),
        processes,
        ptys,
        files,
        code: code.clone(),
        metrics: Arc::new(PlatformMetricsProbe::default()),
        metrics_history: Arc::new(rayd_core::metrics_history::MetricsHistory::default()),
        suspend: suspend.clone(),
        imds: Arc::new(rayd::adapters::ImdsState::default()),
        persistence: Arc::new(rayd::persistence::UnavailablePersistence),
        timeout: TimeoutWatcher::detached(),
        network: network.clone(),
    })
    .serve_with_incoming_shutdown(
        TcpIncoming::from(listener),
        shutdown.clone().cancelled_owned(),
    );
    tokio::spawn(grpc);
    let channel = Channel::from_shared(format!("http://{addr}"))
        .unwrap()
        .connect()
        .await
        .unwrap();
    let hooks = rayd::hooks::router_with(HookServices {
        session: session.clone(),
        code,
        suspend,
        shutdown: shutdown.clone(),
        imds: Arc::new(rayd::adapters::ImdsState::default()),
        user_probe: None,
        timeout: TimeoutWatcher::detached(),
        network,
    });
    Harness {
        channel,
        hooks,
        session,
        _shutdown: shutdown,
    }
}

impl Harness {
    async fn post(&self, hook: Hook, body: String) -> StatusCode {
        let request = Request::post(hook_path(hook))
            .header("content-type", "application/json")
            .body(Body::from(body))
            .unwrap();
        self.hooks.clone().oneshot(request).await.unwrap().status()
    }

    async fn run_enforcing(&self) -> StatusCode {
        let digest = Sha256::digest(SECRET)
            .iter()
            .fold(String::new(), |mut hex, byte| {
                use std::fmt::Write as _;
                write!(hex, "{byte:02x}").unwrap();
                hex
            });
        let payload =
            format!("{{\"v\":1,\"token_sha256\":\"{digest}\",\"network\":{{\"enforce\":true}}}}");
        let body = serde_json::json!({ "microvmId": "mvm-network", "runHookPayload": payload })
            .to_string();
        self.post(Hook::Run, body).await
    }

    async fn egress_enforcement(&self) -> i32 {
        HealthServiceClient::new(self.channel.clone())
            .health(HealthRequest {})
            .await
            .unwrap()
            .into_inner()
            .egress_enforcement
    }

    async fn update(&self, policy: NetworkPolicy) -> Result<NetworkState, Status> {
        let mut request = tonic::Request::new(UpdateNetworkRequest {
            policy: Some(policy),
        });
        authorize(&mut request);
        NetworkServiceClient::new(self.channel.clone())
            .update_network(request)
            .await
            .map(tonic::Response::into_inner)
    }

    async fn get(&self) -> Result<NetworkState, Status> {
        let mut request = tonic::Request::new(GetNetworkRequest {});
        authorize(&mut request);
        NetworkServiceClient::new(self.channel.clone())
            .get_network(request)
            .await
            .map(tonic::Response::into_inner)
    }
}

fn authorize<T>(request: &mut tonic::Request<T>) {
    let encoded = URL_SAFE_NO_PAD.encode(SECRET);
    request
        .metadata_mut()
        .insert("x-access-token", MetadataValue::try_from(encoded).unwrap());
}

fn policy(allow: &[&str], deny: &[&str]) -> NetworkPolicy {
    NetworkPolicy {
        allow_out: allow.iter().map(|entry| (*entry).to_owned()).collect(),
        deny_out: deny.iter().map(|entry| (*entry).to_owned()).collect(),
        egress_proxy: None,
    }
}

#[tokio::test]
async fn without_net_admin_run_answers_200_and_health_reports_none() {
    let harness = harness().await;
    assert_eq!(
        harness.egress_enforcement().await,
        EgressEnforcement::None as i32,
        "an M9 agent never reports UNSPECIFIED"
    );
    assert_eq!(harness.run_enforcing().await, StatusCode::OK);
    assert_eq!(
        harness.egress_enforcement().await,
        EgressEnforcement::None as i32
    );
    harness
        .session
        .set_egress_enforcement(DomainEnforcement::GuestRoutesAndProxy);
    assert_eq!(
        harness.egress_enforcement().await,
        EgressEnforcement::GuestRoutesAndProxy as i32
    );
}

#[tokio::test]
async fn enforcing_policies_are_a_failed_precondition_without_net_admin() {
    let harness = harness().await;
    harness.run_enforcing().await;
    let denied = harness
        .update(policy(&[], &["0.0.0.0/0"]))
        .await
        .unwrap_err();
    assert_eq!(denied.code(), Code::FailedPrecondition);
    assert!(denied.message().contains("rayito-base-caps"), "{denied:?}");
    let chained = harness
        .update(NetworkPolicy {
            egress_proxy: Some(EgressProxy {
                address: "203.0.113.5:1080".to_owned(),
                username: Some("u-marker".to_owned()),
                password: Some("p-marker".to_owned()),
            }),
            ..policy(&[], &[])
        })
        .await
        .unwrap_err();
    assert_eq!(chained.code(), Code::FailedPrecondition);
    assert!(!chained.message().contains("marker"));
    let open = harness
        .update(policy(&["api.example.com", "10.0.0.0/8"], &[]))
        .await
        .unwrap();
    assert_eq!(open.allow_out, ["api.example.com", "10.0.0.0/8"]);
    assert_eq!(open.enforcement, EgressEnforcement::None as i32);
    assert!(!open.egress_proxy_configured);
    assert_eq!(open.local_proxy_port, 0);
    assert_eq!(harness.get().await.unwrap(), open);
}

#[tokio::test]
async fn malformed_entries_are_invalid_arguments_by_list_and_index() {
    let harness = harness().await;
    harness.run_enforcing().await;
    for (entries, message) in [
        (
            policy(&["10.0.0.0/8", "not a host"], &[]),
            "allow_out[1]: no es un CIDR, una IP ni un nombre de host válido",
        ),
        (
            policy(&[], &["example.com"]),
            "deny_out[0]: los nombres de host no se admiten en deny_out",
        ),
    ] {
        let status = harness.update(entries).await.unwrap_err();
        assert_eq!(status.code(), Code::InvalidArgument);
        assert_eq!(status.message(), message);
    }
    let bad_proxy = harness
        .update(NetworkPolicy {
            egress_proxy: Some(EgressProxy {
                address: "127.0.0.1:1080".to_owned(),
                ..EgressProxy::default()
            }),
            ..policy(&[], &[])
        })
        .await
        .unwrap_err();
    assert_eq!(bad_proxy.code(), Code::InvalidArgument);
    assert!(!bad_proxy.message().contains("127.0.0.1"));
}

#[tokio::test]
async fn both_rpcs_follow_the_phase_gate() {
    let harness = harness().await;
    harness.run_enforcing().await;
    assert!(harness.get().await.is_ok());
    assert_eq!(
        harness.post(Hook::Suspend, String::new()).await,
        StatusCode::OK
    );
    assert_eq!(harness.get().await.unwrap_err().code(), Code::Unavailable);
    assert_eq!(
        harness.update(policy(&[], &[])).await.unwrap_err().code(),
        Code::Unavailable
    );
    assert_eq!(
        harness.post(Hook::Resume, String::new()).await,
        StatusCode::OK
    );
    assert!(harness.update(policy(&[], &[])).await.is_ok());
}
