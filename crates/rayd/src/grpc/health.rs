//! `HealthService`: the anonymous readiness probe (with `kernel_ready` and
//! `kernel_state_lost` read from the `KernelStatus` port, `imds_blocked`
//! from the IMDS block state and `hook_anomalies` from the session) and
//! `Metrics`, which samples the CPU twice 100 ms apart through the
//! `MetricsProbe` port.

use std::sync::Arc;
use std::time::{Duration, UNIX_EPOCH};

use rayd_core::code::KernelStatus;
use rayd_core::health::HealthSnapshot;
use rayd_core::metrics::{MetricsError, MetricsProbe, MetricsSnapshot, snapshot};
use rayd_core::session::SandboxSession;
use rayito_proto::v1::health_service_server::HealthService;
use rayito_proto::v1::{HealthRequest, HealthResponse, MetricsRequest, MetricsResponse};
use tonic::{Request, Response, Status};

use crate::adapters::ImdsState;

pub const CPU_SAMPLE_INTERVAL: Duration = Duration::from_millis(100);

pub struct HealthGrpc {
    session: Arc<SandboxSession>,
    probe: Arc<dyn MetricsProbe>,
    kernel: Arc<dyn KernelStatus>,
    imds: Arc<ImdsState>,
}

impl HealthGrpc {
    pub fn new(
        session: Arc<SandboxSession>,
        probe: Arc<dyn MetricsProbe>,
        kernel: Arc<dyn KernelStatus>,
        imds: Arc<ImdsState>,
    ) -> Self {
        Self {
            session,
            probe,
            kernel,
            imds,
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
        Ok(Response::new(to_response(snapshot)))
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
}

fn to_response(snapshot: HealthSnapshot) -> HealthResponse {
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
    }
}

fn to_metrics_response(metrics: &MetricsSnapshot) -> MetricsResponse {
    let timestamp_unix_ms = metrics.wall.duration_since(UNIX_EPOCH).map_or(0, |since| {
        i64::try_from(since.as_millis()).unwrap_or(i64::MAX)
    });
    MetricsResponse {
        cpu_used_pct: metrics.cpu_used_pct,
        mem_used_bytes: metrics.mem_used,
        mem_total_bytes: metrics.mem_total,
        disk_used_bytes: metrics.disk_used,
        disk_total_bytes: metrics.disk_total,
        cpu_count: metrics.cpu_count,
        timestamp_unix_ms,
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
