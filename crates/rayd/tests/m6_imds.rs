//! The IMDS block adapter (design D4, D14 `m6_imds`). With `CAP_NET_ADMIN`
//! in the test process (the Docker loop with `--cap-add NET_ADMIN` as root
//! and `iproute2` installed) the uid rule and the blackhole are installed,
//! `ip ... show` lists them and the install is idempotent; without the
//! capability the same calls report `imds_block_unavailable`-style reasons
//! (fail-open). `Health` mirrors the
//! state whatever the runner allows. Whether IMDS itself answers is not the
//! runner's to know: `root_reachable` is only reported. The end-to-end
//! block (uid 1000 refused, root allowed) is measured on AWS by
//! `test_imds_block` of the Python e2e.
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

use std::sync::Arc;

use common::{Options, harness_with};
use rayd::adapters::{
    ImdsBlock, ImdsState, detect_guest_capabilities, install_imds_block, rule_present,
    verify_imds_block,
};

fn net_admin() -> bool {
    detect_guest_capabilities().net_admin() && common::running_as_root()
}

#[tokio::test]
async fn the_rule_is_installed_idempotently_when_net_admin_is_present() {
    if !net_admin() {
        eprintln!(
            "m6_imds: no CAP_NET_ADMIN as root in this process; only the fail-open path runs"
        );
        return;
    }
    assert_eq!(install_imds_block().await, ImdsBlock::Installed);
    assert!(rule_present().await, "ip rule/route show list both halves");
    assert_eq!(
        install_imds_block().await,
        ImdsBlock::Installed,
        "a second install is a no-op"
    );
    let state = ImdsState::default();
    state.mark_installed();
    let probe = verify_imds_block(&state, None).await;
    assert!(probe.rule_present);
    assert_eq!(
        probe.user_reachable, None,
        "no user probe was wired: the verdict cannot be true"
    );
    assert!(!probe.imds_blocked());
    assert!(!state.blocked());
    eprintln!(
        "m6_imds: rule_present={} root_reachable={} (IMDS presence depends on the runner)",
        probe.rule_present, probe.root_reachable
    );
}

#[tokio::test]
async fn health_mirrors_the_imds_state() {
    let state = Arc::new(ImdsState::default());
    let harness = harness_with(Options {
        imds: state.clone(),
        ..Options::default()
    })
    .await;
    assert!(!harness.health().await.imds_blocked);
    state.set_blocked(true);
    assert!(
        harness.health().await.imds_blocked,
        "Health mirrors the state"
    );
    state.set_blocked(false);
    assert!(!harness.health().await.imds_blocked);
}

#[tokio::test]
async fn without_the_capability_the_block_is_unavailable() {
    if net_admin() {
        eprintln!(
            "m6_imds: this process has CAP_NET_ADMIN; the fail-open path is covered by the unit tests"
        );
        return;
    }
    let outcome = install_imds_block().await;
    match outcome {
        ImdsBlock::Unavailable { reason } => {
            assert!(
                reason.starts_with("ip"),
                "the reason names ip and the step, never its output: {reason}"
            );
        }
        ImdsBlock::Installed => panic!("ip rule add succeeded without CAP_NET_ADMIN"),
    }
    let state = ImdsState::default();
    assert!(!state.installed());
    assert!(!state.blocked());
    assert!(!rule_present().await);
}
