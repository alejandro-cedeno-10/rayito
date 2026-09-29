//! Time as a port. The domain never reads `Instant::now()` or
//! `SystemTime::now()` directly, so lifecycle rules stay deterministic in
//! tests. `Deadline` is an instant on the *running* clock (monotonic time
//! minus suspended time, `SandboxSession::running_now`), which is what every
//! server deadline is measured on (design D6).

use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

/// Monotonic and wall clocks read together.
pub trait Clock: Send + Sync {
    /// Time elapsed on a monotonic clock since a fixed, arbitrary origin.
    fn monotonic(&self) -> Duration;
    /// Wall-clock time.
    fn wall(&self) -> SystemTime;

    fn read(&self) -> ClockReading {
        ClockReading {
            monotonic: self.monotonic(),
            wall: self.wall(),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ClockReading {
    pub monotonic: Duration,
    pub wall: SystemTime,
}

impl ClockReading {
    /// Wall-clock drift relative to the monotonic clock since `earlier`, in
    /// milliseconds: zero when both clocks advanced equally, negative when the
    /// wall clock lagged (frozen during a snapshot), positive when it jumped
    /// ahead. This is what `Health.clock_offset_ms` reports after a resume.
    #[must_use]
    pub fn offset_ms_since(&self, earlier: &ClockReading) -> i64 {
        let wall_delta = signed_millis(earlier.wall, self.wall);
        let monotonic_delta = millis(self.monotonic.saturating_sub(earlier.monotonic));
        wall_delta.saturating_sub(monotonic_delta)
    }
}

fn signed_millis(from: SystemTime, to: SystemTime) -> i64 {
    match to.duration_since(from) {
        Ok(forward) => millis(forward),
        Err(backwards) => millis(backwards.duration()).saturating_neg(),
    }
}

fn millis(duration: Duration) -> i64 {
    i64::try_from(duration.as_millis()).unwrap_or(i64::MAX)
}

/// Milliseconds since the Unix epoch: `0` before the epoch, saturating at
/// `i64::MAX`.
#[must_use]
pub fn unix_millis(wall: SystemTime) -> i64 {
    wall.duration_since(UNIX_EPOCH).map_or(0, |since| {
        i64::try_from(since.as_millis()).unwrap_or(i64::MAX)
    })
}

/// An instant on the running clock. Comparing it with a later
/// `running_now` reading says whether it is due; a pause between the two
/// readings does not consume any of the remaining budget.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub struct Deadline {
    pub at: Duration,
}

impl Deadline {
    #[must_use]
    pub fn after(running_now: Duration, timeout: Duration) -> Self {
        Self {
            at: running_now.saturating_add(timeout),
        }
    }

    /// `None` once the deadline is due.
    #[must_use]
    pub fn remaining(self, running_now: Duration) -> Option<Duration> {
        self.at
            .checked_sub(running_now)
            .filter(|left| !left.is_zero())
    }
}

/// Process clocks: `Instant` for the monotonic side (measured from
/// construction) and `SystemTime` for the wall side.
pub struct SystemClock {
    origin: Instant,
}

impl SystemClock {
    #[must_use]
    pub fn new() -> Self {
        Self {
            origin: Instant::now(),
        }
    }
}

impl Default for SystemClock {
    fn default() -> Self {
        Self::new()
    }
}

impl Clock for SystemClock {
    fn monotonic(&self) -> Duration {
        self.origin.elapsed()
    }

    fn wall(&self) -> SystemTime {
        SystemTime::now()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn reading(monotonic_secs: u64, wall_secs: u64) -> ClockReading {
        ClockReading {
            monotonic: Duration::from_secs(monotonic_secs),
            wall: UNIX_EPOCH + Duration::from_secs(wall_secs),
        }
    }

    #[test]
    fn offset_is_zero_when_both_clocks_advance_equally() {
        let before = reading(10, 1_000);
        let after = reading(323, 1_313);
        assert_eq!(after.offset_ms_since(&before), 0);
    }

    #[test]
    fn offset_is_negative_when_wall_clock_was_frozen() {
        let before = reading(10, 1_000);
        let after = reading(323, 1_001);
        assert_eq!(after.offset_ms_since(&before), -312_000);
    }

    #[test]
    fn offset_is_positive_when_wall_clock_jumped_ahead() {
        let before = reading(10, 1_000);
        let after = reading(11, 1_005);
        assert_eq!(after.offset_ms_since(&before), 4_000);
    }

    #[test]
    fn offset_tolerates_wall_clock_going_backwards() {
        let before = reading(10, 1_000);
        let after = reading(12, 999);
        assert_eq!(after.offset_ms_since(&before), -3_000);
    }

    /// The saturation case only exists where `SystemTime` can hold more than
    /// `i64::MAX` milliseconds (Unix `timespec`); a Windows `FILETIME` tops
    /// out long before, so `checked_add` yields `None` there.
    #[test]
    fn unix_millis_saturates_and_clamps_pre_epoch() {
        assert_eq!(unix_millis(UNIX_EPOCH), 0);
        assert_eq!(
            unix_millis(UNIX_EPOCH + Duration::from_millis(1_790_000_000_123)),
            1_790_000_000_123
        );
        assert_eq!(unix_millis(UNIX_EPOCH - Duration::from_secs(1)), 0);
        let past_i64_millis = Duration::from_secs(9_223_372_036_854_776);
        if let Some(far_future) = UNIX_EPOCH.checked_add(past_i64_millis) {
            assert_eq!(unix_millis(far_future), i64::MAX);
        }
    }

    #[test]
    fn deadline_remaining_before_at_and_after() {
        let deadline = Deadline::after(Duration::from_secs(10), Duration::from_secs(5));
        assert_eq!(deadline.at, Duration::from_secs(15));
        assert_eq!(
            deadline.remaining(Duration::from_secs(12)),
            Some(Duration::from_secs(3))
        );
        assert_eq!(deadline.remaining(Duration::from_secs(15)), None);
        assert_eq!(deadline.remaining(Duration::from_secs(20)), None);
    }

    /// The running clock stands still during a pause, so the reading after
    /// it is what it was before and the remaining budget is intact.
    #[test]
    fn deadline_survives_a_simulated_pause() {
        let deadline = Deadline::after(Duration::from_secs(10), Duration::from_millis(1_500));
        let before_pause = Duration::from_millis(10_500);
        let after_pause = before_pause;
        assert_eq!(
            deadline.remaining(after_pause),
            Some(Duration::from_millis(1_000))
        );
        assert_eq!(
            Deadline::after(Duration::MAX, Duration::from_secs(1)).at,
            Duration::MAX
        );
    }

    #[test]
    fn system_clock_is_monotonic() {
        let clock = SystemClock::new();
        let first = clock.monotonic();
        let second = clock.monotonic();
        assert!(second >= first);
    }
}
