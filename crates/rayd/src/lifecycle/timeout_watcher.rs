//! The watcher of the logical deadline (ADR-011): a named `std::thread`
//! (`rayd-timeout`), independent of the tokio runtime, that ticks the
//! session's deadline every `tick` and acts on what it returns. A stalled
//! runtime therefore cannot skip the deadline: on `Terminate` the thread
//! itself calls the terminator's `force` once `force_budget` has passed.
//!
//! A gap of at least `freeze_threshold` between two ticks is the signature
//! of a real checkpoint (`CLOCK_MONOTONIC` jumps across it, `AWS_API_NOTES.md`
//! §15). The `/resume` hook asks the watcher for that verdict (`frozen`), so
//! a forged `/suspend` + `/resume` pair alone neither opens the resume grace
//! nor applies the auto-resume rule. Starving this thread of CPU from inside
//! the VM can fake the gap, so the deadline machine grants one grace per
//! deadline at most, never past the cap.
//!
//! Every transition logs one `sandbox_timeout` line with the phase, the
//! policy, the timeout, the extension count, the overrun and the action;
//! never a token, a payload character or metadata.

use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::thread::{self, Thread};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use rayd_core::sandbox_timeout::{
    DeadlineAction, SelfTerminator, TIMEOUT_FREEZE_THRESHOLD, TerminationReason, TimeoutAction,
    TimeoutSettings,
};
use rayd_core::session::SandboxSession;

use super::suspend::{StreamCloseReason, SuspendSignal};
use crate::code::CodeManager;

pub const TIMEOUT_THREAD_NAME: &str = "rayd-timeout";
/// Stored in place of a tick instant the watcher has not taken yet.
const NEVER: u64 = u64::MAX;

/// Closes every open client stream the way `/suspend` does, for another
/// reason.
pub struct StreamCloser {
    code: Arc<CodeManager>,
    suspend: Arc<SuspendSignal>,
}

impl StreamCloser {
    #[must_use]
    pub fn new(code: Arc<CodeManager>, suspend: Arc<SuspendSignal>) -> Self {
        Self { code, suspend }
    }

    /// The execute origins are detached first, as on `/suspend`, so closing
    /// their streams never interrupts a running cell. Returns how many
    /// streams were open when the close began.
    #[must_use]
    pub fn close(&self, reason: StreamCloseReason) -> usize {
        self.code.detach_for_suspend();
        let open = self.suspend.open_streams();
        self.suspend.close_all(reason);
        open
    }
}

/// The handle the adapters keep: `wake` after anything that moved the
/// deadline, `frozen` for the `/resume` verdict.
pub struct TimeoutWatcher {
    thread: Option<Thread>,
    ticks: Arc<TickRecord>,
}

impl TimeoutWatcher {
    /// A watcher without a thread, for hosts and tests that never install a
    /// lifecycle: `wake` does nothing and `frozen` is always false.
    #[must_use]
    pub fn detached() -> Arc<Self> {
        Arc::new(Self {
            thread: None,
            ticks: Arc::new(TickRecord::new(TIMEOUT_FREEZE_THRESHOLD)),
        })
    }

    /// Makes the thread evaluate the deadline now instead of at its next
    /// tick: after `/run` installed a lifecycle, after a changed `/resume`
    /// and after every successful `SetTimeout`.
    pub fn wake(&self) {
        if let Some(thread) = &self.thread {
            thread.unpark();
        }
    }

    /// Whether the VM was frozen right before monotonic `now`: no tick for
    /// at least the freeze threshold, or the watcher saw the thaw itself
    /// less than a threshold ago (it may tick between the thaw and the
    /// `/resume`).
    #[must_use]
    pub fn frozen(&self, now: Duration) -> bool {
        self.ticks.frozen(now)
    }
}

/// Starts the watcher thread over the session's own `TimeoutSettings`.
pub fn spawn_timeout_watcher(
    session: Arc<SandboxSession>,
    terminator: Arc<dyn SelfTerminator>,
    closer: StreamCloser,
) -> std::io::Result<Arc<TimeoutWatcher>> {
    let settings = session.settings().timeout;
    let ticks = Arc::new(TickRecord::new(settings.freeze_threshold));
    let watch = Watch {
        session,
        terminator,
        closer,
        settings,
        ticks: ticks.clone(),
    };
    let handle = thread::Builder::new()
        .name(TIMEOUT_THREAD_NAME.to_owned())
        .spawn(move || watch.run())?;
    Ok(Arc::new(TimeoutWatcher {
        thread: Some(handle.thread().clone()),
        ticks,
    }))
}

/// The last tick and the last thaw the thread saw, in monotonic
/// milliseconds, shared with the hooks.
struct TickRecord {
    last_tick_ms: AtomicU64,
    last_thaw_ms: AtomicU64,
    freeze_threshold: Duration,
}

impl TickRecord {
    fn new(freeze_threshold: Duration) -> Self {
        Self {
            last_tick_ms: AtomicU64::new(NEVER),
            last_thaw_ms: AtomicU64::new(NEVER),
            freeze_threshold,
        }
    }

    /// Starts counting gaps from `now`, without inferring a thaw from the
    /// time the thread spent parked.
    fn baseline(&self, now: Duration) {
        self.last_tick_ms.store(millis(now), Ordering::SeqCst);
    }

    /// Records a tick at `now`; true when the gap since the previous one is
    /// a thaw.
    fn tick(&self, now: Duration) -> bool {
        let previous = self.last_tick_ms.swap(millis(now), Ordering::SeqCst);
        let thawed = since(now, previous).is_some_and(|gap| gap >= self.freeze_threshold);
        if thawed {
            self.last_thaw_ms.store(millis(now), Ordering::SeqCst);
        }
        thawed
    }

    fn frozen(&self, now: Duration) -> bool {
        let since_tick = since(now, self.last_tick_ms.load(Ordering::SeqCst));
        let since_thaw = since(now, self.last_thaw_ms.load(Ordering::SeqCst));
        since_tick.is_some_and(|gap| gap >= self.freeze_threshold)
            || since_thaw.is_some_and(|gap| gap < self.freeze_threshold)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Flow {
    Continue,
    Stop,
}

struct Watch {
    session: Arc<SandboxSession>,
    terminator: Arc<dyn SelfTerminator>,
    closer: StreamCloser,
    settings: TimeoutSettings,
    ticks: Arc<TickRecord>,
}

impl Watch {
    /// Parks while no lifecycle is installed; otherwise ticks until a
    /// `Terminate` ended the agent (or the test's stand-in for it).
    fn run(self) {
        loop {
            if !self.session.timeout_managed() {
                thread::park();
                self.ticks.baseline(self.session.clock().monotonic());
                continue;
            }
            let thawed = self.ticks.tick(self.session.clock().monotonic());
            let action = self.session.tick_timeout(thawed);
            if action.is_some_and(|action| self.dispatch(action) == Flow::Stop) {
                return;
            }
            thread::park_timeout(self.settings.tick);
        }
    }

    fn dispatch(&self, action: DeadlineAction) -> Flow {
        self.log_transition(action);
        match action {
            DeadlineAction::Expire | DeadlineAction::Gate => {
                let streams_closed = self.closer.close(StreamCloseReason::SandboxTimeout);
                tracing::info!(
                    streams_closed,
                    action = action.as_str(),
                    "sandbox_timeout streams closed"
                );
                Flow::Continue
            }
            DeadlineAction::ReExpire => Flow::Continue,
            DeadlineAction::Terminate => {
                self.terminate();
                Flow::Stop
            }
        }
    }

    /// `begin` never blocks; if the graceful sequence has not ended the
    /// process within the budget, `force` does.
    fn terminate(&self) {
        let reason = TerminationReason::SandboxTimeout;
        self.terminator.begin(reason);
        thread::sleep(self.settings.force_budget);
        self.terminator.force(reason);
    }

    fn log_transition(&self, action: DeadlineAction) {
        let view = self.session.lifecycle();
        let overrun_ms = unix_millis(self.session.clock().wall()) - view.deadline_unix_ms;
        tracing::info!(
            phase = view.phase.as_str(),
            on_timeout = view.on_timeout.map_or("none", TimeoutAction::as_str),
            timeout_ms = millis(view.timeout),
            extensions = view.extensions,
            overrun_ms,
            action = action.as_str(),
            "sandbox_timeout"
        );
    }
}

fn since(now: Duration, stored_ms: u64) -> Option<Duration> {
    (stored_ms != NEVER).then(|| now.saturating_sub(Duration::from_millis(stored_ms)))
}

fn millis(duration: Duration) -> u64 {
    u64::try_from(duration.as_millis()).unwrap_or(NEVER - 1)
}

fn unix_millis(wall: SystemTime) -> i64 {
    wall.duration_since(UNIX_EPOCH).map_or(0, |since| {
        i64::try_from(since.as_millis()).unwrap_or(i64::MAX)
    })
}

#[cfg(test)]
mod tests {
    use std::sync::Mutex;
    use std::time::Instant;

    use rayd_core::clock::Clock;
    use rayd_core::session::{RunHookInput, SessionSettings};

    use super::*;
    use crate::adapters::OsRandomSource;

    /// A clock the test moves by hand; the wall side follows it.
    #[derive(Default)]
    struct StepClock {
        ms: AtomicU64,
    }

    impl StepClock {
        fn advance(&self, by: Duration) {
            self.ms.fetch_add(millis(by), Ordering::SeqCst);
        }
    }

    impl Clock for StepClock {
        fn monotonic(&self) -> Duration {
            Duration::from_millis(self.ms.load(Ordering::SeqCst))
        }

        fn wall(&self) -> SystemTime {
            UNIX_EPOCH + Duration::from_secs(1_700_000_000) + self.monotonic()
        }
    }

    #[derive(Default)]
    struct RecordingTerminator {
        calls: Mutex<Vec<(&'static str, Instant)>>,
    }

    impl RecordingTerminator {
        fn calls(&self) -> Vec<&'static str> {
            self.calls
                .lock()
                .unwrap()
                .iter()
                .map(|(name, _)| *name)
                .collect()
        }

        fn gap(&self) -> Duration {
            let calls = self.calls.lock().unwrap();
            calls[1].1.duration_since(calls[0].1)
        }
    }

    impl SelfTerminator for RecordingTerminator {
        fn begin(&self, _reason: TerminationReason) {
            self.calls.lock().unwrap().push(("begin", Instant::now()));
        }

        fn force(&self, _reason: TerminationReason) {
            self.calls.lock().unwrap().push(("force", Instant::now()));
        }
    }

    const DIGEST: &str = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";

    fn settings(freeze_threshold: Duration) -> TimeoutSettings {
        TimeoutSettings {
            tick: Duration::from_millis(5),
            freeze_threshold,
            force_budget: Duration::from_millis(60),
            ..TimeoutSettings::default()
        }
    }

    struct Fixture {
        clock: Arc<StepClock>,
        session: Arc<SandboxSession>,
        suspend: Arc<SuspendSignal>,
        terminator: Arc<RecordingTerminator>,
        watcher: Arc<TimeoutWatcher>,
    }

    fn fixture(on_timeout: &str, freeze_threshold: Duration) -> Fixture {
        let clock = Arc::new(StepClock::default());
        let session = Arc::new(SandboxSession::with_settings(
            clock.clone(),
            "test",
            SessionSettings {
                timeout: settings(freeze_threshold),
                ..SessionSettings::default()
            },
        ));
        let payload = format!(
            "{{\"v\":1,\"token_sha256\":\"{DIGEST}\",\"lifecycle\":{{\"auto_resume\":false,\"cap_s\":900,\"on_timeout\":\"{on_timeout}\",\"timeout_s\":60}}}}"
        );
        session.run(RunHookInput {
            sandbox_id: Some("mvm-test"),
            payload: Some(&payload),
        });
        assert!(session.timeout_managed());
        let suspend = Arc::new(SuspendSignal::new());
        let terminator = Arc::new(RecordingTerminator::default());
        let code = CodeManager::disabled(session.clone(), Arc::new(OsRandomSource));
        let watcher = spawn_timeout_watcher(
            session.clone(),
            terminator.clone(),
            StreamCloser::new(code, suspend.clone()),
        )
        .unwrap();
        watcher.wake();
        Fixture {
            clock,
            session,
            suspend,
            terminator,
            watcher,
        }
    }

    fn wait_until(what: &str, condition: impl Fn() -> bool) {
        let deadline = Instant::now() + Duration::from_secs(10);
        while !condition() {
            assert!(Instant::now() < deadline, "timed out waiting for {what}");
            thread::sleep(Duration::from_millis(2));
        }
    }

    #[test]
    fn kill_mode_begins_then_forces_after_the_budget() {
        let fixture = fixture("kill", Duration::from_secs(3_600));
        thread::sleep(Duration::from_millis(30));
        assert!(fixture.terminator.calls().is_empty());
        fixture.clock.advance(Duration::from_secs(61));
        wait_until("begin and force", || fixture.terminator.calls().len() == 2);
        assert_eq!(fixture.terminator.calls(), vec!["begin", "force"]);
        assert!(fixture.terminator.gap() >= Duration::from_millis(60));
        assert!(!fixture.session.admits_rpc("/rayito.v1.ProcessService/List"));
    }

    #[test]
    fn pause_mode_expiry_closes_the_streams_without_terminating() {
        let fixture = fixture("pause", Duration::from_secs(3_600));
        let mut watch = fixture.suspend.subscribe();
        fixture.clock.advance(Duration::from_secs(61));
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_time()
            .build()
            .unwrap();
        runtime
            .block_on(async {
                tokio::time::timeout(Duration::from_secs(10), watch.suspended()).await
            })
            .unwrap();
        assert_eq!(watch.close_reason(), StreamCloseReason::SandboxTimeout);
        assert_eq!(fixture.suspend.generation(), 0);
        thread::sleep(Duration::from_millis(100));
        assert!(fixture.terminator.calls().is_empty());
        assert!(
            fixture
                .session
                .admits_rpc(rayd_core::auth::ANONYMOUS_RPC_PATH)
        );
        assert!(!fixture.session.admits_rpc("/rayito.v1.ProcessService/List"));
    }

    #[test]
    fn a_monotonic_jump_is_a_thaw_and_frozen_reports_it_briefly() {
        let threshold = Duration::from_millis(300);
        let fixture = fixture("kill", threshold);
        wait_until("a first tick", || {
            fixture.ticks_seen() && !fixture.watcher.frozen(fixture.clock.monotonic())
        });
        fixture.clock.advance(Duration::from_secs(5));
        assert!(fixture.watcher.frozen(fixture.clock.monotonic()));
        wait_until("the thaw to age out", || {
            fixture.clock.advance(Duration::from_millis(10));
            !fixture.watcher.frozen(fixture.clock.monotonic())
        });
        assert!(fixture.terminator.calls().is_empty());
    }

    #[test]
    fn a_detached_watcher_never_reports_a_freeze() {
        let watcher = TimeoutWatcher::detached();
        watcher.wake();
        assert!(!watcher.frozen(Duration::from_secs(100_000)));
    }

    impl Fixture {
        fn ticks_seen(&self) -> bool {
            self.watcher.ticks.last_tick_ms.load(Ordering::SeqCst) != NEVER
        }
    }
}
