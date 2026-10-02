//! `FeatureSet`: the six 0.6 feature slots (M15 foundations, ADR-015),
//! built from `main` by `build(&FeatureContext)` — once with a real
//! `FeatureContext` for the slot(s) `hooks::mod`'s participants need
//! (`main` does this for `template_start`, ADR-022), and once more, cheaply,
//! wherever `ConfigureGrpc` is constructed (`grpc::router_with_transfers`),
//! since `ConfigureGrpc` never reads `template_start` or needs its
//! context. Every slot but `template_start` is still `slot::Unsupported`;
//! each feature replaces its own field's construction (inside its own
//! `features::<name>::build`) in its own PR — `FeatureSet`'s field list and
//! `build`'s signature do not change for that.

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

use crate::grpc::PlatformProcessManager;

/// What a feature's `build()` needs from `main` to construct its slot. A
/// feature that needs credentials, a bucket name or other shared context
/// adds a field here in its own PR, never by widening `FeatureSet` itself.
/// `processes` is `template_start`'s (ADR-022): every other slot ignores
/// it. `None` builds a slot that still reports `supported()` correctly but
/// spawns nothing — the shape every non-template test keeps using via
/// `FeatureContext::default()`.
#[derive(Default)]
pub struct FeatureContext {
    pub processes: Option<Arc<PlatformProcessManager>>,
}

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
    fn every_stub_slot_starts_unsupported() {
        let set = build(&FeatureContext::default());
        assert!(!set.s3_mounts.supported());
        assert!(!set.efs_volumes.supported());
        assert!(!set.lifecycle_events.supported());
        assert!(!set.telemetry_export.supported());
        assert!(!set.secret_gateway.supported());
        // template_start (ADR-022, m15-templates) is the first slot with a
        // real adapter: this build always understands the spec, regardless
        // of whether `FeatureContext.processes` (or a template.json) is
        // present — see `template_start::tests::no_template_json_yields_no_participant`
        // for the "no spec" case that actually matters for behaviour.
        assert!(set.template_start.supported());
    }
}
