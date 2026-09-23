//! Which prefixes the uid-scoped routing table blackholes (design D4).
//!
//! Priority map, lowest first: `local` (0), the IMDS rule (100, table
//! 100, never touched here), the emergency deny-all (149, table 103, only
//! during recovery), the two policy slots (150/151, tables 101/102) and
//! `main` (32766). A lookup that finds no route in a policy table falls
//! through to `main`, so only blackholes are needed: after `deny \ allow`
//! everything else is allowed, and allow-over-deny holds even when the
//! allow prefix is broader than the deny prefix, which longest-prefix
//! `throw` routes would get wrong.

use super::EGRESS_MAX_ROUTES_PER_FAMILY;
use super::cidr::{Cidr, Family, subtract};
use super::error::NetworkError;
use super::policy::{EgressMode, EgressPolicy};

/// The sandbox user and anything it could become; never root nor the
/// platform agent's uids 991-994 (Q48). Shared with the IMDS block.
pub const SANDBOX_UID_RANGE: &str = "1000-65535";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Slot {
    A,
    B,
    Emergency,
}

impl Slot {
    pub const POLICY: [Slot; 2] = [Slot::A, Slot::B];

    #[must_use]
    pub fn table(self) -> u32 {
        match self {
            Self::A => 101,
            Self::B => 102,
            Self::Emergency => 103,
        }
    }

    #[must_use]
    pub fn priority(self) -> u32 {
        match self {
            Self::A => 150,
            Self::B => 151,
            Self::Emergency => 149,
        }
    }

    /// The policy slot a swap fills next.
    #[must_use]
    pub fn other(self) -> Self {
        match self {
            Self::A => Self::B,
            Self::B | Self::Emergency => Self::A,
        }
    }
}

/// The families a plan manages: IPv4 always, IPv6 when the guest has it
/// (and then its steps are mandatory, never best effort).
#[must_use]
pub fn managed_families(ipv6_present: bool) -> &'static [Family] {
    if ipv6_present {
        &Family::ALL
    } else {
        &[Family::V4]
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RoutePlan {
    v4: Vec<Cidr>,
    v6: Vec<Cidr>,
    ipv6: bool,
}

impl RoutePlan {
    /// `None` for an unrestricted policy (no rule, no table); everything
    /// blackholed in proxy-only mode; `deny \ allow` in routes mode.
    pub fn for_policy(
        policy: &EgressPolicy,
        ipv6_present: bool,
    ) -> Result<Option<Self>, NetworkError> {
        let cover = match policy.mode() {
            EgressMode::Unrestricted => return Ok(None),
            EgressMode::ProxyOnly => Family::ALL.into_iter().map(Cidr::everything).collect(),
            EgressMode::Routes => subtract(policy.deny_nets(), policy.allow_nets()),
        };
        let (v4, v6): (Vec<Cidr>, Vec<Cidr>) = cover
            .into_iter()
            .partition(|cidr| cidr.family() == Family::V4);
        if v4.len() > EGRESS_MAX_ROUTES_PER_FAMILY || v6.len() > EGRESS_MAX_ROUTES_PER_FAMILY {
            return Err(NetworkError::PolicyTooComplex);
        }
        Ok(Some(Self::from_parts(v4, v6, ipv6_present)))
    }

    #[must_use]
    pub fn deny_all(ipv6_present: bool) -> Self {
        Self::from_parts(
            vec![Cidr::everything(Family::V4)],
            vec![Cidr::everything(Family::V6)],
            ipv6_present,
        )
    }

    fn from_parts(v4: Vec<Cidr>, v6: Vec<Cidr>, ipv6: bool) -> Self {
        Self {
            v4,
            v6: if ipv6 { v6 } else { Vec::new() },
            ipv6,
        }
    }

    #[must_use]
    pub fn prefixes(&self, family: Family) -> &[Cidr] {
        match family {
            Family::V4 => &self.v4,
            Family::V6 => &self.v6,
        }
    }

    #[must_use]
    pub fn ipv6(&self) -> bool {
        self.ipv6
    }

    #[must_use]
    pub fn families(&self) -> &'static [Family] {
        managed_families(self.ipv6)
    }

    #[must_use]
    pub fn denies_all(&self, family: Family) -> bool {
        self.prefixes(family) == [Cidr::everything(family)]
    }

    #[must_use]
    pub fn covers(&self, ip: std::net::IpAddr) -> bool {
        self.v4.iter().chain(&self.v6).any(|cidr| cidr.contains(ip))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::network::entry::ALL_TRAFFIC;
    use crate::network::policy::{PolicyInput, UpstreamInput};

    fn policy(allow: &[&str], deny: &[&str]) -> EgressPolicy {
        EgressPolicy::parse(PolicyInput {
            allow_out: allow.iter().map(|entry| (*entry).to_owned()).collect(),
            deny_out: deny.iter().map(|entry| (*entry).to_owned()).collect(),
            upstream: None,
        })
        .unwrap()
    }

    fn texts(cidrs: &[Cidr]) -> Vec<String> {
        cidrs.iter().map(ToString::to_string).collect()
    }

    #[test]
    fn slots_have_fixed_tables_and_priorities() {
        assert_eq!((Slot::A.table(), Slot::A.priority()), (101, 150));
        assert_eq!((Slot::B.table(), Slot::B.priority()), (102, 151));
        assert_eq!(
            (Slot::Emergency.table(), Slot::Emergency.priority()),
            (103, 149)
        );
        assert_eq!(Slot::A.other(), Slot::B);
        assert_eq!(Slot::B.other(), Slot::A);
    }

    #[test]
    fn plans_for_the_three_modes() {
        assert_eq!(
            RoutePlan::for_policy(&policy(&["example.com"], &[]), true),
            Ok(None)
        );
        let routes =
            RoutePlan::for_policy(&policy(&["198.51.100.7/32"], &["198.51.100.0/24"]), true)
                .unwrap()
                .unwrap();
        assert_eq!(routes.prefixes(Family::V4).len(), 8);
        assert!(routes.prefixes(Family::V6).is_empty());
        assert!(!routes.covers("198.51.100.7".parse().unwrap()));
        assert!(routes.covers("198.51.100.8".parse().unwrap()));
        let proxy = RoutePlan::for_policy(&policy(&["api.example.com"], &[ALL_TRAFFIC]), true)
            .unwrap()
            .unwrap();
        assert_eq!(texts(proxy.prefixes(Family::V4)), ["0.0.0.0/0"]);
        assert_eq!(texts(proxy.prefixes(Family::V6)), ["::/0"]);
        let chained = EgressPolicy::parse(PolicyInput {
            upstream: Some(UpstreamInput {
                address: "203.0.113.5:1080".to_owned(),
                ..UpstreamInput::default()
            }),
            ..PolicyInput::default()
        })
        .unwrap();
        assert!(
            RoutePlan::for_policy(&chained, false)
                .unwrap()
                .unwrap()
                .denies_all(Family::V4)
        );
    }

    #[test]
    fn ipv6_is_dropped_when_absent() {
        let all = policy(&[], &[ALL_TRAFFIC]);
        let with_v6 = RoutePlan::for_policy(&all, true).unwrap().unwrap();
        assert!(with_v6.denies_all(Family::V6));
        assert_eq!(with_v6.families(), [Family::V4, Family::V6]);
        let without = RoutePlan::for_policy(&all, false).unwrap().unwrap();
        assert!(without.prefixes(Family::V6).is_empty());
        assert_eq!(without.families(), [Family::V4]);
        assert_eq!(RoutePlan::deny_all(false), without);
    }

    #[test]
    fn more_than_4096_prefixes_is_too_complex() {
        let holes: Vec<String> = (0..64u32)
            .map(|index| format!("2001:db8:{index:x}::1"))
            .collect();
        let denied = EgressPolicy::parse(PolicyInput {
            allow_out: holes,
            deny_out: vec!["::/0".to_owned()],
            upstream: None,
        })
        .unwrap();
        assert_eq!(
            RoutePlan::for_policy(&denied, true),
            Err(NetworkError::PolicyTooComplex)
        );
        let fits: Vec<String> = (0..=127u32)
            .map(|index| format!("10.{index}.0.1/32"))
            .collect();
        let fitting = EgressPolicy::parse(PolicyInput {
            allow_out: fits,
            deny_out: vec!["10.0.0.0/8".to_owned()],
            upstream: None,
        })
        .unwrap();
        let plan = RoutePlan::for_policy(&fitting, true).unwrap().unwrap();
        assert!(plan.prefixes(Family::V4).len() <= EGRESS_MAX_ROUTES_PER_FAMILY);
    }
}
