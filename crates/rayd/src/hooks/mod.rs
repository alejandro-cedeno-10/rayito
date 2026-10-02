//! HTTP/1.1 adapter for the six AWS lifecycle hooks (`AWS_API_NOTES.md` §8).
//! `/run`, `/suspend`, `/resume` and `/terminate` always answer 200: a
//! non-2xx there terminates the `MicroVM`, so anomalies are logged and
//! reported in the JSON body instead. `/ready` and `/validate` are the
//! build-time hooks where 503 means "retry": they answer it while the
//! default kernel warms up or the validation cell runs. Each handler runs
//! under a budget of 80 % of the timeout declared for the hook in
//! `create-microvm-image` and still answers when it expires.
//!
//! `HookReply.outcome` values: `changed` / `unchanged` / `illegal`
//! (phase transitions), `installed` / `tokenless` / `already_ran` (`/run`),
//! `kernel_warming` (503) / `ready_escape` (`/ready`), `validating` (503) /
//! `validated` / `validate_failed` / `validate_skipped` with `--no-sidecar`
//! (`/validate`), `terminating`, `budget_exceeded`.
//!
//! `/suspend` (design D8) closes every open client stream through the
//! `SuspendSignal`, quiesces the sidecar and syncs each filesystem inside a
//! deadline (`BoundedFlush`, never past the 200), without
//! killing a process, a PTY or a kernel; `/resume` (D10) probes every
//! kernel inside a hard cap, restarts the lost ones in the background and
//! reseeds the rest. Both always answer 200, in every lifecycle phase.
//!
//! The logical deadline (ADR-011): an accepted `/run` that installed a
//! lifecycle wakes the watcher thread, and a changed `/resume` hands the
//! deadline the watcher's freeze verdict (`timeout_resumed`) before the
//! kernel probe, so the resume grace or the auto-resume rule applies only
//! after a real checkpoint.
//!
//! Hooks cannot be authenticated by origin (they arrive from `127.0.0.1`
//! like proxied client traffic), so after the first accepted `/run` every
//! runtime hook is audited (`hook_audit` lines, never a body), every
//! accepted `/suspend` arms the stale-suspend watchdog, and `/run` spawns
//! the IMDS verification when the block was installed at boot. A
//! session-changing `/suspend` or `/resume` is never refused: the adapter
//! cannot tell a forged call from the genuine one right behind it, and a
//! refused genuine `/suspend` would skip the checklist of a real
//! checkpoint while the platform freezes the VM anyway.

use std::sync::Arc;
use std::time::{Duration, Instant};

use axum::Router;
use axum::body::Bytes;
use axum::extract::State;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Json, Response};
use axum::routing::post;
use rayd_core::code::{
    KernelStatus, ReadyDecision, SidecarState, ValidateDecision, ready_hook_decision,
    validate_hook_decision,
};
use rayd_core::hooks::{
    HookCallOutcome, QUIESCE_TIMEOUT, RESUME_PROBE_BUDGET, STREAM_CLOSE_GRACE, suspend_actions,
};
use rayd_core::lifecycle::{Hook, LifecycleError, Transition};
use rayd_core::session::{RunHookInput, RunOutcome, SandboxSession};
use rayd_core::wire_tokens::TERMINATING;
use serde::{Deserialize, Serialize};
use tokio_util::sync::CancellationToken;

use rayd_core::suspend_sync::{FlushReport, ParticipantDemand, SuspendBudget, SuspendShares};

use crate::adapters::{
    BoundedFlush, IMDS_VERIFY_BUDGET, ImdsState, PlatformFilesystemSync, UserConnectProbe,
    rule_present, verify_imds_block,
};
use crate::code::CodeManager;
use crate::lifecycle::{
    LifecycleParticipant, ReadyVerdict, SuspendSignal, TimeoutWatcher, suspend_watchdog,
};
use crate::network::NetworkManager;

/// `warn!` threshold for the wall-clock drift recorded at `/resume`.
pub const CLOCK_OFFSET_WARN_MS: i64 = 5_000;

pub const HOOK_PATH_PREFIX: &str = "/aws/lambda-microvms/runtime/v1";

/// Timeouts declared in the image's `hooks` configuration
/// (`scripts/publish_image.py`, `IMAGE_HOOKS`). The budgets below derive from
/// them, so both must change together.
#[must_use]
pub fn declared_timeout(hook: Hook) -> Duration {
    let seconds = match hook {
        Hook::Ready | Hook::Validate => 600,
        Hook::Run | Hook::Suspend | Hook::Resume => 30,
        Hook::Terminate => 10,
    };
    Duration::from_secs(seconds)
}

#[must_use]
pub fn budget(hook: Hook) -> Duration {
    declared_timeout(hook).mul_f64(0.8)
}

#[must_use]
pub fn hook_path(hook: Hook) -> String {
    format!("{HOOK_PATH_PREFIX}/{hook}")
}

/// What the hooks listener is built from. `suspend` is the broadcast the
/// gRPC streams subscribe to; `shutdown` is cancelled after `/terminate` is
/// acknowledged so both listeners drain and exit; `imds` is the IMDS block
/// state shared with `Health` and `user_probe` the uid-1000 connect probe
/// (`None` where no sandbox process can be spawned); `timeout` is the
/// deadline watcher (`TimeoutWatcher::detached` where no lifecycle is ever
/// installed); `network` is the egress manager `/run` and `/resume` drive
/// (`NetworkManager::unavailable` where the guest cannot enforce).
pub struct HookServices {
    pub session: Arc<SandboxSession>,
    pub code: Arc<CodeManager>,
    pub suspend: Arc<SuspendSignal>,
    pub shutdown: CancellationToken,
    pub imds: Arc<ImdsState>,
    pub user_probe: Option<UserConnectProbe>,
    pub timeout: Arc<TimeoutWatcher>,
    pub network: Arc<NetworkManager>,
    /// M15 foundations (ADR-015): the 0.6 feature slots that opted into
    /// `/suspend`'s bounded flush and `/ready`'s combined verdict
    /// (`ConfigurableFeature::participant`). Empty until a feature's slot
    /// returns `Some`, which is how `/suspend` and `/ready` stay byte-for-byte
    /// the 0.5.x behaviour with no 0.6 feature configured.
    pub participants: Vec<Arc<dyn LifecycleParticipant>>,
}

#[derive(Clone)]
struct HooksState {
    session: Arc<SandboxSession>,
    code: Arc<CodeManager>,
    suspend: Arc<SuspendSignal>,
    shutdown: CancellationToken,
    imds: Arc<ImdsState>,
    user_probe: Option<UserConnectProbe>,
    timeout: Arc<TimeoutWatcher>,
    network: Arc<NetworkManager>,
    flush: Arc<BoundedFlush>,
    participants: Arc<Vec<Arc<dyn LifecycleParticipant>>>,
}

/// The router without an IMDS block, a user probe or a deadline watcher
/// (the integration tests and hosts where neither is ever used).
pub fn router(
    session: Arc<SandboxSession>,
    code: Arc<CodeManager>,
    suspend_signal: Arc<SuspendSignal>,
    shutdown: CancellationToken,
) -> Router {
    let session_for_network = session.clone();
    router_with(HookServices {
        session,
        code,
        suspend: suspend_signal,
        shutdown,
        imds: Arc::new(ImdsState::default()),
        user_probe: None,
        timeout: TimeoutWatcher::detached(),
        network: NetworkManager::unavailable(session_for_network),
        participants: Vec::new(),
    })
}

/// The router for the hooks listener, with the platform's bounded
/// per-filesystem sync on `/suspend`.
pub fn router_with(services: HookServices) -> Router {
    let flush = BoundedFlush::new(
        Arc::new(PlatformFilesystemSync),
        SuspendBudget::for_hook(budget(Hook::Suspend)),
    );
    router_with_flush(services, flush)
}

/// The router with the `/suspend` flush given explicitly (tests inject a
/// fake `FilesystemSync` or a shorter deadline).
pub fn router_with_flush(services: HookServices, flush: BoundedFlush) -> Router {
    let state = HooksState {
        session: services.session,
        code: services.code,
        suspend: services.suspend,
        shutdown: services.shutdown,
        imds: services.imds,
        user_probe: services.user_probe,
        timeout: services.timeout,
        network: services.network,
        flush: Arc::new(flush),
        participants: Arc::new(services.participants),
    };
    Router::new()
        .route(&hook_path(Hook::Ready), post(ready))
        .route(&hook_path(Hook::Validate), post(validate))
        .route(&hook_path(Hook::Run), post(run))
        .route(&hook_path(Hook::Suspend), post(suspend))
        .route(&hook_path(Hook::Resume), post(resume))
        .route(&hook_path(Hook::Terminate), post(terminate))
        .with_state(state)
}

/// Body of every hook response. AWS only reads the status code; the fields
/// exist for `scripts/hooks-sim.py` and for humans. `streams_closed` is
/// only present on `/suspend`, `kernel_state_lost` only on `/resume`.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct HookReply {
    pub hook: String,
    pub outcome: String,
    pub phase: String,
    pub suspend_generation: u64,
    pub resume_generation: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub streams_closed: Option<usize>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub kernel_state_lost: Option<bool>,
}

impl HookReply {
    fn new(hook: Hook, outcome: impl Into<String>, session: &SandboxSession) -> Self {
        Self {
            hook: hook.to_string(),
            outcome: outcome.into(),
            phase: session.phase().to_string(),
            suspend_generation: session.suspend_generation(),
            resume_generation: session.resume_generation(),
            streams_closed: None,
            kernel_state_lost: None,
        }
    }
}

/// What a hook's work reports back besides its outcome.
#[derive(Debug, Default, Clone, Copy)]
struct HookExtras {
    streams_closed: Option<usize>,
    kernel_state_lost: Option<bool>,
}

/// AWS wire format of the `/run` body (`RunRequestContent` in the hook
/// OpenAPI): the only hook with a body.
#[derive(Deserialize)]
struct RunEnvelope {
    #[serde(rename = "microvmId")]
    microvm_id: Option<String>,
    #[serde(rename = "runHookPayload")]
    run_hook_payload: Option<String>,
}

/// 503 without a transition while the default kernel warms (AWS retries),
/// 200 with the transition once it is warm, and 200 anyway five minutes
/// after boot so a broken kernel never blocks a build silently.
async fn ready(State(state): State<HooksState>) -> Response {
    let sidecar_state = state.code.sidecar_state();
    let decision = ready_hook_decision(&sidecar_state, state.code.boot_elapsed());
    if matches!(decision, ReadyDecision::Ok) && !participants_ready(&state.participants) {
        tracing::info!(hook = %Hook::Ready, outcome = "participant_not_ready", "ready deferred");
        return retry_later(Hook::Ready, "participant_not_ready", &state.session);
    }
    match decision {
        ReadyDecision::Retry => {
            tracing::info!(hook = %Hook::Ready, outcome = "kernel_warming", state = sidecar_state.as_str(), "ready deferred");
            retry_later(Hook::Ready, "kernel_warming", &state.session)
        }
        ReadyDecision::Escape => {
            tracing::error!(hook = %Hook::Ready, outcome = "ready_escape", state = sidecar_state.as_str(), "kernel never warmed; snapshot taken anyway");
            within_budget(Hook::Ready, state.session.clone(), async move {
                transition_outcome(Hook::Ready, state.session.ready());
                "ready_escape".to_owned()
            })
            .await
        }
        ReadyDecision::Ok => {
            within_budget(Hook::Ready, state.session.clone(), async move {
                transition_outcome(Hook::Ready, state.session.ready())
            })
            .await
        }
    }
}

/// The first call starts the pandas + matplotlib cell and answers 503;
/// later calls answer 503 while it runs and 200 once it finished, with
/// the outcome in the body (the build is never failed on purpose).
async fn validate(State(state): State<HooksState>) -> Response {
    log_transition(state.session.validate());
    if state.code.sidecar_state() == SidecarState::Disabled {
        return Json(HookReply::new(
            Hook::Validate,
            "validate_skipped",
            &state.session,
        ))
        .into_response();
    }
    match validate_hook_decision(&state.code.validation_state()) {
        ValidateDecision::Start => {
            state.code.start_validation();
            tracing::info!(hook = %Hook::Validate, outcome = "validating", "validate cell started");
            retry_later(Hook::Validate, "validating", &state.session)
        }
        ValidateDecision::Retry => retry_later(Hook::Validate, "validating", &state.session),
        ValidateDecision::Done(outcome) => {
            tracing::info!(hook = %Hook::Validate, outcome = outcome.as_str(), "validate answered");
            Json(HookReply::new(
                Hook::Validate,
                outcome.as_str(),
                &state.session,
            ))
            .into_response()
        }
    }
}

/// The 200 goes out first; the default-kernel rotation (fresh HMAC key,
/// fresh seeds, payload envs) and the IMDS verification run in the
/// background (design D11, D4). A `/run` after the accepted one is an
/// audited anomaly that changes nothing.
///
/// `mark_rotation_pending` runs before the first `await`, closing the
/// kernel rotation window (`MILESTONES.md` M9 deferred list): no readiness
/// probe landing anywhere from here on can see the previous sandbox's
/// kernel as ready. The actual restart request, `spawn_run_rotation`,
/// stays after egress enforcement so the rotated kernel still picks up the
/// settled proxy env.
async fn run(State(state): State<HooksState>, body: Bytes) -> Response {
    within_budget(Hook::Run, state.session.clone(), async move {
        let envelope = parse_envelope(&body);
        let outcome = state.session.run(RunHookInput {
            sandbox_id: envelope.microvm_id.as_deref(),
            payload: envelope
                .run_hook_payload
                .as_deref()
                .filter(|payload| !payload.is_empty()),
        });
        if outcome == RunOutcome::Installed {
            state.code.mark_rotation_pending();
            state.timeout.wake();
            let defaults = state.session.spawn_defaults();
            tracing::info!(
                hook = %Hook::Run,
                metadata_keys = state.session.metadata().len(),
                cpu_seconds = defaults.cpu_seconds,
                lifecycle_phase = state.session.lifecycle().phase.as_str(),
                "run defaults applied"
            );
            state.network.on_run().await;
            state.code.spawn_run_rotation(defaults.envs);
            spawn_imds_verification(&state);
        }
        if matches!(outcome, RunOutcome::AlreadyRan | RunOutcome::Illegal(_)) {
            audit(
                &state.session,
                Hook::Run,
                "already_ran",
                HookCallOutcome::Anomalous,
            );
        }
        run_outcome(&outcome, envelope.run_hook_payload.as_deref())
    })
    .await
}

/// Design D8: transition, detach the execute origins, broadcast, wait the
/// stream-close grace, quiesce, bounded per-filesystem sync, 200. The sync
/// waits at most the `SuspendBudget` deadline; what has not returned by
/// then keeps running off the hook's path. Nothing is killed and nothing
/// in flight is awaited; a repeated `/suspend` changes nothing. Every
/// accepted transition arms the stale-suspend watchdog (design D2).
async fn suspend(State(state): State<HooksState>) -> Response {
    within_budget_extras(Hook::Suspend, state.session.clone(), async move {
        let started = Instant::now();
        let transition = state.session.suspend();
        let close_streams = transition
            .as_ref()
            .is_ok_and(|transition| suspend_actions(transition).close_streams);
        let outcome = transition_outcome(Hook::Suspend, transition.clone());
        audit(
            &state.session,
            Hook::Suspend,
            &outcome,
            HookCallOutcome::Nominal,
        );
        let streams_closed = if close_streams {
            close_client_streams(&state).await
        } else {
            0
        };
        if transition.is_ok() {
            state.code.quiesce(QUIESCE_TIMEOUT).await;
        }
        let (flushed, participants_run) = tokio::join!(
            state.flush.flush(),
            run_participants(&state.participants, state.flush.budget())
        );
        log_flush(&flushed, state.flush.budget());
        if close_streams {
            spawn_suspend_watchdog(&state.session);
        }
        tracing::info!(
            hook = %Hook::Suspend,
            outcome = %outcome,
            suspend_generation = state.session.suspend_generation(),
            streams_closed,
            filesystems_synced = flushed.synced,
            participants_run,
            suspend_ms = millis(started.elapsed()),
            "suspend recorded"
        );
        (
            outcome,
            HookExtras {
                streams_closed: Some(streams_closed),
                kernel_state_lost: None,
            },
        )
    })
    .await
}

/// `/ready` only ever downgrades its own decision: `Ok` from every
/// participant leaves the existing verdict untouched, and any `Retry` or
/// `Fail` keeps answering 503 rather than declare the sandbox ready while a
/// feature says it should not be.
fn participants_ready(participants: &[Arc<dyn LifecycleParticipant>]) -> bool {
    participants
        .iter()
        .all(|participant| participant.ready_gate() == ReadyVerdict::Ok)
}

/// Runs every participant's `on_suspend` concurrently with the
/// per-filesystem `syncfs` calls (`BoundedFlush::flush`), each cut off at
/// its own `SuspendShares` allocation inside the same `SuspendBudget`
/// (`rayd_core::suspend_sync`). Returns how many participants ran, for the
/// `suspend recorded` log line; with no participant configured this
/// resolves immediately and changes nothing about `/suspend`'s 0.5.x
/// behaviour.
async fn run_participants(
    participants: &[Arc<dyn LifecycleParticipant>],
    budget: SuspendBudget,
) -> usize {
    if participants.is_empty() {
        return 0;
    }
    let demands: Vec<ParticipantDemand> = participants.iter().map(|p| p.demand()).collect();
    let shares = SuspendShares::allocate(budget, &demands);
    let mut joins = tokio::task::JoinSet::new();
    // `cloned()` is not redundant here: each spawned task needs an owned,
    // `'static` `Arc`, not a borrow of this function's `participants` slice.
    #[allow(clippy::unnecessary_to_owned)]
    for participant in participants.iter().cloned() {
        let share = shares.share_for(participant.demand().name);
        joins.spawn(async move {
            let _ = tokio::time::timeout(share, participant.on_suspend(share)).await;
        });
    }
    let mut completed = 0;
    while joins.join_next().await.is_some() {
        completed += 1;
    }
    completed
}

fn spawn_suspend_watchdog(session: &Arc<SandboxSession>) {
    let generation = session.suspend_generation();
    tokio::spawn(suspend_watchdog(session.clone(), generation));
}

/// The `hook_audit` line every runtime hook produces after the accepted
/// `/run`: hook, outcome, per-hook counter and the anomaly flag. Bodies,
/// headers and payload characters never appear here.
fn audit(session: &SandboxSession, hook: Hook, outcome: &str, call: HookCallOutcome) {
    if let Some(entry) = session.audit_hook(hook, call) {
        tracing::info!(
            hook = %hook,
            outcome,
            calls_since_run = entry.calls_since_run,
            anomaly = entry.anomaly,
            hook_anomalies = session.hook_anomalies(),
            "hook_audit"
        );
    }
}

/// Design D4 steps 1-3 after the first accepted `/run`, off the hook's
/// budget: only when the block was installed at boot.
fn spawn_imds_verification(state: &HooksState) {
    if !state.imds.installed() {
        return;
    }
    let imds = state.imds.clone();
    let user_probe = state.user_probe.clone();
    tokio::spawn(async move {
        let verified = tokio::time::timeout(
            IMDS_VERIFY_BUDGET,
            verify_imds_block(&imds, user_probe.as_ref()),
        )
        .await;
        let Ok(probe) = verified else {
            imds.set_blocked(false);
            tracing::warn!(
                imds_blocked = false,
                budget_ms = millis(IMDS_VERIFY_BUDGET),
                "imds_probe timed out"
            );
            return;
        };
        tracing::info!(
            rule_present = probe.rule_present,
            root_reachable = probe.root_reachable,
            user_probe_ran = probe.user_reachable.is_some(),
            user_reachable = probe.user_reachable.unwrap_or(true),
            imds_blocked = probe.imds_blocked(),
            "imds_probe"
        );
    });
}

/// `/resume` re-checks the rule only (cheap): a rule that vanished clears
/// `imds_blocked` and is logged as `imds_rule_missing`.
fn spawn_imds_recheck(state: &HooksState) {
    if !state.imds.installed() {
        return;
    }
    let imds = state.imds.clone();
    tokio::spawn(async move {
        if !rule_present().await {
            imds.set_blocked(false);
            tracing::warn!(
                rule_present = false,
                imds_blocked = false,
                "imds_rule_missing"
            );
        }
    });
}

async fn close_client_streams(state: &HooksState) -> usize {
    let detached = state.code.detach_for_suspend();
    let open = state.suspend.open_streams();
    state.suspend.broadcast(state.session.suspend_generation());
    let pending = state.suspend.wait_streams_closed(STREAM_CLOSE_GRACE).await;
    let closed = open.saturating_sub(pending);
    tracing::info!(
        hook = %Hook::Suspend,
        streams_closed = closed,
        streams_pending = pending,
        subscribers = detached,
        "client streams closed"
    );
    closed
}

/// Design D10: transition, hand the deadline the freeze verdict (ADR-011),
/// probe every kernel inside the hard cap (lost ones are restarted in the
/// background), reseed in the background, 200. A `/resume` after a
/// stale-suspend recovery is the real one (`resume_after_stale_recovery`).
async fn resume(State(state): State<HooksState>) -> Response {
    within_budget_extras(Hook::Resume, state.session.clone(), async move {
        let transition = state.session.resume();
        let changed = transition.as_ref().is_ok_and(Transition::changed);
        if transition
            .as_ref()
            .is_ok_and(|transition| transition.after_stale_recovery)
        {
            tracing::warn!(
                hook = %Hook::Resume,
                resume_generation = state.session.resume_generation(),
                "resume_after_stale_recovery"
            );
        }
        let outcome = transition_outcome(Hook::Resume, transition);
        audit(
            &state.session,
            Hook::Resume,
            &outcome,
            HookCallOutcome::Nominal,
        );
        if !changed {
            return (
                outcome,
                HookExtras {
                    streams_closed: None,
                    kernel_state_lost: Some(state.code.kernel_state_lost()),
                },
            );
        }
        resume_deadline(&state);
        state.network.on_resume().await;
        run_participants_on_resume(&state.participants).await;
        let probe_started = Instant::now();
        let probe = state.code.probe_after_resume(RESUME_PROBE_BUDGET).await;
        let probe_ms = millis(probe_started.elapsed());
        let kernel_state_lost = state.code.kernel_state_lost();
        state.code.spawn_resume_reseed();
        spawn_imds_recheck(&state);
        let health = state.session.health();
        tracing::info!(
            hook = %Hook::Resume,
            resume_generation = health.resume_generation,
            clock_offset_ms = health.clock_offset_ms,
            suspended_ms = millis(state.session.suspended_total()),
            probe_ms,
            kernels_alive = probe.alive.len(),
            kernels_lost = probe.lost.len(),
            kernel_state_lost,
            lifecycle_phase = health.lifecycle.phase.as_str(),
            "resume recorded"
        );
        if health.clock_offset_ms.abs() > CLOCK_OFFSET_WARN_MS {
            tracing::warn!(
                hook = %Hook::Resume,
                clock_offset_ms = health.clock_offset_ms,
                "wall clock drifted more than 5 s across the pause"
            );
        }
        (
            outcome,
            HookExtras {
                streams_closed: None,
                kernel_state_lost: Some(kernel_state_lost),
            },
        )
    })
    .await
}

/// Only a `/resume` right after a freeze the watcher saw may open the
/// deadline's one resume grace or apply the auto-resume rule; the watcher
/// is woken so the new phase is evaluated at once.
fn resume_deadline(state: &HooksState) {
    let now = state.session.clock().monotonic();
    state.session.timeout_resumed(state.timeout.frozen(now));
    state.timeout.wake();
}

async fn terminate(State(state): State<HooksState>) -> Response {
    within_budget(Hook::Terminate, state.session.clone(), async move {
        log_transition(state.session.terminate());
        audit(
            &state.session,
            Hook::Terminate,
            TERMINATING,
            HookCallOutcome::Nominal,
        );
        run_participants_on_terminate(&state.participants).await;
        schedule_shutdown(state.shutdown.clone());
        TERMINATING.to_owned()
    })
    .await
}

/// Runs every participant's `on_resume` concurrently; with no participant
/// configured this resolves immediately, same as `run_participants` on
/// `/suspend`. Unlike `/suspend`'s flush, `/resume` has no
/// `SuspendBudget`/`SuspendShares` of its own to divide, so each
/// participant just runs under the hook's own overall deadline
/// (`within_budget_extras`).
async fn run_participants_on_resume(participants: &[Arc<dyn LifecycleParticipant>]) {
    if participants.is_empty() {
        return;
    }
    let mut joins = tokio::task::JoinSet::new();
    #[allow(clippy::unnecessary_to_owned)]
    for participant in participants.iter().cloned() {
        joins.spawn(async move { participant.on_resume().await });
    }
    while joins.join_next().await.is_some() {}
}

/// Runs every participant's `on_terminate` concurrently, before
/// `schedule_shutdown` starts winding the process down — a participant
/// that queues a line (`lifecycle_events`) or flushes a buffer needs to run
/// while stdout and the runtime are still alive.
async fn run_participants_on_terminate(participants: &[Arc<dyn LifecycleParticipant>]) {
    if participants.is_empty() {
        return;
    }
    let mut joins = tokio::task::JoinSet::new();
    #[allow(clippy::unnecessary_to_owned)]
    for participant in participants.iter().cloned() {
        joins.spawn(async move { participant.on_terminate().await });
    }
    while joins.join_next().await.is_some() {}
}

async fn within_budget<F>(hook: Hook, session: Arc<SandboxSession>, work: F) -> Response
where
    F: Future<Output = String>,
{
    within_budget_extras(
        hook,
        session,
        async move { (work.await, HookExtras::default()) },
    )
    .await
}

async fn within_budget_extras<F>(hook: Hook, session: Arc<SandboxSession>, work: F) -> Response
where
    F: Future<Output = (String, HookExtras)>,
{
    let (outcome, extras) = tokio::time::timeout(budget(hook), work)
        .await
        .unwrap_or_else(|_| {
            tracing::error!(hook = %hook, budget_ms = budget(hook).as_millis(), "hook exceeded its budget");
            ("budget_exceeded".to_owned(), HookExtras::default())
        });
    let mut reply = HookReply::new(hook, outcome, &session);
    reply.streams_closed = extras.streams_closed;
    reply.kernel_state_lost = extras.kernel_state_lost;
    Json(reply).into_response()
}

fn millis(duration: Duration) -> u64 {
    u64::try_from(duration.as_millis()).unwrap_or(u64::MAX)
}

fn retry_later(hook: Hook, outcome: &str, session: &SandboxSession) -> Response {
    (
        StatusCode::SERVICE_UNAVAILABLE,
        Json(HookReply::new(hook, outcome, session)),
    )
        .into_response()
}

fn parse_envelope(body: &[u8]) -> RunEnvelope {
    serde_json::from_slice(body).unwrap_or_else(|error| {
        tracing::warn!(hook = %Hook::Run, reason = "envelope is not the expected JSON", line = error.line(), column = error.column(), "run body ignored");
        RunEnvelope {
            microvm_id: None,
            run_hook_payload: None,
        }
    })
}

fn run_outcome(outcome: &RunOutcome, payload: Option<&str>) -> String {
    let payload_chars = payload.map_or(0, |payload| payload.chars().count());
    match outcome {
        RunOutcome::Installed => {
            tracing::info!(hook = %Hook::Run, outcome = "installed", payload_chars, "access token installed");
            "installed".to_owned()
        }
        RunOutcome::Tokenless(error) => {
            tracing::warn!(hook = %Hook::Run, outcome = "tokenless", payload_chars, reason = %error, "agent stays token-less");
            "tokenless".to_owned()
        }
        RunOutcome::AlreadyRan => {
            tracing::warn!(hook = %Hook::Run, outcome = "already_ran", "run already accepted this boot");
            "already_ran".to_owned()
        }
        RunOutcome::Illegal(error) => {
            tracing::warn!(hook = %Hook::Run, outcome = "illegal", reason = %error, "run ignored");
            "illegal".to_owned()
        }
    }
}

fn transition_outcome(hook: Hook, result: Result<Transition, LifecycleError>) -> String {
    match result {
        Ok(transition) => {
            log_transition(transition);
            if transition.changed() {
                "changed"
            } else {
                "unchanged"
            }
            .to_owned()
        }
        Err(error) => {
            tracing::warn!(hook = %hook, outcome = "illegal", reason = %error, "hook ignored");
            "illegal".to_owned()
        }
    }
}

fn log_transition(transition: Transition) {
    tracing::info!(
        hook = %transition.hook,
        from = %transition.from,
        to = %transition.to,
        "hook acknowledged"
    );
}

fn schedule_shutdown(shutdown: CancellationToken) {
    tokio::spawn(async move {
        tokio::time::sleep(Duration::from_millis(50)).await;
        shutdown.cancel();
    });
}

/// Step 4 of the `/suspend` checklist, logged with counts only (never a
/// path): a hit deadline means some filesystem is hung or slow and its
/// dirty pages may miss the checkpoint.
fn log_flush(report: &FlushReport, budget: SuspendBudget) {
    if report.deadline_hit() {
        tracing::warn!(
            hook = %Hook::Suspend,
            sync_pending = report.pending,
            sync_failed = report.failed,
            filesystems_synced = report.synced,
            deadline_ms = millis(budget.sync_deadline()),
            "suspend sync deadline hit"
        );
    } else if report.failed > 0 {
        tracing::warn!(
            hook = %Hook::Suspend,
            sync_failed = report.failed,
            filesystems_synced = report.synced,
            "suspend sync failed on some filesystems"
        );
    }
    if report.skipped_in_flight > 0 {
        tracing::warn!(
            hook = %Hook::Suspend,
            sync_skipped = report.skipped_in_flight,
            "suspend sync skipped filesystems still syncing"
        );
    }
}

#[cfg(test)]
mod tests {
    use rayd_core::network::{EgressEnforcement, RESUME_VERIFY_BUDGET, RUN_ENFORCE_BUDGET};

    use super::*;

    #[test]
    fn an_illegal_transition_has_its_own_outcome() {
        assert_eq!(
            transition_outcome(
                Hook::Suspend,
                Err(LifecycleError::IllegalTransition {
                    hook: Hook::Suspend,
                    phase: rayd_core::lifecycle::HookPhase::Booting
                })
            ),
            "illegal"
        );
    }

    #[test]
    fn hook_reply_omits_the_optional_fields_unless_set() {
        let mut reply = HookReply {
            hook: "suspend".to_owned(),
            outcome: "changed".to_owned(),
            phase: "suspending".to_owned(),
            suspend_generation: 1,
            resume_generation: 0,
            streams_closed: None,
            kernel_state_lost: None,
        };
        let bare = serde_json::to_string(&reply).unwrap();
        assert!(!bare.contains("streams_closed"));
        assert!(!bare.contains("kernel_state_lost"));
        assert_eq!(serde_json::from_str::<HookReply>(&bare).unwrap(), reply);
        reply.streams_closed = Some(8);
        reply.kernel_state_lost = Some(false);
        let full = serde_json::to_string(&reply).unwrap();
        assert!(full.contains("\"streams_closed\":8"));
        assert!(full.contains("\"kernel_state_lost\":false"));
        assert_eq!(serde_json::from_str::<HookReply>(&full).unwrap(), reply);
    }

    /// `declared_timeout` mirrors `IMAGE_HOOKS` in
    /// `clients/python/src/rayito/cli/_publish.py` (the publish pipeline
    /// behind `scripts/publish_image.py`); a change on either side must be
    /// made on both.
    #[test]
    fn declared_timeouts_match_the_published_image_hooks() {
        let publish_script = include_str!("../../../../clients/python/src/rayito/cli/_publish.py");
        for (key, hook) in [
            ("suspendTimeoutInSeconds", Hook::Suspend),
            ("resumeTimeoutInSeconds", Hook::Resume),
            ("runTimeoutInSeconds", Hook::Run),
            ("terminateTimeoutInSeconds", Hook::Terminate),
            ("readyTimeoutInSeconds", Hook::Ready),
            ("validateTimeoutInSeconds", Hook::Validate),
        ] {
            let declared = format!("\"{key}\": {},", declared_timeout(hook).as_secs());
            assert!(
                publish_script.contains(&declared),
                "{declared} not in IMAGE_HOOKS"
            );
        }
    }

    #[test]
    fn budgets_are_eighty_percent_of_the_declared_timeouts() {
        assert_eq!(budget(Hook::Suspend), Duration::from_secs(24));
        assert_eq!(budget(Hook::Resume), Duration::from_secs(24));
        assert!(rayd_core::hooks::RESUME_PROBE_BUDGET < budget(Hook::Resume));
        assert!(
            rayd_core::hooks::RESUME_PROBE_BUDGET + RESUME_VERIFY_BUDGET < budget(Hook::Resume)
        );
        assert!(RUN_ENFORCE_BUDGET < budget(Hook::Run));
        assert!(
            rayd_core::hooks::STREAM_CLOSE_GRACE + rayd_core::hooks::QUIESCE_TIMEOUT
                < budget(Hook::Suspend)
        );
    }

    mod egress {
        use axum::body::Body;
        use axum::http::Request;
        use rayd_core::clock::SystemClock;
        use rayd_core::network::Family;
        use tower::ServiceExt;

        use super::*;
        use crate::adapters::OsRandomSource;
        use crate::network::ProxySeams;
        use crate::network::fake_kernel::FakeKernel;

        const DIGEST_HEX: &str = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";

        fn hooks(kernel: Arc<FakeKernel>) -> (Arc<SandboxSession>, Router) {
            let session = Arc::new(SandboxSession::new(Arc::new(SystemClock::new()), "test"));
            let network = NetworkManager::new(session.clone(), kernel, true, ProxySeams::default());
            let router = router_with(HookServices {
                session: session.clone(),
                code: CodeManager::disabled(session.clone(), Arc::new(OsRandomSource)),
                suspend: Arc::new(SuspendSignal::new()),
                shutdown: CancellationToken::new(),
                imds: Arc::new(ImdsState::default()),
                user_probe: None,
                timeout: TimeoutWatcher::detached(),
                network,
                participants: Vec::new(),
            });
            (session, router)
        }

        async fn post(router: &Router, hook: Hook, body: String) -> StatusCode {
            let request = Request::post(hook_path(hook))
                .header("content-type", "application/json")
                .body(Body::from(body))
                .unwrap();
            router.clone().oneshot(request).await.unwrap().status()
        }

        fn run_body(network: &str) -> String {
            let payload = format!("{{\"v\":1,\"token_sha256\":\"{DIGEST_HEX}\"{network}}}");
            serde_json::json!({"microvmId": "mvm-1", "runHookPayload": payload}).to_string()
        }

        #[tokio::test]
        async fn run_with_enforce_verifies_deny_all_before_answering() {
            let kernel = Arc::new(FakeKernel::new(true, Vec::new()));
            let (session, router) = hooks(kernel.clone());
            let status = post(
                &router,
                Hook::Run,
                run_body(",\"network\":{\"enforce\":true}"),
            )
            .await;
            assert_eq!(status, StatusCode::OK);
            assert_eq!(session.egress_enforcement(), EgressEnforcement::GuestRoutes);
            assert_eq!(kernel.rule_priorities(Family::V4), [150]);
            assert_eq!(kernel.rule_priorities(Family::V6), [150]);
            assert!(session.egress_env().contains_key("HTTPS_PROXY"));
        }

        #[tokio::test(start_paused = true)]
        async fn health_waits_for_a_deny_all_that_outlives_the_run_budget() {
            let kernel = Arc::new(FakeKernel::new(true, Vec::new()));
            kernel.delay_local_addresses(Duration::from_millis(2_763));
            let (session, router) = hooks(kernel);
            let status = post(
                &router,
                Hook::Run,
                run_body(",\"network\":{\"enforce\":true}"),
            )
            .await;
            assert_eq!(status, StatusCode::OK);
            assert_eq!(session.egress_enforcement(), EgressEnforcement::None);
            assert!(!session.health().agent_ready);
            tokio::time::sleep(Duration::from_secs(2)).await;
            assert_eq!(session.egress_enforcement(), EgressEnforcement::GuestRoutes);
            assert!(session.health().agent_ready);
        }

        #[tokio::test]
        async fn an_image_without_net_admin_is_ready_after_run() {
            let session = Arc::new(SandboxSession::new(Arc::new(SystemClock::new()), "test"));
            let network = NetworkManager::unavailable(session.clone());
            let router = router_with(HookServices {
                session: session.clone(),
                code: CodeManager::disabled(session.clone(), Arc::new(OsRandomSource)),
                suspend: Arc::new(SuspendSignal::new()),
                shutdown: CancellationToken::new(),
                imds: Arc::new(ImdsState::default()),
                user_probe: None,
                timeout: TimeoutWatcher::detached(),
                network,
                participants: Vec::new(),
            });
            let status = post(
                &router,
                Hook::Run,
                run_body(",\"network\":{\"enforce\":true}"),
            )
            .await;
            assert_eq!(status, StatusCode::OK);
            assert_eq!(session.egress_enforcement(), EgressEnforcement::None);
            assert!(session.health().agent_ready);
        }

        #[tokio::test]
        async fn run_without_enforce_touches_nothing() {
            let kernel = Arc::new(FakeKernel::new(false, Vec::new()));
            let (session, router) = hooks(kernel.clone());
            assert_eq!(post(&router, Hook::Run, run_body("")).await, StatusCode::OK);
            assert!(kernel.executed().is_empty());
            assert_eq!(session.egress_enforcement(), EgressEnforcement::None);
            assert!(session.egress_env().is_empty());
        }

        #[tokio::test]
        async fn a_failing_executor_still_answers_200_with_none() {
            let kernel = Arc::new(FakeKernel::new(false, Vec::new()));
            kernel.fail_everything();
            let (session, router) = hooks(kernel);
            let status = post(
                &router,
                Hook::Run,
                run_body(",\"network\":{\"enforce\":true}"),
            )
            .await;
            assert_eq!(status, StatusCode::OK);
            assert_eq!(session.egress_enforcement(), EgressEnforcement::None);
        }

        #[tokio::test]
        async fn resume_reinstalls_a_lost_table_before_answering() {
            let kernel = Arc::new(FakeKernel::new(false, Vec::new()));
            let (session, router) = hooks(kernel.clone());
            post(
                &router,
                Hook::Run,
                run_body(",\"network\":{\"enforce\":true}"),
            )
            .await;
            assert_eq!(
                post(&router, Hook::Suspend, String::new()).await,
                StatusCode::OK
            );
            kernel.drop_table(Family::V4, 101);
            assert_eq!(
                post(&router, Hook::Resume, String::new()).await,
                StatusCode::OK
            );
            assert!(kernel.table(Family::V4, 101).is_some());
            assert!(kernel.blocked("1.1.1.1".parse().unwrap()));
            assert_eq!(session.egress_enforcement(), EgressEnforcement::GuestRoutes);
        }
    }

    mod suspend_flush {
        use axum::body::Body;
        use axum::http::Request;
        use http_body_util::BodyExt;
        use rayd_core::clock::SystemClock;
        use rayd_core::suspend_sync::SUSPEND_SYNC_DEADLINE;
        use tower::ServiceExt;

        use super::*;
        use crate::adapters::OsRandomSource;
        use crate::adapters::bounded_sync::fake::{Behaviour, FakeFilesystemSync};

        const DIGEST_HEX: &str = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";

        fn hooks(fake: Arc<FakeFilesystemSync>) -> Router {
            let session = Arc::new(SandboxSession::new(Arc::new(SystemClock::new()), "test"));
            let services = HookServices {
                session: session.clone(),
                code: CodeManager::disabled(session.clone(), Arc::new(OsRandomSource)),
                suspend: Arc::new(SuspendSignal::new()),
                shutdown: CancellationToken::new(),
                imds: Arc::new(ImdsState::default()),
                user_probe: None,
                timeout: TimeoutWatcher::detached(),
                network: NetworkManager::unavailable(session),
                participants: Vec::new(),
            };
            let flush = BoundedFlush::new(fake, SuspendBudget::for_hook(budget(Hook::Suspend)));
            router_with_flush(services, flush)
        }

        async fn post(router: &Router, hook: Hook, body: String) -> (StatusCode, HookReply) {
            let request = Request::post(hook_path(hook))
                .header("content-type", "application/json")
                .body(Body::from(body))
                .unwrap();
            let response = router.clone().oneshot(request).await.unwrap();
            let status = response.status();
            let bytes = response.into_body().collect().await.unwrap().to_bytes();
            (status, serde_json::from_slice(&bytes).unwrap())
        }

        fn run_body() -> String {
            let payload = format!("{{\"v\":1,\"token_sha256\":\"{DIGEST_HEX}\"}}");
            serde_json::json!({"microvmId": "mvm-1", "runHookPayload": payload}).to_string()
        }

        /// A filesystem whose `syncfs` never returns (a hung hard NFS or
        /// FUSE mount) costs `/suspend` the sync deadline and nothing more:
        /// the checklist still completes and the 200 goes out well inside
        /// the hook budget, as does the repeated `/suspend` behind it.
        #[tokio::test(start_paused = true)]
        async fn a_sync_that_never_returns_does_not_hold_the_suspend_200() {
            let fake = Arc::new(FakeFilesystemSync::new(&[(
                "/mnt/hung",
                Behaviour::BlocksForever,
            )]));
            let router = hooks(fake.clone());
            assert_eq!(post(&router, Hook::Run, run_body()).await.0, StatusCode::OK);

            let started = tokio::time::Instant::now();
            let (status, reply) = post(&router, Hook::Suspend, String::new()).await;
            let elapsed = started.elapsed();
            assert_eq!(status, StatusCode::OK);
            assert_eq!(reply.outcome, "changed");
            assert_eq!(reply.suspend_generation, 1);
            assert!(elapsed >= SUSPEND_SYNC_DEADLINE, "{elapsed:?}");
            assert!(elapsed <= budget(Hook::Suspend) / 2, "{elapsed:?}");

            let started = tokio::time::Instant::now();
            let (status, reply) = post(&router, Hook::Suspend, String::new()).await;
            assert_eq!(status, StatusCode::OK);
            assert_eq!(reply.outcome, "unchanged");
            assert!(started.elapsed() < SUSPEND_SYNC_DEADLINE);
            assert!(
                fake.calls() <= 1,
                "the hung filesystem gets no second thread"
            );
        }
    }

    /// `/resume` reaches every registered `LifecycleParticipant`
    /// (`run_participants_on_resume`): without this, a feature's resume
    /// work (`telemetry_export`'s connection-pool rebuild) never runs.
    mod participant_hooks {
        use std::sync::atomic::{AtomicU32, Ordering};

        use axum::body::Body;
        use axum::http::Request;
        use rayd_core::clock::SystemClock;
        use rayd_core::suspend_sync::ParticipantDemand;
        use tower::ServiceExt;

        use super::*;
        use crate::adapters::OsRandomSource;

        const DIGEST_HEX: &str = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";

        #[derive(Default)]
        struct CountingParticipant {
            resumed: AtomicU32,
        }

        #[tonic::async_trait]
        impl LifecycleParticipant for CountingParticipant {
            fn demand(&self) -> ParticipantDemand {
                ParticipantDemand {
                    name: "counting",
                    max: Duration::from_millis(1),
                }
            }

            async fn on_resume(&self) {
                self.resumed.fetch_add(1, Ordering::SeqCst);
            }
        }

        fn hooks(participant: Arc<CountingParticipant>) -> Router {
            let session = Arc::new(SandboxSession::new(Arc::new(SystemClock::new()), "test"));
            router_with(HookServices {
                session: session.clone(),
                code: CodeManager::disabled(session.clone(), Arc::new(OsRandomSource)),
                suspend: Arc::new(SuspendSignal::new()),
                shutdown: CancellationToken::new(),
                imds: Arc::new(ImdsState::default()),
                user_probe: None,
                timeout: TimeoutWatcher::detached(),
                network: NetworkManager::unavailable(session),
                participants: vec![participant],
            })
        }

        async fn post(router: &Router, hook: Hook, body: String) -> StatusCode {
            let request = Request::post(hook_path(hook))
                .header("content-type", "application/json")
                .body(Body::from(body))
                .unwrap();
            router.clone().oneshot(request).await.unwrap().status()
        }

        fn run_body() -> String {
            let payload = format!("{{\"v\":1,\"token_sha256\":\"{DIGEST_HEX}\"}}");
            serde_json::json!({"microvmId": "mvm-1", "runHookPayload": payload}).to_string()
        }

        #[tokio::test]
        async fn a_changed_resume_calls_every_participants_on_resume_once() {
            let participant = Arc::new(CountingParticipant::default());
            let router = hooks(participant.clone());
            assert_eq!(post(&router, Hook::Run, run_body()).await, StatusCode::OK);
            assert_eq!(
                post(&router, Hook::Suspend, String::new()).await,
                StatusCode::OK
            );
            assert_eq!(participant.resumed.load(Ordering::SeqCst), 0);
            assert_eq!(
                post(&router, Hook::Resume, String::new()).await,
                StatusCode::OK
            );
            assert_eq!(participant.resumed.load(Ordering::SeqCst), 1);
            // A repeated `/resume` changes nothing, participants included.
            assert_eq!(
                post(&router, Hook::Resume, String::new()).await,
                StatusCode::OK
            );
            assert_eq!(participant.resumed.load(Ordering::SeqCst), 1);
        }
    }
}
