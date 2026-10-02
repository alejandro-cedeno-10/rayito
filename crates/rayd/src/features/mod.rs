//! `FeatureSet`: the six 0.6 feature slots (M15 foundations, ADR-015),
//! built once from `main` by `build(&FeatureContext)` and shared, as one
//! `Arc<FeatureSet>`, by `ConfigureService`, `Health.features` and the
//! hooks' lifecycle participants (`grpc::router_with_features`,
//! `hooks::HookServices::participants`). Every slot is `slot::Unsupported`
//! in this build; each feature replaces its own field's construction
//! (inside its own `features::<name>::build`) in its own PR —
//! `FeatureSet`'s field list, `build`'s signature and the three views
//! below (`agent_features`, `participants`) do not change for that.

pub mod efs_volumes;
pub mod lifecycle_events;
pub mod s3_mounts;
pub mod secret_gateway;
pub mod slot;
pub mod telemetry_export;
pub mod template_start;

use std::sync::Arc;

use rayito_proto::v1::{
    EfsVolumesConfig, EfsVolumesStatus, LifecycleEventsConfig, LifecycleEventsStatus,
    S3MountsConfig, S3MountsStatus, SecretGatewayConfig, SecretGatewayStatus,
    TelemetryExportConfig, TelemetryExportStatus,
};

use crate::lifecycle::LifecycleParticipant;
use slot::ConfigurableFeature;

/// What a feature's `build()` needs from `main` to construct its slot.
/// Empty today, because every slot is still a stub: a feature that needs
/// credentials, a bucket name or other shared context adds a field here in
/// its own PR, never by widening `FeatureSet` itself.
#[derive(Default)]
pub struct FeatureContext;

pub struct FeatureSet {
    pub s3_mounts: Arc<dyn ConfigurableFeature<S3MountsConfig, S3MountsStatus>>,
    pub efs_volumes: Arc<dyn ConfigurableFeature<EfsVolumesConfig, EfsVolumesStatus>>,
    pub lifecycle_events:
        Arc<dyn ConfigurableFeature<LifecycleEventsConfig, LifecycleEventsStatus>>,
    pub telemetry_export:
        Arc<dyn ConfigurableFeature<TelemetryExportConfig, TelemetryExportStatus>>,
    pub secret_gateway: Arc<dyn ConfigurableFeature<SecretGatewayConfig, SecretGatewayStatus>>,
    /// See `template_start`'s module docs for why this slot's shape differs
    /// from the other five.
    pub template_start: Arc<dyn ConfigurableFeature<(), ()>>,
}

impl FeatureSet {
    /// `Health.features`: capability flags, never "currently configured"
    /// (`rayd_core::features::AgentFeatures`: a flag means this build *has*
    /// a real adapter for the slot, not that a section was ever applied).
    /// With every slot `Unsupported` this equals
    /// `AgentFeatures::foundations_only()`.
    #[must_use]
    pub fn agent_features(&self) -> rayd_core::features::AgentFeatures {
        rayd_core::features::AgentFeatures {
            configure: true,
            s3_mounts: self.s3_mounts.supported(),
            efs_volumes: self.efs_volumes.supported(),
            lifecycle_events: self.lifecycle_events.supported(),
            telemetry_export: self.telemetry_export.supported(),
            secret_gateway: self.secret_gateway.supported(),
            template_start: self.template_start.supported(),
        }
    }

    /// Every slot's `LifecycleParticipant`, in no particular order
    /// (`hooks::mod` keys each `/suspend` share by its own
    /// `demand().name`). `main` hands this to `HookServices.participants`
    /// from the same `FeatureSet` it gives the gRPC router, so a section
    /// `ConfigureService` applied is the state `/suspend`/`/resume`/
    /// `/terminate` act on. Empty while every slot is `Unsupported`, which
    /// keeps every hook byte-for-byte 0.5.x.
    #[must_use]
    pub fn participants(&self) -> Vec<Arc<dyn LifecycleParticipant>> {
        [
            self.s3_mounts.participant(),
            self.efs_volumes.participant(),
            self.lifecycle_events.participant(),
            self.telemetry_export.participant(),
            self.secret_gateway.participant(),
            self.template_start.participant(),
        ]
        .into_iter()
        .flatten()
        .collect()
    }
}

#[must_use]
pub fn build(ctx: &FeatureContext) -> FeatureSet {
    FeatureSet {
        s3_mounts: s3_mounts::build(ctx),
        efs_volumes: efs_volumes::build(ctx),
        lifecycle_events: lifecycle_events::build(ctx),
        telemetry_export: telemetry_export::build(ctx),
        secret_gateway: secret_gateway::build(ctx),
        template_start: template_start::build(ctx),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_slot_starts_unsupported() {
        let set = build(&FeatureContext);
        assert!(!set.s3_mounts.supported());
        assert!(!set.efs_volumes.supported());
        assert!(!set.lifecycle_events.supported());
        assert!(!set.telemetry_export.supported());
        assert!(!set.secret_gateway.supported());
        assert!(!set.template_start.supported());
    }

    #[test]
    fn an_all_unsupported_set_reports_foundations_only_and_no_participants() {
        let set = build(&FeatureContext);
        assert_eq!(
            set.agent_features(),
            rayd_core::features::AgentFeatures::foundations_only()
        );
        assert!(set.participants().is_empty());
    }
}
