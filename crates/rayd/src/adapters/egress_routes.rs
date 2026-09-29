//! The egress policy's routing adapter (ADR-012, design D4-D6): executes
//! the pure `RouteStep` lists of `rayd_core::network::swap` with `ip`, and
//! reads back what the probe needs (`ip rule show`, `ip route show table`,
//! `ip route get <addr> uid 1000`, `ip -o addr show`).
//!
//! A fill is `ip route flush table N` followed by one `ip -batch -` with a
//! `route add blackhole <prefix> table N` line per prefix, which stops at
//! its first error. A table the guest never created answers a flush or a
//! show with "FIB table does not exist" (exit 2); both read that as an
//! empty table, since a fresh guest has none of the policy tables.
//! `AddRule` is idempotent (a rule `ip rule show` already lists is not
//! added again, since `ip rule add` refuses duplicates); `DelRule` of an
//! absent rule fails, which the recovery tolerates. `ip` output is never
//! logged: failures carry the step and the exit code only.

use std::fmt::{self, Write as _};
use std::net::IpAddr;
use std::path::Path;

use rayd_core::network::probe::{interface_addresses, rule_present, table_missing};
use rayd_core::network::route_plan::SANDBOX_UID_RANGE;
use rayd_core::network::{
    Cidr, DEFAULT_LOCAL_PRIORITY, DNS_BLOCK_PORT, DNS_BLOCK_PRIORITY, DnsGuardStep, DnsProto,
    Family, LOCAL_TABLE, MOVED_LOCAL_PRIORITY, RouteStep, Slot,
};

use super::ip_command::{IpOutput, family_flag, run_ip, run_ip_for, run_ip_with_input};

/// `/proc/net/if_inet6` exists only when the guest kernel has IPv6.
pub const IF_INET6: &str = "/proc/net/if_inet6";
/// The uid every probe lookup is made as: the sandbox user.
pub const PROBE_UID: &str = "1000";

/// Why an `ip` invocation failed: the step and an exit code or a fixed
/// phrase, never `ip` output.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RouteCommandError {
    pub reason: String,
}

impl RouteCommandError {
    fn exit(what: &str, family: Family, output: &IpOutput) -> Self {
        Self {
            reason: format!("{what} {} exit {}", family.as_str(), output.code),
        }
    }
}

impl fmt::Display for RouteCommandError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.reason)
    }
}

/// What the egress manager needs from the guest's routing; the tests use
/// an in-memory kernel.
#[tonic::async_trait]
pub trait EgressRoutes: Send + Sync {
    async fn execute(&self, step: &RouteStep) -> Result<(), RouteCommandError>;
    /// Egress option A (ADR-012 addendum, `SECURITY.md` T17): the DNS
    /// guard's own steps, kept off `RouteStep` since they carry no `Slot`
    /// (no table, and the `local` rule steps have no uid selector at all).
    async fn execute_dns_guard(&self, step: &DnsGuardStep) -> Result<(), RouteCommandError>;
    /// Stdout of `ip -4|-6 rule show`.
    async fn show_rules(&self, family: Family) -> Result<String, RouteCommandError>;
    /// Stdout of `ip -4|-6 route show table <table>`.
    async fn show_table(&self, family: Family, table: u32) -> Result<String, RouteCommandError>;
    /// Exit code and stdout of `ip route get <destination> uid 1000`.
    async fn route_get(&self, destination: IpAddr) -> Result<(i32, String), RouteCommandError>;
    /// Every address currently assigned to a guest interface.
    async fn local_addresses(&self) -> Result<Vec<IpAddr>, RouteCommandError>;
    fn ipv6_present(&self) -> bool;
}

/// The real adapter over `ip`.
#[derive(Debug, Clone, Copy, Default)]
pub struct IpEgressRoutes;

async fn ip(family: Family, args: &[&str]) -> Result<IpOutput, RouteCommandError> {
    run_ip_for(family, args)
        .await
        .map_err(|reason| RouteCommandError { reason })
}

async fn ip_ok(what: &str, family: Family, args: &[&str]) -> Result<IpOutput, RouteCommandError> {
    let output = ip(family, args).await?;
    if output.succeeded() {
        Ok(output)
    } else {
        Err(RouteCommandError::exit(what, family, &output))
    }
}

/// One `route add blackhole <prefix> table <table>` line per prefix.
#[must_use]
pub fn blackhole_batch(prefixes: &[Cidr], table: u32) -> String {
    prefixes.iter().fold(String::new(), |mut batch, prefix| {
        let _ = writeln!(batch, "route add blackhole {prefix} table {table}");
        batch
    })
}

fn rule_args(verb: &'static str, slot: Slot) -> [String; 8] {
    [
        "rule".to_owned(),
        verb.to_owned(),
        "uidrange".to_owned(),
        SANDBOX_UID_RANGE.to_owned(),
        "lookup".to_owned(),
        slot.table().to_string(),
        "priority".to_owned(),
        slot.priority().to_string(),
    ]
}

/// The lines of `ip rule show`'s stdout whose priority is `priority`
/// (there can be more than one at the same priority).
fn lines_at_priority(rules: &str, priority: u32) -> impl Iterator<Item = &str> {
    let prefix = format!("{priority}:");
    rules
        .lines()
        .filter(move |line| line.trim_start().starts_with(&prefix))
}

fn is_local_line(line: &str) -> bool {
    line.contains(LOCAL_TABLE)
}

fn is_dns_block_line(line: &str, proto: DnsProto) -> bool {
    line.contains(SANDBOX_UID_RANGE)
        && line.contains(&format!("ipproto {}", proto.as_str()))
        && line.contains(&format!("dport {DNS_BLOCK_PORT}"))
        && line.contains("prohibit")
}

impl IpEgressRoutes {
    async fn flush(family: Family, slot: Slot) -> Result<(), RouteCommandError> {
        let table = slot.table().to_string();
        let output = ip(family, &["route", "flush", "table", &table]).await?;
        if output.succeeded() || table_missing(output.code, &output.stderr) {
            Ok(())
        } else {
            Err(RouteCommandError::exit("route flush", family, &output))
        }
    }

    async fn fill(family: Family, slot: Slot, prefixes: &[Cidr]) -> Result<(), RouteCommandError> {
        Self::flush(family, slot).await?;
        if prefixes.is_empty() {
            return Ok(());
        }
        let batch = blackhole_batch(prefixes, slot.table());
        let output = run_ip_with_input(
            &[family_flag(family), "-batch", "-"],
            Some(batch.as_bytes()),
        )
        .await
        .map_err(|reason| RouteCommandError { reason })?;
        if output.succeeded() {
            Ok(())
        } else {
            Err(RouteCommandError::exit("route batch", family, &output))
        }
    }

    async fn add_rule(family: Family, slot: Slot) -> Result<(), RouteCommandError> {
        let rules = ip_ok("rule show", family, &["rule", "show"]).await?;
        if rule_present(&rules.stdout, slot) {
            return Ok(());
        }
        let args = rule_args("add", slot);
        let args: Vec<&str> = args.iter().map(String::as_str).collect();
        ip_ok("rule add", family, &args).await.map(drop)
    }

    async fn del_rule(family: Family, slot: Slot) -> Result<(), RouteCommandError> {
        let args = rule_args("del", slot);
        let args: Vec<&str> = args.iter().map(String::as_str).collect();
        ip_ok("rule del", family, &args).await.map(drop)
    }

    /// `ip rule show` filtered to the lines at `priority` (there can be
    /// more than one, e.g. the two DNS block rules that share priority 0).
    async fn rule_lines(family: Family, priority: u32) -> Result<Vec<String>, RouteCommandError> {
        let rules = ip_ok("rule show", family, &["rule", "show"]).await?;
        Ok(lines_at_priority(&rules.stdout, priority)
            .map(str::to_owned)
            .collect())
    }

    async fn local_rule_present(family: Family, priority: u32) -> Result<bool, RouteCommandError> {
        let lines = Self::rule_lines(family, priority).await?;
        Ok(lines.iter().any(|line| is_local_line(line)))
    }

    /// `ip rule add|del priority <p> lookup local`.
    fn local_rule_args(verb: &'static str, priority: u32) -> [String; 6] {
        [
            "rule".to_owned(),
            verb.to_owned(),
            "priority".to_owned(),
            priority.to_string(),
            "lookup".to_owned(),
            LOCAL_TABLE.to_owned(),
        ]
    }

    async fn add_local_rule(family: Family, priority: u32) -> Result<(), RouteCommandError> {
        if Self::local_rule_present(family, priority).await? {
            return Ok(());
        }
        let args = Self::local_rule_args("add", priority);
        let args: Vec<&str> = args.iter().map(String::as_str).collect();
        ip_ok("dns_guard rule add", family, &args).await.map(drop)
    }

    async fn del_local_rule(family: Family, priority: u32) -> Result<(), RouteCommandError> {
        if !Self::local_rule_present(family, priority).await? {
            return Ok(());
        }
        let args = Self::local_rule_args("del", priority);
        let args: Vec<&str> = args.iter().map(String::as_str).collect();
        ip_ok("dns_guard rule del", family, &args).await.map(drop)
    }

    async fn dns_block_present(family: Family, proto: DnsProto) -> Result<bool, RouteCommandError> {
        let lines = Self::rule_lines(family, DNS_BLOCK_PRIORITY).await?;
        Ok(lines.iter().any(|line| is_dns_block_line(line, proto)))
    }

    fn dns_block_args(verb: &'static str, proto: DnsProto) -> [String; 10] {
        [
            "rule".to_owned(),
            verb.to_owned(),
            "priority".to_owned(),
            DNS_BLOCK_PRIORITY.to_string(),
            "uidrange".to_owned(),
            SANDBOX_UID_RANGE.to_owned(),
            "ipproto".to_owned(),
            proto.as_str().to_owned(),
            "dport".to_owned(),
            DNS_BLOCK_PORT.to_string(),
        ]
    }

    async fn add_dns_block(family: Family, proto: DnsProto) -> Result<(), RouteCommandError> {
        if Self::dns_block_present(family, proto).await? {
            return Ok(());
        }
        let mut args = Self::dns_block_args("add", proto).to_vec();
        args.push("prohibit".to_owned());
        let args: Vec<&str> = args.iter().map(String::as_str).collect();
        ip_ok("dns_guard block add", family, &args).await.map(drop)
    }

    async fn del_dns_block(family: Family, proto: DnsProto) -> Result<(), RouteCommandError> {
        if !Self::dns_block_present(family, proto).await? {
            return Ok(());
        }
        let mut args = Self::dns_block_args("del", proto).to_vec();
        args.push("prohibit".to_owned());
        let args: Vec<&str> = args.iter().map(String::as_str).collect();
        ip_ok("dns_guard block del", family, &args).await.map(drop)
    }
}

#[tonic::async_trait]
impl EgressRoutes for IpEgressRoutes {
    async fn execute(&self, step: &RouteStep) -> Result<(), RouteCommandError> {
        match step {
            RouteStep::FillTable {
                slot,
                family,
                prefixes,
            } => Self::fill(*family, *slot, prefixes).await,
            RouteStep::FlushTable { slot, family } => Self::flush(*family, *slot).await,
            RouteStep::AddRule { slot, family } => Self::add_rule(*family, *slot).await,
            RouteStep::DelRule { slot, family } => Self::del_rule(*family, *slot).await,
        }
    }

    async fn execute_dns_guard(&self, step: &DnsGuardStep) -> Result<(), RouteCommandError> {
        match *step {
            DnsGuardStep::AddMovedLocal { family } => {
                Self::add_local_rule(family, MOVED_LOCAL_PRIORITY).await
            }
            DnsGuardStep::DelDefaultLocal { family } => {
                Self::del_local_rule(family, DEFAULT_LOCAL_PRIORITY).await
            }
            DnsGuardStep::AddDefaultLocal { family } => {
                Self::add_local_rule(family, DEFAULT_LOCAL_PRIORITY).await
            }
            DnsGuardStep::DelMovedLocal { family } => {
                Self::del_local_rule(family, MOVED_LOCAL_PRIORITY).await
            }
            DnsGuardStep::AddDnsBlock { family, proto } => Self::add_dns_block(family, proto).await,
            DnsGuardStep::DelDnsBlock { family, proto } => Self::del_dns_block(family, proto).await,
        }
    }

    async fn show_rules(&self, family: Family) -> Result<String, RouteCommandError> {
        ip_ok("rule show", family, &["rule", "show"])
            .await
            .map(|output| output.stdout)
    }

    async fn show_table(&self, family: Family, table: u32) -> Result<String, RouteCommandError> {
        let table = table.to_string();
        let output = ip(family, &["route", "show", "table", &table]).await?;
        if output.succeeded() {
            Ok(output.stdout)
        } else if table_missing(output.code, &output.stderr) {
            Ok(String::new())
        } else {
            Err(RouteCommandError::exit("route show", family, &output))
        }
    }

    async fn route_get(&self, destination: IpAddr) -> Result<(i32, String), RouteCommandError> {
        let destination = destination.to_string();
        let output = run_ip(&["route", "get", &destination, "uid", PROBE_UID])
            .await
            .map_err(|reason| RouteCommandError { reason })?;
        Ok((output.code, output.stdout))
    }

    async fn local_addresses(&self) -> Result<Vec<IpAddr>, RouteCommandError> {
        let output = run_ip(&["-o", "addr", "show"])
            .await
            .map_err(|reason| RouteCommandError { reason })?;
        if output.succeeded() {
            Ok(interface_addresses(&output.stdout))
        } else {
            Err(RouteCommandError {
                reason: format!("addr show exit {}", output.code),
            })
        }
    }

    fn ipv6_present(&self) -> bool {
        Path::new(IF_INET6).exists()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_batch_has_one_blackhole_line_per_prefix() {
        let prefixes = [
            Cidr::parse("198.51.100.0/30").unwrap(),
            Cidr::parse("0.0.0.0/0").unwrap(),
        ];
        assert_eq!(
            blackhole_batch(&prefixes, 101),
            "route add blackhole 198.51.100.0/30 table 101\nroute add blackhole 0.0.0.0/0 table 101\n"
        );
        assert_eq!(
            blackhole_batch(&[Cidr::everything(Family::V6)], 102),
            "route add blackhole ::/0 table 102\n"
        );
        assert!(blackhole_batch(&[], 101).is_empty());
    }

    #[test]
    fn rule_arguments_carry_the_slot() {
        assert_eq!(
            rule_args("add", Slot::B).join(" "),
            "rule add uidrange 1000-65535 lookup 102 priority 151"
        );
        assert_eq!(
            rule_args("del", Slot::Emergency).join(" "),
            "rule del uidrange 1000-65535 lookup 103 priority 149"
        );
    }

    #[test]
    fn local_rule_arguments_move_or_restore_local() {
        assert_eq!(
            IpEgressRoutes::local_rule_args("add", MOVED_LOCAL_PRIORITY).join(" "),
            "rule add priority 1 lookup local"
        );
        assert_eq!(
            IpEgressRoutes::local_rule_args("del", DEFAULT_LOCAL_PRIORITY).join(" "),
            "rule del priority 0 lookup local"
        );
    }

    #[test]
    fn dns_block_arguments_carry_the_proto_and_port() {
        let mut args = IpEgressRoutes::dns_block_args("add", DnsProto::Udp).to_vec();
        args.push("prohibit".to_owned());
        assert_eq!(
            args.join(" "),
            "rule add priority 0 uidrange 1000-65535 ipproto udp dport 53 prohibit"
        );
        let mut args = IpEgressRoutes::dns_block_args("del", DnsProto::Tcp).to_vec();
        args.push("prohibit".to_owned());
        assert_eq!(
            args.join(" "),
            "rule del priority 0 uidrange 1000-65535 ipproto tcp dport 53 prohibit"
        );
    }

    /// A representative `ip rule show` dump: the kernel's untouched
    /// defaults, plus what the guard looks like once installed.
    const RULE_SHOW_AFTER_INSTALL: &str = "\
1:\tfrom all lookup local\n\
0:\tfrom all uidrange 1000-65535 ipproto udp dport 53 prohibit\n\
0:\tfrom all uidrange 1000-65535 ipproto tcp dport 53 prohibit\n\
100:\tfrom all uidrange 1000-65535 lookup 100\n\
32766:\tfrom all lookup main\n";

    #[test]
    fn lines_at_priority_keeps_every_line_that_shares_it() {
        let lines: Vec<&str> = lines_at_priority(RULE_SHOW_AFTER_INSTALL, 0).collect();
        assert_eq!(lines.len(), 2);
        assert!(lines.iter().all(|line| line.contains("prohibit")));
        assert_eq!(lines_at_priority(RULE_SHOW_AFTER_INSTALL, 1).count(), 1);
        assert_eq!(lines_at_priority(RULE_SHOW_AFTER_INSTALL, 149).count(), 0);
    }

    #[test]
    fn is_local_line_matches_only_the_lookup_local_rule() {
        let local = lines_at_priority(RULE_SHOW_AFTER_INSTALL, 1)
            .next()
            .unwrap();
        assert!(is_local_line(local));
        let block = lines_at_priority(RULE_SHOW_AFTER_INSTALL, 0)
            .next()
            .unwrap();
        assert!(!is_local_line(block));
    }

    #[test]
    fn is_dns_block_line_matches_its_own_protocol_only() {
        let at_zero: Vec<&str> = lines_at_priority(RULE_SHOW_AFTER_INSTALL, 0).collect();
        assert!(
            at_zero
                .iter()
                .any(|line| is_dns_block_line(line, DnsProto::Udp))
        );
        assert!(
            at_zero
                .iter()
                .any(|line| is_dns_block_line(line, DnsProto::Tcp))
        );
        let local = lines_at_priority(RULE_SHOW_AFTER_INSTALL, 1)
            .next()
            .unwrap();
        assert!(!is_dns_block_line(local, DnsProto::Udp));
    }

    #[test]
    fn command_errors_carry_the_step_and_exit_code_only() {
        let error = RouteCommandError::exit(
            "rule add",
            Family::V6,
            &IpOutput {
                code: 2,
                stdout: "secret output".to_owned(),
                stderr: "secret error".to_owned(),
            },
        );
        assert_eq!(error.to_string(), "rule add v6 exit 2");
    }
}
