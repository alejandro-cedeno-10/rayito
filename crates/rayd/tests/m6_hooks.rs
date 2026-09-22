//! Forged hooks in process (design D1, D2, D14 `m6_hooks`): a `/run` after
//! the accepted one is a no-op the audit records, a burst of `/suspend`
//! is idempotent without a single non-200, a forged `/suspend` +
//! `/resume` pair never makes `rayd` skip the checklist of the real
//! `/suspend` right behind it, a `/suspend` nobody checkpoints is
//! recovered by the watchdog with the generation untouched, a real freeze
//! (the session clock jumps) is left alone, and the `/resume` after a
//! recovery is the real one. The fake sidecar plays the kernels; the
//! processes are real.
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

use std::time::{Duration, Instant};

use common::log_capture::log_capture;
use common::{
    BUDGET, Harness, Options, SECRET, digest_hex, drain_process, forged_envelope, harness,
    harness_with, shell,
};
use http::StatusCode;
use rayd::code::OpTimeouts;
use rayd_core::lifecycle::Hook;
use rayd_core::session::SessionSettings;
use rayito_proto::v1::ListRequest;
use tonic::Code;
use tonic::metadata::MetadataValue;

/// Watchdog settings the tests can wait for: a 1 s gate, a freeze at
/// 400 ms, ticks every 100 ms.
fn scaled_session() -> SessionSettings {
    SessionSettings {
        suspend_gate_timeout: Duration::from_secs(1),
        freeze_threshold: Duration::from_millis(400),
        watchdog_tick: Duration::from_millis(100),
    }
}

async fn list_with(harness: &Harness, secret: &[u8]) -> Result<usize, tonic::Status> {
    use base64::Engine;
    let mut request = tonic::Request::new(ListRequest {});
    let encoded = base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(secret);
    request
        .metadata_mut()
        .insert("x-access-token", MetadataValue::try_from(encoded).unwrap());
    harness
        .processes
        .clone()
        .list(request)
        .await
        .map(|response| response.into_inner().processes.len())
}

async fn wait_until_streams_open(harness: &Harness, budget: Duration) -> Duration {
    let started = Instant::now();
    loop {
        match harness.start(shell("true", 5_000)).await {
            Ok((_, mut stream)) => {
                drain_process(&mut stream, BUDGET).await;
                return started.elapsed();
            }
            Err(status) => {
                assert_eq!(status.code(), Code::Unavailable, "{status:?}");
                assert!(
                    started.elapsed() < budget,
                    "the gate never reopened within {budget:?}"
                );
                tokio::time::sleep(Duration::from_millis(50)).await;
            }
        }
    }
}

#[tokio::test]
async fn a_forged_run_is_a_no_op_that_the_audit_records() {
    let capture = log_capture();
    let harness = harness().await;
    assert_eq!(harness.health().await.hook_anomalies, 0);
    let (status, reply) = harness.post(Hook::Run, Some(forged_envelope())).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(reply.outcome, "already_ran");
    assert_eq!(reply.phase, "running");
    assert_eq!(harness.health().await.hook_anomalies, 1);
    assert!(
        list_with(&harness, SECRET).await.is_ok(),
        "the token is untouched"
    );
    assert_eq!(
        list_with(&harness, b"attacker").await.unwrap_err().code(),
        Code::Unauthenticated
    );
    assert_eq!(
        harness.requests_of("restart_context").len(),
        1,
        "the kernel is not rotated again"
    );
    let log = capture.text();
    let audit = log
        .lines()
        .find(|line| line.contains("hook_audit") && line.contains("\"hook\":\"run\""))
        .expect("a hook_audit line for the forged run");
    assert!(audit.contains("\"outcome\":\"already_ran\""), "{audit}");
    assert!(audit.contains("\"calls_since_run\":1"), "{audit}");
    assert!(audit.contains("\"anomaly\":true"), "{audit}");
    assert!(
        !log.contains(&digest_hex(b"attacker")) && !log.contains(&digest_hex(SECRET)),
        "digests never reach the log"
    );
    assert!(
        !log.contains("runHookPayload"),
        "bodies never reach the log"
    );
}

/// Ten `/suspend` in a row: the first changes the session, the other nine
/// are idempotent repeats (`unchanged`, nothing runs, no anomaly). Nobody
/// checkpoints, so the watchdog reopens the gate after the scaled timeout
/// with the generation untouched and one anomaly counted.
#[tokio::test]
async fn repeated_suspends_are_idempotent_and_the_stale_one_is_recovered() {
    let harness = harness_with(Options {
        session: scaled_session(),
        ..Options::default()
    })
    .await;
    let (_, mut background) = harness.start(shell("sleep 60", 0)).await.unwrap();
    let started = Instant::now();
    let mut outcomes = Vec::new();
    for _ in 0..10 {
        let (status, reply) = harness.post(Hook::Suspend, None).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(reply.suspend_generation, 1);
        outcomes.push((reply.outcome, reply.streams_closed));
    }
    assert!(
        started.elapsed() < Duration::from_secs(2),
        "the burst must fit inside the transition interval"
    );
    assert_eq!(outcomes[0].0, "changed");
    assert_eq!(outcomes[0].1, Some(1));
    for (outcome, streams_closed) in &outcomes[1..] {
        assert_eq!(outcome, "unchanged");
        assert_eq!(*streams_closed, Some(0));
    }
    let tail = drain_process(&mut background, BUDGET).await;
    assert_eq!(tail.end.unwrap().status, "suspending");
    assert_eq!(
        harness
            .start(shell("true", 1_000))
            .await
            .unwrap_err()
            .code(),
        Code::Unavailable
    );
    assert_eq!(harness.health().await.hook_anomalies, 0);
    let reopened_after = wait_until_streams_open(&harness, Duration::from_secs(5)).await;
    assert!(
        reopened_after >= Duration::from_millis(500),
        "{reopened_after:?}"
    );
    let health = harness.health().await;
    assert_eq!(
        health.resume_generation, 0,
        "no generation bump on recovery"
    );
    assert_eq!(health.hook_anomalies, 1, "the recovery");
    assert!(health.agent_ready);
    assert_eq!(harness.session.phase().as_str(), "resumed");
    assert_eq!(
        harness.list().await.len(),
        1,
        "the background process outlived its closed stream"
    );
}

/// The sequence the transition rate limiter of the first M6 draft broke:
/// a forged `/suspend` + `/resume` pair and, still inside what used to be
/// the 2 s interval, the real `/suspend` of the platform. The real one
/// must close the streams and arm the checklist; the real `/resume` after
/// the freeze (the session clock jumps) must be `changed`, bump the
/// generation, probe the kernels and account the frozen span, so no
/// server deadline absorbs it. Nothing is refused and nothing is counted
/// as an anomaly: the audit only sees three genuine-looking transitions.
#[tokio::test]
async fn a_forged_pair_never_makes_rayd_skip_the_real_suspend() {
    let harness = harness().await;
    let (_, mut background) = harness.start(shell("sleep 60", 0)).await.unwrap();
    let started = Instant::now();
    let (_, forged_suspend) = harness.post(Hook::Suspend, None).await;
    assert_eq!(forged_suspend.outcome, "changed");
    assert_eq!(forged_suspend.streams_closed, Some(1));
    let (_, forged_resume) = harness.post(Hook::Resume, None).await;
    assert_eq!(forged_resume.outcome, "changed");
    assert_eq!(forged_resume.resume_generation, 1);
    let tail = drain_process(&mut background, BUDGET).await;
    assert_eq!(tail.end.unwrap().status, "suspending");
    let (_, mut reconnected) = harness.start(shell("sleep 60", 0)).await.unwrap();
    let running_before = harness.session.running_now();
    tokio::time::sleep(Duration::from_millis(1_000)).await;
    let (status, real_suspend) = harness.post(Hook::Suspend, None).await;
    assert!(
        started.elapsed() < Duration::from_secs(2),
        "the real /suspend must land inside the old interval"
    );
    assert_eq!(status, StatusCode::OK);
    assert_eq!(real_suspend.outcome, "changed", "{real_suspend:?}");
    assert_eq!(real_suspend.phase, "suspending");
    assert_eq!(real_suspend.suspend_generation, 2);
    assert_eq!(
        real_suspend.streams_closed,
        Some(1),
        "the checklist ran for the real suspend"
    );
    let tail = drain_process(&mut reconnected, BUDGET).await;
    assert_eq!(tail.end.unwrap().status, "suspending");
    assert_eq!(
        harness
            .start(shell("true", 1_000))
            .await
            .unwrap_err()
            .code(),
        Code::Unavailable,
        "the gate is closed for the checkpoint"
    );
    harness.clock.jump(Duration::from_secs(30));
    let probes_before = harness.requests_of("resume").len();
    let (status, real_resume) = harness.post(Hook::Resume, None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(real_resume.outcome, "changed", "{real_resume:?}");
    assert_eq!(
        real_resume.resume_generation, 2,
        "the real resume bumps the generation"
    );
    assert_eq!(real_resume.kernel_state_lost, Some(false));
    assert_eq!(
        harness.requests_of("resume").len(),
        probes_before + 1,
        "the kernels were probed"
    );
    assert!(
        harness.session.suspended_total() >= Duration::from_secs(30),
        "the frozen span is accounted: {:?}",
        harness.session.suspended_total()
    );
    let running_after = harness.session.running_now();
    assert!(
        running_after < running_before + Duration::from_secs(5),
        "the running clock did not absorb the freeze: {running_before:?} -> {running_after:?}"
    );
    let health = harness.health().await;
    assert_eq!(
        health.hook_anomalies, 0,
        "nothing was refused, nothing is anomalous"
    );
    assert_eq!(health.resume_generation, 2);
    assert_eq!(
        harness.list().await.len(),
        2,
        "both background processes outlived the cycles"
    );
    assert!(harness.start(shell("true", 1_000)).await.is_ok());
}

#[tokio::test]
async fn a_real_freeze_is_left_alone_for_the_resume() {
    let harness = harness_with(Options {
        session: scaled_session(),
        ..Options::default()
    })
    .await;
    let (_, reply) = harness.post(Hook::Suspend, None).await;
    assert_eq!(reply.outcome, "changed");
    tokio::time::sleep(Duration::from_millis(300)).await;
    harness.clock.jump(Duration::from_secs(30));
    tokio::time::sleep(Duration::from_millis(1_800)).await;
    assert_eq!(
        harness
            .start(shell("true", 1_000))
            .await
            .unwrap_err()
            .code(),
        Code::Unavailable,
        "the gate stays closed: the watchdog saw a freeze"
    );
    assert_eq!(harness.health().await.hook_anomalies, 0);
    let (status, resumed) = harness.post(Hook::Resume, None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(resumed.outcome, "changed");
    assert_eq!(resumed.resume_generation, 1);
    assert!(harness.session.suspended_total() >= Duration::from_secs(30));
    assert!(harness.start(shell("true", 1_000)).await.is_ok());
}

#[tokio::test]
async fn the_resume_after_a_recovery_is_the_real_one() {
    let harness = harness_with(Options {
        session: scaled_session(),
        ..Options::default()
    })
    .await;
    harness.post(Hook::Suspend, None).await;
    wait_until_streams_open(&harness, Duration::from_secs(5)).await;
    assert_eq!(harness.health().await.resume_generation, 0);
    let (status, resumed) = harness.post(Hook::Resume, None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(resumed.outcome, "changed");
    assert_eq!(resumed.phase, "resumed");
    assert_eq!(resumed.resume_generation, 1);
    assert_eq!(resumed.kernel_state_lost, Some(false));
    assert!(
        !harness.requests_of("reseed").is_empty() || {
            tokio::time::sleep(Duration::from_millis(200)).await;
            !harness.requests_of("reseed").is_empty()
        }
    );
    let (_, repeated) = harness.post(Hook::Resume, None).await;
    assert_eq!(repeated.outcome, "unchanged");
    assert_eq!(repeated.resume_generation, 1);
    assert_eq!(harness.health().await.hook_anomalies, 1);
}

/// Back-to-back cycles with no gap at all: what a `/suspend` `/resume`
/// flood from an `allPorts` holder looks like. Every call is accepted
/// (never a non-200, never a refusal), every cycle bumps the generation,
/// and the audit counts nothing: the bound is on the client side (its
/// reconnect budget) and on the IAM the holder already has, not in `rayd`.
#[tokio::test]
async fn back_to_back_cycles_are_never_refused() {
    let harness = harness().await;
    for cycle in 1..=5 {
        let (status, suspended) = harness.post(Hook::Suspend, None).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(suspended.outcome, "changed", "cycle {cycle}");
        let (status, resumed) = harness.post(Hook::Resume, None).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(resumed.outcome, "changed", "cycle {cycle}");
        assert_eq!(resumed.resume_generation, cycle);
    }
    assert_eq!(harness.health().await.hook_anomalies, 0);
    assert!(harness.start(shell("true", 1_000)).await.is_ok());
}

/// The sidecar's `reseed` reply is delayed past the op timeout on three
/// consecutive `/resume` cycles: the timeouts are advisory (logged, never
/// counted), the sidecar is never killed and the next op succeeds.
#[tokio::test]
async fn a_slow_reseed_never_trips_the_kill_switch() {
    let harness = harness_with(Options {
        op_timeouts: OpTimeouts {
            reseed: Duration::from_millis(300),
            interrupt: Duration::from_secs(2),
            ..OpTimeouts::default()
        },
        fake_flags: vec!["--reseed-delay-ms".to_owned(), "20000".to_owned()],
        ..Options::default()
    })
    .await;
    for cycle in 1..=3 {
        let (_, suspended) = harness.post(Hook::Suspend, None).await;
        assert_eq!(suspended.outcome, "changed", "cycle {cycle}");
        tokio::time::sleep(Duration::from_millis(1_100)).await;
        let (_, resumed) = harness.post(Hook::Resume, None).await;
        assert_eq!(resumed.outcome, "changed", "cycle {cycle}");
        tokio::time::sleep(Duration::from_millis(1_000)).await;
        assert_eq!(harness.manager.sidecar_restarts(), 0, "cycle {cycle}");
        assert!(harness.create_context().await.is_ok(), "cycle {cycle}");
    }
    assert_eq!(harness.requests_of("reseed").len(), 3);
    assert!(harness.health().await.kernel_ready);
    assert_eq!(harness.health().await.resume_generation, 3);
}
