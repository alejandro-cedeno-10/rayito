//! `PersistenceManager`: `checkpoint()` and `restore()` run the domain's
//! `prepare_*` inline (a failure there is the gRPC status) and hand the
//! rest to a task that emits `Started`, `Progress` at most once per tick
//! and only on change, then `Done` or `Error`. Dropping the returned
//! stream (client gone, `/suspend`) drops the task's channel, which stops
//! the blocking thread and aborts the multipart upload; the lease is
//! released with the task in every path.

use std::future::Future;
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use rayd_core::persistence::{
    BlockingRunner, CheckpointDone, CheckpointRequestInfo, Counters, HomeArchiver, JoinFailure,
    ManifestMeta, ObjectStore, PersistenceDeps, PersistenceError, PersistenceGate,
    PreparedCheckpoint, PreparedRestore, ProgressSampler, RestoreDone, RestoreRequestInfo,
    Snapshot, prepare_checkpoint, prepare_restore, run_checkpoint, run_restore,
};
use rayd_core::process::{UserLookup, UserPolicy};
use rayd_core::session::SandboxSession;
use tokio::sync::mpsc;
use tokio_stream::wrappers::ReceiverStream;

use super::channels::{chunk_channel, part_channel};
use crate::adapters::{PlatformHomeArchiver, S3ObjectStore, SpawnPlatform};

pub const DEFAULT_PROGRESS_INTERVAL: Duration = Duration::from_secs(1);
/// Events in flight between the flow task and the response stream.
const EVENT_CAPACITY: usize = 4;

/// `spawn_blocking` behind the domain's port.
#[derive(Debug, Clone, Copy, Default)]
pub struct TokioRunner;

impl BlockingRunner for TokioRunner {
    fn run<T, F>(&self, job: F) -> impl Future<Output = Result<T, JoinFailure>> + Send
    where
        T: Send + 'static,
        F: FnOnce() -> T + Send + 'static,
    {
        let handle = tokio::task::spawn_blocking(job);
        async move { handle.await.map_err(|_| JoinFailure) }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PersistenceSettings {
    pub progress_interval: Duration,
    pub part_bytes: usize,
}

impl Default for PersistenceSettings {
    fn default() -> Self {
        Self {
            progress_interval: DEFAULT_PROGRESS_INTERVAL,
            part_bytes: rayd_core::persistence::PART_BYTES,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CheckpointItem {
    Started { files: u64, bytes: u64 },
    Progress(Snapshot),
    Done(CheckpointDone),
    Error(PersistenceError),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RestoreItem {
    Started { archive_bytes: u64, files: u64 },
    Progress(Snapshot),
    Done(RestoreDone),
    Error(PersistenceError),
}

pub type CheckpointStream = ReceiverStream<CheckpointItem>;
pub type RestoreStream = ReceiverStream<RestoreItem>;

type BoxFuture<'a, T> = std::pin::Pin<Box<dyn Future<Output = T> + Send + 'a>>;

/// The manager behind a trait object, so the gRPC service is not generic
/// over the store and the archiver (tests wire fakes, `main` the S3 and
/// tar adapters).
pub trait PersistenceBackend: Send + Sync {
    fn checkpoint(
        &self,
        request: CheckpointRequestInfo,
    ) -> BoxFuture<'_, Result<CheckpointStream, PersistenceError>>;

    fn restore(
        &self,
        request: RestoreRequestInfo,
    ) -> BoxFuture<'_, Result<RestoreStream, PersistenceError>>;
}

impl<S, A> PersistenceBackend for PersistenceManager<S, A>
where
    S: ObjectStore,
    A: HomeArchiver + 'static,
{
    fn checkpoint(
        &self,
        request: CheckpointRequestInfo,
    ) -> BoxFuture<'_, Result<CheckpointStream, PersistenceError>> {
        Box::pin(PersistenceManager::checkpoint(self, request))
    }

    fn restore(
        &self,
        request: RestoreRequestInfo,
    ) -> BoxFuture<'_, Result<RestoreStream, PersistenceError>> {
        Box::pin(PersistenceManager::restore(self, request))
    }
}

/// A backend for hosts without a store wired (the pre-M7 integration
/// harnesses): both RPCs answer `UNIMPLEMENTED`.
#[derive(Debug, Default)]
pub struct UnavailablePersistence;

impl PersistenceBackend for UnavailablePersistence {
    fn checkpoint(
        &self,
        _request: CheckpointRequestInfo,
    ) -> BoxFuture<'_, Result<CheckpointStream, PersistenceError>> {
        Box::pin(std::future::ready(Err(PersistenceError::Unsupported)))
    }

    fn restore(
        &self,
        _request: RestoreRequestInfo,
    ) -> BoxFuture<'_, Result<RestoreStream, PersistenceError>> {
        Box::pin(std::future::ready(Err(PersistenceError::Unsupported)))
    }
}

pub struct PersistenceManager<S, A> {
    deps: Arc<PersistenceDeps<S, A, TokioRunner>>,
    session: Arc<SandboxSession>,
    settings: PersistenceSettings,
}

pub type PlatformPersistenceManager = PersistenceManager<S3ObjectStore, PlatformHomeArchiver>;

impl<S, A> PersistenceManager<S, A>
where
    S: ObjectStore,
    A: HomeArchiver + 'static,
{
    pub fn new(
        session: Arc<SandboxSession>,
        store: Arc<S>,
        archiver: Arc<A>,
        lookup: Arc<dyn UserLookup>,
        policy: UserPolicy,
        default_region: Option<String>,
        settings: PersistenceSettings,
    ) -> Arc<Self> {
        Arc::new(Self {
            deps: Arc::new(PersistenceDeps {
                store,
                archiver,
                runner: TokioRunner,
                gate: PersistenceGate::new(),
                lookup,
                policy,
                default_region,
            }),
            session,
            settings,
        })
    }

    #[must_use]
    pub fn is_busy(&self) -> bool {
        self.deps.gate.is_busy()
    }

    /// Everything up to and including the pre-walk; the stream's first
    /// item is `Started`.
    pub async fn checkpoint(
        &self,
        request: CheckpointRequestInfo,
    ) -> Result<CheckpointStream, PersistenceError> {
        self.stream_gate()?;
        let default_user = self.session.spawn_defaults().user;
        let prepared = prepare_checkpoint(&self.deps, request, default_user.as_deref()).await?;
        let (sender, receiver) = mpsc::channel(EVENT_CAPACITY);
        let deps = self.deps.clone();
        let settings = self.settings;
        let meta = self.manifest_meta();
        tokio::spawn(async move {
            let started = CheckpointItem::Started {
                files: prepared.started_files,
                bytes: prepared.started_bytes,
            };
            if sender.send(started).await.is_err() {
                return;
            }
            let counters = Arc::new(Counters::new());
            let flow = checkpoint_flow(&deps, &prepared, settings, counters.clone(), &meta);
            let outcome = drive(flow, &sender, &counters, settings.progress_interval, |s| {
                CheckpointItem::Progress(s)
            })
            .await;
            let item = match outcome {
                Some(Ok(done)) => CheckpointItem::Done(done),
                Some(Err(error)) => CheckpointItem::Error(error),
                None => return,
            };
            log_checkpoint(&item);
            let _ = sender.send(item).await;
            drop(prepared);
        });
        Ok(ReceiverStream::new(receiver))
    }

    /// Everything up to and including the manifest; the stream's first
    /// item is `Started`.
    pub async fn restore(
        &self,
        request: RestoreRequestInfo,
    ) -> Result<RestoreStream, PersistenceError> {
        self.stream_gate()?;
        let default_user = self.session.spawn_defaults().user;
        let prepared = prepare_restore(&self.deps, request, default_user.as_deref()).await?;
        let (sender, receiver) = mpsc::channel(EVENT_CAPACITY);
        let deps = self.deps.clone();
        let interval = self.settings.progress_interval;
        tokio::spawn(async move {
            let started = RestoreItem::Started {
                archive_bytes: prepared.manifest.archive_bytes,
                files: prepared.manifest.files,
            };
            if sender.send(started).await.is_err() {
                return;
            }
            let counters = Arc::new(Counters::new());
            let flow = restore_flow(&deps, &prepared, counters.clone());
            let outcome = drive(flow, &sender, &counters, interval, RestoreItem::Progress).await;
            let item = match outcome {
                Some(Ok(done)) => RestoreItem::Done(done),
                Some(Err(error)) => RestoreItem::Error(error),
                None => return,
            };
            log_restore(&item);
            let _ = sender.send(item).await;
            drop(prepared);
        });
        Ok(ReceiverStream::new(receiver))
    }

    fn stream_gate(&self) -> Result<(), PersistenceError> {
        self.session
            .stream_gate()
            .map_err(|_| PersistenceError::Suspending)
    }

    fn manifest_meta(&self) -> ManifestMeta {
        let health = self.session.health();
        ManifestMeta {
            sandbox_id: health.sandbox_id.unwrap_or_default(),
            agent_version: health.agent_version,
            created_at: iso8601_utc(self.session.clock().wall()),
        }
    }
}

async fn checkpoint_flow<S, A>(
    deps: &PersistenceDeps<S, A, TokioRunner>,
    prepared: &PreparedCheckpoint,
    settings: PersistenceSettings,
    counters: Arc<Counters>,
    meta: &ManifestMeta,
) -> Result<CheckpointDone, PersistenceError>
where
    S: ObjectStore,
    A: HomeArchiver + 'static,
{
    let (sink, parts) = part_channel(settings.part_bytes);
    run_checkpoint(deps, prepared, sink, parts, counters, meta).await
}

async fn restore_flow<S, A>(
    deps: &PersistenceDeps<S, A, TokioRunner>,
    prepared: &PreparedRestore,
    counters: Arc<Counters>,
) -> Result<RestoreDone, PersistenceError>
where
    S: ObjectStore,
    A: HomeArchiver + 'static,
{
    let (sink, source) = chunk_channel();
    run_restore(deps, prepared, sink, source, counters).await
}

/// Runs the flow while sampling the counters every `interval`; `None`
/// when the receiver went away, which drops the flow mid-way (the drop
/// is what cancels the blocking thread and aborts the upload).
async fn drive<T, F, I>(
    flow: F,
    sender: &mpsc::Sender<I>,
    counters: &Counters,
    interval: Duration,
    progress: impl Fn(Snapshot) -> I,
) -> Option<T>
where
    F: Future<Output = T>,
{
    let mut flow = std::pin::pin!(flow);
    let mut ticks = tokio::time::interval(interval);
    ticks.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    ticks.tick().await;
    let mut sampler = ProgressSampler::new();
    loop {
        tokio::select! {
            outcome = &mut flow => return Some(outcome),
            () = sender.closed() => return None,
            _ = ticks.tick() => {
                if let Some(snapshot) = sampler.sample(counters)
                    && sender.send(progress(snapshot)).await.is_err()
                {
                    return None;
                }
            }
        }
    }
}

fn log_checkpoint(item: &CheckpointItem) {
    match item {
        CheckpointItem::Done(done) => tracing::info!(
            rpc = "Checkpoint",
            outcome = "done",
            files = done.files,
            bytes_read = done.bytes_read,
            archive_bytes = done.archive_bytes,
            skipped = done.skipped,
            parts = done.parts,
            duration_ms = done.duration_ms,
            "checkpoint finished"
        ),
        CheckpointItem::Error(error) => tracing::warn!(
            rpc = "Checkpoint",
            outcome = error.stream_code(),
            reason = %error,
            "checkpoint failed"
        ),
        CheckpointItem::Started { .. } | CheckpointItem::Progress(_) => {}
    }
}

fn log_restore(item: &RestoreItem) {
    match item {
        RestoreItem::Done(done) => tracing::info!(
            rpc = "Restore",
            outcome = "done",
            files = done.files,
            bytes_written = done.bytes_written,
            archive_bytes = done.archive_bytes,
            skipped = done.skipped,
            duration_ms = done.duration_ms,
            "restore finished"
        ),
        RestoreItem::Error(error) => tracing::warn!(
            rpc = "Restore",
            outcome = error.stream_code(),
            reason = %error,
            "restore failed"
        ),
        RestoreItem::Started { .. } | RestoreItem::Progress(_) => {}
    }
}

/// `2026-09-16T12:00:00Z` from a wall-clock instant, without a date crate.
#[must_use]
pub fn iso8601_utc(instant: SystemTime) -> String {
    let seconds = instant
        .duration_since(UNIX_EPOCH)
        .map_or(0, |elapsed| elapsed.as_secs());
    let days = i64::try_from(seconds / 86_400).unwrap_or(0);
    let rest = seconds % 86_400;
    let (year, month, day) = civil_from_days(days);
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}Z",
        rest / 3600,
        (rest % 3600) / 60,
        rest % 60
    )
}

/// Howard Hinnant's `civil_from_days` (public domain).
fn civil_from_days(days: i64) -> (i64, u32, u32) {
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let year = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = u32::try_from(doy - (153 * mp + 2) / 5 + 1).unwrap_or(1);
    let month = u32::try_from(if mp < 10 { mp + 3 } else { mp - 9 }).unwrap_or(1);
    (if month <= 2 { year + 1 } else { year }, month, day)
}

/// Builds the manager for the host `rayd` runs on.
pub fn platform_persistence_manager(
    session: Arc<SandboxSession>,
    platform: &SpawnPlatform,
    policy: UserPolicy,
    store: Arc<S3ObjectStore>,
    default_region: Option<String>,
) -> Arc<PlatformPersistenceManager> {
    PersistenceManager::new(
        session,
        store,
        Arc::new(PlatformHomeArchiver::new(platform.identity_switch)),
        platform.lookup.clone(),
        policy,
        default_region,
        PersistenceSettings::default(),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn iso8601_renders_known_instants() {
        assert_eq!(iso8601_utc(UNIX_EPOCH), "1970-01-01T00:00:00Z");
        let instant = UNIX_EPOCH + Duration::from_hours(497_100);
        assert_eq!(iso8601_utc(instant), "2026-09-16T12:00:00Z");
        let leap = UNIX_EPOCH + Duration::from_hours(264_384);
        assert_eq!(iso8601_utc(leap), "2000-02-29T00:00:00Z");
    }
}
