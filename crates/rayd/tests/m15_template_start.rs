//! `ProcessManager::start_at_boot` (m15-templates, ADR-022): a template's
//! `start_cmd` must run before the build-time `/ready`, i.e. before any
//! `/run` opens the stream gate. Client RPCs keep going through `start`,
//! which still refuses until `/run`; only `start_at_boot` skips the gate.
//! Runs as whatever user CI provides (`IdentitySwitch::KeepCurrent` unless
//! root), like `m2_process`.
//!
//! Integration tests are test code, but clippy's `allow-unwrap-in-tests` only
//! recognises `#[test]` functions and `#[cfg(test)]` modules.
#![cfg(unix)]
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::collections::BTreeMap;
use std::sync::Arc;

use rayd::adapters::detect_spawn_platform;
use rayd::process::{ManagerSettings, platform_manager, shared_registry};
use rayd_core::clock::SystemClock;
use rayd_core::process::{ProcessConfigInfo, ProcessError, RegistryLimits, SpawnInput, UserPolicy};
use rayd_core::session::SandboxSession;

fn current_username() -> String {
    let uid = nix::unistd::getuid();
    nix::unistd::User::from_uid(uid)
        .unwrap()
        .map_or_else(|| uid.as_raw().to_string(), |user| user.name)
}

fn exit_zero() -> SpawnInput {
    SpawnInput {
        config: ProcessConfigInfo {
            cmd: "/bin/sh".to_owned(),
            args: vec!["-c".to_owned(), "exit 0".to_owned()],
            envs: BTreeMap::default(),
            cwd: None,
        },
        user: Some(current_username()),
        tag: Some("template_start".to_owned()),
        ..SpawnInput::default()
    }
}

#[tokio::test]
async fn start_at_boot_runs_before_run_while_start_still_refuses() {
    let session = Arc::new(SandboxSession::new(Arc::new(SystemClock::new()), "test"));
    let manager = platform_manager(
        session,
        detect_spawn_platform(),
        UserPolicy {
            allow_root: nix::unistd::geteuid().is_root(),
        },
        shared_registry(RegistryLimits::default()),
        ManagerSettings::default(),
    );

    let refused = manager.start(exit_zero()).await;
    assert!(
        matches!(refused, Err(ProcessError::NotAcceptingStreams { .. })),
        "start must keep refusing before /run"
    );

    let (pid, _stream) = manager.start_at_boot(exit_zero()).await.unwrap();
    assert!(pid.0 > 0);
}
