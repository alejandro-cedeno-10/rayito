//! Domain errors of the PTY module. Their `Display` strings become gRPC
//! status messages and log `reason`s, so none of them quotes the shell's
//! arguments, an environment value, a working directory or terminal bytes.

use thiserror::Error;

use crate::process::{Pid, ProcessError, not_a_pty_message};

#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum PtyError {
    #[error("el tamaño {cols}x{rows} está fuera de 1..={max}")]
    InvalidSize { cols: u32, rows: u32, max: u32 },
    #[error("shell debe ser una ruta absoluta")]
    InvalidShell,
    #[error("{}", not_a_pty_message(*pid))]
    NotAPty { pid: Pid },
    #[error("no hay dispositivos PTY disponibles")]
    NoPtyDevices,
    #[error(transparent)]
    Process(#[from] ProcessError),
    #[error("las terminales no se admiten en esta plataforma")]
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
            "el tamaño 0x5000 está fuera de 1..=4096"
        );
        assert_eq!(
            PtyError::NotAPty { pid: Pid(4) }.to_string(),
            "el pid 4 no es una PTY"
        );
    }
}
