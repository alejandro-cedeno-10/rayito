//! `FilesystemService` end to end, in process, against a real temp
//! directory (design D12): the in-process router on `127.0.0.1:0`, `/run`
//! installed through the hooks router, the watch keepalive, queue and cap
//! shrunk through the settings. Runs as whatever user CI provides
//! (`IdentitySwitch::KeepCurrent` unless root); the identity cases only run
//! as root, where `setfsuid` is real.
//!
//! Integration tests are test code, but clippy's `allow-unwrap-in-tests` only
//! recognises `#[test]` functions and `#[cfg(test)]` modules.
#![cfg(unix)]
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::fs;
use std::net::SocketAddr;
use std::os::unix::fs::{DirBuilderExt, MetadataExt, PermissionsExt, symlink};
use std::sync::Arc;
use std::time::{Duration, Instant};

use axum::Router;
use axum::body::Body;
use base64::Engine;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use http::Request;
use rayd::adapters::OsRandomSource;
use rayd::adapters::{
    NixNameResolver, NotifyWatcher, PlatformMetricsProbe, StdFileSystem, detect_spawn_platform,
};
use rayd::code::CodeManager;
use rayd::filesystem::{
    FilesystemManager, FilesystemPlatform, FilesystemSettings, WatchItem, WriteFailure,
    WriteMessageWithChunk,
};
use rayd::grpc::Services;
use rayd::grpc::StreamSettings;
use rayd::hooks::hook_path;
use rayd::lifecycle::SuspendSignal;
use rayd::process::{ManagerSettings, platform_manager, shared_registry};
use rayd::pty::{PtySettings, platform_pty_manager};
use rayd_core::clock::SystemClock;
use rayd_core::filesystem::{DenyList, FilesystemError, ListingLimits, TEMP_PREFIX, WriteMessage};
use rayd_core::lifecycle::Hook;
use rayd_core::process::{LookupError, ProcessIdentity, RegistryLimits, UserLookup, UserPolicy};
use rayd_core::session::SandboxSession;
use rayito_proto::v1::filesystem_service_client::FilesystemServiceClient;
use rayito_proto::v1::{
    EntryInfo, FileType, FilesystemEvent, FilesystemEventType, ListDirRequest, MakeDirRequest,
    MoveRequest, ReadRequest, RemoveRequest, StatRequest, User, WatchDirRequest, WatchDirResponse,
    WriteRequest, WriteResponse, watch_dir_response,
};
use sha2::{Digest, Sha256};
use tokio::net::TcpListener;
use tokio::sync::mpsc;
use tokio_stream::StreamExt;
use tokio_stream::wrappers::ReceiverStream;
use tokio_util::sync::CancellationToken;
use tonic::metadata::MetadataValue;
use tonic::transport::Channel;
use tonic::transport::server::TcpIncoming;
use tonic::{Code, Status, Streaming};
use tower::ServiceExt;

const SECRET: &[u8] = b"m3-sandbox-secret";
const CHUNK: usize = 262_144;
const EVENT_BUDGET: Duration = Duration::from_secs(5);

struct Options {
    watch_keepalive: Duration,
    watch_queue_capacity: usize,
    max_watches: usize,
    max_entries: usize,
    allow_root: bool,
    lookup: Option<Arc<dyn UserLookup>>,
}

impl Default for Options {
    fn default() -> Self {
        Self {
            watch_keepalive: Duration::from_millis(200),
            watch_queue_capacity: 1024,
            max_watches: 64,
            max_entries: 10_000,
            allow_root: running_as_root(),
            lookup: None,
        }
    }
}

fn running_as_root() -> bool {
    nix::unistd::geteuid().is_root()
}

struct Harness {
    files: FilesystemServiceClient<Channel>,
    hooks: Router,
    manager: Arc<FilesystemManager>,
    addr: SocketAddr,
    #[allow(dead_code)]
    playground: tempfile::TempDir,
    root: String,
}

async fn harness() -> Harness {
    harness_with(Options::default()).await
}

async fn harness_with(options: Options) -> Harness {
    let playground = tempfile::tempdir().unwrap();
    let root = fs::canonicalize(playground.path())
        .unwrap()
        .to_string_lossy()
        .into_owned();
    let session = Arc::new(SandboxSession::new(Arc::new(SystemClock::new()), "test"));
    let policy = UserPolicy {
        allow_root: options.allow_root,
    };
    let platform = detect_spawn_platform();
    let manager = FilesystemManager::new(
        session.clone(),
        FilesystemPlatform {
            fs: Arc::new(StdFileSystem::new(platform.identity_switch)),
            watcher: Arc::new(NotifyWatcher::new(platform.identity_switch)),
            names: Arc::new(NixNameResolver),
            lookup: options.lookup.unwrap_or_else(|| platform.lookup.clone()),
        },
        policy,
        DenyList::default(),
        FilesystemSettings {
            list_limits: ListingLimits {
                max_entries: options.max_entries,
            },
            watch_queue_capacity: options.watch_queue_capacity,
            max_watches: options.max_watches,
            write_error_drain_bytes: 1 << 20,
            write_error_drain_timeout: Duration::from_millis(500),
        },
    );
    let registry = shared_registry(RegistryLimits::default());
    let ptys = platform_pty_manager(
        session.clone(),
        &platform,
        policy,
        registry.clone(),
        PtySettings::default(),
    );
    let processes = platform_manager(
        session.clone(),
        platform,
        policy,
        registry,
        ManagerSettings::default(),
    );
    let code = CodeManager::disabled(session.clone(), Arc::new(OsRandomSource));
    let suspend = Arc::new(SuspendSignal::new());
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let shutdown = CancellationToken::new();
    let grpc = rayd::grpc::router_with_settings(
        Services {
            session: session.clone(),
            processes,
            ptys,
            files: manager.clone(),
            code: code.clone(),
            metrics: Arc::new(PlatformMetricsProbe::default()),
            suspend: suspend.clone(),
            imds: Arc::new(rayd::adapters::ImdsState::default()),
            persistence: Arc::new(rayd::persistence::UnavailablePersistence),
        },
        StreamSettings {
            keepalive_interval: Duration::from_millis(200),
            watch_keepalive_interval: options.watch_keepalive,
            ..StreamSettings::default()
        },
    )
    .serve_with_incoming_shutdown(
        TcpIncoming::from(listener),
        shutdown.clone().cancelled_owned(),
    );
    tokio::spawn(grpc);
    let channel = Channel::from_shared(format!("http://{addr}"))
        .unwrap()
        .connect()
        .await
        .unwrap();
    let harness = Harness {
        files: FilesystemServiceClient::new(channel),
        hooks: rayd::hooks::router(session, code, suspend, shutdown),
        manager,
        addr,
        playground,
        root,
    };
    harness.post(Hook::Run, Some(run_envelope())).await;
    harness
}

/// As root the default identity is root itself (there may be no `user`
/// account on CI); unprivileged runs keep the current user.
fn run_envelope() -> String {
    use std::fmt::Write as _;
    let digest = Sha256::digest(SECRET)
        .iter()
        .fold(String::new(), |mut hex, byte| {
            write!(hex, "{byte:02x}").unwrap();
            hex
        });
    let mut payload = serde_json::json!({ "v": 1, "token_sha256": digest });
    if running_as_root() {
        payload["user"] = serde_json::Value::String("root".to_owned());
    }
    serde_json::json!({ "microvmId": "mvm-m3", "runHookPayload": payload.to_string() }).to_string()
}

fn authenticated<T>(message: T) -> tonic::Request<T> {
    let mut request = tonic::Request::new(message);
    let encoded = URL_SAFE_NO_PAD.encode(SECRET);
    request
        .metadata_mut()
        .insert("x-access-token", MetadataValue::try_from(encoded).unwrap());
    request
}

fn user(name: &str) -> User {
    User {
        username: name.to_owned(),
    }
}

fn temp_files(dir: &str) -> usize {
    fs::read_dir(dir)
        .unwrap()
        .filter_map(Result::ok)
        .filter(|entry| entry.file_name().to_string_lossy().starts_with(TEMP_PREFIX))
        .count()
}

fn write_request(path: Option<&str>, mode: Option<u32>, chunk: &[u8]) -> WriteRequest {
    WriteRequest {
        path: path.map(str::to_owned),
        user: None,
        mode,
        chunk: chunk.to_vec(),
    }
}

impl Harness {
    async fn post(&self, hook: Hook, body: Option<String>) {
        let request = Request::post(hook_path(hook))
            .header("content-type", "application/json")
            .body(body.map_or_else(Body::empty, Body::from))
            .unwrap();
        let response = self.hooks.clone().oneshot(request).await.unwrap();
        assert!(response.status().is_success());
    }

    fn path(&self, relative: &str) -> String {
        format!("{}/{relative}", self.root)
    }

    async fn read(&self, path: &str) -> Result<Vec<Vec<u8>>, Status> {
        self.read_as(path, None).await
    }

    async fn read_as(&self, path: &str, user: Option<User>) -> Result<Vec<Vec<u8>>, Status> {
        let mut stream = self
            .files
            .clone()
            .read(authenticated(ReadRequest {
                path: path.to_owned(),
                user,
            }))
            .await?
            .into_inner();
        let mut chunks = Vec::new();
        while let Some(response) = stream.message().await? {
            chunks.push(response.chunk);
        }
        Ok(chunks)
    }

    async fn write_raw(&self, requests: Vec<WriteRequest>) -> Result<WriteResponse, Status> {
        self.files
            .clone()
            .write(authenticated(tokio_stream::iter(requests)))
            .await
            .map(tonic::Response::into_inner)
    }

    async fn write(&self, path: &str, data: &[u8]) -> Result<EntryInfo, Status> {
        let mut requests = Vec::new();
        for (index, chunk) in data.chunks(1 << 20).enumerate() {
            requests.push(write_request((index == 0).then_some(path), None, chunk));
        }
        if requests.is_empty() {
            requests.push(write_request(Some(path), None, &[]));
        }
        let response = self.write_raw(requests).await?;
        Ok(response.entries.into_iter().next().unwrap())
    }

    async fn stat(&self, path: &str) -> Result<EntryInfo, Status> {
        self.files
            .clone()
            .stat(authenticated(StatRequest {
                path: path.to_owned(),
                user: None,
            }))
            .await
            .map(|response| response.into_inner().entry.unwrap())
    }

    async fn list(&self, path: &str, depth: u32) -> Result<Vec<EntryInfo>, Status> {
        self.files
            .clone()
            .list_dir(authenticated(ListDirRequest {
                path: path.to_owned(),
                depth,
                user: None,
            }))
            .await
            .map(|response| response.into_inner().entries)
    }

    async fn make_dir(&self, path: &str) -> Result<EntryInfo, Status> {
        self.files
            .clone()
            .make_dir(authenticated(MakeDirRequest {
                path: path.to_owned(),
                user: None,
            }))
            .await
            .map(|response| response.into_inner().entry.unwrap())
    }

    async fn rename(&self, source: &str, destination: &str) -> Result<EntryInfo, Status> {
        self.files
            .clone()
            .r#move(authenticated(MoveRequest {
                source: source.to_owned(),
                destination: destination.to_owned(),
                user: None,
            }))
            .await
            .map(|response| response.into_inner().entry.unwrap())
    }

    async fn remove(&self, path: &str, recursive: bool) -> Result<(), Status> {
        self.files
            .clone()
            .remove(authenticated(RemoveRequest {
                path: path.to_owned(),
                recursive,
                user: None,
            }))
            .await
            .map(|_| ())
    }

    async fn watch(
        &self,
        path: &str,
        recursive: bool,
        include_entry: bool,
    ) -> Result<Streaming<WatchDirResponse>, Status> {
        self.watch_as(path, recursive, include_entry, None).await
    }

    async fn watch_as(
        &self,
        path: &str,
        recursive: bool,
        include_entry: bool,
        user: Option<User>,
    ) -> Result<Streaming<WatchDirResponse>, Status> {
        self.files
            .clone()
            .watch_dir(authenticated(WatchDirRequest {
                path: path.to_owned(),
                recursive,
                user,
                include_entry,
            }))
            .await
            .map(tonic::Response::into_inner)
    }

    async fn watch_started(&self, path: &str, recursive: bool) -> Streaming<WatchDirResponse> {
        let mut stream = self.watch(path, recursive, false).await.unwrap();
        expect_started(&mut stream).await;
        stream
    }
}

async fn expect_started(stream: &mut Streaming<WatchDirResponse>) {
    let first = tokio::time::timeout(EVENT_BUDGET, stream.message())
        .await
        .expect("WatchStarted within budget")
        .unwrap()
        .expect("first message");
    assert!(
        matches!(first.event, Some(watch_dir_response::Event::Started(_))),
        "first message must be WatchStarted, got {first:?}"
    );
}

/// The next `FilesystemEvent`, skipping keepalives; `Ok(None)` when the
/// stream ends cleanly, `Err` for a trailing status.
async fn next_event(
    stream: &mut Streaming<WatchDirResponse>,
) -> Result<Option<FilesystemEvent>, Status> {
    loop {
        let message = tokio::time::timeout(EVENT_BUDGET, stream.message())
            .await
            .expect("event within budget")?;
        match message.map(|response| response.event) {
            None => return Ok(None),
            Some(Some(watch_dir_response::Event::Filesystem(event))) => return Ok(Some(event)),
            Some(Some(watch_dir_response::Event::Keepalive(_))) => {}
            Some(other) => panic!("unexpected message {other:?}"),
        }
    }
}

fn typed(event: &FilesystemEvent) -> (String, FilesystemEventType) {
    (
        event.name.clone(),
        FilesystemEventType::try_from(event.r#type).unwrap(),
    )
}

/// Collects events until `last` arrives (inclusive) or the budget ends.
async fn collect_until(
    stream: &mut Streaming<WatchDirResponse>,
    last: (&str, FilesystemEventType),
) -> Vec<(String, FilesystemEventType)> {
    let deadline = Instant::now() + EVENT_BUDGET;
    let mut seen = Vec::new();
    while Instant::now() < deadline {
        let event = next_event(stream)
            .await
            .unwrap()
            .expect("stream still open");
        let item = typed(&event);
        let done = item.0 == last.0 && item.1 == last.1;
        seen.push(item);
        if done {
            break;
        }
    }
    seen
}

#[tokio::test]
async fn read_streams_full_chunks_and_empty_files_produce_none() {
    let harness = harness().await;
    let payload: Vec<u8> = (0..600 * 1024)
        .map(|i| u8::try_from(i % 251).unwrap())
        .collect();
    fs::write(harness.path("big.bin"), &payload).unwrap();
    fs::write(harness.path("empty"), b"").unwrap();
    let started = Instant::now();
    let chunks = harness.read(&harness.path("big.bin")).await.unwrap();
    println!("read 600 KiB in-process: {:?}", started.elapsed());
    let sizes: Vec<usize> = chunks.iter().map(Vec::len).collect();
    assert_eq!(sizes, vec![CHUNK, CHUNK, 90_112]);
    assert_eq!(chunks.concat(), payload);
    assert!(
        harness
            .read(&harness.path("empty"))
            .await
            .unwrap()
            .is_empty()
    );
}

#[tokio::test]
async fn read_rejects_missing_directories_symlinks_and_denied_paths() {
    let harness = harness().await;
    fs::write(harness.path("f"), b"x").unwrap();
    symlink("f", harness.path("link")).unwrap();
    symlink("/etc", harness.path("etc-link")).unwrap();
    let code = |result: Result<Vec<Vec<u8>>, Status>| result.unwrap_err().code();
    assert_eq!(
        code(harness.read(&harness.path("nope")).await),
        Code::NotFound
    );
    assert_eq!(
        code(harness.read(&harness.root).await),
        Code::InvalidArgument
    );
    assert_eq!(
        code(harness.read(&harness.path("link")).await),
        Code::InvalidArgument
    );
    assert_eq!(
        code(harness.read("/etc/passwd").await),
        Code::PermissionDenied
    );
    assert_eq!(
        code(harness.read(&harness.path("etc-link/passwd")).await),
        Code::PermissionDenied
    );
    let status = harness
        .read(&harness.path("../../etc/passwd"))
        .await
        .unwrap_err();
    assert_eq!(status.code(), Code::InvalidArgument);
    assert!(!status.message().contains("etc"));
}

/// `open(2)` of a FIFO with no writer would park a pool thread forever;
/// the adapter opens non-blocking and refuses it as not a regular file.
#[tokio::test]
async fn read_of_a_fifo_answers_invalid_argument_promptly() {
    let harness = harness().await;
    let fifo = harness.path("pipe");
    nix::unistd::mkfifo(fifo.as_str(), nix::sys::stat::Mode::S_IRWXU).unwrap();
    let status = tokio::time::timeout(EVENT_BUDGET, harness.read(&fifo))
        .await
        .expect("Read of a FIFO answered within budget")
        .unwrap_err();
    assert_eq!(status.code(), Code::InvalidArgument);
    assert!(harness.stat(&harness.path("pipe")).await.is_ok());
}

#[tokio::test]
async fn write_single_file_honours_mode_and_reports_the_entry() {
    let harness = harness().await;
    let path = harness.path("secret.txt");
    let response = harness
        .write_raw(vec![
            write_request(Some(&path), Some(0o600), b"hola "),
            write_request(None, None, b"mundo"),
        ])
        .await
        .unwrap();
    assert_eq!(response.entries.len(), 1);
    let entry = &response.entries[0];
    assert_eq!(entry.name, "secret.txt");
    assert_eq!(entry.path, path);
    assert_eq!(entry.size, 10);
    assert_eq!(entry.mode, 0o600);
    assert_eq!(entry.permissions, "-rw-------");
    assert_eq!(entry.r#type, i32::from(FileType::File));
    assert!(!entry.owner.is_empty());
    assert!(entry.modified_time_unix_ms > 1_600_000_000_000);
    assert_eq!(fs::read(&path).unwrap(), b"hola mundo");
    assert_eq!(
        fs::metadata(&path).unwrap().permissions().mode() & 0o7777,
        0o600
    );
    assert_eq!(temp_files(&harness.root), 0);
    let default_mode = harness.write(&harness.path("plain"), b"p").await.unwrap();
    assert_eq!(default_mode.mode, 0o644);
    assert_eq!(default_mode.permissions, "-rw-r--r--");
}

#[tokio::test]
async fn write_many_files_in_one_stream_keeps_request_order() {
    let harness = harness().await;
    let started = Instant::now();
    let mut requests = Vec::new();
    for index in 0..50 {
        let path = harness.path(&format!("many/f{index:02}.txt"));
        requests.push(write_request(
            Some(&path),
            None,
            format!("file {index}\n").as_bytes(),
        ));
    }
    let response = harness.write_raw(requests).await.unwrap();
    println!("50 small files in one stream: {:?}", started.elapsed());
    let names: Vec<&str> = response
        .entries
        .iter()
        .map(|entry| entry.name.as_str())
        .collect();
    let expected: Vec<String> = (0..50).map(|index| format!("f{index:02}.txt")).collect();
    assert_eq!(names, expected);
    assert_eq!(fs::read_dir(harness.path("many")).unwrap().count(), 50);
    assert_eq!(fs::read(harness.path("many/f07.txt")).unwrap(), b"file 7\n");
}

#[tokio::test]
async fn write_round_trips_eight_megabytes() {
    let harness = harness().await;
    let payload: Vec<u8> = (0..8_000_000u32)
        .map(|i| u8::try_from(i.wrapping_mul(2_654_435_761) >> 24).unwrap())
        .collect();
    let path = harness.path("eight.bin");
    let started = Instant::now();
    let entry = harness.write(&path, &payload).await.unwrap();
    let write_elapsed = started.elapsed();
    assert_eq!(entry.size, 8_000_000);
    let started = Instant::now();
    let chunks = harness.read(&path).await.unwrap();
    let read_elapsed = started.elapsed();
    println!("8 MB in-process: write {write_elapsed:?}, read {read_elapsed:?}");
    assert!(chunks.len() >= 30);
    assert!(chunks.iter().all(|chunk| chunk.len() <= CHUNK));
    assert_eq!(Sha256::digest(chunks.concat()), Sha256::digest(&payload));
}

#[tokio::test]
async fn write_rejections_answer_invalid_argument_and_leave_no_temp() {
    let harness = harness().await;
    let path = harness.path("target");
    let oversized = harness
        .write_raw(vec![write_request(
            Some(&path),
            None,
            &vec![0; (1 << 20) + 1],
        )])
        .await
        .unwrap_err();
    assert_eq!(oversized.code(), Code::InvalidArgument);
    assert_eq!(temp_files(&harness.root), 0);
    assert!(!fs::exists(&path).unwrap());
    let no_path = harness
        .write_raw(vec![write_request(None, None, b"x")])
        .await
        .unwrap_err();
    assert_eq!(no_path.code(), Code::InvalidArgument);
    let empty = harness.write_raw(Vec::new()).await.unwrap_err();
    assert_eq!(empty.code(), Code::InvalidArgument);
    let bad_mode = harness
        .write_raw(vec![write_request(Some(&path), Some(0o10000), b"x")])
        .await
        .unwrap_err();
    assert_eq!(bad_mode.code(), Code::InvalidArgument);
    fs::create_dir(harness.path("dir")).unwrap();
    let onto_dir = harness.write(&harness.path("dir"), b"x").await.unwrap_err();
    assert_eq!(onto_dir.code(), Code::InvalidArgument);
    assert_eq!(temp_files(&harness.root), 0);
    let denied = harness
        .write("/usr/local/bin/rayd", b"x")
        .await
        .unwrap_err();
    assert_eq!(denied.code(), Code::PermissionDenied);
    let second_bad = harness
        .write_raw(vec![
            write_request(Some(&harness.path("ok.txt")), None, b"ok"),
            write_request(Some(&harness.path("dir")), None, b"x"),
        ])
        .await
        .unwrap_err();
    assert_eq!(second_bad.code(), Code::InvalidArgument);
    assert_eq!(
        fs::read(harness.path("ok.txt")).unwrap(),
        b"ok",
        "per-file atomicity"
    );
}

#[tokio::test]
async fn write_replaces_a_symlink_and_creates_parents() {
    let harness = harness().await;
    fs::write(harness.path("original"), b"original").unwrap();
    symlink("original", harness.path("link")).unwrap();
    harness.write(&harness.path("link"), b"new").await.unwrap();
    assert!(
        !fs::symlink_metadata(harness.path("link"))
            .unwrap()
            .is_symlink()
    );
    assert_eq!(fs::read(harness.path("link")).unwrap(), b"new");
    assert_eq!(fs::read(harness.path("original")).unwrap(), b"original");
    let entry = harness
        .write(&harness.path("a/b/c/deep.txt"), b"deep")
        .await
        .unwrap();
    assert_eq!(entry.path, harness.path("a/b/c/deep.txt"));
    assert!(fs::metadata(harness.path("a/b")).unwrap().is_dir());
    assert_eq!(
        fs::metadata(harness.path("a/b"))
            .unwrap()
            .permissions()
            .mode()
            & 0o7777,
        0o755
    );
}

/// A client failure mid-stream (a reset, a cancel) must never commit: the
/// open temp file goes with the stream.
#[tokio::test]
async fn write_client_failure_discards_the_open_file() {
    let harness = harness().await;
    let path = harness.path("partial.bin");
    let messages = tokio_stream::iter(vec![
        Ok(WriteMessageWithChunk {
            message: WriteMessage {
                path: Some(path.clone()),
                user: None,
                mode: None,
                chunk_len: 1024,
            },
            chunk: vec![1; 1024],
        }),
        Err(Status::cancelled("client went away")),
    ]);
    let mut messages = messages;
    let failure = harness
        .manager
        .write(&mut messages, || false)
        .await
        .unwrap_err();
    assert!(matches!(failure, WriteFailure::Client(status) if status.code() == Code::Cancelled));
    assert_eq!(temp_files(&harness.root), 0);
    assert!(!fs::exists(&path).unwrap());
    let mut complete_but_aborted =
        tokio_stream::iter(vec![Ok::<_, Status>(WriteMessageWithChunk {
            message: WriteMessage {
                path: Some(path.clone()),
                user: None,
                mode: None,
                chunk_len: 3,
            },
            chunk: b"abc".to_vec(),
        })]);
    let failure = harness
        .manager
        .write(&mut complete_but_aborted, || true)
        .await
        .unwrap_err();
    assert!(matches!(failure, WriteFailure::Aborted));
    assert_eq!(temp_files(&harness.root), 0);
    assert!(!fs::exists(&path).unwrap());
}

/// The client's connection dies with the upload half way: the server sees
/// a broken stream, not an end of stream, and nothing is committed.
#[tokio::test]
async fn write_connection_drop_mid_stream_removes_the_temp_file() {
    let harness = harness().await;
    let path = harness.path("partial.bin");
    let channel = Channel::from_shared(format!("http://{}", harness.addr))
        .unwrap()
        .connect()
        .await
        .unwrap();
    let mut client = FilesystemServiceClient::new(channel);
    let (sender, receiver) = mpsc::channel::<WriteRequest>(4);
    let call = tokio::spawn(async move {
        client
            .write(authenticated(ReceiverStream::new(receiver)))
            .await
    });
    sender
        .send(write_request(Some(&path), None, &[1; 1024]))
        .await
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(2);
    while temp_files(&harness.root) == 0 {
        assert!(Instant::now() < deadline, "temp file never appeared");
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    call.abort();
    let deadline = Instant::now() + Duration::from_secs(3);
    while temp_files(&harness.root) > 0 {
        assert!(
            Instant::now() < deadline,
            "temp file survived the connection drop"
        );
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    assert!(
        !fs::exists(&path).unwrap(),
        "a dropped upload was committed"
    );
    drop(sender);
}

#[tokio::test]
async fn stat_reports_kinds_targets_and_not_found() {
    let harness = harness().await;
    fs::write(harness.path("f"), b"abc").unwrap();
    fs::create_dir(harness.path("d")).unwrap();
    symlink("f", harness.path("l")).unwrap();
    let file = harness.stat(&harness.path("f")).await.unwrap();
    assert_eq!(file.r#type, i32::from(FileType::File));
    assert_eq!(file.size, 3);
    assert_eq!(file.name, "f");
    let dir = harness.stat(&harness.path("d/")).await.unwrap();
    assert_eq!(dir.r#type, i32::from(FileType::Directory));
    assert_eq!(dir.path, harness.path("d"));
    let link = harness.stat(&harness.path("l")).await.unwrap();
    assert_eq!(link.r#type, i32::from(FileType::Symlink));
    assert_eq!(link.symlink_target.as_deref(), Some("f"));
    assert_eq!(link.permissions, "lrwxrwxrwx");
    let started = Instant::now();
    for _ in 0..30 {
        harness.stat(&harness.path("f")).await.unwrap();
    }
    println!("30 stats in-process: {:?}", started.elapsed());
    assert_eq!(
        harness
            .stat(&harness.path("nope"))
            .await
            .unwrap_err()
            .code(),
        Code::NotFound
    );
    assert_eq!(
        harness
            .stat(&harness.path("f/inner"))
            .await
            .unwrap_err()
            .code(),
        Code::NotFound
    );
}

#[tokio::test]
async fn list_dir_orders_by_depth_and_name_without_following_symlinks() {
    let harness = harness_with(Options {
        max_entries: 5,
        ..Options::default()
    })
    .await;
    fs::write(harness.path("b.txt"), b"b").unwrap();
    fs::create_dir_all(harness.path("a/inner")).unwrap();
    fs::write(harness.path("a/z.txt"), b"z").unwrap();
    fs::write(harness.path("a/inner/deep.txt"), b"d").unwrap();
    symlink(harness.path("a"), harness.path("c-link")).unwrap();
    let paths = |entries: Vec<EntryInfo>| -> Vec<String> {
        entries
            .into_iter()
            .map(|entry| {
                entry
                    .path
                    .strip_prefix(&format!("{}/", harness.root))
                    .unwrap()
                    .to_owned()
            })
            .collect()
    };
    assert_eq!(
        paths(harness.list(&harness.root, 0).await.unwrap()),
        vec!["a", "b.txt", "c-link"]
    );
    assert_eq!(
        paths(harness.list(&harness.root, 1).await.unwrap()),
        vec!["a", "b.txt", "c-link"]
    );
    let started = Instant::now();
    let two = harness.list(&harness.root, 2).await.unwrap();
    println!("depth-2 listing in-process: {:?}", started.elapsed());
    assert_eq!(
        paths(two.clone()),
        vec!["a", "a/inner", "a/z.txt", "b.txt", "c-link"]
    );
    let link = two.iter().find(|entry| entry.name == "c-link").unwrap();
    assert_eq!(link.r#type, i32::from(FileType::Symlink));
    assert_eq!(
        harness.list(&harness.root, 3).await.unwrap_err().code(),
        Code::ResourceExhausted
    );
    assert_eq!(
        harness
            .list(&harness.path("b.txt"), 1)
            .await
            .unwrap_err()
            .code(),
        Code::InvalidArgument
    );
    assert_eq!(
        harness
            .list(&harness.path("nope"), 1)
            .await
            .unwrap_err()
            .code(),
        Code::NotFound
    );
    assert_eq!(
        harness.list("/proc", 1).await.unwrap_err().code(),
        Code::PermissionDenied
    );
}

#[tokio::test]
async fn root_listing_names_denied_directories_without_entering_them() {
    let harness = harness().await;
    let root = harness.list("/", 2).await.unwrap();
    assert!(root.iter().any(|entry| entry.name == "etc"));
    assert!(!root.iter().any(|entry| entry.path.starts_with("/etc/")));
    assert!(!root.iter().any(|entry| entry.path.starts_with("/proc/")));
}

#[tokio::test]
async fn make_dir_creates_parents_and_reports_existing_kinds() {
    let harness = harness().await;
    let created = harness.make_dir(&harness.path("x/y/z")).await.unwrap();
    assert_eq!(created.r#type, i32::from(FileType::Directory));
    assert_eq!(created.name, "z");
    assert_eq!(created.permissions, "drwxr-xr-x");
    assert_eq!(
        harness
            .make_dir(&harness.path("x/y/z"))
            .await
            .unwrap_err()
            .code(),
        Code::AlreadyExists
    );
    fs::write(harness.path("file"), b"f").unwrap();
    assert_eq!(
        harness
            .make_dir(&harness.path("file"))
            .await
            .unwrap_err()
            .code(),
        Code::InvalidArgument
    );
    assert_eq!(
        harness
            .make_dir(&harness.path("file/sub"))
            .await
            .unwrap_err()
            .code(),
        Code::InvalidArgument
    );
    assert_eq!(
        harness.make_dir("/etc/rayito-m3").await.unwrap_err().code(),
        Code::PermissionDenied
    );
}

#[tokio::test]
async fn move_renames_and_reports_conflicts() {
    let harness = harness().await;
    fs::write(harness.path("hola.txt"), "hola ñ\n").unwrap();
    fs::create_dir(harness.path("dir")).unwrap();
    fs::create_dir(harness.path("full")).unwrap();
    fs::write(harness.path("full/x"), b"x").unwrap();
    let moved = harness
        .rename(&harness.path("hola.txt"), &harness.path("dir/hola.txt"))
        .await
        .unwrap();
    assert_eq!(moved.path, harness.path("dir/hola.txt"));
    assert_eq!(moved.name, "hola.txt");
    assert!(!fs::exists(harness.path("hola.txt")).unwrap());
    assert_eq!(
        fs::read(harness.path("dir/hola.txt")).unwrap(),
        "hola ñ\n".as_bytes()
    );
    assert_eq!(
        harness
            .rename(&harness.path("missing"), &harness.path("x"))
            .await
            .unwrap_err()
            .code(),
        Code::NotFound
    );
    assert_eq!(
        harness
            .rename(&harness.path("dir/hola.txt"), &harness.path("full"))
            .await
            .unwrap_err()
            .code(),
        Code::FailedPrecondition
    );
    assert_eq!(
        harness
            .rename(&harness.path("dir/hola.txt"), &harness.path("nodir/x"))
            .await
            .unwrap_err()
            .code(),
        Code::NotFound
    );
}

#[tokio::test]
async fn remove_honours_recursive_and_symlinks() {
    let harness = harness().await;
    fs::create_dir(harness.path("many")).unwrap();
    fs::write(harness.path("many/a"), b"a").unwrap();
    fs::write(harness.path("big.bin"), b"b").unwrap();
    symlink("big.bin", harness.path("link")).unwrap();
    fs::create_dir(harness.path("target")).unwrap();
    fs::write(harness.path("target/keep"), b"k").unwrap();
    symlink(harness.path("target"), harness.path("dir-link")).unwrap();
    assert_eq!(
        harness
            .remove(&harness.path("many"), false)
            .await
            .unwrap_err()
            .code(),
        Code::FailedPrecondition
    );
    harness.remove(&harness.path("many"), true).await.unwrap();
    assert!(!fs::exists(harness.path("many")).unwrap());
    assert_eq!(
        harness
            .remove(&harness.path("many"), true)
            .await
            .unwrap_err()
            .code(),
        Code::NotFound
    );
    harness.remove(&harness.path("link"), true).await.unwrap();
    assert!(fs::exists(harness.path("big.bin")).unwrap());
    harness
        .remove(&harness.path("dir-link"), true)
        .await
        .unwrap();
    assert!(fs::exists(harness.path("target/keep")).unwrap());
    harness
        .remove(&harness.path("big.bin"), false)
        .await
        .unwrap();
    assert!(!fs::exists(harness.path("big.bin")).unwrap());
}

#[tokio::test]
async fn watch_reports_started_first_then_events_with_relative_names() {
    let harness = harness().await;
    fs::create_dir(harness.path("watch")).unwrap();
    let started = Instant::now();
    let mut stream = harness
        .watch(&harness.path("watch"), false, false)
        .await
        .unwrap();
    fs::write(harness.path("watch/a.txt"), b"hola").unwrap();
    expect_started(&mut stream).await;
    println!("WatchStarted in-process: {:?}", started.elapsed());
    let create = next_event(&mut stream).await.unwrap().unwrap();
    assert_eq!(
        typed(&create),
        ("a.txt".to_owned(), FilesystemEventType::Create)
    );
    assert!(create.entry.is_none());
    let write = next_event(&mut stream).await.unwrap().unwrap();
    assert_eq!(
        typed(&write),
        ("a.txt".to_owned(), FilesystemEventType::Write)
    );
    fs::set_permissions(
        harness.path("watch/a.txt"),
        fs::Permissions::from_mode(0o600),
    )
    .unwrap();
    let chmod = next_event(&mut stream).await.unwrap().unwrap();
    assert_eq!(
        typed(&chmod),
        ("a.txt".to_owned(), FilesystemEventType::Chmod)
    );
    fs::rename(harness.path("watch/a.txt"), harness.path("watch/b.txt")).unwrap();
    let from = next_event(&mut stream).await.unwrap().unwrap();
    assert_eq!(
        typed(&from),
        ("a.txt".to_owned(), FilesystemEventType::Rename)
    );
    let to = next_event(&mut stream).await.unwrap().unwrap();
    assert_eq!(
        typed(&to),
        ("b.txt".to_owned(), FilesystemEventType::Rename)
    );
    fs::remove_file(harness.path("watch/b.txt")).unwrap();
    let removed = next_event(&mut stream).await.unwrap().unwrap();
    assert_eq!(
        typed(&removed),
        ("b.txt".to_owned(), FilesystemEventType::Remove)
    );
    fs::write(harness.path("outside.txt"), b"x").unwrap();
    fs::write(harness.path("watch/last.txt"), b"x").unwrap();
    let events = collect_until(&mut stream, ("last.txt", FilesystemEventType::Create)).await;
    assert!(
        events
            .iter()
            .all(|(name, _)| !name.contains('/') && !name.contains("outside"))
    );
}

#[tokio::test]
async fn watch_recursive_names_and_include_entry() {
    let harness = harness().await;
    fs::create_dir(harness.path("watch")).unwrap();
    let mut stream = harness
        .watch(&harness.path("watch"), true, true)
        .await
        .unwrap();
    expect_started(&mut stream).await;
    fs::create_dir(harness.path("watch/sub")).unwrap();
    let sub = next_event(&mut stream).await.unwrap().unwrap();
    assert_eq!(typed(&sub), ("sub".to_owned(), FilesystemEventType::Create));
    let entry = sub.entry.expect("include_entry fills CREATE");
    assert_eq!(entry.r#type, i32::from(FileType::Directory));
    assert_eq!(entry.path, harness.path("watch/sub"));
    assert_eq!(entry.name, "sub");
    tokio::time::sleep(Duration::from_millis(100)).await;
    fs::write(harness.path("watch/sub/n.txt"), b"x").unwrap();
    let events = collect_until(&mut stream, ("sub/n.txt", FilesystemEventType::Create)).await;
    assert!(events.contains(&("sub/n.txt".to_owned(), FilesystemEventType::Create)));
    fs::set_permissions(
        harness.path("watch/sub/n.txt"),
        fs::Permissions::from_mode(0o600),
    )
    .unwrap();
    let events = collect_until(&mut stream, ("sub/n.txt", FilesystemEventType::Chmod)).await;
    let (_, chmod) = events.last().unwrap();
    assert_eq!(*chmod, FilesystemEventType::Chmod);
}

/// A symlink to a directory outside the root is an entry of the watched
/// tree, never a watched directory: changes behind it are not reported.
#[tokio::test]
async fn recursive_watch_never_follows_symlinks() {
    let harness = harness().await;
    fs::create_dir(harness.path("outside")).unwrap();
    fs::create_dir(harness.path("watch")).unwrap();
    symlink(harness.path("outside"), harness.path("watch/link")).unwrap();
    let mut stream = harness.watch_started(&harness.path("watch"), true).await;
    fs::write(harness.path("outside/behind.txt"), b"x").unwrap();
    symlink(harness.path("outside"), harness.path("watch/late-link")).unwrap();
    tokio::time::sleep(Duration::from_millis(200)).await;
    fs::write(harness.path("outside/behind-late.txt"), b"x").unwrap();
    fs::write(harness.path("watch/marker"), b"m").unwrap();
    let events = collect_until(&mut stream, ("marker", FilesystemEventType::Create)).await;
    assert!(
        events.iter().any(|(name, _)| name == "late-link"),
        "{events:?}"
    );
    assert!(
        events.iter().all(|(name, _)| !name.contains("behind")),
        "{events:?}"
    );
}

#[tokio::test]
async fn include_entry_reports_the_mode_after_chmod() {
    let harness = harness().await;
    fs::create_dir(harness.path("watch")).unwrap();
    fs::write(harness.path("watch/w.txt"), b"w").unwrap();
    let mut stream = harness
        .watch(&harness.path("watch"), false, true)
        .await
        .unwrap();
    expect_started(&mut stream).await;
    fs::set_permissions(
        harness.path("watch/w.txt"),
        fs::Permissions::from_mode(0o600),
    )
    .unwrap();
    let chmod = next_event(&mut stream).await.unwrap().unwrap();
    assert_eq!(
        typed(&chmod),
        ("w.txt".to_owned(), FilesystemEventType::Chmod)
    );
    assert_eq!(chmod.entry.unwrap().mode, 0o600);
    fs::remove_file(harness.path("watch/w.txt")).unwrap();
    let removed = next_event(&mut stream).await.unwrap().unwrap();
    assert_eq!(removed.r#type, i32::from(FilesystemEventType::Remove));
    assert!(removed.entry.is_none());
}

#[tokio::test]
async fn write_rpc_into_a_watched_directory_surfaces_as_one_rename() {
    let harness = harness().await;
    fs::create_dir(harness.path("watch")).unwrap();
    let mut stream = harness.watch_started(&harness.path("watch"), false).await;
    harness
        .write(&harness.path("watch/w.txt"), b"w")
        .await
        .unwrap();
    fs::write(harness.path("watch/marker"), b"m").unwrap();
    let events = collect_until(&mut stream, ("marker", FilesystemEventType::Create)).await;
    let renames: Vec<_> = events
        .iter()
        .filter(|(name, kind)| name == "w.txt" && *kind == FilesystemEventType::Rename)
        .collect();
    assert_eq!(renames.len(), 1, "{events:?}");
    assert!(
        events
            .iter()
            .all(|(name, _)| !name.starts_with(TEMP_PREFIX))
    );
    assert!(
        !events
            .iter()
            .any(|(name, kind)| name == "w.txt" && *kind != FilesystemEventType::Rename),
        "{events:?}"
    );
}

#[tokio::test]
async fn watch_rejections_caps_and_release_on_drop() {
    let harness = harness_with(Options {
        max_watches: 2,
        ..Options::default()
    })
    .await;
    fs::write(harness.path("file"), b"f").unwrap();
    fs::create_dir(harness.path("watch")).unwrap();
    assert_eq!(
        harness
            .watch(&harness.path("nope"), false, false)
            .await
            .unwrap_err()
            .code(),
        Code::NotFound
    );
    assert_eq!(
        harness
            .watch(&harness.path("file"), false, false)
            .await
            .unwrap_err()
            .code(),
        Code::InvalidArgument
    );
    assert_eq!(
        harness
            .watch("/etc", false, false)
            .await
            .unwrap_err()
            .code(),
        Code::PermissionDenied
    );
    assert_eq!(harness.manager.live_watches(), 0);
    let first = harness.watch_started(&harness.path("watch"), false).await;
    let second = harness.watch_started(&harness.path("watch"), false).await;
    assert_eq!(harness.manager.live_watches(), 2);
    assert_eq!(
        harness
            .watch(&harness.path("watch"), false, false)
            .await
            .unwrap_err()
            .code(),
        Code::ResourceExhausted
    );
    drop(first);
    drop(second);
    let deadline = Instant::now() + Duration::from_secs(1);
    while harness.manager.live_watches() > 0 {
        assert!(Instant::now() < deadline, "watches never released");
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    harness.watch_started(&harness.path("watch"), false).await;
}

#[tokio::test]
async fn silent_watch_sends_keepalives() {
    let harness = harness().await;
    fs::create_dir(harness.path("watch")).unwrap();
    let mut stream = harness.watch_started(&harness.path("watch"), false).await;
    let deadline = Instant::now() + Duration::from_secs(1);
    let mut keepalives = 0;
    while Instant::now() < deadline && keepalives < 3 {
        let message = tokio::time::timeout(Duration::from_secs(1), stream.message())
            .await
            .unwrap()
            .unwrap()
            .unwrap();
        if matches!(message.event, Some(watch_dir_response::Event::Keepalive(_))) {
            keepalives += 1;
        }
    }
    assert!(keepalives >= 3, "only {keepalives} keepalives in 1 s");
}

#[tokio::test]
async fn stalled_watch_overflows_delivers_the_buffer_then_resource_exhausted() {
    let harness = harness_with(Options {
        watch_queue_capacity: 4,
        ..Options::default()
    })
    .await;
    fs::create_dir(harness.path("watch")).unwrap();
    let mut stream = harness
        .manager
        .watch_dir(None, harness.path("watch"), false, false)
        .await
        .unwrap();
    for index in 0..40 {
        fs::create_dir(harness.path(&format!("watch/d{index}"))).unwrap();
    }
    tokio::time::sleep(Duration::from_millis(300)).await;
    assert_eq!(stream.next().await.unwrap().unwrap(), WatchItem::Started);
    let mut delivered = 0;
    let end = loop {
        match stream.next().await.unwrap() {
            Ok(WatchItem::Event { .. }) => delivered += 1,
            Ok(WatchItem::Started) => panic!("started twice"),
            Err(error) => break error,
        }
    };
    assert_eq!(end, FilesystemError::WatchOverflow);
    assert!((1..40).contains(&delivered), "delivered {delivered}");
    assert!(stream.next().await.is_none());
}

#[tokio::test]
async fn removing_the_watched_root_ends_with_not_found() {
    let harness = harness().await;
    fs::create_dir(harness.path("watch")).unwrap();
    let mut stream = harness.watch_started(&harness.path("watch"), false).await;
    fs::remove_dir(harness.path("watch")).unwrap();
    let trailing = loop {
        match next_event(&mut stream).await {
            Ok(Some(_)) => {}
            Ok(None) => panic!("stream ended without a status"),
            Err(status) => break status,
        }
    };
    assert_eq!(trailing.code(), Code::NotFound);
    assert_eq!(trailing.message(), "watched directory removed");
}

#[tokio::test]
async fn streams_are_unavailable_while_suspending() {
    let harness = harness().await;
    fs::write(harness.path("f"), b"f").unwrap();
    harness.post(Hook::Suspend, None).await;
    assert_eq!(
        harness.read(&harness.path("f")).await.unwrap_err().code(),
        Code::Unavailable
    );
    assert_eq!(
        harness
            .write(&harness.path("g"), b"g")
            .await
            .unwrap_err()
            .code(),
        Code::Unavailable
    );
    assert_eq!(
        harness
            .watch(&harness.root, false, false)
            .await
            .unwrap_err()
            .code(),
        Code::Unavailable
    );
    assert!(harness.stat(&harness.path("f")).await.is_ok());
    harness.post(Hook::Resume, None).await;
    assert!(harness.read(&harness.path("f")).await.is_ok());
}

#[tokio::test]
async fn root_is_refused_without_allow_root() {
    let harness = harness_with(Options {
        allow_root: false,
        ..Options::default()
    })
    .await;
    let status = harness
        .files
        .clone()
        .stat(authenticated(StatRequest {
            path: "/".to_owned(),
            user: Some(user("root")),
        }))
        .await
        .unwrap_err();
    assert_eq!(status.code(), Code::PermissionDenied);
}

/// `user` is uid 1000 with the playground as home, without needing an
/// account on the box; `root` is uid 0.
struct PlaygroundLookup {
    home: String,
}

impl UserLookup for PlaygroundLookup {
    fn lookup(&self, username: &str) -> Result<ProcessIdentity, LookupError> {
        match username {
            "user" => Ok(ProcessIdentity {
                uid: 1000,
                gid: 1000,
                groups: vec![1000],
                username: "user".to_owned(),
                home: self.home.clone(),
                shell: "/bin/bash".to_owned(),
            }),
            "root" => Ok(ProcessIdentity {
                uid: 0,
                gid: 0,
                groups: vec![0],
                username: "root".to_owned(),
                home: "/root".to_owned(),
                shell: "/bin/sh".to_owned(),
            }),
            _ => Err(LookupError::UnknownUser),
        }
    }
}

async fn identity_harness() -> Harness {
    let playground = tempfile::tempdir().unwrap();
    let home = fs::canonicalize(playground.path())
        .unwrap()
        .to_string_lossy()
        .into_owned();
    fs::set_permissions(&home, fs::Permissions::from_mode(0o777)).unwrap();
    let lookup: Arc<dyn UserLookup> = Arc::new(PlaygroundLookup { home: home.clone() });
    let mut harness = harness_with(Options {
        allow_root: true,
        lookup: Some(lookup),
        ..Options::default()
    })
    .await;
    harness.playground = playground;
    harness.root = home;
    harness
}

/// Every inotify watch of a recursive `WatchDir` is installed as the
/// requesting user (SECURITY.md T11): a root-only subdirectory, present
/// before the watch or created after it, never leaks the names inside it,
/// while a readable new directory is followed.
#[tokio::test]
async fn recursive_watch_as_user_skips_root_only_directories() {
    if !running_as_root() {
        return;
    }
    let harness = identity_harness().await;
    let make_dir = |name: &str, mode: u32| {
        std::fs::DirBuilder::new()
            .mode(mode)
            .create(harness.path(name))
            .unwrap();
    };
    make_dir("secret", 0o700);
    let mut stream = harness
        .watch_as(&harness.root, true, false, Some(user("user")))
        .await
        .unwrap();
    expect_started(&mut stream).await;
    fs::write(harness.path("secret/leak"), b"root only").unwrap();
    make_dir("late-secret", 0o700);
    make_dir("open", 0o755);
    tokio::time::sleep(Duration::from_millis(300)).await;
    fs::write(harness.path("late-secret/leak"), b"root only").unwrap();
    fs::write(harness.path("open/seen"), b"public").unwrap();
    fs::write(harness.path("marker"), b"m").unwrap();
    let events = collect_until(&mut stream, ("marker", FilesystemEventType::Create)).await;
    assert!(
        events.iter().all(|(name, _)| !name.contains("leak")),
        "{events:?}"
    );
    assert!(
        events.contains(&("late-secret".to_owned(), FilesystemEventType::Create)),
        "{events:?}"
    );
    assert!(
        events.contains(&("open/seen".to_owned(), FilesystemEventType::Create)),
        "{events:?}"
    );
}

/// A symlink to `/etc` inside a recursive watch is never followed, so
/// changes under `/etc` (denied and root-owned) never surface.
#[tokio::test]
async fn recursive_watch_never_reports_etc_through_a_symlink() {
    if !running_as_root() {
        return;
    }
    let harness = identity_harness().await;
    symlink("/etc", harness.path("etc-link")).unwrap();
    let mut stream = harness
        .watch_as(&harness.root, true, false, Some(user("user")))
        .await
        .unwrap();
    expect_started(&mut stream).await;
    let probe = "/etc/rayito-m3-watch-probe";
    fs::write(probe, b"x").unwrap();
    fs::remove_file(probe).unwrap();
    fs::write(harness.path("marker"), b"m").unwrap();
    let events = collect_until(&mut stream, ("marker", FilesystemEventType::Create)).await;
    assert!(
        events
            .iter()
            .all(|(name, _)| !name.contains("probe") && !name.starts_with("etc-link/")),
        "{events:?}"
    );
}

#[tokio::test]
async fn identity_is_enforced_per_thread_when_root() {
    if !running_as_root() {
        return;
    }
    let harness = identity_harness().await;
    let secret = harness.path("root-only");
    fs::write(&secret, b"top secret").unwrap();
    fs::set_permissions(&secret, fs::Permissions::from_mode(0o600)).unwrap();
    assert_eq!(
        harness
            .read_as(&secret, Some(user("user")))
            .await
            .unwrap_err()
            .code(),
        Code::PermissionDenied
    );
    assert!(harness.read_as(&secret, Some(user("root"))).await.is_ok());
    let mut client = harness.files.clone();
    let path = harness.path("owned.txt");
    let response = client
        .write(authenticated(tokio_stream::iter(vec![WriteRequest {
            path: Some(path.clone()),
            user: Some(user("user")),
            mode: None,
            chunk: b"mine".to_vec(),
        }])))
        .await
        .unwrap()
        .into_inner();
    assert_eq!(response.entries[0].owner, "1000".to_owned());
    let metadata = fs::metadata(&path).unwrap();
    assert_eq!(metadata.uid(), 1000);
    assert_eq!(metadata.gid(), 1000);
    let dir = harness.path("user-dir");
    client
        .make_dir(authenticated(MakeDirRequest {
            path: dir.clone(),
            user: Some(user("user")),
        }))
        .await
        .unwrap();
    assert_eq!(fs::metadata(&dir).unwrap().uid(), 1000);
    let list_as_user = client
        .list_dir(authenticated(ListDirRequest {
            path: "/root".to_owned(),
            depth: 1,
            user: Some(user("user")),
        }))
        .await;
    assert_eq!(list_as_user.unwrap_err().code(), Code::PermissionDenied);
    let mut tasks = Vec::new();
    for round in 0..8 {
        let harness_secret = secret.clone();
        let mut as_root = harness.files.clone();
        let mut as_user = harness.files.clone();
        tasks.push(tokio::spawn(async move {
            for _ in 0..10 {
                let root_read = as_root
                    .read(authenticated(ReadRequest {
                        path: harness_secret.clone(),
                        user: Some(user("root")),
                    }))
                    .await;
                let user_read = as_user
                    .read(authenticated(ReadRequest {
                        path: harness_secret.clone(),
                        user: Some(user("user")),
                    }))
                    .await;
                assert!(root_read.is_ok(), "round {round}: root refused");
                assert_eq!(
                    user_read.unwrap_err().code(),
                    Code::PermissionDenied,
                    "round {round}: user allowed"
                );
            }
        }));
    }
    for task in tasks {
        task.await.unwrap();
    }
}
