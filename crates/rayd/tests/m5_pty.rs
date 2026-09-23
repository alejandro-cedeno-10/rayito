//! `PtyService` end to end, in process, against real shells on real
//! pseudo-terminals (design D16 "rayd integration"): the in-process router
//! on `127.0.0.1:0`, `/run` installed through the hooks router, keepalive,
//! stall and drain intervals shrunk through the settings. Runs as whatever
//! user CI provides (`IdentitySwitch::KeepCurrent` unless root); `/dev/pts`
//! must exist, which every Linux container has.
//!
//! Integration tests are test code, but clippy's `allow-unwrap-in-tests` only
//! recognises `#[test]` functions and `#[cfg(test)]` modules.
#![cfg(unix)]
#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::naive_bytecount
)]

use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant};

use axum::Router;
use axum::body::Body;
use base64::Engine;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use http::Request;
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
use rayd_core::pty::PTY_CHUNK_BYTES;
use rayd_core::session::SandboxSession;
use rayito_proto::v1::process_service_client::ProcessServiceClient;
use rayito_proto::v1::pty_service_client::PtyServiceClient;
use rayito_proto::v1::{
    CloseStdinRequest, ConnectRequest, KillPtyRequest, ListRequest, ProcessConfig, ProcessEvent,
    ProcessInfo, ProcessKind, PtyExited, PtyServerMessage, PtySize, PtyStart, ResizeRequest,
    SendInputRequest, SendSignalRequest, StartRequest, data_event, process_event,
    pty_server_message,
};
use sha2::{Digest, Sha256};
use tokio::net::TcpListener;
use tokio_util::sync::CancellationToken;
use tonic::metadata::MetadataValue;
use tonic::transport::Channel;
use tonic::transport::server::TcpIncoming;
use tonic::{Code, Status, Streaming};
use tower::ServiceExt;

const SECRET: &[u8] = b"m5-pty-secret";
const BASH: &str = "/bin/bash";
const WORKDIR: &str = "/tmp";
const SIGKILL: i32 = 9;
const READ_TIMEOUT: Duration = Duration::from_secs(5);

struct Options {
    keepalive: Duration,
    stall: Duration,
    max_live: usize,
}

impl Default for Options {
    fn default() -> Self {
        Self {
            keepalive: Duration::from_millis(200),
            stall: Duration::from_secs(1),
            max_live: 256,
        }
    }
}

fn running_as_root() -> bool {
    nix::unistd::geteuid().is_root()
}

fn current_shell() -> String {
    nix::unistd::User::from_uid(nix::unistd::getuid())
        .ok()
        .flatten()
        .map(|user| user.shell.to_string_lossy().into_owned())
        .filter(|shell| !shell.is_empty())
        .unwrap_or_else(|| "/bin/sh".to_owned())
}

struct Harness {
    ptys: PtyServiceClient<Channel>,
    processes: ProcessServiceClient<Channel>,
    hooks: Router,
}

async fn harness() -> Harness {
    harness_with(Options::default()).await
}

async fn harness_with(options: Options) -> Harness {
    let _ = rayd::logging::init();
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
        FilesystemSettings::default(),
    );
    let registry = shared_registry(RegistryLimits {
        max_live: options.max_live,
        ..RegistryLimits::default()
    });
    let ptys = platform_pty_manager(
        session.clone(),
        &platform,
        policy,
        registry.clone(),
        PtySettings {
            stall_timeout: options.stall,
            ..PtySettings::default()
        },
    );
    let processes = platform_manager(
        session.clone(),
        platform,
        policy,
        registry,
        ManagerSettings {
            stall_timeout: options.stall,
            ..ManagerSettings::default()
        },
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
            files,
            code: code.clone(),
            metrics: Arc::new(PlatformMetricsProbe::default()),
            metrics_history: Arc::new(rayd_core::metrics_history::MetricsHistory::default()),
            suspend: suspend.clone(),
            imds: Arc::new(rayd::adapters::ImdsState::default()),
            persistence: Arc::new(rayd::persistence::UnavailablePersistence),
            timeout: rayd::lifecycle::TimeoutWatcher::detached(),
            network: rayd::network::NetworkManager::unavailable(session.clone()),
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
        ptys: PtyServiceClient::new(channel.clone()),
        processes: ProcessServiceClient::new(channel),
        hooks: rayd::hooks::router(session, code, suspend, shutdown),
    };
    harness.post(Hook::Run, Some(run_envelope())).await;
    harness
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
        "workdir": WORKDIR,
        "envs": {"SANDBOX_ENV": "from-payload"},
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

fn bash_start() -> PtyStart {
    PtyStart {
        size: None,
        envs: HashMap::new(),
        cwd: None,
        user: None,
        shell: Some(BASH.to_owned()),
        timeout_ms: 0,
    }
}

fn shell_process(script: &str) -> StartRequest {
    StartRequest {
        process: Some(ProcessConfig {
            cmd: "/bin/sh".to_owned(),
            args: vec!["-c".to_owned(), script.to_owned()],
            envs: HashMap::new(),
            cwd: None,
        }),
        user: None,
        timeout_ms: 0,
        stdin: true,
        tag: None,
    }
}

/// `echo <word>` typed so that the terminal echo of the command line never
/// contains the word itself (`echo r''eady`): only the command's output
/// does, which is what the assertions wait for.
fn say(word: &str) -> String {
    let (head, tail) = word.split_at(1);
    format!("echo {head}''{tail}\n")
}

async fn first_pid(stream: &mut Streaming<PtyServerMessage>) -> u32 {
    let first = stream.message().await.unwrap().expect("first message");
    assert_eq!(first.seq, 0);
    match first.message {
        Some(pty_server_message::Message::Started(started)) => started.pid,
        other => panic!("first message must be PtyStarted, got {other:?}"),
    }
}

/// Accumulates `data` until `needle` shows up; keepalives are counted, an
/// `exited` before the needle fails.
async fn read_until(stream: &mut Streaming<PtyServerMessage>, needle: &[u8]) -> Vec<u8> {
    read_until_with(stream, needle, READ_TIMEOUT).await.0
}

async fn read_until_with(
    stream: &mut Streaming<PtyServerMessage>,
    needle: &[u8],
    timeout: Duration,
) -> (Vec<u8>, usize, u64) {
    let deadline = Instant::now() + timeout;
    let mut buffer = Vec::new();
    let mut keepalives = 0usize;
    let mut last_seq;
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        assert!(
            !remaining.is_zero(),
            "needle {:?} not found in {:?}",
            String::from_utf8_lossy(needle),
            String::from_utf8_lossy(&buffer)
        );
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
                assert!(message.seq > 0);
                assert!(bytes.len() <= PTY_CHUNK_BYTES);
                last_seq = message.seq;
                buffer.extend(bytes);
                if buffer.windows(needle.len()).any(|window| window == needle) {
                    return (buffer, keepalives, last_seq);
                }
            }
            Some(pty_server_message::Message::Keepalive(_)) => keepalives += 1,
            Some(pty_server_message::Message::Exited(exited)) => {
                panic!("pty exited before {needle:?}: {exited:?}")
            }
            other => panic!("unexpected {other:?}"),
        }
    }
}

/// The text between `prefix` and the `\r\n` that follows it, however the
/// bytes are chunked; an `exited` before that fails.
async fn read_line_after(stream: &mut Streaming<PtyServerMessage>, prefix: &[u8]) -> String {
    let deadline = Instant::now() + READ_TIMEOUT;
    let mut buffer = Vec::new();
    loop {
        if let Some(line) = line_after(&buffer, prefix) {
            return line;
        }
        let remaining = deadline.saturating_duration_since(Instant::now());
        let message = tokio::time::timeout(remaining, stream.message())
            .await
            .unwrap_or_else(|_| {
                panic!(
                    "timed out waiting for a line after {:?}; got {:?}",
                    String::from_utf8_lossy(prefix),
                    String::from_utf8_lossy(&buffer)
                )
            })
            .unwrap()
            .expect("stream ended before the line");
        match message.message {
            Some(pty_server_message::Message::Data(bytes)) => buffer.extend(bytes),
            Some(pty_server_message::Message::Keepalive(_)) => {}
            other => panic!("unexpected {other:?}"),
        }
    }
}

fn line_after(buffer: &[u8], prefix: &[u8]) -> Option<String> {
    let start = buffer
        .windows(prefix.len())
        .position(|window| window == prefix)?
        + prefix.len();
    let rest = &buffer[start..];
    let end = rest.windows(2).position(|window| window == b"\r\n")?;
    Some(String::from_utf8_lossy(&rest[..end]).into_owned())
}

/// A zombie counts as gone: the shell that would reap it is dead and the
/// test process is nobody's subreaper.
fn process_is_gone(pid: i32) -> bool {
    match std::fs::read_to_string(format!("/proc/{pid}/status")) {
        Ok(status) => status
            .lines()
            .any(|line| line.starts_with("State:") && line.contains('Z')),
        Err(_) => true,
    }
}

async fn process_stdout(stream: &mut Streaming<ProcessEvent>) -> String {
    let mut stdout = Vec::new();
    while let Some(event) = stream.message().await.unwrap() {
        match event.event {
            Some(process_event::Event::Data(data)) => {
                if let Some(data_event::Output::Stdout(bytes)) = data.output {
                    stdout.extend(bytes);
                }
            }
            Some(process_event::Event::End(_)) => break,
            Some(process_event::Event::Keepalive(_)) => {}
            other => panic!("unexpected event {other:?}"),
        }
    }
    String::from_utf8_lossy(&stdout).into_owned()
}

/// Everything until `exited`: the bytes, the terminal message and the
/// keepalive count.
async fn collect_until_exit(
    stream: &mut Streaming<PtyServerMessage>,
) -> (Vec<u8>, PtyExited, usize) {
    let deadline = Instant::now() + Duration::from_secs(15);
    let mut buffer = Vec::new();
    let mut keepalives = 0usize;
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        let message = tokio::time::timeout(remaining, stream.message())
            .await
            .unwrap_or_else(|_| {
                panic!(
                    "no PtyExited within 15 s; got {:?}",
                    String::from_utf8_lossy(&buffer)
                )
            })
            .unwrap()
            .expect("stream ended without PtyExited");
        match message.message {
            Some(pty_server_message::Message::Data(bytes)) => buffer.extend(bytes),
            Some(pty_server_message::Message::Keepalive(_)) => keepalives += 1,
            Some(pty_server_message::Message::Exited(exited)) => {
                assert_eq!(message.seq, 0);
                assert!(stream.message().await.unwrap().is_none());
                return (buffer, exited, keepalives);
            }
            other => panic!("unexpected {other:?}"),
        }
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

    async fn create(&self, request: PtyStart) -> Result<Streaming<PtyServerMessage>, Status> {
        self.ptys
            .clone()
            .create(authenticated(request))
            .await
            .map(tonic::Response::into_inner)
    }

    async fn bash(&self) -> (u32, Streaming<PtyServerMessage>) {
        let mut stream = self.create(bash_start()).await.unwrap();
        let pid = first_pid(&mut stream).await;
        (pid, stream)
    }

    async fn connect(
        &self,
        pid: u32,
        from_seq: u64,
    ) -> Result<Streaming<PtyServerMessage>, Status> {
        self.ptys
            .clone()
            .connect(authenticated(ConnectRequest { pid, from_seq }))
            .await
            .map(tonic::Response::into_inner)
    }

    async fn send(&self, pid: u32, text: &str) -> Result<(), Status> {
        let mut client = self.ptys.clone();
        let call = client.send_input(authenticated(SendInputRequest {
            pid,
            data: text.as_bytes().to_vec(),
        }));
        tokio::time::timeout(Duration::from_secs(5), call)
            .await
            .unwrap_or_else(|_| panic!("SendInput hung for {text:?}"))
            .map(|_| ())
    }

    async fn resize(&self, pid: u32, cols: u32, rows: u32) -> Result<(), Status> {
        self.ptys
            .clone()
            .resize(authenticated(ResizeRequest {
                pid,
                size: Some(PtySize { cols, rows }),
            }))
            .await
            .map(|_| ())
    }

    async fn kill(&self, pid: u32) -> Result<(), Status> {
        self.ptys
            .clone()
            .kill(authenticated(KillPtyRequest { pid }))
            .await
            .map(|_| ())
    }

    async fn start_process(&self, request: StartRequest) -> (u32, Streaming<ProcessEvent>) {
        let mut stream = self
            .processes
            .clone()
            .start(authenticated(request))
            .await
            .unwrap()
            .into_inner();
        let first = stream.message().await.unwrap().unwrap();
        match first.event {
            Some(process_event::Event::Start(start)) => (start.pid, stream),
            other => panic!("expected StartEvent, got {other:?}"),
        }
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
}

#[tokio::test]
async fn echo_round_trip_size_tty_uid_and_env() {
    let harness = harness().await;
    let started = Instant::now();
    let (pid, mut stream) = harness.bash().await;
    assert!(pid > 0);
    harness.send(pid, "echo hola\n").await.unwrap();
    let (buffer, _, last_seq) = read_until_with(&mut stream, b"\rhola\r\n", READ_TIMEOUT).await;
    assert!(last_seq >= 1);
    assert!(buffer.windows(4).any(|window| window == b"hola"));
    println!("pty create + echo in-process: {:?}", started.elapsed());
    harness.send(pid, "stty size\n").await.unwrap();
    read_until(&mut stream, b"24 80").await;
    harness.resize(pid, 100, 30).await.unwrap();
    harness.send(pid, "stty size\n").await.unwrap();
    read_until(&mut stream, b"30 100").await;
    harness.send(pid, "tty\n").await.unwrap();
    read_until(&mut stream, b"/dev/pts/").await;
    harness.send(pid, "echo uid=$(id -u)\n").await.unwrap();
    let expected_uid = format!("uid={}\r\n", nix::unistd::getuid().as_raw());
    read_until(&mut stream, expected_uid.as_bytes()).await;
    harness
        .send(pid, "echo $TERM $LANG $LC_ALL $SHELL $SANDBOX_ENV\n")
        .await
        .unwrap();
    read_until(
        &mut stream,
        b"xterm-256color C.UTF-8 C.UTF-8 /bin/bash from-payload\r\n",
    )
    .await;
    harness.kill(pid).await.unwrap();
    let (_, exited, _) = collect_until_exit(&mut stream).await;
    assert_eq!(exited.status, "signaled");
    assert_eq!(exited.exit_code, 137);
    assert_eq!(exited.signal, Some(SIGKILL));
    assert!(exited.exited);
}

#[tokio::test]
async fn default_shell_is_the_login_shell_and_request_envs_win() {
    let harness = harness().await;
    let mut request = bash_start();
    request.shell = None;
    request.envs.insert("TERM".to_owned(), "vt100".to_owned());
    let mut stream = harness.create(request).await.unwrap();
    let pid = first_pid(&mut stream).await;
    harness.send(pid, "echo $TERM $SHELL\n").await.unwrap();
    let expected = format!("vt100 {}\r\n", current_shell());
    read_until(&mut stream, expected.as_bytes()).await;
    let listed = harness
        .list()
        .await
        .into_iter()
        .find(|info| info.pid == pid)
        .expect("listed");
    assert_eq!(listed.kind, i32::from(ProcessKind::Pty));
    let config = listed.config.unwrap();
    assert_eq!(config.cmd, current_shell());
    assert_eq!(config.args, vec!["-i", "-l"]);
    assert_eq!(config.envs.get("TERM").map(String::as_str), Some("vt100"));
    assert_eq!(config.cwd.as_deref(), Some(WORKDIR));
    harness.kill(pid).await.unwrap();
    collect_until_exit(&mut stream).await;
}

#[tokio::test]
async fn kinds_are_enforced_across_both_services_and_signal_works_on_a_pty() {
    let harness = harness().await;
    let (pty, mut pty_stream) = harness.bash().await;
    let (process, mut process_stream) = harness.start_process(shell_process("sleep 30")).await;
    let on_pty = harness
        .processes
        .clone()
        .connect(authenticated(ConnectRequest {
            pid: pty,
            from_seq: 0,
        }))
        .await
        .unwrap_err();
    assert_eq!(on_pty.code(), Code::FailedPrecondition);
    assert_eq!(
        on_pty.message(),
        format!("pid {pty} is a PTY; use PtyService")
    );
    let stdin = harness
        .processes
        .clone()
        .send_input(authenticated(SendInputRequest {
            pid: pty,
            data: b"x".to_vec(),
        }))
        .await
        .unwrap_err();
    assert_eq!(stdin.code(), Code::FailedPrecondition);
    let close = harness
        .processes
        .clone()
        .close_stdin(authenticated(CloseStdinRequest { pid: pty }))
        .await
        .unwrap_err();
    assert_eq!(close.code(), Code::FailedPrecondition);
    for status in [
        harness.connect(process, 0).await.err().unwrap(),
        harness.send(process, "x").await.unwrap_err(),
        harness.resize(process, 10, 10).await.unwrap_err(),
        harness.kill(process).await.unwrap_err(),
    ] {
        assert_eq!(status.code(), Code::FailedPrecondition, "{status}");
        assert_eq!(status.message(), format!("pid {process} is not a PTY"));
    }
    harness
        .processes
        .clone()
        .send_signal(authenticated(SendSignalRequest {
            pid: pty,
            signal: SIGKILL,
        }))
        .await
        .unwrap();
    let (_, exited, _) = collect_until_exit(&mut pty_stream).await;
    assert_eq!(exited.status, "signaled");
    assert_eq!(exited.exit_code, 137);
    harness
        .processes
        .clone()
        .send_signal(authenticated(SendSignalRequest {
            pid: process,
            signal: SIGKILL,
        }))
        .await
        .unwrap();
    while let Some(event) = process_stream.message().await.unwrap() {
        if let Some(process_event::Event::End(end)) = event.event {
            assert_eq!(end.exit_code, 137);
            break;
        }
    }
}

#[tokio::test]
async fn output_is_chunked_at_16_kib_and_replayable_from_the_ring() {
    let harness = harness().await;
    let (pid, mut stream) = harness.bash().await;
    harness.send(pid, &say("ready")).await.unwrap();
    read_until(&mut stream, b"ready\r\n").await;
    harness
        .send(pid, "head -c 100000 /dev/zero | tr '\\0' x; echo d''one\n")
        .await
        .unwrap();
    let (buffer, _, last_seq) = read_until_with(&mut stream, b"done\r\n", READ_TIMEOUT).await;
    let xs = buffer.iter().filter(|byte| **byte == b'x').count();
    assert!((100_000..100_050).contains(&xs), "{xs}");
    assert!(last_seq >= 7, "{last_seq}");
    let mut replay = harness.connect(pid, 1).await.unwrap();
    assert_eq!(first_pid(&mut replay).await, pid);
    let (replayed, _, replayed_seq) = read_until_with(&mut replay, b"done\r\n", READ_TIMEOUT).await;
    let replayed_xs = replayed.iter().filter(|byte| **byte == b'x').count();
    assert_eq!(replayed_xs, xs);
    assert_eq!(replayed_seq, last_seq);
    harness.send(pid, &say("after")).await.unwrap();
    read_until(&mut replay, b"after\r\n").await;
    read_until(&mut stream, b"after\r\n").await;
    let out_of_range = harness.connect(pid, last_seq + 100).await.unwrap_err();
    assert_eq!(out_of_range.code(), Code::OutOfRange);
    harness.kill(pid).await.unwrap();
    collect_until_exit(&mut stream).await;
}

#[tokio::test]
async fn a_stalled_second_subscriber_is_truncated_alone() {
    let harness = harness().await;
    let (pid, mut stream) = harness.bash().await;
    harness.send(pid, &say("ready")).await.unwrap();
    read_until(&mut stream, b"ready\r\n").await;
    let mut stalled = harness.connect(pid, 0).await.unwrap();
    assert_eq!(first_pid(&mut stalled).await, pid);
    harness
        .send(
            pid,
            "head -c 3000000 /dev/zero | tr '\\0' x; echo f''inished\n",
        )
        .await
        .unwrap();
    read_until_with(&mut stream, b"finished\r\n", Duration::from_secs(30)).await;
    harness.send(pid, &say("still-here")).await.unwrap();
    read_until(&mut stream, b"still-here\r\n").await;
    let (_, exited, _) = collect_until_exit(&mut stalled).await;
    assert_eq!(exited.status, "output_truncated");
    assert_eq!(exited.error.unwrap().code, "output_truncated");
    harness.kill(pid).await.unwrap();
    collect_until_exit(&mut stream).await;
}

#[tokio::test]
async fn kill_ends_with_137_and_a_second_kill_is_not_found() {
    let harness = harness().await;
    let (pid, mut stream) = harness.bash().await;
    harness.send(pid, &say("ready")).await.unwrap();
    read_until(&mut stream, b"ready\r\n").await;
    harness.kill(pid).await.unwrap();
    let (_, exited, _) = collect_until_exit(&mut stream).await;
    assert_eq!(exited.status, "signaled");
    assert_eq!(exited.exit_code, 137);
    assert_eq!(exited.signal, Some(SIGKILL));
    assert_eq!(harness.kill(pid).await.unwrap_err().code(), Code::NotFound);
    assert_eq!(
        harness.send(pid, "x").await.unwrap_err().code(),
        Code::NotFound
    );
    assert_eq!(
        harness.resize(pid, 1, 1).await.unwrap_err().code(),
        Code::NotFound
    );
    assert!(harness.list().await.iter().all(|info| info.pid != pid));
}

#[tokio::test]
async fn a_typed_exit_is_retained_and_replayable() {
    let harness = harness().await;
    let (pid, mut stream) = harness.bash().await;
    harness.send(pid, &say("bye")).await.unwrap();
    read_until(&mut stream, b"bye\r\n").await;
    harness.send(pid, "exit 3\n").await.unwrap();
    let (_, exited, _) = collect_until_exit(&mut stream).await;
    assert_eq!(exited.status, "exited");
    assert_eq!(exited.exit_code, 3);
    assert!(exited.exited);
    let mut replay = harness.connect(pid, 1).await.unwrap();
    assert_eq!(first_pid(&mut replay).await, pid);
    let (bytes, exited, _) = collect_until_exit(&mut replay).await;
    assert!(bytes.windows(3).any(|window| window == b"bye"));
    assert_eq!(exited.exit_code, 3);
}

#[tokio::test]
async fn timeout_sends_sigterm_first_and_reports_deadline_exceeded() {
    let harness = harness().await;
    let mut request = bash_start();
    request.timeout_ms = 1_500;
    let mut stream = harness.create(request).await.unwrap();
    let pid = first_pid(&mut stream).await;
    harness
        .send(pid, "trap 'echo TERM-seen; exit 0' TERM; echo a''rmed\n")
        .await
        .unwrap();
    read_until(&mut stream, b"armed\r\n").await;
    let started = Instant::now();
    let (bytes, exited, _) = collect_until_exit(&mut stream).await;
    let elapsed = started.elapsed();
    assert!(elapsed < Duration::from_secs(4), "{elapsed:?}");
    assert!(bytes.windows(9).any(|window| window == b"TERM-seen"));
    assert_eq!(exited.status, "timeout");
    assert!(!exited.exited);
    assert_eq!(exited.error.unwrap().code, "deadline_exceeded");
    assert_eq!(exited.signal, Some(15));
}

#[tokio::test]
async fn invalid_sizes_shells_and_a_failing_shell() {
    let harness = harness().await;
    for size in [(0, 0), (5000, 24), (80, 0)] {
        let mut request = bash_start();
        request.size = Some(PtySize {
            cols: size.0,
            rows: size.1,
        });
        let status = harness.create(request).await.err().unwrap();
        assert_eq!(status.code(), Code::InvalidArgument, "{size:?}");
    }
    let mut relative = bash_start();
    relative.shell = Some("bash".to_owned());
    assert_eq!(
        harness.create(relative).await.err().unwrap().code(),
        Code::InvalidArgument
    );
    let mut missing = bash_start();
    missing.shell = Some("/nonexistent/shell".to_owned());
    assert_eq!(
        harness.create(missing).await.err().unwrap().code(),
        Code::InvalidArgument
    );
    let mut cwd = bash_start();
    cwd.cwd = Some("/nonexistent".to_owned());
    assert_eq!(
        harness.create(cwd).await.err().unwrap().code(),
        Code::InvalidArgument
    );
    let mut root = bash_start();
    root.user = Some(rayito_proto::v1::User {
        username: "root".to_owned(),
    });
    if !running_as_root() {
        assert_eq!(
            harness.create(root).await.err().unwrap().code(),
            Code::PermissionDenied
        );
    }
    let mut failing = bash_start();
    failing.shell = Some("/bin/false".to_owned());
    let mut stream = harness.create(failing).await.unwrap();
    first_pid(&mut stream).await;
    let (_, exited, _) = collect_until_exit(&mut stream).await;
    assert_eq!(exited.status, "exited");
    assert_eq!(exited.exit_code, 1);
}

#[tokio::test]
async fn the_live_cap_is_shared_with_processes() {
    let harness = harness_with(Options {
        max_live: 2,
        ..Options::default()
    })
    .await;
    let (process, _process_stream) = harness.start_process(shell_process("sleep 30")).await;
    let (pty, mut pty_stream) = harness.bash().await;
    let refused = harness.create(bash_start()).await.err().unwrap();
    assert_eq!(refused.code(), Code::ResourceExhausted);
    let refused_process = harness
        .processes
        .clone()
        .start(authenticated(shell_process("true")))
        .await
        .err()
        .unwrap();
    assert_eq!(refused_process.code(), Code::ResourceExhausted);
    harness.kill(pty).await.unwrap();
    collect_until_exit(&mut pty_stream).await;
    let (second, mut second_stream) = harness.bash().await;
    assert_ne!(second, process);
    harness.kill(second).await.unwrap();
    collect_until_exit(&mut second_stream).await;
}

#[tokio::test]
async fn create_is_refused_while_suspending() {
    let harness = harness().await;
    harness.post(Hook::Suspend, None).await;
    let status = harness.create(bash_start()).await.err().unwrap();
    assert_eq!(status.code(), Code::Unavailable);
    assert_eq!(status.message(), "suspending");
    harness.post(Hook::Resume, None).await;
    let (pid, mut stream) = harness.bash().await;
    harness.kill(pid).await.unwrap();
    collect_until_exit(&mut stream).await;
}

#[tokio::test]
async fn keepalives_flow_on_a_silent_shell() {
    let harness = harness().await;
    let (pid, mut stream) = harness.bash().await;
    harness.send(pid, &say("ready")).await.unwrap();
    read_until(&mut stream, b"ready\r\n").await;
    tokio::time::sleep(Duration::from_millis(700)).await;
    harness.send(pid, &say("later")).await.unwrap();
    let (_, keepalives, _) = read_until_with(&mut stream, b"later\r\n", READ_TIMEOUT).await;
    assert!(keepalives >= 2, "{keepalives}");
    harness.kill(pid).await.unwrap();
    collect_until_exit(&mut stream).await;
}

#[tokio::test]
async fn a_background_child_holding_the_slave_does_not_delay_the_end() {
    let harness = harness().await;
    let (pid, mut stream) = harness.bash().await;
    harness.send(pid, "sleep 30 &\n").await.unwrap();
    harness.send(pid, &say("spawned")).await.unwrap();
    read_until(&mut stream, b"spawned\r\n").await;
    let started = Instant::now();
    harness.send(pid, "exit\n").await.unwrap();
    let (_, exited, _) = collect_until_exit(&mut stream).await;
    assert!(
        started.elapsed() < Duration::from_millis(1_500),
        "{:?}",
        started.elapsed()
    );
    assert_eq!(exited.status, "exited");
    assert_eq!(exited.exit_code, 0);
    assert!(harness.list().await.iter().all(|info| info.pid != pid));
}

/// Both ends of the terminal are close-on-exec: the shell holds the slave
/// as its stdio plus its own fd 255 copy (fd 3 is the substitution pipe),
/// a job sees the slave as its stdio only (fd 3 is the directory being
/// listed), and a plain process started while the PTY lives inherits
/// nothing from it either.
#[tokio::test]
async fn jobs_and_processes_inherit_neither_end_of_the_terminal() {
    let harness = harness().await;
    let (pid, mut stream) = harness.bash().await;
    harness
        .send(pid, "echo she''ll=$(ls /proc/$$/fd | tr '\\n' ' ')\n")
        .await
        .unwrap();
    let shell_fds = read_line_after(&mut stream, b"shell=").await;
    assert_eq!(shell_fds.trim(), "0 1 2 255 3");
    harness
        .send(pid, "echo fd''s=$(ls /proc/self/fd | tr '\\n' ' ')\n")
        .await
        .unwrap();
    let job_fds = read_line_after(&mut stream, b"fds=").await;
    assert_eq!(job_fds.trim(), "0 1 2 3");
    let (_, mut process) = harness
        .start_process(shell_process("ls /proc/self/fd"))
        .await;
    let process_fds = process_stdout(&mut process).await;
    assert_eq!(process_fds, "0\n1\n2\n3\n");
    harness.kill(pid).await.unwrap();
    collect_until_exit(&mut stream).await;
}

/// `Kill` ends the shell and the hangup that follows takes its foreground
/// job (its own process group under job control) down with it: nothing
/// else holds the master, so the terminal is really gone.
#[tokio::test]
async fn kill_takes_the_foreground_job_down_with_the_shell() {
    let harness = harness().await;
    let (pid, mut stream) = harness.bash().await;
    harness
        .send(pid, "(echo jo''b=$BASHPID; exec sleep 100)\n")
        .await
        .unwrap();
    let job: i32 = read_line_after(&mut stream, b"job=")
        .await
        .trim()
        .parse()
        .unwrap();
    assert!(!process_is_gone(job));
    harness.kill(pid).await.unwrap();
    let (_, exited, _) = collect_until_exit(&mut stream).await;
    assert_eq!(exited.exit_code, 137);
    let deadline = Instant::now() + Duration::from_secs(2);
    while !process_is_gone(job) {
        if Instant::now() > deadline {
            let _ = nix::sys::signal::kill(
                nix::unistd::Pid::from_raw(job),
                nix::sys::signal::Signal::SIGKILL,
            );
            panic!("sleep {job} outlived the shell");
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
}
