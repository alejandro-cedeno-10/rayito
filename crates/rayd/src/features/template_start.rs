//! `template_start` (m15-templates, ADR-022, investigación §3.5): the only
//! one of the six 0.6 feature slots with no `ConfigureSandbox` section
//! (reading `/etc/rayito/template.json` happens once, at boot, not through
//! `ConfigureService`) and no `ConfigureSandbox` wiring at all —
//! `ConfigureGrpc` never dispatches to it (`configure.proto`'s
//! `ConfigureRequest` has no `template_start` field), so `apply`/`status`
//! stay the trivial `Unsupported` answers `ConfigurableFeature` already
//! gives every stub slot. What is real here is `supported()` (`true`: this
//! build understands the spec, independent of whether any given sandbox
//! has one) and `participant()`, which is `Some` only when a spec was
//! found — so an image with no `template.json` gets no participant at all,
//! and `/suspend`/`/ready` behave exactly as in 0.5.x (the rayd test for
//! that is `no_template_json_yields_no_participant` below).
//!
//! The actual work — spawning `start_cmd` as a managed process and polling
//! `ready_cmd` — happens in `TemplateParticipant::on_run`, not here:
//! `ProcessManager::start` requires the session to already accept new
//! streams (`SandboxSession::accepts_new_streams`), which is only true
//! after the `/run` hook has installed the sandbox's defaults. `build()`
//! only ever does the synchronous, side-effect-free part (reading the
//! file); `hooks::run` is what calls `on_run` once `/run` succeeds.

use std::sync::Arc;
use std::sync::atomic::{AtomicI32, Ordering};
use std::time::{Duration, Instant};

use rayd_core::process::identity::DEFAULT_USERNAME;
use rayd_core::process::{ProcessConfigInfo, SpawnInput};
use rayd_core::suspend_sync::{ParticipantDemand, ParticipantReport};
use rayd_core::template::{ReadyPoll, StartSpec, TemplateReadyDecision, ready_decision};

use super::FeatureContext;
use super::slot::{ConfigurableFeature, Unsupported};
use crate::adapters::{FsTemplateSpecSource, ReadyProbe, ShellReadyProbe, TemplateSpecSource};
use crate::grpc::PlatformProcessManager;
use crate::lifecycle::{LifecycleParticipant, ReadyVerdict};

/// `DEFAULT_TEMPLATE_USER` of `_instructions.py`/`instructions.ts`: the
/// uid every SDK bakes by default, aliased in every Rayito image to
/// `rayd_core::process::identity::DEFAULT_USERNAME`. Any other value is
/// passed to `UserLookup` as-is (a real account name in the image), since
/// 0.6 has no port to resolve an arbitrary numeric uid to a name.
const DEFAULT_TEMPLATE_USER_UID: &str = "1000";

/// A single `ready_cmd` invocation must not itself hang past this,
/// independent of `ReadyPoll.timeout_seconds` — the same bounded-subprocess
/// shape as `adapters::ip_command::IP_COMMAND_TIMEOUT`.
const READY_PROBE_RUN_TIMEOUT: Duration = Duration::from_secs(5);

/// Poll for a `ready_cmd` whose `ready_poll` is absent: what both SDKs
/// bake for a raw-string `ready_cmd` (`set_start_cmd("x", "test -e f")`).
/// Same values as `DEFAULT_READY_POLL_INTERVAL_SECONDS`/
/// `DEFAULT_READY_TIMEOUT_SECONDS` of `_ready_cmds.py`/`ready-cmds.ts`.
const FALLBACK_READY_POLL: ReadyPoll = ReadyPoll {
    interval_seconds: 0.5,
    timeout_seconds: 60.0,
};

/// Sentinel for "no probe has returned yet", outside the `u8` range of a
/// real POSIX exit code (and negative, `wait`'s own range for "killed by a
/// signal").
const NOT_PROBED_YET: i32 = i32::MIN;

/// Tags the managed `start_cmd` process so it is identifiable in
/// `commands.list` and in logs.
const START_CMD_TAG: &str = "template_start";

#[must_use]
pub fn build(ctx: &FeatureContext) -> Arc<dyn ConfigurableFeature<(), ()>> {
    let spec = FsTemplateSpecSource::default().read();
    Arc::new(TemplateStartFeature::new(
        spec,
        ctx.processes.clone(),
        Arc::new(ShellReadyProbe),
    ))
}

pub struct TemplateStartFeature {
    participant: Option<Arc<TemplateParticipant>>,
}

impl TemplateStartFeature {
    #[must_use]
    pub fn new(
        spec: Option<StartSpec>,
        processes: Option<Arc<PlatformProcessManager>>,
        probe: Arc<dyn ReadyProbe>,
    ) -> Self {
        let participant =
            spec.map(|spec| Arc::new(TemplateParticipant::new(spec, processes, probe)));
        Self { participant }
    }
}

#[tonic::async_trait]
impl ConfigurableFeature<(), ()> for TemplateStartFeature {
    fn supported(&self) -> bool {
        true
    }

    async fn apply(&self, cfg: ()) -> rayd_core::configure::SectionOutcome {
        <Unsupported as ConfigurableFeature<(), ()>>::apply(&Unsupported, cfg).await
    }

    async fn status(&self) {
        <Unsupported as ConfigurableFeature<(), ()>>::status(&Unsupported).await;
    }

    fn participant(&self) -> Option<Arc<dyn LifecycleParticipant>> {
        self.participant
            .clone()
            .map(|participant| participant as Arc<dyn LifecycleParticipant>)
    }
}

/// Shared, mutable half of a `TemplateParticipant`: only the ready-probe
/// loop (`on_run`) ever writes `last_exit`, `ready_gate` only ever reads it,
/// and `deadline` is computed once, before the first probe, so both sides
/// agree on when the plazo started (investigación §3.5: "el plazo... desde
/// el primer sondeo").
struct ReadyState {
    ready_cmd: String,
    poll: ReadyPoll,
    last_exit: AtomicI32,
    deadline: std::sync::OnceLock<Instant>,
}

impl ReadyState {
    fn decision(&self) -> TemplateReadyDecision {
        let last_exit = self.last_exit.load(Ordering::SeqCst);
        let last_exit_code = (last_exit != NOT_PROBED_YET).then_some(last_exit);
        let deadline_exceeded = self
            .deadline
            .get()
            .is_some_and(|deadline| Instant::now() >= *deadline);
        ready_decision(Some(&self.ready_cmd), last_exit_code, deadline_exceeded)
    }
}

pub struct TemplateParticipant {
    spec: StartSpec,
    processes: Option<Arc<PlatformProcessManager>>,
    probe: Arc<dyn ReadyProbe>,
    ready: Option<Arc<ReadyState>>,
}

impl TemplateParticipant {
    fn new(
        spec: StartSpec,
        processes: Option<Arc<PlatformProcessManager>>,
        probe: Arc<dyn ReadyProbe>,
    ) -> Self {
        let ready = spec.ready_cmd.clone().map(|ready_cmd| {
            Arc::new(ReadyState {
                ready_cmd,
                poll: spec.ready_poll.unwrap_or(FALLBACK_READY_POLL),
                last_exit: AtomicI32::new(NOT_PROBED_YET),
                deadline: std::sync::OnceLock::new(),
            })
        });
        Self {
            spec,
            processes,
            probe,
            ready,
        }
    }

    fn spawn_input(&self) -> SpawnInput {
        SpawnInput {
            config: ProcessConfigInfo {
                cmd: "/bin/sh".to_owned(),
                args: vec!["-c".to_owned(), self.spec.start_cmd.clone()],
                envs: self.spec.envs.clone(),
                cwd: self.spec.workdir.clone(),
            },
            user: Some(resolve_spec_user(&self.spec.user)),
            tag: Some(START_CMD_TAG.to_owned()),
            ..SpawnInput::default()
        }
    }

    async fn start_the_managed_process(&self) {
        let Some(processes) = self.processes.clone() else {
            return;
        };
        if self.spec.start_cmd.is_empty() {
            return;
        }
        match processes.start(self.spawn_input()).await {
            Ok((pid, _stream)) => {
                tracing::info!(pid = pid.0, "template start_cmd spawned");
            }
            Err(error) => {
                tracing::warn!(error = %error, "template start_cmd could not be spawned");
            }
        }
    }

    fn spawn_ready_probe_loop(&self) {
        let Some(ready) = self.ready.clone() else {
            return;
        };
        let probe = self.probe.clone();
        let _ = ready.deadline.set(Instant::now() + ready.poll.timeout());
        tokio::spawn(async move {
            loop {
                let code = probe.probe(&ready.ready_cmd, READY_PROBE_RUN_TIMEOUT).await;
                ready
                    .last_exit
                    .store(code.unwrap_or(NOT_PROBED_YET), Ordering::SeqCst);
                if code == Some(0) {
                    return;
                }
                if ready.decision() == TemplateReadyDecision::Fail {
                    return;
                }
                tokio::time::sleep(ready.poll.interval()).await;
            }
        });
    }
}

#[tonic::async_trait]
impl LifecycleParticipant for TemplateParticipant {
    fn demand(&self) -> ParticipantDemand {
        // No `/suspend` flush of its own: the managed `start_cmd` process
        // is already synced like any other process, and the ready probe
        // loop only matters before the first `/ready`.
        ParticipantDemand {
            name: "template_start",
            max: Duration::ZERO,
        }
    }

    async fn on_run(&self) {
        self.start_the_managed_process().await;
        self.spawn_ready_probe_loop();
    }

    async fn on_suspend(&self, _share: Duration) -> ParticipantReport {
        ParticipantReport {
            completed: true,
            timed_out: false,
        }
    }

    fn ready_gate(&self) -> ReadyVerdict {
        let Some(ready) = &self.ready else {
            return ReadyVerdict::Ok;
        };
        match ready.decision() {
            TemplateReadyDecision::Ok => ReadyVerdict::Ok,
            TemplateReadyDecision::Retry => ReadyVerdict::Retry,
            TemplateReadyDecision::Fail => ReadyVerdict::Fail,
        }
    }
}

/// `"1000"` (every SDK's default) maps to the image's own default account;
/// anything else is passed through verbatim.
fn resolve_spec_user(user: &str) -> String {
    if user == DEFAULT_TEMPLATE_USER_UID {
        DEFAULT_USERNAME.to_owned()
    } else {
        user.to_owned()
    }
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;
    use std::sync::Mutex;

    use rayd_core::template::TEMPLATE_SPEC_VERSION;

    use super::*;

    fn spec(start_cmd: &str, ready_cmd: Option<&str>, poll: Option<ReadyPoll>) -> StartSpec {
        StartSpec {
            version: TEMPLATE_SPEC_VERSION.to_owned(),
            start_cmd: start_cmd.to_owned(),
            ready_cmd: ready_cmd.map(str::to_owned),
            user: DEFAULT_TEMPLATE_USER_UID.to_owned(),
            workdir: None,
            envs: BTreeMap::new(),
            ready_poll: poll,
        }
    }

    struct ScriptedProbe {
        codes: Mutex<Vec<Option<i32>>>,
    }

    impl ScriptedProbe {
        fn new(codes: Vec<Option<i32>>) -> Arc<Self> {
            Arc::new(Self {
                codes: Mutex::new(codes),
            })
        }
    }

    #[tonic::async_trait]
    impl ReadyProbe for ScriptedProbe {
        async fn probe(&self, _cmd: &str, _budget: Duration) -> Option<i32> {
            let mut codes = self.codes.lock().unwrap();
            if codes.is_empty() {
                Some(1)
            } else {
                codes.remove(0)
            }
        }
    }

    #[test]
    fn no_template_json_yields_no_participant() {
        let feature = TemplateStartFeature::new(None, None, Arc::new(ShellReadyProbe));
        // The agent build still understands the spec (`AgentFeatures.template_start`),
        // but with nothing to gate, hooks::mod's participant list simply
        // never gains an entry: `/suspend` and `/ready` behave exactly as
        // in 0.5.x.
        assert!(feature.supported());
        assert!(feature.participant().is_none());
    }

    #[test]
    fn a_spec_with_no_ready_cmd_always_passes_the_gate() {
        let participant = TemplateParticipant::new(
            spec("python app.py", None, None),
            None,
            Arc::new(ShellReadyProbe),
        );
        assert_eq!(participant.ready_gate(), ReadyVerdict::Ok);
    }

    #[tokio::test]
    async fn the_gate_retries_then_passes_once_the_probe_succeeds() {
        let probe = ScriptedProbe::new(vec![Some(1), Some(0)]);
        let participant = TemplateParticipant::new(
            spec(
                "python app.py",
                Some("test -e /tmp/ready"),
                Some(ReadyPoll {
                    interval_seconds: 0.01,
                    timeout_seconds: 5.0,
                }),
            ),
            None,
            probe,
        );
        // Nothing probed yet: retry, not a definitive failure.
        assert_eq!(participant.ready_gate(), ReadyVerdict::Retry);
        participant.spawn_ready_probe_loop();
        wait_until(Duration::from_secs(1), || {
            participant.ready_gate() == ReadyVerdict::Ok
        })
        .await;
    }

    #[tokio::test]
    async fn the_gate_fails_once_the_deadline_is_exceeded() {
        let probe = ScriptedProbe::new(vec![]);
        let participant = TemplateParticipant::new(
            spec(
                "python app.py",
                Some("false"),
                Some(ReadyPoll {
                    interval_seconds: 0.01,
                    timeout_seconds: 0.05,
                }),
            ),
            None,
            probe,
        );
        participant.spawn_ready_probe_loop();
        wait_until(Duration::from_secs(1), || {
            participant.ready_gate() == ReadyVerdict::Fail
        })
        .await;
    }

    #[test]
    fn the_default_template_uid_maps_to_the_images_default_account() {
        assert_eq!(resolve_spec_user("1000"), DEFAULT_USERNAME);
        assert_eq!(resolve_spec_user("alice"), "alice");
    }

    async fn wait_until(budget: Duration, mut condition: impl FnMut() -> bool) {
        let deadline = Instant::now() + budget;
        loop {
            if condition() {
                return;
            }
            assert!(Instant::now() < deadline, "condition never became true");
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    }
}
