//! When an armed import asks S3 again (design D9): at once, then 1 s after
//! each attempt for the first ten minutes, then every 5 s, and never past
//! the ticket's expiry, which is wall-clock time (AWS corrects the wall
//! clock on resume, so a pause counts against it like it does for S3).

use std::time::{Duration, SystemTime, UNIX_EPOCH};

use super::{TRANSFER_POLL_FAST_INTERVAL, TRANSFER_POLL_FAST_WINDOW, TRANSFER_POLL_SLOW_INTERVAL};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PollStep {
    Wait(Duration),
    Expired,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PollSchedule {
    pub fast_interval: Duration,
    pub slow_interval: Duration,
    pub fast_window: Duration,
}

impl Default for PollSchedule {
    fn default() -> Self {
        Self {
            fast_interval: TRANSFER_POLL_FAST_INTERVAL,
            slow_interval: TRANSFER_POLL_SLOW_INTERVAL,
            fast_window: TRANSFER_POLL_FAST_WINDOW,
        }
    }
}

impl PollSchedule {
    /// The wait before the next attempt, cut short so the expiry itself is
    /// observed on time; `Expired` once the wall clock reached it.
    #[must_use]
    pub fn next(
        &self,
        since_admitted: Duration,
        now_unix_ms: i64,
        expires_at_unix_ms: i64,
    ) -> PollStep {
        let Some(left) = time_left(now_unix_ms, expires_at_unix_ms) else {
            return PollStep::Expired;
        };
        let interval = if since_admitted < self.fast_window {
            self.fast_interval
        } else {
            self.slow_interval
        };
        PollStep::Wait(interval.min(left))
    }
}

/// `None` once `now` reached `expires_at`.
#[must_use]
pub fn time_left(now_unix_ms: i64, expires_at_unix_ms: i64) -> Option<Duration> {
    let left = expires_at_unix_ms.checked_sub(now_unix_ms)?;
    u64::try_from(left)
        .ok()
        .filter(|millis| *millis > 0)
        .map(Duration::from_millis)
}

#[must_use]
pub fn is_expired(now_unix_ms: i64, expires_at_unix_ms: i64) -> bool {
    time_left(now_unix_ms, expires_at_unix_ms).is_none()
}

/// Milliseconds since the Unix epoch; `0` for an instant before it.
#[must_use]
pub fn unix_millis(instant: SystemTime) -> i64 {
    instant.duration_since(UNIX_EPOCH).map_or(0, |elapsed| {
        i64::try_from(elapsed.as_millis()).unwrap_or(i64::MAX)
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    const NOW: i64 = 1_790_000_000_000;
    const HOUR_LATER: i64 = NOW + 3_600_000;

    #[test]
    fn one_second_for_ten_minutes_then_five() {
        let schedule = PollSchedule::default();
        assert_eq!(
            schedule.next(Duration::ZERO, NOW, HOUR_LATER),
            PollStep::Wait(Duration::from_secs(1))
        );
        assert_eq!(
            schedule.next(Duration::from_secs(599), NOW, HOUR_LATER),
            PollStep::Wait(Duration::from_secs(1))
        );
        assert_eq!(
            schedule.next(Duration::from_secs(600), NOW, HOUR_LATER),
            PollStep::Wait(Duration::from_secs(5))
        );
        assert_eq!(
            schedule.next(Duration::from_mins(50), NOW, HOUR_LATER),
            PollStep::Wait(Duration::from_secs(5))
        );
    }

    #[test]
    fn the_last_wait_ends_at_the_expiry_and_then_it_stops() {
        let schedule = PollSchedule::default();
        assert_eq!(
            schedule.next(Duration::from_secs(700), NOW, NOW + 1_200),
            PollStep::Wait(Duration::from_millis(1_200))
        );
        assert_eq!(schedule.next(Duration::ZERO, NOW, NOW), PollStep::Expired);
        assert_eq!(
            schedule.next(Duration::ZERO, NOW, NOW - 1),
            PollStep::Expired
        );
        assert!(is_expired(NOW, NOW));
        assert!(!is_expired(NOW, NOW + 1));
    }

    #[test]
    fn unix_millis_reads_the_epoch_offset() {
        assert_eq!(unix_millis(UNIX_EPOCH), 0);
        assert_eq!(
            unix_millis(UNIX_EPOCH + Duration::from_millis(1_500)),
            1_500
        );
    }
}
