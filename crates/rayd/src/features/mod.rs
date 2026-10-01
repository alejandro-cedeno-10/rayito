//! `FeatureSet`: the six 0.6 feature slots (M15 foundations, ADR-015),
//! built once from `main` by `build(&FeatureContext)`. Every slot starts as
//! `slot::Unsupported`; each feature replaces its own field's construction
//! (inside its own `features::<name>::build`) in its own PR —
//! `FeatureSet`'s field list and `build`'s signature do not change for
//! that. `secret_gateway` (m15-secrets-gateway) is the first to do so.

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
    fn every_slot_starts_unsupported_except_the_features_already_shipped() {
        let set = build(&FeatureContext);
        assert!(!set.s3_mounts.supported());
        assert!(!set.efs_volumes.supported());
        assert!(!set.lifecycle_events.supported());
        assert!(!set.telemetry_export.supported());
        // m15-secrets-gateway: the first 0.6 feature with a real adapter
        // (`features::secret_gateway::build`), so this is the one slot this
        // build actually supports.
        assert!(set.secret_gateway.supported());
        assert!(!set.template_start.supported());
    }
}
