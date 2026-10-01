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

use rayd_core::clock::SystemClock;
use rayd_core::metrics_history::MetricsHistory;
use rayd_core::session::SandboxSession;
use rayito_proto::v1::{
    EfsVolumesConfig, EfsVolumesStatus, LifecycleEventsConfig, LifecycleEventsStatus,
    S3MountsConfig, S3MountsStatus, SecretGatewayConfig, SecretGatewayStatus,
    TelemetryExportConfig, TelemetryExportStatus,
};

use crate::adapters::{ImdsCredentialBroker, PushedCredentials};
use slot::ConfigurableFeature;

/// What a feature's `build()` needs from `main` to construct its slot.
/// `s3_mounts`/`efs_volumes`/`lifecycle_events`/`secret_gateway`/`template_start`
/// still ignore every field (every slot but `telemetry_export`'s is a
/// stub); `telemetry_export` is the first to need shared context
/// (m15-rayd-otlp, ADR-021), which is why this struct already carries the
/// session, the shared IMDS credential broker, the pushed-credentials
/// holder and the region — a future feature needing something else adds a
/// field here too, never by widening `FeatureSet` itself.
#[derive(Clone)]
pub struct FeatureContext {
    pub session: Arc<SandboxSession>,
    pub credentials: Arc<ImdsCredentialBroker>,
    pub pushed: Arc<PushedCredentials>,
    /// The same ring the 5 s sampler already feeds (`main`'s
    /// `spawn_metrics_sampler`): `telemetry_export` reads it instead of
    /// probing procfs a second time.
    pub history: Arc<MetricsHistory>,
    /// `AWS_REGION` as `main` read it; `None` means the platform never set
    /// it (the same degrade `adapters::s3_store` already allows for
    /// persistence) — `telemetry_export::build` reports `Unsupported`
    /// rather than guess a region to sign with or to build the `CloudWatch`
    /// endpoint host from.
    pub region: Option<String>,
}

impl Default for FeatureContext {
    /// Only for tests and for the `ConfigureGrpc`/`features::build` call
    /// sites that have not been threaded with real context yet: `region:
    /// None` keeps every slot, including `telemetry_export`, `Unsupported`
    /// (`every_slot_starts_unsupported` below), exactly like before this
    /// struct grew fields.
    fn default() -> Self {
        Self {
            session: Arc::new(SandboxSession::new(Arc::new(SystemClock::new()), "test")),
            credentials: Arc::new(ImdsCredentialBroker::new()),
            pushed: Arc::new(PushedCredentials::new()),
            history: Arc::new(MetricsHistory::default()),
            region: None,
        }
    }
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
    /// (`AgentFeatures`'s own doc comment: a flag means this build *has* a
    /// real adapter, not that a section was ever applied).
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

    /// `Health.features.root_egress`: same capability-based rule as
    /// `agent_features` (`rayd_core::root_egress`'s own doc comment: "once
    /// it has a real adapter"), never a host or an IP.
    #[must_use]
    pub fn root_egress(&self) -> Vec<rayd_core::root_egress::RootEgressClass> {
        let mut classes = Vec::new();
        if self.telemetry_export.supported() {
            classes.push(rayd_core::root_egress::RootEgressClass::CloudwatchOtlp);
        }
        classes
    }

    /// Every slot's `LifecycleParticipant`, for `hooks::HookServices::participants`
    /// (`main`): generic over all six slots so a future feature's
    /// `participant()` is picked up here without `main` changing again.
    #[must_use]
    pub fn participants(&self) -> Vec<Arc<dyn crate::lifecycle::LifecycleParticipant>> {
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
        let set = build(&FeatureContext::default());
        assert!(!set.s3_mounts.supported());
        assert!(!set.efs_volumes.supported());
        assert!(!set.lifecycle_events.supported());
        assert!(!set.telemetry_export.supported());
        assert!(!set.secret_gateway.supported());
        assert!(!set.template_start.supported());
    }

    #[test]
    fn with_no_region_agent_features_and_root_egress_stay_at_their_0_6_0_defaults() {
        let set = build(&FeatureContext::default());
        assert_eq!(
            set.agent_features(),
            rayd_core::features::AgentFeatures::foundations_only()
        );
        assert!(set.root_egress().is_empty());
        assert!(set.participants().is_empty());
    }

    #[test]
    fn a_known_region_turns_on_the_telemetry_flag_and_its_root_egress_class() {
        let set = build(&FeatureContext {
            region: Some("us-east-1".to_owned()),
            ..FeatureContext::default()
        });
        assert!(set.agent_features().telemetry_export);
        assert_eq!(
            set.root_egress(),
            vec![rayd_core::root_egress::RootEgressClass::CloudwatchOtlp]
        );
    }
}
