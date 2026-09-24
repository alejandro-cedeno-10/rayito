//! The logical sandbox deadline in process (ADR-011, design D11
//! `m9_timeout`): `SetTimeout` behind the access token and the cap,
//! `Health.lifecycle`, pause-mode expiry (streams closed with
//! `sandbox_timeout`, every RPC but `Health` and `SetTimeout` gated,
//! `SetTimeout` reopening), the kill-mode exit sequence (workload
//! signalled, sidecar gone, shutdown cancelled, exit reason recorded), the
//! resume grace after a real freeze (the session clock jumps), the
//! auto-resume rule the `/resume` hook applies after a freeze, the single
//! hold a forged `/suspend` gets, `/suspend` answering 200 past the
//! deadline, and the log lines. The fake sidecar plays the kernels; the
//! processes and the PTY are real. Timings are shrunk through
//! `TimeoutSettings`; the payload's minimum timeout is 1 s, so each test
//! launches with 60 s and shortens with `SetTimeout(EXACT, 1000)` once its
//! streams are open.
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

mod common;

use std::collections::HashMap;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use base64::Engine;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use common::log_capture::log_capture;
use common::{
    BUDGET, Harness, Options, SECRET, authenticated, digest_hex, drain_process, harness_with, shell,
};
use http::StatusCode;
use rayd_core::code::SidecarState;
use rayd_core::lifecycle::Hook;
use rayd_core::sandbox_timeout::{TerminationReason, TimeoutSettings};
use rayd_core::session::SessionSettings;
use rayito_proto::v1::{
    ExecuteEvent, ExecuteRequest, LifecyclePhase, LifecycleState, PtyServerMessage, PtyStart,
    SetTimeoutRequest, TimeoutAction, TimeoutMode, WatchDirRequest, WatchDirResponse,
    pty_server_message, watch_dir_response,
};
use tonic::{Code, Status, Streaming};

const BASH: &str = "/bin/bash";
const EXPIRY_BUDGET: Duration = Duration::from_secs(10);
/// Each report converts monotonic instants with a fresh wall reading, so
/// two reports of the same instant may differ by a millisecond or two.
const WALL_JITTER_MS: i64 = 5;

fn settings() -> TimeoutSettings {
    TimeoutSettings {
        tick: Duration::from_millis(20),
        freeze_threshold: Duration::from_secs(2),
        resume_grace: Duration::from_millis(1_500),
        suspend_hold: Duration::from_millis(800),
        sigterm_grace: Duration::from_millis(600),
        exit_drain: Duration::from_millis(200),
        force_budget: Duration::from_secs(5),
    }
}

fn lifecycle(on_timeout: &str, auto_resume: bool) -> serde_json::Map<String, serde_json::Value> {
    let mut extra = serde_json::Map::new();
    extra.insert(
        "lifecycle".to_owned(),
        serde_json::json!({
            "timeout_s": 60,
            "cap_s": 900,
            "on_timeout": on_timeout,
            "auto_resume": auto_resume,
        }),
    );
    extra
}

/// Every test installs the capturing subscriber first, so it is the one
/// the binary keeps whichever test runs first.
async fn managed(on_timeout: &str, auto_resume: bool) -> Harness {
    managed_with(on_timeout, auto_resume, settings()).await
}

async fn managed_with(on_timeout: &str, auto_resume: bool, timeout: TimeoutSettings) -> Harness {
    let _ = log_capture();
    harness_with(Options {
        session: SessionSettings {
            timeout,
            ..SessionSettings::default()
        },
        payload_extra: lifecycle(on_timeout, auto_resume),
        ..Options::default()
    })
    .await
}

async fn unmanaged() -> Harness {
    let _ = log_capture();
    harness_with(Options {
        session: SessionSettings {
            timeout: settings(),
            ..SessionSettings::default()
        },
        ..Options::default()
    })
    .await
}

fn now_unix_ms() -> i64 {
    i64::try_from(
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_millis(),
    )
    .unwrap()
}

fn phase(state: &LifecycleState) -> LifecyclePhase {
    LifecyclePhase::try_from(state.phase).unwrap()
}

fn assert_sandbox_timeout(status: &Status) {
    assert_eq!(status.code(), Code::FailedPrecondition, "{status:?}");
    assert_eq!(status.message(), "sandbox_timeout");
}

impl Harness {
    async fn set_timeout(
        &self,
        mode: TimeoutMode,
        timeout_ms: u64,
    ) -> Result<LifecycleState, Status> {
        self.lifecycle
            .clone()
            .set_timeout(authenticated(SetTimeoutRequest {
                timeout_ms,
                mode: i32::from(mode),
            }))
            .await
            .map(tonic::Response::into_inner)
    }

    async fn lifecycle_state(&self) -> LifecycleState {
        self.health()
            .await
            .lifecycle
            .expect("an M9 agent always reports the lifecycle")
    }

    async fn wait_phase(&self, wanted: LifecyclePhase) -> Instant {
        let deadline = Instant::now() + EXPIRY_BUDGET;
        loop {
            if phase(&self.lifecycle_state().await) == wanted {
                return Instant::now();
            }
            assert!(
                Instant::now() < deadline,
                "the lifecycle never reached {wanted:?}"
            );
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    }

    async fn wait_shutdown(&self) {
        tokio::time::timeout(EXPIRY_BUDGET, self.shutdown.cancelled())
            .await
            .expect("the exit sequence never cancelled the shutdown token");
    }

    async fn pty(&self) -> (u32, Streaming<PtyServerMessage>) {
        let mut stream = self
            .ptys
            .clone()
            .create(authenticated(PtyStart {
                size: None,
                envs: HashMap::new(),
                cwd: None,
                user: None,
                shell: Some(BASH.to_owned()),
                timeout_ms: 0,
            }))
            .await
            .unwrap()
            .into_inner();
        match stream.message().await.unwrap().unwrap().message {
            Some(pty_server_message::Message::Started(started)) => (started.pid, stream),
            other => panic!("expected PtyStarted, got {other:?}"),
        }
    }

    async fn watch(&self) -> Streaming<WatchDirResponse> {
        let mut stream = self
            .files
            .clone()
            .watch_dir(authenticated(WatchDirRequest {
                path: self.root.clone(),
                recursive: false,
                user: None,
                include_entry: false,
            }))
            .await
            .unwrap()
            .into_inner();
        let first = stream.message().await.unwrap().unwrap();
        assert!(matches!(
            first.event,
            Some(watch_dir_response::Event::Started(_))
        ));
        stream
    }

    async fn execute(&self, code: &str) -> Streaming<ExecuteEvent> {
        let mut stream = self
            .code
            .clone()
            .execute(authenticated(ExecuteRequest {
                context_id: None,
                language: None,
                code: code.to_owned(),
                timeout_ms: 0,
                envs: HashMap::new(),
            }))
            .await
            .unwrap()
            .into_inner();
        stream.message().await.unwrap().unwrap();
        stream
    }
}

/// The PTY stream's terminal message, skipping output and keepalives.
async fn pty_exit(stream: &mut Streaming<PtyServerMessage>) -> rayito_proto::v1::PtyExited {
    loop {
        let message = tokio::time::timeout(EXPIRY_BUDGET, stream.message())
            .await
            .expect("pty stream stalled")
            .unwrap()
            .expect("pty stream ended without PtyExited");
        if let Some(pty_server_message::Message::Exited(exited)) = message.message {
            return exited;
        }
    }
}

/// The trailing status of a stream closed by a status, skipping messages.
async fn closing_status<T>(stream: &mut Streaming<T>) -> Status {
    loop {
        match tokio::time::timeout(EXPIRY_BUDGET, stream.message())
            .await
            .expect("stream stalled")
        {
            Ok(Some(_)) => {}
            Ok(None) => panic!("stream ended without a status"),
            Err(status) => return status,
        }
    }
}

#[tokio::test]
async fn set_timeout_requires_the_access_token() {
    let harness = managed("kill", false).await;
    let anonymous = harness
        .lifecycle
        .clone()
        .set_timeout(SetTimeoutRequest {
            timeout_ms: 120_000,
            mode: i32::from(TimeoutMode::Exact),
        })
        .await
        .unwrap_err();
    assert_eq!(anonymous.code(), Code::Unauthenticated);
    assert_eq!(harness.lifecycle_state().await.extensions, 0);
    let moved = harness
        .set_timeout(TimeoutMode::Exact, 120_000)
        .await
        .unwrap();
    assert_eq!(phase(&moved), LifecyclePhase::Active);
    assert_eq!(moved.extensions, 1);
    assert_eq!(moved.timeout_ms, 120_000);
    assert!((moved.deadline_unix_ms - (now_unix_ms() + 120_000)).abs() < 2_000);
}

#[tokio::test]
async fn set_timeout_beyond_cap_is_invalid_argument_and_keeps_the_deadline() {
    let harness = managed("kill", false).await;
    let before = harness.lifecycle_state().await;
    let beyond = harness
        .set_timeout(TimeoutMode::Exact, 2_000_000)
        .await
        .unwrap_err();
    assert_eq!(beyond.code(), Code::InvalidArgument);
    assert!(
        beyond
            .message()
            .starts_with("timeout beyond cap; cap_unix_ms="),
        "{}",
        beyond.message()
    );
    let cap_unix_ms: i64 = beyond
        .message()
        .trim_start_matches("timeout beyond cap; cap_unix_ms=")
        .parse()
        .unwrap();
    assert!((cap_unix_ms - before.cap_unix_ms).abs() <= WALL_JITTER_MS);
    let below = harness
        .set_timeout(TimeoutMode::Exact, 999)
        .await
        .unwrap_err();
    assert_eq!(below.code(), Code::InvalidArgument);
    let unspecified = harness
        .set_timeout(TimeoutMode::Unspecified, 60_000)
        .await
        .unwrap_err();
    assert_eq!(unspecified.code(), Code::InvalidArgument);
    let after = harness.lifecycle_state().await;
    assert!((after.deadline_unix_ms - before.deadline_unix_ms).abs() <= WALL_JITTER_MS);
    assert_eq!(after.extensions, before.extensions);
    assert_eq!(after.timeout_ms, before.timeout_ms);
    assert_eq!(phase(&after), LifecyclePhase::Active);
}

#[tokio::test]
async fn health_reports_the_lifecycle_after_run_and_unmanaged_without_it() {
    let harness = managed("kill", false).await;
    let state = harness.lifecycle_state().await;
    assert_eq!(phase(&state), LifecyclePhase::Active);
    assert!((state.deadline_unix_ms - (now_unix_ms() + 60_000)).abs() < 2_000);
    assert!((state.cap_unix_ms - (now_unix_ms() + 840_000)).abs() < 2_000);
    assert_eq!(state.timeout_ms, 60_000);
    assert_eq!(state.on_timeout, i32::from(TimeoutAction::Kill));
    assert!(!state.auto_resume);

    let bare = unmanaged().await;
    let state = bare.lifecycle_state().await;
    assert_eq!(phase(&state), LifecyclePhase::Unmanaged);
    assert_eq!(state.deadline_unix_ms, 0);
    assert_eq!(state.cap_unix_ms, 0);
    assert_eq!(state.on_timeout, i32::from(TimeoutAction::Unspecified));
    let refused = bare
        .set_timeout(TimeoutMode::Exact, 60_000)
        .await
        .unwrap_err();
    assert_eq!(refused.code(), Code::FailedPrecondition);
    assert_eq!(refused.message(), "lifecycle_unmanaged");
}

#[tokio::test]
async fn pause_mode_expiry_closes_streams_and_gates_rpcs() {
    let harness = managed("pause", false).await;
    let (sleeper, mut process) = harness.start(shell("sleep 30", 0)).await.unwrap();
    let (_, mut pty) = harness.pty().await;
    let mut watch = harness.watch().await;
    let mut execution = harness.execute("sleep 30").await;
    harness
        .set_timeout(TimeoutMode::Exact, 1_000)
        .await
        .unwrap();

    let tail = drain_process(&mut process, EXPIRY_BUDGET).await;
    let end = tail.end.expect("the process stream ends in-stream");
    assert_eq!(end.status, "sandbox_timeout");
    assert!(!end.exited);
    assert_eq!(end.error.unwrap().code, "sandbox_timeout");
    let exited = pty_exit(&mut pty).await;
    assert_eq!(exited.status, "sandbox_timeout");
    assert!(!exited.exited);
    assert_eq!(exited.error.unwrap().code, "sandbox_timeout");
    assert_sandbox_timeout(&closing_status(&mut watch).await);
    assert_sandbox_timeout(&closing_status(&mut execution).await);

    assert_sandbox_timeout(&harness.start(shell("echo late", 0)).await.unwrap_err());
    assert_eq!(
        phase(&harness.lifecycle_state().await),
        LifecyclePhase::Expired
    );
    assert!(
        harness.requests_of("interrupt").is_empty(),
        "an expiry never interrupts a cell"
    );

    let reopened = harness
        .set_timeout(TimeoutMode::Exact, 60_000)
        .await
        .unwrap();
    assert_eq!(phase(&reopened), LifecyclePhase::Active);
    let (_, mut stream) = harness.start(shell("echo again", 0)).await.unwrap();
    let tail = drain_process(&mut stream, BUDGET).await;
    assert_eq!(tail.end.unwrap().status, "exited");
    assert!(
        harness.list().await.iter().any(|info| info.pid == sleeper),
        "pause mode leaves the workload running"
    );
}

#[tokio::test]
async fn kill_mode_signals_the_workload_and_exits() {
    let harness = managed("kill", false).await;
    let marker = harness.path("term");
    let script = format!("trap 'echo TERM > {marker}; exit 0' TERM; sleep 30 & wait");
    let (_, mut process) = harness.start(shell(&script, 0)).await.unwrap();
    tokio::time::sleep(Duration::from_millis(200)).await;
    harness
        .set_timeout(TimeoutMode::Exact, 1_000)
        .await
        .unwrap();

    let tail = drain_process(&mut process, EXPIRY_BUDGET).await;
    let end = tail.end.expect("the process stream ends in-stream");
    assert_eq!(end.status, "sandbox_timeout");
    assert!(!end.exited);
    assert_eq!(end.error.unwrap().code, "sandbox_timeout");

    harness.wait_shutdown().await;
    assert_eq!(
        harness.exit_reason.get(),
        Some(TerminationReason::SandboxTimeout)
    );
    assert_eq!(harness.exit_reason.exit_code(), 124);
    assert_eq!(std::fs::read_to_string(&marker).unwrap().trim(), "TERM");
    assert!(
        matches!(harness.manager.sidecar_state(), SidecarState::Exited { .. }),
        "the sidecar exited and was not relaunched: {:?}",
        harness.manager.sidecar_state()
    );
    assert_eq!(
        harness
            .forced_exits
            .load(std::sync::atomic::Ordering::SeqCst),
        0,
        "the graceful sequence finished inside the force budget"
    );
}

#[tokio::test]
async fn resume_after_a_freeze_opens_the_grace_and_at_least_reactivates() {
    let harness = managed("kill", false).await;
    let (status, _) = harness.post(Hook::Suspend, None).await;
    assert_eq!(status, StatusCode::OK);
    harness.clock.jump(Duration::from_secs(90));
    let (status, reply) = harness.post(Hook::Resume, None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(reply.outcome, "changed");
    assert_eq!(
        phase(&harness.lifecycle_state().await),
        LifecyclePhase::ResumeGrace
    );
    assert_sandbox_timeout(&harness.start(shell("echo early", 0)).await.unwrap_err());

    let reopened = harness
        .set_timeout(TimeoutMode::AtLeast, 120_000)
        .await
        .unwrap();
    assert_eq!(phase(&reopened), LifecyclePhase::Active);
    assert!((reopened.deadline_unix_ms - (now_unix_ms() + 90_000 + 120_000)).abs() < 2_000);
    let (_, mut stream) = harness.start(shell("echo ok", 0)).await.unwrap();
    assert_eq!(
        drain_process(&mut stream, BUDGET).await.end.unwrap().status,
        "exited"
    );
    tokio::time::sleep(settings().resume_grace + Duration::from_millis(500)).await;
    assert!(
        !harness.shutdown.is_cancelled(),
        "a honoured grace keeps the agent"
    );
    assert_eq!(
        phase(&harness.lifecycle_state().await),
        LifecyclePhase::Active
    );
}

/// Only the `/resume` hook applies E2B's rule: the watcher alone would open
/// the grace after the jump.
#[tokio::test]
async fn auto_resume_after_a_freeze_applies_the_five_minute_minimum() {
    let harness = managed("pause", true).await;
    let (status, _) = harness.post(Hook::Suspend, None).await;
    assert_eq!(status, StatusCode::OK);
    harness.clock.jump(Duration::from_secs(90));
    let (status, reply) = harness.post(Hook::Resume, None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(reply.outcome, "changed");
    let state = harness.lifecycle_state().await;
    assert_eq!(phase(&state), LifecyclePhase::Active);
    assert_eq!(state.timeout_ms, 300_000);
    assert_eq!(state.extensions, 0);
    assert!((state.deadline_unix_ms - (now_unix_ms() + 90_000 + 300_000)).abs() < 2_000);
    let (_, mut stream) = harness.start(shell("echo resumed", 0)).await.unwrap();
    assert_eq!(
        drain_process(&mut stream, BUDGET).await.end.unwrap().status,
        "exited"
    );
}

#[tokio::test]
async fn grace_without_set_timeout_ends_the_agent() {
    let harness = managed("kill", false).await;
    harness.post(Hook::Suspend, None).await;
    harness.clock.jump(Duration::from_secs(90));
    harness.post(Hook::Resume, None).await;
    let graced = Instant::now();
    assert_eq!(
        phase(&harness.lifecycle_state().await),
        LifecyclePhase::ResumeGrace
    );
    harness.wait_shutdown().await;
    assert!(graced.elapsed() >= settings().resume_grace);
    assert_eq!(
        harness.exit_reason.get(),
        Some(TerminationReason::SandboxTimeout)
    );
}

/// The hold is long enough that scheduler lag on a loaded host cannot pass
/// for a second hold, which would act after twice the hold.
#[tokio::test]
async fn forged_suspend_holds_the_deadline_only_once() {
    let settings = TimeoutSettings {
        suspend_hold: Duration::from_secs(2),
        ..settings()
    };
    let harness = managed_with("kill", false, settings).await;
    harness
        .set_timeout(TimeoutMode::Exact, 1_000)
        .await
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(1);
    let (status, _) = harness.post(Hook::Suspend, None).await;
    assert_eq!(status, StatusCode::OK);
    tokio::time::sleep_until((deadline + Duration::from_millis(300)).into()).await;
    let (status, _) = harness.post(Hook::Suspend, None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        phase(&harness.lifecycle_state().await),
        LifecyclePhase::Active,
        "the first hold is honoured"
    );
    let acted = harness.wait_phase(LifecyclePhase::Expired).await;
    let hold = settings.suspend_hold;
    let late = acted.saturating_duration_since(deadline);
    assert!(late + settings.tick >= hold, "acted after {late:?}");
    assert!(late < hold * 2, "acted after {late:?}");
    harness.wait_shutdown().await;
}

#[tokio::test]
async fn suspend_still_answers_200_after_expiry() {
    let harness = managed("pause", true).await;
    harness
        .set_timeout(TimeoutMode::Exact, 1_000)
        .await
        .unwrap();
    harness.wait_phase(LifecyclePhase::Expired).await;
    let (status, reply) = harness.post(Hook::Suspend, None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(reply.outcome, "changed");
    let (status, _) = harness.post(Hook::Resume, None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        phase(&harness.lifecycle_state().await),
        LifecyclePhase::Expired,
        "a forged pair without a freeze never applies the auto-resume rule"
    );
    assert!(!harness.shutdown.is_cancelled());
}

#[tokio::test]
async fn lifecycle_log_lines_carry_their_fields_and_no_secret() {
    let capture = log_capture();
    let harness = managed("pause", false).await;
    harness
        .set_timeout(TimeoutMode::Exact, 1_000)
        .await
        .unwrap();
    harness.wait_phase(LifecyclePhase::Expired).await;
    let text = capture.text();
    let lines: Vec<serde_json::Value> = text
        .lines()
        .filter_map(|line| serde_json::from_str(line).ok())
        .collect();
    let message = |line: &serde_json::Value| line["fields"]["message"].as_str().map(str::to_owned);
    let transition = lines
        .iter()
        .find(|line| {
            message(line).as_deref() == Some("sandbox_timeout")
                && line["fields"]["action"] == "expire"
        })
        .expect("a sandbox_timeout line for the expiry");
    for field in [
        "phase",
        "on_timeout",
        "timeout_ms",
        "extensions",
        "overrun_ms",
        "action",
    ] {
        assert!(
            !transition["fields"][field].is_null(),
            "{field} missing in {transition}"
        );
    }
    assert_eq!(transition["fields"]["on_timeout"], "pause");
    let set_timeout = lines
        .iter()
        .find(|line| message(line).as_deref() == Some("set_timeout"))
        .expect("a set_timeout line");
    for field in ["rpc", "mode", "timeout_ms", "outcome", "extensions"] {
        assert!(
            !set_timeout["fields"][field].is_null(),
            "{field} missing in {set_timeout}"
        );
    }
    assert!(!text.contains(&URL_SAFE_NO_PAD.encode(SECRET)));
    assert!(!text.contains(std::str::from_utf8(SECRET).unwrap()));
    assert!(!text.contains(&digest_hex(SECRET)));
}
