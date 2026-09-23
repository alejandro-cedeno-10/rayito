//! CIDR arithmetic of the egress policy (ADR-012, design D3): parsing with
//! host bits masked and IPv4-mapped IPv6 folded into IPv4, address
//! membership after canonicalization (IPv4-mapped and IPv4-compatible
//! addresses as IPv4), exact prefix arithmetic, and the minimal cover of
//! `minuend \ subtrahend` that the route plan blackholes.

use std::cmp::Ordering;
use std::fmt;
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};

use thiserror::Error;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Family {
    V4,
    V6,
}

impl Family {
    pub const ALL: [Family; 2] = [Family::V4, Family::V6];

    #[must_use]
    pub fn of(ip: IpAddr) -> Self {
        match ip {
            IpAddr::V4(_) => Self::V4,
            IpAddr::V6(_) => Self::V6,
        }
    }

    #[must_use]
    pub fn width(self) -> u8 {
        match self {
            Self::V4 => 32,
            Self::V6 => 128,
        }
    }

    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::V4 => "v4",
            Self::V6 => "v6",
        }
    }
}

#[derive(Debug, Error, Clone, Copy, PartialEq, Eq)]
#[error("not a CIDR or an IP address")]
pub struct CidrError;

/// The kernel routes `::ffff:a.b.c.d` as `a.b.c.d`, so every rule compares
/// the IPv4 form. The deprecated IPv4-compatible `::a.b.c.d` folds the same
/// way, as the transfer URL policy judges it (`Ipv6Addr::to_ipv4`), so the
/// guard sees `::169.254.169.254` as IMDS and the proxy dials the address it
/// checked; `::` and `::1` stay IPv6's own unspecified and loopback.
#[must_use]
pub fn canonical_ip(ip: IpAddr) -> IpAddr {
    match ip {
        IpAddr::V6(v6) if v6.is_unspecified() || v6.is_loopback() => ip,
        IpAddr::V6(v6) => v6.to_ipv4().map_or(ip, IpAddr::V4),
        IpAddr::V4(_) => ip,
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct Cidr {
    network: IpAddr,
    prefix: u8,
}

impl Cidr {
    /// Host bits beyond `prefix` are cleared.
    pub fn new(ip: IpAddr, prefix: u8) -> Result<Self, CidrError> {
        let family = Family::of(ip);
        if prefix > family.width() {
            return Err(CidrError);
        }
        Ok(Self::from_bits(
            family,
            bits(ip) & mask(prefix, family.width()),
            prefix,
        ))
    }

    /// `a.b.c.d`, `a.b.c.d/n`, an IPv6 literal or `literal/n`; no brackets,
    /// no zone. `::ffff:a.b.c.d/n` with `n >= 96` becomes the IPv4 CIDR and
    /// any shorter mapped prefix is refused.
    pub fn parse(text: &str) -> Result<Self, CidrError> {
        let (address, prefix) = match text.split_once('/') {
            Some((address, prefix)) => (address, Some(parse_prefix(prefix)?)),
            None => (text, None),
        };
        let ip: IpAddr = address.parse().map_err(|_| CidrError)?;
        if let IpAddr::V6(v6) = ip
            && let Some(v4) = v6.to_ipv4_mapped()
        {
            return Self::from_mapped(v4, prefix);
        }
        Self::new(ip, prefix.unwrap_or_else(|| Family::of(ip).width()))
    }

    fn from_mapped(v4: Ipv4Addr, prefix: Option<u8>) -> Result<Self, CidrError> {
        let prefix = match prefix {
            None => 32,
            Some(prefix @ 96..=128) => prefix - 96,
            Some(_) => return Err(CidrError),
        };
        Self::new(IpAddr::V4(v4), prefix)
    }

    /// Every address of one family.
    #[must_use]
    pub fn everything(family: Family) -> Self {
        let network = match family {
            Family::V4 => IpAddr::V4(Ipv4Addr::UNSPECIFIED),
            Family::V6 => IpAddr::V6(Ipv6Addr::UNSPECIFIED),
        };
        Self { network, prefix: 0 }
    }

    #[must_use]
    pub fn network(&self) -> IpAddr {
        self.network
    }

    #[must_use]
    pub fn prefix(&self) -> u8 {
        self.prefix
    }

    #[must_use]
    pub fn family(&self) -> Family {
        Family::of(self.network)
    }

    #[must_use]
    pub fn is_host(&self) -> bool {
        self.prefix == self.family().width()
    }

    /// The network address plus one: the first host of a prefix, used as a
    /// probe destination.
    #[must_use]
    pub fn first_host(&self) -> IpAddr {
        if self.is_host() {
            return self.network;
        }
        let family = self.family();
        Self::from_bits(family, bits(self.network) + 1, family.width()).network
    }

    /// Membership of an address, after [`canonical_ip`].
    #[must_use]
    pub fn contains(&self, ip: IpAddr) -> bool {
        self.holds(canonical_ip(ip))
    }

    /// Prefix arithmetic compares the networks as written: no folding.
    #[must_use]
    pub fn covers(&self, other: &Cidr) -> bool {
        self.prefix <= other.prefix && self.holds(other.network)
    }

    fn holds(&self, ip: IpAddr) -> bool {
        let family = self.family();
        Family::of(ip) == family
            && bits(ip) & mask(self.prefix, family.width()) == bits(self.network)
    }

    fn intersects(&self, other: &Cidr) -> bool {
        self.covers(other) || other.covers(self)
    }

    fn halves(&self) -> Option<(Cidr, Cidr)> {
        let family = self.family();
        if self.is_host() {
            return None;
        }
        let child = self.prefix + 1;
        let high_bit = 1u128 << (family.width() - child);
        let low = Self::from_bits(family, bits(self.network), child);
        let high = Self::from_bits(family, bits(self.network) | high_bit, child);
        Some((low, high))
    }

    fn sibling_parent(&self, other: &Cidr) -> Option<Cidr> {
        if self == other
            || self.family() != other.family()
            || self.prefix != other.prefix
            || self.prefix == 0
        {
            return None;
        }
        let parent = Cidr::new(self.network, self.prefix - 1).ok()?;
        let other_parent = Cidr::new(other.network, other.prefix - 1).ok()?;
        (parent == other_parent).then_some(parent)
    }

    fn from_bits(family: Family, value: u128, prefix: u8) -> Self {
        let network = match family {
            Family::V4 => IpAddr::V4(Ipv4Addr::from(u32::try_from(value).unwrap_or(u32::MAX))),
            Family::V6 => IpAddr::V6(Ipv6Addr::from(value)),
        };
        Self { network, prefix }
    }

    fn sort_key(&self) -> (Family, u128, u8) {
        (self.family(), bits(self.network), self.prefix)
    }
}

impl Ord for Cidr {
    fn cmp(&self, other: &Self) -> Ordering {
        self.sort_key().cmp(&other.sort_key())
    }
}

impl PartialOrd for Cidr {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl fmt::Display for Cidr {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}/{}", self.network, self.prefix)
    }
}

/// A minimal, sorted, non-overlapping cover of `minuend \ subtrahend`, per
/// family. A prefix is only split into its halves where a subtrahend
/// falls strictly inside it, so the result stays proportional to the
/// number of holes rather than to the size of the address space.
#[must_use]
pub fn subtract(minuend: &[Cidr], subtrahend: &[Cidr]) -> Vec<Cidr> {
    let mut cover = Vec::new();
    for family in Family::ALL {
        let holes: Vec<Cidr> = of_family(subtrahend, family);
        let mut pieces = Vec::new();
        for block in minimal_cover(of_family(minuend, family)) {
            let relevant: Vec<Cidr> = holes
                .iter()
                .filter(|hole| hole.intersects(&block))
                .copied()
                .collect();
            carve(block, &relevant, &mut pieces);
        }
        cover.extend(minimal_cover(pieces));
    }
    cover
}

fn of_family(cidrs: &[Cidr], family: Family) -> Vec<Cidr> {
    cidrs
        .iter()
        .filter(|cidr| cidr.family() == family)
        .copied()
        .collect()
}

fn carve(block: Cidr, holes: &[Cidr], out: &mut Vec<Cidr>) {
    if holes.iter().any(|hole| hole.covers(&block)) {
        return;
    }
    if holes.is_empty() {
        out.push(block);
        return;
    }
    let Some((low, high)) = block.halves() else {
        return;
    };
    for half in [low, high] {
        let relevant: Vec<Cidr> = holes
            .iter()
            .filter(|hole| hole.intersects(&half))
            .copied()
            .collect();
        carve(half, &relevant, out);
    }
}

/// Sorted, with covered prefixes dropped and sibling pairs merged into
/// their parent until no pair is left.
#[must_use]
pub fn minimal_cover(mut cidrs: Vec<Cidr>) -> Vec<Cidr> {
    cidrs.sort_unstable();
    let mut kept: Vec<Cidr> = Vec::with_capacity(cidrs.len());
    for cidr in cidrs {
        if kept.last().is_some_and(|last| last.covers(&cidr)) {
            continue;
        }
        kept.push(cidr);
    }
    let mut merged: Vec<Cidr> = Vec::with_capacity(kept.len());
    for cidr in kept {
        let mut current = cidr;
        while let Some(parent) = merged.last().and_then(|last| last.sibling_parent(&current)) {
            merged.pop();
            current = parent;
        }
        merged.push(current);
    }
    merged
}

fn parse_prefix(text: &str) -> Result<u8, CidrError> {
    if text.is_empty() || text.len() > 3 || !text.bytes().all(|byte| byte.is_ascii_digit()) {
        return Err(CidrError);
    }
    text.parse().map_err(|_| CidrError)
}

fn bits(ip: IpAddr) -> u128 {
    match ip {
        IpAddr::V4(v4) => u128::from(u32::from(v4)),
        IpAddr::V6(v6) => u128::from(v6),
    }
}

fn mask(prefix: u8, width: u8) -> u128 {
    let full = if width >= 128 {
        u128::MAX
    } else {
        (1u128 << width) - 1
    };
    if prefix == 0 {
        0
    } else if prefix >= width {
        full
    } else {
        full & !(full >> prefix)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cidr(text: &str) -> Cidr {
        Cidr::parse(text).unwrap()
    }

    fn cidrs(texts: &[&str]) -> Vec<Cidr> {
        texts.iter().map(|text| cidr(text)).collect()
    }

    fn texts(cidrs: &[Cidr]) -> Vec<String> {
        cidrs.iter().map(ToString::to_string).collect()
    }

    fn ip(text: &str) -> IpAddr {
        text.parse().unwrap()
    }

    #[test]
    fn parse_table() {
        for (input, expected) in [
            ("10.1.2.3", "10.1.2.3/32"),
            ("10.1.2.3/8", "10.0.0.0/8"),
            ("0.0.0.0/0", "0.0.0.0/0"),
            ("192.168.1.255/24", "192.168.1.0/24"),
            ("2001:db8::1", "2001:db8::1/128"),
            ("2001:db8::1/32", "2001:db8::/32"),
            ("::/0", "::/0"),
            ("::ffff:10.1.2.3", "10.1.2.3/32"),
            ("::ffff:10.1.2.3/120", "10.1.2.0/24"),
            ("::ffff:0.0.0.0/96", "0.0.0.0/0"),
        ] {
            assert_eq!(cidr(input).to_string(), expected, "{input}");
        }
    }

    #[test]
    fn canonical_ip_folds_mapped_and_compatible_ipv4() {
        for (input, expected) in [
            ("10.1.2.3", "10.1.2.3"),
            ("::ffff:10.1.2.3", "10.1.2.3"),
            ("::ffff:169.254.169.254", "169.254.169.254"),
            ("::10.1.2.3", "10.1.2.3"),
            ("::169.254.169.254", "169.254.169.254"),
            ("::127.0.0.1", "127.0.0.1"),
            ("::0.0.0.2", "0.0.0.2"),
            ("::", "::"),
            ("::1", "::1"),
            ("2001:db8::1", "2001:db8::1"),
            ("64:ff9b::10.1.2.3", "64:ff9b::a01:203"),
            ("::1:10.1.2.3", "::1:a01:203"),
            ("fe80::10.1.2.3", "fe80::a01:203"),
        ] {
            assert_eq!(canonical_ip(ip(input)), ip(expected), "{input}");
        }
    }

    /// The egress guard and the transfer URL policy judge an IPv4-mapped or
    /// IPv4-compatible address as the IPv4 address it embeds.
    #[test]
    fn canonical_ip_agrees_with_the_transfer_policy() {
        use crate::transfer::url_policy::is_forbidden_address;
        for input in [
            "::169.254.169.254",
            "::ffff:169.254.169.254",
            "::127.0.0.1",
            "::ffff:127.0.0.1",
            "::224.0.0.1",
            "::10.1.2.3",
            "::ffff:203.0.113.7",
            "::",
            "::1",
        ] {
            let address = ip(input);
            assert_eq!(
                is_forbidden_address(canonical_ip(address)),
                is_forbidden_address(address),
                "{input}"
            );
        }
    }

    #[test]
    fn parse_rejects_malformed_forms() {
        for input in [
            "",
            "10.0.0.0/33",
            "2001:db8::/129",
            "[2001:db8::1]",
            "[2001:db8::1]/64",
            "10.0.0.0/",
            "10.0.0.0/+8",
            "10.0.0.0/ 8",
            "10.0.0.0/8/8",
            "10.0.0",
            "256.0.0.1",
            "fe80::1%eth0",
            "::ffff:10.1.2.3/95",
            "example.com",
            " 10.0.0.1",
        ] {
            assert_eq!(Cidr::parse(input), Err(CidrError), "{input:?}");
        }
    }

    #[test]
    fn contains_canonicalizes_mapped_addresses() {
        let net = cidr("10.0.0.0/8");
        assert!(net.contains(ip("10.255.0.1")));
        assert!(net.contains(ip("::ffff:10.1.1.1")));
        assert!(!net.contains(ip("11.0.0.1")));
        assert!(!net.contains(ip("2001:db8::1")));
        assert!(Cidr::everything(Family::V6).contains(ip("::1")));
        assert!(!Cidr::everything(Family::V6).contains(ip("::ffff:1.2.3.4")));
        assert!(Cidr::everything(Family::V4).contains(ip("::ffff:1.2.3.4")));
        assert!(net.contains(ip("::10.1.1.1")));
        assert!(!Cidr::everything(Family::V6).contains(ip("::10.1.1.1")));
        assert_eq!(cidr("2001:db8::/32").family(), Family::V6);
    }

    /// Only addresses fold; an IPv6 prefix written inside `::/96` keeps its
    /// place in the IPv6 arithmetic.
    #[test]
    fn covers_compares_prefixes_without_folding() {
        let everything = Cidr::everything(Family::V6);
        let compatible = cidr("::a00:0/104");
        assert_eq!(compatible.family(), Family::V6);
        assert!(everything.covers(&compatible));
        assert!(!compatible.covers(&everything));
        assert!(!cidr("10.0.0.0/8").covers(&compatible));
        let cover = subtract(&[everything], &[compatible]);
        assert!(cover.iter().all(|piece| !compatible.covers(piece)));
        assert!(
            cover
                .iter()
                .any(|piece| piece.covers(&cidr("2001:db8::/32")))
        );
    }

    #[test]
    fn first_host_is_the_network_for_host_prefixes() {
        assert_eq!(cidr("10.0.0.0/8").first_host(), ip("10.0.0.1"));
        assert_eq!(cidr("10.0.0.7/32").first_host(), ip("10.0.0.7"));
        assert_eq!(cidr("2001:db8::/32").first_host(), ip("2001:db8::1"));
    }

    #[test]
    fn subtract_disjoint_keeps_the_minuend() {
        let cover = subtract(&cidrs(&["10.0.0.0/8"]), &cidrs(&["192.168.0.0/16"]));
        assert_eq!(texts(&cover), ["10.0.0.0/8"]);
    }

    #[test]
    fn subtract_a_broader_allow_leaves_nothing() {
        let cover = subtract(&cidrs(&["10.1.0.0/16"]), &cidrs(&["10.0.0.0/8"]));
        assert!(cover.is_empty());
        let equal = subtract(&cidrs(&["10.0.0.0/8"]), &cidrs(&["10.0.0.0/8"]));
        assert!(equal.is_empty());
    }

    #[test]
    fn subtract_a_nested_hole_splits_only_along_its_path() {
        let cover = subtract(&cidrs(&["10.0.0.0/8"]), &cidrs(&["10.0.0.0/10"]));
        assert_eq!(texts(&cover), ["10.64.0.0/10", "10.128.0.0/9"]);
        let host = subtract(&cidrs(&["198.51.100.0/24"]), &cidrs(&["198.51.100.7/32"]));
        assert_eq!(host.len(), 8);
        assert!(host.iter().all(|piece| !piece.contains(ip("198.51.100.7"))));
        assert!(host.iter().any(|piece| piece.contains(ip("198.51.100.8"))));
        let covered: u32 = host.iter().map(|piece| 1u32 << (32 - piece.prefix())).sum();
        assert_eq!(covered, 255);
    }

    #[test]
    fn subtract_several_holes_and_overlapping_minuends() {
        let cover = subtract(
            &cidrs(&["10.0.0.0/8", "10.1.0.0/16"]),
            &cidrs(&["10.0.0.0/9", "10.192.0.0/10"]),
        );
        assert_eq!(texts(&cover), ["10.128.0.0/10"]);
    }

    #[test]
    fn subtract_is_minimal_and_sorted() {
        let cover = subtract(&cidrs(&["10.0.0.0/9", "10.128.0.0/9", "9.0.0.0/8"]), &[]);
        assert_eq!(texts(&cover), ["9.0.0.0/8", "10.0.0.0/8"]);
        let merged = minimal_cover(cidrs(&["10.0.0.0/10", "10.64.0.0/10", "10.128.0.0/9"]));
        assert_eq!(texts(&merged), ["10.0.0.0/8"]);
    }

    #[test]
    fn subtract_works_per_family() {
        let cover = subtract(
            &[Cidr::everything(Family::V4), Cidr::everything(Family::V6)],
            &cidrs(&["2001:db8::/32", "0.0.0.0/1"]),
        );
        assert_eq!(cover[0].to_string(), "128.0.0.0/1");
        assert!(cover.iter().all(|piece| !piece.contains(ip("2001:db8::5"))));
        assert!(cover.iter().any(|piece| piece.contains(ip("2001:db9::5"))));
        assert_eq!(
            cover
                .iter()
                .filter(|piece| piece.family() == Family::V6)
                .count(),
            32
        );
    }
}
