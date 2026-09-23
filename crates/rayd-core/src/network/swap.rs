//! The atomic policy swap, its rollback and the deny-all recovery (design
//! D5) as pure step lists the adapter executes in order.
//!
//! A swap fills the idle slot's table for every managed family, then adds
//! its rule, then deletes the active rule and flushes the active table.
//! The kernel matches the lowest priority first and the two policy slots
//! alternate 150/151, so from the first `AddRule` on the uid range always
//! resolves through a complete table: the old one until its rule is
//! deleted, or the new one as soon as its rule wins. A failure while
//! filling is undone by flushing the idle table and leaves the old policy
//! in force; a failure after that runs the recovery.
//!
//! The recovery installs an emergency deny-all at priority 149 before it
//! touches either slot, rebuilds slot `A` with the requested plan and only
//! then removes the emergency rule. With the deny-all plan it is D5's
//! recovery; with the stored plan it is the reinstall `/resume` runs when
//! verification fails.

use super::cidr::{Cidr, Family};
use super::route_plan::{RoutePlan, Slot, managed_families};

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RouteStep {
    /// Flush the slot's table and refill it with one blackhole per prefix.
    FillTable {
        slot: Slot,
        family: Family,
        prefixes: Vec<Cidr>,
    },
    FlushTable {
        slot: Slot,
        family: Family,
    },
    /// `uidrange 1000-65535 lookup <table> priority <priority>`.
    AddRule {
        slot: Slot,
        family: Family,
    },
    DelRule {
        slot: Slot,
        family: Family,
    },
}

impl RouteStep {
    /// The fixed name logged and returned in `egress_update_failed: <step>`.
    #[must_use]
    pub fn name(&self) -> &'static str {
        match self {
            Self::FillTable { .. } => "fill_table",
            Self::FlushTable { .. } => "flush_table",
            Self::AddRule { .. } => "add_rule",
            Self::DelRule { .. } => "del_rule",
        }
    }

    #[must_use]
    pub fn family(&self) -> Family {
        match self {
            Self::FillTable { family, .. }
            | Self::FlushTable { family, .. }
            | Self::AddRule { family, .. }
            | Self::DelRule { family, .. } => *family,
        }
    }
}

/// A recovery step; `tolerate_failure` marks the cleanup of the policy
/// slots, where a rule that is already gone is the expected outcome.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PlannedStep {
    pub step: RouteStep,
    pub tolerate_failure: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SwapPlan {
    /// Filling the idle table: nothing points at it yet.
    pub fill: Vec<RouteStep>,
    /// Switching rules and cleaning the old slot.
    pub commit: Vec<RouteStep>,
    /// The slot that holds the policy afterwards; `None` for unrestricted.
    pub next_slot: Option<Slot>,
}

impl SwapPlan {
    /// Undoes a failed `fill`: the idle table is flushed, the active one is
    /// untouched.
    #[must_use]
    pub fn rollback(&self) -> Vec<RouteStep> {
        self.fill
            .iter()
            .filter_map(|step| match step {
                RouteStep::FillTable { slot, family, .. } => Some(RouteStep::FlushTable {
                    slot: *slot,
                    family: *family,
                }),
                _ => None,
            })
            .collect()
    }
}

/// From the `active` slot (if any) to `next` (`None` = unrestricted).
#[must_use]
pub fn plan_swap(active: Option<Slot>, next: Option<&RoutePlan>, ipv6_present: bool) -> SwapPlan {
    let families = managed_families(ipv6_present);
    let next_slot = next.map(|_| active.map_or(Slot::A, Slot::other));
    let fill = match (next, next_slot) {
        (Some(plan), Some(slot)) => fill_steps(slot, plan, families),
        _ => Vec::new(),
    };
    let mut commit = Vec::new();
    if let Some(slot) = next_slot {
        commit.extend(per_family(families, |family| RouteStep::AddRule {
            slot,
            family,
        }));
    }
    if let Some(slot) = active {
        commit.extend(per_family(families, |family| RouteStep::DelRule {
            slot,
            family,
        }));
        commit.extend(per_family(families, |family| RouteStep::FlushTable {
            slot,
            family,
        }));
    }
    SwapPlan {
        fill,
        commit,
        next_slot,
    }
}

/// Emergency deny-all, both slots cleared, slot `A` rebuilt with `plan`,
/// emergency removed. The result holds `plan` in slot `A`.
#[must_use]
pub fn plan_recovery(plan: &RoutePlan) -> Vec<PlannedStep> {
    let families = plan.families();
    let emergency = Slot::Emergency;
    let deny_all = RoutePlan::deny_all(plan.ipv6());
    let strict = |step| PlannedStep {
        step,
        tolerate_failure: false,
    };
    let tolerant = |step| PlannedStep {
        step,
        tolerate_failure: true,
    };
    let mut steps: Vec<PlannedStep> = fill_steps(emergency, &deny_all, families)
        .into_iter()
        .map(strict)
        .collect();
    steps.extend(
        per_family(families, |family| RouteStep::AddRule {
            slot: emergency,
            family,
        })
        .into_iter()
        .map(strict),
    );
    for slot in Slot::POLICY {
        steps.extend(
            per_family(families, |family| RouteStep::DelRule { slot, family })
                .into_iter()
                .map(tolerant),
        );
        steps.extend(
            per_family(families, |family| RouteStep::FlushTable { slot, family })
                .into_iter()
                .map(tolerant),
        );
    }
    steps.extend(fill_steps(Slot::A, plan, families).into_iter().map(strict));
    steps.extend(
        per_family(families, |family| RouteStep::AddRule {
            slot: Slot::A,
            family,
        })
        .into_iter()
        .map(strict),
    );
    steps.extend(
        per_family(families, |family| RouteStep::DelRule {
            slot: emergency,
            family,
        })
        .into_iter()
        .map(strict),
    );
    steps.extend(
        per_family(families, |family| RouteStep::FlushTable {
            slot: emergency,
            family,
        })
        .into_iter()
        .map(strict),
    );
    steps
}

fn fill_steps(slot: Slot, plan: &RoutePlan, families: &[Family]) -> Vec<RouteStep> {
    per_family(families, |family| RouteStep::FillTable {
        slot,
        family,
        prefixes: plan.prefixes(family).to_vec(),
    })
}

fn per_family(families: &[Family], step: impl Fn(Family) -> RouteStep) -> Vec<RouteStep> {
    families.iter().map(|family| step(*family)).collect()
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;

    use super::*;
    use crate::network::entry::ALL_TRAFFIC;
    use crate::network::policy::{EgressPolicy, PolicyInput};

    fn plan(allow: &[&str], deny: &[&str], ipv6: bool) -> RoutePlan {
        let policy = EgressPolicy::parse(PolicyInput {
            allow_out: allow.iter().map(|entry| (*entry).to_owned()).collect(),
            deny_out: deny.iter().map(|entry| (*entry).to_owned()).collect(),
            upstream: None,
        })
        .unwrap();
        RoutePlan::for_policy(&policy, ipv6).unwrap().unwrap()
    }

    fn names(steps: &[RouteStep]) -> Vec<String> {
        steps
            .iter()
            .map(|step| {
                let slot = match step {
                    RouteStep::FillTable { slot, .. }
                    | RouteStep::FlushTable { slot, .. }
                    | RouteStep::AddRule { slot, .. }
                    | RouteStep::DelRule { slot, .. } => slot.table(),
                };
                format!("{}:{}:{}", step.name(), slot, step.family().as_str())
            })
            .collect()
    }

    #[test]
    fn a_first_policy_fills_and_adds_slot_a() {
        let swap = plan_swap(None, Some(&plan(&[], &[ALL_TRAFFIC], true)), true);
        assert_eq!(swap.next_slot, Some(Slot::A));
        assert_eq!(
            names(&swap.fill),
            ["fill_table:101:v4", "fill_table:101:v6"]
        );
        assert_eq!(names(&swap.commit), ["add_rule:101:v4", "add_rule:101:v6"]);
        assert_eq!(
            names(&swap.rollback()),
            ["flush_table:101:v4", "flush_table:101:v6"]
        );
    }

    #[test]
    fn a_swap_alternates_slots_and_cleans_the_old_one_last() {
        let next = plan(&[], &["10.0.0.0/8"], false);
        let from_a = plan_swap(Some(Slot::A), Some(&next), false);
        assert_eq!(from_a.next_slot, Some(Slot::B));
        assert_eq!(names(&from_a.fill), ["fill_table:102:v4"]);
        assert_eq!(
            names(&from_a.commit),
            ["add_rule:102:v4", "del_rule:101:v4", "flush_table:101:v4"]
        );
        let from_b = plan_swap(Some(Slot::B), Some(&next), false);
        assert_eq!(from_b.next_slot, Some(Slot::A));
        assert_eq!(
            names(&from_b.commit),
            ["add_rule:101:v4", "del_rule:102:v4", "flush_table:102:v4"]
        );
    }

    #[test]
    fn a_swap_to_unrestricted_only_removes_the_active_slot() {
        let swap = plan_swap(Some(Slot::B), None, true);
        assert_eq!(swap.next_slot, None);
        assert!(swap.fill.is_empty());
        assert!(swap.rollback().is_empty());
        assert_eq!(
            names(&swap.commit),
            [
                "del_rule:102:v4",
                "del_rule:102:v6",
                "flush_table:102:v4",
                "flush_table:102:v6"
            ]
        );
        let nothing = plan_swap(None, None, true);
        assert!(nothing.fill.is_empty() && nothing.commit.is_empty());
    }

    #[test]
    fn the_recovery_sequence_and_its_tolerated_steps() {
        let steps = plan_recovery(&RoutePlan::deny_all(false));
        let rendered: Vec<String> = steps
            .iter()
            .map(|planned| {
                let name = names(std::slice::from_ref(&planned.step)).remove(0);
                if planned.tolerate_failure {
                    format!("{name}?")
                } else {
                    name
                }
            })
            .collect();
        assert_eq!(
            rendered,
            [
                "fill_table:103:v4",
                "add_rule:103:v4",
                "del_rule:101:v4?",
                "flush_table:101:v4?",
                "del_rule:102:v4?",
                "flush_table:102:v4?",
                "fill_table:101:v4",
                "add_rule:101:v4",
                "del_rule:103:v4",
                "flush_table:103:v4",
            ]
        );
    }

    /// One family's rules and tables, the way the kernel resolves them.
    #[derive(Debug, Clone, Default)]
    struct Kernel {
        rules: BTreeMap<u32, u32>,
        tables: BTreeMap<u32, Option<Vec<Cidr>>>,
    }

    #[derive(Debug, Clone, PartialEq, Eq)]
    enum Effective {
        Unrestricted,
        Table(Vec<Cidr>),
        Incomplete,
    }

    impl Kernel {
        fn effective(&self) -> Effective {
            let Some((_, table)) = self.rules.iter().next() else {
                return Effective::Unrestricted;
            };
            match self.tables.get(table) {
                Some(Some(prefixes)) => Effective::Table(prefixes.clone()),
                _ => Effective::Incomplete,
            }
        }

        fn in_use(&self, table: u32) -> bool {
            self.rules.values().next() == Some(&table)
        }

        fn apply(&mut self, step: &RouteStep) {
            match step {
                RouteStep::FillTable { slot, prefixes, .. } => {
                    assert!(
                        !self.in_use(slot.table()),
                        "{step:?} refills a table in use"
                    );
                    self.tables.insert(slot.table(), Some(prefixes.clone()));
                }
                RouteStep::FlushTable { slot, .. } => {
                    assert!(
                        !self.in_use(slot.table()),
                        "{step:?} flushes a table in use"
                    );
                    self.tables.insert(slot.table(), None);
                }
                RouteStep::AddRule { slot, .. } => {
                    self.rules.insert(slot.priority(), slot.table());
                }
                RouteStep::DelRule { slot, .. } => {
                    self.rules.remove(&slot.priority());
                }
            }
        }
    }

    fn installed(slot: Slot, plan: &RoutePlan) -> Kernel {
        let mut kernel = Kernel::default();
        kernel
            .tables
            .insert(slot.table(), Some(plan.prefixes(Family::V4).to_vec()));
        kernel.rules.insert(slot.priority(), slot.table());
        kernel
    }

    fn table(plan: &RoutePlan) -> Effective {
        Effective::Table(plan.prefixes(Family::V4).to_vec())
    }

    #[test]
    fn a_swap_never_leaves_the_uid_range_without_a_complete_table() {
        let old = plan(&[], &["10.0.0.0/8"], false);
        let new = plan(&["198.51.100.7/32"], &["198.51.100.0/24"], false);
        for active in Slot::POLICY {
            let mut kernel = installed(active, &old);
            let swap = plan_swap(Some(active), Some(&new), false);
            for step in swap.fill.iter().chain(&swap.commit) {
                kernel.apply(step);
                let effective = kernel.effective();
                assert!(
                    effective == table(&old) || effective == table(&new),
                    "{step:?} left {effective:?}"
                );
            }
            assert_eq!(kernel.effective(), table(&new));
        }
    }

    #[test]
    fn a_failed_fill_is_rolled_back_with_the_old_policy_in_force() {
        let old = plan(&[], &["10.0.0.0/8"], false);
        let new = plan(&[], &[ALL_TRAFFIC], false);
        let mut kernel = installed(Slot::A, &old);
        let swap = plan_swap(Some(Slot::A), Some(&new), false);
        kernel.apply(&swap.fill[0]);
        for step in swap.rollback() {
            kernel.apply(&step);
            assert_eq!(kernel.effective(), table(&old));
        }
        assert!(!kernel.rules.contains_key(&Slot::B.priority()));
    }

    #[test]
    fn the_recovery_keeps_deny_all_or_the_final_plan_from_its_second_step_on() {
        let deny_all = RoutePlan::deny_all(false);
        let stored = plan(&[], &["203.0.113.0/24"], false);
        for target in [&deny_all, &stored] {
            let mut broken = Kernel::default();
            broken.rules.insert(Slot::A.priority(), Slot::A.table());
            broken.rules.insert(Slot::B.priority(), Slot::B.table());
            broken.tables.insert(Slot::B.table(), Some(Vec::new()));
            let steps = plan_recovery(target);
            for (index, planned) in steps.iter().enumerate() {
                broken.apply(&planned.step);
                if index >= 1 {
                    let effective = broken.effective();
                    assert!(
                        effective == table(&deny_all) || effective == table(target),
                        "step {index} {planned:?} left {effective:?}"
                    );
                }
            }
            assert_eq!(broken.effective(), table(target));
            assert_eq!(
                broken.rules.keys().copied().collect::<Vec<_>>(),
                [Slot::A.priority()]
            );
        }
    }
}
