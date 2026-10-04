//! One periodic tick that drops every retained thing whose 30 s window on
//! the running clock closed: ended processes and PTYs (the shared
//! registry) and ended executions; and the child reaper, which also wakes
//! on every `SIGCHLD` so an orphan zombie is reaped the moment it appears
//! (`rayd-orphan-reaper`).

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

/// Runs `reaper` on every `SIGCHLD` and at least every `interval` (the
/// sweep that catches anything a coalesced signal hid), each pass on the
/// blocking pool: it reads `/proc` and briefly holds off spawns. Without
/// a `SIGCHLD` handler (never expected on Linux) it degrades to the
/// periodic sweep alone.
#[must_use]
pub fn spawn_child_reaper(interval: Duration, reaper: Arc<dyn Reaper>) -> JoinHandle<()> {
    tokio::spawn(async move {
        let mut child_signals = child_signals();
        let mut ticks = tokio::time::interval(interval);
        loop {
            tokio::select! {
                _ = ticks.tick() => {}
                () = next_child_signal(&mut child_signals) => {}
            }
            let reaper = Arc::clone(&reaper);
            if let Err(error) = tokio::task::spawn_blocking(move || reaper.reap_expired()).await {
                tracing::warn!(reason = %error, "child reaper pass failed");
            }
        }
    })
}

#[cfg(unix)]
type ChildSignals = Option<tokio::signal::unix::Signal>;

#[cfg(unix)]
fn child_signals() -> ChildSignals {
    use tokio::signal::unix::{SignalKind, signal};
    signal(SignalKind::child())
        .inspect_err(|error| {
            tracing::warn!(reason = %error, "SIGCHLD handler unavailable; periodic sweep only");
        })
        .ok()
}

/// Resolves on the next `SIGCHLD`; never, once there is no handler.
#[cfg(unix)]
async fn next_child_signal(signals: &mut ChildSignals) {
    match signals {
        Some(stream) => {
            if stream.recv().await.is_none() {
                *signals = None;
            }
        }
        None => std::future::pending().await,
    }
}

#[cfg(not(unix))]
type ChildSignals = ();

#[cfg(not(unix))]
fn child_signals() -> ChildSignals {}

#[cfg(not(unix))]
async fn next_child_signal(_signals: &mut ChildSignals) {
    std::future::pending().await
}

#[cfg(test)]
mod tests {
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::time::Instant;

    use super::*;

    /// Far longer than the test: only the immediate first tick and
    /// `SIGCHLD` can wake the reaper.
    const NEVER_AGAIN: Duration = Duration::from_secs(3_600);
    const BUDGET: Duration = Duration::from_secs(10);

    #[derive(Default)]
    struct CountingReaper {
        passes: AtomicUsize,
    }

    impl Reaper for CountingReaper {
        fn reap_expired(&self) {
            self.passes.fetch_add(1, Ordering::SeqCst);
        }
    }

    async fn wait_for_passes(reaper: &CountingReaper, at_least: usize) {
        let deadline = Instant::now() + BUDGET;
        while reaper.passes.load(Ordering::SeqCst) < at_least {
            assert!(Instant::now() < deadline, "the reaper never ran");
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn a_child_exiting_wakes_the_reaper_before_its_next_tick() {
        let reaper = Arc::new(CountingReaper::default());
        let task = spawn_child_reaper(NEVER_AGAIN, reaper.clone());
        wait_for_passes(&reaper, 1).await; // the immediate first tick
        let mut child = crate::adapters::ChildRegistry::process()
            .spawn(&mut tokio::process::Command::new("true"))
            .unwrap();
        child.wait().await.unwrap();
        wait_for_passes(&reaper, 2).await;
        task.abort();
    }
}
