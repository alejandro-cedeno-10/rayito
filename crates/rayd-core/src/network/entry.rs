//! The grammar of one `allow_out` / `deny_out` entry (design D3): a CIDR or
//! IP, `ALL_TRAFFIC`, or a hostname that is exact or `*.suffix`.
//!
//! Hostnames are ASCII only (no IDNA), lowercased, stripped of one
//! trailing dot, made of LDH labels of 1-63 characters, at most 253
//! characters in total and at least two labels. The last label is never
//! all digits, so a malformed IPv4 literal (`256.1.1.1`, `127.1`) is an
//! invalid entry rather than a hostname the resolver would read as an
//! address.

use super::EGRESS_HOSTNAME_MAX_CHARS;
use super::cidr::{Cidr, Family};

/// E2B's constant; here it covers both families.
pub const ALL_TRAFFIC: &str = "0.0.0.0/0";

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HostPattern {
    Exact(String),
    /// `*.suffix`: subdomains at any depth, never the apex (E2B rule).
    Subdomains(String),
}

impl HostPattern {
    #[must_use]
    pub fn parse(text: &str) -> Option<Self> {
        match text.strip_prefix("*.") {
            Some(suffix) => {
                let suffix = normalize_hostname(suffix)?;
                (suffix.len() + 2 <= EGRESS_HOSTNAME_MAX_CHARS).then_some(Self::Subdomains(suffix))
            }
            None => normalize_hostname(text).map(Self::Exact),
        }
    }

    /// `name` must already be normalized.
    #[must_use]
    pub fn matches(&self, name: &str) -> bool {
        match self {
            Self::Exact(exact) => name == exact,
            Self::Subdomains(suffix) => name
                .strip_suffix(suffix.as_str())
                .and_then(|rest| rest.strip_suffix('.'))
                .is_some_and(|label| !label.is_empty()),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum EgressEntry {
    Networks(Vec<Cidr>),
    Host(HostPattern),
}

impl EgressEntry {
    #[must_use]
    pub fn parse(text: &str) -> Option<Self> {
        if text == ALL_TRAFFIC {
            return Some(Self::Networks(vec![
                Cidr::everything(Family::V4),
                Cidr::everything(Family::V6),
            ]));
        }
        if let Ok(cidr) = Cidr::parse(text) {
            return Some(Self::Networks(vec![cidr]));
        }
        HostPattern::parse(text).map(Self::Host)
    }
}

/// The canonical form of a hostname, or `None` when it breaks the rules of
/// the module documentation.
#[must_use]
pub fn normalize_hostname(text: &str) -> Option<String> {
    let trimmed = text.strip_suffix('.').unwrap_or(text);
    if !trimmed.is_ascii() || trimmed.is_empty() || trimmed.len() > EGRESS_HOSTNAME_MAX_CHARS {
        return None;
    }
    let name = trimmed.to_ascii_lowercase();
    let labels: Vec<&str> = name.split('.').collect();
    let last_is_numeric = labels
        .last()
        .is_some_and(|label| label.bytes().all(|byte| byte.is_ascii_digit()));
    if labels.len() < 2 || last_is_numeric || !labels.iter().all(|label| is_ldh_label(label)) {
        return None;
    }
    Some(name)
}

/// `localhost` and every `*.localhost` name (RFC 6761): refused before any
/// resolution.
#[must_use]
pub fn is_localhost_name(text: &str) -> bool {
    let name = text.strip_suffix('.').unwrap_or(text).to_ascii_lowercase();
    name == "localhost" || name.ends_with(".localhost")
}

fn is_ldh_label(label: &str) -> bool {
    (1..=63).contains(&label.len())
        && !label.starts_with('-')
        && !label.ends_with('-')
        && label
            .bytes()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-')
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn all_traffic_covers_both_families_and_the_v6_default_only_v6() {
        let Some(EgressEntry::Networks(all)) = EgressEntry::parse(ALL_TRAFFIC) else {
            panic!("ALL_TRAFFIC is a network entry");
        };
        assert_eq!(
            all,
            [Cidr::everything(Family::V4), Cidr::everything(Family::V6)]
        );
        let Some(EgressEntry::Networks(v6)) = EgressEntry::parse("::/0") else {
            panic!("::/0 is a network entry");
        };
        assert_eq!(v6, [Cidr::everything(Family::V6)]);
        assert_eq!(
            EgressEntry::parse("10.0.0.0/8"),
            Some(EgressEntry::Networks(vec![
                Cidr::parse("10.0.0.0/8").unwrap()
            ]))
        );
    }

    #[test]
    fn hostnames_are_normalized() {
        assert_eq!(
            EgressEntry::parse("Api.Example.COM."),
            Some(EgressEntry::Host(HostPattern::Exact(
                "api.example.com".to_owned()
            )))
        );
        assert_eq!(
            HostPattern::parse("*.Example.com."),
            Some(HostPattern::Subdomains("example.com".to_owned()))
        );
    }

    #[test]
    fn wildcards_match_subdomains_at_any_depth_but_not_the_apex() {
        let pattern = HostPattern::parse("*.example.com").unwrap();
        assert!(pattern.matches("a.example.com"));
        assert!(pattern.matches("a.b.example.com"));
        assert!(!pattern.matches("example.com"));
        assert!(!pattern.matches("badexample.com"));
        assert!(!pattern.matches(".example.com"));
        let exact = HostPattern::parse("example.com").unwrap();
        assert!(exact.matches("example.com"));
        assert!(!exact.matches("a.example.com"));
    }

    #[test]
    fn malformed_hostnames_are_refused() {
        let long_label = format!("{}.com", "a".repeat(64));
        let long_name = format!("{}.coma", ["a"; 125].join("."));
        assert_eq!(long_name.len(), 254);
        for input in [
            "*",
            "*.",
            "*.com",
            "a.*.b",
            "*.*.example.com",
            "localhost",
            "example",
            "-a.example.com",
            "a-.example.com",
            "a_b.example.com",
            "exämple.com",
            "a..example.com",
            "256.1.1.1",
            "127.1",
            "",
            ".",
            long_label.as_str(),
            long_name.as_str(),
        ] {
            assert_eq!(EgressEntry::parse(input), None, "{input:?}");
        }
        let max_name = format!("{}.com", ["a"; 125].join("."));
        assert_eq!(max_name.len(), 253);
        assert!(normalize_hostname(&max_name).is_some());
        assert!(HostPattern::parse(&format!("*.{max_name}")).is_none());
    }

    #[test]
    fn localhost_names_are_recognized() {
        assert!(is_localhost_name("localhost"));
        assert!(is_localhost_name("LOCALHOST."));
        assert!(is_localhost_name("api.localhost"));
        assert!(!is_localhost_name("localhost.example.com"));
    }
}
