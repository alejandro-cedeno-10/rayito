//! Domain errors of the process module. Their `Display` strings are what the
//! gRPC adapter sends to clients and writes to logs, so none of them ever
//! quotes a command, an argument, an environment value, a path or a tag.

use std::fmt;

use thiserror::Error;

use super::Pid;
use super::ports::SpawnError;
use super::registry::ProcessKind;
use crate::lifecycle::HookPhase;

#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum ProcessError {
    #[error("max {max} live processes")]
    TooManyProcesses { max: usize },
    #[error("pid {pid} already has {max} subscribers")]
    TooManySubscribers { pid: Pid, max: usize },
    #[error("pid {pid} not found")]
    NotFound { pid: Pid },
    /// The pid exists but belongs to the other service: a PTY answers only
    /// `PtyService` and a plain process only `ProcessService`.
    #[error("{}", wrong_kind_message(*pid, *expected))]
    WrongKind { pid: Pid, expected: ProcessKind },
    #[error("pid {pid} was started without stdin")]
    StdinNotOpen { pid: Pid },
    #[error("stdin of pid {pid} is closed")]
    StdinClosed { pid: Pid },
    #[error("running as root is not allowed by this image")]
    RootNotAllowed,
    #[error(
        "only unprivileged accounts of this image may run code (uid and gid >= 1000, never in group 0)"
    )]
    PrivilegedAccount,
    #[error("unknown user")]
    UnknownUser,
    #[error("user lookup failed: {0}")]
    UserLookupFailed(String),
    #[error("cwd {0}")]
    InvalidCwd(CwdRejection),
    #[error("cmd is empty")]
    EmptyCommand,
    #[error("signal {0} is outside 1..=64")]
    InvalidSignal(i32),
    #[error("from_seq out of range: oldest retained {oldest}, next {next}")]
    OutOfRange { oldest: u64, next: u64 },
    #[error("{phase}")]
    NotAcceptingStreams { phase: HookPhase },
    #[error(transparent)]
    Spawn(#[from] SpawnError),
    #[error("{operation} failed: {reason}")]
    Internal {
        operation: &'static str,
        reason: String,
    },
}

fn wrong_kind_message(pid: Pid, expected: ProcessKind) -> String {
    match expected {
        ProcessKind::Pty => format!("pid {pid} is not a PTY"),
        ProcessKind::Process => format!("pid {pid} is a PTY; use PtyService"),
    }
}

/// Why a working directory was refused; never carries the path itself.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CwdRejection {
    NotAbsolute,
    ContainsNul,
    NotADirectory,
}

impl fmt::Display for CwdRejection {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::NotAbsolute => "is not an absolute path",
            Self::ContainsNul => "contains a NUL byte",
            Self::NotADirectory => "is not a directory",
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn messages_never_quote_input() {
        assert_eq!(
            ProcessError::InvalidCwd(CwdRejection::NotADirectory).to_string(),
            "cwd is not a directory"
        );
        assert_eq!(ProcessError::UnknownUser.to_string(), "unknown user");
        assert_eq!(ProcessError::EmptyCommand.to_string(), "cmd is empty");
        assert_eq!(
            ProcessError::NotAcceptingStreams {
                phase: HookPhase::Suspending
            }
            .to_string(),
            "suspending"
        );
        assert_eq!(
            ProcessError::Spawn(SpawnError::CannotExecute("ENOENT".to_owned())).to_string(),
            "cannot execute: ENOENT"
        );
    }

    #[test]
    fn wrong_kind_names_the_expected_service() {
        assert_eq!(
            ProcessError::WrongKind {
                pid: Pid(7),
                expected: ProcessKind::Pty
            }
            .to_string(),
            "pid 7 is not a PTY"
        );
        assert_eq!(
            ProcessError::WrongKind {
                pid: Pid(7),
                expected: ProcessKind::Process
            }
            .to_string(),
            "pid 7 is a PTY; use PtyService"
        );
    }
}
