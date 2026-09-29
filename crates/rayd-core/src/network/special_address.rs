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
}
