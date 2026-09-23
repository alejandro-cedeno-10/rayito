//! "hello rayd" in-process: the tonic router on `127.0.0.1:0` and the hooks
//! router driven through `tower::ServiceExt::oneshot`, exercising the
//! access-token rule, the once-per-boot `/run` and the resume generation.
//!
//! Integration tests are test code, but clippy's `allow-unwrap-in-tests` only
//! recognises `#[test]` functions and `#[cfg(test)]` modules.
#![allow(clippy::unwrap_used, clippy::expect_used)]

#[path = "common/log_capture.rs"]
mod log_capture;

use std::sync::Arc;

use axum::Router;
use axum::body::Body;
use base64::Engine;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use http::{Request, StatusCode};
use log_capture::log_capture;
use rayd::adapters::{OsRandomSource, PlatformMetricsProbe, detect_spawn_platform};
use rayd::code::CodeManager;
use rayd::filesystem::{FilesystemSettings, platform_filesystem_manager};
use rayd::grpc::Services;
use rayd::hooks::{HookReply, hook_path};
use rayd::lifecycle::SuspendSignal;
use rayd::process::{ManagerSettings, platform_manager, shared_registry};
use rayd::pty::{PtySettings, platform_pty_manager};
use rayd_core::clock::SystemClock;
use rayd_core::filesystem::DenyList;
use rayd_core::lifecycle::Hook;
use rayd_core::process::{RegistryLimits, UserPolicy};
use rayd_core::session::SandboxSession;
use rayito_proto::v1::filesystem_service_client::FilesystemServiceClient;
use rayito_proto::v1::health_service_client::HealthServiceClient;
use rayito_proto::v1::lifecycle_service_client::LifecycleServiceClient;
use rayito_proto::v1::network_service_client::NetworkServiceClient;
use rayito_proto::v1::process_service_client::ProcessServiceClient;
use rayito_proto::v1::pty_service_client::PtyServiceClient;
use rayito_proto::v1::{
    GetNetworkRequest, GetTransferRequest, HealthRequest, ListRequest, ListResponse,
    MetricsHistoryRequest, MetricsRequest, ResizeRequest, SetTimeoutRequest, UpdateNetworkRequest,
};
use sha2::{Digest, Sha256};
use tokio::net::TcpListener;
use tokio_util::sync::CancellationToken;
use tonic::metadata::MetadataValue;
use tonic::transport::Channel;
use tonic::transport::server::TcpIncoming;
use tonic::{Code, Status};
use tower::ServiceExt;

const SECRET: &[u8] = b"m1-sandbox-secret";
const OTHER_SECRET: &[u8] = b"someone-else";

struct Harness {
    channel: Channel,
    hooks: Router,
    shutdown: CancellationToken,
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
        timeout: rayd::lifecycle::TimeoutWatcher::detached(),
        network: rayd::network::NetworkManager::unavailable(session.clone()),
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
    Harness {
        channel,
        hooks: rayd::hooks::router(session, code, suspend, shutdown.clone()),
        shutdown,
    }
}

impl Harness {
    async fn post(&self, hook: Hook, body: Option<String>) -> (StatusCode, HookReply) {
        let request = Request::post(hook_path(hook))
            .header("content-type", "application/json")
            .body(body.map_or_else(Body::empty, Body::from))
            .unwrap();
        let response = self.hooks.clone().oneshot(request).await.unwrap();
        let status = response.status();
        let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap();
        (status, serde_json::from_slice(&bytes).unwrap())
    }

    async fn run_with_secret(&self, secret: &[u8]) -> (StatusCode, HookReply) {
        self.post(Hook::Run, Some(run_envelope("mvm-test-1", secret)))
            .await
    }

    async fn health(&self) -> rayito_proto::v1::HealthResponse {
        self.health_with_token(None).await
    }

    async fn health_with_token(&self, raw_token: Option<&str>) -> rayito_proto::v1::HealthResponse {
        let mut request = tonic::Request::new(HealthRequest {});
        if let Some(raw_token) = raw_token {
            request.metadata_mut().insert(
                "x-access-token",
                MetadataValue::try_from(raw_token).unwrap(),
            );
        }
        HealthServiceClient::new(self.channel.clone())
            .health(request)
            .await
            .unwrap()
            .into_inner()
    }

    async fn list_processes(&self, secret: Option<&[u8]>) -> Result<ListResponse, Status> {
        let mut request = tonic::Request::new(ListRequest {});
        attach_secret(&mut request, secret);
        ProcessServiceClient::new(self.channel.clone())
            .list(request)
            .await
            .map(tonic::Response::into_inner)
    }

    async fn resize_pty(&self, secret: Option<&[u8]>) -> Status {
        let mut request = tonic::Request::new(ResizeRequest::default());
        attach_secret(&mut request, secret);
        PtyServiceClient::new(self.channel.clone())
            .resize(request)
            .await
            .expect_err("PtyService.Resize on an unknown pid is refused")
    }
}

fn attach_secret<T>(request: &mut tonic::Request<T>, secret: Option<&[u8]>) {
    if let Some(secret) = secret {
        let encoded = URL_SAFE_NO_PAD.encode(secret);
        request
            .metadata_mut()
            .insert("x-access-token", MetadataValue::try_from(encoded).unwrap());
    }
}

fn run_envelope(microvm_id: &str, secret: &[u8]) -> String {
    use std::fmt::Write as _;
    let digest = Sha256::digest(secret)
        .iter()
        .fold(String::new(), |mut hex, byte| {
            write!(hex, "{byte:02x}").unwrap();
            hex
        });
    let payload = format!(
        "{{\"v\":1,\"token_sha256\":\"{digest}\",\"user\":\"user\",\"workdir\":\"/home/user\"}}"
    );
    serde_json::json!({ "microvmId": microvm_id, "runHookPayload": payload }).to_string()
}

#[tokio::test]
async fn health_answers_without_a_token() {
    let harness = harness().await;
    let health = harness.health().await;
    assert!(health.agent_ready);
    assert!(!health.kernel_ready);
    assert_eq!(health.agent_version, "test");
    assert_eq!(health.resume_generation, 0);
    assert_eq!(health.sandbox_id, "");
}

#[tokio::test]
async fn health_ignores_a_garbage_token() {
    let harness = harness().await;
    assert!(
        harness
            .health_with_token(Some("not*base64!"))
            .await
            .agent_ready
    );
}

#[tokio::test]
async fn health_ignores_a_wrong_token() {
    let harness = harness().await;
    harness.run_with_secret(SECRET).await;
    let wrong = URL_SAFE_NO_PAD.encode(OTHER_SECRET);
    let health = harness.health_with_token(Some(&wrong)).await;
    assert!(health.agent_ready);
    assert_eq!(health.sandbox_id, "mvm-test-1");
}

#[tokio::test]
async fn every_other_rpc_is_unauthenticated_without_a_token() {
    let harness = harness().await;
    assert_eq!(
        harness.list_processes(None).await.unwrap_err().code(),
        Code::Unauthenticated
    );
    assert_eq!(
        harness
            .list_processes(Some(SECRET))
            .await
            .unwrap_err()
            .code(),
        Code::Unauthenticated
    );
    let metrics = HealthServiceClient::new(harness.channel.clone())
        .metrics(MetricsRequest {})
        .await
        .expect_err("Metrics requires a token");
    assert_eq!(metrics.code(), Code::Unauthenticated);
}

#[tokio::test]
async fn lifecycle_network_transfer_and_history_rpcs_require_a_token() {
    let harness = harness().await;
    harness.run_with_secret(SECRET).await;
    let channel = harness.channel.clone();
    let set_timeout = LifecycleServiceClient::new(channel.clone())
        .set_timeout(SetTimeoutRequest::default())
        .await
        .expect_err("SetTimeout requires a token");
    let update_network = NetworkServiceClient::new(channel.clone())
        .update_network(UpdateNetworkRequest::default())
        .await
        .expect_err("UpdateNetwork requires a token");
    let get_network = NetworkServiceClient::new(channel.clone())
        .get_network(GetNetworkRequest {})
        .await
        .expect_err("GetNetwork requires a token");
    let get_transfer = FilesystemServiceClient::new(channel.clone())
        .get_transfer(GetTransferRequest::default())
        .await
        .expect_err("GetTransfer requires a token");
    let history = HealthServiceClient::new(channel)
        .metrics_history(MetricsHistoryRequest::default())
        .await
        .expect_err("MetricsHistory requires a token");
    for status in [
        set_timeout,
        update_network,
        get_network,
        get_transfer,
        history,
    ] {
        assert_eq!(status.code(), Code::Unauthenticated);
    }
}

#[tokio::test]
async fn run_installs_the_token_and_pending_rpcs_answer_unimplemented() {
    let harness = harness().await;
    let (status, reply) = harness.run_with_secret(SECRET).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(reply.outcome, "installed");
    assert_eq!(reply.phase, "running");
    assert!(
        harness
            .list_processes(Some(SECRET))
            .await
            .unwrap()
            .processes
            .is_empty()
    );
    assert_eq!(
        harness.resize_pty(Some(SECRET)).await.code(),
        Code::NotFound
    );
    assert_eq!(
        harness.list_processes(None).await.unwrap_err().code(),
        Code::Unauthenticated
    );
    assert_eq!(
        harness
            .list_processes(Some(OTHER_SECRET))
            .await
            .unwrap_err()
            .code(),
        Code::Unauthenticated
    );
    assert_eq!(harness.health().await.sandbox_id, "mvm-test-1");
}

#[tokio::test]
async fn run_twice_keeps_the_first_token() {
    let harness = harness().await;
    harness.run_with_secret(SECRET).await;
    let (status, reply) = harness.run_with_secret(OTHER_SECRET).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(reply.outcome, "already_ran");
    assert!(harness.list_processes(Some(SECRET)).await.is_ok());
    assert_eq!(
        harness
            .list_processes(Some(OTHER_SECRET))
            .await
            .unwrap_err()
            .code(),
        Code::Unauthenticated
    );
}

#[tokio::test]
async fn run_with_a_bad_payload_still_answers_200_but_stays_tokenless() {
    let harness = harness().await;
    let body =
        serde_json::json!({ "microvmId": "mvm-x", "runHookPayload": "{\"v\":9}" }).to_string();
    let (status, reply) = harness.post(Hook::Run, Some(body)).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(reply.outcome, "tokenless");
    assert_eq!(
        harness
            .list_processes(Some(SECRET))
            .await
            .unwrap_err()
            .code(),
        Code::Unauthenticated
    );
    assert!(harness.health().await.agent_ready);
}

#[tokio::test]
async fn run_with_a_malformed_body_answers_200() {
    let harness = harness().await;
    let (status, reply) = harness.post(Hook::Run, Some("not json".to_owned())).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(reply.outcome, "tokenless");
}

#[tokio::test]
async fn ready_and_validate_answer_200() {
    let harness = harness().await;
    let (status, reply) = harness.post(Hook::Ready, None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(reply.phase, "ready");
    let (status, reply) = harness.post(Hook::Validate, None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(reply.outcome, "validate_skipped");
    assert_eq!(reply.phase, "ready");
}

#[tokio::test]
async fn suspend_then_resume_increments_resume_generation_in_health() {
    let harness = harness().await;
    harness.post(Hook::Ready, None).await;
    harness.run_with_secret(SECRET).await;
    let (status, reply) = harness.post(Hook::Suspend, None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(reply.phase, "suspending");
    assert_eq!(reply.suspend_generation, 1);
    let (_, repeated) = harness.post(Hook::Suspend, None).await;
    assert_eq!(repeated.outcome, "unchanged");
    assert_eq!(repeated.suspend_generation, 1);
    let (status, reply) = harness.post(Hook::Resume, None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(reply.phase, "resumed");
    assert_eq!(reply.resume_generation, 1);
    assert_eq!(harness.health().await.resume_generation, 1);
    let (_, second) = harness.post(Hook::Suspend, None).await;
    assert_eq!(
        second.outcome, "changed",
        "a second cycle right behind the first"
    );
    harness.post(Hook::Resume, None).await;
    assert_eq!(harness.health().await.resume_generation, 2);
    assert_eq!(harness.health().await.hook_anomalies, 0);
    assert!(harness.list_processes(Some(SECRET)).await.is_ok());
}

#[tokio::test]
async fn suspend_before_run_is_still_a_200() {
    let harness = harness().await;
    let (status, reply) = harness.post(Hook::Suspend, None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(reply.outcome, "illegal");
    assert_eq!(reply.phase, "booting");
}

#[tokio::test]
async fn terminate_answers_200_and_requests_shutdown() {
    let harness = harness().await;
    let (status, reply) = harness.post(Hook::Terminate, None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(reply.outcome, "terminating");
    tokio::time::timeout(
        std::time::Duration::from_secs(2),
        harness.shutdown.cancelled(),
    )
    .await
    .expect("terminate cancels the shutdown token");
}

fn run_envelope_with_metadata(microvm_id: &str, secret: &[u8], metadata: &str) -> String {
    let base = run_envelope(microvm_id, secret);
    let mut envelope: serde_json::Value = serde_json::from_str(&base).unwrap();
    let payload = envelope["runHookPayload"].as_str().unwrap();
    let mut payload: serde_json::Value = serde_json::from_str(payload).unwrap();
    payload["metadata"] = serde_json::from_str(metadata).unwrap();
    envelope["runHookPayload"] = serde_json::Value::String(payload.to_string());
    envelope.to_string()
}

/// The echo and the logging allowlist in one run: `Health` carries the
/// map back while the `/run` line reports only `metadata_keys` as a count
/// and no log line ever quotes a key or a value.
#[tokio::test]
async fn health_echoes_the_run_payload_metadata_and_logs_only_the_count() {
    let capture = log_capture();
    let harness = harness().await;
    assert!(harness.health().await.metadata.is_empty());
    let envelope = run_envelope_with_metadata(
        "mvm-test-1",
        SECRET,
        "{\"mdkey-zz\":\"mdval-qq\",\"mdkey-yy\":\"mdval-pp\"}",
    );
    let (status, reply) = harness.post(Hook::Run, Some(envelope)).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(reply.outcome, "installed");
    let health = harness.health().await;
    assert_eq!(health.metadata.len(), 2);
    assert_eq!(
        health.metadata.get("mdkey-zz").map(String::as_str),
        Some("mdval-qq")
    );
    let other =
        run_envelope_with_metadata("mvm-test-1", OTHER_SECRET, "{\"mdkey-xx\":\"mdval-oo\"}");
    let (_, again) = harness.post(Hook::Run, Some(other)).await;
    assert_eq!(again.outcome, "already_ran");
    assert!(!harness.health().await.metadata.contains_key("mdkey-xx"));
    let text = capture.text();
    assert!(
        text.contains("\"metadata_keys\":2"),
        "the /run line reports the count: {text}"
    );
    for secret in [
        "mdkey-zz", "mdval-qq", "mdkey-yy", "mdval-pp", "mdkey-xx", "mdval-oo",
    ] {
        assert!(!text.contains(secret), "{secret} leaked into the log");
    }
}
