//! The guest egress policy of ADR-012: the grammar of `allow_out` /
//! `deny_out` entries, E2B evaluation (allow beats deny, default allow),
//! the uid-scoped route plan and its atomic swap, the route probe, the
//! target guard and decision of the local forward proxy, the proxy wire
//! protocols (HTTP CONNECT/forward, SOCKS5 server and client) and the
//! environment every child gets once the proxy is up.
//!
//! Everything here is pure: the `rayd` adapters run `ip`, own the
//! listener and dial. Errors and `Debug` output never carry an entry, a
//! target, a proxy address or a credential.

pub mod cidr;
pub mod entry;
pub mod error;
pub mod guard;
pub mod policy;
pub mod probe;
pub mod proxy_env;
pub mod proxy_protocol;
pub mod route_plan;
pub mod state;
pub mod swap;

use std::time::Duration;

pub use cidr::{Cidr, Family, canonical_ip};
pub use entry::{ALL_TRAFFIC, EgressEntry, HostPattern};
pub use error::{EgressList, NetworkError};
pub use guard::{TargetGuard, UpstreamGuard};
pub use policy::{
    DenyReason, EgressMode, EgressPolicy, IpVerdict, PolicyInput, ProxyCredentials, ResolveKind,
    TargetDecision, TargetHost, UpstreamHost, UpstreamInput, UpstreamProxy,
};
pub use probe::{RouteVerdict, classify_route_get, rule_present, samples};
pub use proxy_env::egress_proxy_env;
pub use route_plan::{RoutePlan, Slot};
pub use state::{EgressEnforcement, NetworkSnapshot};
pub use swap::{PlannedStep, RouteStep, SwapPlan, plan_recovery, plan_swap};
/// Re-exported so the gRPC adapter can wrap credentials without a direct
/// dependency on `zeroize`.
pub use zeroize::Zeroizing;

/// Shared with the SDKs through `limits.json` (the agreement test below).
pub const EGRESS_MAX_ENTRIES_PER_LIST: usize = 256;
pub const EGRESS_MAX_HOSTNAME_ENTRIES: usize = 64;
pub const EGRESS_HOSTNAME_MAX_CHARS: usize = 253;
pub const EGRESS_PROXY_CREDENTIAL_MAX_BYTES: usize = 255;

/// Bounds `ip -batch` and the kernel tables for one address family.
pub const EGRESS_MAX_ROUTES_PER_FAMILY: usize = 4096;
/// `rayd`'s `RLIMIT_NOFILE` hard limit is 1024 (§9); each proxied
/// connection holds two descriptors.
pub const LOCAL_PROXY_MAX_CONNECTIONS: usize = 128;
pub const PROXY_HEAD_MAX_BYTES: usize = 16_384;
pub const PROXY_HEAD_TIMEOUT: Duration = Duration::from_secs(10);
pub const PROXY_CONNECT_TIMEOUT: Duration = Duration::from_secs(10);
pub const UPSTREAM_RESOLVE_TIMEOUT: Duration = Duration::from_secs(5);
/// The share of the `/run` hook budget the deny-all install may use.
pub const RUN_ENFORCE_BUDGET: Duration = Duration::from_millis(1_500);
/// The share of the `/resume` hook budget the re-verification may use.
pub const RESUME_VERIFY_BUDGET: Duration = Duration::from_secs(3);
/// The most one `ip` invocation may take before it is killed. Above the
/// 2.76 s measured for the first `ip` a guest spawns after a restore and
/// above the `/resume` sub-budget, so a slow but live `ip` is never cut
/// while a hook still waits for it; bounded, so a hung one cannot hold the
/// egress manager's lock (and `Health` not ready) indefinitely.
pub const IP_COMMAND_TIMEOUT: Duration = Duration::from_secs(5);

#[cfg(test)]
mod tests {
    use super::*;

    const LIMITS_JSON: &str = include_str!("../../../../limits.json");

    fn shared_limit(key: &str) -> usize {
        let limits: serde_json::Value = serde_json::from_str(LIMITS_JSON).unwrap();
        let value = limits.get(key).and_then(serde_json::Value::as_u64).unwrap();
        usize::try_from(value).unwrap()
    }

    #[test]
    fn the_shared_limits_agree_with_limits_json() {
        assert_eq!(
            shared_limit("egressMaxEntriesPerList"),
            EGRESS_MAX_ENTRIES_PER_LIST
        );
        assert_eq!(
            shared_limit("egressMaxHostnameEntries"),
            EGRESS_MAX_HOSTNAME_ENTRIES
        );
        assert_eq!(
            shared_limit("egressHostnameMaxChars"),
            EGRESS_HOSTNAME_MAX_CHARS
        );
        assert_eq!(
            shared_limit("egressProxyCredentialMaxBytes"),
            EGRESS_PROXY_CREDENTIAL_MAX_BYTES
        );
    }

    #[test]
    fn the_budgets_fit_inside_the_hook_budgets() {
        assert!(RUN_ENFORCE_BUDGET < Duration::from_secs(24));
        assert!(RESUME_VERIFY_BUDGET < Duration::from_secs(24));
        assert!(RUN_ENFORCE_BUDGET < IP_COMMAND_TIMEOUT);
        assert!(RESUME_VERIFY_BUDGET < IP_COMMAND_TIMEOUT);
        assert!(IP_COMMAND_TIMEOUT < Duration::from_secs(24));
    }
}
