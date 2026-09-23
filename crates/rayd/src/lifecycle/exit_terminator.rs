//! The `SelfTerminator` adapter (ADR-011): how `rayd`, PID 1 of the VM,
//! ends the workload and itself at a kill-mode deadline. The VM then goes
//! `TERMINATED` about 15 s later with no control-plane call and no IAM
//! (`AWS_API_NOTES.md` Q58).
//!
//! `begin` spawns the graceful sequence on the runtime and returns: close
//! every open stream with `sandbox_timeout`, `SIGTERM` to every process and
//! PTY group, to every kernel group and to the sidecar's group (with the
//! supervisor barred from relaunching it), `SIGKILL` after
//! `sigterm_grace`, a drain of `exit_drain`, then the exit reason is
//! recorded and the shutdown token cancelled so both listeners stop and
//! `main` returns exit code 124. `force` is the watcher thread's last
//! resort when that never completes.

use std::sync::Arc;
use std::sync::atomic::{AtomicU8, Ordering};

use rayd_core::process::timeout::{SIGKILL, SIGTERM};
use rayd_core::sandbox_timeout::{
    SelfTerminator, TIMEOUT_EXIT_CODE, TerminationReason, TimeoutSettings,
};
use tokio::runtime::Handle;
use tokio_util::sync::CancellationToken;

use super::suspend::{StreamCloseReason, SuspendSignal};
use super::timeout_watcher::StreamCloser;
use crate::code::CodeManager;
use crate::process::{ProcessManager, Spawner};

const NO_REASON: u8 = 0;
const SANDBOX_TIMEOUT: u8 = 1;

/// Why `main` is returning: recorded by the terminator before it cancels
/// the shutdown token, read once the listeners stopped.
#[derive(Debug, Default)]
pub struct ExitReason(AtomicU8);

impl ExitReason {
    pub fn set(&self, reason: TerminationReason) {
        let code = match reason {
            TerminationReason::SandboxTimeout => SANDBOX_TIMEOUT,
        };
        self.0.store(code, Ordering::SeqCst);
    }

    #[must_use]
    pub fn get(&self) -> Option<TerminationReason> {
        (self.0.load(Ordering::SeqCst) != NO_REASON).then_some(TerminationReason::SandboxTimeout)
    }

    /// 124 after a sandbox timeout; 0 after a signal or `/terminate`.
    #[must_use]
    pub fn exit_code(&self) -> u8 {
        self.get().map_or(0, |_| TIMEOUT_EXIT_CODE)
    }

    /// The `reason` field of the `rayd stopped` line.
    #[must_use]
    pub fn as_str(&self) -> &'static str {
        self.get().map_or("shutdown", TerminationReason::as_str)
    }
}

/// Ends the process now; the tests replace it with a recorder.
pub type ForceExit = Arc<dyn Fn(TerminationReason) + Send + Sync>;

/// What the terminator acts on.
pub struct ExitParts<S: Spawner> {
    pub runtime: Handle,
    pub shutdown: CancellationToken,
    pub suspend: Arc<SuspendSignal>,
    pub processes: Arc<ProcessManager<S>>,
    pub code: Arc<CodeManager>,
    pub reason: Arc<ExitReason>,
    pub settings: TimeoutSettings,
}

pub struct ExitTerminator<S: Spawner> {
    parts: ExitParts<S>,
    force_exit: ForceExit,
}

impl<S: Spawner> ExitTerminator<S> {
    #[must_use]
    pub fn new(parts: ExitParts<S>) -> Self {
        Self {
            parts,
            force_exit: Arc::new(exit_process),
        }
    }

    /// Replaces the process exit of `force` (integration tests).
    #[must_use]
    pub fn with_force_exit(mut self, force_exit: ForceExit) -> Self {
        self.force_exit = force_exit;
        self
    }
}

impl<S: Spawner> SelfTerminator for ExitTerminator<S> {
    fn begin(&self, reason: TerminationReason) {
        tracing::warn!(reason = reason.as_str(), "sandbox_timeout exit started");
        let closer = StreamCloser::new(self.parts.code.clone(), self.parts.suspend.clone());
        let sequence = ExitSequence {
            closer,
            processes: self.parts.processes.clone(),
            code: self.parts.code.clone(),
            reason_cell: self.parts.reason.clone(),
            shutdown: self.parts.shutdown.clone(),
            settings: self.parts.settings,
        };
        self.parts.runtime.spawn(sequence.run(reason));
    }

    fn force(&self, reason: TerminationReason) {
        tracing::error!(reason = reason.as_str(), "timeout_forced_exit");
        (self.force_exit)(reason);
    }
}

struct ExitSequence<S: Spawner> {
    closer: StreamCloser,
    processes: Arc<ProcessManager<S>>,
    code: Arc<CodeManager>,
    reason_cell: Arc<ExitReason>,
    shutdown: CancellationToken,
    settings: TimeoutSettings,
}

impl<S: Spawner> ExitSequence<S> {
    async fn run(self, reason: TerminationReason) {
        let streams_closed = self.closer.close(StreamCloseReason::SandboxTimeout);
        let live_processes = self.signal_workload(SIGTERM);
        tracing::info!(
            reason = reason.as_str(),
            streams_closed,
            live_processes,
            signal = SIGTERM,
            "sandbox_timeout workload signalled"
        );
        tokio::time::sleep(self.settings.sigterm_grace).await;
        let live_processes = self.signal_workload(SIGKILL);
        tracing::info!(
            reason = reason.as_str(),
            live_processes,
            signal = SIGKILL,
            "sandbox_timeout workload signalled"
        );
        tokio::time::sleep(self.settings.exit_drain).await;
        self.reason_cell.set(reason);
        self.shutdown.cancel();
    }

    /// Processes and PTYs share one registry; kernels and the sidecar are
    /// the code manager's. Returns the process and PTY groups signalled.
    fn signal_workload(&self, signal: i32) -> usize {
        let groups = self.processes.signal_all(signal);
        self.code.stop_for_exit(signal);
        groups
    }
}

fn exit_process(_reason: TerminationReason) {
    std::process::exit(i32::from(TIMEOUT_EXIT_CODE));
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn exit_reason_maps_to_the_exit_code() {
        let reason = ExitReason::default();
        assert_eq!(reason.get(), None);
        assert_eq!(reason.exit_code(), 0);
        assert_eq!(reason.as_str(), "shutdown");
        reason.set(TerminationReason::SandboxTimeout);
        assert_eq!(reason.get(), Some(TerminationReason::SandboxTimeout));
        assert_eq!(reason.exit_code(), 124);
        assert_eq!(reason.as_str(), "sandbox_timeout");
    }
}
