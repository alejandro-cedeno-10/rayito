//! The server-enforced execution timeout (design D7): interrupt at the
//! deadline, restart the context if the kernel has not gone idle within
//! the grace period. Pure offsets from the moment `rayd` accepted the
//! request; the tokio sleeps live in the `rayd` stream.

use std::time::Duration;

use super::{INTERRUPT_GRACE, MAX_EXECUTE_TIMEOUT_MS};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TimeoutSchedule {
    pub timeout_ms: u64,
    pub interrupt_at: Duration,
    pub restart_at: Duration,
}

/// `0` means no server limit; anything above the sandbox's own maximum
/// lifetime is clamped to it.
#[must_use]
pub fn plan_timeout(timeout_ms: u64) -> Option<TimeoutSchedule> {
    if timeout_ms == 0 {
        return None;
    }
    let timeout_ms = timeout_ms.min(MAX_EXECUTE_TIMEOUT_MS);
    let interrupt_at = Duration::from_millis(timeout_ms);
    Some(TimeoutSchedule {
        timeout_ms,
        interrupt_at,
        restart_at: interrupt_at + INTERRUPT_GRACE,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn zero_means_no_limit() {
        assert_eq!(plan_timeout(0), None);
    }

    #[test]
    fn interrupt_at_the_deadline_and_restart_five_seconds_later() {
        let schedule = plan_timeout(2_000).unwrap();
        assert_eq!(schedule.timeout_ms, 2_000);
        assert_eq!(schedule.interrupt_at, Duration::from_secs(2));
        assert_eq!(schedule.restart_at, Duration::from_secs(7));
    }

    #[test]
    fn timeouts_are_clamped_to_eight_hours() {
        let schedule = plan_timeout(u64::MAX).unwrap();
        assert_eq!(schedule.timeout_ms, MAX_EXECUTE_TIMEOUT_MS);
        assert_eq!(schedule.interrupt_at, Duration::from_secs(28_800));
        assert_eq!(
            schedule.restart_at,
            Duration::from_secs(28_800) + INTERRUPT_GRACE
        );
    }
}
