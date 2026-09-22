//! Test doubles for the persistence flows: an in-memory object store with
//! scripted failures, an archiver over a plain serialisation (no tar, no
//! gzip: the real ones are adapter concerns), std-thread runner and
//! channels, and a poll-loop executor so the async flows run on any host
//! without an async runtime in this crate.

use std::collections::{HashMap, VecDeque};
use std::future::{Future, ready};
use std::io::{Read, Write};
use std::pin::pin;
use std::sync::mpsc::{self, Receiver, Sender};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::task::{Context, Poll, Waker};

use bytes::Bytes;
use sha2::{Digest, Sha256};

use super::checkpoint::{CheckpointRequestInfo, ManifestMeta};
use super::manifest::{Manifest, ManifestInput};
use super::ports::{
    ArchiveError, ArchiveSink, ArchiveSummary, BlockingRunner, ChunkSink, ExtractSummary,
    HomeArchiver, JoinFailure, ObjectBody, ObjectStore, PartSource, PutSummary, SinkClosed,
    StoreError, StoreErrorKind, StoreTarget,
};
use super::progress::Counters;
use super::restore::RestoreRequestInfo;
use super::{ArchivePlan, LocationRequest, PART_BYTES, PersistenceDeps, PersistenceGate};
use crate::process::{LookupError, ProcessIdentity, UserLookup, UserPolicy};

pub fn block_on<F: Future>(future: F) -> F::Output {
    let mut future = pin!(future);
    let mut context = Context::from_waker(Waker::noop());
    loop {
        if let Poll::Ready(output) = future.as_mut().poll(&mut context) {
            return output;
        }
        std::thread::yield_now();
    }
}

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

pub struct Users;

impl UserLookup for Users {
    fn lookup(&self, username: &str) -> Result<ProcessIdentity, LookupError> {
        let (uid, home) = match username {
            "user" => (1000, "/home/user"),
            "root" => (0, "/root"),
            _ => return Err(LookupError::UnknownUser),
        };
        Ok(ProcessIdentity {
            uid,
            gid: uid,
            groups: vec![uid],
            username: username.to_owned(),
            home: home.to_owned(),
            shell: "/bin/sh".to_owned(),
        })
    }
}

/// Runs the job on a std thread; the future resolves when it returns.
pub struct ThreadRunner;

impl BlockingRunner for ThreadRunner {
    fn run<T, F>(&self, job: F) -> impl Future<Output = Result<T, JoinFailure>> + Send
    where
        T: Send + 'static,
        F: FnOnce() -> T + Send + 'static,
    {
        let handle = std::thread::spawn(job);
        ThreadJoin {
            handle: Some(handle),
        }
    }
}

struct ThreadJoin<T> {
    handle: Option<std::thread::JoinHandle<T>>,
}

impl<T: Send + 'static> Future for ThreadJoin<T> {
    type Output = Result<T, JoinFailure>;

    fn poll(mut self: std::pin::Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<Self::Output> {
        let finished = self
            .handle
            .as_ref()
            .is_some_and(std::thread::JoinHandle::is_finished);
        if !finished {
            return Poll::Pending;
        }
        let handle = self.handle.take();
        Poll::Ready(handle.map_or(Err(JoinFailure), |handle| {
            handle.join().map_err(|_| JoinFailure)
        }))
    }
}

pub fn deps_with<S: ObjectStore, R: BlockingRunner>(
    store: S,
    archiver: FakeArchiver,
    runner: R,
) -> (PersistenceDeps<S, FakeArchiver, R>, Arc<FakeArchiver>) {
    let archiver = Arc::new(archiver);
    (
        PersistenceDeps {
            store: Arc::new(store),
            archiver: archiver.clone(),
            runner,
            gate: PersistenceGate::new(),
            lookup: Arc::new(Users),
            policy: UserPolicy::default(),
            default_region: Some("us-east-1".to_owned()),
        },
        archiver,
    )
}

pub fn location() -> LocationRequest {
    LocationRequest {
        bucket: "my-bucket".to_owned(),
        key_prefix: "rayito/x".to_owned(),
        region: None,
    }
}

pub fn request(exclude: &[&str]) -> CheckpointRequestInfo {
    CheckpointRequestInfo {
        location: location(),
        user: None,
        exclude: exclude.iter().map(|entry| (*entry).to_owned()).collect(),
    }
}

pub fn restore_request() -> RestoreRequestInfo {
    RestoreRequestInfo {
        location: location(),
        user: None,
    }
}

pub fn meta() -> ManifestMeta {
    ManifestMeta {
        sandbox_id: "mvm-test".to_owned(),
        agent_version: "0.2.0".to_owned(),
        created_at: "2026-09-16T12:00:00Z".to_owned(),
    }
}

pub type Home = Vec<(String, Vec<u8>)>;

pub fn sample_archive() -> Home {
    vec![
        ("notes.txt".to_owned(), b"hola".to_vec()),
        ("data/blob.bin".to_owned(), vec![7u8; 20]),
        ("proj/a.py".to_owned(), b"print()".to_vec()),
    ]
}

/// A fake "tar": `name\n` `len\n` bytes per entry; sha256 of the whole.
#[derive(Default)]
pub struct FakeArchiver {
    home: Mutex<Home>,
    extracted: Mutex<Home>,
    fail_archive: Mutex<Option<ArchiveError>>,
    fail_extract: Mutex<Option<ArchiveError>>,
}

impl FakeArchiver {
    pub fn set_home(&self, home: Home) {
        *lock(&self.home) = home;
    }

    pub fn extracted(&self) -> Home {
        lock(&self.extracted).clone()
    }

    pub fn fail_archive(&self, error: ArchiveError) {
        *lock(&self.fail_archive) = Some(error);
    }

    pub fn fail_extract(&self, error: ArchiveError) {
        *lock(&self.fail_extract) = Some(error);
    }
}

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

pub fn hex(bytes: &[u8]) -> String {
    bytes.iter().fold(String::new(), |mut acc, byte| {
        use std::fmt::Write as _;
        let _ = write!(acc, "{byte:02x}");
        acc
    })
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
        if let Some(error) = lock(&self.fail_archive).clone() {
            return Err(error);
        }
        let kept: Home = lock(&self.home)
            .iter()
            .filter(|(name, _)| !plan.should_skip(name))
            .cloned()
            .collect();
        let bytes = serialise(&kept);
        sink.write_all(&bytes)
            .map_err(|_| ArchiveError::Cancelled)?;
        sink.finish().map_err(|_| ArchiveError::Cancelled)?;
        counters.add_files_done(kept.len() as u64);
        let bytes_read = kept.iter().map(|(_, data)| data.len() as u64).sum();
        counters.add_bytes_read(bytes_read);
        Ok(ArchiveSummary {
            files: kept.len() as u64,
            bytes_read,
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
        if let Some(error) = lock(&self.fail_extract).clone() {
            return Err(error);
        }
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
    fail_body_after: Option<(usize, StoreError)>,
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

    pub fn remove(&self, key: &str) {
        lock(&self.state).objects.remove(key);
    }

    pub fn aborted(&self) -> usize {
        lock(&self.state).aborted
    }

    pub fn fail_credentials(&self, error: StoreError) {
        lock(&self.state).fail_credentials = Some(error);
    }

    pub fn fail_multipart(&self, error: StoreError) {
        lock(&self.state).fail_multipart = Some(error);
    }

    pub fn fail_body_after(&self, chunks: usize, error: StoreError) {
        lock(&self.state).fail_body_after = Some((chunks, error));
    }

    pub fn corrupt_archive(&self) {
        let mut state = lock(&self.state);
        if let Some(archive) = state.objects.get_mut("rayito/x/home.tar.gz") {
            archive.push(b'!');
        }
    }

    fn record(&self, operation: &str) {
        lock(&self.state).operations.push(operation.to_owned());
    }
}

pub struct FakeBody {
    chunks: VecDeque<Bytes>,
    total: u64,
    served: usize,
    fail_after: Option<(usize, StoreError)>,
}

impl ObjectBody for FakeBody {
    fn content_length(&self) -> Option<u64> {
        Some(self.total)
    }

    fn next_chunk(&mut self) -> impl Future<Output = Result<Option<Bytes>, StoreError>> + Send {
        if let Some((limit, error)) = &self.fail_after
            && self.served >= *limit
        {
            return ready(Err(error.clone()));
        }
        self.served += 1;
        ready(Ok(self.chunks.pop_front()))
    }
}

impl ObjectStore for FakeStore {
    type Body = FakeBody;

    fn probe_credentials(&self) -> impl Future<Output = Result<(), StoreError>> + Send {
        self.record("probe");
        let scripted = lock(&self.state).fail_credentials.clone();
        ready(scripted.map_or(Ok(()), Err))
    }

    fn get(
        &self,
        _target: &StoreTarget,
        key: &str,
    ) -> impl Future<Output = Result<FakeBody, StoreError>> + Send {
        self.record("get");
        let state = lock(&self.state);
        let Some(object) = state.objects.get(key) else {
            return ready(Err(StoreError::new(StoreErrorKind::NotFound)));
        };
        let chunks: VecDeque<Bytes> = object.chunks(5).map(Bytes::copy_from_slice).collect();
        ready(Ok(FakeBody {
            total: object.len() as u64,
            chunks,
            served: 0,
            fail_after: state.fail_body_after.clone(),
        }))
    }

    fn put(
        &self,
        _target: &StoreTarget,
        key: &str,
        body: Bytes,
        _content_type: &str,
    ) -> impl Future<Output = Result<(), StoreError>> + Send {
        self.record("put");
        lock(&self.state)
            .objects
            .insert(key.to_owned(), body.to_vec());
        ready(Ok(()))
    }

    async fn put_multipart<P: PartSource>(
        &self,
        _target: &StoreTarget,
        key: &str,
        _content_type: &str,
        mut parts: P,
    ) -> Result<PutSummary, StoreError> {
        self.record("put_multipart");
        let scripted = lock(&self.state).fail_multipart.clone();
        if let Some(error) = scripted {
            lock(&self.state).aborted += 1;
            return Err(error);
        }
        let mut body = Vec::new();
        let mut count = 0;
        loop {
            match parts.next_part().await {
                Ok(Some(part)) => {
                    count += 1;
                    body.extend_from_slice(&part);
                }
                Ok(None) => break,
                Err(error) => {
                    lock(&self.state).aborted += 1;
                    return Err(error);
                }
            }
        }
        let bytes = body.len() as u64;
        lock(&self.state).objects.insert(key.to_owned(), body);
        Ok(PutSummary {
            parts: count,
            bytes,
        })
    }
}

/// Writes a checkpoint of `home` straight into the store, as a previous
/// sandbox would have; returns the archive bytes and their sha256.
pub fn seed_checkpoint(store: &FakeStore, home: &Home) -> (Vec<u8>, String) {
    let archive = serialise(home);
    let sha = hex(&Sha256::digest(&archive));
    let manifest = Manifest::v1(&ManifestInput {
        sha256: &sha,
        archive_bytes: archive.len() as u64,
        files: home.len() as u64,
        bytes: home.iter().map(|(_, data)| data.len() as u64).sum(),
        skipped: 0,
        home: "/home/user",
        user: "user",
        sandbox_id: "mvm-old",
        agent_version: "0.2.0",
        created_at: "2026-09-16T11:00:00Z",
        excluded: &[],
    });
    store.insert("rayito/x/home.tar.gz", archive.clone());
    store.insert(
        "rayito/x/manifest.json",
        manifest.to_json().unwrap_or_default(),
    );
    (archive, sha)
}

enum Frame {
    Data(Vec<u8>),
    End,
}

/// The upload channel: a sink for the archive thread and a part source
/// for the store, over a std channel.
pub struct VecParts {
    receiver: Receiver<Frame>,
    pending: Vec<u8>,
    ended: bool,
}

struct VecPartsSink {
    sender: Sender<Frame>,
}

impl Write for VecPartsSink {
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        self.sender
            .send(Frame::Data(buf.to_vec()))
            .map_err(|_| std::io::Error::from(std::io::ErrorKind::BrokenPipe))?;
        Ok(buf.len())
    }

    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

impl ArchiveSink for VecPartsSink {
    fn finish(self: Box<Self>) -> std::io::Result<()> {
        self.sender
            .send(Frame::End)
            .map_err(|_| std::io::Error::from(std::io::ErrorKind::BrokenPipe))
    }
}

impl VecParts {
    pub fn pair() -> (Box<dyn ArchiveSink>, Self) {
        let (sender, receiver) = mpsc::channel();
        (
            Box::new(VecPartsSink { sender }),
            Self {
                receiver,
                pending: Vec::new(),
                ended: false,
            },
        )
    }
}

impl VecParts {
    fn next_part_blocking(&mut self) -> Result<Option<Bytes>, StoreError> {
        while !self.ended && self.pending.len() < PART_BYTES {
            match self.receiver.recv() {
                Ok(Frame::Data(data)) => self.pending.extend_from_slice(&data),
                Ok(Frame::End) => self.ended = true,
                Err(_) => return Err(StoreError::new(StoreErrorKind::Interrupted)),
            }
        }
        if self.pending.is_empty() {
            return Ok(None);
        }
        let cut = self.pending.len().min(PART_BYTES);
        let part = self.pending.drain(..cut).collect::<Vec<u8>>();
        Ok(Some(Bytes::from(part)))
    }
}

impl PartSource for VecParts {
    fn next_part(&mut self) -> impl Future<Output = Result<Option<Bytes>, StoreError>> + Send {
        ready(self.next_part_blocking())
    }
}

/// The download channel: a chunk sink for the pump and a `Read` for the
/// unpack thread, over a std channel.
pub struct VecChunks {
    sender: Sender<Frame>,
}

struct VecChunksSource {
    receiver: Receiver<Frame>,
    pending: VecDeque<u8>,
    ended: bool,
}

impl Read for VecChunksSource {
    fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
        while self.pending.is_empty() && !self.ended {
            match self.receiver.recv() {
                Ok(Frame::Data(data)) => self.pending.extend(data),
                Ok(Frame::End) => self.ended = true,
                Err(_) => return Err(std::io::Error::from(std::io::ErrorKind::BrokenPipe)),
            }
        }
        let n = buf.len().min(self.pending.len());
        for slot in buf.iter_mut().take(n) {
            *slot = self.pending.pop_front().unwrap_or_default();
        }
        Ok(n)
    }
}

impl VecChunks {
    pub fn pair() -> (Self, Box<dyn Read + Send>) {
        let (sender, receiver) = mpsc::channel();
        (
            Self { sender },
            Box::new(VecChunksSource {
                receiver,
                pending: VecDeque::new(),
                ended: false,
            }),
        )
    }
}

impl ChunkSink for VecChunks {
    fn push(&mut self, chunk: Bytes) -> impl Future<Output = Result<(), SinkClosed>> + Send {
        ready(
            self.sender
                .send(Frame::Data(chunk.to_vec()))
                .map_err(|_| SinkClosed),
        )
    }

    fn finish(self) -> impl Future<Output = ()> + Send {
        let _ = self.sender.send(Frame::End);
        ready(())
    }
}
