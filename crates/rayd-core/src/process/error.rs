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
    #[error("como máximo {max} procesos vivos")]
    TooManyProcesses { max: usize },
    #[error("el pid {pid} ya tiene {max} suscriptores")]
    TooManySubscribers { pid: Pid, max: usize },
    #[error("no se encontró el pid {pid}")]
    NotFound { pid: Pid },
    /// The pid exists but belongs to the other service: a PTY answers only
    /// `PtyService` and a plain process only `ProcessService`.
    #[error("{}", wrong_kind_message(*pid, *expected))]
    WrongKind { pid: Pid, expected: ProcessKind },
    #[error("el pid {pid} se arrancó sin stdin")]
    StdinNotOpen { pid: Pid },
    #[error("la stdin del pid {pid} está cerrada")]
    StdinClosed { pid: Pid },
    #[error("esta imagen no permite ejecutar como root")]
    RootNotAllowed,
    #[error(
        "sólo las cuentas sin privilegios de esta imagen pueden ejecutar código (uid y gid >= 1000, nunca en el grupo 0)"
    )]
    PrivilegedAccount,
    #[error("usuario desconocido")]
    UnknownUser,
    #[error("falló la búsqueda del usuario: {0}")]
    UserLookupFailed(String),
    #[error("cwd inválido: {0}")]
    InvalidCwd(CwdRejection),
    #[error("cmd está vacío")]
    EmptyCommand,
    #[error("la señal {0} está fuera de 1..=64")]
    InvalidSignal(i32),
    #[error("{}", out_of_range_message(*oldest, *next))]
    OutOfRange { oldest: u64, next: u64 },
    #[error("{phase}")]
    NotAcceptingStreams { phase: HookPhase },
    #[error(transparent)]
    Spawn(#[from] SpawnError),
    #[error("falló {operation}: {reason}")]
    Internal {
        operation: &'static str,
        reason: String,
    },
}

fn wrong_kind_message(pid: Pid, expected: ProcessKind) -> String {
    match expected {
        ProcessKind::Pty => not_a_pty_message(pid),
        ProcessKind::Process => format!("el pid {pid} es una PTY; usa PtyService"),
    }
}

/// Shared by the PTY errors: a plain process asked for as a terminal.
#[must_use]
pub fn not_a_pty_message(pid: Pid) -> String {
    format!("el pid {pid} no es una PTY")
}

/// A replay start the output ring no longer (or not yet) holds; shared by
/// process and code executions.
#[must_use]
pub fn out_of_range_message(oldest: u64, next: u64) -> String {
    format!("from_seq fuera de rango: el más antiguo retenido es {oldest} y el siguiente {next}")
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
            Self::NotAbsolute => "no es una ruta absoluta",
            Self::ContainsNul => "contiene un byte NUL",
            Self::NotADirectory => "no es un directorio",
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
            "cwd inválido: no es un directorio"
        );
        assert_eq!(ProcessError::UnknownUser.to_string(), "usuario desconocido");
        assert_eq!(ProcessError::EmptyCommand.to_string(), "cmd está vacío");
        assert_eq!(
            ProcessError::NotAcceptingStreams {
                phase: HookPhase::Suspending
            }
            .to_string(),
            "suspending"
        );
        assert_eq!(
            ProcessError::Spawn(SpawnError::CannotExecute("ENOENT".to_owned())).to_string(),
            "no se puede ejecutar: ENOENT"
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
            "el pid 7 no es una PTY"
        );
        assert_eq!(
            ProcessError::WrongKind {
                pid: Pid(7),
                expected: ProcessKind::Process
            }
            .to_string(),
            "el pid 7 es una PTY; usa PtyService"
        );
    }
}
