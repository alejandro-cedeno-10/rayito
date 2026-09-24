//! `/suspend` and `/resume` end to end, in process (design D16, the
//! `m5_suspend_resume` list): every kind of client stream open across the
//! hooks, the close form each one gets, nothing killed, the sidecar
//! quiesced and probed, replay after the resume, the running-clock
//! re-arm of timeouts, lost kernels and a slow probe. The fake sidecar of
//! `tests/fixtures/fake_sidecar.py` plays the kernels; the processes and
//! PTYs are real.
//!
//! Integration tests are test code, but clippy's `allow-unwrap-in-tests` only
//! recognises `#[test]` functions and `#[cfg(test)]` modules.
#![cfg(unix)]
#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::too_many_lines
)]

use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use axum::Router;
use axum::body::Body;
use base64::Engine;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use http::{Request, StatusCode};
use rayd::adapters::{
    OsRandomSource, PlatformMetricsProbe, TokioSidecarLauncher, detect_spawn_platform,
};
use rayd::code::{
    CodeManager, CodeSettings, KernelSignaller, OpTimeouts, SidecarSupervisor, sidecar_identity,
};
use rayd::filesystem::{FilesystemSettings, platform_filesystem_manager};
use rayd::grpc::{Services, StreamSettings};
use rayd::hooks::{HookReply, hook_path};
use rayd::lifecycle::SuspendSignal;
use rayd::process::{ManagerSettings, platform_manager, shared_registry};
use rayd::pty::{PtySettings, platform_pty_manager};
use rayd_core::clock::SystemClock;
use rayd_core::code::{
    ContextRegistry, ExecutionLimits, KernelSidecar, SidecarConfig, sidecar_spawn_spec,
};
use rayd_core::filesystem::DenyList;
use rayd_core::lifecycle::Hook;
use rayd_core::process::{RegistryLimits, UserPolicy};
use rayd_core::session::SandboxSession;
use rayito_proto::v1::code_service_client::CodeServiceClient;
use rayito_proto::v1::filesystem_service_client::FilesystemServiceClient;
use rayito_proto::v1::health_service_client::HealthServiceClient;
use rayito_proto::v1::process_service_client::ProcessServiceClient;
use rayito_proto::v1::pty_service_client::PtyServiceClient;
use rayito_proto::v1::{
    ConnectRequest, CreateContextRequest, ExecuteEvent, ExecuteRequest, HealthRequest,
    HealthResponse, ListRequest, ProcessConfig, ProcessEvent, ProcessInfo, PtyServerMessage,
    PtyStart, ReadRequest, ReattachRequest, SendInputRequest, StartRequest, WatchDirRequest,
    WriteRequest, execute_event, process_event, pty_server_message, watch_dir_response,
};
use sha2::{Digest, Sha256};
use tokio::net::TcpListener;
use tokio::sync::mpsc;
use tokio::task::JoinHandle;
use tokio_stream::wrappers::ReceiverStream;
use tokio_util::sync::CancellationToken;
use tonic::metadata::MetadataValue;
use tonic::transport::Channel;
use tonic::transport::server::TcpIncoming;
use tonic::{Code, Status, Streaming};
use tower::ServiceExt;

const SECRET: &[u8] = b"m5-suspend-secret";
const KEEPALIVE: Duration = Duration::from_millis(200);
const TEMP_PREFIX: &str = ".rayito-tmp-";
const BUDGET: Duration = Duration::from_secs(10);

struct Options {
    run: bool,
    stall_timeout: Duration,
    op_timeouts: OpTimeouts,
    execution_retention: Duration,
    fake_flags: Vec<String>,
}

impl Default for Options {
    fn default() -> Self {
        Self {
            run: true,
            stall_timeout: Duration::from_secs(1),
            op_timeouts: OpTimeouts {
                interrupt: Duration::from_secs(2),
                ..OpTimeouts::default()
            },
            execution_retention: Duration::from_secs(30),
            fake_flags: Vec::new(),
        }
    }
}

struct Harness {
    processes: ProcessServiceClient<Channel>,
    ptys: PtyServiceClient<Channel>,
    files: FilesystemServiceClient<Channel>,
    code: CodeServiceClient<Channel>,
    health: HealthServiceClient<Channel>,
    hooks: Router,
    session: Arc<SandboxSession>,
    manager: Arc<CodeManager>,
    log_path: PathBuf,
    root: String,
    _tempdir: tempfile::TempDir,
}

fn fixture(name: &str) -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests")
        .join("fixtures")
        .join(name)
}

fn workspace_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .canonicalize()
        .unwrap()
}

fn running_as_root() -> bool {
    nix::unistd::geteuid().is_root()
}

async fn harness() -> Harness {
    harness_with(Options::default()).await
}

async fn harness_with(options: Options) -> Harness {
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
    let session = Arc::new(SandboxSession::new(Arc::new(SystemClock::new()), "test"));
    let policy = UserPolicy {
        allow_root: running_as_root(),
    };
    let platform = detect_spawn_platform();
    let files = platform_filesystem_manager(
        session.clone(),
        &platform,
        policy,
        DenyList::default(),
        FilesystemSettings {
            write_error_drain_timeout: Duration::from_millis(500),
            ..FilesystemSettings::default()
        },
    );
    let settings = CodeSettings {
        execute_keepalive_interval: KEEPALIVE,
        interrupt_grace: Duration::from_secs(1),
        stall_timeout: options.stall_timeout,
        queue_capacity: 16,
        context_ready_timeout: Duration::from_secs(5),
        sidecar_ready_timeout: Duration::from_secs(60),
        op_timeouts: options.op_timeouts,
        executions: ExecutionLimits {
            retention: options.execution_retention,
            ..ExecutionLimits::default()
        },
        sidecar: Some(config.clone()),
        ..CodeSettings::default()
    };
    let manager = code_manager(&session, &platform, policy, settings, tempdir.path());
    let _supervisor = manager.spawn_supervisor();
    let registry = shared_registry(RegistryLimits::default());
    let ptys = platform_pty_manager(
        session.clone(),
        &platform,
        policy,
        registry.clone(),
        PtySettings {
            stall_timeout: options.stall_timeout,
            ..PtySettings::default()
        },
    );
    let processes = platform_manager(
        session.clone(),
        platform,
        policy,
        registry,
        ManagerSettings {
            stall_timeout: options.stall_timeout,
            ..ManagerSettings::default()
        },
    );
    let suspend = Arc::new(SuspendSignal::new());
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let shutdown = CancellationToken::new();
    let grpc = rayd::grpc::router_with_settings(
        Services {
            session: session.clone(),
            processes,
            ptys,
            files,
            code: manager.clone(),
            metrics: Arc::new(PlatformMetricsProbe::default()),
            metrics_history: Arc::new(rayd_core::metrics_history::MetricsHistory::default()),
            suspend: suspend.clone(),
            imds: Arc::new(rayd::adapters::ImdsState::default()),
            persistence: Arc::new(rayd::persistence::UnavailablePersistence),
            timeout: rayd::lifecycle::TimeoutWatcher::detached(),
            network: rayd::network::NetworkManager::unavailable(session.clone()),
        },
        StreamSettings {
            keepalive_interval: KEEPALIVE,
            watch_keepalive_interval: KEEPALIVE,
            execute_keepalive_interval: KEEPALIVE,
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
        processes: ProcessServiceClient::new(channel.clone()),
        ptys: PtyServiceClient::new(channel.clone()),
        files: FilesystemServiceClient::new(channel.clone()),
        code: CodeServiceClient::new(channel.clone()),
        health: HealthServiceClient::new(channel),
        hooks: rayd::hooks::router(session.clone(), manager.clone(), suspend, shutdown),
        session,
        manager,
        log_path,
        root,
        _tempdir: tempdir,
    };
    if options.run {
        harness.wait_kernel_ready(Duration::from_secs(30)).await;
        harness.post(Hook::Run, Some(run_envelope())).await;
        harness.wait_kernel_ready(Duration::from_secs(30)).await;
    }
    harness
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

fn run_envelope() -> String {
    use std::fmt::Write as _;
    let digest = Sha256::digest(SECRET)
        .iter()
        .fold(String::new(), |mut hex, byte| {
            write!(hex, "{byte:02x}").unwrap();
            hex
        });
    let mut payload = serde_json::json!({
        "v": 1,
        "token_sha256": digest,
        "workdir": "/tmp",
        "envs": {"M5": "1"},
    });
    if running_as_root() {
        payload["user"] = serde_json::Value::String("root".to_owned());
    }
    serde_json::json!({ "microvmId": "mvm-m5", "runHookPayload": payload.to_string() }).to_string()
}

fn authenticated<T>(message: T) -> tonic::Request<T> {
    let mut request = tonic::Request::new(message);
    let encoded = URL_SAFE_NO_PAD.encode(SECRET);
    request
        .metadata_mut()
        .insert("x-access-token", MetadataValue::try_from(encoded).unwrap());
    request
}

fn shell(script: &str, timeout_ms: u64) -> StartRequest {
    StartRequest {
        process: Some(ProcessConfig {
            cmd: "/bin/sh".to_owned(),
            args: vec!["-c".to_owned(), script.to_owned()],
            envs: HashMap::new(),
            cwd: None,
        }),
        user: None,
        timeout_ms,
        stdin: false,
        tag: None,
    }
}

fn execute_request(code: &str, context_id: Option<&str>, timeout_ms: u64) -> ExecuteRequest {
    ExecuteRequest {
        context_id: context_id.map(str::to_owned),
        language: None,
        code: code.to_owned(),
        timeout_ms,
        envs: HashMap::new(),
    }
}

fn temp_files(dir: &str) -> usize {
    fs::read_dir(dir)
        .unwrap()
        .filter_map(Result::ok)
        .filter(|entry| entry.file_name().to_string_lossy().starts_with(TEMP_PREFIX))
        .count()
}

fn kind(event: &ExecuteEvent) -> &'static str {
    match &event.event {
        Some(execute_event::Event::Started(_)) => "started",
        Some(execute_event::Event::Stdout(_)) => "stdout",
        Some(execute_event::Event::Stderr(_)) => "stderr",
        Some(execute_event::Event::Result(_)) => "result",
        Some(execute_event::Event::Error(_)) => "error",
        Some(execute_event::Event::End(_)) => "end",
        Some(execute_event::Event::Keepalive(_)) => "keepalive",
        None => "none",
    }
}

/// A closed process stream: the events before the end, and how it ended.
struct ProcessTail {
    seqs: Vec<u64>,
    end: Option<rayito_proto::v1::EndEvent>,
    status: Option<Status>,
}

async fn drain_process(stream: &mut Streaming<ProcessEvent>) -> ProcessTail {
    let mut tail = ProcessTail {
        seqs: Vec::new(),
        end: None,
        status: None,
    };
    loop {
        match tokio::time::timeout(BUDGET, stream.message())
            .await
            .unwrap()
        {
            Ok(Some(event)) => match event.event {
                Some(process_event::Event::Data(data)) => tail.seqs.push(data.seq),
                Some(process_event::Event::End(end)) => {
                    tail.end = Some(end);
                    assert!(stream.message().await.unwrap().is_none());
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

struct PtyTail {
    bytes: Vec<u8>,
    last_seq: u64,
    exited: Option<rayito_proto::v1::PtyExited>,
}

async fn drain_pty(stream: &mut Streaming<PtyServerMessage>) -> PtyTail {
    let mut tail = PtyTail {
        bytes: Vec::new(),
        last_seq: 0,
        exited: None,
    };
    loop {
        let Some(message) = tokio::time::timeout(BUDGET, stream.message())
            .await
            .unwrap()
            .unwrap()
        else {
            return tail;
        };
        match message.message {
            Some(pty_server_message::Message::Data(bytes)) => {
                tail.last_seq = tail.last_seq.max(message.seq);
                tail.bytes.extend(bytes);
            }
            Some(pty_server_message::Message::Exited(exited)) => {
                tail.exited = Some(exited);
                assert!(stream.message().await.unwrap().is_none());
                return tail;
            }
            _ => {}
        }
    }
}

async fn read_pty_until(stream: &mut Streaming<PtyServerMessage>, needle: &[u8]) -> (Vec<u8>, u64) {
    let deadline = Instant::now() + BUDGET;
    let mut buffer = Vec::new();
    let mut last_seq;
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        let message = tokio::time::timeout(remaining, stream.message())
            .await
            .unwrap_or_else(|_| {
                panic!(
                    "timed out waiting for {:?}; got {:?}",
                    String::from_utf8_lossy(needle),
                    String::from_utf8_lossy(&buffer)
                )
            })
            .unwrap()
            .expect("stream ended before the needle");
        match message.message {
            Some(pty_server_message::Message::Data(bytes)) => {
                last_seq = message.seq;
                buffer.extend(bytes);
                if buffer.windows(needle.len()).any(|window| window == needle) {
                    return (buffer, last_seq);
                }
            }
            Some(pty_server_message::Message::Exited(exited)) => panic!("pty exited: {exited:?}"),
            _ => {}
        }
    }
}

fn say(word: &str) -> String {
    let (head, tail) = word.split_at(1);
    format!("echo {head}''{tail}\n")
}

/// Events of an execute stream until it ends, with the trailing status if
/// any.
struct ExecuteTail {
    kinds: Vec<&'static str>,
    seqs: Vec<u64>,
    error_names: Vec<String>,
    status: Option<Status>,
}

async fn drain_execute(stream: &mut Streaming<ExecuteEvent>) -> ExecuteTail {
    let mut tail = ExecuteTail {
        kinds: Vec::new(),
        seqs: Vec::new(),
        error_names: Vec::new(),
        status: None,
    };
    loop {
        match tokio::time::timeout(BUDGET, stream.message())
            .await
            .unwrap()
        {
            Ok(Some(event)) => {
                let name = kind(&event);
                if name == "keepalive" {
                    continue;
                }
                if let Some(execute_event::Event::Error(error)) = &event.event {
                    tail.error_names.push(error.name.clone());
                }
                tail.kinds.push(name);
                tail.seqs.push(event.seq);
                if name == "end" {
                    return tail;
                }
            }
            Ok(None) => return tail,
            Err(status) => {
                tail.status = Some(status);
                return tail;
            }
        }
    }
}

fn execution_id_of(event: &ExecuteEvent) -> String {
    match &event.event {
        Some(execute_event::Event::Started(started)) => started.execution_id.clone(),
        other => panic!("expected started, got {other:?}"),
    }
}

fn assert_suspending(status: &Status) {
    assert_eq!(status.code(), Code::Unavailable, "{status:?}");
    assert_eq!(status.message(), "suspending");
}

impl Harness {
    async fn post(&self, hook: Hook, body: Option<String>) -> (StatusCode, HookReply) {
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

    async fn health(&self) -> HealthResponse {
        self.health
            .clone()
            .health(HealthRequest {})
            .await
            .unwrap()
            .into_inner()
    }

    async fn wait_kernel_ready(&self, budget: Duration) {
        let deadline = Instant::now() + budget;
        while !self.health().await.kernel_ready {
            assert!(Instant::now() < deadline, "kernel never became ready");
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
    }

    async fn start(&self, request: StartRequest) -> Result<(u32, Streaming<ProcessEvent>), Status> {
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

    async fn connect(&self, pid: u32, from_seq: u64) -> Result<Streaming<ProcessEvent>, Status> {
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

    async fn list(&self) -> Vec<ProcessInfo> {
        self.processes
            .clone()
            .list(authenticated(ListRequest {}))
            .await
            .unwrap()
            .into_inner()
            .processes
    }

    async fn pty(&self) -> Result<(u32, Streaming<PtyServerMessage>), Status> {
        let mut stream = self
            .ptys
            .clone()
            .create(authenticated(PtyStart {
                shell: Some("/bin/bash".to_owned()),
                ..PtyStart::default()
            }))
            .await?
            .into_inner();
        let first = stream.message().await.unwrap().unwrap();
        match first.message {
            Some(pty_server_message::Message::Started(started)) => Ok((started.pid, stream)),
            other => panic!("expected PtyStarted, got {other:?}"),
        }
    }

    async fn pty_connect(
        &self,
        pid: u32,
        from_seq: u64,
    ) -> Result<Streaming<PtyServerMessage>, Status> {
        let mut stream = self
            .ptys
            .clone()
            .connect(authenticated(ConnectRequest { pid, from_seq }))
            .await?
            .into_inner();
        let first = stream.message().await.unwrap().unwrap();
        assert!(matches!(
            first.message,
            Some(pty_server_message::Message::Started(_))
        ));
        Ok(stream)
    }

    async fn pty_send(&self, pid: u32, text: &str) {
        self.ptys
            .clone()
            .send_input(authenticated(SendInputRequest {
                pid,
                data: text.as_bytes().to_vec(),
            }))
            .await
            .unwrap();
    }

    async fn watch(
        &self,
        path: &str,
    ) -> Result<Streaming<rayito_proto::v1::WatchDirResponse>, Status> {
        let mut stream = self
            .files
            .clone()
            .watch_dir(authenticated(WatchDirRequest {
                path: path.to_owned(),
                recursive: false,
                user: None,
                include_entry: false,
            }))
            .await?
            .into_inner();
        let first = stream.message().await.unwrap().unwrap();
        assert!(matches!(
            first.event,
            Some(watch_dir_response::Event::Started(_))
        ));
        Ok(stream)
    }

    async fn read(&self, path: &str) -> Result<Streaming<rayito_proto::v1::ReadResponse>, Status> {
        self.files
            .clone()
            .read(authenticated(ReadRequest {
                path: path.to_owned(),
                user: None,
            }))
            .await
            .map(tonic::Response::into_inner)
    }

    async fn execute(
        &self,
        code: &str,
        context_id: Option<&str>,
        timeout_ms: u64,
    ) -> Result<(String, Streaming<ExecuteEvent>), Status> {
        let mut stream = self
            .code
            .clone()
            .execute(authenticated(execute_request(code, context_id, timeout_ms)))
            .await?
            .into_inner();
        let first = stream.message().await.unwrap().unwrap();
        Ok((execution_id_of(&first), stream))
    }

    async fn reattach(
        &self,
        context_id: &str,
        execution_id: &str,
        from_seq: u64,
    ) -> Result<Streaming<ExecuteEvent>, Status> {
        self.code
            .clone()
            .reattach(authenticated(ReattachRequest {
                context_id: context_id.to_owned(),
                execution_id: execution_id.to_owned(),
                from_seq,
            }))
            .await
            .map(tonic::Response::into_inner)
    }

    async fn create_context(&self) -> Result<String, Status> {
        self.code
            .clone()
            .create_context(authenticated(CreateContextRequest {
                language: String::new(),
                cwd: None,
                envs: HashMap::new(),
            }))
            .await
            .map(|response| response.into_inner().context_id)
    }

    fn requests_of(&self, op: &str) -> Vec<serde_json::Value> {
        fs::read_to_string(&self.log_path)
            .unwrap_or_default()
            .lines()
            .filter(|line| !line.is_empty())
            .map(|line| serde_json::from_str::<serde_json::Value>(line).unwrap())
            .filter(|request| request["op"] == op)
            .collect()
    }

    async fn wait_for_request(&self, op: &str, budget: Duration) -> serde_json::Value {
        let deadline = Instant::now() + budget;
        loop {
            if let Some(request) = self.requests_of(op).into_iter().last() {
                return request;
            }
            assert!(Instant::now() < deadline, "the fake never received {op}");
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    }

    fn path(&self, name: &str) -> String {
        format!("{}/{name}", self.root)
    }

    /// A `Write` whose body stays open: the first message begins the file.
    async fn open_write(
        &self,
        path: &str,
    ) -> (
        mpsc::Sender<WriteRequest>,
        JoinHandle<Result<tonic::Response<rayito_proto::v1::WriteResponse>, Status>>,
    ) {
        let mut client = self.files.clone();
        let (sender, receiver) = mpsc::channel::<WriteRequest>(4);
        let call = tokio::spawn(async move {
            client
                .write(authenticated(ReceiverStream::new(receiver)))
                .await
        });
        sender
            .send(WriteRequest {
                path: Some(path.to_owned()),
                user: None,
                mode: None,
                chunk: vec![7; 1024],
                metadata: HashMap::new(),
            })
            .await
            .unwrap();
        let deadline = Instant::now() + Duration::from_secs(2);
        while temp_files(&self.root) == 0 {
            assert!(Instant::now() < deadline, "temp file never appeared");
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        (sender, call)
    }
}

/// Reads a `Read` stream slowly in the background, so flow control keeps
/// the server polling it while the client is "half way through".
fn slow_reader(
    mut stream: Streaming<rayito_proto::v1::ReadResponse>,
) -> JoinHandle<(usize, Option<Status>)> {
    tokio::spawn(async move {
        let mut chunks = 0usize;
        loop {
            match stream.message().await {
                Ok(Some(_)) => {
                    chunks += 1;
                    tokio::time::sleep(Duration::from_millis(100)).await;
                }
                Ok(None) => return (chunks, None),
                Err(status) => return (chunks, Some(status)),
            }
        }
    })
}

#[tokio::test]
async fn suspend_closes_every_stream_kills_nothing_and_resume_replays() {
    let harness = harness().await;
    fs::write(harness.path("big.bin"), vec![1u8; 4 * 1024 * 1024]).unwrap();
    let (sleeper, mut start_stream) = harness.start(shell("sleep 30", 0)).await.unwrap();
    let mut connect_stream = harness.connect(sleeper, 0).await.unwrap();
    let (pty, mut pty_stream) = harness.pty().await.unwrap();
    harness.pty_send(pty, &say("ready")).await;
    let (_, pty_seq) = read_pty_until(&mut pty_stream, b"ready\r\n").await;
    let mut watch_stream = harness.watch(&harness.root).await.unwrap();
    let read_stream = harness.read(&harness.path("big.bin")).await.unwrap();
    let reader = slow_reader(read_stream);
    let (execution_id, mut execute_stream) = harness.execute("sleep 3", None, 0).await.unwrap();
    let mut reattach_stream = harness.reattach("", &execution_id, 1).await.unwrap();
    let write_path = harness.path("upload.bin");
    let (write_sender, write_call) = harness.open_write(&write_path).await;
    tokio::time::sleep(Duration::from_millis(150)).await;

    let started = Instant::now();
    let (status, reply) = harness.post(Hook::Suspend, None).await;
    let suspend_elapsed = started.elapsed();
    assert_eq!(status, StatusCode::OK);
    assert_eq!(reply.outcome, "changed");
    assert_eq!(reply.phase, "suspending");
    assert_eq!(reply.suspend_generation, 1);
    assert_eq!(reply.streams_closed, Some(8), "{reply:?}");
    assert!(
        suspend_elapsed < Duration::from_secs(2),
        "{suspend_elapsed:?}"
    );
    println!("suspend hook in-process: {suspend_elapsed:?}");

    for stream in [&mut start_stream, &mut connect_stream] {
        let tail = drain_process(stream).await;
        let end = tail.end.expect("EndEvent");
        assert_eq!(end.status, "suspending");
        assert!(!end.exited);
        assert_eq!(end.exit_code, 0);
        assert_eq!(end.error.unwrap().code, "suspending");
        assert!(tail.status.is_none());
    }
    let pty_tail = drain_pty(&mut pty_stream).await;
    let exited = pty_tail.exited.expect("PtyExited");
    assert_eq!(exited.status, "suspending");
    assert!(!exited.exited);
    assert_eq!(exited.error.unwrap().code, "suspending");
    let watch_end = loop {
        match watch_stream.message().await {
            Ok(Some(_)) => {}
            Ok(None) => panic!("watch ended without a status"),
            Err(status) => break status,
        }
    };
    assert_suspending(&watch_end);
    let (chunks, read_end) = reader.await.unwrap();
    assert!((1..16).contains(&chunks), "{chunks}");
    assert_suspending(&read_end.expect("read closed with a status"));
    let execute_tail = drain_execute(&mut execute_stream).await;
    assert!(
        !execute_tail.kinds.contains(&"end"),
        "{:?}",
        execute_tail.kinds
    );
    assert_suspending(&execute_tail.status.expect("execute closed with a status"));
    let reattach_tail = drain_execute(&mut reattach_stream).await;
    assert!(!reattach_tail.kinds.contains(&"end"));
    assert_suspending(&reattach_tail.status.expect("reattach closed with a status"));
    let write_result = write_call.await.unwrap();
    assert_suspending(&write_result.unwrap_err());
    drop(write_sender);
    assert_eq!(temp_files(&harness.root), 0, "the write temporary is gone");
    assert!(
        !fs::exists(&write_path).unwrap(),
        "the destination is untouched"
    );

    assert!(
        !harness.requests_of("quiesce").is_empty(),
        "the sidecar was quiesced"
    );
    assert!(
        harness.requests_of("interrupt").is_empty(),
        "nothing was interrupted"
    );
    let live: Vec<u32> = harness
        .list()
        .await
        .into_iter()
        .map(|info| info.pid)
        .collect();
    assert!(live.contains(&sleeper), "sleep 30 survived: {live:?}");
    assert!(live.contains(&pty), "the shell survived: {live:?}");

    assert_suspending(&harness.start(shell("true", 0)).await.err().unwrap());
    assert_suspending(&harness.pty().await.err().unwrap());
    assert_suspending(&harness.watch(&harness.root).await.err().unwrap());
    assert_suspending(&harness.read(&harness.path("big.bin")).await.err().unwrap());
    assert_suspending(&harness.execute("1", None, 0).await.err().unwrap());
    assert_suspending(&harness.reattach("", &execution_id, 1).await.err().unwrap());
    assert_suspending(&harness.connect(sleeper, 0).await.err().unwrap());
    let (_, again) = harness.post(Hook::Suspend, None).await;
    assert_eq!(again.outcome, "unchanged");
    assert_eq!(again.streams_closed, Some(0));
    assert_eq!(again.suspend_generation, 1);

    let (status, resumed) = harness.post(Hook::Resume, None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(resumed.outcome, "changed");
    assert_eq!(resumed.phase, "resumed");
    assert_eq!(resumed.resume_generation, 1);
    assert_eq!(resumed.kernel_state_lost, Some(false));
    harness
        .wait_for_request("reseed", Duration::from_secs(2))
        .await;
    assert_eq!(harness.requests_of("resume").len(), 1);
    let health = harness.health().await;
    assert_eq!(health.resume_generation, 1);
    assert!(health.agent_ready);
    assert!(!health.kernel_state_lost);
    assert!(
        health.clock_offset_ms.abs() <= 100,
        "{}",
        health.clock_offset_ms
    );

    let mut after = harness.connect(sleeper, 1).await.unwrap();
    assert!(harness.list().await.iter().any(|info| info.pid == sleeper));
    let mut pty_after = harness.pty_connect(pty, pty_seq + 1).await.unwrap();
    harness.pty_send(pty, &say("back")).await;
    let (bytes, _) = read_pty_until(&mut pty_after, b"back\r\n").await;
    assert!(
        !bytes.windows(5).any(|window| window == b"ready"),
        "no replay of old bytes"
    );
    let mut full_replay = harness.pty_connect(pty, 1).await.unwrap();
    let (replayed, _) = read_pty_until(&mut full_replay, b"back\r\n").await;
    assert!(
        replayed.windows(5).any(|window| window == b"ready"),
        "the ring replays"
    );
    let mut reattached = harness
        .reattach(
            "",
            &execution_id,
            execute_tail.seqs.last().copied().unwrap_or(0) + 1,
        )
        .await
        .unwrap();
    let tail = drain_execute(&mut reattached).await;
    assert_eq!(tail.kinds.last().copied(), Some("end"), "{:?}", tail.kinds);
    assert!(tail.status.is_none());
    assert!(tail.error_names.is_empty(), "{:?}", tail.error_names);
    harness
        .processes
        .clone()
        .send_signal(authenticated(rayito_proto::v1::SendSignalRequest {
            pid: sleeper,
            signal: 9,
        }))
        .await
        .unwrap();
    let end = drain_process(&mut after).await.end.unwrap();
    assert_eq!(end.exit_code, 137);
    harness
        .ptys
        .clone()
        .kill(authenticated(rayito_proto::v1::KillPtyRequest { pid: pty }))
        .await
        .unwrap();
    drain_pty(&mut pty_after).await;
}

#[tokio::test]
async fn reattach_beyond_retention_is_not_found_and_a_stalled_execute_is_truncated_alone() {
    let harness = harness_with(Options {
        execution_retention: Duration::from_millis(500),
        ..Options::default()
    })
    .await;
    let (execution_id, mut stream) = harness.execute("x = 42", None, 0).await.unwrap();
    let tail = drain_execute(&mut stream).await;
    assert_eq!(tail.kinds, vec!["end"]);
    let mut replay = harness.reattach("", &execution_id, 1).await.unwrap();
    let replayed = drain_execute(&mut replay).await;
    assert_eq!(replayed.kinds, vec!["started", "end"]);
    tokio::time::sleep(Duration::from_millis(700)).await;
    harness.manager.reap_expired();
    assert_eq!(
        harness
            .reattach("", &execution_id, 1)
            .await
            .err()
            .unwrap()
            .code(),
        Code::NotFound
    );

    let (execution_id, mut stalled) = harness.execute("big 400", None, 0).await.unwrap();
    let mut follower = harness.reattach("", &execution_id, 1).await.unwrap();
    let follower_tail = drain_execute(&mut follower).await;
    assert_eq!(follower_tail.kinds.last().copied(), Some("end"));
    assert_eq!(
        follower_tail
            .kinds
            .iter()
            .filter(|k| **k == "stdout")
            .count(),
        400,
        "the reattach subscriber got everything"
    );
    let stalled_tail = drain_execute(&mut stalled).await;
    assert!(
        stalled_tail
            .error_names
            .contains(&"OutputTruncated".to_owned())
    );
    assert!(
        stalled_tail
            .kinds
            .iter()
            .filter(|k| **k == "stdout")
            .count()
            < 400
    );
    assert_eq!(harness.manager.sidecar_restarts(), 0);
}

#[tokio::test]
async fn timeouts_are_re_armed_on_the_running_clock() {
    let harness = harness().await;
    let (pid, _start_stream) = harness.start(shell("sleep 10", 1_500)).await.unwrap();
    let (execution_id, _execute_stream) = harness.execute("sleep 5", None, 1_500).await.unwrap();
    tokio::time::sleep(Duration::from_millis(500)).await;
    harness.post(Hook::Suspend, None).await;
    tokio::time::sleep(Duration::from_secs(2)).await;
    assert!(harness.requests_of("interrupt").is_empty());
    let resumed_at = Instant::now();
    harness.post(Hook::Resume, None).await;
    assert!(
        harness.session.suspended_total() >= Duration::from_secs(2),
        "{:?}",
        harness.session.suspended_total()
    );
    assert!(
        harness.list().await.iter().any(|info| info.pid == pid),
        "alive at resume"
    );
    tokio::time::sleep(Duration::from_millis(500)).await;
    assert!(
        harness.list().await.iter().any(|info| info.pid == pid),
        "still alive 0.5 s later"
    );
    assert!(
        harness.requests_of("interrupt").is_empty(),
        "no interrupt at resume"
    );
    let mut after = harness.connect(pid, 0).await.unwrap();
    let end = drain_process(&mut after).await.end.unwrap();
    let elapsed = resumed_at.elapsed();
    assert_eq!(end.status, "timeout");
    assert!(
        (Duration::from_millis(800)..Duration::from_millis(2_500)).contains(&elapsed),
        "{elapsed:?}"
    );
    let interrupt = harness
        .wait_for_request("interrupt", Duration::from_secs(3))
        .await;
    assert_eq!(interrupt["execution_id"], execution_id);
    let mut reattached = harness.reattach("", &execution_id, 1).await.unwrap();
    let tail = drain_execute(&mut reattached).await;
    assert!(
        tail.error_names.contains(&"ExecutionTimeout".to_owned()),
        "{:?}",
        tail.error_names
    );
}

#[tokio::test]
async fn a_lost_kernel_is_restarted_and_kernel_state_lost_clears_on_the_next_probe() {
    let harness = harness_with(Options {
        fake_flags: vec!["--resume-lost".to_owned(), "created".to_owned()],
        ..Options::default()
    })
    .await;
    let ctx = harness.create_context().await.unwrap();
    let (execution_id, _stream) = harness.execute("sleep 5", Some(&ctx), 0).await.unwrap();
    harness.post(Hook::Suspend, None).await;
    let (_, resumed) = harness.post(Hook::Resume, None).await;
    assert_eq!(resumed.kernel_state_lost, Some(true));
    assert!(harness.health().await.kernel_state_lost);
    let deadline = Instant::now() + Duration::from_secs(3);
    while !harness
        .requests_of("restart_context")
        .iter()
        .any(|request| request["context_id"] == ctx)
    {
        assert!(Instant::now() < deadline, "no restart_context for {ctx}");
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    tokio::time::sleep(Duration::from_millis(200)).await;
    let mut reattached = harness.reattach(&ctx, &execution_id, 1).await.unwrap();
    let tail = drain_execute(&mut reattached).await;
    assert!(
        tail.error_names.contains(&"KernelRestarted".to_owned()),
        "{:?}",
        tail.error_names
    );
    assert_eq!(tail.kinds.last().copied(), Some("end"));
    harness.post(Hook::Suspend, None).await;
    let (_, again) = harness.post(Hook::Resume, None).await;
    assert_eq!(again.kernel_state_lost, Some(false));
    assert!(!harness.health().await.kernel_state_lost);
    assert_eq!(harness.health().await.resume_generation, 2);
}

#[tokio::test]
async fn a_slow_probe_still_answers_200_within_the_cap_and_reports_state_lost() {
    let harness = harness_with(Options {
        fake_flags: vec!["--resume-delay-ms".to_owned(), "13000".to_owned()],
        ..Options::default()
    })
    .await;
    harness.post(Hook::Suspend, None).await;
    let started = Instant::now();
    let (status, resumed) = harness.post(Hook::Resume, None).await;
    let elapsed = started.elapsed();
    assert_eq!(status, StatusCode::OK);
    assert_eq!(resumed.outcome, "changed");
    assert_eq!(resumed.kernel_state_lost, Some(true));
    assert!(elapsed < Duration::from_secs(13), "{elapsed:?}");
    assert!(elapsed >= Duration::from_secs(11), "{elapsed:?}");
    assert!(harness.health().await.kernel_state_lost);
}

#[tokio::test]
async fn op_timeouts_across_a_resume_are_not_counted() {
    let harness = harness_with(Options {
        op_timeouts: OpTimeouts {
            create_context: Duration::from_millis(300),
            interrupt: Duration::from_secs(2),
            ..OpTimeouts::default()
        },
        fake_flags: vec!["--create-delay-ms".to_owned(), "1000".to_owned()],
        ..Options::default()
    })
    .await;
    for _ in 0..3 {
        let create = {
            let harness_code = harness.code.clone();
            tokio::spawn(async move {
                harness_code
                    .clone()
                    .create_context(authenticated(CreateContextRequest {
                        language: String::new(),
                        cwd: None,
                        envs: HashMap::new(),
                    }))
                    .await
            })
        };
        tokio::time::sleep(Duration::from_millis(50)).await;
        harness.post(Hook::Suspend, None).await;
        harness.post(Hook::Resume, None).await;
        let outcome = create.await.unwrap();
        assert_eq!(outcome.unwrap_err().code(), Code::Unavailable);
    }
    assert_eq!(harness.manager.sidecar_restarts(), 0);
    for _ in 0..2 {
        assert_eq!(
            harness.create_context().await.unwrap_err().code(),
            Code::Unavailable
        );
        tokio::time::sleep(Duration::from_millis(1_200)).await;
    }
    assert_eq!(
        harness.manager.sidecar_restarts(),
        0,
        "two plain timeouts stay below the kill switch"
    );
    assert!(harness.health().await.kernel_ready);
}

#[tokio::test]
async fn suspend_before_run_is_illegal_and_harmless() {
    let harness = harness_with(Options {
        run: false,
        ..Options::default()
    })
    .await;
    let (status, reply) = harness.post(Hook::Suspend, None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(reply.outcome, "illegal");
    assert_eq!(reply.phase, "booting");
    assert_eq!(reply.streams_closed, Some(0));
    assert!(harness.requests_of("quiesce").is_empty());
}
