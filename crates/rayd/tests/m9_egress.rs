//! The egress routes against a real kernel (ADR-012, design D18
//! `m9_egress`): as root inside a fresh network namespace (CI runs this
//! binary under `sudo unshare --net`), `lo` up, a dummy `rtest0` with
//! `192.0.2.1/24` and a default route through it. Then the IMDS block plus
//! the `/run` deny-all, a swap to `deny 198.51.100.0/24, allow
//! 198.51.100.7/32`, a swap to unrestricted, and injected failures while
//! filling and while committing, each checked with `ip route get <addr>
//! uid <n>` (no packet is sent).
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
    ALL_TRAFFIC, EgressEnforcement, Family, NetworkError, PolicyInput, RouteStep,
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
