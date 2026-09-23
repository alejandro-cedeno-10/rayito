//! The deadline state machine (design D4). Pure: no clock reads, no
//! locking, no I/O; `session` wraps it in its own lock and the watcher
//! thread drives `tick`.
//!
//! ```text
//! Unmanaged (no lifecycle block: every call is inert)
//! Active --deadline, kill--> Terminating (reported EXPIRED)
//! Active --deadline, pause--> Expired --SetTimeout--> Active
//! Active --deadline seen after a thaw--> ResumeGrace --grace ends--> Terminating | Expired
//! ResumeGrace --SetTimeout--> Active
//! ```
//!
//! A deadline grants at most one resume grace, and no grace runs past the
//! cap: the thaw that opens it is a ≥ 2 s gap between two watcher ticks,
//! which CPU starvation inside the VM can also produce. Only a deadline
//! that moves (`SetTimeout`, which needs the access token, or E2B's
//! auto-resume rule) is eligible again.

use std::time::{Duration, SystemTime, UNIX_EPOCH};

use thiserror::Error;

use super::TimeoutSettings;
use super::policy::{LifecycleSpec, TimeoutAction, TimeoutMode, TimeoutPolicy};
use super::{AUTO_RESUME_MIN_TIMEOUT, CAP_MARGIN, MIN_SET_TIMEOUT, SET_TIMEOUT_RPC_PATH};
use crate::auth::ANONYMOUS_RPC_PATH;
use crate::clock::ClockReading;

/// The phase `Health` reports.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum LifecyclePhase {
    #[default]
    Unmanaged,
    Active,
    ResumeGrace,
    Expired,
}

impl LifecyclePhase {
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Unmanaged => "unmanaged",
            Self::Active => "active",
            Self::ResumeGrace => "resume_grace",
            Self::Expired => "expired",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Phase {
    Unmanaged,
    Active,
    ResumeGrace { until: Duration },
    Expired,
    Terminating,
}

/// The one wait a deadline grants a `/suspend` in flight; reset whenever
/// the deadline moves.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Hold {
    Unused,
    Until(Duration),
    Spent,
}

/// What the watcher has to do after a tick.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DeadlineAction {
    /// Pause mode reached its deadline: close the streams.
    Expire,
    /// A pause-mode grace ended without `SetTimeout`: back to expired.
    ReExpire,
    /// A checkpoint straddled the deadline: close the streams and wait for
    /// the `/resume` and `connect()`.
    Gate,
    /// Kill mode: end the workload and the agent.
    Terminate,
}

impl DeadlineAction {
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Expire => "expire",
            Self::ReExpire => "re_expire",
            Self::Gate => "gate",
            Self::Terminate => "terminate",
        }
    }
}

/// The lifecycle in wall-clock terms, as `Health` and `SetTimeout` report
/// it. Zeros and no action while unmanaged.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct LifecycleView {
    pub phase: LifecyclePhase,
    pub deadline_unix_ms: i64,
    pub cap_unix_ms: i64,
    pub timeout: Duration,
    pub on_timeout: Option<TimeoutAction>,
    pub auto_resume: bool,
    pub extensions: u32,
}

/// The display strings are the gRPC status messages; none carries a token
/// or payload content.
#[derive(Debug, Error, Clone, Copy, PartialEq, Eq)]
pub enum SandboxTimeoutError {
    #[error("lifecycle_unmanaged")]
    Unmanaged,
    #[error("sandbox_timeout")]
    Expired,
    #[error("timeout below 1 s")]
    InvalidTimeout,
    #[error("timeout beyond cap; cap_unix_ms={cap_unix_ms}")]
    BeyondCap { cap_unix_ms: i64 },
}

#[derive(Debug, Clone)]
pub struct SandboxTimeout {
    phase: Phase,
    timeout: Duration,
    deadline: Duration,
    cap: Duration,
    policy: TimeoutPolicy,
    extensions: u32,
    hold: Hold,
    grace_spent: bool,
    resume_grace: Duration,
    suspend_hold: Duration,
}

impl SandboxTimeout {
    #[must_use]
    pub fn new(settings: &TimeoutSettings) -> Self {
        Self {
            phase: Phase::Unmanaged,
            timeout: Duration::ZERO,
            deadline: Duration::ZERO,
            cap: Duration::ZERO,
            policy: TimeoutPolicy {
                on_timeout: TimeoutAction::Kill,
                auto_resume: false,
            },
            extensions: 0,
            hold: Hold::Unused,
            grace_spent: false,
            resume_grace: settings.resume_grace,
            suspend_hold: settings.suspend_hold,
        }
    }

    /// The accepted `/run` delivered a lifecycle block at monotonic `now`.
    pub fn install(&mut self, spec: LifecycleSpec, now: Duration) {
        self.cap = now.saturating_add(spec.cap).saturating_sub(CAP_MARGIN);
        self.deadline = now.saturating_add(spec.timeout).min(self.cap);
        self.timeout = spec.timeout;
        self.policy = spec.policy;
        self.extensions = 0;
        self.hold = Hold::Unused;
        self.grace_spent = false;
        self.phase = Phase::Active;
    }

    #[must_use]
    pub fn is_managed(&self) -> bool {
        self.phase != Phase::Unmanaged
    }

    #[must_use]
    pub fn phase(&self) -> LifecyclePhase {
        match self.phase {
            Phase::Unmanaged => LifecyclePhase::Unmanaged,
            Phase::Active => LifecyclePhase::Active,
            Phase::ResumeGrace { .. } => LifecyclePhase::ResumeGrace,
            Phase::Expired | Phase::Terminating => LifecyclePhase::Expired,
        }
    }

    /// `Exact` sets `now + timeout` and may shorten; `AtLeast` never
    /// shortens an active deadline, and from a grace or an expiry (where
    /// the old deadline is already past) both reopen the sandbox. A target
    /// beyond the cap changes nothing.
    pub fn set_timeout(
        &mut self,
        reading: ClockReading,
        mode: TimeoutMode,
        timeout: Duration,
    ) -> Result<LifecycleView, SandboxTimeoutError> {
        match self.phase {
            Phase::Unmanaged => return Err(SandboxTimeoutError::Unmanaged),
            Phase::Terminating => return Err(SandboxTimeoutError::Expired),
            Phase::Active | Phase::ResumeGrace { .. } | Phase::Expired => {}
        }
        if timeout < MIN_SET_TIMEOUT {
            return Err(SandboxTimeoutError::InvalidTimeout);
        }
        let target = reading.monotonic.saturating_add(timeout);
        if target > self.cap {
            return Err(SandboxTimeoutError::BeyondCap {
                cap_unix_ms: wall_millis(reading, self.cap),
            });
        }
        let deadline = match (mode, self.phase) {
            (TimeoutMode::AtLeast, Phase::Active) => self.deadline.max(target),
            _ => target,
        };
        if deadline != self.deadline {
            self.timeout = timeout;
            self.extensions = self.extensions.saturating_add(1);
            self.move_deadline(deadline);
        }
        self.phase = Phase::Active;
        Ok(self.view(reading))
    }

    /// `suspending`: the hook phase is `Suspending`. `thawed`: the watcher
    /// saw a monotonic gap of at least the freeze threshold since its
    /// previous tick.
    pub fn tick(
        &mut self,
        now: Duration,
        suspending: bool,
        thawed: bool,
    ) -> Option<DeadlineAction> {
        match self.phase {
            Phase::Active if now >= self.deadline => self.deadline_passed(now, suspending, thawed),
            Phase::ResumeGrace { until } if now >= until => Some(self.act_after_grace()),
            _ => None,
        }
    }

    /// A changed `/resume` at monotonic `now`; `frozen` is the watcher's
    /// verdict that the VM really was frozen, which a forged
    /// `/suspend` + `/resume` pair alone never produces. Past the deadline,
    /// pause mode with `auto_resume` applies E2B's rule and anything else
    /// opens the grace, unless this deadline already spent it.
    pub fn resumed(&mut self, now: Duration, frozen: bool) {
        let deadline_passed = match self.phase {
            Phase::Unmanaged | Phase::Terminating => false,
            Phase::Active => now >= self.deadline,
            Phase::ResumeGrace { .. } | Phase::Expired => true,
        };
        if !frozen || !deadline_passed {
            return;
        }
        if self.policy.on_timeout == TimeoutAction::Pause && self.policy.auto_resume {
            self.apply_auto_resume(now);
        } else {
            self.open_grace(now);
        }
    }

    #[must_use]
    pub fn view(&self, reading: ClockReading) -> LifecycleView {
        if self.phase == Phase::Unmanaged {
            return LifecycleView::default();
        }
        LifecycleView {
            phase: self.phase(),
            deadline_unix_ms: wall_millis(reading, self.deadline),
            cap_unix_ms: wall_millis(reading, self.cap),
            timeout: self.timeout,
            on_timeout: Some(self.policy.on_timeout),
            auto_resume: self.policy.auto_resume,
            extensions: self.extensions,
        }
    }

    #[must_use]
    pub fn admits(&self, rpc_path: &str) -> bool {
        admits(self.phase(), rpc_path)
    }

    /// A thaw seen at the deadline means a checkpoint straddled it and the
    /// `/resume` follows, if this deadline still has its grace; a
    /// `/suspend` in flight gets one bounded wait for its freeze; otherwise
    /// the policy acts.
    fn deadline_passed(
        &mut self,
        now: Duration,
        suspending: bool,
        thawed: bool,
    ) -> Option<DeadlineAction> {
        if thawed && self.open_grace(now) {
            return Some(DeadlineAction::Gate);
        }
        if suspending && self.hold_for_suspend(now) {
            return None;
        }
        Some(self.act_at_deadline())
    }

    fn hold_for_suspend(&mut self, now: Duration) -> bool {
        match self.hold {
            Hold::Unused => {
                self.hold = Hold::Until(now.saturating_add(self.suspend_hold));
                true
            }
            Hold::Until(until) if now < until => true,
            Hold::Until(_) | Hold::Spent => {
                self.hold = Hold::Spent;
                false
            }
        }
    }

    fn act_at_deadline(&mut self) -> DeadlineAction {
        match self.policy.on_timeout {
            TimeoutAction::Kill => {
                self.phase = Phase::Terminating;
                DeadlineAction::Terminate
            }
            TimeoutAction::Pause => {
                self.phase = Phase::Expired;
                DeadlineAction::Expire
            }
        }
    }

    fn act_after_grace(&mut self) -> DeadlineAction {
        match self.policy.on_timeout {
            TimeoutAction::Kill => {
                self.phase = Phase::Terminating;
                DeadlineAction::Terminate
            }
            TimeoutAction::Pause => {
                self.phase = Phase::Expired;
                DeadlineAction::ReExpire
            }
        }
    }

    /// The one grace of the current deadline, ending at the cap at the
    /// latest; false when it was already spent or the cap has passed.
    fn open_grace(&mut self, now: Duration) -> bool {
        if self.grace_spent || now >= self.cap {
            return false;
        }
        self.grace_spent = true;
        self.phase = Phase::ResumeGrace {
            until: now.saturating_add(self.resume_grace).min(self.cap),
        };
        true
    }

    /// E2B: after an auto-resume the sandbox gets at least five minutes,
    /// never past the cap; a resume at or past the cap stays expired.
    fn apply_auto_resume(&mut self, now: Duration) {
        if now >= self.cap {
            self.phase = Phase::Expired;
            return;
        }
        self.timeout = self.timeout.max(AUTO_RESUME_MIN_TIMEOUT);
        self.move_deadline(now.saturating_add(self.timeout).min(self.cap));
        self.phase = Phase::Active;
    }

    fn move_deadline(&mut self, deadline: Duration) {
        self.deadline = deadline;
        self.hold = Hold::Unused;
        self.grace_spent = false;
    }
}

/// Past the deadline only the probe and the call that reopens the sandbox
/// get through.
#[must_use]
pub fn admits(phase: LifecyclePhase, rpc_path: &str) -> bool {
    match phase {
        LifecyclePhase::Unmanaged | LifecyclePhase::Active => true,
        LifecyclePhase::ResumeGrace | LifecyclePhase::Expired => {
            rpc_path == ANONYMOUS_RPC_PATH || rpc_path == SET_TIMEOUT_RPC_PATH
        }
    }
}

/// `instant` on the monotonic clock as wall-clock milliseconds since the
/// epoch: `wall_now + (instant − monotonic_now)`, signed and saturating.
/// The sum is taken in nanoseconds and floored once, so every reading with
/// the same wall-to-monotonic offset reports the same millisecond; flooring
/// each term apart made the result wobble by 1 ms between reads.
fn wall_millis(reading: ClockReading, instant: Duration) -> i64 {
    let nanos = unix_nanos(reading.wall)
        .saturating_add(signed_nanos(instant))
        .saturating_sub(signed_nanos(reading.monotonic));
    let millis = nanos.div_euclid(NANOS_PER_MILLI);
    i64::try_from(millis).unwrap_or(if millis < 0 { i64::MIN } else { i64::MAX })
}

const NANOS_PER_MILLI: i128 = 1_000_000;

fn unix_nanos(wall: SystemTime) -> i128 {
    match wall.duration_since(UNIX_EPOCH) {
        Ok(since) => signed_nanos(since),
        Err(before) => signed_nanos(before.duration()).saturating_neg(),
    }
}

fn signed_nanos(duration: Duration) -> i128 {
    i128::try_from(duration.as_nanos()).unwrap_or(i128::MAX)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sandbox_timeout::{
        AUTO_RESUME_MIN_TIMEOUT, CAP_MARGIN, MAX_LIFETIME_SECONDS, MIN_CAP_SECONDS,
        MIN_TIMEOUT_SECONDS, RESUME_GRACE, TIMEOUT_EXIT_CODE,
    };

    const WALL_ORIGIN: Duration = Duration::from_secs(1_700_000_000);
    const START: &str = "/rayito.v1.ProcessService/Start";

    fn secs(seconds: u64) -> Duration {
        Duration::from_secs(seconds)
    }

    fn reading_at(monotonic: Duration) -> ClockReading {
        ClockReading {
            monotonic,
            wall: UNIX_EPOCH + WALL_ORIGIN + monotonic,
        }
    }

    fn wall_ms(monotonic: Duration) -> i64 {
        i64::try_from((WALL_ORIGIN + monotonic).as_millis()).unwrap()
    }

    fn installed(
        timeout_s: u64,
        cap_s: u64,
        on_timeout: TimeoutAction,
        auto_resume: bool,
        at: Duration,
    ) -> SandboxTimeout {
        let mut machine = SandboxTimeout::new(&TimeoutSettings::default());
        machine.install(
            LifecycleSpec {
                timeout: secs(timeout_s),
                cap: secs(cap_s),
                policy: TimeoutPolicy {
                    on_timeout,
                    auto_resume,
                },
            },
            at,
        );
        machine
    }

    fn kill(timeout_s: u64, cap_s: u64) -> SandboxTimeout {
        installed(timeout_s, cap_s, TimeoutAction::Kill, false, Duration::ZERO)
    }

    fn pause(timeout_s: u64, cap_s: u64, auto_resume: bool) -> SandboxTimeout {
        installed(
            timeout_s,
            cap_s,
            TimeoutAction::Pause,
            auto_resume,
            Duration::ZERO,
        )
    }

    fn view_at(machine: &SandboxTimeout, monotonic: Duration) -> LifecycleView {
        machine.view(reading_at(monotonic))
    }

    #[test]
    fn install_clamps_the_deadline_to_the_cap_margin() {
        let clamped = installed(900, 900, TimeoutAction::Kill, false, secs(10));
        let view = view_at(&clamped, secs(10));
        assert_eq!(view.phase, LifecyclePhase::Active);
        assert_eq!(view.deadline_unix_ms, wall_ms(secs(850)));
        assert_eq!(view.cap_unix_ms, wall_ms(secs(850)));
        let roomy = kill(60, 900);
        let view = view_at(&roomy, Duration::ZERO);
        assert_eq!(view.deadline_unix_ms, wall_ms(secs(60)));
        assert_eq!(view.cap_unix_ms, wall_ms(secs(840)));
        assert_eq!(view.timeout, secs(60));
        assert_eq!(view.on_timeout, Some(TimeoutAction::Kill));
        assert!(roomy.is_managed());
    }

    #[test]
    fn unmanaged_rejects_set_timeout_and_never_ticks() {
        let mut machine = SandboxTimeout::new(&TimeoutSettings::default());
        assert!(!machine.is_managed());
        assert_eq!(
            machine.set_timeout(reading_at(secs(1)), TimeoutMode::Exact, secs(60)),
            Err(SandboxTimeoutError::Unmanaged)
        );
        assert_eq!(machine.tick(secs(100_000), false, true), None);
        machine.resumed(secs(100_000), true);
        assert_eq!(view_at(&machine, secs(5)), LifecycleView::default());
        assert!(machine.admits(START));
    }

    #[test]
    fn exact_moves_the_deadline_both_ways() {
        let mut machine = kill(60, 900);
        let longer = machine
            .set_timeout(reading_at(secs(5)), TimeoutMode::Exact, secs(600))
            .unwrap();
        assert_eq!(longer.deadline_unix_ms, wall_ms(secs(605)));
        assert_eq!(longer.timeout, secs(600));
        assert_eq!(longer.extensions, 1);
        let shorter = machine
            .set_timeout(reading_at(secs(10)), TimeoutMode::Exact, secs(10))
            .unwrap();
        assert_eq!(shorter.deadline_unix_ms, wall_ms(secs(20)));
        assert_eq!(shorter.extensions, 2);
        assert_eq!(
            machine.tick(secs(20), false, false),
            Some(DeadlineAction::Terminate)
        );
    }

    #[test]
    fn a_timeout_below_one_second_is_invalid() {
        let mut machine = kill(60, 900);
        assert_eq!(
            machine.set_timeout(
                reading_at(secs(1)),
                TimeoutMode::Exact,
                Duration::from_millis(999)
            ),
            Err(SandboxTimeoutError::InvalidTimeout)
        );
        assert_eq!(view_at(&machine, secs(1)).extensions, 0);
    }

    #[test]
    fn at_least_never_shortens() {
        let mut machine = kill(600, 900);
        let exact = machine
            .set_timeout(reading_at(Duration::ZERO), TimeoutMode::Exact, secs(10))
            .unwrap();
        assert_eq!(exact.deadline_unix_ms, wall_ms(secs(10)));
        let kept = machine
            .set_timeout(reading_at(secs(2)), TimeoutMode::AtLeast, secs(5))
            .unwrap();
        assert_eq!(kept.deadline_unix_ms, wall_ms(secs(10)));
        assert_eq!(kept.extensions, 1);
        assert_eq!(kept.timeout, secs(10));
        let extended = machine
            .set_timeout(reading_at(secs(2)), TimeoutMode::AtLeast, secs(30))
            .unwrap();
        assert_eq!(extended.deadline_unix_ms, wall_ms(secs(32)));
        assert_eq!(extended.extensions, 2);
    }

    #[test]
    fn beyond_cap_is_rejected_and_nothing_changes() {
        let mut machine = kill(60, 900);
        let before = view_at(&machine, secs(3));
        let error = machine
            .set_timeout(reading_at(secs(3)), TimeoutMode::Exact, secs(2_000))
            .unwrap_err();
        assert_eq!(
            error,
            SandboxTimeoutError::BeyondCap {
                cap_unix_ms: wall_ms(secs(840))
            }
        );
        assert!(
            error
                .to_string()
                .starts_with("timeout beyond cap; cap_unix_ms=")
        );
        assert_eq!(view_at(&machine, secs(3)), before);
        assert_eq!(
            machine.set_timeout(reading_at(secs(3)), TimeoutMode::AtLeast, secs(838)),
            Err(SandboxTimeoutError::BeyondCap {
                cap_unix_ms: wall_ms(secs(840))
            })
        );
        assert!(
            machine
                .set_timeout(reading_at(secs(3)), TimeoutMode::AtLeast, secs(837))
                .is_ok()
        );
    }

    #[test]
    fn kill_mode_terminates_at_the_deadline() {
        let mut machine = kill(60, 900);
        assert_eq!(
            machine.tick(Duration::from_millis(59_900), false, false),
            None
        );
        assert_eq!(
            machine.tick(secs(60), false, false),
            Some(DeadlineAction::Terminate)
        );
        assert_eq!(machine.phase(), LifecyclePhase::Expired);
        assert_eq!(machine.tick(secs(61), false, false), None);
    }

    #[test]
    fn pause_mode_expires_at_the_deadline() {
        let mut machine = pause(60, 900, false);
        assert_eq!(machine.tick(secs(59), false, false), None);
        assert_eq!(
            machine.tick(secs(60), false, false),
            Some(DeadlineAction::Expire)
        );
        assert_eq!(machine.phase(), LifecyclePhase::Expired);
        assert_eq!(machine.tick(secs(70), false, false), None);
        let view = view_at(&machine, secs(70));
        assert_eq!(view.deadline_unix_ms, wall_ms(secs(60)));
        assert_eq!(view.on_timeout, Some(TimeoutAction::Pause));
    }

    /// The fake clock jumps from before the deadline to past it, as
    /// `CLOCK_MONOTONIC` does across a real checkpoint.
    #[test]
    fn suspended_time_counts_toward_the_deadline() {
        let mut machine = kill(60, 900);
        assert_eq!(machine.tick(secs(30), false, false), None);
        assert_eq!(
            machine.tick(secs(90), false, true),
            Some(DeadlineAction::Gate)
        );
        assert_eq!(machine.phase(), LifecyclePhase::ResumeGrace);
    }

    #[test]
    fn grace_without_set_timeout_terminates_in_kill_mode() {
        let mut machine = kill(60, 900);
        assert_eq!(
            machine.tick(secs(90), false, true),
            Some(DeadlineAction::Gate)
        );
        assert_eq!(machine.tick(secs(119), false, false), None);
        assert_eq!(
            machine.tick(secs(90) + RESUME_GRACE, false, false),
            Some(DeadlineAction::Terminate)
        );
        assert_eq!(machine.phase(), LifecyclePhase::Expired);
    }

    #[test]
    fn grace_without_set_timeout_re_expires_in_pause_mode() {
        let mut machine = pause(60, 900, false);
        assert_eq!(
            machine.tick(secs(90), false, true),
            Some(DeadlineAction::Gate)
        );
        assert_eq!(
            machine.tick(secs(120), false, false),
            Some(DeadlineAction::ReExpire)
        );
        assert_eq!(machine.phase(), LifecyclePhase::Expired);
        assert_eq!(machine.tick(secs(200), false, false), None);
    }

    #[test]
    fn at_least_during_grace_reactivates() {
        let mut machine = kill(60, 900);
        assert_eq!(
            machine.tick(secs(90), false, true),
            Some(DeadlineAction::Gate)
        );
        let view = machine
            .set_timeout(reading_at(secs(95)), TimeoutMode::AtLeast, secs(120))
            .unwrap();
        assert_eq!(view.phase, LifecyclePhase::Active);
        assert_eq!(view.deadline_unix_ms, wall_ms(secs(215)));
        assert_eq!(view.extensions, 1);
        assert_eq!(machine.tick(secs(120), false, false), None);
        assert_eq!(
            machine.tick(secs(215), false, false),
            Some(DeadlineAction::Terminate)
        );
    }

    /// cap = 400 − 60 = 340 s: no grace, however it opens, runs past it.
    #[test]
    fn a_grace_never_extends_past_the_cap() {
        let mut thawed = kill(60, 400);
        assert_eq!(
            thawed.tick(secs(330), false, true),
            Some(DeadlineAction::Gate)
        );
        assert_eq!(thawed.tick(secs(339), false, false), None);
        assert_eq!(
            thawed.tick(secs(340), false, false),
            Some(DeadlineAction::Terminate)
        );

        let mut resumed = kill(60, 400);
        resumed.resumed(secs(335), true);
        assert_eq!(resumed.phase(), LifecyclePhase::ResumeGrace);
        assert_eq!(
            resumed.tick(secs(340), false, false),
            Some(DeadlineAction::Terminate)
        );

        let mut at_cap = kill(60, 400);
        assert_eq!(
            at_cap.tick(secs(345), false, true),
            Some(DeadlineAction::Terminate)
        );

        let mut paused = pause(60, 400, false);
        assert_eq!(
            paused.tick(secs(60), false, false),
            Some(DeadlineAction::Expire)
        );
        paused.resumed(secs(350), true);
        assert_eq!(paused.phase(), LifecyclePhase::Expired);
    }

    /// CPU starvation can fake the thaw and a forged `/suspend` +
    /// `/resume` can follow it: neither buys a second grace for the same
    /// deadline, in either mode.
    #[test]
    fn a_deadline_grants_one_grace() {
        let mut kill_mode = kill(60, 900);
        assert_eq!(
            kill_mode.tick(secs(90), false, true),
            Some(DeadlineAction::Gate)
        );
        kill_mode.resumed(secs(110), true);
        assert_eq!(kill_mode.phase(), LifecyclePhase::ResumeGrace);
        assert_eq!(
            kill_mode.tick(secs(120), false, false),
            Some(DeadlineAction::Terminate)
        );

        let mut pause_mode = pause(60, 900, false);
        assert_eq!(
            pause_mode.tick(secs(90), false, true),
            Some(DeadlineAction::Gate)
        );
        assert_eq!(
            pause_mode.tick(secs(120), false, false),
            Some(DeadlineAction::ReExpire)
        );
        pause_mode.resumed(secs(130), true);
        assert_eq!(pause_mode.phase(), LifecyclePhase::Expired);
        assert_eq!(pause_mode.tick(secs(200), false, false), None);
    }

    /// Only a deadline that moved (`SetTimeout`, which needs the access
    /// token, or E2B's auto-resume rule) is eligible for a new grace; the
    /// auto-resume rule itself never depends on the grace.
    #[test]
    fn a_moved_deadline_restores_the_grace() {
        let mut machine = kill(60, 900);
        assert_eq!(
            machine.tick(secs(90), false, true),
            Some(DeadlineAction::Gate)
        );
        machine
            .set_timeout(reading_at(secs(95)), TimeoutMode::Exact, secs(10))
            .unwrap();
        assert_eq!(
            machine.tick(secs(105), false, true),
            Some(DeadlineAction::Gate)
        );
        assert_eq!(
            machine.tick(secs(135), false, false),
            Some(DeadlineAction::Terminate)
        );

        let mut auto = pause(60, 900, true);
        assert_eq!(auto.tick(secs(90), false, true), Some(DeadlineAction::Gate));
        assert_eq!(
            auto.tick(secs(120), false, false),
            Some(DeadlineAction::ReExpire)
        );
        auto.resumed(secs(130), true);
        let view = view_at(&auto, secs(130));
        assert_eq!(view.phase, LifecyclePhase::Active);
        assert_eq!(
            view.deadline_unix_ms,
            wall_ms(secs(130) + AUTO_RESUME_MIN_TIMEOUT)
        );
        assert_eq!(
            auto.tick(secs(430), false, true),
            Some(DeadlineAction::Gate)
        );
    }

    #[test]
    fn a_real_resume_opens_the_grace_in_kill_mode() {
        let mut machine = kill(60, 900);
        machine.resumed(secs(100), true);
        assert_eq!(machine.phase(), LifecyclePhase::ResumeGrace);
        assert_eq!(machine.tick(secs(129), false, false), None);
        assert_eq!(
            machine.tick(secs(130), false, false),
            Some(DeadlineAction::Terminate)
        );
        let mut early = kill(60, 900);
        early.resumed(secs(30), true);
        assert_eq!(early.phase(), LifecyclePhase::Active);
        assert_eq!(
            view_at(&early, secs(30)).deadline_unix_ms,
            wall_ms(secs(60))
        );
    }

    #[test]
    fn auto_resume_applies_the_five_minute_minimum() {
        let mut short = pause(60, 900, true);
        assert_eq!(
            short.tick(secs(61), false, false),
            Some(DeadlineAction::Expire)
        );
        short.resumed(secs(100), true);
        let view = view_at(&short, secs(100));
        assert_eq!(view.phase, LifecyclePhase::Active);
        assert_eq!(
            view.deadline_unix_ms,
            wall_ms(secs(100) + AUTO_RESUME_MIN_TIMEOUT)
        );
        assert_eq!(view.timeout, AUTO_RESUME_MIN_TIMEOUT);
        assert_eq!(view.extensions, 0);

        let mut long = pause(900, 2_000, true);
        assert_eq!(
            long.tick(secs(900), false, false),
            Some(DeadlineAction::Expire)
        );
        long.resumed(secs(1_000), true);
        assert_eq!(
            view_at(&long, secs(1_000)).deadline_unix_ms,
            wall_ms(secs(1_900))
        );

        let mut clamped = pause(60, 400, true);
        assert_eq!(
            clamped.tick(secs(60), false, true),
            Some(DeadlineAction::Gate)
        );
        clamped.resumed(secs(100), true);
        assert_eq!(clamped.phase(), LifecyclePhase::Active);
        assert_eq!(
            view_at(&clamped, secs(100)).deadline_unix_ms,
            wall_ms(secs(340))
        );

        let mut past_cap = pause(60, 400, true);
        assert_eq!(
            past_cap.tick(secs(60), false, false),
            Some(DeadlineAction::Expire)
        );
        past_cap.resumed(secs(350), true);
        assert_eq!(past_cap.phase(), LifecyclePhase::Expired);
    }

    /// A forged `/suspend` + `/resume` pair from inside the VM: no freeze,
    /// so neither the grace nor the auto-resume rule applies.
    #[test]
    fn an_unfrozen_resume_changes_nothing() {
        let mut kill_mode = kill(60, 900);
        assert_eq!(kill_mode.tick(secs(60), true, false), None);
        kill_mode.resumed(secs(61), false);
        assert_eq!(kill_mode.phase(), LifecyclePhase::Active);
        assert_eq!(
            kill_mode.tick(secs(62), false, false),
            Some(DeadlineAction::Terminate)
        );

        let mut auto = pause(60, 900, true);
        assert_eq!(
            auto.tick(secs(60), false, false),
            Some(DeadlineAction::Expire)
        );
        let before = view_at(&auto, secs(70));
        auto.resumed(secs(70), false);
        assert_eq!(view_at(&auto, secs(70)), before);
        assert_eq!(auto.phase(), LifecyclePhase::Expired);
    }

    #[test]
    fn a_forged_suspend_holds_the_deadline_once() {
        let mut machine = kill(60, 900);
        assert_eq!(machine.tick(secs(60), true, false), None);
        assert_eq!(machine.tick(secs(79), true, false), None);
        assert_eq!(
            machine.tick(secs(80), true, false),
            Some(DeadlineAction::Terminate)
        );

        let mut recovered = pause(60, 900, false);
        assert_eq!(recovered.tick(secs(60), true, false), None);
        assert_eq!(
            recovered.tick(secs(65), false, false),
            Some(DeadlineAction::Expire)
        );

        let mut late = kill(60, 900);
        assert_eq!(late.tick(secs(60), true, false), None);
        assert_eq!(
            late.tick(secs(95), true, false),
            Some(DeadlineAction::Terminate)
        );
    }

    #[test]
    fn a_moved_deadline_grants_a_new_hold() {
        let mut machine = pause(60, 900, false);
        assert_eq!(machine.tick(secs(60), true, false), None);
        assert_eq!(
            machine.tick(secs(80), true, false),
            Some(DeadlineAction::Expire)
        );
        machine
            .set_timeout(reading_at(secs(81)), TimeoutMode::Exact, secs(10))
            .unwrap();
        assert_eq!(machine.tick(secs(91), true, false), None);
        assert_eq!(
            machine.tick(secs(111), true, false),
            Some(DeadlineAction::Expire)
        );
    }

    #[test]
    fn exact_reopens_an_expired_pause_sandbox() {
        let mut machine = pause(60, 900, false);
        assert_eq!(
            machine.tick(secs(60), false, false),
            Some(DeadlineAction::Expire)
        );
        assert!(!machine.admits(START));
        let view = machine
            .set_timeout(reading_at(secs(70)), TimeoutMode::Exact, secs(60))
            .unwrap();
        assert_eq!(view.phase, LifecyclePhase::Active);
        assert_eq!(view.deadline_unix_ms, wall_ms(secs(130)));
        assert_eq!(view.extensions, 1);
        assert!(machine.admits(START));
    }

    #[test]
    fn set_timeout_is_refused_once_terminating() {
        let mut machine = kill(60, 900);
        assert_eq!(
            machine.tick(secs(60), false, false),
            Some(DeadlineAction::Terminate)
        );
        let error = machine
            .set_timeout(reading_at(secs(61)), TimeoutMode::AtLeast, secs(60))
            .unwrap_err();
        assert_eq!(error, SandboxTimeoutError::Expired);
        assert_eq!(error.to_string(), "sandbox_timeout");
        machine.resumed(secs(70), true);
        assert_eq!(machine.phase(), LifecyclePhase::Expired);
    }

    #[test]
    fn view_converts_instants_with_the_wall_clock() {
        let machine = installed(60, 900, TimeoutAction::Kill, false, secs(10));
        let skewed = ClockReading {
            monotonic: secs(20),
            wall: UNIX_EPOCH + Duration::from_millis(5_000_123),
        };
        let view = machine.view(skewed);
        assert_eq!(view.deadline_unix_ms, 5_000_123 + 50_000);
        assert_eq!(view.cap_unix_ms, 5_000_123 + 830_000);
        let later = ClockReading {
            monotonic: secs(100),
            wall: UNIX_EPOCH + Duration::from_millis(5_000_123),
        };
        assert_eq!(machine.view(later).deadline_unix_ms, 5_000_123 - 30_000);
        assert_eq!(view.timeout, secs(60));
        assert!(!view.auto_resume);
        assert_eq!(view.extensions, 0);
    }

    #[test]
    fn view_is_stable_across_readings_with_the_same_clock_offset() {
        let mut machine = kill(120, 900);
        let offset = Duration::from_micros(1_700_000_000_000_400);
        let reading = |monotonic: Duration| ClockReading {
            monotonic,
            wall: UNIX_EPOCH + offset + monotonic,
        };
        let first = machine.view(reading(Duration::from_micros(3_000_100)));
        assert_eq!(first.deadline_unix_ms, 1_700_000_120_000);
        for micros in (3_000_000..3_002_000).step_by(37) {
            let view = machine.view(reading(Duration::from_micros(micros)));
            assert_eq!(
                view.deadline_unix_ms, first.deadline_unix_ms,
                "at {micros} µs"
            );
            assert_eq!(view.cap_unix_ms, first.cap_unix_ms, "at {micros} µs");
        }
        let refused = machine.set_timeout(
            reading(Duration::from_micros(3_000_700)),
            TimeoutMode::Exact,
            secs(2_000),
        );
        assert!(matches!(
            refused,
            Err(SandboxTimeoutError::BeyondCap { cap_unix_ms }) if cap_unix_ms == first.cap_unix_ms
        ));
        let after = machine.view(reading(Duration::from_micros(3_001_300)));
        assert_eq!(after.deadline_unix_ms, first.deadline_unix_ms);
    }

    #[test]
    fn view_floors_instants_before_the_epoch() {
        let machine = kill(60, 900);
        let reading = ClockReading {
            monotonic: Duration::from_micros(61_000_500),
            wall: UNIX_EPOCH,
        };
        assert_eq!(machine.view(reading).deadline_unix_ms, -1_001);
    }

    #[test]
    fn expiry_admits_only_health_and_set_timeout() {
        let mut machine = pause(60, 900, false);
        assert!(machine.admits(START));
        assert_eq!(
            machine.tick(secs(60), false, false),
            Some(DeadlineAction::Expire)
        );
        assert!(machine.admits(ANONYMOUS_RPC_PATH));
        assert!(machine.admits(SET_TIMEOUT_RPC_PATH));
        assert!(!machine.admits(START));
        assert!(!machine.admits("/rayito.v1.FilesystemService/Read"));
        assert!(!machine.admits("/rayito.v1.HealthService/Metrics"));
        assert!(!admits(LifecyclePhase::ResumeGrace, START));
        assert!(admits(LifecyclePhase::Active, START));
        assert!(admits(LifecyclePhase::Unmanaged, START));
    }

    #[test]
    fn constants_match_limits_json() {
        let limits: serde_json::Value =
            serde_json::from_str(include_str!("../../../../limits.json")).unwrap();
        let key = |name: &str| limits[name].as_u64().unwrap();
        assert_eq!(key("maxDurationSeconds"), MAX_LIFETIME_SECONDS);
        assert_eq!(key("lifecycleMinMaxLifetimeSeconds"), MIN_CAP_SECONDS);
        assert_eq!(key("lifecycleCapMarginSeconds"), CAP_MARGIN.as_secs());
        assert_eq!(key("lifecycleResumeGraceSeconds"), RESUME_GRACE.as_secs());
        assert_eq!(
            key("lifecycleAutoResumeMinSeconds"),
            AUTO_RESUME_MIN_TIMEOUT.as_secs()
        );
        assert_eq!(key("lifecycleMinTimeoutSeconds"), MIN_TIMEOUT_SECONDS);
        assert_eq!(
            key("lifecycleTimeoutExitCode"),
            u64::from(TIMEOUT_EXIT_CODE)
        );
        assert_eq!(MIN_SET_TIMEOUT, secs(MIN_TIMEOUT_SECONDS));
        assert_eq!(TimeoutSettings::default().resume_grace, RESUME_GRACE);
    }
}
