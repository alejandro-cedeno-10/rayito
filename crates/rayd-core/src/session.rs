//! Application service shared by the gRPC and hooks adapters: one `MicroVM`
//! boot, its phase machine, its access-token gate, its clock, and the hook
//! defences (audit counters, stale-suspend recovery) that make a forged
//! hook visible once `/run` has been accepted. A session-changing
//! `/suspend` or `/resume` is never refused: the phase machine cannot tell
//! a forged call from the genuine one that follows it, and refusing the
//! genuine one would skip the checklist of a real checkpoint.

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::Duration;

use crate::auth::{self, AccessTokenGate, AuthError, InstallOutcome};
use crate::clock::{Clock, ClockReading};
use crate::health::HealthSnapshot;
use crate::hooks::{AuditEntry, HookAudit, HookCallOutcome};
use crate::lifecycle::{
    FREEZE_THRESHOLD, Hook, HookPhase, LifecycleError, LifecycleState, RunClaim,
    SUSPEND_GATE_TIMEOUT, Transition, WATCHDOG_TICK,
};
use crate::process::ProcessError;
use crate::run_payload::{RunDefaults, RunPayloadError, parse_run_payload};

/// Knobs the integration tests shrink; production uses the constants.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SessionSettings {
    pub suspend_gate_timeout: Duration,
    pub freeze_threshold: Duration,
    pub watchdog_tick: Duration,
}

impl Default for SessionSettings {
    fn default() -> Self {
        Self {
            suspend_gate_timeout: SUSPEND_GATE_TIMEOUT,
            freeze_threshold: FREEZE_THRESHOLD,
            watchdog_tick: WATCHDOG_TICK,
        }
    }
}

/// What the `/run` hook delivers, already stripped of its HTTP envelope.
#[derive(Debug, Clone, Copy)]
pub struct RunHookInput<'a> {
    pub sandbox_id: Option<&'a str>,
    pub payload: Option<&'a str>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RunOutcome {
    /// First `/run` of the boot with a valid payload: token digest installed.
    Installed,
    /// First `/run` of the boot, but the payload was rejected: the agent stays
    /// token-less and answers only `Health` (a 4xx here would kill the VM).
    Tokenless(RunPayloadError),
    /// A `/run` was already accepted this boot; nothing changed.
    AlreadyRan,
    /// `/run` arrived in a phase where it is not legal; nothing changed.
    Illegal(LifecycleError),
}

pub struct SandboxSession {
    state: Mutex<LifecycleState>,
    audit: Mutex<HookAudit>,
    defaults: Mutex<Option<RunDefaults>>,
    metadata: Mutex<BTreeMap<String, String>>,
    gate: AccessTokenGate,
    clock: Arc<dyn Clock>,
    booted_at: ClockReading,
    agent_version: &'static str,
    settings: SessionSettings,
}

impl SandboxSession {
    pub fn new(clock: Arc<dyn Clock>, agent_version: &'static str) -> Self {
        Self::with_settings(clock, agent_version, SessionSettings::default())
    }

    pub fn with_settings(
        clock: Arc<dyn Clock>,
        agent_version: &'static str,
        settings: SessionSettings,
    ) -> Self {
        let booted_at = clock.read();
        Self {
            state: Mutex::new(LifecycleState::new()),
            audit: Mutex::new(HookAudit::default()),
            defaults: Mutex::new(None),
            metadata: Mutex::new(BTreeMap::new()),
            gate: AccessTokenGate::new(),
            clock,
            booted_at,
            agent_version,
            settings,
        }
    }

    #[must_use]
    pub fn settings(&self) -> SessionSettings {
        self.settings
    }

    pub fn ready(&self) -> Result<Transition, LifecycleError> {
        self.state().ready()
    }

    pub fn validate(&self) -> Transition {
        self.state().validate()
    }

    pub fn run(&self, input: RunHookInput<'_>) -> RunOutcome {
        let claim = self.state().run(input.sandbox_id);
        match claim {
            Err(error) => RunOutcome::Illegal(error),
            Ok(RunClaim::AlreadyClaimed) => RunOutcome::AlreadyRan,
            Ok(RunClaim::Claimed) => {
                self.audit().run_accepted();
                self.install_payload(input.payload)
            }
        }
    }

    /// Always accepted from `Running`/`Resumed`, idempotent while
    /// `Suspending`: a forged `/suspend` costs the clients one reconnect,
    /// while a refused genuine one would leave the checkpoint unprepared.
    pub fn suspend(&self) -> Result<Transition, LifecycleError> {
        self.state().suspend(self.clock.read())
    }

    /// Always accepted from `Suspending` (or from a stale-recovered
    /// `Resumed`), idempotent otherwise.
    pub fn resume(&self) -> Result<Transition, LifecycleError> {
        self.state().resume(self.clock.read())
    }

    pub fn terminate(&self) -> Transition {
        self.state().terminate()
    }

    /// The watchdog's recovery of a `/suspend` that never froze the VM:
    /// counts one anomaly when it happens; `None` when nothing was left to
    /// recover (a `/resume` arrived or the generation moved on).
    pub fn recover_from_stale_suspend(&self, suspend_generation: u64) -> Option<Transition> {
        let recovered = self
            .state()
            .recover_from_stale_suspend(suspend_generation)?;
        self.audit().note_anomaly();
        Some(recovered)
    }

    /// Records a runtime hook call in the audit (after the accepted `/run`)
    /// and returns the entry to log, `None` before that.
    pub fn audit_hook(&self, hook: Hook, outcome: HookCallOutcome) -> Option<AuditEntry> {
        self.audit().record(hook, outcome)
    }

    /// Anomalous hook calls plus stale-suspend recoveries this boot.
    #[must_use]
    pub fn hook_anomalies(&self) -> u64 {
        self.audit().anomalies()
    }

    /// The `metadata` map of the accepted `/run` payload, verbatim (empty
    /// before `/run` or when the payload carried none).
    #[must_use]
    pub fn metadata(&self) -> BTreeMap<String, String> {
        self.metadata
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .clone()
    }

    #[must_use]
    pub fn phase(&self) -> HookPhase {
        self.state().phase()
    }

    #[must_use]
    pub fn suspend_generation(&self) -> u64 {
        self.state().suspend_generation()
    }

    #[must_use]
    pub fn resume_generation(&self) -> u64 {
        self.state().resume_generation()
    }

    /// Monotonic time spent suspended so far this boot.
    #[must_use]
    pub fn suspended_total(&self) -> Duration {
        self.state().suspended_total()
    }

    /// The running clock every server deadline is measured on: monotonic
    /// time minus every suspended interval (design D6). Frozen while
    /// `Suspending`, so a deadline armed before a pause still has the same
    /// budget left after any `/resume`.
    #[must_use]
    pub fn running_now(&self) -> Duration {
        let now = self.clock.monotonic();
        self.state().running_time(now)
    }

    #[must_use]
    pub fn has_access_token(&self) -> bool {
        self.gate.is_installed()
    }

    #[must_use]
    pub fn defaults(&self) -> Option<RunDefaults> {
        self.defaults
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .clone()
    }

    /// Spawn defaults of this boot: empty when `/run` delivered none.
    #[must_use]
    pub fn spawn_defaults(&self) -> RunDefaults {
        self.defaults().unwrap_or_default()
    }

    /// New process streams are only opened while the sandbox is `Running` or
    /// `Resumed`; during a suspend or a terminate the answer is `UNAVAILABLE`
    /// with the phase as the message.
    pub fn accepts_new_streams(&self) -> Result<(), ProcessError> {
        self.stream_gate()
            .map_err(|phase| ProcessError::NotAcceptingStreams { phase })
    }

    /// The same gate for every service that opens streams, with the
    /// refusing phase for the caller's own error type.
    pub fn stream_gate(&self) -> Result<(), HookPhase> {
        match self.phase() {
            HookPhase::Running | HookPhase::Resumed => Ok(()),
            phase => Err(phase),
        }
    }

    #[must_use]
    pub fn clock(&self) -> Arc<dyn Clock> {
        self.clock.clone()
    }

    #[must_use]
    pub fn health(&self) -> HealthSnapshot {
        let now = self.clock.read();
        let state = self.state();
        HealthSnapshot::builder(self.agent_version)
            .agent_ready(state.phase() != HookPhase::Terminating)
            .uptime(now.monotonic.saturating_sub(self.booted_at.monotonic))
            .sandbox_id(state.sandbox_id())
            .resume_generation(state.resume_generation())
            .clock_offset_ms(state.clock_offset_ms())
            .hook_anomalies(self.hook_anomalies())
            .metadata(self.metadata())
            .build()
    }

    pub fn authorize(
        &self,
        rpc_path: &str,
        presented_secret: Option<&[u8]>,
    ) -> Result<(), AuthError> {
        auth::authorize(&self.gate, rpc_path, presented_secret)
    }

    fn install_payload(&self, payload: Option<&str>) -> RunOutcome {
        let Some(raw) = payload else {
            return RunOutcome::Tokenless(RunPayloadError::Missing);
        };
        match parse_run_payload(raw) {
            Err(error) => RunOutcome::Tokenless(error),
            Ok(parsed) => {
                *self.defaults.lock().unwrap_or_else(PoisonError::into_inner) =
                    Some(parsed.defaults);
                *self.metadata.lock().unwrap_or_else(PoisonError::into_inner) = parsed.metadata;
                match self.gate.install_once(parsed.token_digest) {
                    InstallOutcome::Installed => RunOutcome::Installed,
                    InstallOutcome::AlreadyInstalled => RunOutcome::AlreadyRan,
                }
            }
        }
    }

    fn state(&self) -> MutexGuard<'_, LifecycleState> {
        self.state.lock().unwrap_or_else(PoisonError::into_inner)
    }

    fn audit(&self) -> MutexGuard<'_, HookAudit> {
        self.audit.lock().unwrap_or_else(PoisonError::into_inner)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::auth::ANONYMOUS_RPC_PATH;
    use crate::lifecycle::Hook;
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::time::{Duration, SystemTime, UNIX_EPOCH};

    const SECRET: &[u8] = b"sandbox secret";
    const DIGEST_HEX: &str = "8b6a1fd4d1ab7a1ec1f2c34c2f0ad0d3a9d6e2d7ab5a6d1c3e1c9a3f7e5b2d10";

    struct FakeClock {
        seconds: AtomicU64,
    }

    impl FakeClock {
        fn advance(&self, seconds: u64) {
            self.seconds.fetch_add(seconds, Ordering::SeqCst);
        }
    }

    impl Clock for FakeClock {
        fn monotonic(&self) -> Duration {
            Duration::from_secs(self.seconds.load(Ordering::SeqCst))
        }

        fn wall(&self) -> SystemTime {
            UNIX_EPOCH + Duration::from_secs(1_000 + self.seconds.load(Ordering::SeqCst))
        }
    }

    fn digest_hex_of(secret: &[u8]) -> String {
        use sha2::{Digest, Sha256};
        use std::fmt::Write as _;
        Sha256::digest(secret)
            .iter()
            .fold(String::new(), |mut hex, byte| {
                write!(hex, "{byte:02x}").unwrap();
                hex
            })
    }

    fn payload_for(secret: &[u8]) -> String {
        format!("{{\"v\":1,\"token_sha256\":\"{}\"}}", digest_hex_of(secret))
    }

    fn session() -> (Arc<FakeClock>, SandboxSession) {
        let clock = Arc::new(FakeClock {
            seconds: AtomicU64::new(0),
        });
        let session = SandboxSession::new(clock.clone(), "test");
        (clock, session)
    }

    fn run_with_secret(session: &SandboxSession, secret: &[u8]) -> RunOutcome {
        let payload = payload_for(secret);
        session.run(RunHookInput {
            sandbox_id: Some("mvm-1"),
            payload: Some(&payload),
        })
    }

    #[test]
    fn run_installs_the_token_and_the_defaults() {
        let (_, session) = session();
        let payload = format!(
            "{{\"v\":1,\"token_sha256\":\"{DIGEST_HEX}\",\"user\":\"user\",\"workdir\":\"/home/user\"}}"
        );
        let outcome = session.run(RunHookInput {
            sandbox_id: Some("mvm-1"),
            payload: Some(&payload),
        });
        assert_eq!(outcome, RunOutcome::Installed);
        assert!(session.has_access_token());
        assert_eq!(session.phase(), HookPhase::Running);
        assert_eq!(
            session.defaults().unwrap().workdir.as_deref(),
            Some("/home/user")
        );
        assert_eq!(session.health().sandbox_id.as_deref(), Some("mvm-1"));
    }

    #[test]
    fn second_run_keeps_the_first_token() {
        let (_, session) = session();
        assert_eq!(run_with_secret(&session, SECRET), RunOutcome::Installed);
        assert_eq!(
            run_with_secret(&session, b"attacker"),
            RunOutcome::AlreadyRan
        );
        let path = "/rayito.v1.ProcessService/List";
        assert_eq!(session.authorize(path, Some(SECRET)), Ok(()));
        assert_eq!(
            session.authorize(path, Some(b"attacker")),
            Err(AuthError::TokenMismatch)
        );
    }

    #[test]
    fn invalid_payload_leaves_the_agent_tokenless_but_running() {
        let (_, session) = session();
        let outcome = session.run(RunHookInput {
            sandbox_id: Some("mvm-1"),
            payload: Some("{\"v\":7}"),
        });
        assert_eq!(
            outcome,
            RunOutcome::Tokenless(RunPayloadError::UnsupportedVersion { actual: 7 })
        );
        assert!(!session.has_access_token());
        assert_eq!(session.phase(), HookPhase::Running);
        assert_eq!(session.authorize(ANONYMOUS_RPC_PATH, None), Ok(()));
        assert_eq!(
            session.authorize("/rayito.v1.ProcessService/List", Some(SECRET)),
            Err(AuthError::TokenNotInstalled)
        );
        assert_eq!(run_with_secret(&session, SECRET), RunOutcome::AlreadyRan);
    }

    #[test]
    fn missing_payload_is_tokenless() {
        let (_, session) = session();
        let outcome = session.run(RunHookInput {
            sandbox_id: Some("mvm-1"),
            payload: None,
        });
        assert_eq!(outcome, RunOutcome::Tokenless(RunPayloadError::Missing));
    }

    #[test]
    fn run_after_suspend_is_illegal_and_changes_nothing() {
        let (_, session) = session();
        assert_eq!(run_with_secret(&session, SECRET), RunOutcome::Installed);
        session.suspend().unwrap();
        let outcome = run_with_secret(&session, b"other");
        assert_eq!(outcome, RunOutcome::AlreadyRan);
        assert_eq!(session.phase(), HookPhase::Suspending);
    }

    #[test]
    fn suspend_then_resume_bumps_resume_generation_in_health() {
        let (clock, session) = session();
        run_with_secret(&session, SECRET);
        assert_eq!(session.health().resume_generation, 0);
        session.suspend().unwrap();
        clock.advance(300);
        session.resume().unwrap();
        let health = session.health();
        assert_eq!(health.resume_generation, 1);
        assert_eq!(health.clock_offset_ms, 0);
        assert_eq!(session.suspend_generation(), 1);
    }

    #[test]
    fn running_now_advances_only_by_the_non_suspended_part() {
        let (clock, session) = session();
        run_with_secret(&session, SECRET);
        clock.advance(10);
        assert_eq!(session.running_now(), Duration::from_secs(10));
        session.suspend().unwrap();
        clock.advance(300);
        assert_eq!(session.running_now(), Duration::from_secs(10));
        session.resume().unwrap();
        assert_eq!(session.suspended_total(), Duration::from_secs(300));
        assert_eq!(session.running_now(), Duration::from_secs(10));
        clock.advance(5);
        assert_eq!(session.running_now(), Duration::from_secs(15));
        for _ in 0..3 {
            session.suspend().unwrap();
            clock.advance(100);
            session.resume().unwrap();
            clock.advance(1);
        }
        assert_eq!(session.suspended_total(), Duration::from_secs(600));
        assert_eq!(session.running_now(), Duration::from_secs(18));
    }

    #[test]
    fn streams_are_accepted_only_while_running_or_resumed() {
        let (_, session) = session();
        assert_eq!(
            session.accepts_new_streams(),
            Err(ProcessError::NotAcceptingStreams {
                phase: HookPhase::Booting
            })
        );
        run_with_secret(&session, SECRET);
        assert_eq!(session.accepts_new_streams(), Ok(()));
        session.suspend().unwrap();
        assert_eq!(
            session.accepts_new_streams(),
            Err(ProcessError::NotAcceptingStreams {
                phase: HookPhase::Suspending
            })
        );
        session.resume().unwrap();
        assert_eq!(session.accepts_new_streams(), Ok(()));
        session.terminate();
        assert_eq!(
            session.accepts_new_streams(),
            Err(ProcessError::NotAcceptingStreams {
                phase: HookPhase::Terminating
            })
        );
    }

    #[test]
    fn spawn_defaults_are_empty_until_run_delivers_them() {
        let (_, session) = session();
        assert_eq!(session.spawn_defaults(), RunDefaults::default());
        let payload = format!(
            "{{\"v\":1,\"token_sha256\":\"{DIGEST_HEX}\",\"user\":\"user\",\"workdir\":\"/srv\"}}"
        );
        session.run(RunHookInput {
            sandbox_id: Some("mvm-1"),
            payload: Some(&payload),
        });
        assert_eq!(session.spawn_defaults().workdir.as_deref(), Some("/srv"));
    }

    #[test]
    fn suspend_before_run_reports_the_illegal_hook() {
        let (_, session) = session();
        assert_eq!(
            session.suspend(),
            Err(LifecycleError::IllegalTransition {
                hook: Hook::Suspend,
                phase: HookPhase::Booting
            })
        );
    }

    #[test]
    fn health_tracks_uptime_and_readiness() {
        let (clock, session) = session();
        clock.advance(12);
        let health = session.health();
        assert!(health.agent_ready);
        assert_eq!(health.uptime, Duration::from_secs(12));
        assert_eq!(health.agent_version, "test");
        assert_eq!(health.hook_anomalies, 0);
        assert!(health.metadata.is_empty());
        session.terminate();
        assert!(!session.health().agent_ready);
    }

    /// The sequence the transition rate limiter of the first M6 draft got
    /// wrong: a forged `/suspend` + `/resume` pair immediately followed by
    /// the platform's real `/suspend`. The real one must run the checklist
    /// and the real `/resume` after the freeze must be the one that bumps
    /// the generation and accounts the frozen span.
    #[test]
    fn a_forged_pair_never_makes_the_real_cycle_a_repeat() {
        let (clock, session) = session();
        run_with_secret(&session, SECRET);
        assert!(session.suspend().unwrap().changed());
        assert!(session.resume().unwrap().changed());
        assert_eq!(session.resume_generation(), 1);
        clock.advance(1);
        let real_suspend = session.suspend().unwrap();
        assert!(real_suspend.changed());
        assert_eq!(session.phase(), HookPhase::Suspending);
        assert_eq!(session.suspend_generation(), 2);
        clock.advance(300);
        let real_resume = session.resume().unwrap();
        assert!(real_resume.changed());
        assert_eq!(session.resume_generation(), 2);
        assert_eq!(session.suspended_total(), Duration::from_secs(300));
        assert_eq!(session.running_now(), Duration::from_secs(1));
        assert_eq!(session.hook_anomalies(), 0, "nothing was refused");
    }

    #[test]
    fn idempotent_repeats_change_nothing() {
        let (_, session) = session();
        run_with_secret(&session, SECRET);
        session.suspend().unwrap();
        assert!(!session.suspend().unwrap().changed());
        assert!(!session.suspend().unwrap().changed());
        session.resume().unwrap();
        assert!(!session.resume().unwrap().changed());
        assert_eq!(session.resume_generation(), 1);
        assert_eq!(session.hook_anomalies(), 0);
    }

    #[test]
    fn back_to_back_cycles_are_never_refused() {
        let (_, session) = session();
        run_with_secret(&session, SECRET);
        for cycle in 1..=3 {
            assert!(session.suspend().unwrap().changed(), "cycle {cycle}");
            assert!(session.resume().unwrap().changed(), "cycle {cycle}");
        }
        assert_eq!(session.resume_generation(), 3);
        assert_eq!(session.hook_anomalies(), 0);
    }

    #[test]
    fn recovery_counts_an_anomaly_and_the_next_resume_is_real() {
        let (clock, session) = session();
        run_with_secret(&session, SECRET);
        session.suspend().unwrap();
        clock.advance(25);
        assert_eq!(session.running_now(), Duration::ZERO);
        let recovered = session.recover_from_stale_suspend(1).unwrap();
        assert_eq!(recovered.to, HookPhase::Resumed);
        assert_eq!(session.hook_anomalies(), 1);
        assert_eq!(session.resume_generation(), 0);
        assert_eq!(session.running_now(), Duration::from_secs(25));
        assert_eq!(session.recover_from_stale_suspend(1), None);
        assert_eq!(session.hook_anomalies(), 1);
        clock.advance(5);
        let resumed = session.resume().unwrap();
        assert!(resumed.after_stale_recovery);
        assert_eq!(session.resume_generation(), 1);
        assert_eq!(session.running_now(), Duration::from_secs(30));
        assert!(!session.resume().unwrap().changed());
    }

    #[test]
    fn audit_opens_with_the_accepted_run() {
        let (_, session) = session();
        assert_eq!(
            session.audit_hook(Hook::Suspend, HookCallOutcome::Nominal),
            None
        );
        run_with_secret(&session, SECRET);
        let entry = session
            .audit_hook(Hook::Run, HookCallOutcome::Anomalous)
            .unwrap();
        assert_eq!(entry.calls_since_run, 1);
        assert!(entry.anomaly);
        assert_eq!(session.hook_anomalies(), 1);
        assert_eq!(session.health().hook_anomalies, 1);
    }

    #[test]
    fn metadata_is_echoed_from_the_accepted_run_only() {
        let (_, session) = session();
        let payload =
            format!("{{\"v\":1,\"token_sha256\":\"{DIGEST_HEX}\",\"metadata\":{{\"a\":\"1\"}}}}");
        session.run(RunHookInput {
            sandbox_id: Some("mvm-1"),
            payload: Some(&payload),
        });
        assert_eq!(session.metadata().get("a").map(String::as_str), Some("1"));
        assert_eq!(session.health().metadata.len(), 1);
        let other =
            format!("{{\"v\":1,\"token_sha256\":\"{DIGEST_HEX}\",\"metadata\":{{\"b\":\"2\"}}}}");
        assert_eq!(
            session.run(RunHookInput {
                sandbox_id: Some("mvm-2"),
                payload: Some(&other),
            }),
            RunOutcome::AlreadyRan
        );
        assert_eq!(session.metadata().get("a").map(String::as_str), Some("1"));
        assert!(!session.metadata().contains_key("b"));
    }
}
