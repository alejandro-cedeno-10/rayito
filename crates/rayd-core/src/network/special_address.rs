//! Which addresses are special, in one place: the transfer SSRF guard, the
//! proxy's target and upstream guards and the route probe each pick the
//! classes they refuse or skip from [`SpecialAddress::of`] instead of
//! keeping their own copy of the ranges. The classification never
//! canonicalises; each caller decides whether an IPv4-mapped or
//! IPv4-compatible address is judged as the IPv4 address it embeds
//! (`canonical_ip`).

use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};

pub const IMDS_V4: Ipv4Addr = Ipv4Addr::new(169, 254, 169, 254);
pub const IMDS_V6: Ipv6Addr = Ipv6Addr::new(0xfd00, 0x0ec2, 0, 0, 0, 0, 0, 0x0254);

/// The first class that matches, in declaration order: IMDS is reported as
/// `Imds` although `169.254.169.254` is also link-local.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SpecialAddress {
    Loopback,
    /// `0.0.0.0/8` for IPv4, `::` for IPv6.
    Unspecified,
    Imds,
    /// `169.254.0.0/16`, `fe80::/10`.
    LinkLocal,
    Multicast,
    /// `255.255.255.255`.
    Broadcast,
    Ordinary,
}

impl SpecialAddress {
    #[must_use]
    pub fn of(ip: IpAddr) -> Self {
        match ip {
            IpAddr::V4(v4) => {
                if v4.is_loopback() {
                    Self::Loopback
                } else if v4.octets()[0] == 0 {
                    Self::Unspecified
                } else if v4 == IMDS_V4 {
                    Self::Imds
                } else if v4.is_link_local() {
                    Self::LinkLocal
                } else if v4.is_multicast() {
                    Self::Multicast
                } else if v4.is_broadcast() {
                    Self::Broadcast
                } else {
                    Self::Ordinary
                }
            }
            IpAddr::V6(v6) => {
                if v6.is_loopback() {
                    Self::Loopback
                } else if v6.is_unspecified() {
                    Self::Unspecified
                } else if v6 == IMDS_V6 {
                    Self::Imds
                } else if v6.segments()[0] & 0xffc0 == 0xfe80 {
                    Self::LinkLocal
                } else if v6.is_multicast() {
                    Self::Multicast
                } else {
                    Self::Ordinary
                }
            }
        }
    }
}

/// The four hand-written predicates as they were before this module, kept
/// verbatim to prove the rewrite changes no answer.
#[cfg(test)]
mod oracle {
    use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};

    use super::super::cidr::canonical_ip;
    use super::{IMDS_V4, IMDS_V6};

    pub fn is_forbidden_address(address: IpAddr) -> bool {
        match address {
            IpAddr::V4(v4) => is_forbidden_v4(v4),
            IpAddr::V6(v6) => is_forbidden_v6(v6),
        }
    }

    fn is_forbidden_v4(address: Ipv4Addr) -> bool {
        address.is_loopback()
            || address.octets()[0] == 0
            || address.is_link_local()
            || address.is_multicast()
            || address.is_broadcast()
    }

    fn is_forbidden_v6(address: Ipv6Addr) -> bool {
        if address.is_loopback() || address.is_unspecified() || address.is_multicast() {
            return true;
        }
        if address.segments()[0] & 0xffc0 == 0xfe80 || address == IMDS_V6 {
            return true;
        }
        address.to_ipv4().is_some_and(is_forbidden_v4)
    }

    pub fn target_blocks(local: &[IpAddr], ip: IpAddr) -> bool {
        let local: Vec<IpAddr> = local.iter().copied().map(canonical_ip).collect();
        let ip = canonical_ip(ip);
        is_never_a_destination(ip) || is_link_local(ip) || local.contains(&ip)
    }

    pub fn upstream_blocks(ip: IpAddr) -> bool {
        let ip = canonical_ip(ip);
        let imds = match ip {
            IpAddr::V4(v4) => v4 == IMDS_V4,
            IpAddr::V6(v6) => v6 == IMDS_V6,
        };
        imds || is_loopback_unspecified_or_multicast(ip)
    }

    fn is_never_a_destination(ip: IpAddr) -> bool {
        let broadcast = matches!(ip, IpAddr::V4(v4) if v4.is_broadcast());
        broadcast || is_loopback_unspecified_or_multicast(ip)
    }

    fn is_loopback_unspecified_or_multicast(ip: IpAddr) -> bool {
        match ip {
            IpAddr::V4(v4) => v4.is_loopback() || v4.octets()[0] == 0 || v4.is_multicast(),
            IpAddr::V6(v6) => v6.is_loopback() || v6.is_unspecified() || v6.is_multicast(),
        }
    }

    fn is_link_local(ip: IpAddr) -> bool {
        match ip {
            IpAddr::V4(v4) => v4.is_link_local(),
            IpAddr::V6(v6) => v6.segments()[0] & 0xffc0 == 0xfe80 || v6 == IMDS_V6,
        }
    }

    pub fn answered_before_policy(ip: IpAddr) -> bool {
        match ip {
            IpAddr::V4(v4) => {
                v4.is_loopback() || v4.octets()[0] == 0 || v4.is_multicast() || v4.is_broadcast()
            }
            IpAddr::V6(v6) => v6.is_loopback() || v6.is_unspecified() || v6.is_multicast(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ip(text: &str) -> IpAddr {
        text.parse().unwrap()
    }

    /// The classification of every vector, without canonicalisation (the
    /// `::ffff:` and `::a.b.c.d` forms are ordinary IPv6 addresses here).
    const VECTORS: [(&str, SpecialAddress); 34] = [
        ("0.0.0.0", SpecialAddress::Unspecified),
        ("0.1.2.3", SpecialAddress::Unspecified),
        ("127.0.0.1", SpecialAddress::Loopback),
        ("127.255.255.254", SpecialAddress::Loopback),
        ("169.254.0.1", SpecialAddress::LinkLocal),
        ("169.254.169.254", SpecialAddress::Imds),
        ("224.0.0.1", SpecialAddress::Multicast),
        ("239.255.255.255", SpecialAddress::Multicast),
        ("255.255.255.255", SpecialAddress::Broadcast),
        ("10.0.0.1", SpecialAddress::Ordinary),
        ("100.64.0.1", SpecialAddress::Ordinary),
        ("172.16.0.1", SpecialAddress::Ordinary),
        ("192.168.1.1", SpecialAddress::Ordinary),
        ("8.8.8.8", SpecialAddress::Ordinary),
        ("203.0.113.7", SpecialAddress::Ordinary),
        ("::", SpecialAddress::Unspecified),
        ("::1", SpecialAddress::Loopback),
        ("::2", SpecialAddress::Ordinary),
        ("fe80::1", SpecialAddress::LinkLocal),
        ("febf::1", SpecialAddress::LinkLocal),
        ("fec0::1", SpecialAddress::Ordinary),
        ("fd00:ec2::254", SpecialAddress::Imds),
        ("fd00:ec2::253", SpecialAddress::Ordinary),
        ("fc00::1", SpecialAddress::Ordinary),
        ("ff02::1", SpecialAddress::Multicast),
        ("2001:db8::1", SpecialAddress::Ordinary),
        ("::ffff:127.0.0.1", SpecialAddress::Ordinary),
        ("::ffff:169.254.169.254", SpecialAddress::Ordinary),
        ("::ffff:10.1.2.3", SpecialAddress::Ordinary),
        ("::ffff:203.0.113.7", SpecialAddress::Ordinary),
        ("::127.0.0.1", SpecialAddress::Ordinary),
        ("::169.254.169.254", SpecialAddress::Ordinary),
        ("::224.0.0.1", SpecialAddress::Ordinary),
        ("::10.1.2.3", SpecialAddress::Ordinary),
    ];

    #[test]
    fn every_vector_has_its_class() {
        for (input, class) in VECTORS {
            assert_eq!(SpecialAddress::of(ip(input)), class, "{input}");
        }
    }

    #[test]
    fn the_policies_answer_as_the_hand_written_predicates_did() {
        use crate::network::guard::{TargetGuard, UpstreamGuard};
        use crate::network::probe::answered_before_policy;
        use crate::transfer::url_policy::is_forbidden_address;

        let local = [ip("10.0.1.5"), ip("fd12::5")];
        let guard = TargetGuard::new(local);
        let extra = [
            "10.0.1.5",
            "fd12::5",
            "::ffff:10.0.1.5",
            "::10.0.1.5",
            "10.0.1.6",
        ];
        let inputs = VECTORS.iter().map(|(input, _)| *input).chain(extra);
        for input in inputs {
            let address = ip(input);
            assert_eq!(
                is_forbidden_address(address),
                oracle::is_forbidden_address(address),
                "is_forbidden_address {input}"
            );
            assert_eq!(
                guard.blocks(address),
                oracle::target_blocks(&local, address),
                "TargetGuard {input}"
            );
            assert_eq!(
                UpstreamGuard.blocks(address),
                oracle::upstream_blocks(address),
                "UpstreamGuard {input}"
            );
            assert_eq!(
                answered_before_policy(address),
                oracle::answered_before_policy(address),
                "answered_before_policy {input}"
            );
        }
    }
}
