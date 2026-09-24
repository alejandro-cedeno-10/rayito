//! What `HealthService.Health` reports: the SDK's readiness and liveness
//! probe, the signal (`resume_generation`) that tells clients to
//! re-subscribe their streams after a resume, the hardening facts
//! (`imds_blocked`, `hook_anomalies`), the payload's `metadata` echo,
//! the guest's own view of its CPUs and memory and the logical deadline
//! (`lifecycle`, ADR-011).

use std::collections::BTreeMap;
use std::time::Duration;

use crate::network::EgressEnforcement;
use crate::sandbox_timeout::LifecycleView;

/// The bools mirror the proto fields one to one; a state machine here would
/// only be undone again in `grpc/health.rs`.
#[allow(clippy::struct_excessive_bools)]
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct HealthSnapshot {
    pub agent_ready: bool,
    pub kernel_ready: bool,
    pub agent_version: String,
    pub uptime: Duration,
    pub sandbox_id: Option<String>,
    pub resume_generation: u64,
    pub clock_offset_ms: i64,
    pub kernel_state_lost: bool,
    /// `true` only once the uid-1000 IMDS block is installed and verified.
    pub imds_blocked: bool,
    /// Anomalous hook calls and stale-suspend recoveries this boot.
    pub hook_anomalies: u64,
    pub metadata: BTreeMap<String, String>,
    /// Guest view read by the adapter from the metrics probe (`0` = not
    /// read): CPUs `rayd` can use and `MemTotal` in bytes, not the image
    /// size.
    pub cpu_count: u32,
    pub memory_total_bytes: u64,
    /// Always reported by an M9 agent; phase `Unmanaged` without a
    /// lifecycle block.
    pub lifecycle: LifecycleView,
    /// Set by the adapter from the egress manager (ADR-012); the
    /// `Unspecified` default is what a pre-M9 agent reports.
    pub egress_enforcement: EgressEnforcement,
}

impl HealthSnapshot {
    #[must_use]
    pub fn builder(agent_version: &str) -> HealthSnapshotBuilder {
        HealthSnapshotBuilder {
            snapshot: HealthSnapshot {
                agent_version: agent_version.to_owned(),
                ..HealthSnapshot::default()
            },
        }
    }
}

#[derive(Debug)]
pub struct HealthSnapshotBuilder {
    snapshot: HealthSnapshot,
}

impl HealthSnapshotBuilder {
    #[must_use]
    pub fn agent_ready(mut self, ready: bool) -> Self {
        self.snapshot.agent_ready = ready;
        self
    }

    #[must_use]
    pub fn kernel_ready(mut self, ready: bool) -> Self {
        self.snapshot.kernel_ready = ready;
        self
    }

    #[must_use]
    pub fn uptime(mut self, uptime: Duration) -> Self {
        self.snapshot.uptime = uptime;
        self
    }

    #[must_use]
    pub fn sandbox_id(mut self, sandbox_id: Option<&str>) -> Self {
        self.snapshot.sandbox_id = sandbox_id.map(str::to_owned);
        self
    }

    #[must_use]
    pub fn resume_generation(mut self, generation: u64) -> Self {
        self.snapshot.resume_generation = generation;
        self
    }

    #[must_use]
    pub fn clock_offset_ms(mut self, offset_ms: i64) -> Self {
        self.snapshot.clock_offset_ms = offset_ms;
        self
    }

    #[must_use]
    pub fn kernel_state_lost(mut self, lost: bool) -> Self {
        self.snapshot.kernel_state_lost = lost;
        self
    }

    #[must_use]
    pub fn imds_blocked(mut self, blocked: bool) -> Self {
        self.snapshot.imds_blocked = blocked;
        self
    }

    #[must_use]
    pub fn hook_anomalies(mut self, anomalies: u64) -> Self {
        self.snapshot.hook_anomalies = anomalies;
        self
    }

    #[must_use]
    pub fn metadata(mut self, metadata: BTreeMap<String, String>) -> Self {
        self.snapshot.metadata = metadata;
        self
    }

    #[must_use]
    pub fn lifecycle(mut self, lifecycle: LifecycleView) -> Self {
        self.snapshot.lifecycle = lifecycle;
        self
    }

    #[must_use]
    pub fn egress_enforcement(mut self, enforcement: EgressEnforcement) -> Self {
        self.snapshot.egress_enforcement = enforcement;
        self
    }

    #[must_use]
    pub fn build(self) -> HealthSnapshot {
        self.snapshot
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn builder_starts_from_a_not_ready_snapshot() {
        let snapshot = HealthSnapshot::builder("0.1.0").build();
        assert_eq!(snapshot.agent_version, "0.1.0");
        assert!(!snapshot.agent_ready);
        assert!(!snapshot.kernel_ready);
        assert_eq!(snapshot.sandbox_id, None);
        assert_eq!(snapshot.resume_generation, 0);
        assert!(!snapshot.imds_blocked);
        assert_eq!(snapshot.hook_anomalies, 0);
        assert!(snapshot.metadata.is_empty());
        assert_eq!(snapshot.cpu_count, 0);
        assert_eq!(snapshot.memory_total_bytes, 0);
        assert_eq!(snapshot.egress_enforcement, EgressEnforcement::Unspecified);
    }

    #[test]
    fn builder_sets_every_field() {
        let lifecycle = LifecycleView {
            phase: crate::sandbox_timeout::LifecyclePhase::Active,
            deadline_unix_ms: 60_000,
            ..LifecycleView::default()
        };
        let snapshot = HealthSnapshot::builder("0.1.0")
            .lifecycle(lifecycle)
            .egress_enforcement(EgressEnforcement::GuestRoutes)
            .agent_ready(true)
            .kernel_ready(true)
            .uptime(Duration::from_millis(1_500))
            .sandbox_id(Some("mvm-1"))
            .resume_generation(2)
            .clock_offset_ms(-7)
            .kernel_state_lost(true)
            .imds_blocked(true)
            .hook_anomalies(4)
            .metadata([("a".to_owned(), "1".to_owned())].into())
            .build();
        assert_eq!(
            snapshot,
            HealthSnapshot {
                agent_ready: true,
                kernel_ready: true,
                agent_version: "0.1.0".to_owned(),
                uptime: Duration::from_millis(1_500),
                sandbox_id: Some("mvm-1".to_owned()),
                resume_generation: 2,
                clock_offset_ms: -7,
                kernel_state_lost: true,
                imds_blocked: true,
                hook_anomalies: 4,
                metadata: [("a".to_owned(), "1".to_owned())].into(),
                cpu_count: 0,
                memory_total_bytes: 0,
                lifecycle,
                egress_enforcement: EgressEnforcement::GuestRoutes,
            }
        );
    }
}
