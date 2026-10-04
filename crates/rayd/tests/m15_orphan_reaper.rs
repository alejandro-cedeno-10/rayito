//! PID 1's orphan reaper end to end (`rayd-orphan-reaper`, Q80), in
//! process: this test binary makes itself a child subreaper, so orphans
//! re-parent to it exactly as they re-parent to `rayd` as PID 1 inside the
//! `MicroVM`. A command that starts a double-forked daemon and exits must
//! leave no `<defunct>` behind once the daemon exits, and the exit code of
//! every command `rayd` spawned itself must stay exactly what it returned,
//! with the reaper sweeping as often as it can.
//!
//! One test function on purpose: the subreaper attribute and the reaper
//! are process-wide, and its first phase needs the reaper not running yet.
#![cfg(target_os = "linux")]
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

mod common;

use std::sync::Arc;
use std::time::{Duration, Instant};

use common::{BUDGET, drain_process, harness, shell};
use rayd::adapters::{ChildRegistry, OrphanReaper, ProcfsProcessTable};
use rayd::lifecycle::spawn_child_reaper;
use rayd_core::orphans::ProcessTable;
use tokio::task::JoinSet;

/// The daemon's own lifetime after its parent command already exited.
const DAEMON_LIFETIME: &str = "0.3";
/// What every daemon-starting command exits with.
const LAUNCHER_EXIT_CODE: i32 = 7;
/// As often as the sweep can run: the race against tokio's own `wait` is
/// the thing under test.
const AGGRESSIVE_SWEEP: Duration = Duration::from_millis(1);
/// Concurrent commands in the stress phase.
const CONCURRENT_COMMANDS: i32 = 48;
const COMMAND_TIMEOUT_MS: u64 = 10_000;
const POLL: Duration = Duration::from_millis(20);

/// `sh -c` that starts a daemon the classic way (a subshell that forks it
/// into the background and exits at once, so it re-parents to the
/// subreaper), then exits with `code`.
fn daemon_then_exit(code: i32) -> String {
    format!("( sh -c 'sleep {DAEMON_LIFETIME}' & ) ; exit {code}")
}

fn own_pid() -> i32 {
    i32::try_from(std::process::id()).unwrap()
}

/// Zombies whose parent is this process, i.e. what `ps` would show as
/// `<defunct>` under `rayd`.
fn zombie_children() -> Vec<i32> {
    ProcfsProcessTable
        .snapshot()
        .into_iter()
        .filter(|entry| entry.zombie && entry.ppid == own_pid())
        .map(|entry| entry.pid)
        .collect()
}

async fn wait_until(what: &str, condition: impl Fn() -> bool) {
    let deadline = Instant::now() + BUDGET;
    while !condition() {
        assert!(Instant::now() < deadline, "timed out waiting for {what}");
        tokio::time::sleep(POLL).await;
    }
}

async fn exit_code_of(harness: &common::Harness, script: &str) -> i32 {
    let (_, mut stream) = harness
        .start(shell(script, COMMAND_TIMEOUT_MS))
        .await
        .unwrap();
    let end = drain_process(&mut stream, BUDGET)
        .await
        .end
        .expect("the command ended");
    assert_eq!(end.status, "exited", "{end:?}");
    end.exit_code
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn double_forked_daemons_are_reaped_and_exit_codes_stay_correct() {
    nix::sys::prctl::set_child_subreaper(true).unwrap();
    let harness = Arc::new(harness().await);

    // Without the reaper, the daemon outlives its launcher and stays a
    // zombie of this process (Q80's `<defunct>` under PID 1).
    let code = exit_code_of(&harness, &daemon_then_exit(LAUNCHER_EXIT_CODE)).await;
    assert_eq!(code, LAUNCHER_EXIT_CODE);
    wait_until("the orphan to become a zombie", || {
        !zombie_children().is_empty()
    })
    .await;

    let reaper = OrphanReaper::new(ChildRegistry::process());
    assert!(reaper.is_active(), "a child subreaper adopts orphans");
    let task = spawn_child_reaper(AGGRESSIVE_SWEEP, Arc::new(reaper));
    wait_until("the reaper to clear the zombie", || {
        zombie_children().is_empty()
    })
    .await;

    // Commands, daemons and short-lived children all at once, with the
    // sweep running every millisecond and on every SIGCHLD: no exit code
    // may be lost or changed.
    let mut commands = JoinSet::new();
    for index in 0..CONCURRENT_COMMANDS {
        let harness = Arc::clone(&harness);
        commands.spawn(async move {
            let script = if index % 2 == 0 {
                daemon_then_exit(index)
            } else {
                format!("exit {index}")
            };
            (index, exit_code_of(&harness, &script).await)
        });
    }
    while let Some(joined) = commands.join_next().await {
        let (expected, code) = joined.unwrap();
        assert_eq!(code, expected, "command {expected} lost its exit code");
    }
    wait_until("every daemon to be reaped", || zombie_children().is_empty()).await;
    assert!(
        harness.health().await.kernel_ready,
        "the sidecar is untouched"
    );
    task.abort();
}
