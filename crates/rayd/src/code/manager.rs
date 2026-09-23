//! Application service behind `CodeService` and the kernel halves of the
//! hooks: contexts (explicit and the per-language defaults `Execute`
//! creates lazily under one lock per language), executions (recorded and
//! re-attachable), readiness, the `/run` rotation, the `/suspend` quiesce,
//! the `/resume` probe and reseed and the `/validate` cell, over the
//! supervisor and the `rayd-core` registries. Everything that needs tokio
//! lives here; every rule lives in `rayd_core::code`.

use std::collections::BTreeMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use rayd_core::clock::Clock;
use rayd_core::code::protocol::{KernelPidPayload, ReseedPayload};
use rayd_core::code::{
    CONTEXT_READY_TIMEOUT, CodeError, ContextEntry, ContextId, ContextInfo, ContextPlan,
    ContextRegistry, ContextState, CreateContextInput, EXECUTE_KEEPALIVE_INTERVAL, ExecuteTarget,
    ExecutionId, ExecutionLimits, ExecutionRegistry, ExecutionTracker, INTERRUPT_GRACE,
    KernelSidecar, KernelStatus, Language, MAX_CODE_BYTES, ProbeOutcome, RandomSource,
    SidecarConfig, SidecarOp, SidecarState, TimeoutSchedule, ValidationState, plan_context,
    plan_timeout, probe_outcome, restart_after_resume, sidecar_spawn_spec, validate_envs,
};
use rayd_core::process::identity::resolve_username;
use rayd_core::process::{OutputBudget, UserLookup, UserPolicy};
use rayd_core::session::SandboxSession;
use serde::Deserialize;
use tokio::sync::watch;
use tokio::task::JoinHandle;

use super::execute::{ExecutionSubscriberStream, InFlightGuard};
use super::executions::{
    ExecuteSink, ExecutionRecorder, InterruptHandle, RecorderParts, SharedExecutions,
};
use super::supervisor::{
    KernelSignaller, OpTimeouts, SidecarSupervisor, SupervisorSettings, lock, millis,
};
use super::validate::run_validation;
use crate::adapters::{PlatformSidecarLauncher, SpawnPlatform, signal_process_group};
use crate::lifecycle::Reaper;

#[derive(Debug, Clone)]
pub struct CodeSettings {
    pub execute_keepalive_interval: Duration,
    pub interrupt_grace: Duration,
    pub stall_timeout: Duration,
    pub queue_capacity: usize,
    pub context_ready_timeout: Duration,
    pub sidecar_ready_timeout: Duration,
    pub op_timeouts: OpTimeouts,
    pub executions: ExecutionLimits,
    pub sidecar: Option<SidecarConfig>,
    /// The sandbox-wide output budget the execution rings charge; `main`
    /// hands the same one to the process registry.
    pub output_budget: OutputBudget,
}

impl Default for CodeSettings {
    fn default() -> Self {
        let supervisor = SupervisorSettings::default();
        Self {
            execute_keepalive_interval: EXECUTE_KEEPALIVE_INTERVAL,
            interrupt_grace: INTERRUPT_GRACE,
            stall_timeout: supervisor.stall_timeout,
            queue_capacity: supervisor.queue_capacity,
            context_ready_timeout: CONTEXT_READY_TIMEOUT,
            sidecar_ready_timeout: supervisor.ready_timeout,
            op_timeouts: supervisor.op_timeouts,
            executions: ExecutionLimits::default(),
            sidecar: None,
            output_budget: OutputBudget::unlimited(),
        }
    }
}

impl CodeSettings {
    #[must_use]
    pub fn supervisor_settings(&self) -> SupervisorSettings {
        SupervisorSettings {
            stall_timeout: self.stall_timeout,
            queue_capacity: self.queue_capacity,
            ready_timeout: self.sidecar_ready_timeout,
            op_timeouts: self.op_timeouts,
            max_consecutive_timeouts: SupervisorSettings::default().max_consecutive_timeouts,
        }
    }
}

/// What `Execute` carries after proto conversion.
#[derive(Debug, Clone, Default)]
pub struct ExecuteInput {
    pub context_id: Option<String>,
    /// The per-language default context to run on (`None`/empty: the
    /// context named by `context_id`, else Python's `default`).
    pub language: Option<String>,
    pub code: String,
    pub timeout_ms: u64,
    pub envs: BTreeMap<String, String>,
}

type LanguageLocks = BTreeMap<Language, Arc<tokio::sync::Mutex<()>>>;

fn language_locks() -> LanguageLocks {
    Language::ALL
        .into_iter()
        .map(|language| (language, Arc::new(tokio::sync::Mutex::new(()))))
        .collect()
}

/// The sidecar's `resume` reply.
#[derive(Debug, Deserialize)]
struct ResumePayload {
    contexts: Vec<ResumedContext>,
}

#[derive(Debug, Deserialize)]
struct ResumedContext {
    context_id: String,
    alive: bool,
}

pub struct CodeManager {
    session: Arc<SandboxSession>,
    supervisor: Option<Arc<SidecarSupervisor>>,
    registry: Arc<Mutex<ContextRegistry>>,
    executions: SharedExecutions,
    random: Arc<dyn RandomSource>,
    settings: CodeSettings,
    clock: Arc<dyn Clock>,
    booted_at: Duration,
    home: String,
    validation: Mutex<ValidationState>,
    disabled_state: watch::Sender<SidecarState>,
    kernel_state_lost: AtomicBool,
    language_locks: LanguageLocks,
}

impl CodeManager {
    pub fn new(
        session: Arc<SandboxSession>,
        supervisor: Option<Arc<SidecarSupervisor>>,
        registry: Arc<Mutex<ContextRegistry>>,
        random: Arc<dyn RandomSource>,
        settings: CodeSettings,
        home: String,
    ) -> Arc<Self> {
        let clock = session.clock();
        let booted_at = clock.monotonic();
        let (disabled_state, _) = watch::channel(SidecarState::Disabled);
        let executions = Arc::new(Mutex::new(ExecutionRegistry::with_budget(
            settings.executions,
            settings.output_budget.clone(),
        )));
        Arc::new(Self {
            session,
            supervisor,
            registry,
            executions,
            random,
            settings,
            clock,
            booted_at,
            home,
            validation: Mutex::new(ValidationState::Idle),
            disabled_state,
            kernel_state_lost: AtomicBool::new(false),
            language_locks: language_locks(),
        })
    }

    /// `--no-sidecar` and the M1–M3 tests: no kernel at all, `/ready`
    /// immediate, `kernel_ready` false, `CodeService` answers
    /// `UNAVAILABLE`.
    pub fn disabled(session: Arc<SandboxSession>, random: Arc<dyn RandomSource>) -> Arc<Self> {
        Self::new(
            session,
            None,
            Arc::new(Mutex::new(ContextRegistry::default())),
            random,
            CodeSettings::default(),
            "/home/user".to_owned(),
        )
    }

    /// Starts the supervisor loop; `None` without a sidecar.
    pub fn spawn_supervisor(&self) -> Option<JoinHandle<()>> {
        self.supervisor.as_ref().map(SidecarSupervisor::spawn)
    }

    #[must_use]
    pub fn settings(&self) -> &CodeSettings {
        &self.settings
    }

    #[must_use]
    pub fn sidecar_state(&self) -> SidecarState {
        self.supervisor
            .as_ref()
            .map_or(SidecarState::Disabled, |supervisor| supervisor.state())
    }

    /// Times the sidecar exited and was relaunched since boot.
    #[must_use]
    pub fn sidecar_restarts(&self) -> u64 {
        self.supervisor
            .as_ref()
            .map_or(0, |supervisor| supervisor.restarts())
    }

    /// Whether the sidecar's stdout reader is parked on a recorder that
    /// cannot keep up right now.
    #[must_use]
    pub fn dispatch_blocked(&self) -> bool {
        self.supervisor
            .as_ref()
            .is_some_and(|supervisor| supervisor.dispatch_blocked())
    }

    #[must_use]
    pub fn readiness(&self) -> watch::Receiver<SidecarState> {
        self.supervisor.as_ref().map_or_else(
            || self.disabled_state.subscribe(),
            |supervisor| supervisor.watch_state(),
        )
    }

    #[must_use]
    pub fn boot_elapsed(&self) -> Duration {
        self.clock.monotonic().saturating_sub(self.booted_at)
    }

    #[must_use]
    pub fn validation_state(&self) -> ValidationState {
        lock(&self.validation).clone()
    }

    /// Live plus retained executions.
    #[must_use]
    pub fn execution_count(&self) -> usize {
        lock(&self.executions).len()
    }

    pub async fn create_context(
        &self,
        input: CreateContextInput,
    ) -> Result<ContextInfo, CodeError> {
        let supervisor = self.ready_supervisor()?;
        let defaults = self.session.spawn_defaults();
        let plan = plan_context(&input, &defaults, &self.home)?;
        supervisor.available_languages().require(plan.language)?;
        let context_id = ContextId::generate(self.random.as_ref())?;
        self.start_context(&supervisor, context_id, plan, false)
            .await
    }

    /// Registers the entry, asks the sidecar for the kernel and marks it
    /// ready; a refused start leaves no entry behind. `lazy` only changes
    /// the log line (a per-language default created by `Execute`).
    async fn start_context(
        &self,
        supervisor: &Arc<SidecarSupervisor>,
        context_id: ContextId,
        plan: ContextPlan,
        lazy: bool,
    ) -> Result<ContextInfo, CodeError> {
        let entry = ContextEntry::new(
            context_id.clone(),
            plan.language,
            plan.cwd.clone(),
            plan.envs.clone(),
        );
        let info = entry.info.clone();
        lock(&self.registry).register(entry)?;
        let reply = supervisor
            .call(SidecarOp::CreateContext {
                context_id: context_id.as_str().to_owned(),
                language: plan.language.as_str().to_owned(),
                cwd: plan.cwd,
                envs: plan.envs,
            })
            .await;
        match reply {
            Ok(payload) => {
                let pid: KernelPidPayload = payload.decode().map_err(CodeError::SidecarProtocol)?;
                let mut registry = lock(&self.registry);
                let _ = registry.set_kernel_pid(&context_id, pid.kernel_pid);
                let _ = registry.set_state(&context_id, ContextState::Ready);
                drop(registry);
                tracing::info!(
                    context_id = %context_id,
                    language = plan.language.as_str(),
                    lazy,
                    kernel_pid = pid.kernel_pid,
                    contexts = lock(&self.registry).len(),
                    "context created"
                );
                Ok(info)
            }
            Err(error) => {
                let _ = lock(&self.registry).remove(&context_id);
                tracing::warn!(
                    context_id = %context_id,
                    language = plan.language.as_str(),
                    lazy,
                    reason = %error,
                    "context creation failed"
                );
                Err(error)
            }
        }
    }

    /// The per-language default context, created on first use with the
    /// same `cwd`/`envs` the rotated Python default gets (the `/run`
    /// payload's). One lock per language makes two concurrent first cells
    /// start exactly one kernel: the second finds the entry after the first
    /// released the lock.
    async fn ensure_language_default(
        &self,
        supervisor: &Arc<SidecarSupervisor>,
        language: Language,
    ) -> Result<ContextId, CodeError> {
        let context_id = language.default_context_id();
        let guard = self
            .language_locks
            .get(&language)
            .cloned()
            .ok_or_else(|| CodeError::Internal("language lock missing".to_owned()))?;
        let _held = guard.lock().await;
        if lock(&self.registry).contains(&context_id) {
            return Ok(context_id);
        }
        supervisor.available_languages().require(language)?;
        let defaults = self.session.spawn_defaults();
        let plan = plan_context(
            &CreateContextInput {
                language: language.as_str().to_owned(),
                cwd: None,
                envs: defaults.envs.clone(),
            },
            &defaults,
            &self.home,
        )?;
        self.start_context(supervisor, context_id.clone(), plan, true)
            .await?;
        Ok(context_id)
    }

    /// Where the cell runs (design D2): the named context, Python's
    /// `default`, or the lazily created per-language default. Per-execution
    /// `envs` are refused for anything but Python before any kernel is
    /// touched or started (the set/restore cells are Python source).
    async fn resolve_execute_context(
        &self,
        supervisor: &Arc<SidecarSupervisor>,
        input: &ExecuteInput,
    ) -> Result<ContextId, CodeError> {
        match ExecuteTarget::resolve(input.context_id.as_deref(), input.language.as_deref())? {
            ExecuteTarget::Explicit(context_id) => Ok(context_id),
            ExecuteTarget::LanguageDefault(Language::Python) => Ok(ContextId::default_context()),
            ExecuteTarget::LanguageDefault(language) => {
                require_python_for_envs(language, &input.envs)?;
                supervisor.available_languages().require(language)?;
                self.ensure_language_default(supervisor, language).await
            }
        }
    }

    /// Phase gate, readiness, context, code size, envs; then the origin
    /// stream of a recorded execution.
    pub async fn execute(
        self: &Arc<Self>,
        input: ExecuteInput,
    ) -> Result<ExecutionSubscriberStream, CodeError> {
        self.session
            .stream_gate()
            .map_err(|phase| CodeError::NotAcceptingStreams { phase })?;
        self.execute_unchecked(input).await
    }

    /// `/validate` runs before `/run`, so it skips the phase gate.
    pub async fn execute_unchecked(
        self: &Arc<Self>,
        input: ExecuteInput,
    ) -> Result<ExecutionSubscriberStream, CodeError> {
        let supervisor = self.ready_supervisor()?;
        if input.code.len() > MAX_CODE_BYTES {
            return Err(CodeError::CodeTooLarge {
                max: MAX_CODE_BYTES,
            });
        }
        validate_envs(&input.envs)?;
        let context_id = self.resolve_execute_context(&supervisor, &input).await?;
        let (language, context_envs) = self.wait_for_context(&context_id).await?;
        require_python_for_envs(language, &input.envs)?;
        let execution_id = ExecutionId::generate(self.random.as_ref())?;
        let schedule = self.schedule(input.timeout_ms);
        let (sink, receiver) = ExecuteSink::open(self.settings.queue_capacity, true);
        {
            let mut executions = lock(&self.executions);
            executions.register(execution_id.clone(), context_id.clone());
            executions.attach(&context_id, &execution_id, 0, sink)?;
        }
        let handle = match supervisor.open_execution(SidecarOp::Execute {
            context_id: context_id.as_str().to_owned(),
            execution_id: execution_id.as_str().to_owned(),
            code: input.code,
            envs: input.envs,
        }) {
            Ok(handle) => handle,
            Err(error) => {
                lock(&self.executions).mark_ended(&execution_id, self.session.running_now());
                return Err(error);
            }
        };
        lock(&self.registry).begin_execution(&context_id)?;
        let guard = InFlightGuard::new(self.registry.clone(), context_id.clone());
        tracing::info!(
            context_id = %context_id,
            execution_id = %execution_id,
            timeout_ms = input.timeout_ms,
            "execution accepted"
        );
        ExecutionRecorder::new(RecorderParts {
            handle,
            tracker: ExecutionTracker::new(execution_id.clone()),
            schedule,
            supervisor: supervisor.clone(),
            session: self.session.clone(),
            executions: self.executions.clone(),
            context_id: context_id.clone(),
            envs: context_envs,
            guard,
        })
        .spawn();
        let interrupt = InterruptHandle::new(supervisor, context_id, execution_id);
        Ok(ExecutionSubscriberStream::new(
            Vec::new(),
            Some(receiver),
            Some(interrupt),
        ))
    }

    /// A second (or later) subscriber of a recorded execution: replay from
    /// `from_seq`, then live events; an ended execution replays through
    /// its `End` and closes.
    pub fn reattach(
        &self,
        context_id: Option<&str>,
        execution_id: &str,
        from_seq: u64,
    ) -> Result<ExecutionSubscriberStream, CodeError> {
        self.session
            .stream_gate()
            .map_err(|phase| CodeError::NotAcceptingStreams { phase })?;
        let context_id = ContextId::parse_or_default(context_id)?;
        let execution_id = ExecutionId::parse(execution_id)?;
        let (sink, receiver) = ExecuteSink::open(self.settings.queue_capacity, false);
        let attachment =
            lock(&self.executions).attach(&context_id, &execution_id, from_seq, sink)?;
        tracing::info!(
            context_id = %context_id,
            execution_id = %execution_id,
            from_seq,
            replayed = attachment.replay.len(),
            reattach = !attachment.ended,
            "execution reattached"
        );
        let live = attachment.subscriber.map(|_| receiver);
        Ok(ExecutionSubscriberStream::new(
            attachment.replay,
            live,
            None,
        ))
    }

    /// `/suspend`: no origin stream closed by the broadcast (or by the
    /// connection dying with the VM) may interrupt its cell afterwards.
    pub fn detach_for_suspend(&self) -> usize {
        let sinks = lock(&self.executions).live_subscribers();
        for sink in &sinks {
            sink.mark_detached();
        }
        sinks.len()
    }

    /// The agent is ending at the logical deadline (ADR-011): the sidecar
    /// is never relaunched again and `signal` reaches every kernel group
    /// and the sidecar's group. Returns how many kernel groups were
    /// signalled (zero without a sidecar).
    pub fn stop_for_exit(&self, signal: i32) -> usize {
        self.supervisor
            .as_ref()
            .map_or(0, |supervisor| supervisor.stop_for_exit(signal))
    }

    /// Drops ended executions whose retention window closed, then, while
    /// the shared output budget sits above its high-water mark, the oldest
    /// ended ones regardless of age.
    pub fn reap_expired(&self) -> Vec<ExecutionId> {
        let running_now = self.session.running_now();
        let mut executions = lock(&self.executions);
        let mut expired = executions.reap_expired(running_now);
        let dropped = executions.drop_ended_over_budget();
        let level = executions.budget().level();
        drop(executions);
        if !dropped.is_empty() {
            tracing::warn!(
                output_budget_bytes = level,
                entries_dropped = dropped.len(),
                "retained executions dropped for the output budget"
            );
        }
        if !expired.is_empty() {
            tracing::debug!(reaped = expired.len(), "retained executions expired");
        }
        expired.extend(dropped);
        expired
    }

    #[must_use]
    pub fn list_contexts(&self) -> Vec<ContextInfo> {
        lock(&self.registry).list()
    }

    pub async fn destroy_context(&self, raw_id: &str) -> Result<(), CodeError> {
        let supervisor = self.ready_supervisor()?;
        let context_id = ContextId::parse(raw_id)?;
        if context_id.is_default() {
            return Err(CodeError::DefaultContextProtected);
        }
        lock(&self.registry).get(&context_id)?;
        supervisor
            .call(SidecarOp::DestroyContext {
                context_id: context_id.as_str().to_owned(),
            })
            .await?;
        let _ = lock(&self.registry).remove(&context_id);
        tracing::info!(context_id = %context_id, contexts = lock(&self.registry).len(), "context destroyed");
        Ok(())
    }

    pub async fn restart_context(&self, raw_id: &str) -> Result<(), CodeError> {
        let supervisor = self.ready_supervisor()?;
        let context_id = ContextId::parse(raw_id)?;
        let envs = {
            let mut registry = lock(&self.registry);
            let envs = registry.get(&context_id)?.envs.clone();
            let _ = registry.set_state(&context_id, ContextState::Restarting);
            envs
        };
        let started = tokio::time::Instant::now();
        let reply = supervisor
            .call(SidecarOp::RestartContext {
                context_id: context_id.as_str().to_owned(),
                envs,
            })
            .await;
        let mut registry = lock(&self.registry);
        let _ = registry.set_state(&context_id, ContextState::Ready);
        match reply {
            Ok(payload) => {
                let pid: KernelPidPayload = payload.decode().map_err(CodeError::SidecarProtocol)?;
                let _ = registry.set_kernel_pid(&context_id, pid.kernel_pid);
                drop(registry);
                tracing::info!(
                    context_id = %context_id,
                    restart_ms = millis(started.elapsed()),
                    kernel_pid = pid.kernel_pid,
                    "context restarted"
                );
                Ok(())
            }
            Err(error) => {
                drop(registry);
                tracing::warn!(context_id = %context_id, reason = %error, "context restart failed");
                Err(error)
            }
        }
    }

    /// `/run` accepted: the pre-warmed default kernel gets a fresh
    /// connection file, fresh seeds and the payload's environment.
    pub fn spawn_run_rotation(&self, envs: BTreeMap<String, String>) {
        if let Some(supervisor) = &self.supervisor {
            supervisor.request_rotation(envs);
        }
    }

    /// `/suspend`: best-effort `quiesce` so the sidecar flushes what it
    /// can before the checkpoint; never waits past `timeout`.
    pub async fn quiesce(&self, timeout: Duration) {
        let Some(supervisor) = &self.supervisor else {
            return;
        };
        match tokio::time::timeout(timeout, supervisor.call(SidecarOp::Quiesce)).await {
            Ok(Ok(_)) => tracing::info!("sidecar quiesced"),
            Ok(Err(error)) => tracing::warn!(reason = %error, "quiesce failed"),
            Err(_) => tracing::warn!(timeout_ms = millis(timeout), "quiesce timed out"),
        }
    }

    /// `/resume`: the sidecar probes every kernel (`kernel_info`, 5 s each,
    /// ≤ 8 concurrent) inside `budget`; kernels that did not answer are
    /// restarted in the background and `kernel_state_lost` is latched for
    /// `Health` until the next probe finds every kernel alive. A probe that
    /// times out or fails reports the state as lost (it is unknown).
    pub async fn probe_after_resume(self: &Arc<Self>, budget: Duration) -> ProbeOutcome {
        let Some(supervisor) = self.supervisor.clone() else {
            self.kernel_state_lost.store(false, Ordering::SeqCst);
            return ProbeOutcome::default();
        };
        let outcome = match tokio::time::timeout(budget, supervisor.call(SidecarOp::Resume)).await {
            Ok(Ok(payload)) => match payload.decode::<ResumePayload>() {
                Ok(reply) => Some(probe_outcome(&parse_contexts(&reply))),
                Err(error) => {
                    tracing::warn!(reason = %error, "resume reply malformed");
                    None
                }
            },
            Ok(Err(error)) => {
                tracing::warn!(reason = %error, "resume probe failed");
                None
            }
            Err(_) => {
                tracing::warn!(
                    budget_ms = millis(budget),
                    outcome = "probe_timeout",
                    "resume probe timed out"
                );
                None
            }
        };
        let Some(outcome) = outcome else {
            self.kernel_state_lost.store(true, Ordering::SeqCst);
            return ProbeOutcome::default();
        };
        self.kernel_state_lost
            .store(outcome.kernel_state_lost(), Ordering::SeqCst);
        if !outcome.lost.is_empty() {
            self.spawn_restarts_after_resume(&outcome.lost);
        }
        outcome
    }

    fn spawn_restarts_after_resume(self: &Arc<Self>, lost: &[ContextId]) {
        let ops = restart_after_resume(lost, &lock(&self.registry));
        for op in ops {
            let SidecarOp::RestartContext { context_id, .. } = &op else {
                continue;
            };
            let context_id = context_id.clone();
            let manager = self.clone();
            tokio::spawn(async move {
                if let Ok(id) = ContextId::parse(&context_id) {
                    let _ = lock(&manager.registry).set_state(&id, ContextState::Restarting);
                }
                let Some(supervisor) = manager.supervisor.clone() else {
                    return;
                };
                let result = supervisor.call(op).await;
                let mut registry = lock(&manager.registry);
                if let Ok(id) = ContextId::parse(&context_id) {
                    let _ = registry.set_state(&id, ContextState::Ready);
                    if let Ok(payload) = &result
                        && let Ok(pid) = payload.decode::<KernelPidPayload>()
                    {
                        let _ = registry.set_kernel_pid(&id, pid.kernel_pid);
                    }
                }
                drop(registry);
                match result {
                    Ok(_) => tracing::warn!(context_id, "kernel_restarted_after_resume"),
                    Err(error) => {
                        tracing::error!(context_id, reason = %error, "kernel restart after resume failed");
                    }
                }
            });
        }
    }

    /// `/resume`: reseed `random`/`numpy.random` in every live kernel. The
    /// sidecar answers at once (idle kernels inline, busy ones deferred
    /// behind their running cell); the op is advisory, so a reply that
    /// never comes is logged and never counts towards the kill switch.
    pub fn spawn_resume_reseed(self: &Arc<Self>) {
        let Some(supervisor) = self.supervisor.clone() else {
            return;
        };
        tokio::spawn(async move {
            match supervisor.call(SidecarOp::Reseed).await {
                Ok(payload) => match payload.decode::<ReseedPayload>() {
                    Ok(reseeded) => tracing::info!(
                        reseeded = reseeded.reseeded.len(),
                        deferred = reseeded.deferred.len(),
                        failed = reseeded.failed.len(),
                        skipped = reseeded.skipped.len(),
                        "kernels reseeded"
                    ),
                    Err(error) => tracing::warn!(reason = %error, "reseed reply malformed"),
                },
                Err(error) => tracing::warn!(reason = %error, "reseed failed"),
            }
        });
    }

    /// First `/validate`: run the cell once in the background; later calls
    /// only read the state.
    pub fn start_validation(self: &Arc<Self>) -> bool {
        {
            let mut state = lock(&self.validation);
            if *state != ValidationState::Idle {
                return false;
            }
            *state = ValidationState::Running;
        }
        let manager = self.clone();
        tokio::spawn(async move {
            let outcome = run_validation(&manager).await;
            tracing::info!(outcome = outcome.as_str(), "validate cell finished");
            *lock(&manager.validation) = ValidationState::Done(outcome);
        });
        true
    }

    fn ready_supervisor(&self) -> Result<Arc<SidecarSupervisor>, CodeError> {
        let Some(supervisor) = &self.supervisor else {
            return Err(CodeError::KernelNotReady {
                reason: "no sidecar configured".to_owned(),
            });
        };
        match supervisor.state() {
            SidecarState::Ready | SidecarState::Rotating => Ok(supervisor.clone()),
            other => Err(CodeError::KernelNotReady {
                reason: other.as_str().to_owned(),
            }),
        }
    }

    /// A `Starting`/`Restarting` context is awaited up to the context
    /// timeout; the language decides the per-execution `envs` rule and the
    /// envs come back for the restart branch of the timeout.
    async fn wait_for_context(
        &self,
        context_id: &ContextId,
    ) -> Result<(Language, BTreeMap<String, String>), CodeError> {
        let deadline = tokio::time::Instant::now() + self.settings.context_ready_timeout;
        loop {
            let (state, language, envs) = {
                let registry = lock(&self.registry);
                let entry = registry.get(context_id)?;
                (entry.state, entry.info.language, entry.envs.clone())
            };
            match state {
                ContextState::Ready => return Ok((language, envs)),
                ContextState::Dead => return Err(CodeError::ContextNotFound),
                ContextState::Starting | ContextState::Restarting => {}
            }
            if tokio::time::Instant::now() >= deadline {
                return Err(CodeError::KernelNotReady {
                    reason: "context is restarting".to_owned(),
                });
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
    }

    fn schedule(&self, timeout_ms: u64) -> Option<TimeoutSchedule> {
        plan_timeout(timeout_ms).map(|schedule| TimeoutSchedule {
            restart_at: schedule.interrupt_at + self.settings.interrupt_grace,
            ..schedule
        })
    }
}

fn require_python_for_envs(
    language: Language,
    envs: &BTreeMap<String, String>,
) -> Result<(), CodeError> {
    if envs.is_empty() || language.is_python() {
        Ok(())
    } else {
        Err(CodeError::EnvsPythonOnly)
    }
}

fn parse_contexts(reply: &ResumePayload) -> Vec<(ContextId, bool)> {
    reply
        .contexts
        .iter()
        .filter_map(|context| {
            ContextId::parse(&context.context_id)
                .ok()
                .map(|id| (id, context.alive))
        })
        .collect()
}

impl KernelStatus for CodeManager {
    fn kernel_ready(&self) -> bool {
        self.sidecar_state().kernel_ready()
    }

    fn kernel_state_lost(&self) -> bool {
        self.kernel_state_lost.load(Ordering::SeqCst)
    }
}

impl Reaper for CodeManager {
    fn reap_expired(&self) {
        CodeManager::reap_expired(self);
    }
}

/// Builds the manager for the host `rayd` runs on: the sidecar identity is
/// the sandbox's default user through the same lookup and policy as
/// processes, the launcher is the platform's, kernels are signalled by
/// process group.
pub fn platform_code_manager(
    session: Arc<SandboxSession>,
    platform: &SpawnPlatform,
    policy: UserPolicy,
    settings: CodeSettings,
) -> Result<Arc<CodeManager>, CodeError> {
    let random: Arc<dyn RandomSource> = Arc::new(crate::adapters::OsRandomSource);
    let Some(config) = settings.sidecar.clone() else {
        return Ok(CodeManager::disabled(session, random));
    };
    let identity = sidecar_identity(&session, platform.lookup.as_ref(), policy)?;
    let spec = sidecar_spawn_spec(&identity, &config);
    let launcher: Arc<dyn KernelSidecar> =
        Arc::new(PlatformSidecarLauncher::new(platform.identity_switch));
    let registry = Arc::new(Mutex::new(ContextRegistry::default()));
    let signaller: KernelSignaller = Arc::new(signal_process_group);
    let supervisor = SidecarSupervisor::new(
        launcher,
        spec,
        session.clone(),
        registry.clone(),
        settings.supervisor_settings(),
        signaller,
    );
    Ok(CodeManager::new(
        session,
        Some(supervisor),
        registry,
        random,
        settings,
        identity.home,
    ))
}

/// The sidecar identity is resolved at boot, before `/run`: the payload's
/// `user` cannot apply yet, so it is the image default (`user`).
pub fn sidecar_identity(
    session: &SandboxSession,
    lookup: &dyn UserLookup,
    policy: UserPolicy,
) -> Result<rayd_core::process::ProcessIdentity, CodeError> {
    let defaults = session.spawn_defaults();
    let username = resolve_username(None, defaults.user.as_deref());
    policy
        .authorize(&username)
        .map_err(|error| CodeError::Internal(error.to_string()))?;
    let identity = lookup
        .lookup(&username)
        .map_err(|error| CodeError::Internal(error.to_string()))?;
    policy
        .authorize_identity(&identity)
        .map_err(|error| CodeError::Internal(error.to_string()))?;
    Ok(identity)
}
