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
/// `m15-events-webhooks` does not need one: its event metadata
/// (`sandbox_id`, image ARN/version) arrives in `LifecycleEventsConfig`
/// itself (the SDK already knows all three at `Sandbox.create` time), and
/// its `/resume` generation counter lives in its own participant — so
/// `FeatureContext` stays a unit struct here, even though this feature is
/// the one that first needed `FeatureSet` shared between the gRPC and
/// hooks listeners (see `grpc::Services.features` and `main.rs`).
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
    /// Every slot's `participant()`, in no particular order (`hooks::mod`'s
    /// `SuspendShares::allocate` keys each one by its own `demand().name`,
    /// so the list's order never matters). `main.rs` collects this once at
    /// startup into `HookServices.participants`; with every slot still
    /// `slot::Unsupported` (`participant() -> None`), this is empty and
    /// `/suspend`/`/ready` stay byte-for-byte 0.5.x, which is also what the
    /// zero-cost golden test pins.
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

    // `#[tokio::test]`, not `#[test]`: `build()` now reaches
    // `lifecycle_events::shared_inner()`, which spawns its stdout-draining
    // task (`adapters::stdout_event_sink::spawn`) and so needs a runtime —
    // every other slot stays a stateless `Unsupported`, so this is the one
    // seam in `build()` that is no longer runtime-free.
    #[tokio::test]
    async fn every_still_stubbed_slot_starts_unsupported() {
        let set = build(&FeatureContext);
        assert!(!set.s3_mounts.supported());
        assert!(!set.efs_volumes.supported());
        assert!(!set.telemetry_export.supported());
        assert!(!set.secret_gateway.supported());
        assert!(!set.template_start.supported());
    }

    /// `m15-events-webhooks` replaced its stub: unlike the others, it is
    /// always supported (what stays inert without a `ConfigureSandbox`
    /// call is its key, not the slot itself — see that module's docs).
    #[tokio::test]
    async fn lifecycle_events_is_supported_once_its_feature_lands() {
        let set = build(&FeatureContext);
        assert!(set.lifecycle_events.supported());
    }

    #[tokio::test]
    async fn participants_is_empty_while_every_other_slot_is_still_a_stub() {
        let set = build(&FeatureContext);
        // `lifecycle_events` always returns `Some` from `participant()`,
        // so with the other five still stubs the list has exactly one
        // entry, not zero — pinning the point where `/suspend` and
        // `/ready` stop being byte-for-byte 0.5.x behaviour.
        assert_eq!(set.participants().len(), 1);
    }
}
