//! The server-enforced deadline: SIGTERM to the group at `term_at`, SIGKILL at
//! `kill_at` if the child is still there, and the shape of the terminal event
//! once the child is reaped. Both instants are `Deadline`s on the running
//! clock (design D6): the monotonic clock advances during a suspend (M0
//! Q19), so a deadline measured on it would fire at resume; measured on the
//! running clock a paused process keeps the budget it had.

use std::time::Duration;

use super::events::ProcessEnd;
use crate::clock::Deadline;

pub const KILL_GRACE: Duration = Duration::from_secs(5);
pub const SIGTERM: i32 = 15;
pub const SIGKILL: i32 = 9;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TimeoutPlan {
    pub term_at: Deadline,
    pub kill_at: Deadline,
}

impl TimeoutPlan {
    #[must_use]
    pub fn new(running_now: Duration, timeout: Duration) -> Self {
        let term_at = Deadline::after(running_now, timeout);
        Self {
            term_at,
            kill_at: Deadline::after(term_at.at, KILL_GRACE),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TimeoutStep {
    Term,
    Kill,
}

impl TimeoutStep {
    #[must_use]
    pub fn signal(self) -> i32 {
        match self {
            Self::Term => SIGTERM,
            Self::Kill => SIGKILL,
        }
    }

    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Term => "term",
            Self::Kill => "kill",
        }
    }
}

/// Why the process is ending, as far as `rayd` knows: it decides whether a
/// signal death is reported as `signaled` or as `timeout`.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum EndReason {
    #[default]
    Natural,
    Timeout,
    Signal,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WaitOutcome {
    Exited(i32),
    Signaled(i32),
}

/// A timeout always reports `timeout` with the signal `rayd` delivered, even
/// when the child exited normally while handling the SIGTERM.
#[must_use]
pub fn end_from_wait(outcome: WaitOutcome, reason: EndReason) -> ProcessEnd {
    match (reason, outcome) {
        (EndReason::Timeout, WaitOutcome::Signaled(signal)) => ProcessEnd::timed_out(signal),
        (EndReason::Timeout, WaitOutcome::Exited(_)) => ProcessEnd::timed_out(SIGTERM),
        (_, WaitOutcome::Exited(code)) => ProcessEnd::exited(code),
        (_, WaitOutcome::Signaled(signal)) => ProcessEnd::signaled(signal),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::process::events::EndStatus;

    #[test]
    fn plan_adds_the_grace_period_after_term() {
        let plan = TimeoutPlan::new(Duration::from_secs(10), Duration::from_millis(2_000));
        assert_eq!(plan.term_at.at, Duration::from_secs(12));
        assert_eq!(plan.kill_at.at, Duration::from_secs(17));
        assert_eq!(TimeoutStep::Term.signal(), 15);
        assert_eq!(TimeoutStep::Kill.signal(), 9);
    }

    /// The running clock does not move while suspended, so the same
    /// reading before and after a pause leaves the same remaining budget.
    #[test]
    fn plan_is_measured_on_the_running_clock() {
        let plan = TimeoutPlan::new(Duration::from_secs(10), Duration::from_millis(1_500));
        let running_now = Duration::from_millis(10_500);
        assert_eq!(
            plan.term_at.remaining(running_now),
            Some(Duration::from_millis(1_000))
        );
        assert_eq!(
            plan.kill_at.remaining(running_now),
            Some(Duration::from_millis(6_000))
        );
        assert_eq!(plan.term_at.remaining(Duration::from_secs(12)), None);
    }

    #[test]
    fn natural_exit_is_exited() {
        let end = end_from_wait(WaitOutcome::Exited(3), EndReason::Natural);
        assert_eq!(end.status, EndStatus::Exited);
        assert_eq!(end.exit_code, 3);
        assert!(end.exited);
        assert_eq!(end.signal, None);
    }

    #[test]
    fn signal_death_is_signaled_whoever_sent_it() {
        for reason in [EndReason::Natural, EndReason::Signal] {
            let end = end_from_wait(WaitOutcome::Signaled(9), reason);
            assert_eq!(end.status, EndStatus::Signaled);
            assert_eq!(end.exit_code, 137);
            assert_eq!(end.signal, Some(9));
            assert!(end.exited);
        }
    }

    #[test]
    fn timeout_after_a_clean_exit_still_reports_timeout_with_sigterm() {
        let end = end_from_wait(WaitOutcome::Exited(0), EndReason::Timeout);
        assert_eq!(end.status, EndStatus::Timeout);
        assert!(!end.exited);
        assert_eq!(end.exit_code, 143);
        assert_eq!(end.signal, Some(15));
        assert_eq!(end.error.unwrap().code, "deadline_exceeded");
    }

    #[test]
    fn timeout_after_sigkill_reports_signal_nine() {
        let end = end_from_wait(WaitOutcome::Signaled(9), EndReason::Timeout);
        assert_eq!(end.status, EndStatus::Timeout);
        assert_eq!(end.exit_code, 137);
        assert_eq!(end.signal, Some(9));
    }
}
