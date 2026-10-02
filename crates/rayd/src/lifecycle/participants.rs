//! `LifecycleParticipant` (M15 foundations, ADR-015: bounded `SuspendBudget`
//! reuse): how a 0.6 feature's agent-side slot joins `/suspend`'s bounded
//! flush and `/ready`'s combined verdict, alongside the existing
//! per-filesystem `syncfs` (`rayd_core::suspend_sync`). A slot opts in by
//! returning `Some` from `ConfigurableFeature::participant`; today every
//! slot is `features::slot::Unsupported`, whose `participant()` is always
//! `None`, so `hooks::mod`'s participant list is empty and `/suspend`'s and
//! `/ready`'s behaviour is unchanged from 0.5.x.

use std::time::Duration;

use rayd_core::suspend_sync::{ParticipantDemand, ParticipantReport};

/// What a participant's `ready_gate` tells `/ready`: `Ok` lets the existing
/// decision stand, `Retry` keeps `/ready` answering 503 (AWS retries) and
/// `Fail` answers 500, which AWS treats as definitive and fails the build
/// at once (Q85, `AWS_API_NOTES.md` §27) instead of retrying until
/// `readyTimeoutInSeconds` — `/suspend` always answers 200 regardless
/// (design D7), but `/ready` is the one hook that gates the snapshot, so it
/// is the one place a feature can hold it back.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReadyVerdict {
    Retry,
    Ok,
    Fail,
}

#[tonic::async_trait]
pub trait LifecycleParticipant: Send + Sync {
    /// The name and upper bound `hooks::mod` allocates this participant's
    /// `/suspend` share with (`rayd_core::suspend_sync::SuspendShares`).
    fn demand(&self) -> ParticipantDemand;

    /// Runs once at boot, before the hooks server answers anything — in
    /// particular before the build-time `/ready` that lets AWS take the
    /// snapshot. A no-op unless a feature needs work in the snapshot.
    async fn on_boot(&self) {}

    async fn on_run(&self) {}

    /// Runs concurrently with the per-filesystem `syncfs` calls, never
    /// after them; `share` is this participant's allocation of the
    /// `/suspend` budget (at most its own `demand().max`). `hooks::mod`
    /// also wraps the call in a `tokio::time::timeout(share, ..)`, so a
    /// participant that ignores `share` is cut off rather than delaying
    /// the 200.
    async fn on_suspend(&self, share: Duration) -> ParticipantReport {
        let _ = share;
        ParticipantReport {
            completed: true,
            timed_out: false,
        }
    }

    /// Called by a changed `/resume` (`hooks::mod`'s
    /// `run_participants_on_resume`), awaited inside that hook's own
    /// budget: keep it short and never wait on work started before the
    /// suspension (spawn or abort instead).
    async fn on_resume(&self) {}

    async fn on_terminate(&self) {}

    fn ready_gate(&self) -> ReadyVerdict {
        ReadyVerdict::Ok
    }
}
