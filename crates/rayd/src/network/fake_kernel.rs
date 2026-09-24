//! An in-memory stand-in for the guest's policy routing, answering the
//! `EgressRoutes` calls the way Linux does: rules in priority order, a
//! blackhole in a looked-up table refuses the lookup, a table with no
//! covering route falls through to the next rule and finally to `main`.

use std::collections::BTreeMap;
use std::fmt::Write as _;
use std::net::IpAddr;
use std::sync::Mutex;
use std::time::Duration;

use rayd_core::network::{Cidr, Family, RouteStep, canonical_ip};

use crate::adapters::egress_routes::{EgressRoutes, RouteCommandError};

#[derive(Default)]
struct KernelState {
    rules: BTreeMap<(Family, u32), u32>,
    tables: BTreeMap<(Family, u32), Vec<Cidr>>,
    executed: Vec<String>,
    failing_step: Option<(&'static str, usize)>,
    fail_everything: bool,
    address_delay: Duration,
    address_panic: bool,
}

pub struct FakeKernel {
    state: Mutex<KernelState>,
    ipv6: bool,
    local: Vec<IpAddr>,
}

impl FakeKernel {
    pub fn new(ipv6: bool, local: Vec<IpAddr>) -> Self {
        Self {
            state: Mutex::new(KernelState::default()),
            ipv6,
            local,
        }
    }

    fn state(&self) -> std::sync::MutexGuard<'_, KernelState> {
        self.state.lock().unwrap()
    }

    /// The `occurrence`-th (1-based) execution of the step named `name`
    /// from now on fails.
    pub fn fail_step(&self, name: &'static str, occurrence: usize) {
        self.state().failing_step = Some((name, occurrence));
    }

    pub fn fail_everything(&self) {
        self.state().fail_everything = true;
    }

    /// Every `local_addresses` call panics, as a bug in the task would.
    pub fn panic_on_local_addresses(&self) {
        self.state().address_panic = true;
    }

    /// Every `local_addresses` call takes `delay`, like the first `ip`
    /// spawned by a guest just restored from its snapshot.
    pub fn delay_local_addresses(&self, delay: Duration) {
        self.state().address_delay = delay;
    }

    pub fn rule_priorities(&self, family: Family) -> Vec<u32> {
        self.state()
            .rules
            .keys()
            .filter(|(rule_family, _)| *rule_family == family)
            .map(|(_, priority)| *priority)
            .collect()
    }

    pub fn table(&self, family: Family, table: u32) -> Option<Vec<Cidr>> {
        self.state().tables.get(&(family, table)).cloned()
    }

    pub fn drop_table(&self, family: Family, table: u32) {
        self.state().tables.remove(&(family, table));
    }

    pub fn executed(&self) -> Vec<String> {
        self.state().executed.clone()
    }

    /// What `ip route get <ip> uid 1000` would decide.
    pub fn blocked(&self, ip: IpAddr) -> bool {
        let state = self.state();
        let ip = canonical_ip(ip);
        let family = Family::of(ip);
        state
            .rules
            .iter()
            .filter(|((rule_family, _), _)| *rule_family == family)
            .any(|(_, table)| {
                state
                    .tables
                    .get(&(family, *table))
                    .is_some_and(|prefixes| prefixes.iter().any(|prefix| prefix.contains(ip)))
            })
    }

    fn should_fail(state: &mut KernelState, name: &'static str) -> bool {
        if state.fail_everything {
            return true;
        }
        match &mut state.failing_step {
            Some((failing, remaining)) if *failing == name => {
                *remaining -= 1;
                if *remaining == 0 {
                    state.failing_step = None;
                    return true;
                }
                false
            }
            _ => false,
        }
    }
}

fn failure(reason: &str) -> RouteCommandError {
    RouteCommandError {
        reason: reason.to_owned(),
    }
}

#[tonic::async_trait]
impl EgressRoutes for FakeKernel {
    async fn execute(&self, step: &RouteStep) -> Result<(), RouteCommandError> {
        let mut state = self.state();
        state
            .executed
            .push(format!("{}:{}", step.name(), step.family().as_str()));
        if Self::should_fail(&mut state, step.name()) {
            return Err(failure("injected"));
        }
        match step {
            RouteStep::FillTable {
                slot,
                family,
                prefixes,
            } => {
                state
                    .tables
                    .insert((*family, slot.table()), prefixes.clone());
            }
            RouteStep::FlushTable { slot, family } => {
                state.tables.remove(&(*family, slot.table()));
            }
            RouteStep::AddRule { slot, family } => {
                state.rules.insert((*family, slot.priority()), slot.table());
            }
            RouteStep::DelRule { slot, family } => {
                if state.rules.remove(&(*family, slot.priority())).is_none() {
                    return Err(failure("rule del exit 2"));
                }
            }
        }
        Ok(())
    }

    async fn show_rules(&self, family: Family) -> Result<String, RouteCommandError> {
        let state = self.state();
        if state.fail_everything {
            return Err(failure("rule show exit 1"));
        }
        let mut out = String::from(
            "0:\tfrom all lookup local\n100:\tfrom all uidrange 1000-65535 lookup 100\n",
        );
        for ((rule_family, priority), table) in &state.rules {
            if *rule_family == family {
                let _ = writeln!(
                    out,
                    "{priority}:\tfrom all uidrange 1000-65535 lookup {table}"
                );
            }
        }
        out.push_str("32766:\tfrom all lookup main\n");
        Ok(out)
    }

    async fn show_table(&self, family: Family, table: u32) -> Result<String, RouteCommandError> {
        let state = self.state();
        Ok(state
            .tables
            .get(&(family, table))
            .map(|prefixes| {
                prefixes.iter().fold(String::new(), |mut out, prefix| {
                    let _ = writeln!(out, "blackhole {prefix} ");
                    out
                })
            })
            .unwrap_or_default())
    }

    async fn route_get(&self, destination: IpAddr) -> Result<(i32, String), RouteCommandError> {
        if destination.is_loopback() {
            return Ok((
                0,
                format!("local {destination} dev lo table local src {destination} uid 1000 \n"),
            ));
        }
        if self.blocked(destination) {
            return Ok((2, String::new()));
        }
        Ok((
            0,
            format!("{destination} via 192.0.2.254 dev eth0 src 192.0.2.1 uid 1000 \n"),
        ))
    }

    async fn local_addresses(&self) -> Result<Vec<IpAddr>, RouteCommandError> {
        let (delay, panics) = {
            let state = self.state();
            (state.address_delay, state.address_panic)
        };
        assert!(!panics, "injected local_addresses panic");
        tokio::time::sleep(delay).await;
        Ok(self.local.clone())
    }

    fn ipv6_present(&self) -> bool {
        self.ipv6
    }
}
