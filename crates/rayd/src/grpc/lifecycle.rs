//! `LifecycleService.SetTimeout` (ADR-011): moves the logical deadline
//! through the session and wakes the watcher thread so the new deadline is
//! evaluated right away. It sits behind the access-token layer, so the
//! sandbox's own code cannot extend itself, and it is one of the two RPCs
//! the deadline gate admits past the deadline (the call that reopens the
//! sandbox). Log lines carry the rpc, the mode, the timeout, the outcome
//! and the extension count, never the token.

use std::sync::Arc;
use std::time::Duration;

use rayd_core::sandbox_timeout::{
    LifecyclePhase, LifecycleView, SandboxTimeoutError, TimeoutAction, TimeoutMode,
};
use rayd_core::session::SandboxSession;
use rayito_proto::v1::lifecycle_service_server::LifecycleService;
use rayito_proto::v1::{LifecycleState, SetTimeoutRequest};
use tonic::{Request, Response, Status};

use crate::lifecycle::TimeoutWatcher;

pub struct LifecycleGrpc {
    session: Arc<SandboxSession>,
    watcher: Arc<TimeoutWatcher>,
}

impl LifecycleGrpc {
    #[must_use]
    pub fn new(session: Arc<SandboxSession>, watcher: Arc<TimeoutWatcher>) -> Self {
        Self { session, watcher }
    }
}

#[tonic::async_trait]
impl LifecycleService for LifecycleGrpc {
    async fn set_timeout(
        &self,
        request: Request<SetTimeoutRequest>,
    ) -> Result<Response<LifecycleState>, Status> {
        let request = request.into_inner();
        let mode = timeout_mode(request.mode)?;
        if request.timeout_ms == 0 {
            return Err(Status::invalid_argument("timeout_ms must be positive"));
        }
        let moved = self
            .session
            .set_timeout(mode, Duration::from_millis(request.timeout_ms));
        let outcome = moved.map_or_else(error_outcome, |_| "moved");
        tracing::info!(
            rpc = "SetTimeout",
            mode = mode.as_str(),
            timeout_ms = request.timeout_ms,
            outcome,
            extensions = self.session.lifecycle().extensions,
            "set_timeout"
        );
        let view = moved.map_err(status_for)?;
        self.watcher.wake();
        Ok(Response::new(lifecycle_state(&view)))
    }
}

fn timeout_mode(raw: i32) -> Result<TimeoutMode, Status> {
    match rayito_proto::v1::TimeoutMode::try_from(raw) {
        Ok(rayito_proto::v1::TimeoutMode::Exact) => Ok(TimeoutMode::Exact),
        Ok(rayito_proto::v1::TimeoutMode::AtLeast) => Ok(TimeoutMode::AtLeast),
        _ => Err(Status::invalid_argument("mode must be EXACT or AT_LEAST")),
    }
}

/// Beyond the cap and below one second are the caller's to fix; unmanaged
/// and terminating are states the caller cannot change with this RPC.
fn status_for(error: SandboxTimeoutError) -> Status {
    match error {
        SandboxTimeoutError::BeyondCap { .. } | SandboxTimeoutError::InvalidTimeout => {
            Status::invalid_argument(error.to_string())
        }
        SandboxTimeoutError::Unmanaged | SandboxTimeoutError::Expired => {
            Status::failed_precondition(error.to_string())
        }
    }
}

fn error_outcome(error: SandboxTimeoutError) -> &'static str {
    match error {
        SandboxTimeoutError::BeyondCap { .. } => "beyond_cap",
        SandboxTimeoutError::InvalidTimeout => "invalid_timeout",
        SandboxTimeoutError::Unmanaged => "unmanaged",
        SandboxTimeoutError::Expired => "terminating",
    }
}

/// The wire form of the lifecycle, shared with `Health`.
pub(super) fn lifecycle_state(view: &LifecycleView) -> LifecycleState {
    LifecycleState {
        phase: i32::from(phase_of(view.phase)),
        deadline_unix_ms: view.deadline_unix_ms,
        cap_unix_ms: view.cap_unix_ms,
        timeout_ms: u64::try_from(view.timeout.as_millis()).unwrap_or(u64::MAX),
        on_timeout: i32::from(action_of(view.on_timeout)),
        auto_resume: view.auto_resume,
        extensions: view.extensions,
    }
}

fn phase_of(phase: LifecyclePhase) -> rayito_proto::v1::LifecyclePhase {
    match phase {
        LifecyclePhase::Unmanaged => rayito_proto::v1::LifecyclePhase::Unmanaged,
        LifecyclePhase::Active => rayito_proto::v1::LifecyclePhase::Active,
        LifecyclePhase::ResumeGrace => rayito_proto::v1::LifecyclePhase::ResumeGrace,
        LifecyclePhase::Expired => rayito_proto::v1::LifecyclePhase::Expired,
    }
}

fn action_of(action: Option<TimeoutAction>) -> rayito_proto::v1::TimeoutAction {
    match action {
        None => rayito_proto::v1::TimeoutAction::Unspecified,
        Some(TimeoutAction::Kill) => rayito_proto::v1::TimeoutAction::Kill,
        Some(TimeoutAction::Pause) => rayito_proto::v1::TimeoutAction::Pause,
    }
}

#[cfg(test)]
mod tests {
    use tonic::Code;

    use super::*;

    #[test]
    fn unspecified_mode_is_invalid() {
        assert_eq!(timeout_mode(1).unwrap(), TimeoutMode::Exact);
        assert_eq!(timeout_mode(2).unwrap(), TimeoutMode::AtLeast);
        for raw in [0, 3, -1] {
            let status = timeout_mode(raw).unwrap_err();
            assert_eq!(status.code(), Code::InvalidArgument);
            assert_eq!(status.message(), "mode must be EXACT or AT_LEAST");
        }
    }

    #[test]
    fn errors_map_to_the_design_statuses() {
        let beyond = status_for(SandboxTimeoutError::BeyondCap { cap_unix_ms: 42 });
        assert_eq!(beyond.code(), Code::InvalidArgument);
        assert_eq!(beyond.message(), "timeout beyond cap; cap_unix_ms=42");
        let below = status_for(SandboxTimeoutError::InvalidTimeout);
        assert_eq!(below.code(), Code::InvalidArgument);
        assert_eq!(below.message(), "timeout below 1 s");
        let unmanaged = status_for(SandboxTimeoutError::Unmanaged);
        assert_eq!(unmanaged.code(), Code::FailedPrecondition);
        assert_eq!(unmanaged.message(), "lifecycle_unmanaged");
        let terminating = status_for(SandboxTimeoutError::Expired);
        assert_eq!(terminating.code(), Code::FailedPrecondition);
        assert_eq!(terminating.message(), "sandbox_timeout");
    }

    #[test]
    fn the_view_maps_to_the_wire_state() {
        let view = LifecycleView {
            phase: LifecyclePhase::ResumeGrace,
            deadline_unix_ms: 1_700_000_060_000,
            cap_unix_ms: 1_700_000_840_000,
            timeout: Duration::from_secs(60),
            on_timeout: Some(TimeoutAction::Pause),
            auto_resume: true,
            extensions: 3,
        };
        assert_eq!(
            lifecycle_state(&view),
            LifecycleState {
                phase: i32::from(rayito_proto::v1::LifecyclePhase::ResumeGrace),
                deadline_unix_ms: 1_700_000_060_000,
                cap_unix_ms: 1_700_000_840_000,
                timeout_ms: 60_000,
                on_timeout: i32::from(rayito_proto::v1::TimeoutAction::Pause),
                auto_resume: true,
                extensions: 3,
            }
        );
        let unmanaged = lifecycle_state(&LifecycleView::default());
        assert_eq!(
            unmanaged.phase,
            i32::from(rayito_proto::v1::LifecyclePhase::Unmanaged)
        );
        assert_eq!(unmanaged.deadline_unix_ms, 0);
        assert_eq!(
            unmanaged.on_timeout,
            i32::from(rayito_proto::v1::TimeoutAction::Unspecified)
        );
    }
}
