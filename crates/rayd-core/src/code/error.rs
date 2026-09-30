//! Domain errors of code execution. Their `Display` strings become gRPC
//! status messages and log `reason`s, so none of them quotes code, envs,
//! cwd, output or a context id.

use thiserror::Error;

use super::language::Language;
use super::protocol::ProtocolError;
use crate::lifecycle::HookPhase;
use crate::process::out_of_range_message;
use crate::wire_tokens::KERNEL_NOT_READY_PREFIX;

#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum CodeError {
    #[error("context_id debe tener de 1 a 64 caracteres de [A-Za-z0-9_-]")]
    InvalidContextId,
    #[error("el código supera {max} bytes")]
    CodeTooLarge { max: usize },
    #[error("language debe ser python, bash, javascript o typescript")]
    InvalidLanguage,
    #[error("el lenguaje {0} no está instalado en esta imagen; usa rayito-base-poly")]
    LanguageUnavailable(Language),
    #[error("language no se puede combinar con context_id")]
    LanguageWithContext,
    #[error(
        "los envs por ejecución sólo se admiten en contextos python; pasa envs a create_code_context"
    )]
    EnvsPythonOnly,
    #[error("cwd inválido: {0}")]
    InvalidCwd(String),
    #[error("las claves de envs deben ser no vacías, sin `=` ni NUL, y los valores sin NUL")]
    InvalidEnvs,
    #[error("contexto no encontrado")]
    ContextNotFound,
    #[error("ejecución no encontrada")]
    ExecutionNotFound,
    #[error("execution_id debe ser un id de ejecución de 16 dígitos hex")]
    InvalidExecutionId,
    #[error("{}", out_of_range_message(*oldest, *next))]
    ReplayOutOfRange { oldest: u64, next: u64 },
    #[error("la ejecución ya tiene {max} suscriptores")]
    TooManySubscribers { max: usize },
    #[error("el contexto por defecto no se puede destruir; usa RestartContext")]
    DefaultContextProtected,
    #[error("se alcanzó el límite de contextos ({max}); destruye uno antes")]
    TooManyContexts { max: usize },
    #[error("{KERNEL_NOT_READY_PREFIX}: {reason}")]
    KernelNotReady { reason: String },
    #[error("{KERNEL_NOT_READY_PREFIX}: el sidecar no está disponible")]
    SidecarUnavailable,
    #[error("el contexto se está reiniciando; reintenta")]
    ContextBusy,
    #[error("{0}")]
    SidecarRejected(String),
    #[error("{phase}")]
    NotAcceptingStreams { phase: HookPhase },
    #[error(transparent)]
    SidecarProtocol(ProtocolError),
    #[error("la ejecución de código no se admite en esta plataforma")]
    Unsupported,
    #[error("{0}")]
    Internal(String),
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn messages_never_quote_input() {
        let cases = [
            CodeError::InvalidContextId,
            CodeError::CodeTooLarge { max: 1 },
            CodeError::InvalidLanguage,
            CodeError::LanguageUnavailable(Language::Bash),
            CodeError::LanguageWithContext,
            CodeError::EnvsPythonOnly,
            CodeError::InvalidCwd("no es una ruta absoluta".to_owned()),
            CodeError::InvalidEnvs,
            CodeError::ContextNotFound,
            CodeError::ExecutionNotFound,
            CodeError::InvalidExecutionId,
            CodeError::ReplayOutOfRange { oldest: 3, next: 9 },
            CodeError::TooManySubscribers { max: 8 },
            CodeError::DefaultContextProtected,
            CodeError::TooManyContexts { max: 8 },
            CodeError::KernelNotReady {
                reason: "warming".to_owned(),
            },
            CodeError::SidecarUnavailable,
            CodeError::ContextBusy,
            CodeError::SidecarRejected("kernel failed to start".to_owned()),
            CodeError::NotAcceptingStreams {
                phase: HookPhase::Suspending,
            },
            CodeError::SidecarProtocol(ProtocolError::Malformed),
            CodeError::Unsupported,
        ];
        for error in cases {
            let message = error.to_string();
            assert!(!message.contains("ctx-"), "{message}");
            assert!(!message.contains("exec-"), "{message}");
            assert!(!message.contains('{'), "{message}");
        }
        assert!(
            CodeError::KernelNotReady {
                reason: "no sidecar configured".to_owned()
            }
            .to_string()
            .starts_with("kernel not ready: ")
        );
        assert!(
            CodeError::SidecarUnavailable
                .to_string()
                .starts_with("kernel not ready: ")
        );
        assert_eq!(
            CodeError::LanguageUnavailable(Language::Javascript).to_string(),
            "el lenguaje javascript no está instalado en esta imagen; usa rayito-base-poly"
        );
        assert_eq!(
            CodeError::InvalidLanguage.to_string(),
            "language debe ser python, bash, javascript o typescript"
        );
        let typescript = CodeError::LanguageUnavailable(Language::Typescript).to_string();
        assert!(typescript.contains("typescript"), "{typescript}");
        assert!(typescript.contains("rayito-base-poly"), "{typescript}");
    }
}
