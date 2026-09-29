//! The freeze verdict of ADR-011, as pure rules over the gaps the watcher
//! measures on the monotonic clock.
//!
//! A gap of at least the freeze threshold between two watcher ticks is the
//! signature of a real checkpoint (`CLOCK_MONOTONIC` jumps across it,
//! `AWS_API_NOTES.md` §15). The `/resume` hook asks for that verdict, so a
//! forged `/suspend` + `/resume` pair alone neither opens the resume grace
//! nor applies the auto-resume rule. Starving the watcher of CPU from inside
//! the VM can fake the gap, which is why the deadline machine grants one
//! grace per deadline at most, never past the cap.

use std::time::Duration;

/// True when the gap since the previous tick is a thaw: the VM was frozen
/// across it. `None` (no previous tick) is never a thaw.
#[must_use]
pub fn is_thaw(since_last_tick: Option<Duration>, threshold: Duration) -> bool {
    since_last_tick.is_some_and(|gap| gap >= threshold)
}

/// True when the VM counts as just frozen: either the watcher has not
/// ticked for a whole threshold (a `/resume` that lands before the first
/// tick after the thaw), or it saw a thaw less than a threshold ago. The
/// verdict fades once a threshold has passed since the thaw, so a later
/// forged `/resume` does not reuse it.
#[must_use]
pub fn was_frozen(
    since_tick: Option<Duration>,
    since_thaw: Option<Duration>,
    threshold: Duration,
) -> bool {
    since_tick.is_some_and(|gap| gap >= threshold) || since_thaw.is_some_and(|gap| gap < threshold)
}

#[cfg(test)]
mod tests {
    use super::*;

    const THRESHOLD: Duration = Duration::from_secs(2);
    const MS: Duration = Duration::from_millis(1);
    const JUST_UNDER: Duration = Duration::from_millis(1_999);
    const JUST_OVER: Duration = Duration::from_millis(2_001);

    #[test]
    fn is_thaw_at_and_around_the_threshold() {
        let cases = [
            (None, false),
            (Some(Duration::ZERO), false),
            (Some(JUST_UNDER), false),
            (Some(THRESHOLD), true),
            (Some(JUST_OVER), true),
        ];
        for (gap, expected) in cases {
            assert_eq!(is_thaw(gap, THRESHOLD), expected, "gap {gap:?}");
        }
    }

    #[test]
    fn was_frozen_over_every_tick_and_thaw_combination() {
        let long_ago = Some(THRESHOLD * 10);
        let tick_gaps = [
            (None, false),
            (Some(JUST_UNDER), false),
            (Some(THRESHOLD), true),
            (long_ago, true),
        ];
        let thaw_gaps = [
            (None, false),
            (Some(Duration::ZERO), true),
            (Some(THRESHOLD), false),
            (long_ago, false),
        ];
        for (since_tick, stale_tick) in tick_gaps {
            for (since_thaw, recent_thaw) in thaw_gaps {
                assert_eq!(
                    was_frozen(since_tick, since_thaw, THRESHOLD),
                    stale_tick || recent_thaw,
                    "since_tick {since_tick:?}, since_thaw {since_thaw:?}"
                );
            }
        }
    }

    #[test]
    fn was_frozen_just_before_the_thaw_fades() {
        assert!(was_frozen(Some(MS), Some(JUST_UNDER), THRESHOLD));
        assert!(!was_frozen(Some(MS), Some(THRESHOLD), THRESHOLD));
    }
}
