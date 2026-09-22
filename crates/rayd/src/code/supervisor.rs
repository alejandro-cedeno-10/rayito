//! Owns one sidecar process at a time behind the `KernelSidecar` port
//! (design D8): launches it, waits for `ready`, routes replies to their
//! callers and execution events to their recorders with real backpressure,
//! rotates the default kernel after `/run`, and on exit fails everything
//! in flight, kills orphaned kernels, clears the registry and relaunches
//! after the backoff. Op timeouts stay on the monotonic clock; one that
//! expires across a `/resume` is logged and not counted towards the kill
//! switch (design D6), and neither is an advisory op (`reseed`, queued
//! behind whatever cell is running at `/resume`).

use std::collections::{BTreeMap, HashMap};
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::Duration;

use rayd_core::clock::Clock;
use rayd_core::code::protocol::KernelPidPayload;
use rayd_core::code::{
    AvailableLanguages, CodeError, ContextEntry, ContextId, ContextRegistry, ContextState,
    EXECUTE_QUEUE_CAPACITY, KernelSidecar, Language, ReplyError, ReplyPayload,
    SIDECAR_PROTOCOL_VERSION, SIDECAR_READY_TIMEOUT, STALL_TIMEOUT, SidecarErrorCode, SidecarEvent,
    SidecarEventSink, SidecarExitSink, SidecarLink, SidecarOp, SidecarRequest, SidecarState,
    SyntheticError, encode_request, run_rotation_request,
};
use rayd_core::process::SpawnSpec;
use rayd_core::session::SandboxSession;
use tokio::sync::mpsc::error::{SendTimeoutError, TrySendError};
use tokio::sync::{mpsc, oneshot, watch};
use tokio::task::JoinHandle;

/// Reply deadlines per op (design D8).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct OpTimeouts {
    pub ping: Duration,
    pub create_context: Duration,
    pub restart_context: Duration,
    pub destroy_context: Duration,
    pub interrupt: Duration,
    pub list_contexts: Duration,
    pub reseed: Duration,
    pub quiesce: Duration,
    pub resume: Duration,
}

impl Default for OpTimeouts {
    fn default() -> Self {
        Self {
            ping: Duration::from_secs(5),
            create_context: Duration::from_secs(60),
            restart_context: Duration::from_secs(60),
            destroy_context: Duration::from_secs(15),
            interrupt: Duration::from_secs(5),
            list_contexts: Duration::from_secs(5),
            reseed: Duration::from_secs(15),
            quiesce: Duration::from_secs(5),
            resume: Duration::from_secs(15),
        }
    }
}

impl OpTimeouts {
    #[must_use]
    pub fn for_op(&self, op: &SidecarOp) -> Duration {
        match op {
            SidecarOp::Ping => self.ping,
            SidecarOp::CreateContext { .. } | SidecarOp::Execute { .. } => self.create_context,
            SidecarOp::RestartContext { .. } => self.restart_context,
            SidecarOp::DestroyContext { .. } => self.destroy_context,
            SidecarOp::Interrupt { .. } => self.interrupt,
            SidecarOp::ListContexts => self.list_contexts,
            SidecarOp::Reseed => self.reseed,
            SidecarOp::Quiesce => self.quiesce,
            SidecarOp::Resume => self.resume,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SupervisorSettings {
    pub stall_timeout: Duration,
    pub queue_capacity: usize,
    pub ready_timeout: Duration,
    pub op_timeouts: OpTimeouts,
    /// Op timeouts in a row before the sidecar is killed.
    pub max_consecutive_timeouts: u32,
}

impl Default for SupervisorSettings {
    fn default() -> Self {
        Self {
            stall_timeout: STALL_TIMEOUT,
            queue_capacity: EXECUTE_QUEUE_CAPACITY,
            ready_timeout: SIDECAR_READY_TIMEOUT,
            op_timeouts: OpTimeouts::default(),
            max_consecutive_timeouts: 3,
        }
    }
}

/// Kills an orphaned kernel by pid (its own process group); injected so
/// the supervisor compiles on any host.
pub type KernelKiller = Arc<dyn Fn(u32) + Send + Sync>;

type PendingReply = oneshot::Sender<Result<ReplyPayload, CodeError>>;

#[derive(Clone)]
struct ExecutionChannel {
    sender: mpsc::Sender<SidecarEvent>,
    stalled: Arc<AtomicBool>,
}

/// The receiving side of one execution's events plus the stall flag the
/// dispatcher raises when it gives up on the client.
pub struct ExecutionHandle {
    pub request_id: u64,
    pub receiver: mpsc::Receiver<SidecarEvent>,
    pub stalled: Arc<AtomicBool>,
}

enum Control {
    Rotate(BTreeMap<String, String>),
}

pub struct SidecarSupervisor {
    launcher: Arc<dyn KernelSidecar>,
    spec: SpawnSpec,
    session: Arc<SandboxSession>,
    clock: Arc<dyn Clock>,
    settings: SupervisorSettings,
    state: watch::Sender<SidecarState>,
    link: Mutex<Option<Arc<dyn SidecarLink>>>,
    generation: AtomicU64,
    next_id: AtomicU64,
    pending: Mutex<HashMap<u64, PendingReply>>,
    executions: Mutex<HashMap<u64, ExecutionChannel>>,
    registry: Arc<Mutex<ContextRegistry>>,
    languages: Mutex<AvailableLanguages>,
    ready_signal: Mutex<Option<oneshot::Sender<SidecarEvent>>>,
    control: mpsc::UnboundedSender<Control>,
    control_receiver: Mutex<Option<mpsc::UnboundedReceiver<Control>>>,
    rotation_envs: Mutex<Option<BTreeMap<String, String>>>,
    consecutive_timeouts: AtomicU32,
    dispatch_blocked: AtomicBool,
    restarts: AtomicU64,
    dropped_events: AtomicU64,
    kernel_killer: KernelKiller,
}

impl SidecarSupervisor {
    pub fn new(
        launcher: Arc<dyn KernelSidecar>,
        spec: SpawnSpec,
        session: Arc<SandboxSession>,
        registry: Arc<Mutex<ContextRegistry>>,
        settings: SupervisorSettings,
        kernel_killer: KernelKiller,
    ) -> Arc<Self> {
        let clock = session.clock();
        let (state, _) = watch::channel(SidecarState::Starting {
            attempt: 1,
            since: clock.monotonic(),
        });
        let (control, control_receiver) = mpsc::unbounded_channel();
        Arc::new(Self {
            launcher,
            spec,
            session,
            clock,
            settings,
            state,
            link: Mutex::new(None),
            generation: AtomicU64::new(0),
            next_id: AtomicU64::new(0),
            pending: Mutex::new(HashMap::new()),
            executions: Mutex::new(HashMap::new()),
            registry,
            languages: Mutex::new(AvailableLanguages::default()),
            ready_signal: Mutex::new(None),
            control,
            control_receiver: Mutex::new(Some(control_receiver)),
            rotation_envs: Mutex::new(None),
            consecutive_timeouts: AtomicU32::new(0),
            dispatch_blocked: AtomicBool::new(false),
            restarts: AtomicU64::new(0),
            dropped_events: AtomicU64::new(0),
            kernel_killer,
        })
    }

    #[must_use]
    pub fn state(&self) -> SidecarState {
        *self.state.borrow()
    }

    #[must_use]
    pub fn watch_state(&self) -> watch::Receiver<SidecarState> {
        self.state.subscribe()
    }

    #[must_use]
    pub fn settings(&self) -> SupervisorSettings {
        self.settings
    }

    #[must_use]
    pub fn restarts(&self) -> u64 {
        self.restarts.load(Ordering::Relaxed)
    }

    /// What the serving sidecar announced in `ready.languages` (Python only
    /// for an older sidecar or before the first `ready`).
    #[must_use]
    pub fn available_languages(&self) -> AvailableLanguages {
        lock(&self.languages).clone()
    }

    /// Whether the stdout reader is parked on a full execution queue right
    /// now; replies cannot arrive while it is.
    #[must_use]
    pub fn dispatch_blocked(&self) -> bool {
        self.dispatch_blocked.load(Ordering::Relaxed)
    }

    /// Starts the launch/serve/relaunch loop; called once by the owner of
    /// the runtime.
    pub fn spawn(self: &Arc<Self>) -> JoinHandle<()> {
        let receiver = lock(&self.control_receiver).take();
        let supervisor = self.clone();
        tokio::spawn(async move {
            if let Some(receiver) = receiver {
                supervisor.run_loop(receiver).await;
            }
        })
    }

    /// `/run` happened: rotate the default kernel now if the sidecar is
    /// serving, or right after the next `ready` otherwise.
    pub fn request_rotation(&self, envs: BTreeMap<String, String>) {
        let mut slot = lock(&self.rotation_envs);
        *slot = Some(envs.clone());
        if matches!(self.state(), SidecarState::Ready | SidecarState::Rotating) {
            let _ = self.control.send(Control::Rotate(envs));
        }
        drop(slot);
    }

    /// A request with a reply, bounded by the op's deadline.
    pub async fn call(&self, op: SidecarOp) -> Result<ReplyPayload, CodeError> {
        let timeout = self.settings.op_timeouts.for_op(&op);
        let advisory = op.is_advisory();
        let name = op.name();
        let link = self.serving_link()?;
        let issued_generation = self.session.resume_generation();
        let id = self.next_id.fetch_add(1, Ordering::Relaxed) + 1;
        let (sender, receiver) = oneshot::channel();
        lock(&self.pending).insert(id, sender);
        let encoded = encode_request(&SidecarRequest { id, op });
        if link.send(&encoded).is_err() {
            lock(&self.pending).remove(&id);
            return Err(CodeError::SidecarUnavailable);
        }
        match tokio::time::timeout(timeout, receiver).await {
            Ok(Ok(result)) => {
                self.consecutive_timeouts.store(0, Ordering::Relaxed);
                result
            }
            Ok(Err(_)) => Err(CodeError::SidecarUnavailable),
            Err(_) => {
                lock(&self.pending).remove(&id);
                if advisory {
                    tracing::warn!(
                        op = name,
                        timeout_ms = millis(timeout),
                        advisory_op_timeout = true,
                        "sidecar advisory op timed out; not counted"
                    );
                } else if self.session.resume_generation() > issued_generation {
                    tracing::warn!(
                        op = name,
                        timeout_ms = millis(timeout),
                        op_timeout_across_resume = true,
                        "sidecar op timed out across a resume; not counted"
                    );
                } else {
                    self.note_op_timeout(&link, timeout);
                }
                Err(CodeError::SidecarUnavailable)
            }
        }
    }

    /// Registers the execution's channel before the request leaves, so no
    /// event can slip past.
    pub fn open_execution(&self, op: SidecarOp) -> Result<ExecutionHandle, CodeError> {
        let link = self.serving_link()?;
        let id = self.next_id.fetch_add(1, Ordering::Relaxed) + 1;
        let (sender, receiver) = mpsc::channel(self.settings.queue_capacity);
        let stalled = Arc::new(AtomicBool::new(false));
        lock(&self.executions).insert(
            id,
            ExecutionChannel {
                sender,
                stalled: stalled.clone(),
            },
        );
        let encoded = encode_request(&SidecarRequest { id, op });
        if link.send(&encoded).is_err() {
            lock(&self.executions).remove(&id);
            return Err(CodeError::SidecarUnavailable);
        }
        Ok(ExecutionHandle {
            request_id: id,
            receiver,
            stalled,
        })
    }

    pub fn unregister_execution(&self, request_id: u64) {
        lock(&self.executions).remove(&request_id);
    }

    /// Fire-and-forget request whose reply nobody waits for (interrupts
    /// from a dropped stream, restarts at the timeout grace).
    pub fn send_detached(self: &Arc<Self>, op: SidecarOp) {
        let supervisor = self.clone();
        let name = op.name();
        tokio::spawn(async move {
            if let Err(error) = supervisor.call(op).await {
                tracing::debug!(op = name, reason = %error, "detached request failed");
            }
        });
    }

    fn serving_link(&self) -> Result<Arc<dyn SidecarLink>, CodeError> {
        let state = self.state();
        match state {
            SidecarState::Ready | SidecarState::Rotating => {}
            other => {
                return Err(CodeError::KernelNotReady {
                    reason: other.as_str().to_owned(),
                });
            }
        }
        lock(&self.link)
            .clone()
            .ok_or(CodeError::SidecarUnavailable)
    }

    /// A reply that never came counts towards the kill switch only when the
    /// reader was free to deliver it: while the dispatcher is parked on a
    /// recorder that cannot keep up every reply waits behind it by design,
    /// and that is not the sidecar being unresponsive.
    fn note_op_timeout(&self, link: &Arc<dyn SidecarLink>, timeout: Duration) {
        if self.dispatch_blocked() {
            tracing::warn!(
                timeout_ms = millis(timeout),
                dispatch_blocked = true,
                "sidecar op timed out behind a stalled execute recorder"
            );
            return;
        }
        let count = self.consecutive_timeouts.fetch_add(1, Ordering::Relaxed) + 1;
        tracing::warn!(
            timeout_ms = millis(timeout),
            consecutive_timeouts = count,
            "sidecar op timed out"
        );
        if count >= self.settings.max_consecutive_timeouts {
            tracing::error!(
                consecutive_timeouts = count,
                "sidecar unresponsive; killing it"
            );
            link.kill();
        }
    }

    async fn run_loop(self: Arc<Self>, mut control: mpsc::UnboundedReceiver<Control>) {
        loop {
            let state = self.state();
            let attempt = state.attempt().max(1);
            self.state.send_replace(SidecarState::Starting {
                attempt,
                since: self.clock.monotonic(),
            });
            let generation = self.generation.fetch_add(1, Ordering::Relaxed) + 1;
            let (ready_sender, ready_receiver) = oneshot::channel();
            *lock(&self.ready_signal) = Some(ready_sender);
            let (exit_sender, mut exit_receiver) = oneshot::channel();
            let sink = self.event_sink(generation);
            let exit_sink: SidecarExitSink = Box::new(move |code| {
                let _ = exit_sender.send(code);
            });
            let link = match self.launcher.launch(&self.spec, sink, exit_sink) {
                Ok(link) => Arc::<dyn SidecarLink>::from(link),
                Err(error) => {
                    tracing::error!(attempt, reason = %error, "sidecar spawn failed");
                    self.backoff(None).await;
                    continue;
                }
            };
            *lock(&self.link) = Some(link.clone());
            self.state.send_replace(SidecarState::Warming { attempt });
            tracing::info!(attempt, pid = link.pid().0, "sidecar launched");
            let ready = tokio::select! {
                ready = ready_receiver => ready.ok(),
                code = &mut exit_receiver => {
                    let code = code.ok().flatten();
                    self.handle_exit(code);
                    self.backoff(code).await;
                    continue;
                }
                () = tokio::time::sleep(self.settings.ready_timeout) => None,
            };
            let Some(ready) = ready else {
                tracing::error!(
                    attempt,
                    timeout_ms = millis(self.settings.ready_timeout),
                    "sidecar never reported ready; killing it"
                );
                link.kill();
                let code = exit_receiver.await.ok().flatten();
                self.handle_exit(code);
                self.backoff(code).await;
                continue;
            };
            self.on_ready(&ready);
            let code = loop {
                tokio::select! {
                    code = &mut exit_receiver => break code.ok().flatten(),
                    command = control.recv() => match command {
                        Some(Control::Rotate(envs)) => self.begin_rotation(envs),
                        None => break exit_receiver.await.ok().flatten(),
                    },
                }
            };
            self.handle_exit(code);
            self.backoff(code).await;
        }
    }

    fn on_ready(self: &Arc<Self>, ready: &SidecarEvent) {
        let SidecarEvent::Ready {
            v,
            default_context_id,
            kernel_pid,
            warmup_ms,
            languages,
        } = ready
        else {
            return;
        };
        if *v != SIDECAR_PROTOCOL_VERSION {
            tracing::error!(version = v, "sidecar protocol version mismatch");
        }
        let context_id =
            ContextId::parse(default_context_id).unwrap_or_else(|_| ContextId::default_context());
        let available = AvailableLanguages::from_ready(languages);
        if available.unknown > 0 {
            tracing::warn!(
                unknown_languages = available.unknown,
                "sidecar announced languages this agent does not know"
            );
        }
        let names: Vec<&'static str> = available
            .languages
            .iter()
            .map(|language| language.as_str())
            .collect();
        *lock(&self.languages) = available;
        let rotation = lock(&self.rotation_envs);
        {
            let mut registry = lock(&self.registry);
            registry.clear();
            let mut entry = ContextEntry::new(
                context_id.clone(),
                Language::Python,
                self.spec.cwd.clone(),
                rotation.clone().unwrap_or_default(),
            );
            entry.state = ContextState::Ready;
            entry.kernel_pid = *kernel_pid;
            let _ = registry.register(entry);
        }
        tracing::info!(
            kernel_pid,
            warmup_ms,
            languages = ?names,
            sidecar_restarts = self.restarts(),
            "sidecar ready"
        );
        self.consecutive_timeouts.store(0, Ordering::Relaxed);
        self.state.send_replace(SidecarState::Ready);
        if let Some(envs) = rotation.clone() {
            self.begin_rotation(envs);
        }
        drop(rotation);
    }

    /// `Rotating` until the default kernel came back with a fresh
    /// connection file; runs as its own task so an exit during the restart
    /// is still noticed by the loop.
    fn begin_rotation(self: &Arc<Self>, envs: BTreeMap<String, String>) {
        let started = self.state.send_if_modified(|state| {
            if matches!(state, SidecarState::Ready) {
                *state = SidecarState::Rotating;
                true
            } else {
                false
            }
        });
        if !started {
            return;
        }
        let context_id = ContextId::default_context();
        {
            let mut registry = lock(&self.registry);
            let _ = registry.set_state(&context_id, ContextState::Restarting);
            let _ = registry.set_envs(&context_id, envs.clone());
        }
        tracing::info!("default kernel rotation started");
        let supervisor = self.clone();
        tokio::spawn(async move {
            let started = tokio::time::Instant::now();
            let result = supervisor.restart_default(envs).await;
            let restart_ms = millis(started.elapsed());
            let mut registry = lock(&supervisor.registry);
            match result {
                Ok(kernel_pid) => {
                    let _ = registry.set_kernel_pid(&context_id, kernel_pid);
                    tracing::info!(restart_ms, kernel_pid, "default kernel rotated");
                }
                Err(ref error) => {
                    tracing::error!(restart_ms, reason = %error, "default kernel rotation failed");
                }
            }
            let _ = registry.set_state(&context_id, ContextState::Ready);
            drop(registry);
            supervisor.state.send_if_modified(|state| {
                if matches!(state, SidecarState::Rotating) {
                    *state = SidecarState::Ready;
                    true
                } else {
                    false
                }
            });
        });
    }

    async fn restart_default(
        &self,
        envs: BTreeMap<String, String>,
    ) -> Result<Option<u32>, CodeError> {
        let payload = self.call(run_rotation_request(envs)).await?;
        let decoded: KernelPidPayload = payload.decode().map_err(CodeError::SidecarProtocol)?;
        Ok(decoded.kernel_pid)
    }

    fn handle_exit(&self, code: Option<i32>) {
        *lock(&self.link) = None;
        *lock(&self.languages) = AvailableLanguages::default();
        let pending: Vec<PendingReply> = lock(&self.pending).drain().map(|(_, s)| s).collect();
        for sender in pending {
            let _ = sender.send(Err(CodeError::SidecarUnavailable));
        }
        let executions: Vec<(u64, ExecutionChannel)> = lock(&self.executions).drain().collect();
        for (id, channel) in &executions {
            let _ = channel.sender.try_send(SidecarEvent::Error {
                id: *id,
                execution_id: String::new(),
                name: SyntheticError::KernelDied.as_str().to_owned(),
                value: "the kernel sidecar exited".to_owned(),
                traceback: Vec::new(),
            });
            let _ = channel.sender.try_send(SidecarEvent::End {
                id: *id,
                execution_id: String::new(),
                execution_count: 0,
            });
        }
        let kernel_pids = {
            let mut registry = lock(&self.registry);
            let pids = registry.kernel_pids();
            registry.clear();
            pids
        };
        for pid in &kernel_pids {
            (self.kernel_killer)(*pid);
        }
        let restarts = self.restarts.fetch_add(1, Ordering::Relaxed) + 1;
        tracing::warn!(
            exit_code = code,
            pending = pending_count(&executions),
            kernels_killed = kernel_pids.len(),
            sidecar_restarts = restarts,
            dropped_events = self.dropped_events.load(Ordering::Relaxed),
            "sidecar exited; contexts cleared"
        );
    }

    async fn backoff(&self, exit_code: Option<i32>) {
        let now = self.clock.monotonic();
        let next = self.state().exited(now);
        self.state.send_replace(next);
        let delay = match next {
            SidecarState::Exited { backoff_until, .. } => backoff_until.saturating_sub(now),
            _ => Duration::ZERO,
        };
        tracing::warn!(
            attempt = next.attempt(),
            backoff_ms = millis(delay),
            exit_code,
            "sidecar relaunch scheduled"
        );
        tokio::time::sleep(delay).await;
    }

    fn event_sink(self: &Arc<Self>, generation: u64) -> SidecarEventSink {
        let supervisor = self.clone();
        Box::new(move |event| {
            let supervisor = supervisor.clone();
            Box::pin(async move { supervisor.dispatch(generation, event).await })
        })
    }

    async fn dispatch(&self, generation: u64, event: SidecarEvent) {
        if generation != self.generation.load(Ordering::Relaxed) {
            self.dropped_events.fetch_add(1, Ordering::Relaxed);
            return;
        }
        match event {
            SidecarEvent::Ready { .. } => {
                if let Some(sender) = lock(&self.ready_signal).take() {
                    let _ = sender.send(event);
                }
            }
            SidecarEvent::Reply {
                id,
                ok,
                payload,
                error,
            } => self.dispatch_reply(id, ok, payload, error).await,
            SidecarEvent::KernelDied {
                ref context_id,
                exit_code,
                ref execution_id,
            } => {
                tracing::warn!(
                    context_id,
                    exit_code,
                    execution_id,
                    "kernel died; the sidecar restarts it"
                );
                if let Ok(id) = ContextId::parse(context_id) {
                    let _ = lock(&self.registry).set_kernel_pid(&id, None);
                }
            }
            SidecarEvent::Started { id, .. }
            | SidecarEvent::Stdout { id, .. }
            | SidecarEvent::Stderr { id, .. }
            | SidecarEvent::Result { id, .. }
            | SidecarEvent::Error { id, .. }
            | SidecarEvent::End { id, .. } => self.dispatch_execution_event(id, event).await,
        }
    }

    async fn dispatch_reply(
        &self,
        id: u64,
        ok: bool,
        payload: Option<serde_json::Value>,
        error: Option<ReplyError>,
    ) {
        let result = if ok {
            Ok(ReplyPayload(payload.unwrap_or(serde_json::Value::Null)))
        } else {
            Err(reply_error(error))
        };
        if let Some(sender) = lock(&self.pending).remove(&id) {
            let _ = sender.send(result);
            return;
        }
        if lock(&self.executions).contains_key(&id) {
            let message = match result {
                Ok(_) => "unexpected reply".to_owned(),
                Err(error) => error.to_string(),
            };
            self.dispatch_execution_event(
                id,
                SidecarEvent::Error {
                    id,
                    execution_id: String::new(),
                    name: SyntheticError::KernelDied.as_str().to_owned(),
                    value: message,
                    traceback: Vec::new(),
                },
            )
            .await;
            self.dispatch_execution_event(
                id,
                SidecarEvent::End {
                    id,
                    execution_id: String::new(),
                    execution_count: 0,
                },
            )
            .await;
        }
    }

    /// Waits for a full channel up to the stall timeout while the reader
    /// (and so the sidecar's stdout) is held; then gives the recorder up.
    async fn dispatch_execution_event(&self, id: u64, event: SidecarEvent) {
        let Some(channel) = lock(&self.executions).get(&id).cloned() else {
            self.dropped_events.fetch_add(1, Ordering::Relaxed);
            return;
        };
        let event = match channel.sender.try_send(event) {
            Ok(()) => return,
            Err(TrySendError::Full(event)) => event,
            Err(TrySendError::Closed(_)) => {
                lock(&self.executions).remove(&id);
                return;
            }
        };
        self.dispatch_blocked.store(true, Ordering::Relaxed);
        let sent = channel
            .sender
            .send_timeout(event, self.settings.stall_timeout)
            .await;
        self.dispatch_blocked.store(false, Ordering::Relaxed);
        self.consecutive_timeouts.store(0, Ordering::Relaxed);
        match sent {
            Ok(()) => {}
            Err(SendTimeoutError::Timeout(_)) => {
                channel.stalled.store(true, Ordering::Relaxed);
                lock(&self.executions).remove(&id);
                tracing::warn!(
                    id,
                    timeout_ms = millis(self.settings.stall_timeout),
                    "execute recorder stalled; execution truncated"
                );
            }
            Err(SendTimeoutError::Closed(_)) => {
                lock(&self.executions).remove(&id);
            }
        }
    }
}

fn reply_error(error: Option<ReplyError>) -> CodeError {
    let Some(error) = error else {
        return CodeError::Internal("sidecar reply without error".to_owned());
    };
    match error.code {
        SidecarErrorCode::NotFound => CodeError::ContextNotFound,
        SidecarErrorCode::InvalidArgument => CodeError::SidecarRejected(error.message),
        SidecarErrorCode::KernelDead => CodeError::KernelNotReady {
            reason: error.message,
        },
        SidecarErrorCode::Busy => CodeError::ContextBusy,
        SidecarErrorCode::Internal | SidecarErrorCode::Unknown => {
            CodeError::Internal(error.message)
        }
    }
}

fn pending_count(executions: &[(u64, ExecutionChannel)]) -> usize {
    executions.len()
}

pub(crate) fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

/// Log fields are `u64`: a `u128` would be written as a string.
pub(crate) fn millis(duration: Duration) -> u64 {
    u64::try_from(duration.as_millis()).unwrap_or(u64::MAX)
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;
    use std::sync::Arc;
    use std::time::Duration;

    use rayd_core::code::{CodeError, SidecarEvent, SidecarOp};

    use super::super::fake_sidecar::ready_supervisor;
    use super::{OpTimeouts, SidecarSupervisor, SupervisorSettings};

    fn settings() -> SupervisorSettings {
        SupervisorSettings {
            stall_timeout: Duration::from_secs(2),
            queue_capacity: 1,
            op_timeouts: OpTimeouts {
                list_contexts: Duration::from_millis(100),
                ..OpTimeouts::default()
            },
            max_consecutive_timeouts: 3,
            ..SupervisorSettings::default()
        }
    }

    fn execute_op() -> SidecarOp {
        SidecarOp::Execute {
            context_id: "default".to_owned(),
            execution_id: "exec-1".to_owned(),
            code: "print(1)".to_owned(),
            envs: BTreeMap::new(),
        }
    }

    fn stdout(id: u64) -> SidecarEvent {
        SidecarEvent::Stdout {
            id,
            execution_id: "exec-1".to_owned(),
            text: "x".to_owned(),
            timestamp_unix_ns: 1,
        }
    }

    /// `list_contexts` against a fake that never replies.
    async fn time_out(supervisor: &Arc<SidecarSupervisor>, times: u32) {
        for _ in 0..times {
            let reply = supervisor.call(SidecarOp::ListContexts).await;
            assert!(
                matches!(reply, Err(CodeError::SidecarUnavailable)),
                "{reply:?}"
            );
        }
    }

    #[tokio::test(start_paused = true)]
    async fn three_op_timeouts_in_a_row_kill_a_silent_sidecar() {
        let fixture = ready_supervisor(settings()).await;
        time_out(&fixture.supervisor, 2).await;
        assert!(!fixture.launched.log.killed());
        time_out(&fixture.supervisor, 1).await;
        assert!(fixture.launched.log.killed());
    }

    #[tokio::test(start_paused = true)]
    async fn op_timeouts_behind_a_stalled_client_neither_count_nor_kill() {
        let fixture = ready_supervisor(settings()).await;
        let supervisor = fixture.supervisor.clone();
        let log = fixture.launched.log.clone();
        let mut launched = fixture.launched;
        time_out(&supervisor, 2).await;
        let handle = supervisor.open_execution(execute_op()).unwrap();
        let id = handle.request_id;
        launched.emit(stdout(id)).await;
        let reader = tokio::spawn(launched.line(stdout(id)));
        tokio::time::sleep(Duration::from_millis(1)).await;
        assert!(supervisor.dispatch_blocked());
        time_out(&supervisor, 3).await;
        assert!(!log.killed(), "timeouts behind the stall must not kill");
        assert!(supervisor.dispatch_blocked());
        drop(handle);
        reader.await.unwrap();
        assert!(!supervisor.dispatch_blocked());
        time_out(&supervisor, 2).await;
        assert!(!log.killed(), "the counter restarts when the stall ends");
        time_out(&supervisor, 1).await;
        assert!(log.killed());
    }
}
