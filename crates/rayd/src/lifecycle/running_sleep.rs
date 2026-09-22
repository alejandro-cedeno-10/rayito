//! A sleep whose deadline is measured on the running clock. tokio timers run
//! on `CLOCK_MONOTONIC`, which keeps advancing while the VM is suspended
//! (M0 Q19), so a plain `sleep` armed before a pause fires at resume. This
//! one sleeps for the remaining running time, wakes (early, after a pause),
//! re-reads the running clock and sleeps the rest, however many times.

use std::sync::Arc;

use rayd_core::clock::Deadline;
use rayd_core::session::SandboxSession;

pub async fn running_sleep(session: &Arc<SandboxSession>, deadline: Deadline) {
    loop {
        match deadline.remaining(session.running_now()) {
            None => return,
            Some(remaining) => tokio::time::sleep(remaining).await,
        }
    }
}

#[cfg(test)]
mod tests {
    use std::time::{Duration, SystemTime, UNIX_EPOCH};

    use rayd_core::clock::Clock;
    use rayd_core::session::RunHookInput;

    use super::*;

    /// Follows tokio's paused clock, so `sleep`/`advance` move the monotonic
    /// reading the way a suspended VM's clock jumps at resume; the pause
    /// itself is the session phase, as in the in-process suspend tests.
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

    fn running_session() -> Arc<SandboxSession> {
        let clock = Arc::new(TestClock {
            origin: tokio::time::Instant::now(),
        });
        let session = Arc::new(SandboxSession::new(clock, "test"));
        let payload = "{\"v\":1,\"token_sha256\":\"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\"}";
        session.run(RunHookInput {
            sandbox_id: Some("mvm-1"),
            payload: Some(payload),
        });
        session
    }

    #[tokio::test(start_paused = true)]
    async fn wakes_at_the_deadline_without_a_pause() {
        let session = running_session();
        let deadline = Deadline::after(session.running_now(), Duration::from_secs(2));
        let started = tokio::time::Instant::now();
        running_sleep(&session, deadline).await;
        assert_eq!(started.elapsed(), Duration::from_secs(2));
    }

    #[tokio::test(start_paused = true)]
    async fn a_pause_extends_the_sleep_by_the_suspended_time() {
        let session = running_session();
        let deadline = Deadline::after(session.running_now(), Duration::from_millis(1_500));
        let sleeper = tokio::spawn({
            let session = session.clone();
            async move {
                let started = tokio::time::Instant::now();
                running_sleep(&session, deadline).await;
                started.elapsed()
            }
        });
        tokio::time::sleep(Duration::from_millis(500)).await;
        session.suspend().unwrap();
        tokio::time::sleep(Duration::from_secs(300)).await;
        assert!(!sleeper.is_finished(), "must not fire while suspending");
        session.resume().unwrap();
        let elapsed = sleeper.await.unwrap();
        assert_eq!(elapsed, Duration::from_millis(500 + 300_000 + 1_000));
    }

    /// The VM case: the monotonic clock jumps at resume, the pending timer
    /// fires at once, and the sleep re-arms for the running time left.
    #[tokio::test(start_paused = true)]
    async fn a_monotonic_jump_wakes_early_and_re_sleeps_the_remainder() {
        let session = running_session();
        let deadline = Deadline::after(session.running_now(), Duration::from_secs(2));
        let sleeper = tokio::spawn({
            let session = session.clone();
            async move { running_sleep(&session, deadline).await }
        });
        tokio::time::sleep(Duration::from_millis(500)).await;
        session.suspend().unwrap();
        tokio::time::advance(Duration::from_secs(60)).await;
        session.resume().unwrap();
        assert_eq!(session.suspended_total(), Duration::from_secs(60));
        assert_eq!(session.running_now(), Duration::from_millis(500));
        tokio::time::sleep(Duration::from_millis(1_400)).await;
        assert!(!sleeper.is_finished());
        tokio::time::sleep(Duration::from_millis(200)).await;
        assert!(sleeper.is_finished());
    }
}
