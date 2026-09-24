//! Decision counters of the local proxy (design D12): the only thing about
//! proxied traffic that is ever logged. `egress_proxy_stats` goes out
//! every 60 s and only when a counter changed; no target, address, port
//! or credential is ever part of it.

use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use rayd_core::network::DenyReason;
use tokio::task::JoinHandle;

pub const STATS_INTERVAL: Duration = Duration::from_mins(1);

#[derive(Debug, Default)]
pub struct DecisionCounters {
    allowed: AtomicU64,
    denied_policy: AtomicU64,
    denied_guard: AtomicU64,
    denied_invalid: AtomicU64,
    upstream_failed: AtomicU64,
    dial_failed: AtomicU64,
    rejected_busy: AtomicU64,
    active: AtomicU64,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct CountersSnapshot {
    pub allowed: u64,
    pub denied_policy: u64,
    pub denied_guard: u64,
    pub denied_invalid: u64,
    pub upstream_failed: u64,
    pub dial_failed: u64,
    pub rejected_busy: u64,
    pub active: u64,
}

impl DecisionCounters {
    pub fn allowed(&self) {
        self.allowed.fetch_add(1, Ordering::Relaxed);
    }

    pub fn denied(&self, reason: DenyReason) {
        let counter = match reason {
            DenyReason::Policy => &self.denied_policy,
            DenyReason::Guard => &self.denied_guard,
            DenyReason::Invalid => &self.denied_invalid,
        };
        counter.fetch_add(1, Ordering::Relaxed);
    }

    pub fn upstream_failed(&self) {
        self.upstream_failed.fetch_add(1, Ordering::Relaxed);
    }

    pub fn dial_failed(&self) {
        self.dial_failed.fetch_add(1, Ordering::Relaxed);
    }

    pub fn rejected_busy(&self) {
        self.rejected_busy.fetch_add(1, Ordering::Relaxed);
    }

    /// Counts one open client connection until the guard is dropped.
    #[must_use]
    pub fn open_connection(self: &Arc<Self>) -> ActiveConnection {
        self.active.fetch_add(1, Ordering::Relaxed);
        ActiveConnection {
            counters: self.clone(),
        }
    }

    #[must_use]
    pub fn snapshot(&self) -> CountersSnapshot {
        CountersSnapshot {
            allowed: self.allowed.load(Ordering::Relaxed),
            denied_policy: self.denied_policy.load(Ordering::Relaxed),
            denied_guard: self.denied_guard.load(Ordering::Relaxed),
            denied_invalid: self.denied_invalid.load(Ordering::Relaxed),
            upstream_failed: self.upstream_failed.load(Ordering::Relaxed),
            dial_failed: self.dial_failed.load(Ordering::Relaxed),
            rejected_busy: self.rejected_busy.load(Ordering::Relaxed),
            active: self.active.load(Ordering::Relaxed),
        }
    }
}

pub struct ActiveConnection {
    counters: Arc<DecisionCounters>,
}

impl Drop for ActiveConnection {
    fn drop(&mut self) {
        self.counters.active.fetch_sub(1, Ordering::Relaxed);
    }
}

/// Logs `egress_proxy_stats` every `interval` when anything changed.
pub fn spawn_stats_logger(counters: Arc<DecisionCounters>, interval: Duration) -> JoinHandle<()> {
    tokio::spawn(async move {
        let mut last = CountersSnapshot::default();
        let mut ticker = tokio::time::interval(interval);
        ticker.tick().await;
        loop {
            ticker.tick().await;
            let current = counters.snapshot();
            if current != last {
                log_stats(&current);
                last = current;
            }
        }
    })
}

fn log_stats(stats: &CountersSnapshot) {
    tracing::info!(
        allowed = stats.allowed,
        denied_policy = stats.denied_policy,
        denied_guard = stats.denied_guard,
        denied_invalid = stats.denied_invalid,
        upstream_failed = stats.upstream_failed,
        dial_failed = stats.dial_failed,
        rejected_busy = stats.rejected_busy,
        active = stats.active,
        "egress_proxy_stats"
    );
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn counters_track_decisions_and_open_connections() {
        let counters = Arc::new(DecisionCounters::default());
        counters.allowed();
        counters.denied(DenyReason::Policy);
        counters.denied(DenyReason::Guard);
        counters.denied(DenyReason::Guard);
        counters.denied(DenyReason::Invalid);
        counters.upstream_failed();
        counters.dial_failed();
        counters.rejected_busy();
        let open = counters.open_connection();
        assert_eq!(
            counters.snapshot(),
            CountersSnapshot {
                allowed: 1,
                denied_policy: 1,
                denied_guard: 2,
                denied_invalid: 1,
                upstream_failed: 1,
                dial_failed: 1,
                rejected_busy: 1,
                active: 1,
            }
        );
        drop(open);
        assert_eq!(counters.snapshot().active, 0);
    }
}
