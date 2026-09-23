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
use rayd_core::network::{Cidr, Family, RouteStep, Slot};

use super::ip_command::{IpOutput, run_ip, run_ip_with_input};

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

fn family_flag(family: Family) -> &'static str {
    match family {
        Family::V4 => "-4",
        Family::V6 => "-6",
    }
}

async fn ip(family: Family, args: &[&str]) -> Result<IpOutput, RouteCommandError> {
    let mut full = Vec::with_capacity(args.len() + 1);
    full.push(family_flag(family));
    full.extend_from_slice(args);
    run_ip(&full)
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
