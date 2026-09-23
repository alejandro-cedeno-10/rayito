//! The logical sandbox deadline (ADR-011): `create(timeout=)` enforced by
//! `rayd` itself, moved by `LifecycleService.SetTimeout` and bounded by the
//! platform cap chosen at `create()` (`maximumDurationInSeconds`).
//!
//! Every instant is a raw reading of the monotonic side of the `Clock`
//! port, never the running clock of `clock::Deadline`: `CLOCK_MONOTONIC`
//! keeps advancing while the VM is suspended (`AWS_API_NOTES.md` §15), so
//! suspended time counts toward the deadline exactly as it counts toward
//! E2B's wall-clock `end_at` and toward the platform cap. The hook phase
//! machine of `lifecycle` stays untouched: the adapters hand this module
//! the hook phase and the freeze signature they observe.

mod machine;
mod policy;
mod ports;

use std::time::Duration;

pub use machine::{
    DeadlineAction, LifecyclePhase, LifecycleView, SandboxTimeout, SandboxTimeoutError, admits,
};
pub use policy::{LifecycleSpec, TimeoutAction, TimeoutMode, TimeoutPolicy};
pub use ports::{SelfTerminator, TerminationReason};

use crate::lifecycle::SUSPEND_GATE_TIMEOUT;

/// Longest platform cap (`maximumDurationInSeconds`, `AWS_API_NOTES.md` §2).
pub const MAX_LIFETIME_SECONDS: u64 = 28_800;
/// Shortest cap a lifecycle block may ask for: the `/run` margin plus one
/// minute of usable life.
pub const MIN_CAP_SECONDS: u64 = 120;
/// Shortest logical timeout, in the payload and in `SetTimeout`.
pub const MIN_TIMEOUT_SECONDS: u64 = 1;
/// `/run` lands about 2 s after the platform's `startedAt` and `rayd` only
/// knows its own `/run` instant, so the effective cap is
/// `/run + cap_s − CAP_MARGIN`: always before the platform's own kill, so
/// the logical path (streams closed, graceful exit) runs first.
pub const CAP_MARGIN: Duration = Duration::from_secs(60);
/// How long a sandbox resumed after its deadline waits for
/// `SetTimeout(AT_LEAST)` from `connect()`.
pub const RESUME_GRACE: Duration = Duration::from_secs(30);
/// E2B's minimum timeout after an auto-resume.
pub const AUTO_RESUME_MIN_TIMEOUT: Duration = Duration::from_secs(300);
pub const MIN_SET_TIMEOUT: Duration = Duration::from_secs(MIN_TIMEOUT_SECONDS);
/// The `timeout(1)` convention; the platform reports it in `stateReason`.
pub const TIMEOUT_EXIT_CODE: u8 = 124;
/// Stream close code, status message and log line name of the deadline.
pub const SANDBOX_TIMEOUT_CODE: &str = "sandbox_timeout";
pub const SET_TIMEOUT_RPC_PATH: &str = "/rayito.v1.LifecycleService/SetTimeout";

pub const TIMEOUT_TICK: Duration = Duration::from_millis(500);
/// A gap this long between two watcher ticks is the signature of a real
/// checkpoint. Starving the watcher of CPU from inside the VM can fake it,
/// which is why a deadline grants one grace at most, never past the cap.
pub const TIMEOUT_FREEZE_THRESHOLD: Duration = Duration::from_secs(2);
pub const SIGTERM_GRACE: Duration = Duration::from_secs(5);
pub const EXIT_DRAIN: Duration = Duration::from_secs(2);
/// After this long the watcher ends the process itself, whatever state the
/// async runtime is in.
pub const FORCE_BUDGET: Duration = Duration::from_secs(12);

/// Timing knobs of the deadline; the integration tests shrink them and
/// production uses `Default`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TimeoutSettings {
    pub tick: Duration,
    pub freeze_threshold: Duration,
    pub resume_grace: Duration,
    /// How long a deadline that passes while `Suspending` waits, once, for
    /// the checkpoint to freeze the VM.
    pub suspend_hold: Duration,
    pub sigterm_grace: Duration,
    pub exit_drain: Duration,
    pub force_budget: Duration,
}

impl Default for TimeoutSettings {
    fn default() -> Self {
        Self {
            tick: TIMEOUT_TICK,
            freeze_threshold: TIMEOUT_FREEZE_THRESHOLD,
            resume_grace: RESUME_GRACE,
            suspend_hold: SUSPEND_GATE_TIMEOUT,
            sigterm_grace: SIGTERM_GRACE,
            exit_drain: EXIT_DRAIN,
            force_budget: FORCE_BUDGET,
        }
    }
}
