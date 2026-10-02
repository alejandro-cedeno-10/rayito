//! `HealthService`: the anonymous readiness probe (with `kernel_ready` and
//! `kernel_state_lost` read from the `KernelStatus` port, `imds_blocked`
//! from the IMDS block state and `hook_anomalies` from the session) and
//! `Metrics`, which samples the CPU twice 100 ms apart through the
//! `MetricsProbe` port, plus the guest's CPUs and `MemTotal` on `Health`.
//! `MetricsHistory` serves the 5 s ring the sampler feeds; it is not
//! phase-gated because it only reads memory. `lifecycle` is always set
//! (phase `UNMANAGED` without a lifecycle block, ADR-011);
//! `egress_enforcement` is what the egress manager last verified and
//! published through the session (ADR-012).

use std::sync::Arc;
use std::time::Duration;

use rayd_core::code::KernelStatus;
use rayd_core::health::HealthSnapshot;
use rayd_core::metrics::{MetricsError, MetricsProbe, MetricsSnapshot, snapshot, unix_millis};
use rayd_core::metrics_history::{HistoryPage, MetricsHistory, MetricsSample, RangeQuery};
use rayd_core::session::SandboxSession;
use rayito_proto::v1::health_service_server::HealthService;
use rayito_proto::v1::{
    HealthRequest, HealthResponse, MetricsHistoryRequest, MetricsHistoryResponse, MetricsRequest,
    MetricsResponse,
};
use tonic::{Request, Response, Status};

use super::lifecycle::lifecycle_state;
use crate::adapters::ImdsState;
use crate::features::FeatureSet;

pub const CPU_SAMPLE_INTERVAL: Duration = Duration::from_millis(100);

pub struct HealthGrpc {
    session: Arc<SandboxSession>,
    probe: Arc<dyn MetricsProbe>,
    history: Arc<MetricsHistory>,
    kernel: Arc<dyn KernelStatus>,
    imds: Arc<ImdsState>,
    features: Arc<FeatureSet>,
}

impl HealthGrpc {
    pub fn new(
        session: Arc<SandboxSession>,
        probe: Arc<dyn MetricsProbe>,
        history: Arc<MetricsHistory>,
        kernel: Arc<dyn KernelStatus>,
        imds: Arc<ImdsState>,
        features: Arc<FeatureSet>,
    ) -> Self {
        Self {
            session,
            probe,
            history,
            kernel,
            imds,
            features,
        }
    }
}

#[tonic::async_trait]
impl HealthService for HealthGrpc {
    async fn health(
        &self,
        _request: Request<HealthRequest>,
    ) -> Result<Response<HealthResponse>, Status> {
        let mut snapshot = self.session.health();
        snapshot.kernel_ready = self.kernel.kernel_ready();
        snapshot.kernel_state_lost = self.kernel.kernel_state_lost();
        snapshot.imds_blocked = self.imds.blocked();
        snapshot.cpu_count = self.probe.cpu_count();
        snapshot.memory_total_bytes = self.probe.memory().map_or(0, |memory| memory.total);
        Ok(Response::new(to_response(snapshot, &self.features)))
    }

    async fn metrics(
        &self,
        _request: Request<MetricsRequest>,
    ) -> Result<Response<MetricsResponse>, Status> {
        let first = self
            .probe
            .cpu_times()
            .map_err(|error| metrics_status(&error))?;
        tokio::time::sleep(CPU_SAMPLE_INTERVAL).await;
        let second = self
            .probe
            .cpu_times()
            .map_err(|error| metrics_status(&error))?;
        let memory = self
            .probe
            .memory()
            .map_err(|error| metrics_status(&error))?;
        let disk = self
            .probe
            .disk_root()
            .map_err(|error| metrics_status(&error))?;
        let wall = self.session.clock().wall();
        let metrics = snapshot(first, second, memory, disk, self.probe.cpu_count(), wall);
        Ok(Response::new(to_metrics_response(&metrics)))
    }

    async fn metrics_history(
        &self,
        request: Request<MetricsHistoryRequest>,
    ) -> Result<Response<MetricsHistoryResponse>, Status> {
        let wire = request.into_inner();
        let query = RangeQuery::from_wire(wire.start_unix_ms, wire.end_unix_ms, wire.max_points)
            .map_err(|error| Status::invalid_argument(error.to_string()))?;
        let page = self.history.query(&query);
        tracing::debug!(
            rpc = "MetricsHistory",
            samples = page.samples.len(),
            "metrics history served"
        );
        Ok(Response::new(to_history_response(&page)))
    }
}

fn to_response(snapshot: HealthSnapshot, features: &FeatureSet) -> HealthResponse {
    HealthResponse {
        agent_ready: snapshot.agent_ready,
        kernel_ready: snapshot.kernel_ready,
        agent_version: snapshot.agent_version,
        uptime_ms: u64::try_from(snapshot.uptime.as_millis()).unwrap_or(u64::MAX),
        sandbox_id: snapshot.sandbox_id.unwrap_or_default(),
        resume_generation: snapshot.resume_generation,
        clock_offset_ms: snapshot.clock_offset_ms,
        kernel_state_lost: snapshot.kernel_state_lost,
        imds_blocked: snapshot.imds_blocked,
        hook_anomalies: snapshot.hook_anomalies,
        metadata: snapshot.metadata.into_iter().collect(),
        lifecycle: Some(lifecycle_state(&snapshot.lifecycle)),
        egress_enforcement: i32::from(super::network::proto_enforcement(
            snapshot.egress_enforcement,
        )),
        cpu_count: snapshot.cpu_count,
        memory_total_bytes: snapshot.memory_total_bytes,
        // Read live from the same `FeatureSet` `ConfigureGrpc` dispatches
        // to, never a literal: `m15-s3-mounts`'s own `supported()` reflects
        // whether *this* image variant actually has the binary, the
        // device and the mount user (`features::s3_mounts::build`), so a
        // `rayito-base` agent reports `false` here even though the slot
        // itself is the same build.
        features: Some(agent_features_message(features.s3_mounts.supported())),
    }
}

fn agent_features_message(s3_mounts_supported: bool) -> rayito_proto::v1::AgentFeatures {
    let features = rayd_core::features::AgentFeatures {
        s3_mounts: s3_mounts_supported,
        ..rayd_core::features::AgentFeatures::foundations_only()
    };
    let mut root_egress = Vec::new();
    if s3_mounts_supported {
        // `mount-s3` reads the execution role from IMDS as root before
        // dropping to the dedicated mount user (ADR-017); every other
        // feature's root-egress class is still unreported until it has a
        // real adapter.
        root_egress.push(i32::from(rayito_proto::v1::RootEgressClass::S3));
    }
    rayito_proto::v1::AgentFeatures {
        configure: features.configure,
        s3_mounts: features.s3_mounts,
        efs_volumes: features.efs_volumes,
        lifecycle_events: features.lifecycle_events,
        telemetry_export: features.telemetry_export,
        secret_gateway: features.secret_gateway,
        template_start: features.template_start,
        root_egress,
    }
}

fn to_metrics_response(metrics: &MetricsSnapshot) -> MetricsResponse {
    MetricsResponse {
        cpu_used_pct: metrics.cpu_used_pct,
        mem_used_bytes: metrics.mem_used,
        mem_total_bytes: metrics.mem_total,
        disk_used_bytes: metrics.disk_used,
        disk_total_bytes: metrics.disk_total,
        cpu_count: metrics.cpu_count,
        timestamp_unix_ms: unix_millis(metrics.wall),
        mem_cache_bytes: metrics.mem_cache,
    }
}

fn to_history_response(page: &HistoryPage) -> MetricsHistoryResponse {
    MetricsHistoryResponse {
        samples: page.samples.iter().map(sample_to_response).collect(),
        oldest_unix_ms: page.oldest_unix_ms.unwrap_or(0),
    }
}

fn sample_to_response(sample: &MetricsSample) -> MetricsResponse {
    MetricsResponse {
        cpu_used_pct: sample.cpu_used_pct,
        mem_used_bytes: sample.mem_used,
        mem_total_bytes: sample.mem_total,
        disk_used_bytes: sample.disk_used,
        disk_total_bytes: sample.disk_total,
        cpu_count: sample.cpu_count,
        timestamp_unix_ms: sample.unix_ms,
        mem_cache_bytes: sample.mem_cache,
    }
}

fn metrics_status(error: &MetricsError) -> Status {
    tracing::warn!(rpc = "Metrics", reason = %error, "metrics unavailable");
    match error {
        MetricsError::Unsupported => Status::unavailable(error.to_string()),
        MetricsError::Malformed { .. } | MetricsError::Io { .. } => {
            Status::internal(error.to_string())
        }
    }
}
