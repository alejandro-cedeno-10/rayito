//! The egress routes against a real kernel (ADR-012, design D18
//! `m9_egress`): as root inside a fresh network namespace (CI runs this
//! binary under `sudo unshare --net`), `lo` up, a dummy `rtest0` with
//! `192.0.2.1/24` and a default route through it. Then the IMDS block plus
//! the `/run` deny-all — including egress option A's DNS guard
//! (`SECURITY.md` T17): the moved `local` rule, the two `prohibit` rules at
//! priority 0, and that a uid-1000 DNS query no longer resolves while
//! ordinary loopback routing and other ports are untouched — a swap to
//! `deny 198.51.100.0/24, allow 198.51.100.7/32` (where the guard comes
//! back off, checked once the policy returns to unrestricted), and injected
//! failures while filling and while committing, each checked with `ip route
//! get <addr> uid <n>` (no packet is sent).
//!
//! It self-skips unless it runs as root with `CAP_NET_ADMIN` in a
//! namespace whose only interface is `lo`, so a developer's root shell or
//! a container never gets its routing changed. With
//! `RAYITO_REQUIRE_EGRESS_NETNS=1` (the CI namespace step) that skip is a
//! failure instead.
//!
//! Integration tests are test code, but clippy's `allow-unwrap-in-tests` only
//! recognises `#[test]` functions and `#[cfg(test)]` modules.
#![cfg(unix)]
#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::too_many_lines
)]

use std::net::IpAddr;
use std::sync::{Arc, Mutex};

use rayd::adapters::egress_routes::{EgressRoutes, IpEgressRoutes, RouteCommandError};
use rayd::adapters::ip_command::run_ip;
use rayd::adapters::{ImdsBlock, detect_guest_capabilities, install_imds_block};
use rayd::network::{NetworkManager, ProxySeams};
use rayd_core::clock::SystemClock;
use rayd_core::network::probe::{RouteVerdict, classify_route_get};
use rayd_core::network::{
    ALL_TRAFFIC, DnsGuardStep, EgressEnforcement, Family, NetworkError, PolicyInput, RouteStep,
};
use rayd_core::session::SandboxSession;

/// CI sets it to `1` on the namespace step, so a runner where the suite
/// would self-skip fails instead of passing without checking anything.
const REQUIRE_NETNS_ENV: &str = "RAYITO_REQUIRE_EGRESS_NETNS";

/// The real adapter, with one step name failing on its next execution.
struct FailingRoutes {
    inner: IpEgressRoutes,
    failing: Mutex<Option<&'static str>>,
}

impl FailingRoutes {
    fn fail_next(&self, step: &'static str) {
        *self.failing.lock().unwrap() = Some(step);
    }
}

#[tonic::async_trait]
impl EgressRoutes for FailingRoutes {
    async fn execute(&self, step: &RouteStep) -> Result<(), RouteCommandError> {
        let injected = {
            let mut failing = self.failing.lock().unwrap();
            if *failing == Some(step.name()) {
                *failing = None;
                true
            } else {
                false
            }
        };
        if injected {
            return Err(RouteCommandError {
                reason: "injected".to_owned(),
            });
        }
        self.inner.execute(step).await
    }

    async fn execute_dns_guard(&self, step: &DnsGuardStep) -> Result<(), RouteCommandError> {
        self.inner.execute_dns_guard(step).await
    }

    async fn show_rules(&self, family: Family) -> Result<String, RouteCommandError> {
        self.inner.show_rules(family).await
    }

    async fn show_table(&self, family: Family, table: u32) -> Result<String, RouteCommandError> {
        self.inner.show_table(family, table).await
    }

    async fn route_get(&self, destination: IpAddr) -> Result<(i32, String), RouteCommandError> {
        self.inner.route_get(destination).await
    }

    async fn local_addresses(&self) -> Result<Vec<IpAddr>, RouteCommandError> {
        self.inner.local_addresses().await
    }

    fn ipv6_present(&self) -> bool {
        self.inner.ipv6_present()
    }
}

async fn ip_ok(args: &[&str]) -> String {
    let output = run_ip(args).await.unwrap();
    assert_eq!(output.code, 0, "ip {}", args.join(" "));
    output.stdout
}

/// `ip route get <destination> uid <uid>` as the probe reads it.
async fn route(destination: &str, uid: &str) -> RouteVerdict {
    let output = run_ip(&["route", "get", destination, "uid", uid])
        .await
        .unwrap();
    classify_route_get(output.code, &output.stdout)
}

async fn fresh_namespace() -> bool {
    let links = ip_ok(&["-o", "link", "show"]).await;
    links
        .lines()
        .all(|line| line.split_whitespace().nth(1) == Some("lo:"))
}

fn input(allow: &[&str], deny: &[&str]) -> PolicyInput {
    PolicyInput {
        allow_out: allow.iter().map(|entry| (*entry).to_owned()).collect(),
        deny_out: deny.iter().map(|entry| (*entry).to_owned()).collect(),
        upstream: None,
    }
}

async fn policy_priorities() -> Vec<u32> {
    let rules = ip_ok(&["-4", "rule", "show"]).await;
    rayd_core::network::probe::policy_rule_priorities(&rules)
}

/// Egress option A (ADR-012 addendum): whether a uid-1000 DNS query (port
/// 53, `proto`) to `destination` would resolve to a route at all right
/// now (no packet sent, exactly like `route()` above).
async fn dns_query_resolves(destination: &str, proto: &str) -> bool {
    let output = run_ip(&[
        "route",
        "get",
        destination,
        "uid",
        "1000",
        "ipproto",
        proto,
        "dport",
        "53",
    ])
    .await
    .unwrap();
    output.code == 0
}

/// The lines of `ip -4 rule show` at `priority`.
async fn rule_lines_at(priority: u32) -> Vec<String> {
    let prefix = format!("{priority}:");
    ip_ok(&["-4", "rule", "show"])
        .await
        .lines()
        .filter(|line| line.trim_start().starts_with(&prefix))
        .map(str::to_owned)
        .collect()
}

#[tokio::test]
async fn policy_routes_swap_verify_and_recover_in_a_network_namespace() {
    let root = nix::unistd::geteuid().is_root();
    if !root || !detect_guest_capabilities().net_admin() || !fresh_namespace().await {
        assert!(
            std::env::var_os(REQUIRE_NETNS_ENV).is_none_or(|value| value != "1"),
            "m9_egress: {REQUIRE_NETNS_ENV}=1 but this is not root with CAP_NET_ADMIN \
             in a fresh network namespace"
        );
        eprintln!(
            "m9_egress: needs root with CAP_NET_ADMIN in a fresh network namespace \
             (sudo unshare --net); skipped"
        );
        return;
    }
    ip_ok(&["link", "set", "lo", "up"]).await;
    ip_ok(&["link", "add", "rtest0", "type", "dummy"]).await;
    ip_ok(&["addr", "add", "192.0.2.1/24", "dev", "rtest0"]).await;
    ip_ok(&["link", "set", "rtest0", "up"]).await;
    ip_ok(&["route", "add", "default", "dev", "rtest0"]).await;
    assert_eq!(install_imds_block().await, ImdsBlock::Installed);

    let session = Arc::new(SandboxSession::new(Arc::new(SystemClock::new()), "test"));
    let routes = Arc::new(FailingRoutes {
        inner: IpEgressRoutes,
        failing: Mutex::new(None),
    });
    let manager = NetworkManager::new(session.clone(), routes.clone(), true, ProxySeams::default());

    assert_eq!(
        manager.enforce_deny_all_at_run().await,
        EgressEnforcement::GuestRoutes
    );
    assert_eq!(route("198.51.100.7", "1000").await, RouteVerdict::Blocked);
    assert_eq!(route("198.51.100.7", "0").await, RouteVerdict::Routable);
    assert_eq!(route("127.0.0.1", "1000").await, RouteVerdict::Local);
    assert_eq!(
        route("169.254.169.254", "1000").await,
        RouteVerdict::Blocked
    );
    assert_eq!(policy_priorities().await, [150]);

    // Egress option A (ADR-012 addendum, `SECURITY.md` T17): under this
    // deny-all, uid 1000's own DNS queries must not resolve either,
    // closing the Q66 residual (`getaddrinfo` succeeding while every
    // actual connection is blocked).
    let local_lines = rule_lines_at(0).await;
    assert_eq!(local_lines.len(), 2, "{local_lines:?}");
    assert!(
        local_lines
            .iter()
            .all(|line| line.contains("prohibit") && line.contains("dport 53")),
        "{local_lines:?}"
    );
    let moved_local = rule_lines_at(1).await;
    assert_eq!(moved_local.len(), 1, "{moved_local:?}");
    assert!(moved_local[0].contains("lookup local"), "{moved_local:?}");
    assert!(
        !dns_query_resolves("127.0.0.53", "udp").await,
        "a loopback resolver's DNS port must not resolve under deny-all"
    );
    assert!(
        !dns_query_resolves("127.0.0.53", "tcp").await,
        "the TCP fallback path must not resolve either"
    );
    assert_eq!(
        route("127.0.0.53", "1000").await,
        RouteVerdict::Local,
        "ordinary loopback routing (no port selector) is untouched"
    );
    let other_port = run_ip(&[
        "route",
        "get",
        "127.0.0.53",
        "uid",
        "1000",
        "ipproto",
        "udp",
        "dport",
        "80",
    ])
    .await
    .unwrap();
    assert_eq!(
        other_port.code, 0,
        "only port 53 is blocked, not the rest of loopback: {other_port:?}"
    );

    let swapped = manager
        .update(input(&["198.51.100.7/32"], &["198.51.100.0/24"]))
        .await
        .unwrap();
    assert_eq!(swapped.enforcement, EgressEnforcement::GuestRoutes);
    assert_eq!(route("198.51.100.7", "1000").await, RouteVerdict::Routable);
    assert_eq!(route("198.51.100.8", "1000").await, RouteVerdict::Blocked);
    assert_eq!(route("1.1.1.1", "1000").await, RouteVerdict::Routable);
    assert_eq!(policy_priorities().await, [151]);

    let open = manager.update(input(&[], &[])).await.unwrap();
    assert_eq!(open.enforcement, EgressEnforcement::None);
    assert!(policy_priorities().await.is_empty());
    assert_eq!(route("198.51.100.8", "1000").await, RouteVerdict::Routable);
    assert_eq!(
        route("169.254.169.254", "1000").await,
        RouteVerdict::Blocked,
        "the IMDS rule is never touched"
    );
    assert!(
        rule_lines_at(1).await.is_empty(),
        "the moved local rule is dropped once the guard uninstalls"
    );
    assert_eq!(
        rule_lines_at(0).await.len(),
        1,
        "priority 0 is back to exactly the kernel's own local rule"
    );
    assert!(
        dns_query_resolves("127.0.0.53", "udp").await,
        "dns resolves again once deny-all lifts"
    );

    manager.update(input(&[], &[ALL_TRAFFIC])).await.unwrap();
    routes.fail_next("fill_table");
    assert_eq!(
        manager
            .update(input(&[], &["203.0.113.0/24"]))
            .await
            .unwrap_err(),
        NetworkError::InstallFailed { step: "fill_table" }
    );
    assert_eq!(route("1.1.1.1", "1000").await, RouteVerdict::Blocked);
    assert_eq!(session.egress_enforcement(), EgressEnforcement::GuestRoutes);

    routes.fail_next("add_rule");
    assert_eq!(
        manager
            .update(input(&[], &["203.0.113.0/24"]))
            .await
            .unwrap_err(),
        NetworkError::InstallFailed { step: "add_rule" }
    );
    assert_eq!(route("1.1.1.1", "1000").await, RouteVerdict::Blocked);
    assert_eq!(route("1.1.1.1", "0").await, RouteVerdict::Routable);
    assert_eq!(policy_priorities().await, [150]);
    assert_eq!(session.egress_enforcement(), EgressEnforcement::GuestRoutes);
}
