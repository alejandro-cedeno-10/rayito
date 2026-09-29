//! Egress, option A (ADR-012 addendum, `SECURITY.md` T17): the guest's own
//! platform resolvers (one loopback, one link-local, Q66) answer uid ≥ 1000
//! under deny-all because they are reached through the kernel's `local`
//! table at rule priority 0, ahead of the uid-scoped policy rules at
//! 150/151. Nothing below priority 0 exists to intercept them, and rule
//! priority is an unsigned kernel field: 0 is already the floor. The only
//! way to run something *before* `local` is to vacate priority 0 for it,
//! which means moving `local` itself first.
//!
//! The install order is fixed so the guest is never without local routing,
//! and the new rule never has to share priority 0 with the kernel's own
//! (same-priority ties break in insertion order, so a rule added after the
//! kernel's default would lose to it, never intercepting a loopback- or
//! link-local-bound DNS query):
//!
//! 1. Add a second `local` rule at [`MOVED_LOCAL_PRIORITY`]: a harmless
//!    duplicate, nothing yet depends on it.
//! 2. Delete the kernel's original rule at priority 0 (`local` routing now
//!    depends solely on the duplicate above, uninterrupted throughout).
//! 3. Add the block rules at [`DNS_BLOCK_PRIORITY`] (now free, and alone):
//!    one `uidrange 1000-65535 ipproto <udp|tcp> dport 53 prohibit` per
//!    transport per managed family.
//!
//! Uninstalling reverses the order (block rules first, since removing them
//! only relaxes the policy; the default `local` rule restored before the
//! duplicate is dropped, so routing is never without at least one).
//! [`plan_dns_guard_rollback`] undoes exactly the steps that already
//! succeeded, in reverse, so a failure anywhere during install restores the
//! pre-install state rather than leaving a half-moved `local` rule.

use super::cidr::Family;

/// Where the block rules live once installed; vacated by moving `local`.
pub const DNS_BLOCK_PRIORITY: u32 = 0;
/// The kernel's own `local` rule, before it moves; the same numeric value
/// as [`DNS_BLOCK_PRIORITY`] (there is only one priority 0), named
/// separately so a call site reads as "the original rule", not "the block
/// rule's slot".
pub const DEFAULT_LOCAL_PRIORITY: u32 = DNS_BLOCK_PRIORITY;
/// Where the kernel's own `local` rule moves to, freeing priority 0.
pub const MOVED_LOCAL_PRIORITY: u32 = 1;
/// What both the moved and the original `local` rule look up.
pub const LOCAL_TABLE: &str = "local";
pub const DNS_BLOCK_PORT: u16 = 53;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum DnsProto {
    Udp,
    Tcp,
}

impl DnsProto {
    pub const ALL: [DnsProto; 2] = [DnsProto::Udp, DnsProto::Tcp];

    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Udp => "udp",
            Self::Tcp => "tcp",
        }
    }
}

/// One step of the install/remove sequence; the adapter runs each as one
/// `ip` invocation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DnsGuardStep {
    /// `ip rule add priority 1 lookup local`.
    AddMovedLocal {
        family: Family,
    },
    /// `ip rule del priority 0 lookup local`: the kernel's own rule.
    DelDefaultLocal {
        family: Family,
    },
    /// The exact reverse of `DelDefaultLocal`, only used to undo it.
    AddDefaultLocal {
        family: Family,
    },
    /// `ip rule del priority 1 lookup local`: undoes `AddMovedLocal`.
    DelMovedLocal {
        family: Family,
    },
    /// `ip rule add priority 0 uidrange 1000-65535 ipproto <proto> dport 53 prohibit`.
    AddDnsBlock {
        family: Family,
        proto: DnsProto,
    },
    DelDnsBlock {
        family: Family,
        proto: DnsProto,
    },
}

impl DnsGuardStep {
    /// The fixed name logged on failure, never the `ip` output.
    #[must_use]
    pub fn name(&self) -> &'static str {
        match self {
            Self::AddMovedLocal { .. } => "add_moved_local",
            Self::DelDefaultLocal { .. } => "del_default_local",
            Self::AddDefaultLocal { .. } => "add_default_local",
            Self::DelMovedLocal { .. } => "del_moved_local",
            Self::AddDnsBlock { .. } => "add_dns_block",
            Self::DelDnsBlock { .. } => "del_dns_block",
        }
    }

    #[must_use]
    pub fn family(&self) -> Family {
        match self {
            Self::AddMovedLocal { family }
            | Self::DelDefaultLocal { family }
            | Self::AddDefaultLocal { family }
            | Self::DelMovedLocal { family }
            | Self::AddDnsBlock { family, .. }
            | Self::DelDnsBlock { family, .. } => *family,
        }
    }
}

/// Install order (see the module doc for why it is fixed): local routing
/// stays continuous throughout, and the block rules end up alone at
/// priority 0.
#[must_use]
pub fn plan_dns_guard_install(families: &[Family]) -> Vec<DnsGuardStep> {
    let mut steps = Vec::new();
    for &family in families {
        steps.push(DnsGuardStep::AddMovedLocal { family });
    }
    for &family in families {
        steps.push(DnsGuardStep::DelDefaultLocal { family });
    }
    for &family in families {
        for proto in DnsProto::ALL {
            steps.push(DnsGuardStep::AddDnsBlock { family, proto });
        }
    }
    steps
}

/// Reverse of [`plan_dns_guard_install`]: block rules removed first (safe
/// at any time), the default `local` rule restored, the duplicate dropped
/// last, so routing is never without a `local` rule.
#[must_use]
pub fn plan_dns_guard_remove(families: &[Family]) -> Vec<DnsGuardStep> {
    let mut steps = Vec::new();
    for &family in families {
        for proto in DnsProto::ALL {
            steps.push(DnsGuardStep::DelDnsBlock { family, proto });
        }
    }
    for &family in families {
        steps.push(DnsGuardStep::AddDefaultLocal { family });
    }
    for &family in families {
        steps.push(DnsGuardStep::DelMovedLocal { family });
    }
    steps
}

/// Undoes exactly the `applied` install steps, in reverse, so a failure
/// partway through [`plan_dns_guard_install`] restores the state from
/// before the first one ran; steps with no inverse (the block rules that
/// never got added) contribute nothing.
#[must_use]
pub fn plan_dns_guard_rollback(applied: &[DnsGuardStep]) -> Vec<DnsGuardStep> {
    applied
        .iter()
        .rev()
        .map(|step| match *step {
            DnsGuardStep::AddMovedLocal { family } => DnsGuardStep::DelMovedLocal { family },
            DnsGuardStep::DelDefaultLocal { family } => DnsGuardStep::AddDefaultLocal { family },
            DnsGuardStep::AddDnsBlock { family, proto } => {
                DnsGuardStep::DelDnsBlock { family, proto }
            }
            // Never produced by `plan_dns_guard_install`; kept exhaustive
            // so a new install step cannot silently skip its own rollback.
            DnsGuardStep::AddDefaultLocal { family } => DnsGuardStep::DelDefaultLocal { family },
            DnsGuardStep::DelMovedLocal { family } => DnsGuardStep::AddMovedLocal { family },
            DnsGuardStep::DelDnsBlock { family, proto } => {
                DnsGuardStep::AddDnsBlock { family, proto }
            }
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn names(steps: &[DnsGuardStep]) -> Vec<String> {
        steps
            .iter()
            .map(|step| format!("{}:{}", step.name(), step.family().as_str()))
            .collect()
    }

    #[test]
    fn install_moves_local_before_touching_priority_zero() {
        let steps = plan_dns_guard_install(&[Family::V4]);
        assert_eq!(
            names(&steps),
            [
                "add_moved_local:v4",
                "del_default_local:v4",
                "add_dns_block:v4",
                "add_dns_block:v4",
            ]
        );
    }

    #[test]
    fn install_covers_both_families_and_both_protocols() {
        let steps = plan_dns_guard_install(&Family::ALL);
        assert_eq!(steps.len(), 8);
        let blocks: Vec<(Family, DnsProto)> = steps
            .iter()
            .filter_map(|step| match step {
                DnsGuardStep::AddDnsBlock { family, proto } => Some((*family, *proto)),
                _ => None,
            })
            .collect();
        assert_eq!(
            blocks,
            [
                (Family::V4, DnsProto::Udp),
                (Family::V4, DnsProto::Tcp),
                (Family::V6, DnsProto::Udp),
                (Family::V6, DnsProto::Tcp),
            ]
        );
    }

    #[test]
    fn remove_restores_the_default_before_dropping_the_duplicate() {
        let steps = plan_dns_guard_remove(&[Family::V4]);
        assert_eq!(
            names(&steps),
            [
                "del_dns_block:v4",
                "del_dns_block:v4",
                "add_default_local:v4",
                "del_moved_local:v4",
            ]
        );
    }

    #[test]
    fn rollback_undoes_exactly_what_was_applied_in_reverse() {
        let applied = [
            DnsGuardStep::AddMovedLocal { family: Family::V4 },
            DnsGuardStep::DelDefaultLocal { family: Family::V4 },
            DnsGuardStep::AddDnsBlock {
                family: Family::V4,
                proto: DnsProto::Udp,
            },
        ];
        assert_eq!(
            names(&plan_dns_guard_rollback(&applied)),
            [
                "del_dns_block:v4",
                "add_default_local:v4",
                "del_moved_local:v4",
            ]
        );
        assert!(plan_dns_guard_rollback(&[]).is_empty());
    }

    /// A DNS query from uid 1000 to a resolver at a `local`-table address
    /// (loopback or link-local, Q66): blocked by every priority-0 rule
    /// query the *destination* would have gone through, and answered by
    /// `local` before our fix reached that far.
    #[derive(Debug, Clone, Copy, PartialEq, Eq)]
    enum Lookup {
        Local,
        Prohibited,
        NoRule,
    }

    /// One family's rule list, in the kernel's own evaluation order: a
    /// `Vec` so equal priorities keep insertion order, exactly the way
    /// `fib_insert_rule` breaks ties (new rules of an existing priority go
    /// after it, never before).
    #[derive(Debug, Clone)]
    struct Kernel {
        rules: Vec<(u32, Rule)>,
    }

    #[derive(Debug, Clone, Copy, PartialEq, Eq)]
    enum Rule {
        Local,
        DnsBlock(DnsProto),
    }

    impl Kernel {
        fn boot() -> Self {
            Self {
                rules: vec![(0, Rule::Local)],
            }
        }

        fn apply(&mut self, step: DnsGuardStep) {
            match step {
                DnsGuardStep::AddMovedLocal { .. } => {
                    self.rules.push((MOVED_LOCAL_PRIORITY, Rule::Local));
                }
                DnsGuardStep::DelDefaultLocal { .. } => {
                    let index = self
                        .position(0, Rule::Local)
                        .expect("del_default_local: no rule at priority 0");
                    self.rules.remove(index);
                }
                DnsGuardStep::AddDefaultLocal { .. } => {
                    assert!(
                        self.position(0, Rule::Local).is_none(),
                        "add_default_local refills priority 0"
                    );
                    self.rules.insert(0, (0, Rule::Local));
                }
                DnsGuardStep::DelMovedLocal { .. } => {
                    let index = self
                        .position(MOVED_LOCAL_PRIORITY, Rule::Local)
                        .expect("del_moved_local: nothing to remove");
                    self.rules.remove(index);
                }
                DnsGuardStep::AddDnsBlock { proto, .. } => {
                    assert!(
                        self.position(0, Rule::DnsBlock(proto)).is_none(),
                        "add_dns_block refills its own protocol at priority 0"
                    );
                    self.rules.push((0, Rule::DnsBlock(proto)));
                }
                DnsGuardStep::DelDnsBlock { proto, .. } => {
                    let index = self
                        .position(0, Rule::DnsBlock(proto))
                        .expect("del_dns_block: nothing to remove");
                    self.rules.remove(index);
                }
            }
        }

        fn position(&self, priority: u32, rule: Rule) -> Option<usize> {
            self.rules
                .iter()
                .position(|(candidate, kind)| *candidate == priority && *kind == rule)
        }

        /// What a query would resolve to right now: the kernel evaluates
        /// the lowest-priority rule first and, among rules that share a
        /// priority, in insertion order (`push` always appends rather
        /// than reordering by priority, so a priority group's relative
        /// order is exactly call order — matching `fib_insert_rule`). A
        /// DNS query (dport 53, uid 1000) matches both `local`
        /// (destination-based, no port selector) and the block rule
        /// (uid+port selector, no destination), so whichever rule the
        /// packet's own destination would have resolved through by
        /// `local` is exactly what a real loopback/link-local resolver
        /// query hits first here too.
        fn dns_lookup(&self) -> Lookup {
            let Some(lowest) = self.rules.iter().map(|(priority, _)| *priority).min() else {
                return Lookup::NoRule;
            };
            self.rules
                .iter()
                .find(|(priority, _)| *priority == lowest)
                .map_or(Lookup::NoRule, |(_, rule)| match rule {
                    Rule::Local => Lookup::Local,
                    Rule::DnsBlock(_) => Lookup::Prohibited,
                })
        }

        /// Whether *some* rule still resolves plain local routing (ordinary
        /// loopback traffic on a port other than 53, any uid): true as long
        /// as any `local` rule, moved or original, remains.
        fn local_routing_intact(&self) -> bool {
            self.rules.iter().any(|(_, rule)| *rule == Rule::Local)
        }
    }

    #[test]
    fn before_install_a_dns_query_resolves_through_local_the_q66_bug() {
        assert_eq!(Kernel::boot().dns_lookup(), Lookup::Local);
    }

    #[test]
    fn install_never_drops_local_routing_and_ends_with_dns_prohibited() {
        let mut kernel = Kernel::boot();
        for step in plan_dns_guard_install(&[Family::V4]) {
            kernel.apply(step);
            assert!(
                kernel.local_routing_intact(),
                "{step:?} broke local routing"
            );
        }
        assert_eq!(kernel.dns_lookup(), Lookup::Prohibited);
    }

    #[test]
    fn remove_never_drops_local_routing_and_ends_with_dns_resolving_again() {
        let mut kernel = Kernel::boot();
        for step in plan_dns_guard_install(&[Family::V4]) {
            kernel.apply(step);
        }
        for step in plan_dns_guard_remove(&[Family::V4]) {
            kernel.apply(step);
            assert!(
                kernel.local_routing_intact(),
                "{step:?} broke local routing"
            );
        }
        assert_eq!(kernel.rules, vec![(0, Rule::Local)]);
        assert_eq!(kernel.dns_lookup(), Lookup::Local);
    }

    #[test]
    fn rollback_after_a_partial_install_restores_local_routing_throughout() {
        for failed_after in 0..=2 {
            let mut kernel = Kernel::boot();
            let install = plan_dns_guard_install(&[Family::V4]);
            let applied = &install[..=failed_after];
            for step in applied {
                kernel.apply(*step);
                assert!(
                    kernel.local_routing_intact(),
                    "{step:?} broke local routing"
                );
            }
            for step in plan_dns_guard_rollback(applied) {
                kernel.apply(step);
                assert!(
                    kernel.local_routing_intact(),
                    "{step:?} broke local routing"
                );
            }
            assert_eq!(kernel.rules, vec![(0, Rule::Local)]);
            assert_eq!(kernel.dns_lookup(), Lookup::Local);
        }
    }
}
