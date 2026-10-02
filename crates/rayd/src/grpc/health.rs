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
use rayd_core::root_egress::RootEgressClass;
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
    /// The process's one `FeatureSet` (`grpc::router_with_features`);
    /// `None` only for a `HealthGrpc` built without `with_features`, which
    /// reports `AgentFeatures::foundations_only()` exactly like an
    /// all-`Unsupported` set would.
    features: Option<Arc<FeatureSet>>,
}

impl HealthGrpc {
    pub fn new(
        session: Arc<SandboxSession>,
        probe: Arc<dyn MetricsProbe>,
        history: Arc<MetricsHistory>,
        kernel: Arc<dyn KernelStatus>,
        imds: Arc<ImdsState>,
    ) -> Self {
        Self {
            session,
            probe,
            history,
            kernel,
            imds,
            features: None,
        }
    }

    /// Reports `features.agent_features()` in `Health.features`: the same
    /// `FeatureSet` `ConfigureService` dispatches to, so a flag here and a
    /// section's `Unsupported` outcome there can never disagree.
    #[must_use]
    pub fn with_features(mut self, features: Arc<FeatureSet>) -> Self {
        self.features = Some(features);
        self
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
        let features = self.features.as_deref();
        let flags = features.map_or_else(
            rayd_core::features::AgentFeatures::foundations_only,
            FeatureSet::agent_features,
        );
        let root_egress = features.map(FeatureSet::root_egress).unwrap_or_default();
        Ok(Response::new(to_response(snapshot, flags, &root_egress)))
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

fn to_response(
    snapshot: HealthSnapshot,
    features: rayd_core::features::AgentFeatures,
    root_egress: &[RootEgressClass],
) -> HealthResponse {
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
        // Derived from the slots themselves (`FeatureSet::agent_features`/
        // `root_egress`): each slot's own `supported()` and
        // `root_egress_class()` say what *this* boot offers — e.g.
        // `m15-s3-mounts` reports `false` without `CAP_SYS_ADMIN`, so a
        // `rayito-base` agent never claims it — and a feature that gives
        // its slot a real adapter turns its own flag on without touching
        // this file.
        features: Some(agent_features_message(features, root_egress)),
    }
}

fn agent_features_message(
    features: rayd_core::features::AgentFeatures,
    root_egress: &[RootEgressClass],
) -> rayito_proto::v1::AgentFeatures {
    rayito_proto::v1::AgentFeatures {
        configure: features.configure,
        s3_mounts: features.s3_mounts,
        efs_volumes: features.efs_volumes,
        lifecycle_events: features.lifecycle_events,
        telemetry_export: features.telemetry_export,
        secret_gateway: features.secret_gateway,
        template_start: features.template_start,
        root_egress: root_egress
            .iter()
            .map(|class| i32::from(wire_root_egress(*class)))
            .collect(),
    }
}

fn wire_root_egress(class: RootEgressClass) -> rayito_proto::v1::RootEgressClass {
    match class {
        RootEgressClass::S3 => rayito_proto::v1::RootEgressClass::S3,
        RootEgressClass::CloudwatchOtlp => rayito_proto::v1::RootEgressClass::CloudwatchOtlp,
        RootEgressClass::SecretGatewayUpstream => {
            rayito_proto::v1::RootEgressClass::SecretGatewayUpstream
        }
        RootEgressClass::Efs => rayito_proto::v1::RootEgressClass::Efs,
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_root_egress_class_maps_to_its_own_wire_value() {
        let cases = [
            (RootEgressClass::S3, rayito_proto::v1::RootEgressClass::S3),
            (
                RootEgressClass::CloudwatchOtlp,
                rayito_proto::v1::RootEgressClass::CloudwatchOtlp,
            ),
            (
                RootEgressClass::SecretGatewayUpstream,
                rayito_proto::v1::RootEgressClass::SecretGatewayUpstream,
            ),
            (RootEgressClass::Efs, rayito_proto::v1::RootEgressClass::Efs),
        ];
        for (class, wire) in cases {
            assert_eq!(wire_root_egress(class), wire);
        }
    }

    #[test]
    fn the_message_copies_every_flag_and_the_declared_egress() {
        let features = rayd_core::features::AgentFeatures {
            s3_mounts: true,
            ..rayd_core::features::AgentFeatures::foundations_only()
        };
        let message = agent_features_message(features, &[RootEgressClass::S3]);
        assert!(message.configure);
        assert!(message.s3_mounts);
        assert!(!message.efs_volumes);
        assert_eq!(
            message.root_egress,
            vec![i32::from(rayito_proto::v1::RootEgressClass::S3)]
        );
        let stubs_only =
            agent_features_message(rayd_core::features::AgentFeatures::foundations_only(), &[]);
        assert!(!stubs_only.s3_mounts);
        assert!(stubs_only.root_egress.is_empty());
    }
}
