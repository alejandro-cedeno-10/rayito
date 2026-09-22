//! The pure halves of `/suspend` and `/resume`: what a transition obliges
//! the agent to do before the checkpoint, the budgets each step gets, and
//! the audit counters behind `Health.hook_anomalies` that a forged hook
//! meets once `/run` has been accepted. Origin cannot be validated (hooks
//! and proxied client traffic both arrive from `127.0.0.1`), so the audit
//! makes a forgery visible instead of refusing it: `rayd` never refuses a
//! session-changing `/suspend` or `/resume`, because a refusal would also
//! hit the genuine hook that follows a forged one and make the agent skip
//! the checklist of a real checkpoint (streams left open, no quiesce, no
//! sync, and a `/resume` treated as a repeat). The kernel halves
//! (`probe_outcome`, `restart_after_resume`) live in `code::hooks`; the
//! tokio work lives in the `rayd` hooks adapter.

use std::time::Duration;

use crate::lifecycle::{Hook, Transition};

/// How long `/suspend` waits for the live client streams to observe the
/// broadcast and close before it moves on (never past the hook budget).
pub const STREAM_CLOSE_GRACE: Duration = Duration::from_secs(2);
/// Bound on the best-effort `quiesce` to the sidecar during `/suspend`.
pub const QUIESCE_TIMEOUT: Duration = Duration::from_secs(2);
/// Hard cap on the `/resume` kernel probe: a kernel that has not answered
/// by then is reported as lost rather than delaying the 200.
pub const RESUME_PROBE_BUDGET: Duration = Duration::from_secs(12);

/// What the audit records about one runtime hook call.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HookCallOutcome {
    /// The call did what it came for (or nothing, idempotently).
    Nominal,
    /// A `/run` after the accepted one: the only hook call the audit can
    /// tell apart from a genuine one.
    Anomalous,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct AuditEntry {
    pub hook: Hook,
    /// Per-hook monotone count of calls after the accepted `/run`.
    pub calls_since_run: u64,
    pub anomaly: bool,
}

/// Counters behind the `hook_audit` log lines and `Health.hook_anomalies`:
/// per-hook calls after the first accepted `/run` and the anomalies of the
/// boot (anomalous calls plus stale-suspend recoveries).
#[derive(Debug, Default, Clone)]
pub struct HookAudit {
    run_accepted: bool,
    calls_since_run: [u64; Hook::ALL.len()],
    anomalies: u64,
}

impl HookAudit {
    /// The first accepted `/run` opens the audit; it is not itself audited.
    pub fn run_accepted(&mut self) {
        self.run_accepted = true;
    }

    /// `None` before the accepted `/run` (nothing to audit yet), otherwise
    /// the entry to log; an anomalous outcome is counted either way once
    /// the audit is open.
    pub fn record(&mut self, hook: Hook, outcome: HookCallOutcome) -> Option<AuditEntry> {
        if !self.run_accepted {
            return None;
        }
        let slot = &mut self.calls_since_run[hook_index(hook)];
        *slot += 1;
        let anomaly = outcome == HookCallOutcome::Anomalous;
        if anomaly {
            self.anomalies += 1;
        }
        Some(AuditEntry {
            hook,
            calls_since_run: *slot,
            anomaly,
        })
    }

    /// A stale-suspend recovery: not a hook call, still an anomaly.
    pub fn note_anomaly(&mut self) {
        self.anomalies += 1;
    }

    #[must_use]
    pub fn anomalies(&self) -> u64 {
        self.anomalies
    }

    #[must_use]
    pub fn calls_since_run(&self, hook: Hook) -> u64 {
        self.calls_since_run[hook_index(hook)]
    }
}

fn hook_index(hook: Hook) -> usize {
    match hook {
        Hook::Ready => 0,
        Hook::Validate => 1,
        Hook::Run => 2,
        Hook::Suspend => 3,
        Hook::Resume => 4,
        Hook::Terminate => 5,
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SuspendActions {
    /// Broadcast the suspend to every open client stream. A repeated
    /// `/suspend` (no phase change) has nothing left to close.
    pub close_streams: bool,
}

#[must_use]
pub fn suspend_actions(transition: &Transition) -> SuspendActions {
    SuspendActions {
        close_streams: transition.changed(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::lifecycle::HookPhase;

    #[test]
    fn a_phase_change_closes_streams_and_a_repeat_does_not() {
        let changed = Transition::new(Hook::Suspend, HookPhase::Running, HookPhase::Suspending);
        assert!(suspend_actions(&changed).close_streams);
        let repeated = Transition::new(Hook::Suspend, HookPhase::Suspending, HookPhase::Suspending);
        assert!(!suspend_actions(&repeated).close_streams);
    }

    #[test]
    fn budgets_fit_inside_the_hook_budget() {
        let hook_budget = Duration::from_secs(24);
        assert!(STREAM_CLOSE_GRACE + QUIESCE_TIMEOUT < Duration::from_secs(5));
        assert!(RESUME_PROBE_BUDGET < hook_budget);
    }

    #[test]
    fn audit_is_silent_before_run_and_counts_per_hook_afterwards() {
        let mut audit = HookAudit::default();
        assert_eq!(
            audit.record(Hook::Suspend, HookCallOutcome::Anomalous),
            None
        );
        assert_eq!(audit.anomalies(), 0);
        audit.run_accepted();
        let first = audit.record(Hook::Run, HookCallOutcome::Anomalous).unwrap();
        assert_eq!(
            first,
            AuditEntry {
                hook: Hook::Run,
                calls_since_run: 1,
                anomaly: true
            }
        );
        let suspend = audit
            .record(Hook::Suspend, HookCallOutcome::Nominal)
            .unwrap();
        assert_eq!(suspend.calls_since_run, 1);
        assert!(!suspend.anomaly);
        let again = audit
            .record(Hook::Suspend, HookCallOutcome::Anomalous)
            .unwrap();
        assert_eq!(again.calls_since_run, 2);
        assert!(again.anomaly);
        assert_eq!(audit.calls_since_run(Hook::Suspend), 2);
        assert_eq!(audit.calls_since_run(Hook::Resume), 0);
        assert_eq!(audit.anomalies(), 2);
        audit.note_anomaly();
        assert_eq!(audit.anomalies(), 3);
    }
}
