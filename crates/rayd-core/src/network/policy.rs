//! The egress policy object shared by the routes and the local proxy
//! (design D3, D9, D10): parsing with the caps of D16, E2B evaluation
//! (allow beats deny, default allow), the enforcement mode, the target
//! decision of the proxy and the operator's SOCKS5 upstream.
//!
//! When `deny_out` is empty the allow entries are kept for the echo but
//! ignored for evaluation: under "default allow" they cannot change a
//! verdict, so `allow_out` alone never triggers enforcement.

use std::fmt;
use std::net::{IpAddr, Ipv4Addr};

use zeroize::Zeroizing;

use super::cidr::{Cidr, Family, canonical_ip};
use super::entry::{EgressEntry, HostPattern, is_localhost_name, normalize_hostname};
use super::error::{EgressList, NetworkError};
use super::guard::{TargetGuard, UpstreamGuard};
use super::{
    EGRESS_MAX_ENTRIES_PER_LIST, EGRESS_MAX_HOSTNAME_ENTRIES, EGRESS_PROXY_CREDENTIAL_MAX_BYTES,
};

/// Ports on which a hostname rule may allow a target by name (E2B: HTTP
/// Host on 80, TLS SNI on 443).
pub const NAME_RULE_PORTS: [u16; 2] = [80, 443];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EgressMode {
    Unrestricted,
    Routes,
    ProxyOnly,
}

impl EgressMode {
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Unrestricted => "unrestricted",
            Self::Routes => "routes",
            Self::ProxyOnly => "proxy_only",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum IpVerdict {
    Allow,
    Deny,
}

/// The transport-free mirror of `UpdateNetworkRequest` the gRPC adapter
/// fills.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct PolicyInput {
    pub allow_out: Vec<String>,
    pub deny_out: Vec<String>,
    pub upstream: Option<UpstreamInput>,
}

#[derive(Clone, Default, PartialEq, Eq)]
pub struct UpstreamInput {
    pub address: String,
    pub username: Option<Zeroizing<String>>,
    pub password: Option<Zeroizing<String>>,
}

impl fmt::Debug for UpstreamInput {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("UpstreamInput(<redacted>)")
    }
}

/// RFC 1929 credentials, wiped from memory when the policy that holds them
/// is replaced. An absent password is sent as an empty one.
#[derive(Clone, PartialEq, Eq)]
pub struct ProxyCredentials {
    username: Zeroizing<String>,
    password: Zeroizing<String>,
}

impl ProxyCredentials {
    #[must_use]
    pub fn username(&self) -> &str {
        &self.username
    }

    #[must_use]
    pub fn password(&self) -> &str {
        &self.password
    }
}

impl fmt::Debug for ProxyCredentials {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("ProxyCredentials(<redacted>)")
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum UpstreamHost {
    Ip(IpAddr),
    Name(String),
}

#[derive(Clone, PartialEq, Eq)]
pub struct UpstreamProxy {
    host: UpstreamHost,
    port: u16,
    credentials: Option<ProxyCredentials>,
}

impl fmt::Debug for UpstreamProxy {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("UpstreamProxy(<redacted>)")
    }
}

impl UpstreamProxy {
    /// `host:port` or `[v6]:port`, port 1-65535; credentials 1-255 bytes
    /// each, a password never without a username. An IP literal the
    /// upstream guard refuses is `ProxyForbiddenAddress`; a hostname is
    /// resolved and checked by the adapter.
    pub fn parse(input: UpstreamInput) -> Result<Self, NetworkError> {
        let UpstreamInput {
            address,
            username,
            password,
        } = input;
        let (host, port) = parse_host_port(&address).ok_or(NetworkError::InvalidProxyAddress)?;
        if let UpstreamHost::Ip(ip) = host
            && UpstreamGuard.blocks(ip)
        {
            return Err(NetworkError::ProxyForbiddenAddress);
        }
        Ok(Self {
            host,
            port,
            credentials: parse_credentials(username, password)?,
        })
    }

    #[must_use]
    pub fn host(&self) -> &UpstreamHost {
        &self.host
    }

    #[must_use]
    pub fn port(&self) -> u16 {
        self.port
    }

    #[must_use]
    pub fn credentials(&self) -> Option<&ProxyCredentials> {
        self.credentials.as_ref()
    }
}

fn parse_credentials(
    username: Option<Zeroizing<String>>,
    password: Option<Zeroizing<String>>,
) -> Result<Option<ProxyCredentials>, NetworkError> {
    let valid = |value: &str| (1..=EGRESS_PROXY_CREDENTIAL_MAX_BYTES).contains(&value.len());
    match (username, password) {
        (None, None) => Ok(None),
        (None, Some(_)) => Err(NetworkError::InvalidProxyCredentials),
        (Some(username), password) => {
            if !valid(&username) || password.as_ref().is_some_and(|password| !valid(password)) {
                return Err(NetworkError::InvalidProxyCredentials);
            }
            Ok(Some(ProxyCredentials {
                username,
                password: password.unwrap_or_default(),
            }))
        }
    }
}

fn parse_host_port(address: &str) -> Option<(UpstreamHost, u16)> {
    let (host, port) = if let Some(rest) = address.strip_prefix('[') {
        let (literal, port) = rest.split_once("]:")?;
        let ip: std::net::Ipv6Addr = literal.parse().ok()?;
        (UpstreamHost::Ip(IpAddr::V6(ip)), port)
    } else {
        let (host, port) = address.rsplit_once(':')?;
        if host.contains(':') {
            return None;
        }
        let host = match host.parse::<Ipv4Addr>() {
            Ok(v4) => UpstreamHost::Ip(IpAddr::V4(v4)),
            Err(_) => UpstreamHost::Name(normalize_hostname(host)?),
        };
        (host, port)
    };
    Some((host, parse_port(port)?))
}

/// Decimal digits only, 1-65535.
#[must_use]
pub fn parse_port(text: &str) -> Option<u16> {
    if text.is_empty() || text.len() > 5 || !text.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    text.parse().ok().filter(|port| *port != 0)
}

/// The destination a proxy client asked for.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TargetHost {
    Ip(IpAddr),
    Name(String),
}

impl TargetHost {
    /// An IP literal (IPv6 with or without brackets) or a name to be
    /// validated by the decision.
    #[must_use]
    pub fn parse(host: &str) -> Self {
        let unbracketed = host
            .strip_prefix('[')
            .and_then(|rest| rest.strip_suffix(']'))
            .unwrap_or(host);
        unbracketed
            .parse::<IpAddr>()
            .map_or_else(|_| Self::Name(host.to_owned()), Self::Ip)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DenyReason {
    Policy,
    Guard,
    Invalid,
}

impl DenyReason {
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Policy => "policy",
            Self::Guard => "guard",
            Self::Invalid => "invalid",
        }
    }
}

/// What the proxy does with a target before any byte leaves (design D9).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TargetDecision {
    Deny(DenyReason),
    /// Dial (or hand to the upstream as ATYP IPv4/IPv6) exactly this address.
    ConnectIp(IpAddr),
    /// Name-allowed with an upstream: ATYP domain, no local resolution.
    ForwardByName(String),
    /// Name-allowed without an upstream: resolve and drop guarded addresses.
    ResolveByName(String),
    /// Not name-allowed: resolve, drop guarded and denied addresses.
    ResolveChecked(String),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ResolveKind {
    ByName,
    Checked,
}

#[derive(Clone, Default, PartialEq, Eq)]
pub struct EgressPolicy {
    raw_allow: Vec<String>,
    raw_deny: Vec<String>,
    hostname_entries: usize,
    allow_nets: Vec<Cidr>,
    deny_nets: Vec<Cidr>,
    allow_hosts: Vec<HostPattern>,
    upstream: Option<UpstreamProxy>,
}

impl fmt::Debug for EgressPolicy {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("EgressPolicy")
            .field("mode", &self.mode())
            .field("allow_count", &self.raw_allow.len())
            .field("deny_count", &self.raw_deny.len())
            .field("hostname_count", &self.hostname_entries)
            .finish_non_exhaustive()
    }
}

impl EgressPolicy {
    pub fn parse(input: PolicyInput) -> Result<Self, NetworkError> {
        let PolicyInput {
            allow_out,
            deny_out,
            upstream,
        } = input;
        check_list_size(EgressList::AllowOut, &allow_out)?;
        check_list_size(EgressList::DenyOut, &deny_out)?;
        let (allow_nets, allow_hosts) = parse_allow(&allow_out)?;
        let deny_nets = parse_deny(&deny_out)?;
        let upstream = upstream.map(UpstreamProxy::parse).transpose()?;
        let hostname_entries = allow_hosts.len();
        let evaluates_allow = !deny_nets.is_empty();
        Ok(Self {
            raw_allow: allow_out,
            raw_deny: deny_out,
            hostname_entries,
            allow_nets: if evaluates_allow {
                allow_nets
            } else {
                Vec::new()
            },
            allow_hosts: if evaluates_allow {
                allow_hosts
            } else {
                Vec::new()
            },
            deny_nets,
            upstream,
        })
    }

    /// `deny_out = ["0.0.0.0/0"]`: what `/run` installs and what recovery
    /// falls back to.
    #[must_use]
    pub fn deny_all() -> Self {
        Self {
            raw_deny: vec![super::entry::ALL_TRAFFIC.to_owned()],
            deny_nets: Family::ALL.into_iter().map(Cidr::everything).collect(),
            ..Self::default()
        }
    }

    /// Allow if any evaluated allow network contains the address, else deny
    /// if any deny network does, else allow.
    #[must_use]
    pub fn ip_verdict(&self, ip: IpAddr) -> IpVerdict {
        let ip = canonical_ip(ip);
        if self.allow_nets.iter().any(|net| net.contains(ip)) {
            IpVerdict::Allow
        } else if self.deny_nets.iter().any(|net| net.contains(ip)) {
            IpVerdict::Deny
        } else {
            IpVerdict::Allow
        }
    }

    /// Both families fully denied, which is what `ALL_TRAFFIC` in
    /// `deny_out` yields.
    #[must_use]
    pub fn deny_by_default(&self) -> bool {
        Family::ALL.into_iter().all(|family| {
            self.deny_nets
                .iter()
                .any(|net| net.family() == family && net.prefix() == 0)
        })
    }

    #[must_use]
    pub fn mode(&self) -> EgressMode {
        if self.upstream.is_some() || (!self.deny_nets.is_empty() && !self.allow_hosts.is_empty()) {
            EgressMode::ProxyOnly
        } else if self.deny_nets.is_empty() {
            EgressMode::Unrestricted
        } else {
            EgressMode::Routes
        }
    }

    #[must_use]
    pub fn requires_enforcement(&self) -> bool {
        self.mode() != EgressMode::Unrestricted
    }

    #[must_use]
    pub fn raw_allow(&self) -> &[String] {
        &self.raw_allow
    }

    #[must_use]
    pub fn raw_deny(&self) -> &[String] {
        &self.raw_deny
    }

    #[must_use]
    pub fn allow_nets(&self) -> &[Cidr] {
        &self.allow_nets
    }

    #[must_use]
    pub fn deny_nets(&self) -> &[Cidr] {
        &self.deny_nets
    }

    #[must_use]
    pub fn allow_hosts(&self) -> &[HostPattern] {
        &self.allow_hosts
    }

    /// Hostname entries as sent, for the `egress_policy_applied` count.
    #[must_use]
    pub fn hostname_entries(&self) -> usize {
        self.hostname_entries
    }

    #[must_use]
    pub fn upstream(&self) -> Option<&UpstreamProxy> {
        self.upstream.as_ref()
    }

    /// The first half of design D9: everything that can be decided before
    /// any resolution. A denied name is never resolved under a
    /// deny-by-default policy, so `rayd`'s resolver is not a DNS
    /// exfiltration path.
    #[must_use]
    pub fn decide(&self, host: &TargetHost, port: u16, guard: &TargetGuard) -> TargetDecision {
        if port == 0 {
            return TargetDecision::Deny(DenyReason::Invalid);
        }
        match host {
            TargetHost::Ip(ip) => self.decide_ip(*ip, guard),
            TargetHost::Name(name) => self.decide_name(name, port),
        }
    }

    fn decide_ip(&self, ip: IpAddr, guard: &TargetGuard) -> TargetDecision {
        let ip = canonical_ip(ip);
        if guard.blocks(ip) {
            TargetDecision::Deny(DenyReason::Guard)
        } else if self.ip_verdict(ip) == IpVerdict::Deny {
            TargetDecision::Deny(DenyReason::Policy)
        } else {
            TargetDecision::ConnectIp(ip)
        }
    }

    fn decide_name(&self, raw: &str, port: u16) -> TargetDecision {
        if is_localhost_name(raw) {
            return TargetDecision::Deny(DenyReason::Guard);
        }
        let Some(name) = normalize_hostname(raw) else {
            return TargetDecision::Deny(DenyReason::Invalid);
        };
        let name_allowed = NAME_RULE_PORTS.contains(&port)
            && self
                .allow_hosts
                .iter()
                .any(|pattern| pattern.matches(&name));
        if name_allowed {
            return if self.upstream.is_some() {
                TargetDecision::ForwardByName(name)
            } else {
                TargetDecision::ResolveByName(name)
            };
        }
        if self.deny_by_default() {
            return TargetDecision::Deny(DenyReason::Policy);
        }
        TargetDecision::ResolveChecked(name)
    }

    /// The second half of design D9, after resolution: guarded addresses are
    /// always dropped, denied ones too unless the name itself was allowed.
    /// Nothing left is a guard denial when every address was guarded, a
    /// policy denial otherwise.
    pub fn select_addresses(
        &self,
        kind: ResolveKind,
        resolved: &[IpAddr],
        guard: &TargetGuard,
    ) -> Result<Vec<IpAddr>, DenyReason> {
        let unguarded: Vec<IpAddr> = resolved
            .iter()
            .map(|ip| canonical_ip(*ip))
            .filter(|ip| !guard.blocks(*ip))
            .collect();
        if unguarded.is_empty() && !resolved.is_empty() {
            return Err(DenyReason::Guard);
        }
        let kept: Vec<IpAddr> = match kind {
            ResolveKind::ByName => unguarded,
            ResolveKind::Checked => unguarded
                .into_iter()
                .filter(|ip| self.ip_verdict(*ip) == IpVerdict::Allow)
                .collect(),
        };
        if kept.is_empty() {
            Err(DenyReason::Policy)
        } else {
            Ok(kept)
        }
    }
}

fn check_list_size(list: EgressList, entries: &[String]) -> Result<(), NetworkError> {
    if entries.len() > EGRESS_MAX_ENTRIES_PER_LIST {
        Err(NetworkError::TooManyEntries { list })
    } else {
        Ok(())
    }
}

fn parse_allow(entries: &[String]) -> Result<(Vec<Cidr>, Vec<HostPattern>), NetworkError> {
    let mut nets = Vec::new();
    let mut hosts = Vec::new();
    for (index, entry) in entries.iter().enumerate() {
        match EgressEntry::parse(entry) {
            Some(EgressEntry::Networks(parsed)) => nets.extend(parsed),
            Some(EgressEntry::Host(pattern)) => hosts.push(pattern),
            None => {
                return Err(NetworkError::InvalidEntry {
                    list: EgressList::AllowOut,
                    index,
                });
            }
        }
    }
    if hosts.len() > EGRESS_MAX_HOSTNAME_ENTRIES {
        return Err(NetworkError::TooManyHostnames);
    }
    Ok((nets, hosts))
}

fn parse_deny(entries: &[String]) -> Result<Vec<Cidr>, NetworkError> {
    let mut nets = Vec::new();
    for (index, entry) in entries.iter().enumerate() {
        match EgressEntry::parse(entry) {
            Some(EgressEntry::Networks(parsed)) => nets.extend(parsed),
            Some(EgressEntry::Host(_)) => return Err(NetworkError::HostnameInDenyOut { index }),
            None => {
                return Err(NetworkError::InvalidEntry {
                    list: EgressList::DenyOut,
                    index,
                });
            }
        }
    }
    Ok(nets)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::network::entry::ALL_TRAFFIC;

    fn strings(entries: &[&str]) -> Vec<String> {
        entries.iter().map(|entry| (*entry).to_owned()).collect()
    }

    fn policy(allow: &[&str], deny: &[&str]) -> EgressPolicy {
        EgressPolicy::parse(PolicyInput {
            allow_out: strings(allow),
            deny_out: strings(deny),
            upstream: None,
        })
        .unwrap()
    }

    fn upstream(address: &str) -> UpstreamInput {
        UpstreamInput {
            address: address.to_owned(),
            ..UpstreamInput::default()
        }
    }

    fn with_upstream(allow: &[&str], deny: &[&str], address: &str) -> EgressPolicy {
        EgressPolicy::parse(PolicyInput {
            allow_out: strings(allow),
            deny_out: strings(deny),
            upstream: Some(upstream(address)),
        })
        .unwrap()
    }

    fn ip(text: &str) -> IpAddr {
        text.parse().unwrap()
    }

    fn name(text: &str) -> TargetHost {
        TargetHost::Name(text.to_owned())
    }

    #[test]
    fn allow_beats_deny_even_when_broader() {
        let policy = policy(&["10.0.0.0/8"], &["10.1.0.0/16", ALL_TRAFFIC]);
        assert_eq!(policy.ip_verdict(ip("10.1.2.3")), IpVerdict::Allow);
        assert_eq!(policy.ip_verdict(ip("11.0.0.1")), IpVerdict::Deny);
        assert_eq!(policy.ip_verdict(ip("::1")), IpVerdict::Deny);
        assert_eq!(policy.ip_verdict(ip("::ffff:10.9.9.9")), IpVerdict::Allow);
        assert_eq!(policy.ip_verdict(ip("::ffff:11.9.9.9")), IpVerdict::Deny);
    }

    #[test]
    fn default_allow_and_allow_out_alone_restrict_nothing() {
        let open = policy(&[], &["203.0.113.0/24"]);
        assert_eq!(open.ip_verdict(ip("198.51.100.1")), IpVerdict::Allow);
        assert_eq!(open.ip_verdict(ip("203.0.113.9")), IpVerdict::Deny);
        let allow_only = policy(&["example.com", "10.0.0.0/8"], &[]);
        assert_eq!(allow_only.mode(), EgressMode::Unrestricted);
        assert!(!allow_only.requires_enforcement());
        assert!(allow_only.allow_hosts().is_empty());
        assert!(allow_only.allow_nets().is_empty());
        assert_eq!(allow_only.raw_allow(), ["example.com", "10.0.0.0/8"]);
        assert_eq!(allow_only.hostname_entries(), 1);
    }

    #[test]
    fn mode_selection_table() {
        assert_eq!(policy(&[], &[]).mode(), EgressMode::Unrestricted);
        assert_eq!(
            policy(&["1.2.3.4"], &[ALL_TRAFFIC]).mode(),
            EgressMode::Routes
        );
        assert_eq!(policy(&[], &["10.0.0.0/8"]).mode(), EgressMode::Routes);
        assert_eq!(
            policy(&["api.example.com"], &[ALL_TRAFFIC]).mode(),
            EgressMode::ProxyOnly
        );
        assert_eq!(
            with_upstream(&[], &[], "203.0.113.5:1080").mode(),
            EgressMode::ProxyOnly
        );
        assert!(with_upstream(&[], &[], "203.0.113.5:1080").requires_enforcement());
        assert!(policy(&[], &["10.0.0.0/8"]).requires_enforcement());
    }

    #[test]
    fn deny_by_default_needs_both_families() {
        assert!(policy(&[], &[ALL_TRAFFIC]).deny_by_default());
        assert!(!policy(&[], &["0.0.0.0/1", "128.0.0.0/1", "::/0"]).deny_by_default());
        assert!(!policy(&[], &["::/0"]).deny_by_default());
        assert!(EgressPolicy::deny_all().deny_by_default());
        assert_eq!(EgressPolicy::deny_all().raw_deny(), [ALL_TRAFFIC]);
        assert_eq!(EgressPolicy::deny_all().mode(), EgressMode::Routes);
    }

    #[test]
    fn entry_errors_name_the_list_and_index_only() {
        let error = EgressPolicy::parse(PolicyInput {
            allow_out: strings(&["*.Example.com."]),
            deny_out: strings(&["example.com"]),
            upstream: None,
        })
        .unwrap_err();
        assert_eq!(error, NetworkError::HostnameInDenyOut { index: 0 });
        assert!(error.to_string().starts_with("deny_out[0]"));
        assert!(!error.to_string().contains("example.com"));
        let invalid = EgressPolicy::parse(PolicyInput {
            allow_out: strings(&["10.0.0.0/8", "10.0.0.0/8", "10.0.0.0/8", "not a host"]),
            ..PolicyInput::default()
        })
        .unwrap_err();
        assert_eq!(
            invalid.to_string(),
            "allow_out[3]: no es un CIDR, una IP ni un nombre de host válido"
        );
        let bad_deny = EgressPolicy::parse(PolicyInput {
            deny_out: strings(&["10.0.0.0/8", "10.0.0.0/33"]),
            ..PolicyInput::default()
        })
        .unwrap_err();
        assert_eq!(
            bad_deny,
            NetworkError::InvalidEntry {
                list: EgressList::DenyOut,
                index: 1
            }
        );
    }

    #[test]
    fn caps_on_entries_and_hostnames() {
        let too_many = vec!["10.0.0.1".to_owned(); EGRESS_MAX_ENTRIES_PER_LIST + 1];
        assert_eq!(
            EgressPolicy::parse(PolicyInput {
                deny_out: too_many.clone(),
                ..PolicyInput::default()
            }),
            Err(NetworkError::TooManyEntries {
                list: EgressList::DenyOut
            })
        );
        assert_eq!(
            EgressPolicy::parse(PolicyInput {
                allow_out: too_many,
                ..PolicyInput::default()
            }),
            Err(NetworkError::TooManyEntries {
                list: EgressList::AllowOut
            })
        );
        let hosts: Vec<String> = (0..=EGRESS_MAX_HOSTNAME_ENTRIES)
            .map(|index| format!("h{index}.example.com"))
            .collect();
        assert_eq!(
            EgressPolicy::parse(PolicyInput {
                allow_out: hosts.clone(),
                ..PolicyInput::default()
            }),
            Err(NetworkError::TooManyHostnames)
        );
        assert!(
            EgressPolicy::parse(PolicyInput {
                allow_out: hosts[..EGRESS_MAX_HOSTNAME_ENTRIES].to_vec(),
                ..PolicyInput::default()
            })
            .is_ok()
        );
    }

    #[test]
    fn upstream_address_and_credentials_are_validated() {
        let parse = |address: &str, username: Option<&str>, password: Option<&str>| {
            UpstreamProxy::parse(UpstreamInput {
                address: address.to_owned(),
                username: username.map(|value| Zeroizing::new(value.to_owned())),
                password: password.map(|value| Zeroizing::new(value.to_owned())),
            })
        };
        let named = parse("proxy.example.com:1080", Some("u"), Some("p")).unwrap();
        assert_eq!(
            named.host(),
            &UpstreamHost::Name("proxy.example.com".to_owned())
        );
        assert_eq!(named.port(), 1080);
        assert_eq!(named.credentials().unwrap().username(), "u");
        assert_eq!(named.credentials().unwrap().password(), "p");
        let v6 = parse("[2001:db8::5]:1080", Some("u"), None).unwrap();
        assert_eq!(v6.host(), &UpstreamHost::Ip(ip("2001:db8::5")));
        assert_eq!(v6.credentials().unwrap().password(), "");
        assert!(
            parse("10.0.0.5:1080", None, None)
                .unwrap()
                .credentials()
                .is_none()
        );
        for bad in [
            "proxy.example.com",
            "proxy.example.com:0",
            "proxy.example.com:65536",
            "2001:db8::5:1080",
            "[2001:db8::5]1080",
            "bad_host:1080",
            ":1080",
        ] {
            assert_eq!(
                parse(bad, None, None),
                Err(NetworkError::InvalidProxyAddress),
                "{bad}"
            );
        }
        for forbidden in [
            "127.0.0.1:1080",
            "169.254.169.254:80",
            "[fd00:ec2::254]:80",
            "[::ffff:127.0.0.1]:1080",
            "0.0.0.0:1080",
        ] {
            assert_eq!(
                parse(forbidden, None, None),
                Err(NetworkError::ProxyForbiddenAddress),
                "{forbidden}"
            );
        }
        let long = "x".repeat(EGRESS_PROXY_CREDENTIAL_MAX_BYTES + 1);
        for (username, password) in [
            (None, Some("p")),
            (Some(""), None),
            (Some("u"), Some("")),
            (Some(long.as_str()), None),
            (Some("u"), Some(long.as_str())),
        ] {
            assert_eq!(
                parse("10.0.0.5:1080", username, password),
                Err(NetworkError::InvalidProxyCredentials)
            );
        }
    }

    #[test]
    fn debug_output_never_carries_credentials_or_addresses() {
        let input = UpstreamInput {
            address: "proxy.example.com:1080".to_owned(),
            username: Some(Zeroizing::new("u-marker".to_owned())),
            password: Some(Zeroizing::new("p-marker".to_owned())),
        };
        let parsed = EgressPolicy::parse(PolicyInput {
            allow_out: strings(&["secret-host.example"]),
            deny_out: strings(&[ALL_TRAFFIC]),
            upstream: Some(input.clone()),
        })
        .unwrap();
        let rendered = format!(
            "{input:?} {parsed:?} {:?} {:?}",
            parsed.upstream().unwrap(),
            parsed.upstream().unwrap().credentials().unwrap()
        );
        for secret in ["u-marker", "p-marker", "proxy.example", "secret-host"] {
            assert!(!rendered.contains(secret), "{rendered}");
        }
    }

    #[test]
    fn ip_literal_decisions() {
        let guard = TargetGuard::new([ip("10.0.1.5")]);
        let policy = policy(&["203.0.113.0/24"], &[ALL_TRAFFIC]);
        assert_eq!(
            policy.decide(&TargetHost::Ip(ip("203.0.113.7")), 443, &guard),
            TargetDecision::ConnectIp(ip("203.0.113.7"))
        );
        assert_eq!(
            policy.decide(&TargetHost::Ip(ip("198.51.100.7")), 443, &guard),
            TargetDecision::Deny(DenyReason::Policy)
        );
        for guarded in [
            "169.254.169.254",
            "127.0.0.1",
            "10.0.1.5",
            "::ffff:169.254.169.254",
        ] {
            assert_eq!(
                policy.decide(&TargetHost::Ip(ip(guarded)), 80, &guard),
                TargetDecision::Deny(DenyReason::Guard),
                "{guarded}"
            );
        }
        assert_eq!(
            policy.decide(&TargetHost::Ip(ip("203.0.113.7")), 0, &guard),
            TargetDecision::Deny(DenyReason::Invalid)
        );
    }

    #[test]
    fn name_decisions() {
        let guard = TargetGuard::default();
        let routes = policy(&["api.example.test"], &[ALL_TRAFFIC]);
        assert_eq!(
            routes.decide(&name("API.example.test"), 443, &guard),
            TargetDecision::ResolveByName("api.example.test".to_owned())
        );
        assert_eq!(
            routes.decide(&name("api.example.test"), 8443, &guard),
            TargetDecision::Deny(DenyReason::Policy)
        );
        assert_eq!(
            routes.decide(&name("other.example.test"), 443, &guard),
            TargetDecision::Deny(DenyReason::Policy)
        );
        let chained = with_upstream(&["api.example.test"], &[ALL_TRAFFIC], "203.0.113.5:1080");
        assert_eq!(
            chained.decide(&name("api.example.test"), 80, &guard),
            TargetDecision::ForwardByName("api.example.test".to_owned())
        );
        let partial = policy(&["*.example.test"], &["203.0.113.0/24"]);
        assert_eq!(
            partial.decide(&name("other.example.org"), 8443, &guard),
            TargetDecision::ResolveChecked("other.example.org".to_owned())
        );
        for refused in ["localhost", "db.localhost", "LOCALHOST."] {
            assert_eq!(
                partial.decide(&name(refused), 80, &guard),
                TargetDecision::Deny(DenyReason::Guard),
                "{refused}"
            );
        }
        for invalid in ["single", "bad_name.example", "exämple.com"] {
            assert_eq!(
                partial.decide(&name(invalid), 80, &guard),
                TargetDecision::Deny(DenyReason::Invalid),
                "{invalid}"
            );
        }
    }

    #[test]
    fn select_addresses_reasons() {
        let guard = TargetGuard::new([ip("10.0.1.5")]);
        let policy = policy(&["203.0.113.0/24"], &["198.51.100.0/24"]);
        assert_eq!(
            policy.select_addresses(
                ResolveKind::Checked,
                &[ip("127.0.0.1"), ip("10.0.1.5")],
                &guard
            ),
            Err(DenyReason::Guard)
        );
        assert_eq!(
            policy.select_addresses(
                ResolveKind::Checked,
                &[ip("127.0.0.1"), ip("198.51.100.9")],
                &guard
            ),
            Err(DenyReason::Policy)
        );
        assert_eq!(
            policy.select_addresses(
                ResolveKind::Checked,
                &[
                    ip("198.51.100.9"),
                    ip("192.0.2.1"),
                    ip("::ffff:203.0.113.1")
                ],
                &guard
            ),
            Ok(vec![ip("192.0.2.1"), ip("203.0.113.1")])
        );
        assert_eq!(
            policy.select_addresses(
                ResolveKind::ByName,
                &[ip("198.51.100.9"), ip("127.0.0.1")],
                &guard
            ),
            Ok(vec![ip("198.51.100.9")])
        );
        assert_eq!(
            policy.select_addresses(ResolveKind::ByName, &[ip("169.254.169.254")], &guard),
            Err(DenyReason::Guard)
        );
    }

    #[test]
    fn target_host_parsing() {
        assert_eq!(
            TargetHost::parse("203.0.113.7"),
            TargetHost::Ip(ip("203.0.113.7"))
        );
        assert_eq!(
            TargetHost::parse("[2001:db8::1]"),
            TargetHost::Ip(ip("2001:db8::1"))
        );
        assert_eq!(
            TargetHost::parse("2001:db8::1"),
            TargetHost::Ip(ip("2001:db8::1"))
        );
        assert_eq!(TargetHost::parse("Example.com"), name("Example.com"));
        assert_eq!(parse_port("443"), Some(443));
        assert_eq!(parse_port("+443"), None);
        assert_eq!(parse_port("0"), None);
        assert_eq!(parse_port("65536"), None);
    }
}
