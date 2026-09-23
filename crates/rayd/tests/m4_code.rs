//! `CodeService`, `Health.kernel_ready` and the kernel halves of the hooks
//! end to end, in process, against the stdlib-only fake sidecar of
//! `tests/fixtures/fake_sidecar.py` (design D13): the in-process router on
//! `127.0.0.1:0`, `/run` installed through the hooks router, keepalive,
//! stall and grace intervals shrunk through the settings, every request
//! the fake receives asserted through its `--log` file. One `#[ignore]`
//! case runs the real `kernel-sidecar` when `RAYITO_SIDECAR_ROOT` is set.
//!
//! Integration tests are test code, but clippy's `allow-unwrap-in-tests` only
//! recognises `#[test]` functions and `#[cfg(test)]` modules.
#![cfg(unix)]
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::collections::HashMap;
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
use rayito_proto::v1::health_service_client::HealthServiceClient;
use rayito_proto::v1::{
    CreateContextRequest, DestroyContextRequest, ExecuteEvent, ExecuteRequest, HealthRequest,
    ListContextsRequest, ReattachRequest, RestartContextRequest, execute_event,
};
use sha2::{Digest, Sha256};
use tokio::net::TcpListener;
use tokio_util::sync::CancellationToken;
use tonic::metadata::MetadataValue;
use tonic::transport::Channel;
use tonic::transport::server::TcpIncoming;
use tonic::{Code, Status, Streaming};
use tower::ServiceExt;

const SECRET: &[u8] = b"m4-sandbox-secret";
const KEEPALIVE: Duration = Duration::from_millis(200);
const STALL: Duration = Duration::from_secs(1);
const GRACE: Duration = Duration::from_secs(1);

struct Options {
    warmup_ms: u64,
    restart_ms: u64,
    noisy_stderr: bool,
    sidecar: bool,
    run: bool,
    stall_timeout: Duration,
    op_timeouts: OpTimeouts,
}

impl Default for Options {
    fn default() -> Self {
        Self {
            warmup_ms: 0,
            restart_ms: 0,
            noisy_stderr: false,
            sidecar: true,
            run: true,
            stall_timeout: STALL,
            op_timeouts: OpTimeouts {
                interrupt: Duration::from_secs(2),
                ..OpTimeouts::default()
            },
        }
    }
}

struct Harness {
    code: CodeServiceClient<Channel>,
    health: HealthServiceClient<Channel>,
    hooks: Router,
    manager: Arc<CodeManager>,
    killed: Arc<Mutex<Vec<u32>>>,
    log_path: PathBuf,
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

async fn harness() -> Harness {
    harness_with(Options::default()).await
}

async fn harness_with(options: Options) -> Harness {
    let tempdir = tempfile::tempdir().unwrap();
    let log_path = tempdir.path().join("requests.jsonl");
    let mut command = vec![
        "python3".to_owned(),
        fixture("fake_sidecar.py").to_string_lossy().into_owned(),
        "--warmup-ms".to_owned(),
        options.warmup_ms.to_string(),
        "--restart-ms".to_owned(),
        options.restart_ms.to_string(),
        "--log".to_owned(),
        log_path.to_string_lossy().into_owned(),
    ];
    if options.noisy_stderr {
        command.push("--noisy-stderr".to_owned());
    }
    let config = SidecarConfig::new(
        command,
        &workspace_root().join("kernel-sidecar").to_string_lossy(),
        &tempdir.path().join("k").to_string_lossy(),
    );
    build_harness(options, config, tempdir, log_path).await
}

async fn build_harness(
    options: Options,
    config: SidecarConfig,
    tempdir: tempfile::TempDir,
    log_path: PathBuf,
) -> Harness {
    let session = Arc::new(SandboxSession::new(Arc::new(SystemClock::new()), "test"));
    let policy = UserPolicy {
        allow_root: nix::unistd::geteuid().is_root(),
    };
    let platform = detect_spawn_platform();
    let files = platform_filesystem_manager(
        session.clone(),
        &platform,
        policy,
        DenyList::default(),
        FilesystemSettings::default(),
    );
    let killed = Arc::new(Mutex::new(Vec::new()));
    let manager = if options.sidecar {
        code_manager(
            &session,
            &platform,
            policy,
            code_settings(&options, &config),
            tempdir.path(),
            &killed,
        )
    } else {
        CodeManager::disabled(session.clone(), Arc::new(OsRandomSource))
    };
    let _supervisor = manager.spawn_supervisor();
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
            execute_keepalive_interval: KEEPALIVE,
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
        code: CodeServiceClient::new(channel.clone()),
        health: HealthServiceClient::new(channel),
        hooks: rayd::hooks::router(session, manager.clone(), suspend, shutdown),
        manager,
        killed,
        log_path,
        _tempdir: tempdir,
    };
    if options.run {
        harness.wait_kernel_ready(Duration::from_secs(30)).await;
        harness.post(Hook::Run, Some(run_envelope())).await;
        harness.wait_kernel_ready(Duration::from_secs(30)).await;
    }
    harness
}

/// Shrunk intervals for the fake.
fn code_settings(options: &Options, config: &SidecarConfig) -> CodeSettings {
    CodeSettings {
        execute_keepalive_interval: KEEPALIVE,
        interrupt_grace: GRACE,
        stall_timeout: options.stall_timeout,
        queue_capacity: 16,
        context_ready_timeout: Duration::from_secs(5),
        sidecar_ready_timeout: Duration::from_secs(60),
        op_timeouts: options.op_timeouts,
        executions: ExecutionLimits::default(),
        sidecar: Some(config.clone()),
        ..CodeSettings::default()
    }
}

/// A launcher for this host and a kernel killer that only records pids:
/// the fake's pids are made up and must never be signalled.
fn code_manager(
    session: &Arc<SandboxSession>,
    platform: &rayd::adapters::SpawnPlatform,
    policy: UserPolicy,
    settings: CodeSettings,
    cwd: &Path,
    killed: &Arc<Mutex<Vec<u32>>>,
) -> Arc<CodeManager> {
    let config = settings.sidecar.clone().unwrap();
    let identity = sidecar_identity(session, platform.lookup.as_ref(), policy).unwrap();
    let mut spec = sidecar_spawn_spec(&identity, &config);
    spec.cwd = cwd.to_string_lossy().into_owned();
    let launcher: Arc<dyn KernelSidecar> =
        Arc::new(TokioSidecarLauncher::new(platform.identity_switch));
    let registry = Arc::new(Mutex::new(ContextRegistry::default()));
    let recorder = killed.clone();
    let kernel_killer: KernelSignaller =
        Arc::new(move |pid, _signal| recorder.lock().unwrap().push(pid));
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
        "envs": {"M4": "1"},
    });
    if nix::unistd::geteuid().is_root() {
        payload["user"] = serde_json::Value::String("root".to_owned());
    }
    serde_json::json!({ "microvmId": "mvm-m4", "runHookPayload": payload.to_string() }).to_string()
}

fn authenticated<T>(message: T) -> tonic::Request<T> {
    let mut request = tonic::Request::new(message);
    let encoded = URL_SAFE_NO_PAD.encode(SECRET);
    request
        .metadata_mut()
        .insert("x-access-token", MetadataValue::try_from(encoded).unwrap());
    request
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

#[derive(Debug, Default)]
struct Collected {
    events: Vec<ExecuteEvent>,
    keepalives: usize,
}

impl Collected {
    fn kinds(&self) -> Vec<&'static str> {
        self.events.iter().map(kind).collect()
    }

    fn seqs(&self) -> Vec<u64> {
        self.events.iter().map(|event| event.seq).collect()
    }

    fn error_name(&self) -> Option<String> {
        self.events.iter().find_map(|event| match &event.event {
            Some(execute_event::Event::Error(error)) => Some(error.name.clone()),
            _ => None,
        })
    }

    fn main_text(&self) -> Option<String> {
        self.events.iter().find_map(|event| match &event.event {
            Some(execute_event::Event::Result(result)) if result.is_main_result => {
                result.text.clone()
            }
            _ => None,
        })
    }

    fn end_count(&self) -> Option<u64> {
        self.events.iter().find_map(|event| match &event.event {
            Some(execute_event::Event::End(end)) => Some(end.execution_count),
            _ => None,
        })
    }

    fn stdout(&self) -> String {
        self.events
            .iter()
            .filter_map(|event| match &event.event {
                Some(execute_event::Event::Stdout(chunk)) => Some(chunk.text.as_str()),
                _ => None,
            })
            .collect()
    }
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

async fn collect(mut stream: Streaming<ExecuteEvent>) -> Collected {
    let mut collected = Collected::default();
    while let Some(event) = stream.message().await.unwrap() {
        if matches!(event.event, Some(execute_event::Event::Keepalive(_))) {
            assert_eq!(event.seq, 0);
            collected.keepalives += 1;
            continue;
        }
        let done = matches!(event.event, Some(execute_event::Event::End(_)));
        collected.events.push(event);
        if done {
            break;
        }
    }
    collected
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

    async fn kernel_ready(&self) -> bool {
        self.health
            .clone()
            .health(HealthRequest {})
            .await
            .unwrap()
            .into_inner()
            .kernel_ready
    }

    async fn wait_kernel_ready(&self, budget: Duration) {
        let deadline = Instant::now() + budget;
        while !self.kernel_ready().await {
            assert!(Instant::now() < deadline, "kernel never became ready");
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
    }

    async fn wait_kernel_not_ready(&self, budget: Duration) {
        let deadline = Instant::now() + budget;
        while self.kernel_ready().await {
            assert!(Instant::now() < deadline, "kernel stayed ready");
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    }

    async fn open(
        &self,
        code: &str,
        context_id: Option<&str>,
        timeout_ms: u64,
    ) -> Result<Streaming<ExecuteEvent>, Status> {
        self.code
            .clone()
            .execute(authenticated(execute_request(code, context_id, timeout_ms)))
            .await
            .map(tonic::Response::into_inner)
    }

    async fn run(&self, code: &str, context_id: Option<&str>, timeout_ms: u64) -> Collected {
        collect(self.open(code, context_id, timeout_ms).await.unwrap()).await
    }

    async fn create_context(&self, cwd: Option<&str>) -> Result<String, Status> {
        self.code
            .clone()
            .create_context(authenticated(CreateContextRequest {
                language: String::new(),
                cwd: cwd.map(str::to_owned),
                envs: HashMap::new(),
            }))
            .await
            .map(|response| response.into_inner().context_id)
    }

    async fn list_contexts(&self) -> Vec<String> {
        self.code
            .clone()
            .list_contexts(authenticated(ListContextsRequest {}))
            .await
            .unwrap()
            .into_inner()
            .contexts
            .into_iter()
            .map(|context| context.context_id)
            .collect()
    }

    async fn destroy_context(&self, context_id: &str) -> Result<(), Status> {
        self.code
            .clone()
            .destroy_context(authenticated(DestroyContextRequest {
                context_id: context_id.to_owned(),
            }))
            .await
            .map(|_| ())
    }

    async fn restart_context(&self, context_id: &str) -> Result<(), Status> {
        self.code
            .clone()
            .restart_context(authenticated(RestartContextRequest {
                context_id: context_id.to_owned(),
            }))
            .await
            .map(|_| ())
    }

    fn requests(&self) -> Vec<serde_json::Value> {
        std::fs::read_to_string(&self.log_path)
            .unwrap_or_default()
            .lines()
            .filter(|line| !line.is_empty())
            .map(|line| serde_json::from_str(line).unwrap())
            .collect()
    }

    fn requests_of(&self, op: &str) -> Vec<serde_json::Value> {
        self.requests()
            .into_iter()
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
}

#[tokio::test]
async fn ready_answers_503_while_warming_then_200_and_kernel_ready_follows() {
    let harness = harness_with(Options {
        warmup_ms: 1_000,
        run: false,
        ..Options::default()
    })
    .await;
    let (status, reply) = harness.post(Hook::Ready, None).await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(reply.outcome, "kernel_warming");
    assert_eq!(reply.phase, "booting");
    assert!(!harness.kernel_ready().await);
    harness.wait_kernel_ready(Duration::from_secs(10)).await;
    let (status, reply) = harness.post(Hook::Ready, None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(reply.outcome, "changed");
    assert_eq!(reply.phase, "ready");
}

#[tokio::test]
async fn validate_answers_503_until_the_cell_finished() {
    let harness = harness_with(Options {
        run: false,
        ..Options::default()
    })
    .await;
    harness.wait_kernel_ready(Duration::from_secs(10)).await;
    harness.post(Hook::Ready, None).await;
    let (status, reply) = harness.post(Hook::Validate, None).await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(reply.outcome, "validating");
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        let (status, reply) = harness.post(Hook::Validate, None).await;
        if status == StatusCode::OK {
            assert_eq!(reply.outcome, "validated");
            break;
        }
        assert_eq!(reply.outcome, "validating");
        assert!(Instant::now() < deadline);
        tokio::time::sleep(Duration::from_millis(200)).await;
    }
    let execute = harness
        .wait_for_request("execute", Duration::from_secs(2))
        .await;
    assert!(execute["code"].as_str().unwrap().contains("import pandas"));
    assert_eq!(harness.requests_of("execute").len(), 1);
    let restart = harness.requests_of("restart_context");
    assert_eq!(
        restart.len(),
        1,
        "validate restarts the default kernel like /run"
    );
    assert_eq!(restart[0]["context_id"], "default");
}

#[tokio::test]
async fn run_rotates_the_default_kernel_with_the_payload_envs() {
    let harness = harness_with(Options {
        run: false,
        ..Options::default()
    })
    .await;
    harness.wait_kernel_ready(Duration::from_secs(10)).await;
    harness.post(Hook::Ready, None).await;
    let (status, reply) = harness.post(Hook::Run, Some(run_envelope())).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(reply.outcome, "installed");
    let restart = harness
        .wait_for_request("restart_context", Duration::from_secs(5))
        .await;
    assert_eq!(restart["context_id"], "default");
    assert_eq!(restart["envs"], serde_json::json!({"M4": "1"}));
    harness.wait_kernel_ready(Duration::from_secs(10)).await;
    assert_eq!(harness.requests_of("restart_context").len(), 1);
    let (_, again) = harness.post(Hook::Run, Some(run_envelope())).await;
    assert_eq!(again.outcome, "already_ran");
    tokio::time::sleep(Duration::from_millis(300)).await;
    assert_eq!(harness.requests_of("restart_context").len(), 1);
}

#[tokio::test]
async fn kernel_ready_dips_during_the_rotation() {
    let harness = harness_with(Options {
        restart_ms: 600,
        run: false,
        ..Options::default()
    })
    .await;
    harness.wait_kernel_ready(Duration::from_secs(10)).await;
    harness.post(Hook::Ready, None).await;
    harness.post(Hook::Run, Some(run_envelope())).await;
    harness.wait_kernel_not_ready(Duration::from_secs(1)).await;
    assert!(matches!(
        harness.manager.sidecar_state(),
        rayd_core::code::SidecarState::Rotating
    ));
    harness.wait_kernel_ready(Duration::from_secs(5)).await;
    assert_eq!(harness.requests_of("restart_context").len(), 1);
}

#[tokio::test]
async fn execute_event_order_and_sequence_numbers() {
    let harness = harness().await;
    let first = harness.run("x = 42", None, 0).await;
    assert_eq!(first.kinds(), vec!["started", "end"]);
    assert_eq!(first.seqs(), vec![1, 2]);
    assert_eq!(first.end_count(), Some(1));
    let second = harness.run("x", Some("default"), 0).await;
    assert_eq!(second.kinds(), vec!["started", "result", "end"]);
    assert_eq!(second.seqs(), vec![1, 2, 3]);
    assert_eq!(second.main_text().as_deref(), Some("42"));
    assert_eq!(second.end_count(), Some(2));
    let third = harness.run("print(x)", Some(""), 0).await;
    assert_eq!(third.kinds(), vec!["started", "stdout", "end"]);
    assert_eq!(third.stdout(), "42\n");
    let started = match &third.events[0].event {
        Some(execute_event::Event::Started(started)) => started.clone(),
        other => panic!("unexpected {other:?}"),
    };
    assert!(started.execution_id.starts_with("exec-"));
    assert_eq!(started.execution_id.len(), 21);
    let error = harness.run("1/0", None, 0).await;
    assert_eq!(error.kinds(), vec!["started", "error", "end"]);
    assert_eq!(error.error_name().as_deref(), Some("ZeroDivisionError"));
    let plot = harness.run("plot", None, 0).await;
    match &plot.events[1].event {
        Some(execute_event::Event::Result(result)) => {
            assert!(!result.is_main_result);
            assert!(result.png.is_some());
            assert!(result.chart.is_some());
            assert_eq!(result.text, None);
        }
        other => panic!("unexpected {other:?}"),
    }
    let frame = harness.run("df", None, 0).await;
    match &frame.events[1].event {
        Some(execute_event::Event::Result(result)) => {
            assert!(result.is_main_result);
            assert!(result.html.is_some());
            assert_eq!(result.data.as_deref(), Some("{\"a\": [1, 2]}"));
        }
        other => panic!("unexpected {other:?}"),
    }
    let omitted = harness.run("omit", None, 0).await;
    match &omitted.events[1].event {
        Some(execute_event::Event::Result(result)) => {
            assert_eq!(
                result.extra.get("rayito/omitted").map(String::as_str),
                Some("image/png: 9000000 bytes")
            );
        }
        other => panic!("unexpected {other:?}"),
    }
}

#[tokio::test]
async fn execute_rejections_before_the_first_message() {
    let harness = harness().await;
    assert_eq!(
        harness
            .open("x", Some("ctx-000000000000"), 0)
            .await
            .unwrap_err()
            .code(),
        Code::NotFound
    );
    assert_eq!(
        harness
            .open("x", Some("bad id!"), 0)
            .await
            .unwrap_err()
            .code(),
        Code::InvalidArgument
    );
    let huge = "x".repeat(1024 * 1024 + 1);
    assert_eq!(
        harness.open(&huge, None, 0).await.unwrap_err().code(),
        Code::InvalidArgument
    );
    let reattach = harness
        .code
        .clone()
        .reattach(authenticated(ReattachRequest {
            context_id: String::new(),
            execution_id: "exec-0123456789abcdef".to_owned(),
            from_seq: 0,
        }))
        .await
        .expect_err("unknown execution");
    assert_eq!(reattach.code(), Code::NotFound);
    let malformed = harness
        .code
        .clone()
        .reattach(authenticated(ReattachRequest::default()))
        .await
        .expect_err("malformed execution id");
    assert_eq!(malformed.code(), Code::InvalidArgument);
    let unauthenticated = harness
        .code
        .clone()
        .execute(execute_request("x", None, 0))
        .await
        .expect_err("no token");
    assert_eq!(unauthenticated.code(), Code::Unauthenticated);
}

#[tokio::test]
async fn timeout_interrupts_an_obedient_cell_and_keeps_state() {
    let harness = harness().await;
    harness.run("x = 42", None, 0).await;
    let started = Instant::now();
    let timed_out = harness.run("sleep 5", None, 500).await;
    assert!(
        started.elapsed() < Duration::from_secs(2),
        "{:?}",
        started.elapsed()
    );
    assert_eq!(timed_out.kinds(), vec!["started", "error", "end"]);
    assert_eq!(timed_out.error_name().as_deref(), Some("ExecutionTimeout"));
    match &timed_out.events[1].event {
        Some(execute_event::Event::Error(error)) => {
            assert_eq!(error.value, "execution exceeded 500 ms");
            assert!(error.traceback.is_empty());
        }
        other => panic!("unexpected {other:?}"),
    }
    let interrupt = harness
        .wait_for_request("interrupt", Duration::from_secs(2))
        .await;
    assert_eq!(interrupt["context_id"], "default");
    assert!(
        interrupt["execution_id"]
            .as_str()
            .unwrap()
            .starts_with("exec-")
    );
    assert!(harness.requests_of("restart_context").len() <= 1);
    assert_eq!(
        harness.run("x", None, 0).await.main_text().as_deref(),
        Some("42")
    );
}

#[tokio::test]
async fn timeout_restarts_a_hung_cell() {
    let harness = harness().await;
    let before = harness.requests_of("restart_context").len();
    let started = Instant::now();
    let timed_out = harness.run("hang 5", None, 500).await;
    assert!(
        started.elapsed() < Duration::from_secs(4),
        "{:?}",
        started.elapsed()
    );
    assert_eq!(timed_out.error_name().as_deref(), Some("ExecutionTimeout"));
    assert_eq!(timed_out.end_count(), Some(0));
    harness
        .wait_for_request("interrupt", Duration::from_secs(2))
        .await;
    let deadline = Instant::now() + Duration::from_secs(3);
    while harness.requests_of("restart_context").len() <= before {
        assert!(Instant::now() < deadline, "restart_context never sent");
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    tokio::time::sleep(Duration::from_millis(200)).await;
    assert_eq!(
        harness.run("1+1", None, 0).await.main_text().as_deref(),
        Some("1+1")
    );
}

#[tokio::test]
async fn dropping_the_stream_interrupts_the_cell() {
    let harness = harness().await;
    let mut stream = harness.open("sleep 5", None, 0).await.unwrap();
    let first = stream.message().await.unwrap().unwrap();
    assert_eq!(kind(&first), "started");
    let started = Instant::now();
    drop(stream);
    let interrupt = harness
        .wait_for_request("interrupt", Duration::from_secs(1))
        .await;
    assert!(started.elapsed() < Duration::from_secs(1));
    assert_eq!(interrupt["context_id"], "default");
    assert_eq!(
        harness.run("2", None, 0).await.main_text().as_deref(),
        Some("2")
    );
}

#[tokio::test]
async fn keepalives_flow_on_a_silent_cell() {
    let harness = harness().await;
    let collected = harness.run("sleep 1", None, 0).await;
    assert!(collected.keepalives >= 2, "{collected:?}");
    assert_eq!(collected.kinds(), vec!["started", "end"]);
}

#[tokio::test]
async fn stalled_client_gets_output_truncated() {
    let harness = harness().await;
    let mut stream = harness.open("big 400", None, 0).await.unwrap();
    let first = stream.message().await.unwrap().unwrap();
    assert_eq!(kind(&first), "started");
    tokio::time::sleep(STALL + Duration::from_millis(500)).await;
    let mut kinds = Vec::new();
    let mut truncated = None;
    while let Some(event) = stream.message().await.unwrap() {
        let name = kind(&event);
        if name == "keepalive" {
            continue;
        }
        if let Some(execute_event::Event::Error(error)) = &event.event {
            truncated = Some(error.clone());
        }
        kinds.push(name);
        if name == "end" {
            break;
        }
    }
    let truncated = truncated.expect("an OutputTruncated error");
    assert_eq!(truncated.name, "OutputTruncated");
    assert_eq!(kinds.last().copied(), Some("end"));
    assert!(kinds.iter().filter(|k| **k == "stdout").count() < 400);
    assert_eq!(
        harness.run("3", None, 0).await.main_text().as_deref(),
        Some("3")
    );
}

/// The recorder always drains the sidecar (design D9): a stalled client is
/// truncated at its own subscriber while the sidecar's stdout keeps
/// flowing, so ops issued meanwhile are answered and nothing is relaunched.
#[tokio::test]
async fn a_stalled_client_never_parks_the_sidecar() {
    let harness = harness_with(Options {
        stall_timeout: Duration::from_secs(1),
        ..Options::default()
    })
    .await;
    let mut stream = harness.open("big 2000", None, 0).await.unwrap();
    let first = stream.message().await.unwrap().unwrap();
    assert_eq!(kind(&first), "started");
    tokio::time::sleep(Duration::from_millis(1_500)).await;
    assert!(!harness.manager.dispatch_blocked());
    let ctx = harness.create_context(None).await.unwrap();
    assert!(ctx.starts_with("ctx-"));
    let collected = collect(stream).await;
    assert_eq!(collected.kinds().last().copied(), Some("end"));
    assert_eq!(collected.error_name().as_deref(), Some("OutputTruncated"));
    assert!(collected.kinds().iter().filter(|k| **k == "stdout").count() < 2000);
    assert_eq!(harness.manager.sidecar_restarts(), 0);
    assert!(harness.kernel_ready().await);
    assert_eq!(
        harness.run("3", None, 0).await.main_text().as_deref(),
        Some("3")
    );
}

#[tokio::test]
async fn contexts_lifecycle_and_isolation() {
    let harness = harness().await;
    harness.run("x = 42", None, 0).await;
    let ctx = harness.create_context(None).await.unwrap();
    assert!(ctx.starts_with("ctx-"));
    assert_eq!(ctx.len(), 16);
    let isolated = harness.run("x", Some(&ctx), 0).await;
    assert_eq!(isolated.error_name().as_deref(), Some("NameError"));
    let create = harness.requests_of("create_context").pop().unwrap();
    assert_eq!(create["context_id"], ctx);
    assert!(create["cwd"].as_str().unwrap().starts_with('/'));
    let listed = harness.list_contexts().await;
    assert_eq!(listed, vec!["default".to_owned(), ctx.clone()]);
    let a = harness.open("sleep 0.5", Some(&ctx), 0).await.unwrap();
    let b = harness.open("sleep 0.5", None, 0).await.unwrap();
    let (a, b) = tokio::join!(collect(a), collect(b));
    assert_eq!(a.kinds(), vec!["started", "end"]);
    assert_eq!(b.kinds(), vec!["started", "end"]);
    assert_eq!(
        harness.destroy_context("default").await.unwrap_err().code(),
        Code::FailedPrecondition
    );
    assert_eq!(
        harness
            .destroy_context("ctx-000000000000")
            .await
            .unwrap_err()
            .code(),
        Code::NotFound
    );
    let mut stream = harness.open("sleep 5", Some(&ctx), 0).await.unwrap();
    stream.message().await.unwrap().unwrap();
    harness.restart_context(&ctx).await.unwrap();
    let restarted = collect(stream).await;
    assert_eq!(restarted.error_name().as_deref(), Some("KernelRestarted"));
    assert_eq!(restarted.end_count(), Some(0));
    let mut stream = harness.open("sleep 5", Some(&ctx), 0).await.unwrap();
    stream.message().await.unwrap().unwrap();
    harness.destroy_context(&ctx).await.unwrap();
    let destroyed = collect(stream).await;
    assert_eq!(destroyed.error_name().as_deref(), Some("ContextDestroyed"));
    assert_eq!(harness.list_contexts().await, vec!["default".to_owned()]);
    assert_eq!(
        harness.open("1", Some(&ctx), 0).await.unwrap_err().code(),
        Code::NotFound
    );
    assert_eq!(
        harness.restart_context(&ctx).await.unwrap_err().code(),
        Code::NotFound
    );
}

#[tokio::test]
async fn context_cap_and_bad_inputs() {
    let harness = harness().await;
    for _ in 0..7 {
        harness.create_context(None).await.unwrap();
    }
    assert_eq!(harness.list_contexts().await.len(), 8);
    assert_eq!(
        harness.create_context(None).await.unwrap_err().code(),
        Code::ResourceExhausted
    );
    assert_eq!(
        harness
            .create_context(Some("relative"))
            .await
            .unwrap_err()
            .code(),
        Code::InvalidArgument
    );
    let language = harness
        .code
        .clone()
        .create_context(authenticated(CreateContextRequest {
            language: "r".to_owned(),
            cwd: None,
            envs: HashMap::new(),
        }))
        .await
        .unwrap_err();
    assert_eq!(language.code(), Code::InvalidArgument);
    assert_eq!(
        language.message(),
        "language must be one of python, bash, javascript, typescript"
    );
    let listed = harness.list_contexts().await;
    let ctx = listed[1].clone();
    harness.destroy_context(&ctx).await.unwrap();
    assert_eq!(
        harness
            .create_context(Some("/nonexistent"))
            .await
            .unwrap_err()
            .code(),
        Code::InvalidArgument
    );
    assert_eq!(harness.list_contexts().await.len(), 7);
}

#[tokio::test]
async fn sidecar_death_ends_executions_and_relaunches() {
    let harness = harness().await;
    let ctx = harness.create_context(None).await.unwrap();
    let pending = harness.open("sleep 5", Some(&ctx), 0).await.unwrap();
    let died = harness.run("die", None, 0).await;
    assert_eq!(died.error_name().as_deref(), Some("KernelDied"));
    assert_eq!(died.end_count(), Some(0));
    let pending = collect(pending).await;
    assert_eq!(pending.error_name().as_deref(), Some("KernelDied"));
    harness.wait_kernel_not_ready(Duration::from_secs(2)).await;
    let killed = harness.killed.lock().unwrap().clone();
    assert!(killed.len() >= 2, "{killed:?}");
    harness.wait_kernel_ready(Duration::from_secs(10)).await;
    assert_eq!(
        harness.open("1", Some(&ctx), 0).await.unwrap_err().code(),
        Code::NotFound
    );
    assert_eq!(harness.list_contexts().await, vec!["default".to_owned()]);
    let restarts = harness.requests_of("restart_context");
    assert!(
        restarts.len() >= 2,
        "the relaunched sidecar must be rotated again"
    );
    assert_eq!(
        restarts.last().unwrap()["envs"],
        serde_json::json!({"M4": "1"})
    );
    assert_eq!(
        harness.run("4", None, 0).await.main_text().as_deref(),
        Some("4")
    );
}

#[tokio::test]
async fn kernel_death_inside_a_cell_is_reported_and_the_context_recovers() {
    let harness = harness().await;
    let died = harness.run("kernel-die", None, 0).await;
    assert_eq!(died.kinds(), vec!["started", "error", "end"]);
    assert_eq!(died.error_name().as_deref(), Some("KernelDied"));
    assert_eq!(
        harness.run("5", None, 0).await.main_text().as_deref(),
        Some("5")
    );
    assert!(harness.kernel_ready().await);
}

#[tokio::test]
async fn execute_is_refused_while_suspending() {
    let harness = harness().await;
    harness.post(Hook::Suspend, None).await;
    let status = harness.open("1", None, 0).await.unwrap_err();
    assert_eq!(status.code(), Code::Unavailable);
    assert_eq!(status.message(), "suspending");
    harness.post(Hook::Resume, None).await;
    harness
        .wait_for_request("reseed", Duration::from_secs(2))
        .await;
    assert_eq!(
        harness.run("6", None, 0).await.main_text().as_deref(),
        Some("6")
    );
}

#[tokio::test]
async fn no_sidecar_keeps_ready_immediate_and_code_unavailable() {
    let harness = harness_with(Options {
        sidecar: false,
        run: false,
        ..Options::default()
    })
    .await;
    let (status, reply) = harness.post(Hook::Ready, None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(reply.phase, "ready");
    assert!(!harness.kernel_ready().await);
    harness.post(Hook::Run, Some(run_envelope())).await;
    let status = harness.open("1", None, 0).await.unwrap_err();
    assert_eq!(status.code(), Code::Unavailable);
    assert!(status.message().starts_with("kernel not ready: "));
    assert_eq!(
        harness.create_context(None).await.unwrap_err().code(),
        Code::Unavailable
    );
    assert!(harness.list_contexts().await.is_empty());
}

#[tokio::test]
async fn noisy_stderr_is_counted_not_echoed() {
    let harness = harness_with(Options {
        noisy_stderr: true,
        ..Options::default()
    })
    .await;
    assert_eq!(
        harness.run("7", None, 0).await.main_text().as_deref(),
        Some("7")
    );
}

#[tokio::test]
#[ignore = "needs the real kernel-sidecar with its pins installed (RAYITO_SIDECAR_ROOT)"]
async fn real_sidecar_round_trip() {
    let Ok(root) = std::env::var("RAYITO_SIDECAR_ROOT") else {
        panic!("set RAYITO_SIDECAR_ROOT to the kernel-sidecar checkout");
    };
    let tempdir = tempfile::tempdir().unwrap();
    let python = std::env::var("RAYITO_SIDECAR_PYTHON").unwrap_or_else(|_| "python3".to_owned());
    let config = SidecarConfig::new(
        vec![python, "-m".to_owned(), "rayito_kernel_sidecar".to_owned()],
        &root,
        &tempdir.path().join("k").to_string_lossy(),
    );
    let log_path = tempdir.path().join("unused.jsonl");
    let harness = build_harness(Options::default(), config, tempdir, log_path).await;
    harness.run("x = 42", None, 0).await;
    assert_eq!(
        harness.run("x", None, 0).await.main_text().as_deref(),
        Some("42")
    );
    let plot = harness
        .run(
            "import matplotlib.pyplot as plt; plt.plot([1, 2, 3]); plt.show()",
            None,
            30_000,
        )
        .await;
    match &plot.events[1].event {
        Some(execute_event::Event::Result(result)) => {
            assert!(result.png.is_some());
            assert!(result.chart.is_some());
        }
        other => panic!("unexpected {other:?}"),
    }
    let error = harness.run("1/0", None, 0).await;
    assert_eq!(error.error_name().as_deref(), Some("ZeroDivisionError"));
    let started = Instant::now();
    let timed_out = harness
        .run("import time; time.sleep(10)", None, 2_000)
        .await;
    assert_eq!(timed_out.error_name().as_deref(), Some("ExecutionTimeout"));
    assert!(started.elapsed() < Duration::from_secs(8));
    assert_eq!(
        harness.run("x", None, 0).await.main_text().as_deref(),
        Some("42")
    );
}
