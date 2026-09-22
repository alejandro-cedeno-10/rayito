//! `Restore` in two halves (design D4/D7/D8). `prepare_restore` runs
//! before the first message: validation, identity, lease, credentials and
//! the manifest (`NoSuchKey` is `NOT_FOUND` here, never later).
//! `run_restore` runs behind `started`: the archive body is pulled chunk
//! by chunk into the unpack thread and the compressed checksum is checked
//! against the manifest at the end.

use std::future::Future;
use std::io::Read;
use std::sync::Arc;
use std::time::Instant;

use super::checkpoint::{elapsed_ms, join_failure, username_of};
use super::error::PersistenceError;
use super::manifest::{MANIFEST_MAX_BYTES, Manifest};
use super::plan::{ArchivePlan, ExcludeList};
use super::ports::{
    BlockingRunner, ChunkSink, ExtractSummary, HomeArchiver, ObjectBody, ObjectStore,
    StoreErrorKind,
};
use super::progress::Counters;
use super::{
    LocationRequest, PersistenceDeps, PersistenceLease, ResolvedLocation, resolve_home_identity,
    resolve_location,
};

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct RestoreRequestInfo {
    pub location: LocationRequest,
    pub user: Option<String>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct RestoreDone {
    pub files: u64,
    pub bytes_written: u64,
    pub archive_bytes: u64,
    pub sha256: String,
    pub skipped: u64,
    pub duration_ms: u32,
}

#[derive(Debug)]
pub struct PreparedRestore {
    pub location: ResolvedLocation,
    pub plan: ArchivePlan,
    pub manifest: Manifest,
    pub lease: PersistenceLease,
    pub started_at: Instant,
}

pub async fn prepare_restore<S, A, R>(
    deps: &PersistenceDeps<S, A, R>,
    request: RestoreRequestInfo,
    default_user: Option<&str>,
) -> Result<PreparedRestore, PersistenceError>
where
    S: ObjectStore,
    A: HomeArchiver,
    R: BlockingRunner,
{
    let location = resolve_location(&request.location, deps.default_region.as_deref())?;
    let identity = resolve_home_identity(
        request.user.as_deref(),
        default_user,
        deps.policy,
        deps.lookup.as_ref(),
    )?;
    let username = username_of(request.user.as_deref(), default_user);
    let plan = ArchivePlan::new(identity, username, ExcludeList::default());
    let lease = deps.gate.acquire()?;
    deps.store
        .probe_credentials()
        .await
        .map_err(|error| PersistenceError::from_store(&error))?;
    let manifest = fetch_manifest(deps.store.as_ref(), &location).await?;
    Ok(PreparedRestore {
        location,
        plan,
        manifest,
        lease,
        started_at: Instant::now(),
    })
}

async fn fetch_manifest<S: ObjectStore>(
    store: &S,
    location: &ResolvedLocation,
) -> Result<Manifest, PersistenceError> {
    let mut body = store
        .get(&location.target, &location.keys.manifest)
        .await
        .map_err(|error| PersistenceError::from_store(&error))?;
    let mut bytes = Vec::new();
    while let Some(chunk) = body
        .next_chunk()
        .await
        .map_err(|error| PersistenceError::from_store(&error))?
    {
        bytes.extend_from_slice(&chunk);
        if bytes.len() > MANIFEST_MAX_BYTES {
            return Err(PersistenceError::InvalidManifest(
                super::error::ManifestRejection::TooLarge,
            ));
        }
    }
    Manifest::parse(&bytes)
}

/// `source` is the reading end of the channel `sink` feeds; the unpack
/// thread owns it. A download that stops early leaves the home partially
/// restored (documented; the recovery is another restore).
pub async fn run_restore<S, A, R, K>(
    deps: &PersistenceDeps<S, A, R>,
    prepared: &PreparedRestore,
    sink: K,
    source: Box<dyn Read + Send>,
    counters: Arc<Counters>,
) -> Result<RestoreDone, PersistenceError>
where
    S: ObjectStore,
    A: HomeArchiver + 'static,
    R: BlockingRunner,
    K: ChunkSink,
{
    let body = deps
        .store
        .get(&prepared.location.target, &prepared.location.keys.archive)
        .await
        .map_err(|error| match error.kind {
            StoreErrorKind::NotFound => PersistenceError::ArchiveMissing,
            _ => PersistenceError::from_store(&error),
        })?;
    let extract = spawn_extract(deps, &prepared.plan, source, counters.clone());
    let pumped = pump(body, sink, &counters).await;
    let summary = match pumped {
        Ok(()) => extract.await?,
        Err(PersistenceError::Cancelled) => {
            extract.await?;
            return Err(PersistenceError::Cancelled);
        }
        Err(error) => return Err(error),
    };
    verify(&prepared.manifest, &summary)?;
    Ok(RestoreDone {
        files: summary.files,
        bytes_written: summary.bytes_written,
        archive_bytes: summary.archive_bytes,
        sha256: summary.sha256,
        skipped: summary.skipped,
        duration_ms: elapsed_ms(prepared.started_at),
    })
}

fn spawn_extract<S, A, R>(
    deps: &PersistenceDeps<S, A, R>,
    plan: &ArchivePlan,
    source: Box<dyn Read + Send>,
    counters: Arc<Counters>,
) -> impl Future<Output = Result<ExtractSummary, PersistenceError>> + Send
where
    S: ObjectStore,
    A: HomeArchiver + 'static,
    R: BlockingRunner,
{
    let archiver = deps.archiver.clone();
    let plan = plan.clone();
    let job = deps
        .runner
        .run(move || archiver.extract(&plan, source, counters));
    async move {
        job.await
            .map_err(|_| join_failure())?
            .map_err(|error| PersistenceError::from_archive(&error))
    }
}

async fn pump<B: ObjectBody, K: ChunkSink>(
    mut body: B,
    mut sink: K,
    counters: &Counters,
) -> Result<(), PersistenceError> {
    while let Some(chunk) = body
        .next_chunk()
        .await
        .map_err(|error| PersistenceError::from_store(&error))?
    {
        counters.add_bytes_downloaded(chunk.len() as u64);
        sink.push(chunk)
            .await
            .map_err(|_| PersistenceError::Cancelled)?;
    }
    sink.finish().await;
    Ok(())
}

fn verify(manifest: &Manifest, summary: &ExtractSummary) -> Result<(), PersistenceError> {
    if !summary.sha256.eq_ignore_ascii_case(&manifest.sha256) {
        return Err(PersistenceError::ChecksumMismatch);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::super::fake::{
        FakeArchiver, FakeStore, ThreadRunner, VecChunks, block_on, deps_with, restore_request,
        sample_archive, seed_checkpoint,
    };
    use super::*;
    use crate::persistence::StatusKind;
    use crate::persistence::ports::StoreError;

    #[test]
    fn restore_downloads_unpacks_and_verifies() {
        let store = FakeStore::default();
        let (deps, archiver) = deps_with(store.clone(), FakeArchiver::default(), ThreadRunner);
        let (archive, sha) = seed_checkpoint(&store, &sample_archive());
        let prepared = block_on(prepare_restore(&deps, restore_request(), None)).unwrap();
        assert_eq!(prepared.manifest.sha256, sha);
        assert_eq!(prepared.manifest.files, 3);
        let (sink, source) = VecChunks::pair();
        let counters = Arc::new(Counters::new());
        let done = block_on(run_restore(
            &deps,
            &prepared,
            sink,
            source,
            counters.clone(),
        ))
        .unwrap();
        assert_eq!(done.files, 3);
        assert_eq!(done.sha256, sha);
        assert_eq!(done.archive_bytes, archive.len() as u64);
        assert_eq!(counters.snapshot().bytes_downloaded, archive.len() as u64);
        assert_eq!(archiver.extracted(), sample_archive());
        assert_eq!(store.operations(), ["probe", "get", "get"]);
    }

    #[test]
    fn missing_manifest_is_not_found_before_started() {
        let store = FakeStore::default();
        let (deps, _) = deps_with(store.clone(), FakeArchiver::default(), ThreadRunner);
        let error = block_on(prepare_restore(&deps, restore_request(), None)).unwrap_err();
        assert_eq!(error, PersistenceError::NotFound);
        assert_eq!(error.status_kind(), StatusKind::NotFound);
        assert!(!deps.gate.is_busy());
    }

    #[test]
    fn checksum_mismatch_is_internal_after_started() {
        let store = FakeStore::default();
        let (deps, _) = deps_with(store.clone(), FakeArchiver::default(), ThreadRunner);
        let (_, _) = seed_checkpoint(&store, &sample_archive());
        store.corrupt_archive();
        let prepared = block_on(prepare_restore(&deps, restore_request(), None)).unwrap();
        let (sink, source) = VecChunks::pair();
        let error = block_on(run_restore(
            &deps,
            &prepared,
            sink,
            source,
            Arc::new(Counters::new()),
        ))
        .unwrap_err();
        assert_eq!(error, PersistenceError::ChecksumMismatch);
        assert_eq!(error.stream_code(), "internal");
    }

    #[test]
    fn archive_missing_although_manifest_exists_is_not_found_after_started() {
        let store = FakeStore::default();
        let (deps, _) = deps_with(store.clone(), FakeArchiver::default(), ThreadRunner);
        seed_checkpoint(&store, &sample_archive());
        store.remove("rayito/x/home.tar.gz");
        let prepared = block_on(prepare_restore(&deps, restore_request(), None)).unwrap();
        let (sink, source) = VecChunks::pair();
        let error = block_on(run_restore(
            &deps,
            &prepared,
            sink,
            source,
            Arc::new(Counters::new()),
        ))
        .unwrap_err();
        assert_eq!(error, PersistenceError::ArchiveMissing);
        assert_eq!(error.stream_code(), "not_found");
    }

    #[test]
    fn extract_failure_wins_over_the_closed_sink() {
        let store = FakeStore::default();
        let (deps, archiver) = deps_with(store.clone(), FakeArchiver::default(), ThreadRunner);
        seed_checkpoint(&store, &sample_archive());
        archiver.fail_extract(crate::persistence::ArchiveError::DiskFull);
        let prepared = block_on(prepare_restore(&deps, restore_request(), None)).unwrap();
        let (sink, source) = VecChunks::pair();
        let error = block_on(run_restore(
            &deps,
            &prepared,
            sink,
            source,
            Arc::new(Counters::new()),
        ))
        .unwrap_err();
        assert_eq!(error, PersistenceError::DiskFull);
    }

    #[test]
    fn download_failure_after_started_is_reported_as_store() {
        let store = FakeStore::default();
        let (deps, _) = deps_with(store.clone(), FakeArchiver::default(), ThreadRunner);
        seed_checkpoint(&store, &sample_archive());
        let prepared = block_on(prepare_restore(&deps, restore_request(), None)).unwrap();
        store.fail_body_after(1, StoreError::new(StoreErrorKind::Other));
        let (sink, source) = VecChunks::pair();
        let error = block_on(run_restore(
            &deps,
            &prepared,
            sink,
            source,
            Arc::new(Counters::new()),
        ))
        .unwrap_err();
        assert_eq!(error, PersistenceError::Store);
    }
}
