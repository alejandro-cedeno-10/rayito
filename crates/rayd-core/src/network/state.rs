//! How the policy is enforced right now (`Health.egress_enforcement`) and
//! what `GetNetwork` answers. The snapshot echoes the lists as sent and
//! only says whether an upstream proxy is configured, never where.

use super::policy::{EgressMode, EgressPolicy};

/// Mirrors the proto enum; `Unspecified` is what a pre-M9 agent reports.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum EgressEnforcement {
    #[default]
    Unspecified,
    None,
    GuestRoutes,
    GuestRoutesAndProxy,
}

impl EgressEnforcement {
    /// The value a passing verification publishes for `mode`.
    #[must_use]
    pub fn verified(mode: EgressMode) -> Self {
        match mode {
            EgressMode::Unrestricted => Self::None,
            EgressMode::Routes => Self::GuestRoutes,
            EgressMode::ProxyOnly => Self::GuestRoutesAndProxy,
        }
    }

    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Unspecified => "unspecified",
            Self::None => "none",
            Self::GuestRoutes => "guest_routes",
            Self::GuestRoutesAndProxy => "guest_routes_and_proxy",
        }
    }

    /// The compact form kept in an atomic.
    #[must_use]
    pub fn to_code(self) -> u8 {
        match self {
            Self::Unspecified => 0,
            Self::None => 1,
            Self::GuestRoutes => 2,
            Self::GuestRoutesAndProxy => 3,
        }
    }

    /// Any code this type never produced reads as `None`: an unknown state
    /// is never reported as enforced.
    #[must_use]
    pub fn from_code(code: u8) -> Self {
        match code {
            0 => Self::Unspecified,
            2 => Self::GuestRoutes,
            3 => Self::GuestRoutesAndProxy,
            _ => Self::None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct NetworkSnapshot {
    pub allow_out: Vec<String>,
    pub deny_out: Vec<String>,
    pub egress_proxy_configured: bool,
    pub enforcement: EgressEnforcement,
    /// `None` while the local proxy is not running.
    pub local_proxy_port: Option<u16>,
}

impl NetworkSnapshot {
    #[must_use]
    pub fn of(
        policy: &EgressPolicy,
        enforcement: EgressEnforcement,
        local_proxy_port: Option<u16>,
    ) -> Self {
        Self {
            allow_out: policy.raw_allow().to_vec(),
            deny_out: policy.raw_deny().to_vec(),
            egress_proxy_configured: policy.upstream().is_some(),
            enforcement,
            local_proxy_port,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::network::policy::{PolicyInput, UpstreamInput};

    #[test]
    fn verified_values_follow_the_mode() {
        assert_eq!(
            EgressEnforcement::verified(EgressMode::Unrestricted),
            EgressEnforcement::None
        );
        assert_eq!(
            EgressEnforcement::verified(EgressMode::Routes),
            EgressEnforcement::GuestRoutes
        );
        assert_eq!(
            EgressEnforcement::verified(EgressMode::ProxyOnly),
            EgressEnforcement::GuestRoutesAndProxy
        );
    }

    #[test]
    fn codes_round_trip_and_unknown_codes_are_none() {
        for value in [
            EgressEnforcement::Unspecified,
            EgressEnforcement::None,
            EgressEnforcement::GuestRoutes,
            EgressEnforcement::GuestRoutesAndProxy,
        ] {
            assert_eq!(EgressEnforcement::from_code(value.to_code()), value);
        }
        assert_eq!(EgressEnforcement::from_code(9), EgressEnforcement::None);
        assert_eq!(EgressEnforcement::default(), EgressEnforcement::Unspecified);
    }

    #[test]
    fn the_snapshot_echoes_the_lists_but_never_the_proxy() {
        let policy = EgressPolicy::parse(PolicyInput {
            allow_out: vec!["Api.Example.com".to_owned()],
            deny_out: vec!["0.0.0.0/0".to_owned()],
            upstream: Some(UpstreamInput {
                address: "proxy.example.com:1080".to_owned(),
                ..UpstreamInput::default()
            }),
        })
        .unwrap();
        let snapshot =
            NetworkSnapshot::of(&policy, EgressEnforcement::GuestRoutesAndProxy, Some(4000));
        assert_eq!(snapshot.allow_out, ["Api.Example.com"]);
        assert_eq!(snapshot.deny_out, ["0.0.0.0/0"]);
        assert!(snapshot.egress_proxy_configured);
        assert_eq!(snapshot.local_proxy_port, Some(4000));
        assert!(!format!("{snapshot:?}").contains("proxy.example"));
    }
}
