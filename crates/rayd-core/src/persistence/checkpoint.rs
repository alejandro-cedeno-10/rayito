//! `Checkpoint` in two halves (design D7/D8). `prepare_checkpoint` runs
//! before the first message and decides the gRPC status: location and
//! exclude validation, the identity, the lease, the credential probe and
//! the pre-walk. `run_checkpoint` runs behind the `started` message: the
//! archive on a blocking thread feeding parts to the multipart upload,
//! then the manifest, whose presence in S3 means the archive is whole.

use std::future::Future;
use std::sync::Arc;
use std::time::Instant;

use bytes::Bytes;

use super::error::PersistenceError;
use super::manifest::{Manifest, ManifestInput};
use super::plan::{ArchivePlan, ExcludeList};
use super::ports::{
    ArchiveSink, ArchiveSummary, BlockingRunner, HomeArchiver, ObjectStore, PartSource,
};
use super::progress::Counters;
use super::{
    ARCHIVE_CONTENT_TYPE, LocationRequest, MANIFEST_CONTENT_TYPE, PersistenceDeps,
    PersistenceLease, ResolvedLocation, resolve_home_identity, resolve_location,
};

/// The request as the gRPC layer hands it over: plain data.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct CheckpointRequestInfo {
    pub location: LocationRequest,
    pub user: Option<String>,
    pub exclude: Vec<String>,
}

/// What `CheckpointDone` carries.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct CheckpointDone {
    pub files: u64,
    pub bytes_read: u64,
    pub archive_bytes: u64,
    pub sha256: String,
    pub skipped: u64,
    pub parts: u32,
    pub duration_ms: u32,
}

/// Everything the second half needs, with the lease already held.
#[derive(Debug)]
pub struct PreparedCheckpoint {
    pub location: ResolvedLocation,
    pub plan: ArchivePlan,
    pub username: String,
    pub started_files: u64,
    pub started_bytes: u64,
    pub lease: PersistenceLease,
    pub started_at: Instant,
}

/// Facts the manifest records about the sandbox writing it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ManifestMeta {
    pub sandbox_id: String,
    pub agent_version: String,
    pub created_at: String,
}

pub async fn prepare_checkpoint<S, A, R>(
    deps: &PersistenceDeps<S, A, R>,
    request: CheckpointRequestInfo,
    default_user: Option<&str>,
) -> Result<PreparedCheckpoint, PersistenceError>
where
    S: ObjectStore,
    A: HomeArchiver + 'static,
    R: BlockingRunner,
{
    let location = resolve_location(&request.location, deps.default_region.as_deref())?;
    let excludes = ExcludeList::parse(&request.exclude)?;
    let identity = resolve_home_identity(
        request.user.as_deref(),
        default_user,
        deps.policy,
        deps.lookup.as_ref(),
    )?;
    let username = username_of(request.user.as_deref(), default_user);
    let plan = ArchivePlan::new(identity, username.clone(), excludes);
    let lease = deps.gate.acquire()?;
    deps.store
        .probe_credentials()
        .await
        .map_err(|error| PersistenceError::from_store(&error))?;
    let counted = {
        let archiver = deps.archiver.clone();
        let plan = plan.clone();
        deps.runner
            .run(move || archiver.count(&plan))
            .await
            .map_err(|_| join_failure())?
            .map_err(|error| PersistenceError::from_archive(&error))?
    };
    Ok(PreparedCheckpoint {
        location,
        plan,
        username,
        started_files: counted.files,
        started_bytes: counted.bytes_read,
        lease,
        started_at: Instant::now(),
    })
}

/// The archive thread writes into `sink`; `parts` is the other end of the
/// same channel, consumed by the multipart upload. The manifest is
/// written only after both halves succeeded.
pub async fn run_checkpoint<S, A, R, P>(
    deps: &PersistenceDeps<S, A, R>,
    prepared: &PreparedCheckpoint,
    sink: Box<dyn ArchiveSink>,
    parts: P,
    counters: Arc<Counters>,
    meta: &ManifestMeta,
) -> Result<CheckpointDone, PersistenceError>
where
    S: ObjectStore,
    A: HomeArchiver + 'static,
    R: BlockingRunner,
    P: PartSource,
{
    let archive = spawn_archive(deps, &prepared.plan, sink, counters);
    let uploaded = deps
        .store
        .put_multipart(
            &prepared.location.target,
            &prepared.location.keys.archive,
            ARCHIVE_CONTENT_TYPE,
            parts,
        )
        .await
        .map_err(|error| PersistenceError::from_store(&error))?;
    let summary = archive.await?;
    publish_manifest(deps, prepared, &summary, meta).await?;
    Ok(CheckpointDone {
        files: summary.files,
        bytes_read: summary.bytes_read,
        archive_bytes: summary.archive_bytes,
        sha256: summary.sha256,
        skipped: summary.skipped,
        parts: uploaded.parts,
        duration_ms: elapsed_ms(prepared.started_at),
    })
}

fn spawn_archive<S, A, R>(
    deps: &PersistenceDeps<S, A, R>,
    plan: &ArchivePlan,
    sink: Box<dyn ArchiveSink>,
    counters: Arc<Counters>,
) -> impl Future<Output = Result<ArchiveSummary, PersistenceError>> + Send
where
    S: ObjectStore,
    A: HomeArchiver + 'static,
    R: BlockingRunner,
{
    let archiver = deps.archiver.clone();
    let plan = plan.clone();
    let job = deps
        .runner
        .run(move || archiver.archive(&plan, sink, counters));
    async move {
        job.await
            .map_err(|_| join_failure())?
            .map_err(|error| PersistenceError::from_archive(&error))
    }
}

async fn publish_manifest<S, A, R>(
    deps: &PersistenceDeps<S, A, R>,
    prepared: &PreparedCheckpoint,
    summary: &ArchiveSummary,
    meta: &ManifestMeta,
) -> Result<(), PersistenceError>
where
    S: ObjectStore,
    A: HomeArchiver,
    R: BlockingRunner,
{
    let manifest = Manifest::v1(&ManifestInput {
        sha256: &summary.sha256,
        archive_bytes: summary.archive_bytes,
        files: summary.files,
        bytes: summary.bytes_read,
        skipped: summary.skipped,
        home: prepared.plan.root(),
        user: &prepared.username,
        sandbox_id: &meta.sandbox_id,
        agent_version: &meta.agent_version,
        created_at: &meta.created_at,
        excluded: prepared.plan.excludes.entries(),
    });
    let json = manifest.to_json()?;
    deps.store
        .put(
            &prepared.location.target,
            &prepared.location.keys.manifest,
            Bytes::from(json),
            MANIFEST_CONTENT_TYPE,
        )
        .await
        .map_err(|error| PersistenceError::from_store(&error))
}

pub(super) fn join_failure() -> PersistenceError {
    PersistenceError::Archive {
        operation: "blocking task",
    }
}

pub(super) fn elapsed_ms(since: Instant) -> u32 {
    u32::try_from(since.elapsed().as_millis()).unwrap_or(u32::MAX)
}

pub(super) fn username_of(request_user: Option<&str>, default_user: Option<&str>) -> String {
    request_user
        .filter(|name| !name.is_empty())
        .or(default_user.filter(|name| !name.is_empty()))
        .unwrap_or("user")
        .to_owned()
}

#[cfg(test)]
mod tests {
    use super::super::fake::{
        FakeArchiver, FakeStore, ThreadRunner, VecParts, block_on, deps_with, meta, request,
        sample_archive,
    };
    use super::*;
    use crate::persistence::StoreErrorKind;
    use crate::persistence::ports::StoreError;

    #[test]
    fn checkpoint_uploads_the_archive_then_the_manifest() {
        let store = FakeStore::default();
        let (deps, archiver) = deps_with(store.clone(), FakeArchiver::default(), ThreadRunner);
        archiver.set_home(sample_archive());
        let prepared = block_on(prepare_checkpoint(&deps, request(&["skipme"]), None)).unwrap();
        assert_eq!(prepared.started_files, 3);
        assert_eq!(prepared.started_bytes, 31);
        assert!(deps.gate.is_busy());
        let (sink, parts) = VecParts::pair();
        let done = block_on(run_checkpoint(
            &deps,
            &prepared,
            sink,
            parts,
            Arc::new(Counters::new()),
            &meta(),
        ))
        .unwrap();
        assert_eq!(done.files, 3);
        assert_eq!(done.bytes_read, 31);
        assert!(done.archive_bytes > 0);
        assert_eq!(done.sha256.len(), 64);
        assert_eq!(store.operations(), ["probe", "put_multipart", "put"]);
        let manifest = Manifest::parse(&store.object("rayito/x/manifest.json").unwrap()).unwrap();
        assert_eq!(manifest.sha256, done.sha256);
        assert_eq!(manifest.files, 3);
        assert_eq!(manifest.excluded, ["skipme"]);
        assert_eq!(manifest.user, "user");
        assert_eq!(manifest.home, "/home/user");
        assert_eq!(manifest.sandbox_id, "mvm-test");
        assert_eq!(
            store.object("rayito/x/home.tar.gz").unwrap().len(),
            usize::try_from(done.archive_bytes).unwrap()
        );
        drop(prepared);
        assert!(!deps.gate.is_busy());
    }

    #[test]
    fn store_failure_after_started_aborts_and_writes_no_manifest() {
        let store = FakeStore::default();
        store.fail_multipart(StoreError::new(StoreErrorKind::AccessDenied));
        let (deps, archiver) = deps_with(store.clone(), FakeArchiver::default(), ThreadRunner);
        archiver.set_home(sample_archive());
        let prepared = block_on(prepare_checkpoint(&deps, request(&[]), None)).unwrap();
        let (sink, parts) = VecParts::pair();
        let error = block_on(run_checkpoint(
            &deps,
            &prepared,
            sink,
            parts,
            Arc::new(Counters::new()),
            &meta(),
        ))
        .unwrap_err();
        assert_eq!(error, PersistenceError::AccessDenied);
        assert_eq!(error.stream_code(), "permission_denied");
        assert_eq!(store.aborted(), 1);
        assert!(store.object("rayito/x/manifest.json").is_none());
    }

    #[test]
    fn interrupted_parts_abort_the_upload() {
        let store = FakeStore::default();
        let (deps, archiver) = deps_with(store.clone(), FakeArchiver::default(), ThreadRunner);
        archiver.set_home(sample_archive());
        archiver.fail_archive(super::super::ArchiveError::Cancelled);
        let prepared = block_on(prepare_checkpoint(&deps, request(&[]), None)).unwrap();
        let (sink, parts) = VecParts::pair();
        let error = block_on(run_checkpoint(
            &deps,
            &prepared,
            sink,
            parts,
            Arc::new(Counters::new()),
            &meta(),
        ))
        .unwrap_err();
        assert_eq!(error, PersistenceError::Cancelled);
        assert_eq!(store.aborted(), 1);
        assert!(store.object("rayito/x/manifest.json").is_none());
    }

    #[test]
    fn busy_gate_and_credentials_are_decided_before_started() {
        let store = FakeStore::default();
        let (deps, archiver) = deps_with(store.clone(), FakeArchiver::default(), ThreadRunner);
        archiver.set_home(sample_archive());
        let first = block_on(prepare_checkpoint(&deps, request(&[]), None)).unwrap();
        assert_eq!(
            block_on(prepare_checkpoint(&deps, request(&[]), None)).unwrap_err(),
            PersistenceError::Busy
        );
        drop(first);
        store.fail_credentials(StoreError::new(StoreErrorKind::NoCredentials));
        assert_eq!(
            block_on(prepare_checkpoint(&deps, request(&[]), None)).unwrap_err(),
            PersistenceError::NoCredentials
        );
        assert!(!deps.gate.is_busy(), "a refused probe releases the lease");
        assert_eq!(
            store
                .operations()
                .iter()
                .filter(|op| op.as_str() == "put_multipart")
                .count(),
            0
        );
    }

    #[test]
    fn invalid_input_never_reaches_the_store() {
        let store = FakeStore::default();
        let (deps, _) = deps_with(store.clone(), FakeArchiver::default(), ThreadRunner);
        let mut bad = request(&[]);
        bad.location.bucket = "Bad".to_owned();
        assert!(matches!(
            block_on(prepare_checkpoint(&deps, bad, None)).unwrap_err(),
            PersistenceError::InvalidBucket(_)
        ));
        let mut root = request(&[]);
        root.user = Some("root".to_owned());
        assert_eq!(
            block_on(prepare_checkpoint(&deps, root, None)).unwrap_err(),
            PersistenceError::RootNotAllowed
        );
        let mut excludes = request(&[]);
        excludes.exclude = vec!["../x".to_owned()];
        assert!(matches!(
            block_on(prepare_checkpoint(&deps, excludes, None)).unwrap_err(),
            PersistenceError::InvalidExclude(_)
        ));
        assert!(store.operations().is_empty());
        assert!(!deps.gate.is_busy());
    }
}
