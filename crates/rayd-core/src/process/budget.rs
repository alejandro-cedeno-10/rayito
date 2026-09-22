//! The sandbox-wide byte budget every replay ring (process, PTY, execution)
//! charges what it retains against, so a sandbox cannot turn `rayd`'s
//! replay memory into 384 MiB of rings: over budget the oldest bytes go
//! first, and the reaper drops ended entries early above the high-water
//! mark. One shared counter, one atomic add or sub per chunk.

use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};

pub const SANDBOX_OUTPUT_BUDGET_BYTES: usize = 128 * 1024 * 1024;
/// Above this the reaper drops ended entries oldest-first regardless of
/// their retention window.
pub const OUTPUT_BUDGET_HIGH_WATER: usize = 96 * 1024 * 1024;

#[derive(Debug, Clone)]
pub struct OutputBudget {
    used: Arc<AtomicUsize>,
    capacity: usize,
    high_water: usize,
}

impl Default for OutputBudget {
    fn default() -> Self {
        Self::new(SANDBOX_OUTPUT_BUDGET_BYTES, OUTPUT_BUDGET_HIGH_WATER)
    }
}

impl OutputBudget {
    #[must_use]
    pub fn new(capacity: usize, high_water: usize) -> Self {
        Self {
            used: Arc::new(AtomicUsize::new(0)),
            capacity,
            high_water: high_water.min(capacity),
        }
    }

    /// No bound at all: for hosts and tests that only care about the
    /// per-ring capacity.
    #[must_use]
    pub fn unlimited() -> Self {
        Self::new(usize::MAX, usize::MAX)
    }

    /// Reserves `bytes` when the total stays within the capacity; `false`
    /// leaves the counter untouched.
    #[must_use]
    pub fn charge(&self, bytes: usize) -> bool {
        self.used
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |used| {
                used.checked_add(bytes)
                    .filter(|total| *total <= self.capacity)
            })
            .is_ok()
    }

    /// Never underflows: a release larger than the level clamps to zero.
    pub fn release(&self, bytes: usize) {
        let _ = self
            .used
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |used| {
                Some(used.saturating_sub(bytes))
            });
    }

    #[must_use]
    pub fn level(&self) -> usize {
        self.used.load(Ordering::Acquire)
    }

    #[must_use]
    pub fn capacity(&self) -> usize {
        self.capacity
    }

    #[must_use]
    pub fn high_water(&self) -> usize {
        self.high_water
    }

    #[must_use]
    pub fn above_high_water(&self) -> bool {
        self.level() > self.high_water
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_are_the_contract_values() {
        let budget = OutputBudget::default();
        assert_eq!(budget.capacity(), 128 * 1024 * 1024);
        assert_eq!(budget.high_water(), 96 * 1024 * 1024);
        assert_eq!(budget.level(), 0);
        assert!(!budget.above_high_water());
    }

    #[test]
    fn charge_refuses_past_the_capacity_and_release_never_underflows() {
        let budget = OutputBudget::new(10, 6);
        assert!(budget.charge(6));
        assert!(!budget.above_high_water());
        assert!(budget.charge(4));
        assert!(budget.above_high_water());
        assert!(!budget.charge(1));
        assert_eq!(budget.level(), 10);
        budget.release(4);
        assert_eq!(budget.level(), 6);
        budget.release(100);
        assert_eq!(budget.level(), 0);
        assert!(budget.charge(10));
    }

    #[test]
    fn clones_share_the_counter() {
        let budget = OutputBudget::new(8, 8);
        let other = budget.clone();
        assert!(other.charge(8));
        assert!(!budget.charge(1));
        budget.release(8);
        assert!(other.charge(1));
    }

    #[test]
    fn high_water_never_exceeds_the_capacity() {
        let budget = OutputBudget::new(4, 9);
        assert_eq!(budget.high_water(), 4);
        assert!(OutputBudget::unlimited().charge(usize::MAX - 1));
    }
}
