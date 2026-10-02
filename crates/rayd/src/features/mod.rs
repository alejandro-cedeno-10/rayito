//! `FeatureSet`: the six 0.6 feature slots (M15 foundations, ADR-015),
//! built once from `main` by `build(&FeatureContext)`. Every slot is
//! `slot::Unsupported` in this build; each feature replaces its own
//! field's construction (inside its own `features::<name>::build`) in its
//! own PR — `FeatureSet`'s field list and `build`'s signature do not
//! change for that.

pub mod efs_volumes;
pub mod lifecycle_events;
pub mod s3_mounts;
pub mod secret_gateway;
pub mod slot;
pub mod telemetry_export;
pub mod template_start;

use std::sync::Arc;

use rayd_core::features::AgentFeatures;
use rayd_core::root_egress::RootEgressClass;
use rayito_proto::v1::{
    EfsVolumesConfig, EfsVolumesStatus, LifecycleEventsConfig, LifecycleEventsStatus,
    S3MountsConfig, S3MountsStatus, SecretGatewayConfig, SecretGatewayStatus,
    TelemetryExportConfig, TelemetryExportStatus,
};

use slot::ConfigurableFeature;

/// What a feature's `build()` needs from `main` to construct its slot.
/// `child_registry` is `m15-s3-mounts`'s own addition (its `mount-s3`
/// daemon registers each pid there, see `adapters::mount_s3`'s module
/// doc); every other slot is still a stub and ignores it. A feature that
/// needs a bucket name, a credential broker or other shared context adds
/// its own field here in its own PR, never by widening `FeatureSet` itself.
#[derive(Default)]
pub struct FeatureContext {
    pub child_registry: Arc<crate::adapters::ChildRegistry>,
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

impl FeatureSet {
    /// `Health.features`, read live from every slot's own `supported()`:
    /// a feature that gains a real adapter changes nothing here, only its
    /// own `build()`.
    #[must_use]
    pub fn agent_features(&self) -> AgentFeatures {
        AgentFeatures {
            s3_mounts: self.s3_mounts.supported(),
            efs_volumes: self.efs_volumes.supported(),
            lifecycle_events: self.lifecycle_events.supported(),
            telemetry_export: self.telemetry_export.supported(),
            secret_gateway: self.secret_gateway.supported(),
            template_start: self.template_start.supported(),
            ..AgentFeatures::foundations_only()
        }
    }

    /// `Health.features.root_egress`: the declared class of every supported
    /// slot that opens one (`ConfigurableFeature::root_egress_class`), in
    /// `FeatureSet` field order.
    #[must_use]
    pub fn root_egress(&self) -> Vec<RootEgressClass> {
        [
            active_egress(self.s3_mounts.as_ref()),
            active_egress(self.efs_volumes.as_ref()),
            active_egress(self.lifecycle_events.as_ref()),
            active_egress(self.telemetry_export.as_ref()),
            active_egress(self.secret_gateway.as_ref()),
            active_egress(self.template_start.as_ref()),
        ]
        .into_iter()
        .flatten()
        .collect()
    }
}

/// A slot's root-egress class, only while it is `supported()`: an
/// `Unsupported` stub never opens one, whatever it would declare.
fn active_egress<Cfg, Status>(
    slot: &dyn ConfigurableFeature<Cfg, Status>,
) -> Option<RootEgressClass> {
    if slot.supported() {
        slot.root_egress_class()
    } else {
        None
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
    fn every_slot_still_a_stub_starts_unsupported() {
        // `s3_mounts` has a real adapter since `m15-s3-mounts`
        // (`features::s3_mounts::build`), asserted separately in that
        // module's own tests; every other slot is still `Unsupported`.
        let set = build(&FeatureContext::default());
        assert!(!set.efs_volumes.supported());
        assert!(!set.lifecycle_events.supported());
        assert!(!set.telemetry_export.supported());
        assert!(!set.secret_gateway.supported());
        assert!(!set.template_start.supported());
    }

    #[test]
    fn agent_features_and_root_egress_are_read_from_the_slots() {
        // Whatever this host makes of `s3_mounts` (a CI runner has no
        // `CAP_SYS_ADMIN`/`mount-s3`), the reported flag and the reported
        // root egress follow that slot's own `supported()`, and every stub
        // stays `false` with no egress.
        let set = build(&FeatureContext::default());
        let features = set.agent_features();
        assert!(features.configure);
        assert_eq!(features.s3_mounts, set.s3_mounts.supported());
        assert!(!features.efs_volumes);
        assert!(!features.secret_gateway);
        let expected: Vec<RootEgressClass> = if set.s3_mounts.supported() {
            vec![RootEgressClass::S3]
        } else {
            Vec::new()
        };
        assert_eq!(set.root_egress(), expected);
    }
}
