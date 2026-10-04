//! `ReadyProbe` (m15-templates, investigación §3.5): runs a template's
//! `ready_cmd` with `/bin/sh -c`, the same contract `_ready_cmds.py`'s
//! helpers document ("compilan a una cadena de shell que `rayd` ejecuta con
//! `/bin/sh -c`"). Unlike `start_cmd` (a long-lived, managed process
//! visible in `commands.list` via `ProcessManager`), a `ready_cmd` probe is
//! a short-lived, unmanaged child: polling it through the full process
//! registry would fill `commands.list` with one retired entry per poll
//! tick. `rayd_core::template::ready_decision` is the pure state machine
//! this feeds; this adapter only ever reports an exit code or `None`.
//!
//! Runs as `rayd`'s own identity (root in production), not the template
//! user: `ready_cmd` is baked into the image by the template author at
//! `Template.build()` time, the same trust level as a Dockerfile `RUN`
//! step, and it only ever checks a local readiness signal (a port, a URL,
//! a file, a process) that does not need the sandbox user's privileges to
//! observe.

use std::process::Stdio;
use std::time::Duration;

use rayd_core::pty::FALLBACK_SHELL;
use tokio::process::Command;

use super::child_registry::ChildRegistry;

#[tonic::async_trait]
pub trait ReadyProbe: Send + Sync {
    /// The exit code of `cmd`, or `None` when it could not even be spawned
    /// or did not finish inside `budget`. The caller treats `None` the same
    /// as a non-zero exit: keep retrying until `ReadyPoll`'s own deadline.
    async fn probe(&self, cmd: &str, budget: Duration) -> Option<i32>;
}

#[derive(Debug, Default, Clone, Copy)]
pub struct ShellReadyProbe;

#[tonic::async_trait]
impl ReadyProbe for ShellReadyProbe {
    async fn probe(&self, cmd: &str, budget: Duration) -> Option<i32> {
        let mut command = Command::new(FALLBACK_SHELL);
        command
            .arg("-c")
            .arg(cmd)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .kill_on_drop(true);
        let mut child = ChildRegistry::process().spawn(&mut command).ok()?;
        match tokio::time::timeout(budget, child.wait()).await {
            Ok(Ok(status)) => status.code(),
            Ok(Err(_)) | Err(_) => None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const GENEROUS_BUDGET: Duration = Duration::from_secs(5);

    #[tokio::test]
    async fn a_zero_exit_is_reported() {
        let code = ShellReadyProbe.probe("exit 0", GENEROUS_BUDGET).await;
        assert_eq!(code, Some(0));
    }

    #[tokio::test]
    async fn a_nonzero_exit_is_reported() {
        let code = ShellReadyProbe.probe("exit 7", GENEROUS_BUDGET).await;
        assert_eq!(code, Some(7));
    }

    #[tokio::test]
    async fn a_command_that_outlives_its_budget_is_none() {
        let code = ShellReadyProbe
            .probe("sleep 5", Duration::from_millis(50))
            .await;
        assert_eq!(code, None);
    }

    #[tokio::test]
    async fn stdout_and_stderr_never_reach_the_caller() {
        // Also proves a chatty ready_cmd cannot deadlock on a full pipe:
        // both streams are `/dev/null`, never captured.
        let code = ShellReadyProbe
            .probe("echo out; echo err >&2; exit 3", GENEROUS_BUDGET)
            .await;
        assert_eq!(code, Some(3));
    }
}
