//! The watchdog every accepted `/suspend` spawns (design D2). A real
//! suspension freezes the VM after the checkpoint and `CLOCK_MONOTONIC`
//! jumps by the frozen time at restore (measured), so a tick whose elapsed
//! time exceeds the freeze threshold means "AWS will call `/resume`" and
//! the watchdog stops. A `/suspend` nobody followed with a checkpoint (a
//! forged one through the proxy) keeps ticking unfrozen; after the gate
//! timeout the session is recovered into `Resumed` so new streams open
//! again, with one anomaly counted and no generation bump.

use std::sync::Arc;
use std::time::Duration;

use rayd_core::lifecycle::HookPhase;
use rayd_core::session::SandboxSession;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WatchdogOutcome {
    /// A tick jumped past the freeze threshold: a real checkpoint happened.
    Frozen,
    /// `/resume` (or a newer `/suspend`) moved the session first.
    Superseded,
    /// The gate timeout passed unfrozen and the session was recovered.
    Recovered,
}

/// Runs until the suspend of `suspend_generation` is resolved one way or
/// the other; the tick, the threshold and the timeout come from the
/// session's settings so tests can scale them.
pub async fn suspend_watchdog(
    session: Arc<SandboxSession>,
    suspend_generation: u64,
) -> WatchdogOutcome {
    let settings = session.settings();
    let clock = session.clock();
    let mut last = clock.monotonic();
    let mut unfrozen = Duration::ZERO;
    loop {
        tokio::time::sleep(settings.watchdog_tick).await;
        let now = clock.monotonic();
        let elapsed = now.saturating_sub(last);
        last = now;
        if elapsed > settings.freeze_threshold {
            return WatchdogOutcome::Frozen;
        }
        if !still_pending(&session, suspend_generation) {
            return WatchdogOutcome::Superseded;
        }
        unfrozen += elapsed;
        if unfrozen >= settings.suspend_gate_timeout {
            return recover(&session, suspend_generation);
        }
    }
}

fn still_pending(session: &SandboxSession, suspend_generation: u64) -> bool {
    session.phase() == HookPhase::Suspending && session.suspend_generation() == suspend_generation
}

fn recover(session: &SandboxSession, suspend_generation: u64) -> WatchdogOutcome {
    match session.recover_from_stale_suspend(suspend_generation) {
        Some(transition) => {
            tracing::warn!(
                hook = "suspend",
                suspend_generation,
                from = %transition.from,
                to = %transition.to,
                hook_anomalies = session.hook_anomalies(),
                "stale_suspend_recovered"
            );
            WatchdogOutcome::Recovered
        }
        None => WatchdogOutcome::Superseded,
    }
}

#[cfg(test)]
mod tests {
    use std::time::{SystemTime, UNIX_EPOCH};

    use rayd_core::clock::Clock;
    use rayd_core::session::{RunHookInput, SessionSettings};

    use super::*;

    struct TestClock {
        origin: tokio::time::Instant,
    }

    impl Clock for TestClock {
        fn monotonic(&self) -> Duration {
            self.origin.elapsed()
        }

        fn wall(&self) -> SystemTime {
            UNIX_EPOCH + self.monotonic()
        }
    }

    fn suspended_session() -> Arc<SandboxSession> {
        let clock = Arc::new(TestClock {
            origin: tokio::time::Instant::now(),
        });
        let session = Arc::new(SandboxSession::with_settings(
            clock,
            "test",
            SessionSettings::default(),
        ));
        let payload = "{\"v\":1,\"token_sha256\":\"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\"}";
        session.run(RunHookInput {
            sandbox_id: Some("mvm-1"),
            payload: Some(payload),
        });
        session.suspend().unwrap();
        session
    }

    #[tokio::test(start_paused = true)]
    async fn steady_ticks_recover_the_session_at_the_gate_timeout() {
        let session = suspended_session();
        let started = tokio::time::Instant::now();
        let outcome = suspend_watchdog(session.clone(), 1).await;
        assert_eq!(outcome, WatchdogOutcome::Recovered);
        assert_eq!(started.elapsed(), Duration::from_secs(20));
        assert_eq!(session.phase(), HookPhase::Resumed);
        assert_eq!(session.resume_generation(), 0);
        assert_eq!(session.hook_anomalies(), 1);
        assert_eq!(session.accepts_new_streams(), Ok(()));
    }

    #[tokio::test(start_paused = true)]
    async fn a_monotonic_jump_means_a_real_suspend_and_exits_without_recovery() {
        let session = suspended_session();
        let watchdog = tokio::spawn(suspend_watchdog(session.clone(), 1));
        tokio::time::sleep(Duration::from_millis(1_500)).await;
        tokio::time::advance(Duration::from_secs(30)).await;
        assert_eq!(watchdog.await.unwrap(), WatchdogOutcome::Frozen);
        assert_eq!(session.phase(), HookPhase::Suspending);
        assert_eq!(session.hook_anomalies(), 0);
        assert!(session.resume().unwrap().changed());
        assert_eq!(session.resume_generation(), 1);
    }

    #[tokio::test(start_paused = true)]
    async fn a_resume_first_makes_the_watchdog_a_no_op() {
        let session = suspended_session();
        let watchdog = tokio::spawn(suspend_watchdog(session.clone(), 1));
        tokio::time::sleep(Duration::from_millis(2_500)).await;
        session.resume().unwrap();
        assert_eq!(watchdog.await.unwrap(), WatchdogOutcome::Superseded);
        assert_eq!(session.hook_anomalies(), 0);
        assert_eq!(session.resume_generation(), 1);
    }

    #[tokio::test(start_paused = true)]
    async fn a_newer_suspend_supersedes_the_old_watchdog() {
        let session = suspended_session();
        let watchdog = tokio::spawn(suspend_watchdog(session.clone(), 1));
        tokio::time::sleep(Duration::from_millis(2_500)).await;
        session.resume().unwrap();
        tokio::time::sleep(Duration::from_millis(2_500)).await;
        session.suspend().unwrap();
        assert_eq!(watchdog.await.unwrap(), WatchdogOutcome::Superseded);
        assert_eq!(session.suspend_generation(), 2);
        assert_eq!(session.phase(), HookPhase::Suspending);
    }
}
