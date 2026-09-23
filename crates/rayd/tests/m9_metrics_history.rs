//! `HealthService.MetricsHistory` and the guest facts of `Health` against
//! the in-process tonic router (design D3, D13). No sampler runs here: the
//! tests seed the ring with `record`, so they are deterministic and need no
//! sidecar, which keeps them on every host (like `m1_hello.rs`). Only the
//! `Metrics` snapshot check needs procfs and is Linux-only.
//!
//! Integration tests are test code, but clippy's `allow-unwrap-in-tests` only
//! recognises `#[test]` functions and `#[cfg(test)]` modules.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;

use base64::Engine;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use rayd::adapters::{ImdsState, OsRandomSource, PlatformMetricsProbe, detect_spawn_platform};
use rayd::code::CodeManager;
use rayd::filesystem::{FilesystemSettings, platform_filesystem_manager};
use rayd::grpc::Services;
use rayd::lifecycle::SuspendSignal;
use rayd::persistence::UnavailablePersistence;
use rayd::process::{ManagerSettings, platform_manager, shared_registry};
use rayd::pty::{PtySettings, platform_pty_manager};
use rayd_core::clock::SystemClock;
use rayd_core::filesystem::DenyList;
use rayd_core::metrics::MetricsProbe;
use rayd_core::metrics_history::{MetricsHistory, MetricsSample};
use rayd_core::process::{RegistryLimits, UserPolicy};
use rayd_core::session::{RunHookInput, RunOutcome, SandboxSession};
use rayito_proto::v1::health_service_client::HealthServiceClient;
use rayito_proto::v1::{
    HealthRequest, MetricsHistoryRequest, MetricsHistoryResponse, MetricsResponse,
};
use sha2::{Digest, Sha256};
use tokio::net::TcpListener;
use tokio_util::sync::CancellationToken;
use tonic::metadata::MetadataValue;
use tonic::transport::Channel;
use tonic::transport::server::TcpIncoming;
use tonic::{Code, Status};

const SECRET: &[u8] = b"m9-history-secret";
const OTHER_SECRET: &[u8] = b"someone-else";

struct Harness {
    health: HealthServiceClient<Channel>,
    history: Arc<MetricsHistory>,
    _shutdown: CancellationToken,
}

/// The router after an accepted `/run`, so the access token is installed.
async fn running_harness() -> Harness {
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
    let history = Arc::new(MetricsHistory::default());
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let shutdown = CancellationToken::new();
    let grpc = rayd::grpc::router(Services {
        session: session.clone(),
        processes,
        ptys,
        files,
        code: CodeManager::disabled(session.clone(), Arc::new(OsRandomSource)),
        metrics: Arc::new(PlatformMetricsProbe::default()),
        metrics_history: history.clone(),
        suspend: Arc::new(SuspendSignal::new()),
        imds: Arc::new(ImdsState::default()),
        persistence: Arc::new(UnavailablePersistence),
        timeout: rayd::lifecycle::TimeoutWatcher::detached(),
        network: rayd::network::NetworkManager::unavailable(session.clone()),
    })
    .serve_with_incoming_shutdown(
        TcpIncoming::from(listener),
        shutdown.clone().cancelled_owned(),
    );
    tokio::spawn(grpc);
    let payload = format!("{{\"v\":1,\"token_sha256\":\"{}\"}}", digest_hex(SECRET));
    let outcome = session.run(RunHookInput {
        sandbox_id: Some("mvm-m9"),
        payload: Some(&payload),
    });
    assert_eq!(outcome, RunOutcome::Installed);
    let channel = Channel::from_shared(format!("http://{addr}"))
        .unwrap()
        .connect()
        .await
        .unwrap();
    Harness {
        health: HealthServiceClient::new(channel),
        history,
        _shutdown: shutdown,
    }
}

impl Harness {
    async fn history_rpc(
        &self,
        request: MetricsHistoryRequest,
    ) -> Result<MetricsHistoryResponse, Status> {
        self.history_with(request, Some(SECRET)).await
    }

    async fn history_with(
        &self,
        request: MetricsHistoryRequest,
        secret: Option<&[u8]>,
    ) -> Result<MetricsHistoryResponse, Status> {
        let mut request = tonic::Request::new(request);
        if let Some(secret) = secret {
            let encoded = URL_SAFE_NO_PAD.encode(secret);
            request
                .metadata_mut()
                .insert("x-access-token", MetadataValue::try_from(encoded).unwrap());
        }
        self.health
            .clone()
            .metrics_history(request)
            .await
            .map(tonic::Response::into_inner)
    }

    /// Ten samples one second apart (1 000 … 10 000 ms) whose CPU is 10 × the
    /// second, so bucket means are easy to check.
    fn seed_ten_seconds(&self) {
        for second in 1..=10_u32 {
            self.history.record(sample(second));
        }
    }
}

fn sample(second: u32) -> MetricsSample {
    MetricsSample {
        unix_ms: i64::from(second) * 1_000,
        cpu_used_pct: f64::from(second * 10),
        mem_used: 1_000 + u64::from(second),
        mem_total: 8_000_000,
        mem_cache: 42_000,
        disk_used: 10,
        disk_total: 100,
        cpu_count: 2,
    }
}

fn range(start_unix_ms: i64, end_unix_ms: i64, max_points: u32) -> MetricsHistoryRequest {
    MetricsHistoryRequest {
        start_unix_ms,
        end_unix_ms,
        max_points,
    }
}

fn stamps(response: &MetricsHistoryResponse) -> Vec<i64> {
    response
        .samples
        .iter()
        .map(|sample| sample.timestamp_unix_ms)
        .collect()
}

fn cpu(response: &MetricsHistoryResponse) -> Vec<f64> {
    response
        .samples
        .iter()
        .map(|sample| sample.cpu_used_pct)
        .collect()
}

fn digest_hex(secret: &[u8]) -> String {
    use std::fmt::Write as _;
    Sha256::digest(secret)
        .iter()
        .fold(String::new(), |mut hex, byte| {
            write!(hex, "{byte:02x}").unwrap();
            hex
        })
}

#[tokio::test]
async fn metrics_history_requires_the_access_token() {
    let harness = running_harness().await;
    let missing = harness
        .history_with(MetricsHistoryRequest::default(), None)
        .await
        .expect_err("MetricsHistory without a token");
    assert_eq!(missing.code(), Code::Unauthenticated);
    let wrong = harness
        .history_with(MetricsHistoryRequest::default(), Some(OTHER_SECRET))
        .await
        .expect_err("MetricsHistory with another sandbox's token");
    assert_eq!(wrong.code(), Code::Unauthenticated);
    let granted = harness
        .history_rpc(MetricsHistoryRequest::default())
        .await
        .unwrap();
    assert!(granted.samples.is_empty());
}

#[tokio::test]
async fn metrics_history_filters_the_range_and_downsamples() {
    let harness = running_harness().await;
    harness.seed_ten_seconds();
    let window = harness.history_rpc(range(3_000, 7_000, 0)).await.unwrap();
    assert_eq!(stamps(&window), [3_000, 4_000, 5_000, 6_000, 7_000]);
    assert_eq!(
        window.samples.first(),
        Some(&MetricsResponse {
            cpu_used_pct: 30.0,
            mem_used_bytes: 1_003,
            mem_total_bytes: 8_000_000,
            disk_used_bytes: 10,
            disk_total_bytes: 100,
            cpu_count: 2,
            timestamp_unix_ms: 3_000,
            mem_cache_bytes: 42_000,
        })
    );
    let reduced = harness.history_rpc(range(0, 0, 3)).await.unwrap();
    assert_eq!(stamps(&reduced), [3_000, 6_000, 10_000]);
    assert_eq!(cpu(&reduced), [20.0, 50.0, 85.0]);
    let reduced_window = harness.history_rpc(range(2_000, 9_000, 2)).await.unwrap();
    assert_eq!(stamps(&reduced_window), [5_000, 9_000]);
    assert_eq!(cpu(&reduced_window), [35.0, 75.0]);
    let above_the_selection = harness.history_rpc(range(9_000, 0, 50)).await.unwrap();
    assert_eq!(stamps(&above_the_selection), [9_000, 10_000]);
}

#[tokio::test]
async fn metrics_history_rejects_an_inverted_range() {
    let harness = running_harness().await;
    harness.seed_ten_seconds();
    let inverted = harness
        .history_rpc(range(5_000, 1_000, 0))
        .await
        .expect_err("start after end");
    assert_eq!(inverted.code(), Code::InvalidArgument);
    let negative = harness
        .history_rpc(range(-1, 0, 0))
        .await
        .expect_err("negative start");
    assert_eq!(negative.code(), Code::InvalidArgument);
    let open_end = harness.history_rpc(range(9_000, 0, 0)).await.unwrap();
    assert_eq!(stamps(&open_end), [9_000, 10_000]);
}

#[tokio::test]
async fn metrics_history_reports_the_oldest_sample_and_empty_ring() {
    let harness = running_harness().await;
    let empty = harness
        .history_rpc(MetricsHistoryRequest::default())
        .await
        .unwrap();
    assert!(empty.samples.is_empty());
    assert_eq!(empty.oldest_unix_ms, 0);
    harness.seed_ten_seconds();
    let late = harness.history_rpc(range(8_000, 0, 0)).await.unwrap();
    assert_eq!(stamps(&late), [8_000, 9_000, 10_000]);
    assert_eq!(late.oldest_unix_ms, 1_000);
}

/// `memory_total_bytes` comes from `/proc/meminfo`, so only Linux can
/// require it; everywhere the anonymous `Health` carries the probe's CPUs.
#[tokio::test]
async fn health_reports_guest_cpu_and_memory() {
    let harness = running_harness().await;
    let health = harness
        .health
        .clone()
        .health(HealthRequest {})
        .await
        .unwrap()
        .into_inner();
    assert!(health.cpu_count >= 1);
    assert_eq!(
        health.cpu_count,
        PlatformMetricsProbe::default().cpu_count()
    );
    if cfg!(target_os = "linux") {
        assert!(health.memory_total_bytes > 0);
    }
}

#[cfg(target_os = "linux")]
#[tokio::test]
async fn metrics_snapshot_carries_mem_cache() {
    use rayito_proto::v1::MetricsRequest;

    let harness = running_harness().await;
    let mut request = tonic::Request::new(MetricsRequest {});
    request.metadata_mut().insert(
        "x-access-token",
        MetadataValue::try_from(URL_SAFE_NO_PAD.encode(SECRET)).unwrap(),
    );
    let metrics = harness
        .health
        .clone()
        .metrics(request)
        .await
        .unwrap()
        .into_inner();
    assert!(metrics.mem_cache_bytes > 0);
    assert!(metrics.mem_cache_bytes <= metrics.mem_total_bytes);
}
