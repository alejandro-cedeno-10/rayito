//! Validates one `telemetry_export` section's desired state, with no I/O:
//! the wire adapter (`grpc::configure`'s `S3Mounts`-style translation, done
//! by `features::telemetry_export::build`'s `apply`) turns a proto
//! `TelemetryExportConfig` into this and maps a rejection to the
//! `error_class` strings `telemetry_export.proto` documents.

use std::time::Duration;

use zeroize::Zeroizing;

use super::model::{NameStyle, ResourceAttrs};

/// Lower bound on `interval_s` (`TelemetryExportConfig.interval_s`,
/// research §6.3/§6.8 option B1: tighter than this risks the account's
/// 500 TPS / 1000-points-per-request OTLP quota under many concurrent
/// sandboxes). Provisional until OT2 re-measures billed bytes.
pub const MIN_INTERVAL: Duration = Duration::from_secs(15);
/// Upper bound on `interval_s`: beyond this a gap in the exported series
/// stops being useful for the dashboards research §6 targets.
pub const MAX_INTERVAL: Duration = Duration::from_secs(300);

/// Where the exporter's `CloudWatch` credentials come from (research §6.3:
/// option B1 vs. the bearer-token option B1', preferred per OT9 because the
/// execution-role policy cannot be scoped by `CloudWatch` namespace).
#[derive(Clone, PartialEq, Eq)]
pub enum TelemetryAuth {
    /// `SigV4` over the execution role's IMDS credentials
    /// (`adapters::credential_broker::ImdsCredentialBroker`); needs
    /// `rayito-base-caps`.
    ExecutionRole,
    /// A bearer token pushed by the SDK through this same `Configure` call,
    /// kept only in process memory. Never `Debug`/`Display`.
    Bearer(Zeroizing<String>),
}

impl std::fmt::Debug for TelemetryAuth {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::ExecutionRole => f.write_str("ExecutionRole"),
            Self::Bearer(_) => f.write_str("Bearer(..)"),
        }
    }
}

/// The complete desired state of one `telemetry_export` section, already
/// validated: constructing one is the only way to get past `rayd`'s
/// section-level rejection (`TelemetryConfigError`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TelemetryConfig {
    pub interval: Duration,
    pub service_name: String,
    pub names: NameStyle,
    pub resource: ResourceAttrs,
    pub auth: TelemetryAuth,
}

/// `error_class` for `SectionResult`, as `telemetry_export.proto` documents
/// it (lowercase snake, never an AWS message).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TelemetryConfigError {
    InvalidInterval,
    EmptyServiceName,
    MissingAuth,
}

impl TelemetryConfigError {
    #[must_use]
    pub const fn error_class(self) -> &'static str {
        match self {
            Self::InvalidInterval => "invalid_interval",
            Self::EmptyServiceName => "empty_service_name",
            Self::MissingAuth => "missing_auth",
        }
    }
}

impl TelemetryConfig {
    /// `None` auth is `MissingAuth`: the wire adapter passes `None` when
    /// neither `oneof auth` branch was set (proto3 message presence), not
    /// a default — there is no implicit auth mode.
    pub fn validate(
        interval: Duration,
        service_name: &str,
        names: NameStyle,
        resource: ResourceAttrs,
        auth: Option<TelemetryAuth>,
    ) -> Result<Self, TelemetryConfigError> {
        if interval < MIN_INTERVAL || interval > MAX_INTERVAL {
            return Err(TelemetryConfigError::InvalidInterval);
        }
        if service_name.trim().is_empty() {
            return Err(TelemetryConfigError::EmptyServiceName);
        }
        let auth = auth.ok_or(TelemetryConfigError::MissingAuth)?;
        Ok(Self {
            interval,
            service_name: service_name.to_owned(),
            names,
            resource,
            auth,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn resource() -> ResourceAttrs {
        ResourceAttrs {
            sandbox_id: "mvm-test".to_owned(),
            image_arn: "arn:aws:lambda:us-east-1:123456789012:microvm-image/rayito-base".to_owned(),
            image_version: "3".to_owned(),
            image_memory_mib: 2048,
        }
    }

    #[test]
    fn a_60s_interval_with_a_name_and_role_auth_is_valid() {
        let config = TelemetryConfig::validate(
            Duration::from_secs(60),
            "agente",
            NameStyle::Rayito,
            resource(),
            Some(TelemetryAuth::ExecutionRole),
        )
        .unwrap();
        assert_eq!(config.interval, Duration::from_secs(60));
        assert_eq!(config.auth, TelemetryAuth::ExecutionRole);
    }

    #[test]
    fn the_bounds_are_inclusive() {
        for seconds in [15, 300] {
            assert!(
                TelemetryConfig::validate(
                    Duration::from_secs(seconds),
                    "agente",
                    NameStyle::Rayito,
                    resource(),
                    Some(TelemetryAuth::ExecutionRole),
                )
                .is_ok()
            );
        }
    }

    #[test]
    fn zero_is_invalid_not_a_default() {
        let error = TelemetryConfig::validate(
            Duration::ZERO,
            "agente",
            NameStyle::Rayito,
            resource(),
            Some(TelemetryAuth::ExecutionRole),
        )
        .unwrap_err();
        assert_eq!(error, TelemetryConfigError::InvalidInterval);
        assert_eq!(error.error_class(), "invalid_interval");
    }

    #[test]
    fn an_interval_past_the_ceiling_is_invalid() {
        let error = TelemetryConfig::validate(
            Duration::from_secs(301),
            "agente",
            NameStyle::Rayito,
            resource(),
            Some(TelemetryAuth::ExecutionRole),
        )
        .unwrap_err();
        assert_eq!(error, TelemetryConfigError::InvalidInterval);
    }

    #[test]
    fn a_blank_service_name_is_rejected() {
        let error = TelemetryConfig::validate(
            Duration::from_secs(60),
            "   ",
            NameStyle::Rayito,
            resource(),
            Some(TelemetryAuth::ExecutionRole),
        )
        .unwrap_err();
        assert_eq!(error, TelemetryConfigError::EmptyServiceName);
        assert_eq!(error.error_class(), "empty_service_name");
    }

    #[test]
    fn no_auth_branch_set_is_missing_auth() {
        let error = TelemetryConfig::validate(
            Duration::from_secs(60),
            "agente",
            NameStyle::Rayito,
            resource(),
            None,
        )
        .unwrap_err();
        assert_eq!(error, TelemetryConfigError::MissingAuth);
        assert_eq!(error.error_class(), "missing_auth");
    }

    #[test]
    fn a_bearer_auth_never_debug_prints_its_token() {
        let auth = TelemetryAuth::Bearer(Zeroizing::new("super-secret".to_owned()));
        let printed = format!("{auth:?}");
        assert!(!printed.contains("super-secret"));
        assert_eq!(printed, "Bearer(..)");
    }
}
