//! The IMDS block for the sandbox user (design D4, mechanism ratified
//! after measurement): a policy route. Sockets owned by uids 1000-65535
//! consult routing table `100` first, where `169.254.169.254/32` is a
//! `blackhole`, so their connects fail at once with `EINVAL` and never
//! leave the guest; root (`rayd` itself) keeps the main table and still
//! reaches IMDS. The range starts at 1000 on purpose: the platform's
//! in-VM agent owns sockets as uids 991-994 and keeps its own connection
//! to `169.254.169.254:80` (measured, Q48); a blackhole for every non-root
//! uid cut that channel and the image build's `/validate` timed out after
//! 10 minutes. Installed at boot when the guest
//! grants `CAP_NET_ADMIN` (the image variant published with
//! `additionalOsCapabilities: ["ALL"]`), re-checked at `/run` and `/resume`,
//! and verified by two connects: as root (must succeed, nothing is ever
//! sent) and as uid 1000 through the process spawner (must fail).
//! Everything is fail-open and logged: without the capability, without `ip`
//! or on a non-zero exit `rayd` keeps serving with `imds_blocked = false`.
//! `ip` output and the probes' output are never logged.
//!
//! Why routing and not the `iptables -m owner` rule the design started
//! with: measured on `rayito-base-caps` 2.0 (Q48), the Firecracker guest
//! kernel (6.1 amzn2023, no loadable modules) ships `x_tables` with only
//! `conntrack icmp addrtype udplite udp tcp` as matches, so
//! `iptables-legacy` and the `nf_tables` front-end both fail with
//! "Extension owner revision 0 not supported". `ip rule ... uidrange`
//! (in the kernel since 4.10) is the uid selector that is there.

use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

pub const IMDS_ADDRESS: &str = "169.254.169.254";
pub const IMDS_PORT: u16 = 80;
/// Bound on the whole verification task spawned by the first accepted
/// `/run` (route check, root connect, uid-1000 connect).
pub const IMDS_VERIFY_BUDGET: Duration = Duration::from_secs(10);
/// The root connect: IMDS answers on the link-local address within
/// milliseconds; a second means it is not there.
pub const ROOT_CONNECT_TIMEOUT: Duration = Duration::from_secs(1);
/// The routing table that holds the blackhole and the priority of the rule
/// that sends non-root sockets to it (before `main` at 32766).
pub const IMDS_ROUTE_TABLE: &str = "100";
pub const IMDS_RULE_PRIORITY: &str = "100";
/// The sandbox user and anything it could become; never the platform's
/// agent uids (991-994) nor root. The egress policy rules share it.
pub const SANDBOX_UID_RANGE: &str = rayd_core::network::route_plan::SANDBOX_UID_RANGE;
pub use super::ip_command::IP_BINARIES;
/// The IPv6 IMDS endpoint (best effort, never part of `imds_blocked`).
pub const IMDS_ADDRESS_V6: &str = "fd00:ec2::254";

/// What the health probe reports about the block.
#[derive(Debug, Default)]
pub struct ImdsState {
    installed: AtomicBool,
    blocked: AtomicBool,
}

impl ImdsState {
    /// Whether the route was installed at boot (so `/run` and `/resume`
    /// should keep checking it).
    #[must_use]
    pub fn installed(&self) -> bool {
        self.installed.load(Ordering::Acquire)
    }

    pub fn mark_installed(&self) {
        self.installed.store(true, Ordering::Release);
    }

    #[must_use]
    pub fn blocked(&self) -> bool {
        self.blocked.load(Ordering::Acquire)
    }

    pub fn set_blocked(&self, blocked: bool) {
        self.blocked.store(blocked, Ordering::Release);
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ImdsBlock {
    Installed,
    /// Why nothing was installed: the reason is an exit code or a short
    /// fixed phrase, never `ip` output.
    Unavailable {
        reason: String,
    },
}

/// Runs the uid-1000 connect through the sandbox's own process spawner;
/// `Some(reachable)` when the probe ran, `None` when it could not.
pub type UserConnectProbe =
    Arc<dyn Fn() -> Pin<Box<dyn Future<Output = Option<bool>> + Send>> + Send + Sync>;

/// The Python one-liner the uid-1000 probe runs: exit 0 when the connect
/// succeeds, 1 when it fails or times out.
pub const USER_PROBE_PROGRAM: &str = "/usr/bin/python3";
pub const USER_PROBE_CODE: &str = "import socket, sys\n\
s = socket.socket()\n\
s.settimeout(2)\n\
try:\n    s.connect((\"169.254.169.254\", 80))\n\
except OSError:\n    sys.exit(1)\n\
sys.exit(0)\n";

/// One verification pass (design D4 steps 1-3), logged as `imds_probe`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ImdsProbe {
    pub rule_present: bool,
    pub root_reachable: bool,
    /// `None` when the uid-1000 probe could not run.
    pub user_reachable: Option<bool>,
}

impl ImdsProbe {
    /// The route is in place, root still reaches IMDS and uid 1000 does not.
    #[must_use]
    pub fn imds_blocked(&self) -> bool {
        self.rule_present && self.root_reachable && self.user_reachable == Some(false)
    }
}

/// Steps 1-3 of the verification, storing the verdict in `state`. The
/// route is re-installed first when it is missing: a resumed or cloned VM
/// must not trust the snapshot blindly.
pub async fn verify_imds_block(
    state: &ImdsState,
    user_probe: Option<&UserConnectProbe>,
) -> ImdsProbe {
    let rule_present = ensure_rule().await;
    let root_reachable = probe_root().await;
    let user_reachable = match user_probe {
        Some(probe) => probe().await,
        None => None,
    };
    let probe = ImdsProbe {
        rule_present,
        root_reachable,
        user_reachable,
    };
    state.set_blocked(probe.imds_blocked());
    probe
}

async fn ensure_rule() -> bool {
    if rule_present().await {
        return true;
    }
    install_imds_block().await == ImdsBlock::Installed && rule_present().await
}

/// What `ip rule show` prints for the uid selector.
#[must_use]
pub fn rule_signature() -> String {
    format!("uidrange {SANDBOX_UID_RANGE} lookup {IMDS_ROUTE_TABLE}")
}

/// What `ip route show table 100` prints for the blackhole.
#[must_use]
pub fn route_signature() -> String {
    format!("blackhole {IMDS_ADDRESS}")
}

#[cfg(unix)]
pub use unix::{install_imds_block, probe_root, rule_present};

#[cfg(not(unix))]
pub use unsupported::{install_imds_block, probe_root, rule_present};

#[cfg(unix)]
mod unix {
    use tokio::net::TcpStream;

    use super::{
        IMDS_ADDRESS, IMDS_ADDRESS_V6, IMDS_PORT, IMDS_ROUTE_TABLE, IMDS_RULE_PRIORITY, ImdsBlock,
        ROOT_CONNECT_TIMEOUT, SANDBOX_UID_RANGE, route_signature, rule_signature,
    };
    use crate::adapters::ip_command::{IpOutput, run_ip};

    #[derive(Debug, Clone, Copy)]
    enum Family {
        V4,
        V6,
    }

    impl Family {
        fn flag(self) -> &'static str {
            match self {
                Self::V4 => "-4",
                Self::V6 => "-6",
            }
        }

        fn prefix(self) -> String {
            match self {
                Self::V4 => format!("{IMDS_ADDRESS}/32"),
                Self::V6 => format!("{IMDS_ADDRESS_V6}/128"),
            }
        }

        fn blackhole_signature(self) -> String {
            match self {
                Self::V4 => route_signature(),
                Self::V6 => format!("blackhole {IMDS_ADDRESS_V6}"),
            }
        }
    }

    /// Runs `ip <family> <args>` through the shared runner; stdout comes
    /// back for the `show` checks, stderr is discarded.
    async fn ip(family: Family, args: &[&str]) -> Result<IpOutput, String> {
        let mut full = Vec::with_capacity(args.len() + 1);
        full.push(family.flag());
        full.extend_from_slice(args);
        run_ip(&full).await
    }

    async fn rule_installed(family: Family) -> bool {
        matches!(
            ip(family, &["rule", "show"]).await,
            Ok(completed) if completed.code == 0 && completed.stdout.contains(&rule_signature())
        )
    }

    async fn route_installed(family: Family) -> bool {
        let signature = family.blackhole_signature();
        matches!(
            ip(family, &["route", "show", "table", IMDS_ROUTE_TABLE]).await,
            Ok(completed) if completed.code == 0 && completed.stdout.contains(&signature)
        )
    }

    /// `ip rule add` refuses an identical rule with `EEXIST`, so the rule is
    /// only added when `show` does not list it; `route replace` is
    /// idempotent by itself.
    async fn install_family(family: Family) -> Result<(), String> {
        if !rule_installed(family).await {
            let rule = [
                "rule",
                "add",
                "uidrange",
                SANDBOX_UID_RANGE,
                "lookup",
                IMDS_ROUTE_TABLE,
                "priority",
                IMDS_RULE_PRIORITY,
            ];
            expect_success(family, "rule add", &ip(family, &rule).await?)?;
        }
        let prefix = family.prefix();
        let route = [
            "route",
            "replace",
            "blackhole",
            prefix.as_str(),
            "table",
            IMDS_ROUTE_TABLE,
        ];
        expect_success(family, "route replace", &ip(family, &route).await?)
    }

    fn expect_success(family: Family, step: &str, completed: &IpOutput) -> Result<(), String> {
        if completed.code == 0 {
            Ok(())
        } else {
            Err(format!(
                "ip {} {step} exit {}",
                family.flag(),
                completed.code
            ))
        }
    }

    /// Installs the IPv4 rule and blackhole; the IPv6 pair is attempted
    /// the same way and its failure only logged at `debug`.
    pub async fn install_imds_block() -> ImdsBlock {
        match install_family(Family::V4).await {
            Ok(()) => {
                install_v6_best_effort().await;
                ImdsBlock::Installed
            }
            Err(reason) => ImdsBlock::Unavailable { reason },
        }
    }

    async fn install_v6_best_effort() {
        match install_family(Family::V6).await {
            Ok(()) => tracing::debug!("imds v6 block installed"),
            Err(reason) => tracing::debug!(reason = %reason, "imds v6 block skipped"),
        }
    }

    /// Both halves listed by `show`: the uid rule and the blackhole.
    pub async fn rule_present() -> bool {
        rule_installed(Family::V4).await && route_installed(Family::V4).await
    }

    /// TCP connect as this process (root): success within a second and
    /// nothing sent, so `rayd` never reads credentials.
    pub async fn probe_root() -> bool {
        let connect = TcpStream::connect((IMDS_ADDRESS, IMDS_PORT));
        matches!(
            tokio::time::timeout(ROOT_CONNECT_TIMEOUT, connect).await,
            Ok(Ok(_))
        )
    }
}

#[cfg(not(unix))]
mod unsupported {
    use super::ImdsBlock;

    pub async fn install_imds_block() -> ImdsBlock {
        ImdsBlock::Unavailable {
            reason: "unsupported platform".to_owned(),
        }
    }

    pub async fn rule_present() -> bool {
        false
    }

    pub async fn probe_root() -> bool {
        false
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn imds_blocked_needs_all_three_facts() {
        let blocked = ImdsProbe {
            rule_present: true,
            root_reachable: true,
            user_reachable: Some(false),
        };
        assert!(blocked.imds_blocked());
        for open in [
            ImdsProbe {
                rule_present: false,
                ..blocked
            },
            ImdsProbe {
                root_reachable: false,
                ..blocked
            },
            ImdsProbe {
                user_reachable: Some(true),
                ..blocked
            },
            ImdsProbe {
                user_reachable: None,
                ..blocked
            },
        ] {
            assert!(!open.imds_blocked(), "{open:?}");
        }
    }

    #[test]
    fn state_starts_open_and_records_the_verdict() {
        let state = ImdsState::default();
        assert!(!state.installed());
        assert!(!state.blocked());
        state.mark_installed();
        state.set_blocked(true);
        assert!(state.installed());
        assert!(state.blocked());
        state.set_blocked(false);
        assert!(!state.blocked());
    }

    #[test]
    fn the_user_probe_code_exits_non_zero_when_unreachable() {
        assert!(USER_PROBE_CODE.contains("sys.exit(1)"));
        assert!(USER_PROBE_CODE.contains(IMDS_ADDRESS));
        assert!(!USER_PROBE_CODE.contains("security-credentials"));
    }

    #[test]
    fn the_signatures_match_what_ip_prints() {
        assert_eq!(rule_signature(), "uidrange 1000-65535 lookup 100");
        assert_eq!(route_signature(), "blackhole 169.254.169.254");
    }
}
