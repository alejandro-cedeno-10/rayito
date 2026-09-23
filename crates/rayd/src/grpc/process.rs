//! `ProcessService` over `ProcessManager`: proto <-> domain conversion, the
//! gRPC status table of design D11 for everything that fails before the
//! first message, the keepalive wrapper on both streams and the suspend
//! close with the `EndEvent{suspending}` terminal (design D7), or
//! `EndEvent{sandbox_timeout}` at the logical deadline (ADR-011). Error
//! messages and log lines carry pids, codes and errno names, never the
//! command.

use std::collections::BTreeMap;
use std::sync::Arc;
use std::time::Duration;

use bytes::Bytes;
use rayd_core::process::{
    EndStatus, Pid, ProcessConfigInfo, ProcessEnd, ProcessError, ProcessEvent as DomainEvent,
    ProcessKind, ProcessSummary, SpawnError, SpawnInput, StdinMode, StreamFailure,
};
use rayito_proto::v1::process_service_server::ProcessService;
use rayito_proto::v1::{
    CloseStdinRequest, CloseStdinResponse, ConnectRequest, DataEvent, EndEvent, KeepAlive,
    ListRequest, ListResponse, ProcessConfig, ProcessEvent, ProcessInfo, SendInputRequest,
    SendInputResponse, SendSignalRequest, SendSignalResponse, StartEvent, StartRequest,
    StreamError, data_event, process_event,
};
use tonic::codegen::BoxStream;
use tonic::{Request, Response, Status};

use super::keepalive::{DEFAULT_KEEPALIVE_INTERVAL, KeepAliveStream};
use crate::lifecycle::{StreamCloseReason, SuspendClose, SuspendSignal, SuspendableStream};
use crate::process::{ProcessManager, Spawner, SubscriberStream};
use crate::transfer::TransferBarrier;

pub struct ProcessGrpc<S: Spawner> {
    manager: Arc<ProcessManager<S>>,
    suspend: Arc<SuspendSignal>,
    keepalive_interval: Duration,
    barrier: TransferBarrier,
}

impl<S: Spawner> ProcessGrpc<S> {
    pub fn new(manager: Arc<ProcessManager<S>>, suspend: Arc<SuspendSignal>) -> Self {
        Self::with_keepalive_interval(manager, suspend, DEFAULT_KEEPALIVE_INTERVAL)
    }

    pub fn with_keepalive_interval(
        manager: Arc<ProcessManager<S>>,
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

    /// The read-after-upload barrier consulted before every spawn
    /// (ADR-010); disabled unless a transfer manager is wired.
    #[must_use]
    pub fn with_barrier(mut self, barrier: TransferBarrier) -> Self {
        self.barrier = barrier;
        self
    }

    fn wrap(&self, stream: SubscriberStream) -> BoxStream<ProcessEvent> {
        let closing = SuspendableStream::new(
            stream,
            self.suspend.subscribe(),
            |event| Ok(to_proto(event)),
            |reason| SuspendClose::Terminal(closing_event(reason)),
        );
        Box::pin(KeepAliveStream::new(
            closing,
            self.keepalive_interval,
            || Ok(keepalive_event()),
        ))
    }
}

#[tonic::async_trait]
impl<S: Spawner> ProcessService for ProcessGrpc<S> {
    type StartStream = BoxStream<ProcessEvent>;
    type ConnectStream = BoxStream<ProcessEvent>;

    async fn start(
        &self,
        request: Request<StartRequest>,
    ) -> Result<Response<Self::StartStream>, Status> {
        let input = spawn_input(request.into_inner());
        self.barrier.before_workload("Start").await;
        let (_, stream) = self
            .manager
            .start(input)
            .await
            .map_err(|error| rejected("Start", &error))?;
        Ok(Response::new(self.wrap(stream)))
    }

    async fn connect(
        &self,
        request: Request<ConnectRequest>,
    ) -> Result<Response<Self::ConnectStream>, Status> {
        let request = request.into_inner();
        let stream = self
            .manager
            .connect(Pid(request.pid), request.from_seq)
            .map_err(|error| rejected("Connect", &error))?;
        Ok(Response::new(self.wrap(stream)))
    }

    async fn send_input(
        &self,
        request: Request<SendInputRequest>,
    ) -> Result<Response<SendInputResponse>, Status> {
        let request = request.into_inner();
        self.manager
            .send_input(Pid(request.pid), Bytes::from(request.data))
            .await
            .map_err(|error| rejected("SendInput", &error))?;
        Ok(Response::new(SendInputResponse {}))
    }

    async fn close_stdin(
        &self,
        request: Request<CloseStdinRequest>,
    ) -> Result<Response<CloseStdinResponse>, Status> {
        self.manager
            .close_stdin(Pid(request.into_inner().pid))
            .await
            .map_err(|error| rejected("CloseStdin", &error))?;
        Ok(Response::new(CloseStdinResponse {}))
    }

    async fn send_signal(
        &self,
        request: Request<SendSignalRequest>,
    ) -> Result<Response<SendSignalResponse>, Status> {
        let request = request.into_inner();
        self.manager
            .send_signal(Pid(request.pid), request.signal)
            .map_err(|error| rejected("SendSignal", &error))?;
        Ok(Response::new(SendSignalResponse {}))
    }

    async fn list(&self, _request: Request<ListRequest>) -> Result<Response<ListResponse>, Status> {
        let processes = self.manager.list().into_iter().map(to_info).collect();
        Ok(Response::new(ListResponse { processes }))
    }
}

fn spawn_input(request: StartRequest) -> SpawnInput {
    SpawnInput {
        config: request.process.map(config_info).unwrap_or_default(),
        user: request
            .user
            .map(|user| user.username)
            .filter(|name| !name.is_empty()),
        timeout: (request.timeout_ms > 0).then(|| Duration::from_millis(request.timeout_ms)),
        stdin: StdinMode::from_flag(request.stdin),
        tag: request.tag,
    }
}

fn config_info(config: ProcessConfig) -> ProcessConfigInfo {
    ProcessConfigInfo {
        cmd: config.cmd,
        args: config.args,
        envs: config.envs.into_iter().collect::<BTreeMap<_, _>>(),
        cwd: config.cwd,
    }
}

fn to_info(summary: ProcessSummary) -> ProcessInfo {
    let kind = match summary.kind {
        ProcessKind::Process => rayito_proto::v1::ProcessKind::Process,
        ProcessKind::Pty => rayito_proto::v1::ProcessKind::Pty,
    };
    ProcessInfo {
        pid: summary.pid.0,
        config: Some(ProcessConfig {
            cmd: summary.config.cmd,
            args: summary.config.args,
            envs: summary.config.envs.into_iter().collect(),
            cwd: summary.config.cwd,
        }),
        tag: summary.tag,
        kind: i32::from(kind),
    }
}

fn to_proto(event: DomainEvent) -> ProcessEvent {
    let event = match event {
        DomainEvent::Started { pid } => process_event::Event::Start(StartEvent { pid: pid.0 }),
        DomainEvent::Output(output) => {
            let payload = output.bytes.to_vec();
            let stream = match output.stream {
                rayd_core::process::OutputStream::Stdout => data_event::Output::Stdout(payload),
                rayd_core::process::OutputStream::Stderr => data_event::Output::Stderr(payload),
            };
            process_event::Event::Data(DataEvent {
                output: Some(stream),
                seq: output.seq,
            })
        }
        DomainEvent::Ended(end) => process_event::Event::End(EndEvent {
            exit_code: end.exit_code,
            exited: end.exited,
            status: end.status.as_str().to_owned(),
            error: end.error.map(|failure| StreamError {
                code: failure.code.to_owned(),
                message: failure.message,
            }),
            signal: end.signal,
        }),
    };
    ProcessEvent { event: Some(event) }
}

fn keepalive_event() -> ProcessEvent {
    ProcessEvent {
        event: Some(process_event::Event::Keepalive(KeepAlive {})),
    }
}

/// The in-stream close of design D7: the process is alive, only the stream
/// ends; `Connect(pid, from_seq)` picks it up after the resume.
fn suspending_event() -> ProcessEvent {
    to_proto(DomainEvent::Ended(ProcessEnd {
        status: EndStatus::Suspending,
        exited: false,
        exit_code: 0,
        signal: None,
        error: Some(StreamFailure {
            code: "suspending",
            message: "sandbox suspending; reconnect with Connect(pid, from_seq)".to_owned(),
        }),
    }))
}

/// The deadline's close is terminal: the SDK raises instead of
/// reconnecting.
fn closing_event(reason: StreamCloseReason) -> ProcessEvent {
    match reason {
        StreamCloseReason::Suspending => suspending_event(),
        StreamCloseReason::SandboxTimeout => {
            to_proto(DomainEvent::Ended(ProcessEnd::sandbox_timeout()))
        }
    }
}

fn rejected(rpc: &'static str, error: &ProcessError) -> Status {
    tracing::warn!(rpc, reason = %error, "process rpc rejected");
    status_for(error)
}

fn status_for(error: &ProcessError) -> Status {
    let message = error.to_string();
    match error {
        ProcessError::EmptyCommand
        | ProcessError::InvalidCwd(_)
        | ProcessError::UnknownUser
        | ProcessError::InvalidSignal(_)
        | ProcessError::Spawn(SpawnError::CannotExecute(_)) => Status::invalid_argument(message),
        ProcessError::RootNotAllowed | ProcessError::PrivilegedAccount => {
            Status::permission_denied(message)
        }
        ProcessError::NotFound { .. } => Status::not_found(message),
        ProcessError::WrongKind { .. }
        | ProcessError::StdinNotOpen { .. }
        | ProcessError::StdinClosed { .. } => Status::failed_precondition(message),
        ProcessError::TooManyProcesses { .. } | ProcessError::TooManySubscribers { .. } => {
            Status::resource_exhausted(message)
        }
        ProcessError::OutOfRange { .. } => Status::out_of_range(message),
        ProcessError::NotAcceptingStreams { .. } => Status::unavailable(message),
        ProcessError::Spawn(SpawnError::Unsupported) => Status::unimplemented(message),
        ProcessError::Spawn(SpawnError::Failed(_))
        | ProcessError::UserLookupFailed(_)
        | ProcessError::Internal { .. } => Status::internal(message),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rayd_core::lifecycle::HookPhase;
    use rayd_core::process::{CwdRejection, OutputEvent, OutputStream};
    use rayito_proto::v1::User;
    use tonic::Code;

    #[test]
    fn start_request_maps_to_spawn_input() {
        let request = StartRequest {
            process: Some(ProcessConfig {
                cmd: "/bin/bash".to_owned(),
                args: vec!["-l".to_owned(), "-c".to_owned(), "true".to_owned()],
                envs: [("A".to_owned(), "1".to_owned())].into(),
                cwd: Some("/tmp".to_owned()),
            }),
            user: Some(User {
                username: String::new(),
            }),
            timeout_ms: 2_000,
            stdin: true,
            tag: Some("m2".to_owned()),
        };
        let input = spawn_input(request);
        assert_eq!(input.config.cmd, "/bin/bash");
        assert_eq!(input.config.envs.get("A").map(String::as_str), Some("1"));
        assert_eq!(input.config.cwd.as_deref(), Some("/tmp"));
        assert_eq!(input.user, None);
        assert_eq!(input.timeout, Some(Duration::from_millis(2_000)));
        assert_eq!(input.stdin, StdinMode::Pipe);
        assert_eq!(input.tag.as_deref(), Some("m2"));
        let bare = spawn_input(StartRequest::default());
        assert_eq!(bare.config.cmd, "");
        assert_eq!(bare.timeout, None);
        assert_eq!(bare.stdin, StdinMode::Null);
    }

    #[test]
    fn events_map_to_the_proto_oneof() {
        let started = to_proto(DomainEvent::Started { pid: Pid(7) });
        assert!(matches!(
            started.event,
            Some(process_event::Event::Start(StartEvent { pid: 7 }))
        ));
        let output = to_proto(DomainEvent::Output(OutputEvent {
            seq: 3,
            stream: OutputStream::Stderr,
            bytes: Bytes::from_static(b"err"),
        }));
        match output.event {
            Some(process_event::Event::Data(data)) => {
                assert_eq!(data.seq, 3);
                assert_eq!(
                    data.output,
                    Some(data_event::Output::Stderr(b"err".to_vec()))
                );
            }
            other => panic!("unexpected {other:?}"),
        }
        let ended = to_proto(DomainEvent::Ended(ProcessEnd::timed_out(9)));
        match ended.event {
            Some(process_event::Event::End(end)) => {
                assert_eq!(end.status, EndStatus::Timeout.as_str());
                assert_eq!(end.exit_code, 137);
                assert!(!end.exited);
                assert_eq!(end.signal, Some(9));
                assert_eq!(end.error.unwrap().code, "deadline_exceeded");
            }
            other => panic!("unexpected {other:?}"),
        }
        match suspending_event().event {
            Some(process_event::Event::End(end)) => {
                assert_eq!(end.status, "suspending");
                assert!(!end.exited);
                assert_eq!(end.exit_code, 0);
                assert_eq!(end.signal, None);
                assert_eq!(end.error.unwrap().code, "suspending");
            }
            other => panic!("unexpected {other:?}"),
        }
    }

    #[test]
    fn each_close_reason_has_its_end_event() {
        assert_eq!(
            closing_event(StreamCloseReason::Suspending),
            suspending_event()
        );
        match closing_event(StreamCloseReason::SandboxTimeout).event {
            Some(process_event::Event::End(end)) => {
                assert_eq!(end.status, "sandbox_timeout");
                assert!(!end.exited);
                assert_eq!(end.exit_code, 0);
                assert_eq!(end.signal, None);
                let error = end.error.unwrap();
                assert_eq!(error.code, "sandbox_timeout");
                assert_eq!(error.message, "sandbox timeout");
            }
            other => panic!("unexpected {other:?}"),
        }
    }

    #[test]
    fn status_table_matches_the_design() {
        let cases = [
            (ProcessError::EmptyCommand, Code::InvalidArgument),
            (
                ProcessError::InvalidCwd(CwdRejection::NotADirectory),
                Code::InvalidArgument,
            ),
            (ProcessError::UnknownUser, Code::InvalidArgument),
            (ProcessError::InvalidSignal(0), Code::InvalidArgument),
            (
                ProcessError::Spawn(SpawnError::CannotExecute("ENOENT".to_owned())),
                Code::InvalidArgument,
            ),
            (ProcessError::RootNotAllowed, Code::PermissionDenied),
            (ProcessError::PrivilegedAccount, Code::PermissionDenied),
            (ProcessError::NotFound { pid: Pid(1) }, Code::NotFound),
            (
                ProcessError::StdinNotOpen { pid: Pid(1) },
                Code::FailedPrecondition,
            ),
            (
                ProcessError::WrongKind {
                    pid: Pid(1),
                    expected: ProcessKind::Process,
                },
                Code::FailedPrecondition,
            ),
            (
                ProcessError::StdinClosed { pid: Pid(1) },
                Code::FailedPrecondition,
            ),
            (
                ProcessError::TooManyProcesses { max: 256 },
                Code::ResourceExhausted,
            ),
            (
                ProcessError::TooManySubscribers {
                    pid: Pid(1),
                    max: 8,
                },
                Code::ResourceExhausted,
            ),
            (
                ProcessError::OutOfRange { oldest: 5, next: 9 },
                Code::OutOfRange,
            ),
            (
                ProcessError::NotAcceptingStreams {
                    phase: HookPhase::Suspending,
                },
                Code::Unavailable,
            ),
            (
                ProcessError::Spawn(SpawnError::Unsupported),
                Code::Unimplemented,
            ),
            (
                ProcessError::Spawn(SpawnError::Failed("EPERM".to_owned())),
                Code::Internal,
            ),
            (
                ProcessError::UserLookupFailed("x".to_owned()),
                Code::Internal,
            ),
        ];
        for (error, code) in cases {
            assert_eq!(status_for(&error).code(), code, "{error}");
        }
        assert_eq!(
            status_for(&ProcessError::OutOfRange { oldest: 5, next: 9 }).message(),
            "from_seq out_of_range"
                .replace("out_of_range", "out of range: oldest retained 5, next 9")
        );
    }
}
