//! Domain errors of the PTY module. Their `Display` strings become gRPC
//! status messages and log `reason`s, so none of them quotes the shell's
//! arguments, an environment value, a working directory or terminal bytes.

use thiserror::Error;

use crate::process::{Pid, ProcessError};

#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum PtyError {
    #[error("size {cols}x{rows} is outside 1..={max}")]
    InvalidSize { cols: u32, rows: u32, max: u32 },
    #[error("shell must be an absolute path")]
    InvalidShell,
    #[error("pid {pid} is not a PTY")]
    NotAPty { pid: Pid },
    #[error("pty devices unavailable")]
    NoPtyDevices,
    #[error(transparent)]
    Process(#[from] ProcessError),
    #[error("terminals are not supported on this platform")]
    Unsupported,
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::process::SpawnError;

    #[test]
    fn messages_never_quote_input() {
        let cases = [
            PtyError::InvalidSize {
                cols: 0,
                rows: 5000,
                max: 4096,
            },
            PtyError::InvalidShell,
            PtyError::NotAPty { pid: Pid(4) },
            PtyError::NoPtyDevices,
            PtyError::Process(ProcessError::Spawn(SpawnError::CannotExecute(
                "EACCES".to_owned(),
            ))),
            PtyError::Unsupported,
        ];
        for error in cases {
            let message = error.to_string();
            assert!(!message.contains('/'), "{message}");
            assert!(!message.contains("bash"), "{message}");
        }
        assert_eq!(
            PtyError::InvalidSize {
                cols: 0,
                rows: 5000,
                max: 4096
            }
            .to_string(),
            "size 0x5000 is outside 1..=4096"
        );
        assert_eq!(
            PtyError::NotAPty { pid: Pid(4) }.to_string(),
            "pid 4 is not a PTY"
        );
    }
}
