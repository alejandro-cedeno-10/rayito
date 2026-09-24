//! Addresses the local proxy never dials, whatever the policy says (design
//! D9, D10). The proxy runs as root, so without this guard a uid-1000
//! client could use it to reach IMDS (T1), the hooks listener on `:9000`
//! or `rayd`'s own `:8080` through a guest address, all of which the
//! routes alone would never let it touch.

use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};

use super::cidr::canonical_ip;

pub const IMDS_V4: Ipv4Addr = Ipv4Addr::new(169, 254, 169, 254);
pub const IMDS_V6: Ipv6Addr = Ipv6Addr::new(0xfd00, 0x0ec2, 0, 0, 0, 0, 0, 0x0254);

/// The client-facing guard: loopback, unspecified, link-local (IMDS
/// included), multicast, broadcast and every address currently assigned to
/// a guest interface, checked after IPv4-mapped canonicalization.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct TargetGuard {
    local: Vec<IpAddr>,
}

impl TargetGuard {
    #[must_use]
    pub fn new(local: impl IntoIterator<Item = IpAddr>) -> Self {
        Self {
            local: local.into_iter().map(canonical_ip).collect(),
        }
    }

    #[must_use]
    pub fn blocks(&self, ip: IpAddr) -> bool {
        let ip = canonical_ip(ip);
        is_never_a_destination(ip) || is_link_local(ip) || self.local.contains(&ip)
    }
}

/// The operator's upstream is configuration delivered by the
/// token-authenticated RPC, so private, link-local and own addresses stay
/// allowed (the acceptance runs its recording server on the VM's own
/// address); only loopback, unspecified, multicast and the two IMDS
/// endpoints are refused.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct UpstreamGuard;

impl UpstreamGuard {
    #[must_use]
    pub fn blocks(self, ip: IpAddr) -> bool {
        let ip = canonical_ip(ip);
        let imds = match ip {
            IpAddr::V4(v4) => v4 == IMDS_V4,
            IpAddr::V6(v6) => v6 == IMDS_V6,
        };
        imds || is_loopback_unspecified_or_multicast(ip)
    }
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

#[cfg(test)]
mod tests {
    use super::*;

    fn ip(text: &str) -> IpAddr {
        text.parse().unwrap()
    }

    #[test]
    fn target_guard_blocks_every_special_range() {
        let guard = TargetGuard::new([ip("10.0.1.5"), ip("fd12::5")]);
        for blocked in [
            "127.0.0.1",
            "127.200.0.1",
            "::1",
            "0.0.0.0",
            "0.1.2.3",
            "::",
            "169.254.169.254",
            "169.254.0.1",
            "::ffff:169.254.169.254",
            "::ffff:127.0.0.1",
            "fe80::1",
            "febf::1",
            "fd00:ec2::254",
            "224.0.0.1",
            "239.255.255.250",
            "ff02::1",
            "255.255.255.255",
            "10.0.1.5",
            "::ffff:10.0.1.5",
            "::10.0.1.5",
            "::169.254.169.254",
            "::127.0.0.1",
            "fd12::5",
        ] {
            assert!(guard.blocks(ip(blocked)), "{blocked}");
        }
        for allowed in [
            "1.1.1.1",
            "10.0.1.6",
            "192.168.0.1",
            "203.0.113.7",
            "2606:4700:4700::1111",
            "fd00:ec2::253",
            "fec0::1",
        ] {
            assert!(!guard.blocks(ip(allowed)), "{allowed}");
        }
    }

    #[test]
    fn upstream_guard_allows_private_and_own_addresses_but_not_imds_or_loopback() {
        let guard = UpstreamGuard;
        for blocked in [
            "127.0.0.1",
            "::1",
            "0.0.0.0",
            "::",
            "169.254.169.254",
            "::ffff:169.254.169.254",
            "::169.254.169.254",
            "::127.0.0.1",
            "fd00:ec2::254",
            "224.0.0.1",
            "ff02::1",
        ] {
            assert!(guard.blocks(ip(blocked)), "{blocked}");
        }
        for allowed in [
            "10.0.1.5",
            "192.168.1.1",
            "169.254.10.1",
            "fe80::1",
            "203.0.113.9",
        ] {
            assert!(!guard.blocks(ip(allowed)), "{allowed}");
        }
    }
}
