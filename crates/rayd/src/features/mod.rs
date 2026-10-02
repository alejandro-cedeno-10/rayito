//! `FeatureSet`: the six 0.6 feature slots (M15 foundations, ADR-015),
//! built once from `main` by `build(&FeatureContext)` and shared, as one
//! `Arc<FeatureSet>`, by `ConfigureService`, `Health.features` and the
//! hooks' lifecycle participants (`grpc::router_with_features`,
//! `hooks::HookServices::participants`). Each feature replaces its own
//! field's construction (inside its own `features::<name>::build`, e.g.
//! `m15-s3-mounts`'s, `m15-events-webhooks`'s, `m15-templates`'s and
//! `m15-secrets-gateway`'s real adapters) in its own PR, every other slot
//! staying `slot::Unsupported` — `FeatureSet`'s field list, `build`'s
//! signature and the three views below (`agent_features`, `root_egress`,
//! `participants`) do not change for that.

pub mod efs_volumes;
pub mod lifecycle_events;
pub mod s3_mounts;
pub mod secret_gateway;
pub mod slot;
pub mod telemetry_export;
pub mod template_start;

use std::sync::Arc;

use rayd_core::clock::SystemClock;
use rayd_core::features::AgentFeatures;
use rayd_core::metrics_history::MetricsHistory;
use rayd_core::root_egress::RootEgressClass;
use rayd_core::session::SandboxSession;
use rayito_proto::v1::{
    EfsVolumesConfig, EfsVolumesStatus, LifecycleEventsConfig, LifecycleEventsStatus,
    S3MountsConfig, S3MountsStatus, SecretGatewayConfig, SecretGatewayStatus,
    TelemetryExportConfig, TelemetryExportStatus,
};

use crate::adapters::{ChildRegistry, ImdsCredentialBroker, PushedCredentials};
use crate::grpc::PlatformProcessManager;
use crate::lifecycle::LifecycleParticipant;
use slot::ConfigurableFeature;

/// What a feature's `build()` needs from `main` to construct its slot. A
/// feature that needs credentials, a bucket name or other shared context
/// adds its own field here in its own PR, never by widening `FeatureSet`
/// itself; every other slot ignores it.
#[derive(Clone)]
pub struct FeatureContext {
    /// `m15-s3-mounts`: its `mount-s3` daemon registers each pid here (see
    /// `adapters::mount_s3`'s module doc).
    pub child_registry: Arc<ChildRegistry>,
    /// `template_start` (ADR-022): `None` builds a slot that still reports
    /// `supported()` correctly but spawns nothing.
    pub processes: Option<Arc<PlatformProcessManager>>,
    /// `telemetry_export` (m15-rayd-otlp, ADR-021) reads the session,
    /// signs with `credentials`/`pushed` and samples `history`.
    pub session: Arc<SandboxSession>,
    /// Built by `main` over the same execution-role provider instance
    /// persistence's `S3ObjectStore` uses (`ImdsCredentialBroker::sharing`),
    /// so every feature and persistence share one IMDS cache.
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
    /// The context every test (and `grpc::router_with_transfers`) builds
    /// with: no process manager and `region: None`, so `template_start`
    /// spawns nothing and `telemetry_export` stays `Unsupported`.
    fn default() -> Self {
        Self {
            child_registry: Arc::default(),
            processes: None,
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
        // Every other slot has a real adapter (`telemetry_export` only with
        // a region), asserted in its own module's tests and below.
        let set = build(&FeatureContext::default());
        assert!(!set.efs_volumes.supported());
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
        // `telemetry_export` needs a region (`FeatureContext::default()`
        // has none): see the next test.
        assert!(!features.telemetry_export);
        // `template_start` always understands the spec; without a
        // `template.json` (any test host) it has no participant.
        assert!(features.template_start);
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

    #[tokio::test]
    async fn a_known_region_turns_on_the_telemetry_flag_and_its_root_egress_class() {
        let set = build(&FeatureContext {
            region: Some("us-east-1".to_owned()),
            ..FeatureContext::default()
        });
        assert!(set.agent_features().telemetry_export);
        assert!(set.root_egress().contains(&RootEgressClass::CloudwatchOtlp));
    }
}
