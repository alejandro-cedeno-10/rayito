//! One periodic tick that drops every retained thing whose 30 s window on
//! the running clock closed: ended processes and PTYs (the shared
//! registry) and ended executions.

use std::sync::Arc;
use std::time::Duration;

use tokio::task::JoinHandle;

pub const DEFAULT_REAPER_INTERVAL: Duration = Duration::from_secs(5);

pub trait Reaper: Send + Sync {
    fn reap_expired(&self);
}

/// Called once by whoever owns the runtime (`main`, the tests).
#[must_use]
pub fn spawn_reaper(interval: Duration, reapers: Vec<Arc<dyn Reaper>>) -> JoinHandle<()> {
    tokio::spawn(async move {
        let mut ticks = tokio::time::interval(interval);
        loop {
            ticks.tick().await;
            for reaper in &reapers {
                reaper.reap_expired();
            }
        }
    })
}
