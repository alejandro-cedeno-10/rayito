//! `PtyService` over `PtyManager`: proto <-> domain conversion, the gRPC
//! status table of design D5 for everything that fails before the first
//! message, the keepalive on both streams and the suspend close with the
//! `exited{suspending}` terminal (`exited{sandbox_timeout}` at the logical
//! deadline, ADR-011). Error messages and log lines carry pids,
//! sizes and errno names, never terminal bytes, the shell's arguments or
//! its environment.

use std::collections::BTreeMap;
use std::sync::Arc;
use std::time::Duration;

use bytes::Bytes;
use rayd_core::process::{
    EndStatus, Pid, ProcessEnd, ProcessError, ProcessEvent as DomainEvent, SpawnError,
    StreamFailure,
};
use rayd_core::pty::{PtyError, PtySpawnInput};
use rayito_proto::v1::pty_service_server::PtyService;
use rayito_proto::v1::{
    ConnectRequest, KeepAlive, KillPtyRequest, KillPtyResponse, PtyExited, PtyServerMessage,
    PtyStart, PtyStarted, ResizeRequest, ResizeResponse, SendInputRequest, SendInputResponse,
    StreamError, pty_server_message,
};
use tonic::codegen::BoxStream;
use tonic::{Request, Response, Status};

use super::keepalive::{DEFAULT_KEEPALIVE_INTERVAL, KeepAliveStream};
use crate::lifecycle::{StreamCloseReason, SuspendClose, SuspendSignal, SuspendableStream};
use crate::process::SubscriberStream;
use crate::pty::{PtyManager, PtySpawner};
use crate::transfer::TransferBarrier;

pub struct PtyGrpc<B: PtySpawner> {
    manager: Arc<PtyManager<B>>,
    suspend: Arc<SuspendSignal>,
    keepalive_interval: Duration,
    barrier: TransferBarrier,
}

impl<B: PtySpawner> PtyGrpc<B> {
    pub fn new(manager: Arc<PtyManager<B>>, suspend: Arc<SuspendSignal>) -> Self {
        Self::with_keepalive_interval(manager, suspend, DEFAULT_KEEPALIVE_INTERVAL)
    }

    pub fn with_keepalive_interval(
        manager: Arc<PtyManager<B>>,
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

    /// The read-after-upload barrier consulted before every terminal
    /// (ADR-010); disabled unless a transfer manager is wired.
    #[must_use]
    pub fn with_barrier(mut self, barrier: TransferBarrier) -> Self {
        self.barrier = barrier;
        self
    }

    fn wrap(&self, stream: SubscriberStream) -> BoxStream<PtyServerMessage> {
        let closing = SuspendableStream::new(
            stream,
            self.suspend.subscribe(),
            |event| Ok(to_proto(event)),
            |reason| SuspendClose::Terminal(closing_message(reason)),
        );
        Box::pin(KeepAliveStream::new(
            closing,
            self.keepalive_interval,
            || Ok(keepalive_message()),
        ))
    }
}

#[tonic::async_trait]
impl<B: PtySpawner> PtyService for PtyGrpc<B> {
    type CreateStream = BoxStream<PtyServerMessage>;
    type ConnectStream = BoxStream<PtyServerMessage>;

    async fn create(
        &self,
        request: Request<PtyStart>,
    ) -> Result<Response<Self::CreateStream>, Status> {
        let input = spawn_input(request.into_inner());
        self.barrier.before_workload("Create").await;
        let (_, stream) = self
            .manager
            .create(input)
            .await
            .map_err(|error| rejected("Create", &error))?;
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

    async fn resize(
        &self,
        request: Request<ResizeRequest>,
    ) -> Result<Response<ResizeResponse>, Status> {
        let request = request.into_inner();
        let (cols, rows) = request.size.map_or((0, 0), |size| (size.cols, size.rows));
        self.manager
            .resize(Pid(request.pid), cols, rows)
            .map_err(|error| rejected("Resize", &error))?;
        Ok(Response::new(ResizeResponse {}))
    }

    async fn kill(
        &self,
        request: Request<KillPtyRequest>,
    ) -> Result<Response<KillPtyResponse>, Status> {
        self.manager
            .kill(Pid(request.into_inner().pid))
            .map_err(|error| rejected("Kill", &error))?;
        Ok(Response::new(KillPtyResponse {}))
    }
}

fn spawn_input(request: PtyStart) -> PtySpawnInput {
    PtySpawnInput {
        size: request.size.map(|size| (size.cols, size.rows)),
        envs: request.envs.into_iter().collect::<BTreeMap<_, _>>(),
        cwd: request.cwd,
        user: request
            .user
            .map(|user| user.username)
            .filter(|name| !name.is_empty()),
        shell: request.shell.filter(|shell| !shell.is_empty()),
        timeout: (request.timeout_ms > 0).then(|| Duration::from_millis(request.timeout_ms)),
    }
}

fn to_proto(event: DomainEvent) -> PtyServerMessage {
    match event {
        DomainEvent::Started { pid } => PtyServerMessage {
            message: Some(pty_server_message::Message::Started(PtyStarted {
                pid: pid.0,
            })),
            seq: 0,
        },
        DomainEvent::Output(output) => PtyServerMessage {
            message: Some(pty_server_message::Message::Data(output.bytes.to_vec())),
            seq: output.seq,
        },
        DomainEvent::Ended(end) => exited_message(&end),
    }
}

fn exited_message(end: &ProcessEnd) -> PtyServerMessage {
    PtyServerMessage {
        message: Some(pty_server_message::Message::Exited(PtyExited {
            exit_code: end.exit_code,
            exited: end.exited,
            status: end.status.as_str().to_owned(),
            error: end.error.as_ref().map(|failure| StreamError {
                code: failure.code.to_owned(),
                message: failure.message.clone(),
            }),
            signal: end.signal,
        })),
        seq: 0,
    }
}

/// The in-stream close of design D7: the terminal is alive, only the
/// stream ends.
fn suspending_message() -> PtyServerMessage {
    exited_message(&ProcessEnd {
        status: EndStatus::Suspending,
        exited: false,
        exit_code: 0,
        signal: None,
        error: Some(StreamFailure {
            code: "suspending",
            message: "sandbox suspending; reconnect with Connect(pid, from_seq)".to_owned(),
        }),
    })
}

fn closing_message(reason: StreamCloseReason) -> PtyServerMessage {
    match reason {
        StreamCloseReason::Suspending => suspending_message(),
        StreamCloseReason::SandboxTimeout => exited_message(&ProcessEnd::sandbox_timeout()),
    }
}

fn keepalive_message() -> PtyServerMessage {
    PtyServerMessage {
        message: Some(pty_server_message::Message::Keepalive(KeepAlive {})),
        seq: 0,
    }
}

fn rejected(rpc: &'static str, error: &PtyError) -> Status {
    tracing::warn!(rpc, reason = %error, "pty rpc rejected");
    status_for(error)
}

fn status_for(error: &PtyError) -> Status {
    let message = error.to_string();
    match error {
        PtyError::InvalidSize { .. } | PtyError::InvalidShell => Status::invalid_argument(message),
        PtyError::NotAPty { .. } | PtyError::NoPtyDevices => Status::failed_precondition(message),
        PtyError::Unsupported => Status::unimplemented(message),
        PtyError::Process(process) => process_status(process, message),
    }
}

fn process_status(error: &ProcessError, message: String) -> Status {
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
    use rayd_core::process::{CwdRejection, OutputEvent, OutputStream, ProcessKind};
    use rayito_proto::v1::{PtySize, User};
    use tonic::Code;

    #[test]
    fn pty_start_maps_to_spawn_input() {
        let request = PtyStart {
            size: Some(PtySize {
                cols: 100,
                rows: 30,
            }),
            envs: [("A".to_owned(), "1".to_owned())].into(),
            cwd: Some("/tmp".to_owned()),
            user: Some(User {
                username: String::new(),
            }),
            shell: Some(String::new()),
            timeout_ms: 1_500,
        };
        let input = spawn_input(request);
        assert_eq!(input.size, Some((100, 30)));
        assert_eq!(input.envs.get("A").map(String::as_str), Some("1"));
        assert_eq!(input.cwd.as_deref(), Some("/tmp"));
        assert_eq!(input.user, None);
        assert_eq!(input.shell, None);
        assert_eq!(input.timeout, Some(Duration::from_millis(1_500)));
        let bare = spawn_input(PtyStart::default());
        assert_eq!(bare.size, None);
        assert_eq!(bare.timeout, None);
    }

    #[test]
    fn events_map_to_the_proto_oneof_with_seq_only_on_data() {
        let started = to_proto(DomainEvent::Started { pid: Pid(7) });
        assert_eq!(started.seq, 0);
        assert!(matches!(
            started.message,
            Some(pty_server_message::Message::Started(PtyStarted { pid: 7 }))
        ));
        let data = to_proto(DomainEvent::Output(OutputEvent {
            seq: 3,
            stream: OutputStream::Stdout,
            bytes: Bytes::from_static(b"hola\r\n"),
        }));
        assert_eq!(data.seq, 3);
        assert_eq!(
            data.message,
            Some(pty_server_message::Message::Data(b"hola\r\n".to_vec()))
        );
        let exited = to_proto(DomainEvent::Ended(ProcessEnd::signaled(9)));
        assert_eq!(exited.seq, 0);
        match exited.message {
            Some(pty_server_message::Message::Exited(end)) => {
                assert_eq!(end.status, "signaled");
                assert_eq!(end.exit_code, 137);
                assert_eq!(end.signal, Some(9));
                assert!(end.exited);
            }
            other => panic!("unexpected {other:?}"),
        }
        let suspending = suspending_message();
        match suspending.message {
            Some(pty_server_message::Message::Exited(end)) => {
                assert_eq!(end.status, "suspending");
                assert!(!end.exited);
                assert_eq!(end.exit_code, 0);
                assert_eq!(end.error.unwrap().code, "suspending");
            }
            other => panic!("unexpected {other:?}"),
        }
        assert_eq!(
            closing_message(StreamCloseReason::Suspending),
            suspending_message()
        );
        match closing_message(StreamCloseReason::SandboxTimeout).message {
            Some(pty_server_message::Message::Exited(end)) => {
                assert_eq!(end.status, "sandbox_timeout");
                assert!(!end.exited);
                assert_eq!(end.exit_code, 0);
                assert_eq!(end.error.unwrap().code, "sandbox_timeout");
            }
            other => panic!("unexpected {other:?}"),
        }
        assert!(matches!(
            keepalive_message().message,
            Some(pty_server_message::Message::Keepalive(_))
        ));
    }

    fn every_status_case() -> Vec<(PtyError, Code)> {
        vec![
            (
                PtyError::InvalidSize {
                    cols: 0,
                    rows: 0,
                    max: 4096,
                },
                Code::InvalidArgument,
            ),
            (PtyError::InvalidShell, Code::InvalidArgument),
            (
                PtyError::Process(ProcessError::InvalidCwd(CwdRejection::NotADirectory)),
                Code::InvalidArgument,
            ),
            (
                PtyError::Process(ProcessError::UnknownUser),
                Code::InvalidArgument,
            ),
            (
                PtyError::Process(ProcessError::Spawn(SpawnError::CannotExecute(
                    "EACCES".to_owned(),
                ))),
                Code::InvalidArgument,
            ),
            (
                PtyError::Process(ProcessError::RootNotAllowed),
                Code::PermissionDenied,
            ),
            (
                PtyError::Process(ProcessError::PrivilegedAccount),
                Code::PermissionDenied,
            ),
            (
                PtyError::Process(ProcessError::NotFound { pid: Pid(1) }),
                Code::NotFound,
            ),
            (PtyError::NotAPty { pid: Pid(1) }, Code::FailedPrecondition),
            (
                PtyError::Process(ProcessError::WrongKind {
                    pid: Pid(1),
                    expected: ProcessKind::Pty,
                }),
                Code::FailedPrecondition,
            ),
            (PtyError::NoPtyDevices, Code::FailedPrecondition),
            (
                PtyError::Process(ProcessError::OutOfRange { oldest: 1, next: 2 }),
                Code::OutOfRange,
            ),
            (
                PtyError::Process(ProcessError::TooManyProcesses { max: 256 }),
                Code::ResourceExhausted,
            ),
            (
                PtyError::Process(ProcessError::TooManySubscribers {
                    pid: Pid(1),
                    max: 8,
                }),
                Code::ResourceExhausted,
            ),
            (
                PtyError::Process(ProcessError::NotAcceptingStreams {
                    phase: HookPhase::Suspending,
                }),
                Code::Unavailable,
            ),
            (
                PtyError::Process(ProcessError::Spawn(SpawnError::Failed("ENOSPC".to_owned()))),
                Code::Internal,
            ),
            (
                PtyError::Process(ProcessError::Internal {
                    operation: "resize",
                    reason: "x".to_owned(),
                }),
                Code::Internal,
            ),
            (PtyError::Unsupported, Code::Unimplemented),
            (
                PtyError::Process(ProcessError::Spawn(SpawnError::Unsupported)),
                Code::Unimplemented,
            ),
        ]
    }

    #[test]
    fn status_table_matches_the_design() {
        for (error, code) in every_status_case() {
            assert_eq!(status_for(&error).code(), code, "{error}");
        }
        assert_eq!(
            status_for(&PtyError::Process(ProcessError::NotAcceptingStreams {
                phase: HookPhase::Suspending
            }))
            .message(),
            "suspending"
        );
        assert_eq!(
            status_for(&PtyError::Process(ProcessError::WrongKind {
                pid: Pid(4),
                expected: ProcessKind::Pty
            }))
            .message(),
            "pid 4 is not a PTY"
        );
    }
}
