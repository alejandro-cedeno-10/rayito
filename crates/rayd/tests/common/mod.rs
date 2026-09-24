//! The in-process `rayd` the M6 suites drive: the real process, PTY and
//! filesystem managers, the fake sidecar of `tests/fixtures/fake_sidecar.py`,
//! the gRPC router on a loopback port and the hooks router called directly,
//! with the knobs the hardening tests scale (session settings, output
//! budget, the filesystem's free space, the IMDS state), a session clock
//! the tests can jump like a restore does, and the logical deadline's
//! watcher over the real exit sequence with the process exit replaced by a
//! counter (ADR-011). A suite may wire the presigned-transfer manager over
//! its own `SignedHttp` (ADR-010); the others get the `UNIMPLEMENTED`
//! backend and a barrier that never waits.
#![cfg(unix)]
#![allow(
    dead_code,
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::too_many_lines
)]

pub mod log_capture;

use std::fs;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, Once};
use std::time::{Duration, Instant, SystemTime};

use axum::Router;
use axum::body::Body;
use base64::Engine;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use http::{Request, StatusCode};
use rayd::adapters::{
    ImdsState, OsRandomSource, PlatformMetricsProbe, PlatformNameResolver, PlatformWatcher,
    StdFileSystem, TokioSidecarLauncher, UserConnectProbe, detect_spawn_platform,
};
use rayd::code::{
    CodeManager, CodeSettings, KernelSignaller, OpTimeouts, SidecarSupervisor, sidecar_identity,
};
use rayd::filesystem::{FilesystemManager, FilesystemPlatform, FilesystemSettings};
use rayd::grpc::{PlatformProcessManager, Services, StreamSettings, TransferServices};
use rayd::hooks::{HookReply, HookServices, hook_path};
use rayd::lifecycle::{
    ExitParts, ExitReason, ExitTerminator, StreamCloser, SuspendSignal, TimeoutWatcher,
    spawn_timeout_watcher,
};
use rayd::process::{ManagerSettings, platform_manager, shared_registry_with_budget};
use rayd::pty::{PtySettings, platform_pty_manager};
use rayd_core::clock::{Clock, SystemClock};
use rayd_core::code::{
    ContextRegistry, ExecutionLimits, KernelSidecar, SidecarConfig, sidecar_spawn_spec,
};
use rayd_core::filesystem::{
    DenyList, EntryKind, FileMetadata, FileSystem, FsIdentity, FsIoError, OpenedSnapshot, RawEntry,
    WriteSink,
};
use rayd_core::lifecycle::Hook;
use rayd_core::metrics_history::MetricsHistory;
use rayd_core::process::{OutputBudget, RegistryLimits, UserPolicy};
use rayd_core::sandbox_timeout::SelfTerminator;
use rayd_core::session::{SandboxSession, SessionSettings};
use rayito_proto::v1::code_service_client::CodeServiceClient;
use rayito_proto::v1::filesystem_service_client::FilesystemServiceClient;
use rayito_proto::v1::health_service_client::HealthServiceClient;
use rayito_proto::v1::lifecycle_service_client::LifecycleServiceClient;
use rayito_proto::v1::process_service_client::ProcessServiceClient;
use rayito_proto::v1::pty_service_client::PtyServiceClient;
use rayito_proto::v1::{
    ConnectRequest, CreateContextRequest, HealthRequest, HealthResponse, ListRequest,
    ProcessConfig, ProcessEvent, ProcessInfo, StartRequest, process_event,
};
use sha2::{Digest, Sha256};
use tokio::net::TcpListener;
use tokio_util::sync::CancellationToken;
use tonic::metadata::MetadataValue;
use tonic::transport::Channel;
use tonic::transport::server::TcpIncoming;
use tonic::{Status, Streaming};
use tower::ServiceExt;

pub const SECRET: &[u8] = b"m6-hardening-secret";
pub const KEEPALIVE: Duration = Duration::from_millis(200);
pub const BUDGET: Duration = Duration::from_secs(10);
pub const TEMP_PREFIX: &str = ".rayito-tmp-";

/// `SystemClock` plus an offset the test can bump: what a restore looks
/// like to `CLOCK_MONOTONIC` (it jumps by the frozen time).
pub struct JumpClock {
    inner: SystemClock,
    offset_ms: AtomicU64,
}

impl JumpClock {
    pub fn new() -> Self {
        Self {
            inner: SystemClock::new(),
            offset_ms: AtomicU64::new(0),
        }
    }

    pub fn jump(&self, by: Duration) {
        self.offset_ms
            .fetch_add(u64::try_from(by.as_millis()).unwrap(), Ordering::SeqCst);
    }
}

impl Clock for JumpClock {
    fn monotonic(&self) -> Duration {
        self.inner.monotonic() + Duration::from_millis(self.offset_ms.load(Ordering::SeqCst))
    }

    fn wall(&self) -> SystemTime {
        self.inner.wall() + Duration::from_millis(self.offset_ms.load(Ordering::SeqCst))
    }
}

/// How the fake filesystem misbehaves on purpose.
#[derive(Debug, Clone)]
pub enum DiskFault {
    /// `free_bytes` answers whatever the shared cell holds (the test lowers
    /// it mid-stream).
    FreeBytes(Arc<AtomicU64>),
    /// Every `write_chunk` fails with `ENOSPC`.
    WriteEnospc,
}

/// The real `StdFileSystem` with one fault injected.
pub struct FaultyFileSystem {
    inner: StdFileSystem,
    fault: DiskFault,
}

impl FileSystem for FaultyFileSystem {
    fn canonicalize(&self, id: &FsIdentity, path: &str) -> Result<String, FsIoError> {
        self.inner.canonicalize(id, path)
    }

    fn lstat(&self, id: &FsIdentity, path: &str) -> Result<RawEntry, FsIoError> {
        self.inner.lstat(id, path)
    }

    fn read_dir(&self, id: &FsIdentity, path: &str) -> Result<Vec<RawEntry>, FsIoError> {
        self.inner.read_dir(id, path)
    }

    fn open_read(&self, id: &FsIdentity, path: &str) -> Result<Box<dyn Read + Send>, FsIoError> {
        self.inner.open_read(id, path)
    }

    fn open_snapshot(&self, id: &FsIdentity, path: &str) -> Result<OpenedSnapshot, FsIoError> {
        self.inner.open_snapshot(id, path)
    }

    fn read_metadata(&self, id: &FsIdentity, path: &str) -> Result<FileMetadata, FsIoError> {
        self.inner.read_metadata(id, path)
    }

    fn free_bytes(&self, id: &FsIdentity, dir: &str) -> Result<u64, FsIoError> {
        match &self.fault {
            DiskFault::FreeBytes(free) => Ok(free.load(Ordering::SeqCst)),
            DiskFault::WriteEnospc => self.inner.free_bytes(id, dir),
        }
    }

    fn begin_write(
        &self,
        id: &FsIdentity,
        dir: &str,
        mode: u32,
    ) -> Result<Box<dyn WriteSink>, FsIoError> {
        let sink = self.inner.begin_write(id, dir, mode)?;
        match &self.fault {
            DiskFault::WriteEnospc => Ok(Box::new(EnospcSink { inner: sink })),
            DiskFault::FreeBytes(_) => Ok(sink),
        }
    }

    fn make_dir(&self, id: &FsIdentity, path: &str, mode: u32) -> Result<(), FsIoError> {
        self.inner.make_dir(id, path, mode)
    }

    fn rename(&self, id: &FsIdentity, from: &str, to: &str) -> Result<(), FsIoError> {
        self.inner.rename(id, from, to)
    }

    fn remove(
        &self,
        id: &FsIdentity,
        path: &str,
        kind: EntryKind,
        recursive: bool,
    ) -> Result<(), FsIoError> {
        self.inner.remove(id, path, kind, recursive)
    }
}

struct EnospcSink {
    inner: Box<dyn WriteSink>,
}

impl WriteSink for EnospcSink {
    fn write_chunk(&mut self, _bytes: &[u8]) -> Result<(), FsIoError> {
        Err(FsIoError::NoSpace)
    }

    fn set_metadata(&mut self, metadata: &FileMetadata) -> Result<(), FsIoError> {
        self.inner.set_metadata(metadata)
    }

    fn commit(self: Box<Self>, final_name: &str, id: &FsIdentity) -> Result<RawEntry, FsIoError> {
        self.inner.commit(final_name, id)
    }
}

/// Builds the transfer backend and barrier over the harness's own session,
/// filesystem manager and suspend broadcast.
pub type TransferFactory = Box<
    dyn FnOnce(
            &Arc<SandboxSession>,
            &Arc<FilesystemManager>,
            &Arc<SuspendSignal>,
        ) -> TransferServices
        + Send,
>;

pub struct Options {
    pub run: bool,
    pub session: SessionSettings,
    pub budget: OutputBudget,
    pub disk_fault: Option<DiskFault>,
    pub op_timeouts: OpTimeouts,
    pub fake_flags: Vec<String>,
    pub imds: Arc<ImdsState>,
    pub user_probe: Option<UserConnectProbe>,
    /// Extra fields of the run payload (`"limits":{"cpu_seconds":1}`).
    pub payload_extra: serde_json::Map<String, serde_json::Value>,
    pub transfers: Option<TransferFactory>,
}

impl Default for Options {
    fn default() -> Self {
        Self {
            run: true,
            session: SessionSettings::default(),
            budget: OutputBudget::default(),
            disk_fault: None,
            op_timeouts: OpTimeouts {
                interrupt: Duration::from_secs(2),
                ..OpTimeouts::default()
            },
            fake_flags: Vec::new(),
            imds: Arc::new(ImdsState::default()),
            user_probe: None,
            payload_extra: serde_json::Map::new(),
            transfers: None,
        }
    }
}

pub struct Harness {
    pub processes: ProcessServiceClient<Channel>,
    pub ptys: PtyServiceClient<Channel>,
    pub files: FilesystemServiceClient<Channel>,
    pub code: CodeServiceClient<Channel>,
    pub health: HealthServiceClient<Channel>,
    pub lifecycle: LifecycleServiceClient<Channel>,
    pub hooks: Router,
    pub session: Arc<SandboxSession>,
    pub clock: Arc<JumpClock>,
    pub manager: Arc<CodeManager>,
    pub process_manager: Arc<PlatformProcessManager>,
    /// The ring `MetricsHistory` serves; no sampler runs in the harness, so
    /// tests seed it with `record`.
    pub history: Arc<MetricsHistory>,
    pub budget: OutputBudget,
    pub log_path: PathBuf,
    pub root: String,
    /// Cancelled by `/terminate` and by the kill-mode exit sequence.
    pub shutdown: CancellationToken,
    pub exit_reason: Arc<ExitReason>,
    /// How many times the watcher fell back to `force` (a real agent
    /// would have called `std::process::exit` instead).
    pub forced_exits: Arc<AtomicU64>,
    _tempdir: tempfile::TempDir,
}

pub fn fixture(name: &str) -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests")
        .join("fixtures")
        .join(name)
}

pub fn workspace_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .canonicalize()
        .unwrap()
}

/// Marks every descriptor this test process inherited (a CI runner can leave
/// pipes open past stdio) close-on-exec, so the agent's children see only
/// what the agent itself hands them, as they do inside a MicroVM.
fn seal_inherited_descriptors() {
    static SEAL: Once = Once::new();
    SEAL.call_once(|| {
        let descriptors: Vec<i32> = fs::read_dir("/proc/self/fd")
            .map(|entries| {
                entries
                    .filter_map(Result::ok)
                    .filter_map(|entry| entry.file_name().to_str()?.parse().ok())
                    .filter(|fd| *fd > 2)
                    .collect()
            })
            .unwrap_or_default();
        for fd in descriptors {
            let flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
            if flags >= 0 {
                unsafe { libc::fcntl(fd, libc::F_SETFD, flags | libc::FD_CLOEXEC) };
            }
        }
    });
}

pub fn running_as_root() -> bool {
    nix::unistd::geteuid().is_root()
}

pub async fn harness() -> Harness {
    harness_with(Options::default()).await
}

pub async fn harness_with(options: Options) -> Harness {
    seal_inherited_descriptors();
    let _ = rayd::logging::init();
    let tempdir = tempfile::tempdir().unwrap();
    let root = fs::canonicalize(tempdir.path())
        .unwrap()
        .to_string_lossy()
        .into_owned();
    let log_path = tempdir.path().join("requests.jsonl");
    let mut command = vec![
        "python3".to_owned(),
        fixture("fake_sidecar.py").to_string_lossy().into_owned(),
        "--log".to_owned(),
        log_path.to_string_lossy().into_owned(),
    ];
    command.extend(options.fake_flags.iter().cloned());
    let config = SidecarConfig::new(
        command,
        &workspace_root().join("kernel-sidecar").to_string_lossy(),
        &tempdir.path().join("k").to_string_lossy(),
    );
    let clock = Arc::new(JumpClock::new());
    let session = Arc::new(SandboxSession::with_settings(
        clock.clone(),
        "test",
        options.session,
    ));
    let policy = UserPolicy {
        allow_root: running_as_root(),
    };
    let platform = detect_spawn_platform();
    let files = filesystem_manager(&session, &platform, policy, options.disk_fault);
    let settings = CodeSettings {
        execute_keepalive_interval: KEEPALIVE,
        interrupt_grace: Duration::from_secs(1),
        stall_timeout: Duration::from_secs(1),
        queue_capacity: 16,
        context_ready_timeout: Duration::from_secs(5),
        sidecar_ready_timeout: Duration::from_secs(60),
        op_timeouts: options.op_timeouts,
        executions: ExecutionLimits::default(),
        sidecar: Some(config.clone()),
        output_budget: options.budget.clone(),
    };
    let manager = code_manager(&session, &platform, policy, settings, tempdir.path());
    let _supervisor = manager.spawn_supervisor();
    let registry = shared_registry_with_budget(RegistryLimits::default(), options.budget.clone());
    let ptys = platform_pty_manager(
        session.clone(),
        &platform,
        policy,
        registry.clone(),
        PtySettings {
            stall_timeout: Duration::from_secs(1),
            ..PtySettings::default()
        },
    );
    let processes = platform_manager(
        session.clone(),
        platform,
        policy,
        registry,
        ManagerSettings {
            stall_timeout: Duration::from_secs(1),
            ..ManagerSettings::default()
        },
    );
    let suspend = Arc::new(SuspendSignal::new());
    let transfers = options
        .transfers
        .map_or_else(TransferServices::unavailable, |factory| {
            factory(&session, &files, &suspend)
        });
    let history = Arc::new(MetricsHistory::default());
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let shutdown = CancellationToken::new();
    let exit_reason = Arc::new(ExitReason::default());
    let forced_exits = Arc::new(AtomicU64::new(0));
    let timeout = timeout_watcher(
        &session,
        ExitParts {
            runtime: tokio::runtime::Handle::current(),
            shutdown: shutdown.clone(),
            suspend: suspend.clone(),
            processes: processes.clone(),
            code: manager.clone(),
            reason: exit_reason.clone(),
            settings: session.settings().timeout,
        },
        forced_exits.clone(),
    );
    let network = rayd::network::NetworkManager::unavailable(session.clone());
    let grpc = rayd::grpc::router_with_transfers(
        Services {
            session: session.clone(),
            processes: processes.clone(),
            ptys,
            files,
            code: manager.clone(),
            metrics: Arc::new(PlatformMetricsProbe::default()),
            metrics_history: history.clone(),
            suspend: suspend.clone(),
            imds: options.imds.clone(),
            persistence: Arc::new(rayd::persistence::UnavailablePersistence),
            timeout: timeout.clone(),
            network: network.clone(),
        },
        StreamSettings {
            keepalive_interval: KEEPALIVE,
            watch_keepalive_interval: KEEPALIVE,
            execute_keepalive_interval: KEEPALIVE,
        },
        transfers,
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
    let hooks = rayd::hooks::router_with(HookServices {
        session: session.clone(),
        code: manager.clone(),
        suspend,
        shutdown: shutdown.clone(),
        imds: options.imds,
        user_probe: options.user_probe,
        timeout,
        network,
    });
    let harness = Harness {
        processes: ProcessServiceClient::new(channel.clone()),
        ptys: PtyServiceClient::new(channel.clone()),
        files: FilesystemServiceClient::new(channel.clone()),
        code: CodeServiceClient::new(channel.clone()),
        health: HealthServiceClient::new(channel.clone()),
        lifecycle: LifecycleServiceClient::new(channel),
        hooks,
        session,
        clock,
        manager,
        process_manager: processes,
        history,
        budget: options.budget,
        log_path,
        root,
        shutdown,
        exit_reason,
        forced_exits,
        _tempdir: tempdir,
    };
    if options.run {
        harness.wait_kernel_ready(Duration::from_secs(30)).await;
        harness
            .post(Hook::Run, Some(run_envelope(&options.payload_extra)))
            .await;
        harness.wait_kernel_ready(Duration::from_secs(30)).await;
    }
    harness
}

/// The real watcher and exit sequence; only `std::process::exit` is
/// replaced, by a counter.
fn timeout_watcher(
    session: &Arc<SandboxSession>,
    parts: ExitParts<rayd::adapters::PlatformSpawner>,
    forced_exits: Arc<AtomicU64>,
) -> Arc<TimeoutWatcher> {
    let closer = StreamCloser::new(parts.code.clone(), parts.suspend.clone());
    let terminator: Arc<dyn SelfTerminator> = Arc::new(ExitTerminator::new(parts).with_force_exit(
        Arc::new(move |_| {
            forced_exits.fetch_add(1, Ordering::SeqCst);
        }),
    ));
    spawn_timeout_watcher(session.clone(), terminator, closer).unwrap()
}

fn filesystem_manager(
    session: &Arc<SandboxSession>,
    platform: &rayd::adapters::SpawnPlatform,
    policy: UserPolicy,
    fault: Option<DiskFault>,
) -> Arc<FilesystemManager> {
    let inner = StdFileSystem::new(platform.identity_switch);
    let fs: Arc<dyn FileSystem> = match fault {
        Some(fault) => Arc::new(FaultyFileSystem { inner, fault }),
        None => Arc::new(inner),
    };
    FilesystemManager::new(
        session.clone(),
        FilesystemPlatform {
            fs,
            watcher: Arc::new(PlatformWatcher::new(platform.identity_switch)),
            names: Arc::new(PlatformNameResolver::default()),
            lookup: platform.lookup.clone(),
        },
        policy,
        DenyList::default(),
        FilesystemSettings {
            write_error_drain_timeout: Duration::from_millis(500),
            ..FilesystemSettings::default()
        },
    )
}

fn code_manager(
    session: &Arc<SandboxSession>,
    platform: &rayd::adapters::SpawnPlatform,
    policy: UserPolicy,
    settings: CodeSettings,
    cwd: &Path,
) -> Arc<CodeManager> {
    let config = settings.sidecar.clone().unwrap();
    let identity = sidecar_identity(session, platform.lookup.as_ref(), policy).unwrap();
    let mut spec = sidecar_spawn_spec(&identity, &config);
    spec.cwd = cwd.to_string_lossy().into_owned();
    let launcher: Arc<dyn KernelSidecar> =
        Arc::new(TokioSidecarLauncher::new(platform.identity_switch));
    let registry = Arc::new(Mutex::new(ContextRegistry::default()));
    let kernel_killer: KernelSignaller = Arc::new(|_, _| {});
    let supervisor = SidecarSupervisor::new(
        launcher,
        spec,
        session.clone(),
        registry.clone(),
        settings.supervisor_settings(),
        kernel_killer,
    );
    CodeManager::new(
        session.clone(),
        Some(supervisor),
        registry,
        Arc::new(OsRandomSource),
        settings,
        identity.home,
    )
}

pub fn digest_hex(secret: &[u8]) -> String {
    use std::fmt::Write as _;
    Sha256::digest(secret)
        .iter()
        .fold(String::new(), |mut hex, byte| {
            write!(hex, "{byte:02x}").unwrap();
            hex
        })
}

pub fn run_payload(secret: &[u8], extra: &serde_json::Map<String, serde_json::Value>) -> String {
    let mut payload = serde_json::json!({
        "v": 1,
        "token_sha256": digest_hex(secret),
        "workdir": "/tmp",
        "envs": {"M6": "1"},
    });
    if running_as_root() {
        payload["user"] = serde_json::Value::String("root".to_owned());
    }
    for (key, value) in extra {
        payload[key] = value.clone();
    }
    payload.to_string()
}

pub fn run_envelope(extra: &serde_json::Map<String, serde_json::Value>) -> String {
    serde_json::json!({ "microvmId": "mvm-m6", "runHookPayload": run_payload(SECRET, extra) })
        .to_string()
}

pub fn forged_envelope() -> String {
    serde_json::json!({
        "microvmId": "mvm-forged",
        "runHookPayload": run_payload(b"attacker", &serde_json::Map::new()),
    })
    .to_string()
}

pub fn authenticated<T>(message: T) -> tonic::Request<T> {
    let mut request = tonic::Request::new(message);
    let encoded = URL_SAFE_NO_PAD.encode(SECRET);
    request
        .metadata_mut()
        .insert("x-access-token", MetadataValue::try_from(encoded).unwrap());
    request
}

pub fn shell(script: &str, timeout_ms: u64) -> StartRequest {
    StartRequest {
        process: Some(ProcessConfig {
            cmd: "/bin/sh".to_owned(),
            args: vec!["-c".to_owned(), script.to_owned()],
            envs: std::collections::HashMap::new(),
            cwd: None,
        }),
        user: None,
        timeout_ms,
        stdin: false,
        tag: None,
    }
}

pub fn temp_files(dir: &str) -> usize {
    fs::read_dir(dir)
        .unwrap()
        .filter_map(Result::ok)
        .filter(|entry| entry.file_name().to_string_lossy().starts_with(TEMP_PREFIX))
        .count()
}

/// The payload of a `DataEvent`, whichever stream it came from.
pub fn data_bytes(data: &rayito_proto::v1::DataEvent) -> &[u8] {
    use rayito_proto::v1::data_event::Output;
    match &data.output {
        Some(Output::Stdout(bytes) | Output::Stderr(bytes)) => bytes,
        None => &[],
    }
}

/// A closed process stream: the data bytes, the seqs and how it ended.
pub struct ProcessTail {
    pub bytes: usize,
    pub seqs: Vec<u64>,
    pub end: Option<rayito_proto::v1::EndEvent>,
    pub status: Option<Status>,
}

pub async fn drain_process(stream: &mut Streaming<ProcessEvent>, budget: Duration) -> ProcessTail {
    let mut tail = ProcessTail {
        bytes: 0,
        seqs: Vec::new(),
        end: None,
        status: None,
    };
    loop {
        match tokio::time::timeout(budget, stream.message())
            .await
            .expect("stream stalled")
        {
            Ok(Some(event)) => match event.event {
                Some(process_event::Event::Data(data)) => {
                    tail.seqs.push(data.seq);
                    tail.bytes += data_bytes(&data).len();
                }
                Some(process_event::Event::End(end)) => {
                    tail.end = Some(end);
                    return tail;
                }
                _ => {}
            },
            Ok(None) => return tail,
            Err(status) => {
                tail.status = Some(status);
                return tail;
            }
        }
    }
}

impl Harness {
    pub async fn post(&self, hook: Hook, body: Option<String>) -> (StatusCode, HookReply) {
        let request = Request::post(hook_path(hook))
            .header("content-type", "application/json")
            .body(body.map_or_else(Body::empty, Body::from))
            .unwrap();
        let response = self.hooks.clone().oneshot(request).await.unwrap();
        let status = response.status();
        let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap();
        (status, serde_json::from_slice(&bytes).unwrap())
    }

    pub async fn health(&self) -> HealthResponse {
        self.health
            .clone()
            .health(HealthRequest {})
            .await
            .unwrap()
            .into_inner()
    }

    pub async fn wait_kernel_ready(&self, budget: Duration) {
        let deadline = Instant::now() + budget;
        while !self.health().await.kernel_ready {
            assert!(Instant::now() < deadline, "kernel never became ready");
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
    }

    pub async fn start(
        &self,
        request: StartRequest,
    ) -> Result<(u32, Streaming<ProcessEvent>), Status> {
        let mut stream = self
            .processes
            .clone()
            .start(authenticated(request))
            .await?
            .into_inner();
        let first = stream.message().await.unwrap().unwrap();
        match first.event {
            Some(process_event::Event::Start(start)) => Ok((start.pid, stream)),
            other => panic!("expected StartEvent, got {other:?}"),
        }
    }

    pub async fn connect(
        &self,
        pid: u32,
        from_seq: u64,
    ) -> Result<Streaming<ProcessEvent>, Status> {
        let mut stream = self
            .processes
            .clone()
            .connect(authenticated(ConnectRequest { pid, from_seq }))
            .await?
            .into_inner();
        let first = stream.message().await.unwrap().unwrap();
        assert!(matches!(first.event, Some(process_event::Event::Start(_))));
        Ok(stream)
    }

    pub async fn list(&self) -> Vec<ProcessInfo> {
        self.processes
            .clone()
            .list(authenticated(ListRequest {}))
            .await
            .unwrap()
            .into_inner()
            .processes
    }

    pub async fn create_context(&self) -> Result<String, Status> {
        self.code
            .clone()
            .create_context(authenticated(CreateContextRequest {
                language: String::new(),
                cwd: None,
                envs: std::collections::HashMap::new(),
            }))
            .await
            .map(|response| response.into_inner().context_id)
    }

    /// Every request line the fake sidecar logged, in order.
    pub fn requests(&self) -> Vec<serde_json::Value> {
        fs::read_to_string(&self.log_path)
            .unwrap_or_default()
            .lines()
            .filter(|line| !line.is_empty())
            .map(|line| serde_json::from_str::<serde_json::Value>(line).unwrap())
            .collect()
    }

    pub fn requests_of(&self, op: &str) -> Vec<serde_json::Value> {
        self.requests()
            .into_iter()
            .filter(|request| request["op"] == op)
            .collect()
    }

    pub async fn wait_for_request(&self, op: &str, budget: Duration) -> serde_json::Value {
        let deadline = Instant::now() + budget;
        loop {
            if let Some(request) = self.requests_of(op).into_iter().last() {
                return request;
            }
            assert!(Instant::now() < deadline, "the fake never received {op}");
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    }

    pub fn path(&self, name: &str) -> String {
        format!("{}/{name}", self.root)
    }
}
