//! `ProcessService` and `Metrics` end to end, in process, against real
//! children of the test runner (design D16): the in-process router on
//! `127.0.0.1:0`, `/run` installed through the hooks router, keepalive,
//! stall and retention intervals shrunk through the constructors. Runs as
//! whatever user CI provides (`IdentitySwitch::KeepCurrent` unless root).
//!
//! Integration tests are test code, but clippy's `allow-unwrap-in-tests` only
//! recognises `#[test]` functions and `#[cfg(test)]` modules.
#![cfg(unix)]
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use axum::Router;
use axum::body::Body;
use base64::Engine;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use http::Request;
use nix::sys::resource::{Resource, getrlimit};
use rayd::adapters::{OsRandomSource, PlatformMetricsProbe, detect_spawn_platform};
use rayd::code::CodeManager;
use rayd::filesystem::{FilesystemSettings, platform_filesystem_manager};
use rayd::grpc::{Services, StreamSettings};
use rayd::hooks::hook_path;
use rayd::lifecycle::SuspendSignal;
use rayd::process::{ManagerSettings, platform_manager, shared_registry};
use rayd::pty::{PtySettings, platform_pty_manager};
use rayd_core::clock::SystemClock;
use rayd_core::filesystem::DenyList;
use rayd_core::lifecycle::Hook;
use rayd_core::process::{RegistryLimits, UserPolicy};
use rayd_core::session::SandboxSession;
use rayito_proto::v1::health_service_client::HealthServiceClient;
use rayito_proto::v1::process_service_client::ProcessServiceClient;
use rayito_proto::v1::{
    CloseStdinRequest, ConnectRequest, EndEvent, ListRequest, MetricsRequest, ProcessConfig,
    ProcessEvent, ProcessInfo, ProcessKind, SendInputRequest, SendSignalRequest, StartRequest,
    User, data_event, process_event,
};
use sha2::{Digest, Sha256};
use tokio::net::TcpListener;
use tokio_util::sync::CancellationToken;
use tonic::metadata::MetadataValue;
use tonic::transport::Channel;
use tonic::transport::server::TcpIncoming;
use tonic::{Code, Status, Streaming};
use tower::ServiceExt;

const SECRET: &[u8] = b"m2-sandbox-secret";
const SHELL: &str = "/bin/sh";
const WORKDIR: &str = "/tmp";
const SIGKILL: i32 = 9;

struct Options {
    keepalive: Duration,
    stall: Duration,
    retention: Duration,
    reaper: Duration,
    max_live: usize,
    max_retained: usize,
    allow_root: bool,
}

impl Default for Options {
    fn default() -> Self {
        Self {
            keepalive: Duration::from_millis(200),
            stall: Duration::from_millis(300),
            retention: Duration::from_secs(30),
            reaper: Duration::from_secs(5),
            max_live: 256,
            max_retained: 256,
            allow_root: running_as_root(),
        }
    }
}

fn running_as_root() -> bool {
    nix::unistd::geteuid().is_root()
}

struct Harness {
    processes: ProcessServiceClient<Channel>,
    health: HealthServiceClient<Channel>,
    hooks: Router,
}

async fn harness() -> Harness {
    harness_with(Options::default()).await
}

async fn harness_with(options: Options) -> Harness {
    let session = Arc::new(SandboxSession::new(Arc::new(SystemClock::new()), "test"));
    let settings = ManagerSettings {
        stall_timeout: options.stall,
        reaper_interval: options.reaper,
    };
    let registry = shared_registry(RegistryLimits {
        max_live: options.max_live,
        max_subscribers_per_pid: 8,
        retention: options.retention,
        max_retained: options.max_retained,
    });
    let policy = UserPolicy {
        allow_root: options.allow_root,
    };
    let platform = detect_spawn_platform();
    let files = platform_filesystem_manager(
        session.clone(),
        &platform,
        policy,
        DenyList::default(),
        FilesystemSettings::default(),
    );
    let ptys = platform_pty_manager(
        session.clone(),
        &platform,
        policy,
        registry.clone(),
        PtySettings::default(),
    );
    let manager = platform_manager(session.clone(), platform, policy, registry, settings);
    let _reaper = manager.spawn_reaper();
    let code = CodeManager::disabled(session.clone(), Arc::new(OsRandomSource));
    let suspend = Arc::new(SuspendSignal::new());
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let shutdown = CancellationToken::new();
    let grpc = rayd::grpc::router_with_settings(
        Services {
            session: session.clone(),
            processes: manager,
            ptys,
            files,
            code: code.clone(),
            metrics: Arc::new(PlatformMetricsProbe::default()),
            suspend: suspend.clone(),
            imds: Arc::new(rayd::adapters::ImdsState::default()),
            persistence: Arc::new(rayd::persistence::UnavailablePersistence),
        },
        StreamSettings {
            keepalive_interval: options.keepalive,
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
        processes: ProcessServiceClient::new(channel.clone()),
        health: HealthServiceClient::new(channel),
        hooks: rayd::hooks::router(session, code, suspend, shutdown),
    };
    harness.post(Hook::Run, Some(run_envelope())).await;
    harness
}

/// The `/run` payload: as root the default identity is root itself (there
/// may be no `user` account on CI); unprivileged runs keep the current user.
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
        "workdir": WORKDIR,
        "envs": {"SANDBOX_ENV": "from-payload"},
    });
    if running_as_root() {
        payload["user"] = serde_json::Value::String("root".to_owned());
    }
    serde_json::json!({ "microvmId": "mvm-m2", "runHookPayload": payload.to_string() }).to_string()
}

fn authenticated<T>(message: T) -> tonic::Request<T> {
    let mut request = tonic::Request::new(message);
    let encoded = URL_SAFE_NO_PAD.encode(SECRET);
    request
        .metadata_mut()
        .insert("x-access-token", MetadataValue::try_from(encoded).unwrap());
    request
}

fn shell(script: &str) -> StartRequest {
    StartRequest {
        process: Some(ProcessConfig {
            cmd: SHELL.to_owned(),
            args: vec!["-c".to_owned(), script.to_owned()],
            envs: HashMap::new(),
            cwd: None,
        }),
        user: None,
        timeout_ms: 0,
        stdin: false,
        tag: None,
    }
}

#[derive(Debug, Default)]
struct Collected {
    pid: u32,
    stdout: Vec<u8>,
    stderr: Vec<u8>,
    seqs: Vec<u64>,
    keepalives: usize,
    end: Option<EndEvent>,
}

impl Collected {
    fn end(&self) -> &EndEvent {
        self.end.as_ref().expect("stream ended with an EndEvent")
    }

    fn stdout_text(&self) -> String {
        String::from_utf8_lossy(&self.stdout).into_owned()
    }

    fn stderr_text(&self) -> String {
        String::from_utf8_lossy(&self.stderr).into_owned()
    }
}

async fn first_pid(stream: &mut Streaming<ProcessEvent>) -> u32 {
    let first = stream.message().await.unwrap().expect("first message");
    match first.event {
        Some(process_event::Event::Start(start)) => start.pid,
        other => panic!("first message must be StartEvent, got {other:?}"),
    }
}

/// The next `DataEvent`, skipping keepalives (the test interval is 200 ms).
async fn next_data_seq(stream: &mut Streaming<ProcessEvent>) -> u64 {
    loop {
        let event = stream.message().await.unwrap().expect("data before end");
        match event.event {
            Some(process_event::Event::Data(data)) => return data.seq,
            Some(process_event::Event::Keepalive(_)) => {}
            other => panic!("expected DataEvent, got {other:?}"),
        }
    }
}

async fn collect(stream: &mut Streaming<ProcessEvent>) -> Collected {
    let mut collected = Collected {
        pid: first_pid(stream).await,
        ..Collected::default()
    };
    collect_rest(stream, &mut collected).await;
    collected
}

async fn collect_rest(stream: &mut Streaming<ProcessEvent>, collected: &mut Collected) {
    while let Some(event) = stream.message().await.unwrap() {
        match event.event {
            Some(process_event::Event::Data(data)) => {
                collected.seqs.push(data.seq);
                match data.output {
                    Some(data_event::Output::Stdout(bytes)) => collected.stdout.extend(bytes),
                    Some(data_event::Output::Stderr(bytes)) => collected.stderr.extend(bytes),
                    None => panic!("DataEvent without output"),
                }
            }
            Some(process_event::Event::End(end)) => {
                collected.end = Some(end);
                break;
            }
            Some(process_event::Event::Keepalive(_)) => collected.keepalives += 1,
            other => panic!("unexpected event {other:?}"),
        }
    }
    assert!(collected.end.is_some(), "stream closed without EndEvent");
    assert!(
        stream.message().await.unwrap().is_none(),
        "nothing follows the EndEvent"
    );
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

    async fn start(&self, request: StartRequest) -> Result<Streaming<ProcessEvent>, Status> {
        self.processes
            .clone()
            .start(authenticated(request))
            .await
            .map(tonic::Response::into_inner)
    }

    async fn run(&self, script: &str) -> Collected {
        let mut stream = self.start(shell(script)).await.unwrap();
        collect(&mut stream).await
    }

    async fn connect(&self, pid: u32, from_seq: u64) -> Result<Streaming<ProcessEvent>, Status> {
        self.processes
            .clone()
            .connect(authenticated(ConnectRequest { pid, from_seq }))
            .await
            .map(tonic::Response::into_inner)
    }

    /// Retries `RESOURCE_EXHAUSTED` for up to one second: dead slots are
    /// reclaimed as soon as the server has processed the client's reset.
    async fn connect_once_a_slot_frees(&self, pid: u32) -> Streaming<ProcessEvent> {
        let deadline = Instant::now() + Duration::from_secs(1);
        loop {
            match self.connect(pid, 0).await {
                Ok(stream) => return stream,
                Err(status) if status.code() == Code::ResourceExhausted => {
                    assert!(Instant::now() < deadline, "subscriber slots never freed");
                    tokio::time::sleep(Duration::from_millis(10)).await;
                }
                Err(status) => panic!("connect failed: {status}"),
            }
        }
    }

    async fn send_input(&self, pid: u32, data: &[u8]) -> Result<(), Status> {
        self.processes
            .clone()
            .send_input(authenticated(SendInputRequest {
                pid,
                data: data.to_vec(),
            }))
            .await
            .map(|_| ())
    }

    async fn close_stdin(&self, pid: u32) -> Result<(), Status> {
        self.processes
            .clone()
            .close_stdin(authenticated(CloseStdinRequest { pid }))
            .await
            .map(|_| ())
    }

    async fn send_signal(&self, pid: u32, signal: i32) -> Result<(), Status> {
        self.processes
            .clone()
            .send_signal(authenticated(SendSignalRequest { pid, signal }))
            .await
            .map(|_| ())
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

    async fn listed(&self, pid: u32) -> Option<ProcessInfo> {
        self.list().await.into_iter().find(|info| info.pid == pid)
    }
}

#[tokio::test]
async fn echo_streams_start_data_and_end_in_order() {
    let harness = harness().await;
    let started = Instant::now();
    let collected = harness.run("echo hola").await;
    let in_vm_latency = started.elapsed();
    assert_eq!(collected.stdout_text(), "hola\n");
    assert_eq!(collected.stderr_text(), "");
    assert_eq!(collected.seqs, vec![1]);
    let end = collected.end();
    assert_eq!(end.status, "exited");
    assert!(end.exited);
    assert_eq!(end.exit_code, 0);
    assert_eq!(end.signal, None);
    assert_eq!(end.error, None);
    println!("echo round trip in-process: {in_vm_latency:?}");
}

#[tokio::test]
async fn exit_code_and_stderr_are_reported() {
    let harness = harness().await;
    let collected = harness.run("echo out; echo err >&2; exit 3").await;
    assert_eq!(collected.stdout_text(), "out\n");
    assert_eq!(collected.stderr_text(), "err\n");
    let mut sorted = collected.seqs.clone();
    sorted.sort_unstable();
    assert_eq!(sorted, vec![1, 2]);
    assert_eq!(collected.seqs, sorted, "delivery follows seq order");
    assert_eq!(collected.end().exit_code, 3);
    assert_eq!(collected.end().status, "exited");
}

/// `cat` exits on the first close, and `CloseStdin` on an ended pid is
/// `NOT_FOUND` by contract; the trailing `sleep` keeps the process alive so
/// the second close exercises the idempotent path.
#[tokio::test]
async fn stdin_pipe_round_trips_and_close_is_idempotent() {
    let harness = harness().await;
    let mut request = shell("cat; sleep 1");
    request.stdin = true;
    let mut stream = harness.start(request).await.unwrap();
    let pid = first_pid(&mut stream).await;
    harness.send_input(pid, b"hola\n").await.unwrap();
    harness.close_stdin(pid).await.unwrap();
    harness.close_stdin(pid).await.unwrap();
    let mut collected = Collected {
        pid,
        ..Collected::default()
    };
    collect_rest(&mut stream, &mut collected).await;
    assert_eq!(collected.stdout_text(), "hola\n");
    assert_eq!(collected.end().exit_code, 0);
}

#[tokio::test]
async fn input_without_a_stdin_pipe_is_failed_precondition() {
    let harness = harness().await;
    let mut stream = harness.start(shell("sleep 30")).await.unwrap();
    let pid = first_pid(&mut stream).await;
    assert_eq!(
        harness.send_input(pid, b"x").await.unwrap_err().code(),
        Code::FailedPrecondition
    );
    assert_eq!(
        harness.close_stdin(pid).await.unwrap_err().code(),
        Code::FailedPrecondition
    );
    harness.send_signal(pid, SIGKILL).await.unwrap();
    let collected = collect(&mut harness.connect(pid, 0).await.unwrap()).await;
    assert_eq!(collected.end().status, "signaled");
}

#[tokio::test]
async fn timeout_sends_sigterm_and_reports_deadline_exceeded() {
    let harness = harness().await;
    let mut request = shell("sleep 30");
    request.timeout_ms = 300;
    let started = Instant::now();
    let mut stream = harness.start(request).await.unwrap();
    let collected = collect(&mut stream).await;
    assert!(started.elapsed() < Duration::from_secs(2));
    let end = collected.end();
    assert_eq!(end.status, "timeout");
    assert!(!end.exited);
    assert_eq!(end.signal, Some(15));
    assert_eq!(end.exit_code, 143);
    assert_eq!(
        end.error.as_ref().map(|error| error.code.as_str()),
        Some("deadline_exceeded")
    );
    assert!(harness.listed(collected.pid).await.is_none());
}

#[tokio::test]
async fn timeout_escalates_to_sigkill_when_sigterm_is_ignored() {
    let harness = harness().await;
    let mut request = shell("trap '' TERM; while :; do sleep 1; done");
    request.timeout_ms = 300;
    let started = Instant::now();
    let mut stream = harness.start(request).await.unwrap();
    let collected = collect(&mut stream).await;
    let elapsed = started.elapsed();
    assert!(elapsed >= Duration::from_secs(5), "{elapsed:?}");
    assert!(elapsed < Duration::from_secs(9), "{elapsed:?}");
    let end = collected.end();
    assert_eq!(end.status, "timeout");
    assert_eq!(end.signal, Some(9));
    assert_eq!(end.exit_code, 137);
}

#[tokio::test]
async fn send_signal_kills_the_group_and_ended_pids_are_not_found() {
    let harness = harness().await;
    let mut stream = harness.start(shell("sleep 30")).await.unwrap();
    let pid = first_pid(&mut stream).await;
    assert_eq!(
        harness.send_signal(pid, 0).await.unwrap_err().code(),
        Code::InvalidArgument
    );
    assert_eq!(
        harness.send_signal(pid, 65).await.unwrap_err().code(),
        Code::InvalidArgument
    );
    harness.send_signal(pid, SIGKILL).await.unwrap();
    let mut collected = Collected {
        pid,
        ..Collected::default()
    };
    collect_rest(&mut stream, &mut collected).await;
    let end = collected.end();
    assert_eq!(end.status, "signaled");
    assert!(end.exited);
    assert_eq!(end.exit_code, 137);
    assert_eq!(end.signal, Some(9));
    assert_eq!(
        harness.send_signal(pid, SIGKILL).await.unwrap_err().code(),
        Code::NotFound
    );
    assert_eq!(
        harness.send_input(pid, b"x").await.unwrap_err().code(),
        Code::NotFound
    );
    assert_eq!(
        harness.close_stdin(pid).await.unwrap_err().code(),
        Code::NotFound
    );
    assert_eq!(
        harness
            .send_signal(999_999, SIGKILL)
            .await
            .unwrap_err()
            .code(),
        Code::NotFound
    );
}

#[tokio::test]
async fn connect_replays_from_seq_then_follows_live_output() {
    let harness = harness().await;
    let mut original = harness
        .start(shell("for i in 1 2 3; do echo $i; sleep 0.3; done"))
        .await
        .unwrap();
    let pid = first_pid(&mut original).await;
    assert_eq!(next_data_seq(&mut original).await, 1);
    let full = collect(&mut harness.connect(pid, 1).await.unwrap()).await;
    assert_eq!(full.pid, pid);
    assert_eq!(full.stdout_text(), "1\n2\n3\n");
    assert_eq!(full.seqs, vec![1, 2, 3]);
    assert_eq!(full.end().exit_code, 0);
    let mut rest = Collected {
        pid,
        ..Collected::default()
    };
    collect_rest(&mut original, &mut rest).await;
    assert_eq!(rest.stdout_text(), "2\n3\n");
    assert_eq!(rest.seqs, vec![2, 3]);
    let retained = collect(&mut harness.connect(pid, 0).await.unwrap()).await;
    assert!(retained.stdout.is_empty());
    assert_eq!(retained.end().exit_code, 0);
    let replayed = collect(&mut harness.connect(pid, 2).await.unwrap()).await;
    assert_eq!(replayed.stdout_text(), "2\n3\n");
    let out_of_range = harness.connect(pid, 999).await.unwrap_err();
    assert_eq!(out_of_range.code(), Code::OutOfRange);
    assert!(out_of_range.message().contains("next 4"));
    assert_eq!(
        harness.connect(999_999, 0).await.unwrap_err().code(),
        Code::NotFound
    );
}

#[tokio::test]
async fn connect_after_retention_is_not_found() {
    let harness = harness_with(Options {
        retention: Duration::from_millis(200),
        reaper: Duration::from_millis(50),
        ..Options::default()
    })
    .await;
    let collected = harness.run("true").await;
    assert!(harness.connect(collected.pid, 0).await.is_ok());
    tokio::time::sleep(Duration::from_millis(600)).await;
    assert_eq!(
        harness.connect(collected.pid, 0).await.unwrap_err().code(),
        Code::NotFound
    );
}

#[tokio::test]
async fn list_shows_live_processes_with_tag_and_kind() {
    let harness = harness().await;
    let mut request = shell("sleep 30");
    request.tag = Some("m2".to_owned());
    let mut stream = harness.start(request).await.unwrap();
    let pid = first_pid(&mut stream).await;
    let info = harness.listed(pid).await.expect("live process is listed");
    assert_eq!(info.kind, i32::from(ProcessKind::Process));
    assert_eq!(info.tag.as_deref(), Some("m2"));
    assert_eq!(info.config.as_ref().unwrap().cmd, SHELL);
    harness.send_signal(pid, SIGKILL).await.unwrap();
    let mut collected = Collected {
        pid,
        ..Collected::default()
    };
    collect_rest(&mut stream, &mut collected).await;
    assert!(harness.listed(pid).await.is_none());
}

#[tokio::test]
async fn live_cap_refuses_before_spawning() {
    let harness = harness_with(Options {
        max_live: 2,
        ..Options::default()
    })
    .await;
    let mut first = harness.start(shell("sleep 30")).await.unwrap();
    let mut second = harness.start(shell("sleep 30")).await.unwrap();
    let first_pid_value = first_pid(&mut first).await;
    let second_pid_value = first_pid(&mut second).await;
    let refused = harness.start(shell("sleep 30")).await.unwrap_err();
    assert_eq!(refused.code(), Code::ResourceExhausted);
    assert_eq!(harness.list().await.len(), 2);
    harness.send_signal(first_pid_value, SIGKILL).await.unwrap();
    harness
        .send_signal(second_pid_value, SIGKILL)
        .await
        .unwrap();
    for (stream, pid) in [
        (&mut first, first_pid_value),
        (&mut second, second_pid_value),
    ] {
        let mut collected = Collected {
            pid,
            ..Collected::default()
        };
        collect_rest(stream, &mut collected).await;
    }
    assert!(harness.start(shell("true")).await.is_ok());
}

#[tokio::test]
async fn ninth_subscriber_is_resource_exhausted() {
    let harness = harness().await;
    let mut stream = harness.start(shell("sleep 30")).await.unwrap();
    let pid = first_pid(&mut stream).await;
    let mut held = Vec::new();
    for _ in 0..7 {
        held.push(harness.connect(pid, 0).await.unwrap());
    }
    assert_eq!(
        harness.connect(pid, 0).await.unwrap_err().code(),
        Code::ResourceExhausted
    );
    harness.send_signal(pid, SIGKILL).await.unwrap();
    let mut collected = Collected {
        pid,
        ..Collected::default()
    };
    collect_rest(&mut stream, &mut collected).await;
    assert_eq!(collected.end().status, "signaled");
}

/// The SDK's `disconnect()` drops the stream without the process writing
/// anything; those slots must not count against the cap on the next
/// `Connect`. The server notices a client's `RST_STREAM` asynchronously, so
/// a freshly dropped slot may take a few milliseconds to read as closed.
#[tokio::test]
async fn dropped_streams_free_their_subscriber_slots_on_a_silent_process() {
    let harness = harness().await;
    let mut stream = harness.start(shell("sleep 30")).await.unwrap();
    let pid = first_pid(&mut stream).await;
    drop(stream);
    for _ in 0..8 {
        let mut connected = harness.connect_once_a_slot_frees(pid).await;
        assert_eq!(first_pid(&mut connected).await, pid);
        drop(connected);
    }
    let mut survivor = harness.connect_once_a_slot_frees(pid).await;
    assert_eq!(first_pid(&mut survivor).await, pid);
    harness.send_signal(pid, SIGKILL).await.unwrap();
    let mut collected = Collected {
        pid,
        ..Collected::default()
    };
    collect_rest(&mut survivor, &mut collected).await;
    assert_eq!(collected.end().status, "signaled");
}

#[tokio::test]
async fn retained_entries_are_capped_before_retention_expires() {
    let harness = harness_with(Options {
        max_retained: 2,
        ..Options::default()
    })
    .await;
    let first = harness.run("echo 1").await;
    let second = harness.run("echo 2").await;
    let third = harness.run("echo 3").await;
    assert_eq!(
        harness.connect(first.pid, 0).await.unwrap_err().code(),
        Code::NotFound
    );
    for retained in [second, third] {
        let replayed = collect(&mut harness.connect(retained.pid, 1).await.unwrap()).await;
        assert_eq!(replayed.stdout, retained.stdout);
        assert_eq!(replayed.end().exit_code, 0);
    }
}

#[tokio::test]
async fn stalled_subscriber_is_truncated_while_the_process_finishes() {
    let harness = harness().await;
    let mut stalled = harness
        .start(shell("head -c 5242880 /dev/zero"))
        .await
        .unwrap();
    let pid = first_pid(&mut stalled).await;
    let consumer = collect(&mut harness.connect(pid, 0).await.unwrap()).await;
    assert_eq!(consumer.end().status, "exited");
    assert_eq!(consumer.end().exit_code, 0);
    assert!(!consumer.stdout.is_empty());
    let mut truncated = Collected {
        pid,
        ..Collected::default()
    };
    collect_rest(&mut stalled, &mut truncated).await;
    let end = truncated.end();
    assert_eq!(end.status, "output_truncated");
    assert!(!end.exited);
    assert_eq!(
        end.error.as_ref().map(|error| error.code.as_str()),
        Some("output_truncated")
    );
    assert!(end.error.as_ref().unwrap().message.contains("at seq"));
    assert!(truncated.stdout.len() < 5_242_880);
    assert_eq!(
        truncated.seqs.last().copied(),
        Some(truncated.seqs.len() as u64),
        "the stalled subscriber saw a gap-free prefix"
    );
}

#[tokio::test]
async fn keepalives_flow_while_the_process_is_silent() {
    let harness = harness().await;
    let collected = harness.run("sleep 1").await;
    assert!(collected.keepalives >= 3, "{}", collected.keepalives);
    assert_eq!(collected.end().exit_code, 0);
}

#[tokio::test]
async fn large_output_arrives_complete_and_in_order() {
    let harness = harness().await;
    let collected = harness.run("head -c 300000 /dev/zero | tr '\\0' a").await;
    assert_eq!(collected.stdout.len(), 300_000);
    assert!(collected.stdout.iter().all(|byte| *byte == b'a'));
    assert!(collected.seqs.windows(2).all(|pair| pair[0] < pair[1]));
    assert_eq!(collected.end().exit_code, 0);
}

#[tokio::test]
async fn environment_is_built_from_scratch() {
    let harness = harness().await;
    let env = harness.run("env").await.stdout_text();
    for expected in [
        "HOME=",
        "USER=",
        "LOGNAME=",
        "PATH=",
        "SANDBOX_ENV=from-payload",
    ] {
        assert!(
            env.lines().any(|line| line.starts_with(expected)),
            "{expected}"
        );
    }
    assert!(
        !env.contains("CARGO_MANIFEST_DIR"),
        "the test runner environment leaked into the child"
    );
    assert!(!env.contains("RAYD_LOG"));
    let mut request = shell("env");
    request
        .process
        .as_mut()
        .unwrap()
        .envs
        .insert("SANDBOX_ENV".to_owned(), "from-request".to_owned());
    let mut stream = harness.start(request).await.unwrap();
    let env = collect(&mut stream).await.stdout_text();
    assert!(env.lines().any(|line| line == "SANDBOX_ENV=from-request"));
}

#[tokio::test]
async fn cwd_precedence_and_validation() {
    let harness = harness().await;
    assert_eq!(
        harness.run("pwd").await.stdout_text(),
        format!("{WORKDIR}\n")
    );
    let mut request = shell("pwd");
    request.process.as_mut().unwrap().cwd = Some("/".to_owned());
    let mut stream = harness.start(request).await.unwrap();
    assert_eq!(collect(&mut stream).await.stdout_text(), "/\n");
    for invalid in ["/does/not/exist", "relative"] {
        let mut request = shell("true");
        request.process.as_mut().unwrap().cwd = Some(invalid.to_owned());
        let status = harness.start(request).await.unwrap_err();
        assert_eq!(status.code(), Code::InvalidArgument, "{invalid}");
        assert!(
            !status.message().contains(invalid),
            "path must not be echoed"
        );
    }
}

#[tokio::test]
async fn empty_and_missing_programs_are_invalid_argument() {
    let harness = harness().await;
    let empty = harness
        .start(StartRequest {
            process: None,
            ..shell("true")
        })
        .await
        .unwrap_err();
    assert_eq!(empty.code(), Code::InvalidArgument);
    let mut blank = shell("true");
    blank.process.as_mut().unwrap().cmd = String::new();
    assert_eq!(
        harness.start(blank).await.unwrap_err().code(),
        Code::InvalidArgument
    );
    let mut missing = shell("true");
    missing.process.as_mut().unwrap().cmd = "/nonexistent/rayito-binary".to_owned();
    let status = harness.start(missing).await.unwrap_err();
    assert_eq!(status.code(), Code::InvalidArgument);
    assert!(status.message().contains("ENOENT"), "{}", status.message());
    assert!(!status.message().contains("rayito-binary"));
}

#[tokio::test]
async fn root_is_refused_without_allow_root() {
    let harness = harness_with(Options {
        allow_root: false,
        ..Options::default()
    })
    .await;
    let mut request = shell("whoami");
    request.user = Some(User {
        username: "root".to_owned(),
    });
    assert_eq!(
        harness.start(request).await.unwrap_err().code(),
        Code::PermissionDenied
    );
}

#[tokio::test]
async fn unknown_user_is_invalid_argument_when_identities_are_enforced() {
    if !running_as_root() {
        return;
    }
    let harness = harness().await;
    let mut request = shell("true");
    request.user = Some(User {
        username: "rayito-no-such-user".to_owned(),
    });
    assert_eq!(
        harness.start(request).await.unwrap_err().code(),
        Code::InvalidArgument
    );
}

#[tokio::test]
async fn streams_are_unavailable_while_suspending() {
    let harness = harness().await;
    let mut stream = harness.start(shell("sleep 30")).await.unwrap();
    let pid = first_pid(&mut stream).await;
    harness.post(Hook::Suspend, None).await;
    let refused = harness.start(shell("true")).await.unwrap_err();
    assert_eq!(refused.code(), Code::Unavailable);
    assert_eq!(refused.message(), "suspending");
    assert_eq!(
        harness.connect(pid, 0).await.unwrap_err().code(),
        Code::Unavailable
    );
    assert!(harness.listed(pid).await.is_some());
    harness.post(Hook::Resume, None).await;
    assert!(harness.connect(pid, 0).await.is_ok());
    harness.send_signal(pid, SIGKILL).await.unwrap();
    let mut collected = Collected {
        pid,
        ..Collected::default()
    };
    collect_rest(&mut stream, &mut collected).await;
}

/// The soft `Max processes` value of `/proc/self/limits`: Debian's `dash`
/// (`/bin/sh` on CI runners) has no `ulimit -u`, so the kernel view is the
/// portable source.
fn soft_nproc(limits: &str) -> String {
    limits
        .lines()
        .find_map(|line| line.strip_prefix("Max processes"))
        .and_then(|rest| rest.split_whitespace().next())
        .expect("Max processes in /proc/self/limits")
        .to_owned()
}

#[tokio::test]
async fn resource_limits_are_applied_or_clamped() {
    let harness = harness().await;
    let (_, nofile_hard) = getrlimit(Resource::RLIMIT_NOFILE).unwrap();
    let (_, nproc_hard) = getrlimit(Resource::RLIMIT_NPROC).unwrap();
    let output = harness
        .run("ulimit -n; ulimit -c; cat /proc/self/limits")
        .await;
    let text = output.stdout_text();
    let mut lines = text.lines().map(str::trim);
    let nofile = lines.next().expect("ulimit -n line");
    let core = lines.next().expect("ulimit -c line");
    let nproc = soft_nproc(&text);
    let expected_nofile = if running_as_root() {
        4096
    } else {
        4096.min(nofile_hard)
    };
    let expected_nproc = if running_as_root() {
        512
    } else {
        512.min(nproc_hard)
    };
    assert!(
        nofile == expected_nofile.to_string() || nofile == nofile_hard.to_string(),
        "nofile {nofile} (hard {nofile_hard})"
    );
    assert!(
        nproc == expected_nproc.to_string() || nproc == nproc_hard.to_string(),
        "nproc {nproc} (hard {nproc_hard})"
    );
    assert_eq!(core, "0");
}

#[tokio::test]
async fn metrics_report_procfs_values_and_require_a_token() {
    let harness = harness().await;
    let started = Instant::now();
    let metrics = harness
        .health
        .clone()
        .metrics(authenticated(MetricsRequest {}))
        .await
        .unwrap()
        .into_inner();
    println!("metrics call: {:?}", started.elapsed());
    assert!(metrics.cpu_count >= 1);
    assert!(metrics.mem_total_bytes > 0);
    assert!(metrics.mem_used_bytes <= metrics.mem_total_bytes);
    assert!(metrics.disk_total_bytes > 0);
    assert!(metrics.disk_used_bytes <= metrics.disk_total_bytes);
    assert!((0.0..=100.0).contains(&metrics.cpu_used_pct));
    let now_ms = i64::try_from(
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_millis(),
    )
    .unwrap();
    assert!((now_ms - metrics.timestamp_unix_ms).abs() < 60_000);
    let anonymous = harness
        .health
        .clone()
        .metrics(MetricsRequest {})
        .await
        .unwrap_err();
    assert_eq!(anonymous.code(), Code::Unauthenticated);
}
