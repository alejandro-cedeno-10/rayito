//! When the kernel counts as ready (design D8/D9): the sidecar process is
//! up, said `ready`, the default context is not being rotated and no
//! relaunch is pending. Drives `Health.kernel_ready` and the `/ready` hook.

use std::time::Duration;

use super::READY_ESCAPE;

const RESTART_DELAY_BASE: Duration = Duration::from_millis(500);
const RESTART_DELAY_MAX: Duration = Duration::from_secs(30);

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum SidecarState {
    /// `--no-sidecar`: nothing to wait for, `kernel_ready` stays false.
    #[default]
    Disabled,
    /// The process is being spawned.
    Starting { attempt: u32, since: Duration },
    /// The process is up, `ready` not seen yet.
    Warming { attempt: u32 },
    /// The default context is warm and serving.
    Ready,
    /// The `/run` rotation (or the post-relaunch rotation) is in progress.
    Rotating,
    /// The process exited; the loop sleeps until `backoff_until`.
    Exited {
        attempt: u32,
        backoff_until: Duration,
    },
}

impl SidecarState {
    #[must_use]
    pub fn kernel_ready(&self) -> bool {
        matches!(self, Self::Ready)
    }

    #[must_use]
    pub fn attempt(&self) -> u32 {
        match self {
            Self::Starting { attempt, .. }
            | Self::Warming { attempt }
            | Self::Exited { attempt, .. } => *attempt,
            Self::Disabled | Self::Ready | Self::Rotating => 0,
        }
    }

    /// The state after the process exited (or failed to spawn): the
    /// attempt counter continues while the sidecar never reached `Ready`
    /// and restarts at 1 after it had been serving.
    #[must_use]
    pub fn exited(&self, now: Duration) -> Self {
        let attempt = match self {
            Self::Starting { attempt, .. }
            | Self::Warming { attempt }
            | Self::Exited { attempt, .. } => attempt + 1,
            Self::Disabled | Self::Ready | Self::Rotating => 1,
        };
        Self::Exited {
            attempt,
            backoff_until: now + restart_delay(attempt),
        }
    }

    #[must_use]
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Disabled => "disabled",
            Self::Starting { .. } => "starting",
            Self::Warming { .. } => "warming",
            Self::Ready => "ready",
            Self::Rotating => "rotating",
            Self::Exited { .. } => "exited",
        }
    }
}

/// `min(0.5 s × 2^(attempt−1), 30 s)`, no attempt cap.
#[must_use]
pub fn restart_delay(attempt: u32) -> Duration {
    let exponent = attempt.saturating_sub(1).min(16);
    let factor = 1u32 << exponent;
    (RESTART_DELAY_BASE * factor).min(RESTART_DELAY_MAX)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReadyDecision {
    /// 200 with the phase transition.
    Ok,
    /// 503 right away, no transition: AWS retries.
    Retry,
    /// 200 with the transition although the kernel never warmed; logged.
    Escape,
}

#[must_use]
pub fn ready_hook_decision(state: &SidecarState, boot_elapsed: Duration) -> ReadyDecision {
    match state {
        SidecarState::Disabled | SidecarState::Ready => ReadyDecision::Ok,
        _ if boot_elapsed >= READY_ESCAPE => ReadyDecision::Escape,
        _ => ReadyDecision::Retry,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn kernel_ready_only_when_ready() {
        assert!(SidecarState::Ready.kernel_ready());
        for state in [
            SidecarState::Disabled,
            SidecarState::Starting {
                attempt: 1,
                since: Duration::ZERO,
            },
            SidecarState::Warming { attempt: 1 },
            SidecarState::Rotating,
            SidecarState::Exited {
                attempt: 1,
                backoff_until: Duration::ZERO,
            },
        ] {
            assert!(!state.kernel_ready(), "{state:?}");
        }
    }

    #[test]
    fn restart_delay_doubles_from_half_a_second_to_thirty() {
        let delays: Vec<u64> = (1..=9)
            .map(|attempt| restart_delay(attempt).as_millis().try_into().unwrap())
            .collect();
        assert_eq!(
            delays,
            vec![
                500, 1_000, 2_000, 4_000, 8_000, 16_000, 30_000, 30_000, 30_000
            ]
        );
        assert_eq!(restart_delay(0), Duration::from_millis(500));
        assert_eq!(restart_delay(u32::MAX), Duration::from_secs(30));
    }

    #[test]
    fn exit_counts_attempts_while_never_ready_and_resets_after_serving() {
        let now = Duration::from_secs(10);
        let first = SidecarState::Starting {
            attempt: 1,
            since: Duration::ZERO,
        }
        .exited(now);
        assert_eq!(
            first,
            SidecarState::Exited {
                attempt: 2,
                backoff_until: now + Duration::from_secs(1)
            }
        );
        let third = first.exited(now);
        assert_eq!(third.attempt(), 3);
        let after_serving = SidecarState::Ready.exited(now);
        assert_eq!(
            after_serving,
            SidecarState::Exited {
                attempt: 1,
                backoff_until: now + Duration::from_millis(500)
            }
        );
        assert_eq!(SidecarState::Rotating.exited(now).attempt(), 1);
    }

    #[test]
    fn ready_hook_retries_until_ready_and_escapes_at_five_minutes() {
        let warming = SidecarState::Warming { attempt: 1 };
        assert_eq!(
            ready_hook_decision(&warming, Duration::from_secs(1)),
            ReadyDecision::Retry
        );
        assert_eq!(
            ready_hook_decision(&warming, Duration::from_secs(299)),
            ReadyDecision::Retry
        );
        assert_eq!(
            ready_hook_decision(&warming, Duration::from_secs(300)),
            ReadyDecision::Escape
        );
        assert_eq!(
            ready_hook_decision(&SidecarState::Ready, Duration::ZERO),
            ReadyDecision::Ok
        );
        assert_eq!(
            ready_hook_decision(&SidecarState::Disabled, Duration::ZERO),
            ReadyDecision::Ok
        );
        assert_eq!(
            ready_hook_decision(
                &SidecarState::Exited {
                    attempt: 1,
                    backoff_until: Duration::ZERO
                },
                Duration::from_secs(5)
            ),
            ReadyDecision::Retry
        );
    }

    #[test]
    fn state_names_are_stable() {
        assert_eq!(SidecarState::default().as_str(), "disabled");
        assert_eq!(SidecarState::Rotating.as_str(), "rotating");
    }
}
