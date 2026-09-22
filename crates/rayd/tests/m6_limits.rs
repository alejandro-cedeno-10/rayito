//! Resource bounds in process (design D5-D7, D14 `m6_limits`): the run
//! payload's `limits.cpu_seconds` kills a busy loop with `SIGXCPU`/`SIGKILL`
//! while a sleeping process is untouched, the sandbox-wide output budget
//! keeps live delivery complete while bounding replay across rings, and
//! `Write` refuses a file below the disk reserve before creating any
//! temporary (the file committed earlier in the stream stays) and maps
//! `ENOSPC` to `disk_full`. The disk faults are injected around the real
//! `StdFileSystem`; a full tmpfs is not available to the runner as uid
//! 1000, so the kernel-level `ENOSPC` is covered by the errno unit test.
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

use std::fs;
use std::sync::Arc;
use std::sync::atomic::AtomicU64;
use std::time::{Duration, Instant};

use common::{
    BUDGET, DiskFault, Harness, Options, authenticated, data_bytes, drain_process, harness_with,
    shell, temp_files,
};
use rayd_core::process::OutputBudget;
use rayito_proto::v1::{ProcessConfig, StartRequest, WriteRequest};
use tokio::sync::mpsc;
use tokio_stream::wrappers::ReceiverStream;
use tonic::{Code, Status};

const KIB: usize = 1024;
const MIB: u64 = 1024 * 1024;

fn python(code: &str, timeout_ms: u64) -> StartRequest {
    StartRequest {
        process: Some(ProcessConfig {
            cmd: "python3".to_owned(),
            args: vec!["-c".to_owned(), code.to_owned()],
            envs: std::collections::HashMap::new(),
            cwd: None,
        }),
        user: None,
        timeout_ms,
        stdin: false,
        tag: None,
    }
}

fn cpu_limited() -> Options {
    let mut extra = serde_json::Map::new();
    extra.insert("limits".to_owned(), serde_json::json!({ "cpu_seconds": 1 }));
    Options {
        payload_extra: extra,
        ..Options::default()
    }
}

#[tokio::test]
async fn a_busy_loop_is_signalled_at_its_cpu_budget_and_a_sleep_is_not() {
    let harness = harness_with(cpu_limited()).await;
    assert_eq!(harness.session.spawn_defaults().cpu_seconds, Some(1));
    let started = Instant::now();
    let (_, mut stream) = harness
        .start(python("while True: pass", 30_000))
        .await
        .unwrap();
    let tail = drain_process(&mut stream, Duration::from_secs(10)).await;
    let elapsed = started.elapsed();
    let end = tail.end.expect("the loop ended");
    assert_eq!(end.status, "signaled", "{end:?}");
    assert!(
        end.signal == Some(24) || end.signal == Some(9),
        "SIGXCPU or SIGKILL, got {:?}",
        end.signal
    );
    assert!(elapsed < Duration::from_secs(7), "{elapsed:?}");
    let (_, mut sleeper) = harness.start(shell("sleep 2", 10_000)).await.unwrap();
    let slept = drain_process(&mut sleeper, BUDGET).await.end.unwrap();
    assert_eq!(slept.status, "exited");
    assert_eq!(slept.exit_code, 0, "sleeping is not CPU time");
    let (_, mut limits) = harness
        .start(shell("ulimit -t; ulimit -Ht", 5_000))
        .await
        .unwrap();
    let mut shown = Vec::new();
    while let Some(event) = limits.message().await.unwrap() {
        if let Some(rayito_proto::v1::process_event::Event::Data(data)) = event.event {
            shown.extend_from_slice(data_bytes(&data));
        }
    }
    assert_eq!(String::from_utf8_lossy(&shown), "1\n6\n");
}

#[tokio::test]
async fn an_unlimited_payload_leaves_the_cpu_limit_off() {
    let harness = harness_with(Options::default()).await;
    assert_eq!(harness.session.spawn_defaults().cpu_seconds, None);
    let (_, mut limits) = harness.start(shell("ulimit -t", 5_000)).await.unwrap();
    let mut shown = Vec::new();
    while let Some(event) = limits.message().await.unwrap() {
        if let Some(rayito_proto::v1::process_event::Event::Data(data)) = event.event {
            shown.extend_from_slice(data_bytes(&data));
        }
    }
    assert_eq!(String::from_utf8_lossy(&shown), "unlimited\n");
}

/// Three processes print 200 KiB each into a 256 KiB budget: every live
/// subscriber gets its full output, the first ring keeps its 200 KiB, the
/// later rings only what was left, so their replay from the start is out
/// of range, and the budget never exceeds its capacity.
#[tokio::test]
async fn the_output_budget_bounds_replay_without_touching_live_delivery() {
    let budget = OutputBudget::new(256 * KIB, 192 * KIB);
    let harness = harness_with(Options {
        budget: budget.clone(),
        ..Options::default()
    })
    .await;
    let script = "head -c 204800 /dev/zero | tr '\\0' x";
    let mut pids = Vec::new();
    for _ in 0..3 {
        let (pid, mut stream) = harness.start(shell(script, 10_000)).await.unwrap();
        let tail = drain_process(&mut stream, BUDGET).await;
        assert_eq!(tail.bytes, 200 * KIB, "live delivery is complete");
        assert_eq!(tail.end.unwrap().exit_code, 0);
        pids.push(pid);
        assert!(budget.level() <= 256 * KIB, "{}", budget.level());
    }
    let mut first = harness.connect(pids[0], 1).await.unwrap();
    let replayed = drain_process(&mut first, BUDGET).await;
    assert_eq!(replayed.bytes, 200 * KIB, "the first ring kept everything");
    let third = harness.connect(pids[2], 1).await.unwrap_err();
    assert_eq!(third.code(), Code::OutOfRange, "{third:?}");
    let mut live_only = harness.connect(pids[2], 0).await.unwrap();
    assert_eq!(drain_process(&mut live_only, BUDGET).await.bytes, 0);
    assert!(budget.level() <= 256 * KIB);
    assert!(budget.level() >= 200 * KIB, "{}", budget.level());
}

async fn write_files(harness: &Harness, files: Vec<WriteRequest>) -> Result<Vec<String>, Status> {
    let mut client = harness.files.clone();
    let (sender, receiver) = mpsc::channel::<WriteRequest>(4);
    let call = tokio::spawn(async move {
        client
            .write(authenticated(ReceiverStream::new(receiver)))
            .await
    });
    for request in files {
        sender.send(request).await.unwrap();
    }
    drop(sender);
    call.await.unwrap().map(|response| {
        response
            .into_inner()
            .entries
            .into_iter()
            .map(|entry| entry.name)
            .collect()
    })
}

fn file(path: &str, bytes: usize) -> WriteRequest {
    WriteRequest {
        path: Some(path.to_owned()),
        user: None,
        mode: None,
        chunk: vec![7; bytes],
    }
}

#[tokio::test]
async fn write_refuses_a_file_below_the_disk_reserve_before_any_temporary() {
    let free = Arc::new(AtomicU64::new(64 * 1024 * MIB));
    let harness = harness_with(Options {
        disk_fault: Some(DiskFault::FreeBytes(free.clone())),
        ..Options::default()
    })
    .await;
    let first = harness.path("first.txt");
    let second = harness.path("second.txt");
    let mut client = harness.files.clone();
    let (sender, receiver) = mpsc::channel::<WriteRequest>(4);
    let call = tokio::spawn(async move {
        client
            .write(authenticated(ReceiverStream::new(receiver)))
            .await
    });
    sender.send(file(&first, 1024)).await.unwrap();
    tokio::time::sleep(Duration::from_millis(200)).await;
    free.store(10 * MIB, std::sync::atomic::Ordering::SeqCst);
    sender.send(file(&second, 1024)).await.unwrap();
    tokio::time::sleep(Duration::from_millis(200)).await;
    drop(sender);
    let status = call.await.unwrap().unwrap_err();
    assert_eq!(status.code(), Code::ResourceExhausted, "{status:?}");
    assert_eq!(status.message(), "disk_reserve");
    assert_eq!(
        fs::read(&first).unwrap().len(),
        1024,
        "the first file stays committed"
    );
    assert!(!fs::exists(&second).unwrap());
    assert_eq!(
        temp_files(&harness.root),
        0,
        "no temporary for the refused file"
    );
    free.store(300 * MIB, std::sync::atomic::Ordering::SeqCst);
    let names = write_files(&harness, vec![file(&second, 8)]).await.unwrap();
    assert_eq!(names, vec!["second.txt"]);
}

#[tokio::test]
async fn enospc_during_a_write_is_disk_full_and_leaves_no_temporary() {
    let harness = harness_with(Options {
        disk_fault: Some(DiskFault::WriteEnospc),
        ..Options::default()
    })
    .await;
    let path = harness.path("full.txt");
    let status = write_files(&harness, vec![file(&path, 16)])
        .await
        .unwrap_err();
    assert_eq!(status.code(), Code::ResourceExhausted, "{status:?}");
    assert_eq!(status.message(), "disk_full");
    assert!(!fs::exists(&path).unwrap());
    assert_eq!(temp_files(&harness.root), 0);
}
