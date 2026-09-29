//! What the egress manager has installed (ADR-012, design D5-D7): the
//! policy, its route plan, the slot whose rules hold that plan and
//! whether the DNS guard is up. Every transition the manager makes is one
//! method here, so the recovery's slot rule and the fallback plan of a
//! `/resume` reinstall live in one place; the adapter runs the `ip` steps
//! and calls the matching transition, in its own order.

use super::policy::EgressPolicy;
use super::route_plan::{RoutePlan, Slot, managed_families};
use super::swap::RECOVERY_SLOT;

/// `slot` holds the rules of `plan`. Starts unrestricted with nothing
/// installed.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Installation {
    policy: EgressPolicy,
    plan: Option<RoutePlan>,
    slot: Option<Slot>,
    /// Egress option A (ADR-012 addendum, `SECURITY.md` T17): whether the
    /// DNS block for uid ≥ 1000 is installed. Tracked apart from `plan`
    /// because it survives `plan` changing to another deny-all policy
    /// (nothing to redo) and only moves when denying-all itself starts or
    /// stops.
    dns_guard: bool,
}

impl Installation {
    #[must_use]
    pub fn policy(&self) -> &EgressPolicy {
        &self.policy
    }

    #[must_use]
    pub fn plan(&self) -> Option<&RoutePlan> {
        self.plan.as_ref()
    }

    #[must_use]
    pub fn slot(&self) -> Option<Slot> {
        self.slot
    }

    #[must_use]
    pub fn dns_guard(&self) -> bool {
        self.dns_guard
    }

    /// A swap committed (or half-committed): `next_slot` now holds `plan`.
    pub fn after_swap(
        &mut self,
        policy: EgressPolicy,
        plan: Option<RoutePlan>,
        next_slot: Option<Slot>,
    ) {
        self.slot = next_slot;
        self.plan = plan;
        self.policy = policy;
    }

    /// Without `CAP_NET_ADMIN` only the policy is recorded; no route moves.
    pub fn set_policy_only(&mut self, policy: EgressPolicy) {
        self.policy = policy;
    }

    /// The deny-all recovery ran: deny-all is the policy, in `RECOVERY_SLOT`.
    pub fn after_recovery(&mut self, deny_all: RoutePlan) {
        self.policy = EgressPolicy::deny_all();
        self.plan = Some(deny_all);
        self.slot = Some(RECOVERY_SLOT);
    }

    /// The plan a `/resume` reinstalls behind the emergency deny-all: the
    /// stored one, deny-all when none is stored.
    #[must_use]
    pub fn reinstall_plan(&self, ipv6_present: bool) -> RoutePlan {
        self.plan
            .clone()
            .unwrap_or_else(|| RoutePlan::deny_all(ipv6_present))
    }

    /// The reinstall ran: `plan` is in `RECOVERY_SLOT`, the policy stays.
    pub fn after_reinstall(&mut self, plan: RoutePlan) {
        self.slot = Some(RECOVERY_SLOT);
        self.plan = Some(plan);
    }

    /// No rule installed and no proxy running: `/resume` has nothing to
    /// re-verify.
    #[must_use]
    pub fn nothing_to_reverify(&self, proxy_running: bool) -> bool {
        self.slot.is_none() && !proxy_running
    }

    /// Egress option A applies exactly under "deny-all" (`SECURITY.md`
    /// T17): every family the guest manages has nothing left to allow
    /// through directly.
    #[must_use]
    pub fn wants_dns_guard(&self, ipv6_present: bool) -> bool {
        let Some(plan) = &self.plan else {
            return false;
        };
        managed_families(ipv6_present)
            .iter()
            .all(|family| plan.denies_all(*family))
    }

    pub fn set_dns_guard(&mut self, on: bool) {
        self.dns_guard = on;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::network::cidr::Family;
    use crate::network::entry::ALL_TRAFFIC;
    use crate::network::policy::PolicyInput;

    fn policy(allow: &[&str], deny: &[&str]) -> EgressPolicy {
        EgressPolicy::parse(PolicyInput {
            allow_out: allow.iter().map(|entry| (*entry).to_owned()).collect(),
            deny_out: deny.iter().map(|entry| (*entry).to_owned()).collect(),
            upstream: None,
        })
        .unwrap()
    }

    fn partial() -> (EgressPolicy, RoutePlan) {
        let policy = policy(&["198.51.100.7/32"], &["198.51.100.0/24"]);
        let plan = RoutePlan::for_policy(&policy, true).unwrap().unwrap();
        (policy, plan)
    }

    fn state(installation: &Installation) -> (EgressPolicy, Option<RoutePlan>, Option<Slot>, bool) {
        (
            installation.policy().clone(),
            installation.plan().cloned(),
            installation.slot(),
            installation.dns_guard(),
        )
    }

    #[test]
    fn starts_unrestricted_with_nothing_installed() {
        assert_eq!(
            state(&Installation::default()),
            (EgressPolicy::default(), None, None, false)
        );
    }

    #[test]
    fn after_swap_records_the_policy_its_plan_and_the_next_slot() {
        let (policy, plan) = partial();
        let mut installation = Installation::default();
        installation.set_dns_guard(true);
        installation.after_swap(policy.clone(), Some(plan.clone()), Some(Slot::B));
        assert_eq!(
            state(&installation),
            (policy, Some(plan), Some(Slot::B), true)
        );
        installation.after_swap(EgressPolicy::default(), None, None);
        assert_eq!(
            state(&installation),
            (EgressPolicy::default(), None, None, true)
        );
    }

    #[test]
    fn set_policy_only_leaves_the_routes() {
        let (policy, plan) = partial();
        let mut installation = Installation::default();
        installation.after_swap(EgressPolicy::deny_all(), Some(plan.clone()), Some(Slot::A));
        installation.set_policy_only(policy.clone());
        assert_eq!(
            state(&installation),
            (policy, Some(plan), Some(Slot::A), false)
        );
    }

    #[test]
    fn after_recovery_is_deny_all_in_the_recovery_slot() {
        let (policy, plan) = partial();
        let mut installation = Installation::default();
        installation.after_swap(policy, Some(plan), Some(Slot::B));
        installation.after_recovery(RoutePlan::deny_all(false));
        assert_eq!(
            state(&installation),
            (
                EgressPolicy::deny_all(),
                Some(RoutePlan::deny_all(false)),
                Some(RECOVERY_SLOT),
                false
            )
        );
        assert_eq!(RECOVERY_SLOT, Slot::A);
    }

    #[test]
    fn after_reinstall_keeps_the_policy() {
        let (policy, plan) = partial();
        let mut installation = Installation::default();
        installation.after_swap(policy.clone(), Some(plan.clone()), Some(Slot::B));
        installation.after_reinstall(plan.clone());
        assert_eq!(
            state(&installation),
            (policy, Some(plan), Some(RECOVERY_SLOT), false)
        );
    }

    #[test]
    fn reinstall_plan_is_the_stored_plan_or_deny_all() {
        let (policy, plan) = partial();
        let mut installation = Installation::default();
        assert_eq!(installation.reinstall_plan(true), RoutePlan::deny_all(true));
        assert_eq!(
            installation.reinstall_plan(false),
            RoutePlan::deny_all(false)
        );
        installation.after_swap(policy, Some(plan.clone()), Some(Slot::A));
        assert_eq!(installation.reinstall_plan(false), plan);
    }

    #[test]
    fn nothing_to_reverify_needs_no_slot_and_no_proxy() {
        let mut installation = Installation::default();
        assert!(installation.nothing_to_reverify(false));
        assert!(!installation.nothing_to_reverify(true));
        installation.after_recovery(RoutePlan::deny_all(false));
        assert!(!installation.nothing_to_reverify(false));
        assert!(!installation.nothing_to_reverify(true));
    }

    #[test]
    fn the_dns_guard_is_wanted_exactly_under_deny_all() {
        let mut installation = Installation::default();
        assert!(!installation.wants_dns_guard(false));
        installation.after_recovery(RoutePlan::deny_all(false));
        assert!(installation.wants_dns_guard(false));
        installation.after_recovery(RoutePlan::deny_all(true));
        assert!(installation.wants_dns_guard(true));
        let v4_only = RoutePlan::deny_all(false);
        assert!(v4_only.denies_all(Family::V4));
        installation.after_recovery(v4_only);
        assert!(installation.wants_dns_guard(false));
        assert!(!installation.wants_dns_guard(true));
        let (partial_policy, partial_plan) = partial();
        installation.after_swap(partial_policy, Some(partial_plan), Some(Slot::A));
        assert!(!installation.wants_dns_guard(false));
        let everything = policy(&[], &[ALL_TRAFFIC]);
        let plan = RoutePlan::for_policy(&everything, true).unwrap();
        installation.after_swap(everything, plan, Some(Slot::B));
        assert!(installation.wants_dns_guard(true));
    }
}
