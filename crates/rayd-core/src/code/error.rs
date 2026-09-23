//! Domain errors of code execution. Their `Display` strings become gRPC
//! status messages and log `reason`s, so none of them quotes code, envs,
//! cwd, output or a context id.

use thiserror::Error;

use super::language::Language;
use super::protocol::ProtocolError;
use crate::lifecycle::HookPhase;

#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum CodeError {
    #[error("context_id must be 1-64 characters of [A-Za-z0-9_-]")]
    InvalidContextId,
    #[error("code exceeds {max} bytes")]
    CodeTooLarge { max: usize },
    #[error("language must be one of python, bash, javascript, typescript")]
    InvalidLanguage,
    #[error("language {0} is not installed in this image; use rayito-base-poly")]
    LanguageUnavailable(Language),
    #[error("language cannot be combined with context_id")]
    LanguageWithContext,
    #[error(
        "envs per execution are only supported on python contexts; pass envs to create_code_context instead"
    )]
    EnvsPythonOnly,
    #[error("cwd {0}")]
    InvalidCwd(String),
    #[error("envs keys must be non-empty without `=` or NUL and values without NUL")]
    InvalidEnvs,
    #[error("context not found")]
    ContextNotFound,
    #[error("execution not found")]
    ExecutionNotFound,
    #[error("execution_id must be an exec id of 16 hex digits")]
    InvalidExecutionId,
    #[error("from_seq out of range: oldest retained {oldest}, next {next}")]
    ReplayOutOfRange { oldest: u64, next: u64 },
    #[error("execution already has {max} subscribers")]
    TooManySubscribers { max: usize },
    #[error("the default context cannot be destroyed; use RestartContext")]
    DefaultContextProtected,
    #[error("context limit reached ({max}); destroy one first")]
    TooManyContexts { max: usize },
    #[error("kernel not ready: {reason}")]
    KernelNotReady { reason: String },
    #[error("kernel not ready: sidecar unavailable")]
    SidecarUnavailable,
    #[error("context is being restarted; retry")]
    ContextBusy,
    #[error("{0}")]
    SidecarRejected(String),
    #[error("{phase}")]
    NotAcceptingStreams { phase: HookPhase },
    #[error(transparent)]
    SidecarProtocol(ProtocolError),
    #[error("code execution is not supported on this platform")]
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
            CodeError::InvalidCwd("is not an absolute path".to_owned()),
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
            "language javascript is not installed in this image; use rayito-base-poly"
        );
        assert_eq!(
            CodeError::InvalidLanguage.to_string(),
            "language must be one of python, bash, javascript, typescript"
        );
        let typescript = CodeError::LanguageUnavailable(Language::Typescript).to_string();
        assert!(typescript.contains("typescript"), "{typescript}");
        assert!(typescript.contains("rayito-base-poly"), "{typescript}");
    }
}
