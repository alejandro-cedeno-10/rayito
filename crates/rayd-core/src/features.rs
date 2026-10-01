//! Which 0.6 features this agent build actually supports (M15 foundations).
//! Mirrors `AgentFeatures` of `proto/rayito/v1/features.proto`
//! (`HealthResponse.features`, field 16): an agent older than 0.6 never
//! sends the field at all (proto3 message presence), which is how the SDK
//! tells "no 0.6 support" apart from "0.6 support, but this particular
//! feature's slot is `Unsupported`" (`rayd::features::slot::Unsupported`).
//! `configure` is the only flag foundations itself ever sets to `true`:
//! every other flag stays `false` until the feature that owns it builds a
//! real slot.

/// The flags of one `Health` response. `Default` is "pre-0.6 shaped": every
/// flag `false`, which is also what an agent whose build never wires a
/// feature's slot reports for that flag.
///
/// The bools mirror `AgentFeatures` of `features.proto` one to one; a state
/// machine here would only be undone again in `grpc/health.rs`.
#[allow(clippy::struct_excessive_bools)]
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct AgentFeatures {
    /// `ConfigureService` exists on this build (always `true` from 0.6.0).
    pub configure: bool,
    pub s3_mounts: bool,
    pub efs_volumes: bool,
    pub lifecycle_events: bool,
    pub telemetry_export: bool,
    pub secret_gateway: bool,
    pub template_start: bool,
}

impl AgentFeatures {
    /// What a 0.6.0 build with every feature still a stub reports: only
    /// `ConfigureService` itself exists.
    #[must_use]
    pub const fn foundations_only() -> Self {
        Self {
            configure: true,
            s3_mounts: false,
            efs_volumes: false,
            lifecycle_events: false,
            telemetry_export: false,
            secret_gateway: false,
            template_start: false,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_default_is_a_pre_0_6_agent_shape() {
        assert_eq!(
            AgentFeatures::default(),
            AgentFeatures {
                configure: false,
                s3_mounts: false,
                efs_volumes: false,
                lifecycle_events: false,
                telemetry_export: false,
                secret_gateway: false,
                template_start: false,
            }
        );
    }

    #[test]
    fn foundations_only_sets_exactly_configure() {
        let features = AgentFeatures::foundations_only();
        assert!(features.configure);
        assert!(!features.s3_mounts);
        assert!(!features.efs_volumes);
        assert!(!features.lifecycle_events);
        assert!(!features.telemetry_export);
        assert!(!features.secret_gateway);
        assert!(!features.template_start);
    }
}
