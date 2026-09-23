//! The route probe that verifies an installed plan (design D6): which
//! destinations to ask `ip route get <addr> uid 1000` about and what each
//! must answer, how to read that answer, and how to read `ip rule show`
//! and `ip route show table <T>`. No packet is ever sent.
//!
//! A blocked sample prefers a well-known public resolver the prefix
//! contains over the prefix's first host, and skips loopback,
//! unspecified, multicast and broadcast candidates: the `local` table (or
//! the kernel's refusal of those destinations) would answer for them
//! before any policy rule is consulted.

use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};

use super::cidr::{Cidr, Family};
use super::policy::{EgressMode, EgressPolicy, IpVerdict};
use super::route_plan::{RoutePlan, SANDBOX_UID_RANGE, Slot};

pub const MAX_BLOCKED_SAMPLES: usize = 3;
pub const LOOPBACK_SAMPLE: IpAddr = IpAddr::V4(Ipv4Addr::LOCALHOST);
pub const PROXY_BLOCKED_V4: IpAddr = IpAddr::V4(Ipv4Addr::new(1, 1, 1, 1));
pub const PROXY_BLOCKED_V6: IpAddr =
    IpAddr::V6(Ipv6Addr::new(0x2606, 0x4700, 0x4700, 0, 0, 0, 0, 0x1111));
/// Tried in order for the `Routable` sample.
pub const ROUTABLE_CANDIDATES: [Ipv4Addr; 4] = [
    Ipv4Addr::new(1, 1, 1, 1),
    Ipv4Addr::new(8, 8, 8, 8),
    Ipv4Addr::new(9, 9, 9, 9),
    Ipv4Addr::new(208, 67, 222, 222),
];
const PREFERRED_BLOCKED_V6: [Ipv6Addr; 2] = [
    Ipv6Addr::new(0x2606, 0x4700, 0x4700, 0, 0, 0, 0, 0x1111),
    Ipv6Addr::new(0x2001, 0x4860, 0x4860, 0, 0, 0, 0, 0x8888),
];

/// What `ip route get` says about one destination; also what a sample
/// expects (`Unknown` is never expected, so it always fails).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RouteVerdict {
    Local,
    Blocked,
    Routable,
    Unknown,
}

impl RouteVerdict {
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Local => "local",
            Self::Blocked => "blocked",
            Self::Routable => "routable",
            Self::Unknown => "unknown",
        }
    }
}

/// A blackhole makes the kernel refuse the lookup (non-zero exit);
/// anything the parser does not recognise is `Unknown` and fails the
/// verification (fail closed).
#[must_use]
pub fn classify_route_get(exit_code: i32, stdout: &str) -> RouteVerdict {
    let answer = stdout.trim_start();
    if exit_code != 0
        || ["blackhole", "unreachable", "prohibit"]
            .iter()
            .any(|kind| answer.starts_with(kind))
    {
        RouteVerdict::Blocked
    } else if answer.starts_with("local ") {
        RouteVerdict::Local
    } else if answer.contains(" dev ") {
        RouteVerdict::Routable
    } else {
        RouteVerdict::Unknown
    }
}

/// The destinations to probe for `plan` and the verdict each must get.
#[must_use]
pub fn samples(policy: &EgressPolicy, plan: &RoutePlan) -> Vec<(IpAddr, RouteVerdict)> {
    let mut samples = vec![(LOOPBACK_SAMPLE, RouteVerdict::Local)];
    if policy.mode() == EgressMode::ProxyOnly {
        samples.push((PROXY_BLOCKED_V4, RouteVerdict::Blocked));
        if plan.ipv6() {
            samples.push((PROXY_BLOCKED_V6, RouteVerdict::Blocked));
        }
        return samples;
    }
    let blocked = Family::ALL
        .iter()
        .flat_map(|family| plan.prefixes(*family))
        .filter_map(blocked_sample)
        .take(MAX_BLOCKED_SAMPLES)
        .map(|ip| (ip, RouteVerdict::Blocked));
    samples.extend(blocked);
    if !plan.denies_all(Family::V4)
        && let Some(ip) = ROUTABLE_CANDIDATES
            .iter()
            .map(|ip| IpAddr::V4(*ip))
            .find(|ip| policy.ip_verdict(*ip) == IpVerdict::Allow && !plan.covers(*ip))
    {
        samples.push((ip, RouteVerdict::Routable));
    }
    samples
}

fn blocked_sample(prefix: &Cidr) -> Option<IpAddr> {
    let preferred = ROUTABLE_CANDIDATES
        .iter()
        .map(|ip| IpAddr::V4(*ip))
        .chain(PREFERRED_BLOCKED_V6.iter().map(|ip| IpAddr::V6(*ip)))
        .find(|ip| prefix.contains(*ip));
    let candidate = preferred.unwrap_or_else(|| prefix.first_host());
    (!answered_before_policy(candidate)).then_some(candidate)
}

fn answered_before_policy(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(v4) => {
            v4.is_loopback() || v4.octets()[0] == 0 || v4.is_multicast() || v4.is_broadcast()
        }
        IpAddr::V6(v6) => v6.is_loopback() || v6.is_unspecified() || v6.is_multicast(),
    }
}

/// `(priority, table)` of every `from all uidrange 1000-65535 lookup <T>`
/// rule in `ip rule show` output.
#[must_use]
pub fn uid_rules(stdout: &str) -> Vec<(u32, u32)> {
    stdout.lines().filter_map(parse_uid_rule).collect()
}

fn parse_uid_rule(line: &str) -> Option<(u32, u32)> {
    let (priority, rest) = line.trim().split_once(':')?;
    let tokens: Vec<&str> = rest.split_whitespace().collect();
    match tokens.as_slice() {
        ["from", "all", "uidrange", range, "lookup", table, ..] if *range == SANDBOX_UID_RANGE => {
            Some((priority.trim().parse().ok()?, table.parse().ok()?))
        }
        _ => None,
    }
}

#[must_use]
pub fn rule_present(stdout: &str, slot: Slot) -> bool {
    uid_rules(stdout).contains(&(slot.priority(), slot.table()))
}

/// The priorities of this module's rules (the two policy slots and the
/// emergency slot); the IMDS rule at 100 is not one of them.
#[must_use]
pub fn policy_rule_priorities(stdout: &str) -> Vec<u32> {
    let ours = [Slot::A, Slot::B, Slot::Emergency];
    uid_rules(stdout)
        .into_iter()
        .filter(|(priority, table)| {
            ours.iter()
                .any(|slot| slot.priority() == *priority && slot.table() == *table)
        })
        .map(|(priority, _)| priority)
        .collect()
}

/// Every address assigned to a guest interface, from `ip -o addr show`
/// (the token after `inet`/`inet6`, prefix length dropped). The proxy's
/// target guard refuses them so it never dials the guest's own listeners.
#[must_use]
pub fn interface_addresses(stdout: &str) -> Vec<IpAddr> {
    stdout
        .lines()
        .filter_map(|line| {
            let mut tokens = line.split_whitespace();
            tokens.find(|token| *token == "inet" || *token == "inet6")?;
            let address = tokens.next()?;
            let address = address.split_once('/').map_or(address, |(ip, _)| ip);
            address.parse().ok()
        })
        .collect()
}

/// Lines of `ip route show table <T>` that are blackholes.
#[must_use]
pub fn blackhole_count(stdout: &str) -> usize {
    stdout
        .lines()
        .filter(|line| line.trim_start().starts_with("blackhole"))
        .count()
}

/// The kernel's extack text when a routing table was never created. A
/// fresh guest has none of the policy tables, and since Linux 5.x both
/// `ip route flush table <T>` and `ip route show table <T>` fail with it
/// (exit 2) instead of treating the table as empty.
pub const MISSING_TABLE_MESSAGE: &str = "FIB table does not exist";

/// Whether a failed `ip route flush|show table <T>` only means the table
/// was never created, which for a flush or a count is an empty table.
/// Reads `ip`'s stderr for that fixed phrase and nothing else.
#[must_use]
pub fn table_missing(exit_code: i32, stderr: &str) -> bool {
    exit_code != 0 && stderr.contains(MISSING_TABLE_MESSAGE)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::network::entry::ALL_TRAFFIC;
    use crate::network::policy::PolicyInput;

    fn policy(allow: &[&str], deny: &[&str]) -> EgressPolicy {
        EgressPolicy::parse(PolicyInput {
            allow_out: allow.iter().map(|entry| (*entry).to_owned()).collect(),
            deny_out: deny.iter().map(|entry| (*entry).to_owned()).collect(),
            upstream: None,
        })
        .unwrap()
    }

    fn ip(text: &str) -> IpAddr {
        text.parse().unwrap()
    }

    fn samples_for(allow: &[&str], deny: &[&str], ipv6: bool) -> Vec<(IpAddr, RouteVerdict)> {
        let policy = policy(allow, deny);
        let plan = RoutePlan::for_policy(&policy, ipv6).unwrap().unwrap();
        samples(&policy, &plan)
    }

    #[test]
    fn classify_route_get_table() {
        for (code, stdout, expected) in [
            (2, "", RouteVerdict::Blocked),
            (
                0,
                "blackhole 1.1.1.1 table 101 uid 1000 \n",
                RouteVerdict::Blocked,
            ),
            (0, "unreachable 1.1.1.1 table 101\n", RouteVerdict::Blocked),
            (0, "prohibit 1.1.1.1 table 101\n", RouteVerdict::Blocked),
            (
                0,
                "local 127.0.0.1 dev lo table local src 127.0.0.1 uid 1000 \n    cache <local> \n",
                RouteVerdict::Local,
            ),
            (
                0,
                "1.1.1.1 via 169.254.0.1 dev eth0 src 169.254.0.5 uid 0 \n    cache \n",
                RouteVerdict::Routable,
            ),
            (
                0,
                "2606:4700:4700::1111 from :: via fe80::1 dev eth0 proto ra src fd12::5 metric 1024 pref medium\n",
                RouteVerdict::Routable,
            ),
            (
                0,
                "broadcast 255.255.255.255 table local\n",
                RouteVerdict::Unknown,
            ),
            (0, "", RouteVerdict::Unknown),
        ] {
            assert_eq!(
                classify_route_get(code, stdout),
                expected,
                "{code} {stdout:?}"
            );
        }
    }

    #[test]
    fn deny_all_samples_loopback_and_public_resolvers() {
        assert_eq!(
            samples_for(&[], &[ALL_TRAFFIC], true),
            [
                (ip("127.0.0.1"), RouteVerdict::Local),
                (ip("1.1.1.1"), RouteVerdict::Blocked),
                (ip("2606:4700:4700::1111"), RouteVerdict::Blocked),
            ]
        );
        assert_eq!(
            samples_for(&[], &[ALL_TRAFFIC], false),
            [
                (ip("127.0.0.1"), RouteVerdict::Local),
                (ip("1.1.1.1"), RouteVerdict::Blocked),
            ]
        );
    }

    #[test]
    fn routes_samples_probe_the_holes_and_one_allowed_resolver() {
        let got = samples_for(&["198.51.100.7/32"], &["198.51.100.0/24"], false);
        assert_eq!(got[0], (ip("127.0.0.1"), RouteVerdict::Local));
        let blocked: Vec<IpAddr> = got
            .iter()
            .filter(|(_, verdict)| *verdict == RouteVerdict::Blocked)
            .map(|(ip, _)| *ip)
            .collect();
        assert_eq!(
            blocked,
            [ip("198.51.100.1"), ip("198.51.100.5"), ip("198.51.100.6")]
        );
        assert!(!blocked.contains(&ip("198.51.100.7")));
        assert_eq!(got.last(), Some(&(ip("1.1.1.1"), RouteVerdict::Routable)));
        let skip_denied = samples_for(&[], &["1.1.1.1/32", "8.8.8.8/32"], false);
        assert_eq!(
            skip_denied.last(),
            Some(&(ip("9.9.9.9"), RouteVerdict::Routable))
        );
    }

    #[test]
    fn samples_never_pick_addresses_the_local_table_answers() {
        let got = samples_for(&["1.1.1.1/32"], &[ALL_TRAFFIC], false);
        assert!(
            got[1..].iter().all(|(ip, _)| !answered_before_policy(*ip)),
            "{got:?}"
        );
        assert_eq!(got.last(), Some(&(ip("1.1.1.1"), RouteVerdict::Routable)));
        assert_eq!(
            got.iter()
                .filter(|(_, verdict)| *verdict == RouteVerdict::Blocked)
                .count(),
            MAX_BLOCKED_SAMPLES
        );
        let loopback_only = samples_for(&[], &["127.0.0.0/8"], false);
        assert_eq!(
            loopback_only,
            [
                (ip("127.0.0.1"), RouteVerdict::Local),
                (ip("1.1.1.1"), RouteVerdict::Routable),
            ]
        );
    }

    #[test]
    fn proxy_only_samples() {
        assert_eq!(
            samples_for(&["api.example.com"], &[ALL_TRAFFIC], true),
            [
                (ip("127.0.0.1"), RouteVerdict::Local),
                (ip("1.1.1.1"), RouteVerdict::Blocked),
                (ip("2606:4700:4700::1111"), RouteVerdict::Blocked),
            ]
        );
        assert_eq!(
            samples_for(&["api.example.com"], &[ALL_TRAFFIC], false).len(),
            2
        );
    }

    #[test]
    fn rule_show_parsing() {
        let stdout = "0:\tfrom all lookup local\n\
100:\tfrom all uidrange 1000-65535 lookup 100\n\
150:\tfrom all uidrange 1000-65535 lookup 101 proto boot\n\
151:\tfrom all uidrange 1000-65535 lookup 102\n\
160:\tfrom all uidrange 0-999 lookup 101\n\
32766:\tfrom all lookup main\n\
32767:\tfrom all lookup default\n";
        assert_eq!(uid_rules(stdout), [(100, 100), (150, 101), (151, 102)]);
        assert!(rule_present(stdout, Slot::A));
        assert!(rule_present(stdout, Slot::B));
        assert!(!rule_present(stdout, Slot::Emergency));
        assert_eq!(policy_rule_priorities(stdout), [150, 151]);
        assert!(!rule_present(
            "150:\tfrom all uidrange 1000-65535 lookup 102\n",
            Slot::A
        ));
        assert!(policy_rule_priorities("garbage\n:\n").is_empty());
    }

    #[test]
    fn interface_addresses_from_ip_addr_show() {
        let stdout = "1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever\n\
1: lo    inet6 ::1/128 scope host \\       valid_lft forever preferred_lft forever\n\
2: eth0    inet 169.254.0.21/30 brd 169.254.0.23 scope global eth0\\       valid_lft forever\n\
2: eth0    inet6 fe80::1c2b:3cff:fe4d:5e6f/64 scope link \\       valid_lft forever\n\
3: tun0    inet 10.8.0.1 peer 10.8.0.2/32 scope global tun0\n\
garbage line\n";
        assert_eq!(
            interface_addresses(stdout),
            [
                ip("127.0.0.1"),
                ip("::1"),
                ip("169.254.0.21"),
                ip("fe80::1c2b:3cff:fe4d:5e6f"),
                ip("10.8.0.1"),
            ]
        );
        assert!(interface_addresses("").is_empty());
    }

    #[test]
    fn a_never_created_table_is_told_apart_from_other_failures() {
        assert!(table_missing(
            2,
            "Error: ipv4: FIB table does not exist.\nFlush terminated\n"
        ));
        assert!(table_missing(
            2,
            "Error: ipv6: FIB table does not exist.\nDump terminated\n"
        ));
        assert!(!table_missing(
            0,
            "Error: ipv4: FIB table does not exist.\n"
        ));
        assert!(!table_missing(
            2,
            "RTNETLINK answers: Operation not permitted\n"
        ));
        assert!(!table_missing(2, ""));
    }

    #[test]
    fn blackholes_are_counted_per_line() {
        let stdout = "blackhole 198.51.100.0/30 \nblackhole default \nunreachable 10.0.0.0/8\n";
        assert_eq!(blackhole_count(stdout), 2);
        assert_eq!(blackhole_count(""), 0);
    }
}
