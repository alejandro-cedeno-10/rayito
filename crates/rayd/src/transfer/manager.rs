//! `TransferManager` (design D8, D9, D10, D12): accepts an import or an
//! export after every check that needs no network (phase and `/run` gates,
//! URL policy, request rules, identity with C-05, deny list, destination or
//! source), admits it in the registry and spawns its task. The registry and
//! one `watch` channel per active transfer live in `TransferHub`, the
//! non-generic half the barrier and the gRPC layer share; byte moves take
//! one of two permits. Tasks subscribe to `SuspendSignal` while they are
//! active: a `/suspend` stops their I/O and requeues them (the hook's
//! stream-close grace waits for that), they issue nothing until the gate
//! reopens, and a `/terminate` cancels them without a cleanup `DELETE`.

use std::collections::HashMap;
use std::future::Future;
use std::pin::Pin;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::Duration;

use rayd_core::clock::Clock;
use rayd_core::code::RandomSource;
use rayd_core::lifecycle::HookPhase;
use rayd_core::session::SandboxSession;
use rayd_core::transfer::{
    BarrierTicket, CancelOutcome, ExportInput, ExportRequest, FailureReason, ImportInput,
    ImportRequest, NewTransfer, PollSchedule, RegistryError, RegistryLimits, SignedHttp,
    TRANSFER_PROBE_BUDGET, TRANSFER_PROGRESS_INTERVAL, TRANSFER_RETRY_DELAY, TransferDirection,
    TransferError, TransferFailure, TransferId, TransferRegistry, TransferSnapshot, plan_parts,
    unix_millis,
};
use tokio::sync::{Notify, Semaphore, watch};
use tokio_util::sync::CancellationToken;

use super::barrier::TransferBarrier;
use super::export::{ExportJob, run_export};
use super::import::{ImportJob, run_import};
use super::watch::{TransferWatch, watch_snapshots};
use crate::filesystem::FilesystemManager;
use crate::lifecycle::{SuspendSignal, SuspendWatch};

/// How often a task parked by `/suspend` looks at the gate again (the VM is
/// frozen most of that time, so the loop costs nothing).
pub const GATE_POLL_INTERVAL: Duration = Duration::from_millis(50);

/// Knobs the integration tests shrink; production uses the defaults.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TransferSettings {
    pub poll: PollSchedule,
    pub limits: RegistryLimits,
    pub retry_delay: Duration,
    pub progress_interval: Duration,
    pub probe_budget: Duration,
    pub gate_poll: Duration,
}

impl Default for TransferSettings {
    fn default() -> Self {
        Self {
            poll: PollSchedule::default(),
            limits: RegistryLimits::default(),
            retry_delay: TRANSFER_RETRY_DELAY,
            progress_interval: TRANSFER_PROGRESS_INTERVAL,
            probe_budget: TRANSFER_PROBE_BUDGET,
            gate_poll: GATE_POLL_INTERVAL,
        }
    }
}

type BoxFuture<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

/// The manager behind a trait object, so the gRPC service is not generic
/// over the HTTP client (tests wire a fake, `main` the hyper adapter).
pub trait TransferBackend: Send + Sync {
    fn start_import(&self, input: ImportInput) -> BoxFuture<'_, Result<TransferId, TransferError>>;
    fn start_export(&self, input: ExportInput) -> BoxFuture<'_, Result<TransferId, TransferError>>;
    fn get(&self, raw_id: &str) -> Result<TransferSnapshot, TransferError>;
    fn watch(&self, raw_id: &str) -> Result<TransferWatch, TransferError>;
    fn cancel(&self, raw_id: &str) -> Result<(), TransferError>;
}

/// What an agent without the transfer client answers: `UNIMPLEMENTED`,
/// like an agent older than the RPCs.
#[derive(Debug, Default, Clone, Copy)]
pub struct UnavailableTransfers;

impl TransferBackend for UnavailableTransfers {
    fn start_import(
        &self,
        _input: ImportInput,
    ) -> BoxFuture<'_, Result<TransferId, TransferError>> {
        Box::pin(std::future::ready(Err(TransferError::Unsupported)))
    }

    fn start_export(
        &self,
        _input: ExportInput,
    ) -> BoxFuture<'_, Result<TransferId, TransferError>> {
        Box::pin(std::future::ready(Err(TransferError::Unsupported)))
    }

    fn get(&self, _raw_id: &str) -> Result<TransferSnapshot, TransferError> {
        Err(TransferError::Unsupported)
    }

    fn watch(&self, _raw_id: &str) -> Result<TransferWatch, TransferError> {
        Err(TransferError::Unsupported)
    }

    fn cancel(&self, _raw_id: &str) -> Result<(), TransferError> {
        Err(TransferError::Unsupported)
    }
}

/// One active transfer's channels: the snapshots its watchers and the
/// barrier follow, the barrier's "poll now", and its cancellation.
pub(crate) struct RecordHandle {
    pub(crate) snapshots: watch::Sender<TransferSnapshot>,
    pub(crate) poll_now: Notify,
    pub(crate) cancel: CancellationToken,
}

/// The registry and the channels of the active transfers; not generic, so
/// the barrier in every service holds it without knowing the HTTP client.
pub(crate) struct TransferHub {
    session: Arc<SandboxSession>,
    clock: Arc<dyn Clock>,
    pub(crate) settings: TransferSettings,
    registry: Mutex<TransferRegistry>,
    handles: Mutex<HashMap<TransferId, Arc<RecordHandle>>>,
    armed: AtomicUsize,
    pub(crate) permits: Arc<Semaphore>,
}

impl TransferHub {
    fn new(session: Arc<SandboxSession>, settings: TransferSettings) -> Self {
        Self {
            clock: session.clock(),
            session,
            registry: Mutex::new(TransferRegistry::new(settings.limits)),
            handles: Mutex::new(HashMap::new()),
            armed: AtomicUsize::new(0),
            permits: Arc::new(Semaphore::new(settings.limits.max_running)),
            settings,
        }
    }

    pub(crate) fn now(&self) -> Duration {
        self.clock.monotonic()
    }

    pub(crate) fn wall_ms(&self) -> i64 {
        unix_millis(self.clock.wall())
    }

    /// An atomic load: the barrier's whole cost when nothing is armed.
    pub(crate) fn armed_count(&self) -> usize {
        self.armed.load(Ordering::SeqCst)
    }

    pub(crate) fn armed_tickets(&self) -> Vec<BarrierTicket> {
        self.registry().armed_tickets()
    }

    pub(crate) fn handle(&self, id: &TransferId) -> Option<Arc<RecordHandle>> {
        self.handles().get(id).cloned()
    }

    /// Suspending and terminating refuse like `Read`/`Write`; before `/run`
    /// there is no sandbox id to bind keys to.
    pub(crate) fn sandbox_id(&self) -> Result<String, TransferError> {
        let phase = self.session.phase();
        if matches!(phase, HookPhase::Suspending | HookPhase::Terminating) {
            return Err(TransferError::NotAccepting { phase });
        }
        self.bound_sandbox_id()
    }

    fn bound_sandbox_id(&self) -> Result<String, TransferError> {
        self.session
            .health()
            .sandbox_id
            .filter(|id| !id.is_empty())
            .ok_or(TransferError::NotRunning)
    }

    fn admit(
        &self,
        random: &dyn RandomSource,
        new: NewTransfer,
    ) -> Result<(TransferId, Arc<RecordHandle>), TransferError> {
        let id = TransferId::generate(random).map_err(|_| TransferError::Internal)?;
        let snapshot = {
            let mut registry = self.registry();
            let snapshot =
                registry
                    .admit(id.clone(), new, self.now())
                    .map_err(|error| match error {
                        RegistryError::Full => TransferError::Full,
                        _ => TransferError::Internal,
                    })?;
            self.armed.store(registry.armed_count(), Ordering::SeqCst);
            snapshot
        };
        let handle = Arc::new(RecordHandle {
            snapshots: watch::Sender::new(snapshot),
            poll_now: Notify::new(),
            cancel: CancellationToken::new(),
        });
        self.handles().insert(id.clone(), handle.clone());
        Ok((id, handle))
    }

    /// One registry change, published to the transfer's watchers; a
    /// terminal state releases its channels. An illegal transition is
    /// logged and changes nothing.
    pub(crate) fn apply(
        &self,
        id: &TransferId,
        change: impl FnOnce(&mut TransferRegistry, Duration) -> Result<TransferSnapshot, RegistryError>,
    ) -> Option<TransferSnapshot> {
        let outcome = {
            let mut registry = self.registry();
            let outcome = change(&mut registry, self.now());
            self.armed.store(registry.armed_count(), Ordering::SeqCst);
            outcome
        };
        match outcome {
            Ok(snapshot) => {
                self.publish(&snapshot);
                Some(snapshot)
            }
            Err(error) => {
                tracing::debug!(transfer_id = %id, reason = %error, "transfer state unchanged");
                None
            }
        }
    }

    fn publish(&self, snapshot: &TransferSnapshot) {
        let handle = if snapshot.phase.is_terminal() {
            self.handles().remove(&snapshot.id)
        } else {
            self.handles().get(&snapshot.id).cloned()
        };
        if let Some(handle) = handle {
            handle.snapshots.send_replace(snapshot.clone());
        }
    }

    pub(crate) fn snapshot(&self, raw_id: &str) -> Result<TransferSnapshot, TransferError> {
        if raw_id.is_empty() {
            return Err(TransferError::UnknownTransfer);
        }
        self.bound_sandbox_id()?;
        self.registry()
            .lookup(raw_id, self.now())
            .map_err(|_| TransferError::UnknownTransfer)
    }

    /// `WatchTransfer`: the live channel of an active transfer, or a
    /// channel that only holds the final snapshot of a finished one.
    fn watch(&self, raw_id: &str) -> Result<TransferWatch, TransferError> {
        self.sandbox_id()?;
        let snapshot = self.snapshot(raw_id)?;
        let receiver = self.handle(&snapshot.id).map_or_else(
            || watch::Sender::new(snapshot).subscribe(),
            |handle| handle.snapshots.subscribe(),
        );
        Ok(watch_snapshots(receiver, self.settings.progress_interval))
    }

    /// Idempotent: a finished transfer stays as it is.
    fn cancel(&self, raw_id: &str) -> Result<(), TransferError> {
        if raw_id.is_empty() {
            return Err(TransferError::UnknownTransfer);
        }
        self.bound_sandbox_id()?;
        let outcome = {
            let mut registry = self.registry();
            let outcome = registry.cancel(
                raw_id,
                TransferFailure::of(FailureReason::Cancelled),
                self.now(),
            );
            self.armed.store(registry.armed_count(), Ordering::SeqCst);
            outcome
        };
        match outcome {
            Ok(CancelOutcome::Cancelled(snapshot)) => {
                if let Some(handle) = self.handle(&snapshot.id) {
                    handle.cancel.cancel();
                }
                tracing::info!(
                    transfer_id = %snapshot.id,
                    direction = snapshot.direction.as_str(),
                    phase = snapshot.phase.as_str(),
                    "transfer cancelled"
                );
                self.publish(&snapshot);
                Ok(())
            }
            Ok(CancelOutcome::AlreadyFinished(_)) => Ok(()),
            Err(_) => Err(TransferError::UnknownTransfer),
        }
    }

    /// `/terminate` seen by a task: this transfer ends `Cancelled`.
    pub(crate) fn terminate(&self, id: &TransferId) {
        self.apply(id, |registry, now| {
            match registry.cancel(
                id.as_str(),
                TransferFailure::of(FailureReason::Cancelled),
                now,
            )? {
                CancelOutcome::Cancelled(snapshot) | CancelOutcome::AlreadyFinished(snapshot) => {
                    Ok(snapshot)
                }
            }
        });
    }

    /// Subscribes to the broadcast, then looks at the phase: a `/suspend`
    /// that lands between the two is seen by one or the other.
    pub(crate) fn interrupts(
        &self,
        suspend: &SuspendSignal,
        cancel: &CancellationToken,
    ) -> Option<Interrupts> {
        let watch = suspend.subscribe();
        self.session.stream_gate().ok().map(|()| Interrupts {
            watch,
            cancel: cancel.clone(),
        })
    }

    /// Parks a task while the sandbox is suspending (the VM is about to
    /// freeze or frozen); `Terminated` once `/terminate` arrived.
    pub(crate) async fn wait_for_gate(&self, cancel: &CancellationToken) -> Gate {
        loop {
            match self.session.stream_gate() {
                Ok(()) => return Gate::Open,
                Err(HookPhase::Terminating) => return Gate::Terminated,
                Err(_) => {}
            }
            tokio::select! {
                () = cancel.cancelled() => return Gate::Cancelled,
                () = tokio::time::sleep(self.settings.gate_poll) => {}
            }
        }
    }

    fn registry(&self) -> MutexGuard<'_, TransferRegistry> {
        self.registry.lock().unwrap_or_else(PoisonError::into_inner)
    }

    fn handles(&self) -> MutexGuard<'_, HashMap<TransferId, Arc<RecordHandle>>> {
        self.handles.lock().unwrap_or_else(PoisonError::into_inner)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Gate {
    Open,
    Terminated,
    Cancelled,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Interrupt {
    Cancelled,
    Suspended,
}

/// What stops an active task: its cancellation, or the suspend broadcast
/// of the generation it subscribed to. Dropping it frees the open-stream
/// slot the `/suspend` hook waits for.
pub(crate) struct Interrupts {
    watch: SuspendWatch,
    cancel: CancellationToken,
}

impl Interrupts {
    pub(crate) async fn fired(&mut self) -> Interrupt {
        tokio::select! {
            () = self.cancel.cancelled() => Interrupt::Cancelled,
            () = self.watch.suspended() => Interrupt::Suspended,
        }
    }
}

/// What every task shares: the hub, the HTTP client, the filesystem and the
/// suspend broadcast.
pub(crate) struct TransferContext<H> {
    pub(crate) hub: Arc<TransferHub>,
    pub(crate) http: H,
    pub(crate) files: Arc<FilesystemManager>,
    pub(crate) suspend: Arc<SuspendSignal>,
}

pub struct TransferManager<H> {
    context: Arc<TransferContext<H>>,
    random: Arc<dyn RandomSource>,
}

impl<H: SignedHttp> TransferManager<H> {
    pub fn new(
        session: Arc<SandboxSession>,
        files: Arc<FilesystemManager>,
        suspend: Arc<SuspendSignal>,
        http: H,
        random: Arc<dyn RandomSource>,
        settings: TransferSettings,
    ) -> Arc<Self> {
        Arc::new(Self {
            context: Arc::new(TransferContext {
                hub: Arc::new(TransferHub::new(session, settings)),
                http,
                files,
                suspend,
            }),
            random,
        })
    }

    /// The handle every RPC consults before touching an armed destination.
    #[must_use]
    pub fn barrier(&self) -> TransferBarrier {
        TransferBarrier::new(self.context.hub.clone())
    }

    async fn import(&self, input: ImportInput) -> Result<TransferId, TransferError> {
        let hub = &self.context.hub;
        let sandbox_id = hub.sandbox_id()?;
        let request = ImportRequest::validate(input, &sandbox_id, hub.wall_ms())?;
        let identity = self.context.files.identity(request.user.as_deref())?;
        let destination = self
            .context
            .files
            .import_destination(&identity, &request.path)
            .await?;
        let (id, handle) = hub.admit(
            self.random.as_ref(),
            NewTransfer {
                direction: TransferDirection::Import,
                armed: request.wait_for_object,
                bytes_total: 0,
                entry: None,
                request_path: destination.path.as_str().to_owned(),
                destination: destination.canonical.clone(),
            },
        )?;
        tracing::info!(
            transfer_id = %id,
            direction = TransferDirection::Import.as_str(),
            armed = request.wait_for_object,
            "transfer accepted"
        );
        tokio::spawn(run_import(
            self.context.clone(),
            ImportJob {
                id: id.clone(),
                handle,
                request,
                identity,
                admitted_at: hub.now(),
            },
        ));
        Ok(id)
    }

    async fn export(&self, input: ExportInput) -> Result<TransferId, TransferError> {
        let hub = &self.context.hub;
        let sandbox_id = hub.sandbox_id()?;
        let request = ExportRequest::validate(input, &sandbox_id, hub.wall_ms())?;
        let identity = self.context.files.identity(request.user.as_deref())?;
        let source = self
            .context
            .files
            .export_source(&identity, &request.path)
            .await?;
        let plan = plan_parts(source.entry.size, &request.target)?;
        let (id, handle) = hub.admit(
            self.random.as_ref(),
            NewTransfer {
                direction: TransferDirection::Export,
                armed: false,
                bytes_total: source.entry.size,
                entry: Some(source.entry.clone()),
                request_path: source.path.as_str().to_owned(),
                destination: String::new(),
            },
        )?;
        tracing::info!(
            transfer_id = %id,
            direction = TransferDirection::Export.as_str(),
            bytes = source.entry.size,
            parts = plan.len(),
            "transfer accepted"
        );
        tokio::spawn(run_export(
            self.context.clone(),
            ExportJob {
                id: id.clone(),
                handle,
                target: request.target,
                plan,
                file: Arc::from(source.file),
                size: source.entry.size,
            },
        ));
        Ok(id)
    }
}

impl<H: SignedHttp> TransferBackend for TransferManager<H> {
    fn start_import(&self, input: ImportInput) -> BoxFuture<'_, Result<TransferId, TransferError>> {
        Box::pin(self.import(input))
    }

    fn start_export(&self, input: ExportInput) -> BoxFuture<'_, Result<TransferId, TransferError>> {
        Box::pin(self.export(input))
    }

    fn get(&self, raw_id: &str) -> Result<TransferSnapshot, TransferError> {
        self.context.hub.snapshot(raw_id)
    }

    fn watch(&self, raw_id: &str) -> Result<TransferWatch, TransferError> {
        self.context.hub.watch(raw_id)
    }

    fn cancel(&self, raw_id: &str) -> Result<(), TransferError> {
        self.context.hub.cancel(raw_id)
    }
}

/// Lowercase hex of a digest.
pub(crate) fn lower_hex(bytes: &[u8]) -> String {
    use std::fmt::Write as _;
    bytes
        .iter()
        .fold(String::with_capacity(bytes.len() * 2), |mut out, byte| {
            let _ = write!(out, "{byte:02x}");
            out
        })
}
