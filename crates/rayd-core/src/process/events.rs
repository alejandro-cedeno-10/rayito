//! What a process stream carries: the start marker, sequenced output chunks
//! and exactly one terminal end, in the five shapes of the contract.

use std::fmt;
use std::time::Duration;

use bytes::Bytes;

use super::Pid;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OutputStream {
    Stdout,
    Stderr,
}

/// One read from a pipe, numbered by the per-process sequence shared by both
/// streams. `seq` starts at 1 so that `Connect{from_seq: 0}` can mean "only
/// new output".
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OutputEvent {
    pub seq: u64,
    pub stream: OutputStream,
    pub bytes: Bytes,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EndStatus {
    Exited,
    Signaled,
    Timeout,
    Suspending,
    OutputTruncated,
}

impl EndStatus {
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Exited => "exited",
            Self::Signaled => "signaled",
            Self::Timeout => "timeout",
            Self::Suspending => "suspending",
            Self::OutputTruncated => "output_truncated",
        }
    }
}

impl fmt::Display for EndStatus {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// The in-stream error of `common.proto` (`StreamError`): a code from the
/// closed set plus a human message.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StreamFailure {
    pub code: &'static str,
    pub message: String,
}

impl StreamFailure {
    pub const DEADLINE_EXCEEDED: &'static str = "deadline_exceeded";
    pub const OUTPUT_TRUNCATED: &'static str = "output_truncated";
    pub const INTERNAL: &'static str = "internal";
}

/// Terminal event of a stream. `exit_code` is `128 + signal` when the process
/// died by a signal, whoever sent it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProcessEnd {
    pub status: EndStatus,
    pub exited: bool,
    pub exit_code: i32,
    pub signal: Option<i32>,
    pub error: Option<StreamFailure>,
}

impl ProcessEnd {
    #[must_use]
    pub fn exited(exit_code: i32) -> Self {
        Self {
            status: EndStatus::Exited,
            exited: true,
            exit_code,
            signal: None,
            error: None,
        }
    }

    #[must_use]
    pub fn signaled(signal: i32) -> Self {
        Self {
            status: EndStatus::Signaled,
            exited: true,
            exit_code: 128 + signal,
            signal: Some(signal),
            error: None,
        }
    }

    /// The server deadline fired: `signal` is the one `rayd` delivered (15,
    /// or 9 after the grace period), whatever the process did afterwards.
    #[must_use]
    pub fn timed_out(signal: i32) -> Self {
        Self {
            status: EndStatus::Timeout,
            exited: false,
            exit_code: 128 + signal,
            signal: Some(signal),
            error: Some(StreamFailure {
                code: StreamFailure::DEADLINE_EXCEEDED,
                message: "timeout_ms expired".to_owned(),
            }),
        }
    }

    /// Only this subscriber is dropped; the process keeps running and
    /// `Connect(pid, from_seq = last_seq + 1)` resumes without a gap.
    #[must_use]
    pub fn output_truncated(last_seq: u64, stall: Duration) -> Self {
        Self {
            status: EndStatus::OutputTruncated,
            exited: false,
            exit_code: 0,
            signal: None,
            error: Some(StreamFailure {
                code: StreamFailure::OUTPUT_TRUNCATED,
                message: format!(
                    "subscriber stalled for {} s at seq {last_seq}",
                    stall.as_secs()
                ),
            }),
        }
    }

    /// `wait(2)` itself failed: the process is gone but its status is unknown.
    #[must_use]
    pub fn wait_failed() -> Self {
        Self {
            status: EndStatus::Exited,
            exited: false,
            exit_code: -1,
            signal: None,
            error: Some(StreamFailure {
                code: StreamFailure::INTERNAL,
                message: "wait failed".to_owned(),
            }),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ProcessEvent {
    Started { pid: Pid },
    Output(OutputEvent),
    Ended(ProcessEnd),
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn signaled_and_timed_out_encode_the_signal_in_the_exit_code() {
        let signaled = ProcessEnd::signaled(9);
        assert_eq!(signaled.exit_code, 137);
        assert!(signaled.exited);
        assert_eq!(signaled.error, None);
        let timed_out = ProcessEnd::timed_out(15);
        assert_eq!(timed_out.exit_code, 143);
        assert!(!timed_out.exited);
        assert_eq!(
            timed_out.error.as_ref().map(|error| error.code),
            Some("deadline_exceeded")
        );
    }

    #[test]
    fn output_truncated_names_the_last_seq_delivered() {
        let end = ProcessEnd::output_truncated(41, Duration::from_secs(30));
        assert_eq!(end.status.as_str(), "output_truncated");
        assert_eq!(
            end.error.unwrap().message,
            "subscriber stalled for 30 s at seq 41"
        );
    }
}
