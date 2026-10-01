//! Domain of `ConfigureService` (M15 foundations, ADR-015): the per-sandbox
//! configuration channel for the 0.6 optional features. `ConfigureSandbox`
//! is the only way a client sets up s3-mounts, efs-volumes,
//! lifecycle-events, telemetry-export or secret-gateway after `/run`; with
//! no section present in a request, nothing in `rayd` changes (ADR-014
//! rule 4: no 0.6 behaviour is ever reached without an explicit call).
//!
//! A request carries a fixed set of sections (`ConfigSection`); an absent
//! section is left untouched, a present one (even empty) replaces the
//! feature's previous configuration. `APPLY_ORDER` is fixed so that event
//! delivery and telemetry are wired before anything that might itself
//! generate events or metrics worth exporting, and mounts (which may
//! return `PENDING`, ADR-017/018) apply last.

/// Which 0.6 feature a section configures. The wire enum
/// (`proto/rayito/v1/configure.proto`) mirrors this order.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum ConfigSection {
    LifecycleEvents,
    TelemetryExport,
    SecretGateway,
    S3Mounts,
    EfsVolumes,
}

/// `events → telemetry → gateway → s3_mounts → efs_volumes`: events first so
/// every later section's own activity is observable, mounts last because
/// they may need the longest to settle (`PENDING`).
pub const APPLY_ORDER: [ConfigSection; 5] = [
    ConfigSection::LifecycleEvents,
    ConfigSection::TelemetryExport,
    ConfigSection::SecretGateway,
    ConfigSection::S3Mounts,
    ConfigSection::EfsVolumes,
];

/// The per-section result `ConfigureResponse.results` reports back.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SectionCode {
    /// The section's desired state is now in effect.
    Applied,
    /// Accepted but not yet in effect (a mount still settling).
    Pending,
    /// This agent build has no slot for the feature (`features::build`
    /// returned `Unsupported`).
    Unsupported,
    /// The section's own contents were rejected before anything changed.
    Invalid,
    /// Accepted and attempted, but the feature's own adapter failed.
    Failed,
}

/// What one feature's slot reports back to `ConfigureGrpc` after `apply()`;
/// `error_class` is a closed, lowercase snake string the feature's own
/// `.proto` documents (never an AWS message, host or path).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SectionOutcome {
    pub code: SectionCode,
    pub error_class: Option<String>,
}

impl SectionOutcome {
    #[must_use]
    pub fn applied() -> Self {
        Self {
            code: SectionCode::Applied,
            error_class: None,
        }
    }

    #[must_use]
    pub fn unsupported() -> Self {
        Self {
            code: SectionCode::Unsupported,
            error_class: None,
        }
    }
}

/// `ConfigureService` only accepts calls once the sandbox is actually
/// serving traffic: before `/run` there is nothing to configure, and after
/// `/suspend` or during `/terminate` a change would race the checkpoint or
/// never reach a feature that already tore down. Mirrors the detail string
/// `rayd`'s other gates use (`"not_running"`), not an AWS or proxy message.
pub const NOT_RUNNING_DETAIL: &str = "not_running";

#[must_use]
pub fn is_configurable(phase: crate::lifecycle::HookPhase) -> bool {
    use crate::lifecycle::HookPhase;
    matches!(phase, HookPhase::Running | HookPhase::Resumed)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::lifecycle::HookPhase;

    #[test]
    fn apply_order_is_events_then_telemetry_then_gateway_then_mounts() {
        assert_eq!(
            APPLY_ORDER,
            [
                ConfigSection::LifecycleEvents,
                ConfigSection::TelemetryExport,
                ConfigSection::SecretGateway,
                ConfigSection::S3Mounts,
                ConfigSection::EfsVolumes,
            ]
        );
    }

    #[test]
    fn only_running_and_resumed_accept_configure() {
        assert!(is_configurable(HookPhase::Running));
        assert!(is_configurable(HookPhase::Resumed));
        for phase in [
            HookPhase::Booting,
            HookPhase::Ready,
            HookPhase::Suspending,
            HookPhase::Terminating,
        ] {
            assert!(!is_configurable(phase));
        }
    }

    #[test]
    fn unsupported_outcome_carries_no_error_class() {
        let outcome = SectionOutcome::unsupported();
        assert_eq!(outcome.code, SectionCode::Unsupported);
        assert_eq!(outcome.error_class, None);
    }
}
