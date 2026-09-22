//! Phase machine of one `MicroVM` boot, driven by the six AWS lifecycle hooks.
//! Pure state: no clock reads, no locking, no I/O. `session` wraps it for the
//! adapters.
//!
//! ```text
//! Booting --ready--> Ready --run--> Running --suspend--> Suspending --resume--> Resumed
//!                                     ^                       ^      |          |
//!                                     |                       +------|-suspend--+
//!                             (run is claimed once per boot)         |
//!                                            (stale suspend watchdog)+--> Resumed (stale_recovered)
//! any --terminate--> Terminating
//! ```
//!
//! A `/suspend` that never freezes the VM (forged through the proxy, the
//! platform never checkpoints) is recovered by the watchdog into `Resumed`
//! without bumping `resume_generation`; the `/resume` that may still follow
//! is then accepted as the real one (`resume_after_stale_recovery`).

use std::fmt;
use std::time::Duration;

use thiserror::Error;

use crate::clock::ClockReading;

/// Unfrozen running time after an accepted `/suspend` before the gate is
/// reopened without a `/resume`.
pub const SUSPEND_GATE_TIMEOUT: Duration = Duration::from_secs(20);
/// A watchdog tick whose elapsed monotonic time exceeds this is the
/// signature of a real checkpoint (`CLOCK_MONOTONIC` advances during a
/// suspension, measured): the platform will call `/resume`.
pub const FREEZE_THRESHOLD: Duration = Duration::from_secs(5);
pub const WATCHDOG_TICK: Duration = Duration::from_secs(1);

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum HookPhase {
    #[default]
    Booting,
    Ready,
    Running,
    Suspending,
    Resumed,
    Terminating,
}

impl HookPhase {
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Booting => "booting",
            Self::Ready => "ready",
            Self::Running => "running",
            Self::Suspending => "suspending",
            Self::Resumed => "resumed",
            Self::Terminating => "terminating",
        }
    }
}

impl fmt::Display for HookPhase {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Hook {
    Ready,
    Validate,
    Run,
    Suspend,
    Resume,
    Terminate,
}

impl Hook {
    pub const ALL: [Hook; 6] = [
        Hook::Ready,
        Hook::Validate,
        Hook::Run,
        Hook::Suspend,
        Hook::Resume,
        Hook::Terminate,
    ];

    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Ready => "ready",
            Self::Validate => "validate",
            Self::Run => "run",
            Self::Suspend => "suspend",
            Self::Resume => "resume",
            Self::Terminate => "terminate",
        }
    }
}

impl fmt::Display for Hook {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum LifecycleError {
    #[error("hook `{hook}` is not legal while {phase}")]
    IllegalTransition { hook: Hook, phase: HookPhase },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Transition {
    pub hook: Hook,
    pub from: HookPhase,
    pub to: HookPhase,
    /// A `/resume` accepted from `Resumed` because the watchdog had already
    /// recovered the stale suspend: the phase stays, the generation moves.
    pub after_stale_recovery: bool,
}

impl Transition {
    #[must_use]
    pub fn new(hook: Hook, from: HookPhase, to: HookPhase) -> Self {
        Self {
            hook,
            from,
            to,
            after_stale_recovery: false,
        }
    }

    /// Whether the hook's work has to run: a phase change, or the real
    /// `/resume` that lands after a stale-suspend recovery.
    #[must_use]
    pub fn changed(&self) -> bool {
        self.from != self.to || self.after_stale_recovery
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RunClaim {
    /// This `/run` is the first of the boot and owns the sandbox identity.
    Claimed,
    /// A `/run` was already accepted this boot; nothing changed.
    AlreadyClaimed,
}

#[derive(Debug, Default)]
pub struct LifecycleState {
    phase: HookPhase,
    sandbox_id: Option<String>,
    run_claimed: bool,
    suspend_generation: u64,
    resume_generation: u64,
    suspended_at: Option<ClockReading>,
    clock_offset_ms: i64,
    suspended_total: Duration,
    stale_recovered: bool,
}

impl LifecycleState {
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    #[must_use]
    pub fn phase(&self) -> HookPhase {
        self.phase
    }

    #[must_use]
    pub fn sandbox_id(&self) -> Option<&str> {
        self.sandbox_id.as_deref()
    }

    #[must_use]
    pub fn suspend_generation(&self) -> u64 {
        self.suspend_generation
    }

    #[must_use]
    pub fn resume_generation(&self) -> u64 {
        self.resume_generation
    }

    #[must_use]
    pub fn clock_offset_ms(&self) -> i64 {
        self.clock_offset_ms
    }

    /// Monotonic time spent between accepted `/suspend` and `/resume` pairs.
    #[must_use]
    pub fn suspended_total(&self) -> Duration {
        self.suspended_total
    }

    /// The running clock: `now` on the monotonic clock minus every
    /// suspended interval, the current one included while `Suspending`, so
    /// deadlines measured on it exclude paused time and never go backwards.
    #[must_use]
    pub fn running_time(&self, now: Duration) -> Duration {
        let frozen_since = match (self.phase, self.suspended_at) {
            (HookPhase::Suspending, Some(suspended_at)) => {
                now.saturating_sub(suspended_at.monotonic)
            }
            _ => Duration::ZERO,
        };
        now.saturating_sub(self.suspended_total)
            .saturating_sub(frozen_since)
    }

    pub fn ready(&mut self) -> Result<Transition, LifecycleError> {
        match self.phase {
            HookPhase::Booting | HookPhase::Ready => {
                Ok(self.move_to(Hook::Ready, HookPhase::Ready))
            }
            phase => Err(illegal(Hook::Ready, phase)),
        }
    }

    /// `/validate` runs on a throwaway VM booted from the snapshot: it never
    /// changes the phase.
    pub fn validate(&mut self) -> Transition {
        self.move_to(Hook::Validate, self.phase)
    }

    /// The first `/run` of the boot claims the sandbox identity and moves to
    /// `Running`; every later `/run` is acknowledged without changes.
    pub fn run(&mut self, sandbox_id: Option<&str>) -> Result<RunClaim, LifecycleError> {
        if self.run_claimed {
            return Ok(RunClaim::AlreadyClaimed);
        }
        match self.phase {
            HookPhase::Booting | HookPhase::Ready => {
                self.run_claimed = true;
                self.sandbox_id = sandbox_id.map(str::to_owned);
                self.move_to(Hook::Run, HookPhase::Running);
                Ok(RunClaim::Claimed)
            }
            phase => Err(illegal(Hook::Run, phase)),
        }
    }

    /// Whether the last accepted `/suspend` was recovered by the watchdog.
    #[must_use]
    pub fn stale_recovered(&self) -> bool {
        self.stale_recovered
    }

    /// Idempotent: a repeated `/suspend` neither bumps the generation nor
    /// re-records the clock.
    pub fn suspend(&mut self, now: ClockReading) -> Result<Transition, LifecycleError> {
        match self.phase {
            HookPhase::Running | HookPhase::Resumed => {
                self.suspend_generation += 1;
                self.suspended_at = Some(now);
                self.stale_recovered = false;
                Ok(self.move_to(Hook::Suspend, HookPhase::Suspending))
            }
            HookPhase::Suspending => Ok(self.move_to(Hook::Suspend, HookPhase::Suspending)),
            phase => Err(illegal(Hook::Suspend, phase)),
        }
    }

    /// Bumps `resume_generation`, records the wall-clock drift since the
    /// matching `/suspend` and adds the pause to `suspended_total`. A
    /// repeated `/resume` is acknowledged unchanged and adds nothing, except
    /// after a stale-suspend recovery, where the first `/resume` is the real
    /// one: generation bumped and drift measured from the original
    /// `suspended_at`, but nothing added to `suspended_total`, because the
    /// running clock already advanced through the unfrozen wait and must
    /// never go backwards (a deadline armed after the recovery would
    /// otherwise stretch).
    pub fn resume(&mut self, now: ClockReading) -> Result<Transition, LifecycleError> {
        match self.phase {
            HookPhase::Suspending => {
                self.account_resume(now, true);
                Ok(self.move_to(Hook::Resume, HookPhase::Resumed))
            }
            HookPhase::Resumed if self.stale_recovered => {
                self.account_resume(now, false);
                self.stale_recovered = false;
                let mut transition = self.move_to(Hook::Resume, HookPhase::Resumed);
                transition.after_stale_recovery = true;
                Ok(transition)
            }
            HookPhase::Resumed => Ok(self.move_to(Hook::Resume, HookPhase::Resumed)),
            phase => Err(illegal(Hook::Resume, phase)),
        }
    }

    /// The watchdog found no freeze for the gate timeout: reopen the gate
    /// (phase `Resumed`) without touching `resume_generation` or
    /// `suspended_total`, remembering that the suspend was never resolved.
    /// `None` when the generation moved on or a `/resume` already arrived.
    pub fn recover_from_stale_suspend(&mut self, suspend_generation: u64) -> Option<Transition> {
        if self.phase != HookPhase::Suspending || self.suspend_generation != suspend_generation {
            return None;
        }
        self.stale_recovered = true;
        Some(self.move_to(Hook::Suspend, HookPhase::Resumed))
    }

    pub fn terminate(&mut self) -> Transition {
        self.move_to(Hook::Terminate, HookPhase::Terminating)
    }

    fn account_resume(&mut self, now: ClockReading, add_suspended_span: bool) {
        self.resume_generation += 1;
        self.clock_offset_ms = self
            .suspended_at
            .map_or(0, |suspended_at| now.offset_ms_since(&suspended_at));
        if add_suspended_span {
            self.suspended_total += self.suspended_at.map_or(Duration::ZERO, |suspended_at| {
                now.monotonic.saturating_sub(suspended_at.monotonic)
            });
        }
    }

    fn move_to(&mut self, hook: Hook, to: HookPhase) -> Transition {
        let from = self.phase;
        self.phase = to;
        Transition::new(hook, from, to)
    }
}

fn illegal(hook: Hook, phase: HookPhase) -> LifecycleError {
    LifecycleError::IllegalTransition { hook, phase }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::UNIX_EPOCH;

    fn reading(monotonic_secs: u64, wall_secs: u64) -> ClockReading {
        ClockReading {
            monotonic: Duration::from_secs(monotonic_secs),
            wall: UNIX_EPOCH + Duration::from_secs(wall_secs),
        }
    }

    fn running() -> LifecycleState {
        let mut state = LifecycleState::new();
        state.ready().unwrap();
        assert_eq!(state.run(Some("mvm-1")).unwrap(), RunClaim::Claimed);
        state
    }

    #[test]
    fn boots_into_booting() {
        let state = LifecycleState::new();
        assert_eq!(state.phase(), HookPhase::Booting);
        assert_eq!(state.sandbox_id(), None);
        assert_eq!(state.resume_generation(), 0);
    }

    #[test]
    fn ready_is_idempotent_during_build() {
        let mut state = LifecycleState::new();
        assert!(state.ready().unwrap().changed());
        assert!(!state.ready().unwrap().changed());
        assert_eq!(state.phase(), HookPhase::Ready);
    }

    #[test]
    fn ready_is_illegal_once_running() {
        let mut state = running();
        assert_eq!(
            state.ready(),
            Err(LifecycleError::IllegalTransition {
                hook: Hook::Ready,
                phase: HookPhase::Running
            })
        );
    }

    #[test]
    fn validate_never_changes_the_phase() {
        let mut state = LifecycleState::new();
        assert!(!state.validate().changed());
        let mut state = running();
        assert!(!state.validate().changed());
        assert_eq!(state.phase(), HookPhase::Running);
    }

    #[test]
    fn run_is_claimed_once_per_boot() {
        let mut state = running();
        assert_eq!(state.sandbox_id(), Some("mvm-1"));
        assert_eq!(state.run(Some("mvm-2")).unwrap(), RunClaim::AlreadyClaimed);
        assert_eq!(state.sandbox_id(), Some("mvm-1"));
    }

    #[test]
    fn run_works_straight_from_booting() {
        let mut state = LifecycleState::new();
        assert_eq!(state.run(Some("mvm-1")).unwrap(), RunClaim::Claimed);
        assert_eq!(state.phase(), HookPhase::Running);
    }

    #[test]
    fn suspend_before_run_is_illegal() {
        let mut state = LifecycleState::new();
        assert_eq!(
            state.suspend(reading(1, 1)),
            Err(LifecycleError::IllegalTransition {
                hook: Hook::Suspend,
                phase: HookPhase::Booting
            })
        );
    }

    #[test]
    fn suspend_is_idempotent() {
        let mut state = running();
        assert!(state.suspend(reading(10, 1_000)).unwrap().changed());
        assert!(!state.suspend(reading(11, 1_001)).unwrap().changed());
        assert_eq!(state.suspend_generation(), 1);
        assert_eq!(state.phase(), HookPhase::Suspending);
    }

    #[test]
    fn resume_bumps_the_generation_and_records_the_clock_offset() {
        let mut state = running();
        state.suspend(reading(10, 1_000)).unwrap();
        let transition = state.resume(reading(323, 1_310)).unwrap();
        assert_eq!(transition.to, HookPhase::Resumed);
        assert_eq!(state.resume_generation(), 1);
        assert_eq!(state.clock_offset_ms(), -3_000);
    }

    #[test]
    fn repeated_resume_is_acknowledged_unchanged() {
        let mut state = running();
        state.suspend(reading(10, 1_000)).unwrap();
        state.resume(reading(20, 1_010)).unwrap();
        assert!(!state.resume(reading(21, 1_011)).unwrap().changed());
        assert_eq!(state.resume_generation(), 1);
    }

    #[test]
    fn resume_without_suspend_is_illegal() {
        let mut state = running();
        assert_eq!(
            state.resume(reading(1, 1)),
            Err(LifecycleError::IllegalTransition {
                hook: Hook::Resume,
                phase: HookPhase::Running
            })
        );
    }

    #[test]
    fn suspend_resume_cycles_count_generations() {
        let mut state = running();
        for cycle in 1..=3 {
            state.suspend(reading(cycle * 10, cycle * 10)).unwrap();
            state
                .resume(reading(cycle * 10 + 5, cycle * 10 + 5))
                .unwrap();
        }
        assert_eq!(state.suspend_generation(), 3);
        assert_eq!(state.resume_generation(), 3);
        assert_eq!(state.clock_offset_ms(), 0);
    }

    #[test]
    fn suspended_total_accumulates_across_cycles_but_not_on_a_repeated_resume() {
        let mut state = running();
        assert_eq!(state.suspended_total(), Duration::ZERO);
        state.suspend(reading(10, 1_000)).unwrap();
        state.resume(reading(310, 1_300)).unwrap();
        assert_eq!(state.suspended_total(), Duration::from_secs(300));
        state.resume(reading(320, 1_310)).unwrap();
        assert_eq!(state.suspended_total(), Duration::from_secs(300));
        state.suspend(reading(400, 1_390)).unwrap();
        state.suspend(reading(401, 1_391)).unwrap();
        state.resume(reading(450, 1_440)).unwrap();
        assert_eq!(state.suspended_total(), Duration::from_secs(350));
        state.suspend(reading(500, 1_490)).unwrap();
        state.resume(reading(501, 1_491)).unwrap();
        assert_eq!(state.suspended_total(), Duration::from_secs(351));
    }

    #[test]
    fn running_time_excludes_suspended_intervals_and_freezes_while_suspending() {
        let mut state = running();
        assert_eq!(state.running_time(secs(10)), secs(10));
        state.suspend(reading(10, 1_000)).unwrap();
        assert_eq!(state.running_time(secs(10)), secs(10));
        assert_eq!(state.running_time(secs(200)), secs(10));
        state.resume(reading(310, 1_300)).unwrap();
        assert_eq!(state.running_time(secs(310)), secs(10));
        assert_eq!(state.running_time(secs(315)), secs(15));
        state.suspend(reading(320, 1_310)).unwrap();
        assert_eq!(state.running_time(secs(330)), secs(20));
        state.resume(reading(330, 1_320)).unwrap();
        assert_eq!(state.running_time(secs(331)), secs(21));
    }

    fn secs(value: u64) -> Duration {
        Duration::from_secs(value)
    }

    #[test]
    fn terminate_is_legal_from_anywhere() {
        let mut state = LifecycleState::new();
        assert_eq!(state.terminate().to, HookPhase::Terminating);
        let mut state = running();
        state.suspend(reading(1, 1)).unwrap();
        assert_eq!(state.terminate().to, HookPhase::Terminating);
        assert!(!state.terminate().changed());
    }

    #[test]
    fn stale_suspend_recovery_reopens_the_gate_without_a_generation_bump() {
        let mut state = running();
        state.suspend(reading(10, 1_000)).unwrap();
        let recovered = state.recover_from_stale_suspend(1).unwrap();
        assert_eq!(recovered.from, HookPhase::Suspending);
        assert_eq!(recovered.to, HookPhase::Resumed);
        assert!(recovered.changed());
        assert_eq!(state.phase(), HookPhase::Resumed);
        assert_eq!(state.resume_generation(), 0);
        assert_eq!(state.suspended_total(), Duration::ZERO);
        assert!(state.stale_recovered());
        assert_eq!(state.running_time(secs(40)), secs(40));
    }

    #[test]
    fn recovery_is_a_no_op_once_resume_arrived_or_the_generation_moved() {
        let mut state = running();
        state.suspend(reading(10, 1_000)).unwrap();
        state.resume(reading(20, 1_010)).unwrap();
        assert_eq!(state.recover_from_stale_suspend(1), None);
        assert!(!state.stale_recovered());
        state.suspend(reading(30, 1_020)).unwrap();
        assert_eq!(state.recover_from_stale_suspend(1), None);
        assert_eq!(state.phase(), HookPhase::Suspending);
        assert!(state.recover_from_stale_suspend(2).is_some());
    }

    #[test]
    fn resume_after_a_recovery_is_the_real_resume() {
        let mut state = running();
        state.suspend(reading(10, 1_000)).unwrap();
        state.recover_from_stale_suspend(1).unwrap();
        let transition = state.resume(reading(45, 1_035)).unwrap();
        assert_eq!(transition.from, HookPhase::Resumed);
        assert_eq!(transition.to, HookPhase::Resumed);
        assert!(transition.after_stale_recovery);
        assert!(transition.changed());
        assert_eq!(state.resume_generation(), 1);
        assert_eq!(state.clock_offset_ms(), 0);
        assert_eq!(state.suspended_total(), Duration::ZERO);
        assert!(!state.stale_recovered());
        let repeated = state.resume(reading(46, 1_036)).unwrap();
        assert!(!repeated.changed());
        assert!(!repeated.after_stale_recovery);
        assert_eq!(state.resume_generation(), 1);
    }
}
