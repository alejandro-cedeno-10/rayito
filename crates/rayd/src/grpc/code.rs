//! `CodeService` over `CodeManager`: proto <-> domain conversion, the gRPC
//! status table of design D10 for everything that fails before the first
//! message (plus the M7 language rows: a known language the image does not
//! ship is `UNIMPLEMENTED`, `language` next to `context_id` or per-execution
//! `envs` on a non-Python context are `INVALID_ARGUMENT`), the 5 s keepalive
//! on `Execute` and `Reattach`, and the suspend close with a trailing
//! `UNAVAILABLE suspending` (design D7: no `ExecutionEnd`, so
//! "`ExecutionError` is never terminal" stays true), or
//! `FAILED_PRECONDITION sandbox_timeout` at the logical deadline
//! (ADR-011). Error messages and log
//! lines carry ids, counts, language names and mime type names, never code,
//! output, envs, cwd or tracebacks.

use std::collections::BTreeMap;
use std::sync::Arc;
use std::time::Duration;

use rayd_core::code::{
    CodeError, ContextInfo as DomainContextInfo, CreateContextInput, EXECUTE_KEEPALIVE_INTERVAL,
    ExecuteOutput, ExecutionErrorInfo, ResultBundle,
};
use rayito_proto::v1::code_service_server::CodeService;
use rayito_proto::v1::{
    ContextInfo, CreateContextRequest, CreateContextResponse, DestroyContextRequest,
    DestroyContextResponse, ExecuteEvent, ExecuteRequest, ExecutionEnd, ExecutionError,
    ExecutionResult, ExecutionStarted, KeepAlive, ListContextsRequest, ListContextsResponse,
    OutputChunk, ReattachRequest, RestartContextRequest, RestartContextResponse, execute_event,
};
use tonic::codegen::BoxStream;
use tonic::{Request, Response, Status};

use super::keepalive::KeepAliveStream;
use crate::code::{CodeManager, ExecuteInput, ExecutionSubscriberStream};
use crate::lifecycle::{SuspendClose, SuspendSignal, SuspendableStream};
use crate::transfer::TransferBarrier;

pub struct CodeGrpc {
    manager: Arc<CodeManager>,
    suspend: Arc<SuspendSignal>,
    keepalive_interval: Duration,
    barrier: TransferBarrier,
}

impl CodeGrpc {
    #[must_use]
    pub fn new(manager: Arc<CodeManager>, suspend: Arc<SuspendSignal>) -> Self {
        Self::with_keepalive_interval(manager, suspend, EXECUTE_KEEPALIVE_INTERVAL)
    }

    #[must_use]
    pub fn with_keepalive_interval(
        manager: Arc<CodeManager>,
        suspend: Arc<SuspendSignal>,
        interval: Duration,
    ) -> Self {
        Self {
            manager,
            suspend,
            keepalive_interval: interval,
            barrier: TransferBarrier::disabled(),
        }
    }

    /// The read-after-upload barrier consulted before every cell
    /// (ADR-010); disabled unless a transfer manager is wired.
    #[must_use]
    pub fn with_barrier(mut self, barrier: TransferBarrier) -> Self {
        self.barrier = barrier;
        self
    }

    fn wrap(&self, stream: ExecutionSubscriberStream) -> BoxStream<ExecuteEvent> {
        let closing = SuspendableStream::new(
            stream,
            self.suspend.subscribe(),
            |output| Ok(to_proto(output)),
            |_| SuspendClose::Status,
        );
        Box::pin(KeepAliveStream::new(
            closing,
            self.keepalive_interval,
            || Ok(keepalive_event()),
        ))
    }
}

#[tonic::async_trait]
impl CodeService for CodeGrpc {
    type ExecuteStream = BoxStream<ExecuteEvent>;
    type ReattachStream = BoxStream<ExecuteEvent>;

    async fn create_context(
        &self,
        request: Request<CreateContextRequest>,
    ) -> Result<Response<CreateContextResponse>, Status> {
        let request = request.into_inner();
        let info = self
            .manager
            .create_context(CreateContextInput {
                language: request.language,
                cwd: request.cwd,
                envs: request.envs.into_iter().collect(),
            })
            .await
            .map_err(|error| rejected("CreateContext", &error))?;
        Ok(Response::new(CreateContextResponse {
            context_id: info.context_id.as_str().to_owned(),
        }))
    }

    async fn execute(
        &self,
        request: Request<ExecuteRequest>,
    ) -> Result<Response<Self::ExecuteStream>, Status> {
        let request = request.into_inner();
        self.barrier.before_workload("Execute").await;
        let stream = self
            .manager
            .execute(ExecuteInput {
                context_id: request.context_id,
                language: request.language,
                code: request.code,
                timeout_ms: request.timeout_ms,
                envs: request.envs.into_iter().collect::<BTreeMap<_, _>>(),
            })
            .await
            .map_err(|error| rejected("Execute", &error))?;
        Ok(Response::new(self.wrap(stream)))
    }

    async fn reattach(
        &self,
        request: Request<ReattachRequest>,
    ) -> Result<Response<Self::ReattachStream>, Status> {
        let request = request.into_inner();
        let context_id = Some(request.context_id.as_str()).filter(|id| !id.is_empty());
        let stream = self
            .manager
            .reattach(context_id, &request.execution_id, request.from_seq)
            .map_err(|error| rejected("Reattach", &error))?;
        Ok(Response::new(self.wrap(stream)))
    }

    async fn list_contexts(
        &self,
        _: Request<ListContextsRequest>,
    ) -> Result<Response<ListContextsResponse>, Status> {
        let contexts = self
            .manager
            .list_contexts()
            .into_iter()
            .map(to_context_info)
            .collect();
        Ok(Response::new(ListContextsResponse { contexts }))
    }

    async fn destroy_context(
        &self,
        request: Request<DestroyContextRequest>,
    ) -> Result<Response<DestroyContextResponse>, Status> {
        self.manager
            .destroy_context(&request.into_inner().context_id)
            .await
            .map_err(|error| rejected("DestroyContext", &error))?;
        Ok(Response::new(DestroyContextResponse {}))
    }

    async fn restart_context(
        &self,
        request: Request<RestartContextRequest>,
    ) -> Result<Response<RestartContextResponse>, Status> {
        self.manager
            .restart_context(&request.into_inner().context_id)
            .await
            .map_err(|error| rejected("RestartContext", &error))?;
        Ok(Response::new(RestartContextResponse {}))
    }
}

fn to_context_info(info: DomainContextInfo) -> ContextInfo {
    ContextInfo {
        context_id: info.context_id.as_str().to_owned(),
        language: info.language.as_str().to_owned(),
        cwd: info.cwd,
    }
}

fn to_proto(output: ExecuteOutput) -> ExecuteEvent {
    let seq = output.seq();
    let event = match output {
        ExecuteOutput::Started {
            execution_id,
            execution_count,
            ..
        } => execute_event::Event::Started(ExecutionStarted {
            execution_id: execution_id.as_str().to_owned(),
            execution_count,
        }),
        ExecuteOutput::Stdout {
            text,
            timestamp_unix_ns,
            ..
        } => execute_event::Event::Stdout(OutputChunk {
            text,
            timestamp_unix_ns,
        }),
        ExecuteOutput::Stderr {
            text,
            timestamp_unix_ns,
            ..
        } => execute_event::Event::Stderr(OutputChunk {
            text,
            timestamp_unix_ns,
        }),
        ExecuteOutput::Result { bundle, .. } => execute_event::Event::Result(to_result(*bundle)),
        ExecuteOutput::Error { error, .. } => execute_event::Event::Error(to_error(error)),
        ExecuteOutput::End {
            execution_count, ..
        } => execute_event::Event::End(ExecutionEnd { execution_count }),
    };
    ExecuteEvent {
        event: Some(event),
        seq,
    }
}

fn to_result(bundle: ResultBundle) -> ExecutionResult {
    ExecutionResult {
        is_main_result: bundle.is_main_result,
        text: bundle.text,
        html: bundle.html,
        markdown: bundle.markdown,
        latex: bundle.latex,
        json: bundle.json,
        javascript: bundle.javascript,
        png: bundle.png,
        jpeg: bundle.jpeg,
        svg: bundle.svg,
        pdf: bundle.pdf,
        chart: bundle.chart,
        data: bundle.data,
        extra: bundle.extra.into_iter().collect(),
    }
}

fn to_error(error: ExecutionErrorInfo) -> ExecutionError {
    ExecutionError {
        name: error.name,
        value: error.value,
        traceback: error.traceback,
    }
}

fn keepalive_event() -> ExecuteEvent {
    ExecuteEvent {
        event: Some(execute_event::Event::Keepalive(KeepAlive {})),
        seq: 0,
    }
}

fn rejected(rpc: &'static str, error: &CodeError) -> Status {
    tracing::warn!(rpc, reason = %error, "code rpc rejected");
    status_for(error)
}

fn status_for(error: &CodeError) -> Status {
    let message = error.to_string();
    match error {
        CodeError::InvalidContextId
        | CodeError::CodeTooLarge { .. }
        | CodeError::InvalidLanguage
        | CodeError::LanguageWithContext
        | CodeError::EnvsPythonOnly
        | CodeError::InvalidCwd(_)
        | CodeError::InvalidEnvs
        | CodeError::InvalidExecutionId
        | CodeError::SidecarRejected(_) => Status::invalid_argument(message),
        CodeError::ContextNotFound | CodeError::ExecutionNotFound => Status::not_found(message),
        CodeError::ReplayOutOfRange { .. } => Status::out_of_range(message),
        CodeError::DefaultContextProtected => Status::failed_precondition(message),
        CodeError::TooManyContexts { .. } | CodeError::TooManySubscribers { .. } => {
            Status::resource_exhausted(message)
        }
        CodeError::KernelNotReady { .. }
        | CodeError::SidecarUnavailable
        | CodeError::NotAcceptingStreams { .. } => Status::unavailable(message),
        CodeError::ContextBusy => Status::aborted(message),
        CodeError::Unsupported | CodeError::LanguageUnavailable(_) => {
            Status::unimplemented(message)
        }
        CodeError::SidecarProtocol(_) | CodeError::Internal(_) => Status::internal(message),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rayd_core::code::{ExecutionId, Language, ProtocolError};
    use rayd_core::lifecycle::HookPhase;
    use tonic::Code;

    #[test]
    fn outputs_map_to_the_proto_oneof_with_their_seq() {
        let started = to_proto(ExecuteOutput::Started {
            seq: 1,
            execution_id: ExecutionId::from_raw("exec-1"),
            execution_count: 3,
        });
        assert_eq!(started.seq, 1);
        match started.event {
            Some(execute_event::Event::Started(inner)) => {
                assert_eq!(inner.execution_id, "exec-1");
                assert_eq!(inner.execution_count, 3);
            }
            other => panic!("unexpected {other:?}"),
        }
        let mime: BTreeMap<String, String> = [
            ("text/plain".to_owned(), "42".to_owned()),
            ("x/y".to_owned(), "z".to_owned()),
        ]
        .into();
        let result = to_proto(ExecuteOutput::Result {
            seq: 2,
            bundle: Box::new(ResultBundle::from_mime(true, mime)),
        });
        match result.event {
            Some(execute_event::Event::Result(inner)) => {
                assert!(inner.is_main_result);
                assert_eq!(inner.text.as_deref(), Some("42"));
                assert_eq!(inner.extra.get("x/y").map(String::as_str), Some("z"));
                assert_eq!(inner.png, None);
            }
            other => panic!("unexpected {other:?}"),
        }
        let end = to_proto(ExecuteOutput::End {
            seq: 9,
            execution_count: 4,
        });
        assert_eq!(end.seq, 9);
        assert!(matches!(
            end.event,
            Some(execute_event::Event::End(ExecutionEnd {
                execution_count: 4
            }))
        ));
        let keepalive = keepalive_event();
        assert_eq!(keepalive.seq, 0);
        assert!(matches!(
            keepalive.event,
            Some(execute_event::Event::Keepalive(_))
        ));
    }

    #[test]
    fn status_table_matches_the_design() {
        let cases = [
            (CodeError::InvalidContextId, Code::InvalidArgument),
            (CodeError::CodeTooLarge { max: 1 }, Code::InvalidArgument),
            (CodeError::InvalidLanguage, Code::InvalidArgument),
            (CodeError::LanguageWithContext, Code::InvalidArgument),
            (CodeError::EnvsPythonOnly, Code::InvalidArgument),
            (
                CodeError::LanguageUnavailable(Language::Bash),
                Code::Unimplemented,
            ),
            (CodeError::InvalidCwd("x".to_owned()), Code::InvalidArgument),
            (CodeError::InvalidEnvs, Code::InvalidArgument),
            (
                CodeError::SidecarRejected("x".to_owned()),
                Code::InvalidArgument,
            ),
            (CodeError::ContextNotFound, Code::NotFound),
            (CodeError::ExecutionNotFound, Code::NotFound),
            (CodeError::InvalidExecutionId, Code::InvalidArgument),
            (
                CodeError::ReplayOutOfRange { oldest: 1, next: 4 },
                Code::OutOfRange,
            ),
            (
                CodeError::TooManySubscribers { max: 8 },
                Code::ResourceExhausted,
            ),
            (CodeError::DefaultContextProtected, Code::FailedPrecondition),
            (
                CodeError::TooManyContexts { max: 8 },
                Code::ResourceExhausted,
            ),
            (
                CodeError::KernelNotReady {
                    reason: "warming".to_owned(),
                },
                Code::Unavailable,
            ),
            (CodeError::SidecarUnavailable, Code::Unavailable),
            (
                CodeError::NotAcceptingStreams {
                    phase: HookPhase::Suspending,
                },
                Code::Unavailable,
            ),
            (CodeError::ContextBusy, Code::Aborted),
            (CodeError::Unsupported, Code::Unimplemented),
            (
                CodeError::SidecarProtocol(ProtocolError::Malformed),
                Code::Internal,
            ),
            (CodeError::Internal("x".to_owned()), Code::Internal),
        ];
        for (error, code) in cases {
            assert_eq!(status_for(&error).code(), code, "{error}");
        }
        assert!(
            status_for(&CodeError::KernelNotReady {
                reason: "no sidecar configured".to_owned()
            })
            .message()
            .starts_with("kernel not ready: ")
        );
        assert_eq!(
            status_for(&CodeError::NotAcceptingStreams {
                phase: HookPhase::Suspending
            })
            .message(),
            "suspending"
        );
        let unavailable = status_for(&CodeError::LanguageUnavailable(Language::Javascript));
        assert!(unavailable.message().contains("rayito-base-poly"));
        assert!(unavailable.message().contains("javascript"));
    }

    #[test]
    fn context_info_carries_the_language_name() {
        let info = to_context_info(DomainContextInfo {
            context_id: rayd_core::code::ContextId::parse("default-bash").unwrap(),
            language: Language::Bash,
            cwd: "/home/user".to_owned(),
        });
        assert_eq!(info.language, "bash");
        assert_eq!(info.context_id, "default-bash");
    }
}
