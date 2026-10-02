//! `FeatureSet`: the six 0.6 feature slots (M15 foundations, ADR-015),
//! built once from `main` by `build(&FeatureContext)` and shared, as one
//! `Arc<FeatureSet>`, by `ConfigureService`, `Health.features` and the
//! hooks' lifecycle participants (`grpc::router_with_features`,
//! `hooks::HookServices::participants`). Each feature replaces its own
//! field's construction (inside its own `features::<name>::build`, e.g.
//! `m15-s3-mounts`'s, `m15-events-webhooks`'s and `m15-secrets-gateway`'s
//! real adapters) in its own PR, every other slot staying
//! `slot::Unsupported` — `FeatureSet`'s field list, `build`'s signature and
//! the three views below (`agent_features`, `root_egress`, `participants`)
//! do not change for that.

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

use crate::lifecycle::LifecycleParticipant;
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
    /// `Health.features`: capability flags, never "currently configured"
    /// (`rayd_core::features::AgentFeatures`: a flag means this build *has*
    /// a real adapter for the slot, not that a section was ever applied),
    /// read live from every slot's own `supported()`. With every slot
    /// `Unsupported` this equals `AgentFeatures::foundations_only()`.
    #[must_use]
    pub fn agent_features(&self) -> AgentFeatures {
        AgentFeatures {
            configure: true,
            s3_mounts: self.s3_mounts.supported(),
            efs_volumes: self.efs_volumes.supported(),
            lifecycle_events: self.lifecycle_events.supported(),
            telemetry_export: self.telemetry_export.supported(),
            secret_gateway: self.secret_gateway.supported(),
            template_start: self.template_start.supported(),
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

    // `#[tokio::test]`: `lifecycle_events::build` spawns its stdout drain
    // task, so `build()` needs a runtime.
    #[tokio::test]
    async fn every_slot_still_a_stub_starts_unsupported() {
        // `s3_mounts` (`m15-s3-mounts`), `lifecycle_events`
        // (`m15-events-webhooks`) and `secret_gateway` (`m15-secrets-gateway`)
        // have real adapters, asserted separately in their own modules'
        // tests; every other slot is still `Unsupported`.
        let set = build(&FeatureContext::default());
        assert!(!set.efs_volumes.supported());
        assert!(!set.telemetry_export.supported());
        assert!(!set.template_start.supported());
    }

    #[tokio::test]
    async fn the_views_are_read_from_the_slots() {
        // Whatever this host makes of `s3_mounts` (a CI runner has no
        // `CAP_SYS_ADMIN`/`mount-s3`) or `secret_gateway` (it degrades
        // without a TLS trust store), the reported flags and the reported
        // root egress follow each slot's own `supported()`, in `FeatureSet`
        // field order; `lifecycle_events` is always supported and always a
        // participant (what stays inert without a `ConfigureSandbox`
        // section is its key), and every stub stays `false` with no egress
        // and no participant.
        let set = build(&FeatureContext::default());
        let features = set.agent_features();
        assert!(features.configure);
        assert_eq!(features.s3_mounts, set.s3_mounts.supported());
        assert_eq!(features.secret_gateway, set.secret_gateway.supported());
        assert!(features.lifecycle_events);
        assert!(!features.efs_volumes);
        let expected: Vec<RootEgressClass> = [
            (set.s3_mounts.supported(), RootEgressClass::S3),
            (
                set.secret_gateway.supported(),
                RootEgressClass::SecretGatewayUpstream,
            ),
        ]
        .into_iter()
        .filter_map(|(supported, class)| supported.then_some(class))
        .collect();
        assert_eq!(set.root_egress(), expected);
        let names: Vec<&str> = set
            .participants()
            .iter()
            .map(|participant| participant.demand().name)
            .collect();
        assert_eq!(
            names,
            [
                s3_mounts::PARTICIPANT_NAME,
                rayd_core::lifecycle_events::PARTICIPANT_NAME
            ]
        );
    }
}
