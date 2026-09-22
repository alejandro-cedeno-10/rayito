//! Test doubles for the gRPC persistence tests: an in-memory object store
//! with scripted failures and a slow mode (so a client can cancel
//! mid-upload), and an archiver over a plain serialisation that works on
//! any host. Mirrors `rayd_core::persistence::fake` without the domain
//! crate's test-only visibility.
#![allow(
    clippy::must_use_candidate,
    clippy::missing_panics_doc,
    clippy::unused_async_trait_impl
)]

use std::collections::HashMap;
use std::io::{Read, Write};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::Duration;

use bytes::Bytes;
use rayd_core::persistence::{
    ArchiveError, ArchivePlan, ArchiveSink, ArchiveSummary, Counters, ExtractSummary, HomeArchiver,
    ObjectBody, ObjectStore, PartSource, PutSummary, StoreError, StoreErrorKind, StoreTarget,
};
use sha2::{Digest, Sha256};

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

pub fn hex(bytes: &[u8]) -> String {
    use std::fmt::Write as _;
    bytes.iter().fold(String::new(), |mut acc, byte| {
        let _ = write!(acc, "{byte:02x}");
        acc
    })
}

pub type Home = Vec<(String, Vec<u8>)>;

pub fn serialise(home: &Home) -> Vec<u8> {
    let mut out = Vec::new();
    for (name, data) in home {
        out.extend_from_slice(name.as_bytes());
        out.push(b'\n');
        out.extend_from_slice(data.len().to_string().as_bytes());
        out.push(b'\n');
        out.extend_from_slice(data);
    }
    out
}

fn deserialise(bytes: &[u8]) -> Home {
    let mut home = Vec::new();
    let mut rest = bytes;
    while let Some(cut) = rest.iter().position(|byte| *byte == b'\n') {
        let name = String::from_utf8_lossy(&rest[..cut]).into_owned();
        rest = &rest[cut + 1..];
        let cut = rest
            .iter()
            .position(|byte| *byte == b'\n')
            .unwrap_or(rest.len());
        let len: usize = String::from_utf8_lossy(&rest[..cut]).parse().unwrap_or(0);
        rest = &rest[(cut + 1).min(rest.len())..];
        let data = rest[..len.min(rest.len())].to_vec();
        rest = &rest[len.min(rest.len())..];
        home.push((name, data));
    }
    home
}

#[derive(Default)]
pub struct FakeArchiver {
    home: Mutex<Home>,
    extracted: Mutex<Home>,
}

impl FakeArchiver {
    pub fn set_home(&self, home: Home) {
        *lock(&self.home) = home;
    }

    pub fn extracted(&self) -> Home {
        lock(&self.extracted).clone()
    }
}

impl HomeArchiver for FakeArchiver {
    fn count(&self, plan: &ArchivePlan) -> Result<ArchiveSummary, ArchiveError> {
        let home = lock(&self.home);
        let kept: Vec<_> = home
            .iter()
            .filter(|(name, _)| !plan.should_skip(name))
            .collect();
        Ok(ArchiveSummary {
            files: kept.len() as u64,
            bytes_read: kept.iter().map(|(_, data)| data.len() as u64).sum(),
            ..ArchiveSummary::default()
        })
    }

    fn archive(
        &self,
        plan: &ArchivePlan,
        mut sink: Box<dyn ArchiveSink>,
        counters: Arc<Counters>,
    ) -> Result<ArchiveSummary, ArchiveError> {
        let kept: Home = lock(&self.home)
            .iter()
            .filter(|(name, _)| !plan.should_skip(name))
            .cloned()
            .collect();
        let bytes = serialise(&kept);
        for chunk in bytes.chunks(64) {
            sink.write_all(chunk).map_err(|_| ArchiveError::Cancelled)?;
            counters.add_bytes_read(chunk.len() as u64);
        }
        sink.finish().map_err(|_| ArchiveError::Cancelled)?;
        counters.add_files_done(kept.len() as u64);
        Ok(ArchiveSummary {
            files: kept.len() as u64,
            bytes_read: kept.iter().map(|(_, data)| data.len() as u64).sum(),
            archive_bytes: bytes.len() as u64,
            sha256: hex(&Sha256::digest(&bytes)),
            skipped: 0,
        })
    }

    fn extract(
        &self,
        _plan: &ArchivePlan,
        mut source: Box<dyn Read + Send>,
        counters: Arc<Counters>,
    ) -> Result<ExtractSummary, ArchiveError> {
        let mut bytes = Vec::new();
        source
            .read_to_end(&mut bytes)
            .map_err(|_| ArchiveError::Cancelled)?;
        let home = deserialise(&bytes);
        let written = home.iter().map(|(_, data)| data.len() as u64).sum();
        counters.add_files_done(home.len() as u64);
        counters.add_bytes_written(written);
        let files = home.len() as u64;
        *lock(&self.extracted) = home;
        Ok(ExtractSummary {
            files,
            bytes_written: written,
            archive_bytes: bytes.len() as u64,
            sha256: hex(&Sha256::digest(&bytes)),
            skipped: 0,
        })
    }
}

#[derive(Default)]
struct StoreState {
    objects: HashMap<String, Vec<u8>>,
    operations: Vec<String>,
    aborted: usize,
    fail_credentials: Option<StoreError>,
    fail_multipart: Option<StoreError>,
    part_delay: Option<Duration>,
    parts_seen: usize,
}

#[derive(Clone, Default)]
pub struct FakeStore {
    state: Arc<Mutex<StoreState>>,
}

impl FakeStore {
    pub fn operations(&self) -> Vec<String> {
        lock(&self.state).operations.clone()
    }

    pub fn object(&self, key: &str) -> Option<Vec<u8>> {
        lock(&self.state).objects.get(key).cloned()
    }

    pub fn insert(&self, key: &str, bytes: Vec<u8>) {
        lock(&self.state).objects.insert(key.to_owned(), bytes);
    }

    pub fn aborted(&self) -> usize {
        lock(&self.state).aborted
    }

    pub fn parts_seen(&self) -> usize {
        lock(&self.state).parts_seen
    }

    pub fn fail_credentials(&self, error: StoreError) {
        lock(&self.state).fail_credentials = Some(error);
    }

    pub fn fail_multipart(&self, error: StoreError) {
        lock(&self.state).fail_multipart = Some(error);
    }

    /// Every part takes this long to "upload", so a test can cancel
    /// mid-way.
    pub fn slow_parts(&self, delay: Duration) {
        lock(&self.state).part_delay = Some(delay);
    }

    fn record(&self, operation: &str) {
        lock(&self.state).operations.push(operation.to_owned());
    }
}

pub struct FakeBody {
    chunks: std::collections::VecDeque<Bytes>,
    total: u64,
}

impl ObjectBody for FakeBody {
    fn content_length(&self) -> Option<u64> {
        Some(self.total)
    }

    async fn next_chunk(&mut self) -> Result<Option<Bytes>, StoreError> {
        Ok(self.chunks.pop_front())
    }
}

impl ObjectStore for FakeStore {
    type Body = FakeBody;

    async fn probe_credentials(&self) -> Result<(), StoreError> {
        self.record("probe");
        let scripted = lock(&self.state).fail_credentials.clone();
        scripted.map_or(Ok(()), Err)
    }

    async fn get(&self, _target: &StoreTarget, key: &str) -> Result<FakeBody, StoreError> {
        self.record("get");
        let state = lock(&self.state);
        let object = state
            .objects
            .get(key)
            .ok_or_else(|| StoreError::new(StoreErrorKind::NotFound))?;
        Ok(FakeBody {
            total: object.len() as u64,
            chunks: object.chunks(7).map(Bytes::copy_from_slice).collect(),
        })
    }

    async fn put(
        &self,
        _target: &StoreTarget,
        key: &str,
        body: Bytes,
        _content_type: &str,
    ) -> Result<(), StoreError> {
        self.record("put");
        lock(&self.state)
            .objects
            .insert(key.to_owned(), body.to_vec());
        Ok(())
    }

    async fn put_multipart<P: PartSource>(
        &self,
        _target: &StoreTarget,
        key: &str,
        _content_type: &str,
        mut parts: P,
    ) -> Result<PutSummary, StoreError> {
        self.record("put_multipart");
        let (scripted, delay) = {
            let state = lock(&self.state);
            (state.fail_multipart.clone(), state.part_delay)
        };
        if let Some(error) = scripted {
            lock(&self.state).aborted += 1;
            return Err(error);
        }
        let guard = AbortOnDrop {
            store: self.clone(),
            armed: true,
        };
        let mut body = Vec::new();
        let mut count = 0;
        loop {
            match parts.next_part().await {
                Ok(Some(part)) => {
                    if let Some(delay) = delay {
                        tokio::time::sleep(delay).await;
                    }
                    count += 1;
                    lock(&self.state).parts_seen += 1;
                    body.extend_from_slice(&part);
                }
                Ok(None) => break,
                Err(error) => return Err(error),
            }
        }
        let mut guard = guard;
        guard.armed = false;
        let bytes = body.len() as u64;
        lock(&self.state).objects.insert(key.to_owned(), body);
        Ok(PutSummary {
            parts: count,
            bytes,
        })
    }
}

/// Counts an abort whenever an upload future is dropped or fails before
/// completing, like the real adapter does.
struct AbortOnDrop {
    store: FakeStore,
    armed: bool,
}

impl Drop for AbortOnDrop {
    fn drop(&mut self) {
        if self.armed {
            lock(&self.store.state).aborted += 1;
        }
    }
}

/// Writes a checkpoint of `home` straight into the store under `prefix`.
pub fn seed_checkpoint(store: &FakeStore, prefix: &str, home: &Home) -> String {
    let archive = serialise(home);
    let sha = hex(&Sha256::digest(&archive));
    let manifest = serde_json::json!({
        "version": 1,
        "archive": "home.tar.gz",
        "compression": "gzip",
        "sha256": sha,
        "archive_bytes": archive.len(),
        "files": home.len(),
        "bytes": home.iter().map(|(_, data)| data.len()).sum::<usize>(),
        "skipped": 0,
        "home": "/home/user",
        "user": "user",
        "sandbox_id": "mvm-old",
        "agent_version": "0.2.0",
        "created_at": "2026-09-16T11:00:00Z",
        "excluded": [],
    });
    store.insert(&format!("{prefix}/home.tar.gz"), archive);
    store.insert(
        &format!("{prefix}/manifest.json"),
        manifest.to_string().into_bytes(),
    );
    sha
}
