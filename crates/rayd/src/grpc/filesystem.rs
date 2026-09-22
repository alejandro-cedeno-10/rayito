//! `FilesystemService` over `FilesystemManager`: proto <-> domain
//! conversion, the gRPC status table of design D10, the keepalive wrapper
//! on `WatchDir` and the suspend close (design D7): `Read` and `WatchDir`
//! end with `UNAVAILABLE suspending`, a `Write` in flight is aborted (its
//! temporary removed) and answered the same way. `Checkpoint` and
//! `Restore` are delegated to `PersistenceGrpc` (ADR-009). Error messages
//! and log lines carry codes, counts and errno names, never a path, a
//! name, a target or file bytes.

use std::sync::Arc;
use std::time::Duration;

use rayd_core::filesystem::{
    Entry, EntryKind, FilesystemError, WatchEvent, WatchEventKind, WriteMessage,
};
use rayito_proto::v1::filesystem_service_server::FilesystemService;
use rayito_proto::v1::{
    CheckpointEvent, CheckpointRequest, EntryInfo, FileType, FilesystemEvent, FilesystemEventType,
    KeepAlive, ListDirRequest, ListDirResponse, MakeDirRequest, MakeDirResponse, MoveRequest,
    MoveResponse, ReadRequest, ReadResponse, RemoveRequest, RemoveResponse, RestoreEvent,
    RestoreRequest, StatRequest, StatResponse, User, WatchDirRequest, WatchDirResponse,
    WatchStarted, WriteRequest, WriteResponse, watch_dir_response,
};
use tokio_stream::StreamExt;
use tonic::codegen::BoxStream;
use tonic::{Code, Request, Response, Status, Streaming};

use super::client_abort::ClientAbort;
use super::keepalive::{DEFAULT_KEEPALIVE_INTERVAL, KeepAliveStream};
use super::persistence::PersistenceGrpc;
use crate::filesystem::write::drain_after_error;
use crate::filesystem::{FilesystemManager, WatchItem, WriteFailure, WriteMessageWithChunk};
use crate::lifecycle::suspend::suspending_status;
use crate::lifecycle::{SuspendClose, SuspendSignal, SuspendableStream};
use crate::persistence::{PersistenceBackend, UnavailablePersistence};

/// A silent watch stream sends a keepalive at this cadence so bytes keep
/// crossing the proxy (`AWS_API_NOTES.md` Q15).
pub const DEFAULT_WATCH_KEEPALIVE_INTERVAL: Duration = Duration::from_secs(50);
/// How long a `Write` aborted by `/suspend` keeps draining the client's
/// body before the status goes out.
pub const WRITE_SUSPEND_DRAIN_TIMEOUT: Duration = Duration::from_millis(200);

pub struct FilesystemGrpc {
    manager: Arc<FilesystemManager>,
    suspend: Arc<SuspendSignal>,
    watch_keepalive_interval: Duration,
    persistence: PersistenceGrpc,
}

impl FilesystemGrpc {
    #[must_use]
    pub fn new(manager: Arc<FilesystemManager>, suspend: Arc<SuspendSignal>) -> Self {
        Self::with_keepalive_interval(manager, suspend, DEFAULT_WATCH_KEEPALIVE_INTERVAL)
    }

    /// Without `with_persistence`, `Checkpoint`/`Restore` answer
    /// `UNIMPLEMENTED` (no store wired).
    #[must_use]
    pub fn with_keepalive_interval(
        manager: Arc<FilesystemManager>,
        suspend: Arc<SuspendSignal>,
        interval: Duration,
    ) -> Self {
        let persistence = PersistenceGrpc::new(
            Arc::new(UnavailablePersistence),
            suspend.clone(),
            DEFAULT_KEEPALIVE_INTERVAL,
        );
        Self {
            manager,
            suspend,
            watch_keepalive_interval: interval,
            persistence,
        }
    }

    /// The backend behind `Checkpoint`/`Restore` and the keepalive cadence
    /// of their streams.
    #[must_use]
    pub fn with_persistence(
        mut self,
        backend: Arc<dyn PersistenceBackend>,
        keepalive_interval: Duration,
    ) -> Self {
        self.persistence = PersistenceGrpc::new(backend, self.suspend.clone(), keepalive_interval);
        self
    }
}

#[tonic::async_trait]
impl FilesystemService for FilesystemGrpc {
    type ReadStream = BoxStream<ReadResponse>;
    type WatchDirStream = BoxStream<WatchDirResponse>;
    type CheckpointStream = BoxStream<CheckpointEvent>;
    type RestoreStream = BoxStream<RestoreEvent>;

    async fn checkpoint(
        &self,
        request: Request<CheckpointRequest>,
    ) -> Result<Response<Self::CheckpointStream>, Status> {
        self.persistence.checkpoint(request).await
    }

    async fn restore(
        &self,
        request: Request<RestoreRequest>,
    ) -> Result<Response<Self::RestoreStream>, Status> {
        self.persistence.restore(request).await
    }

    async fn read(
        &self,
        request: Request<ReadRequest>,
    ) -> Result<Response<Self::ReadStream>, Status> {
        let request = request.into_inner();
        let stream = self
            .manager
            .read(username(request.user), request.path)
            .await
            .map_err(|error| rejected("Read", &error))?;
        let responses = SuspendableStream::new(
            stream,
            self.suspend.subscribe(),
            |item| {
                item.map(|chunk| ReadResponse { chunk })
                    .map_err(|error| status_for(&error))
            },
            || SuspendClose::Status,
        );
        Ok(Response::new(Box::pin(responses)))
    }

    async fn write(
        &self,
        request: Request<Streaming<WriteRequest>>,
    ) -> Result<Response<WriteResponse>, Status> {
        let abort = request
            .extensions()
            .get::<ClientAbort>()
            .cloned()
            .unwrap_or_default();
        let mut messages = request.into_inner().map(|item| item.map(write_message));
        let mut watch = self.suspend.subscribe();
        let outcome = tokio::select! {
            outcome = self.manager.write(&mut messages, move || abort.aborted()) => outcome,
            () = watch.suspended() => {
                tracing::info!(rpc = "Write", "write aborted by suspend");
                drain_after_error(&mut messages, usize::MAX, WRITE_SUSPEND_DRAIN_TIMEOUT).await;
                return Err(suspending_status());
            }
        };
        let entries = outcome.map_err(|failure| match failure {
            WriteFailure::Filesystem(error) => rejected("Write", &error),
            WriteFailure::Client(status) => status,
            WriteFailure::Aborted => Status::cancelled("upload cancelled by the client"),
        })?;
        Ok(Response::new(WriteResponse {
            entries: entries.into_iter().map(to_entry_info).collect(),
        }))
    }

    async fn stat(&self, request: Request<StatRequest>) -> Result<Response<StatResponse>, Status> {
        let request = request.into_inner();
        let entry = self
            .manager
            .stat(username(request.user), request.path)
            .await
            .map_err(|error| rejected("Stat", &error))?;
        Ok(Response::new(StatResponse {
            entry: Some(to_entry_info(entry)),
        }))
    }

    async fn list_dir(
        &self,
        request: Request<ListDirRequest>,
    ) -> Result<Response<ListDirResponse>, Status> {
        let request = request.into_inner();
        let entries = self
            .manager
            .list_dir(username(request.user), request.path, request.depth)
            .await
            .map_err(|error| rejected("ListDir", &error))?;
        Ok(Response::new(ListDirResponse {
            entries: entries.into_iter().map(to_entry_info).collect(),
        }))
    }

    async fn make_dir(
        &self,
        request: Request<MakeDirRequest>,
    ) -> Result<Response<MakeDirResponse>, Status> {
        let request = request.into_inner();
        let entry = self
            .manager
            .make_dir(username(request.user), request.path)
            .await
            .map_err(|error| rejected("MakeDir", &error))?;
        Ok(Response::new(MakeDirResponse {
            entry: Some(to_entry_info(entry)),
        }))
    }

    async fn r#move(
        &self,
        request: Request<MoveRequest>,
    ) -> Result<Response<MoveResponse>, Status> {
        let request = request.into_inner();
        let entry = self
            .manager
            .rename(username(request.user), request.source, request.destination)
            .await
            .map_err(|error| rejected("Move", &error))?;
        Ok(Response::new(MoveResponse {
            entry: Some(to_entry_info(entry)),
        }))
    }

    async fn remove(
        &self,
        request: Request<RemoveRequest>,
    ) -> Result<Response<RemoveResponse>, Status> {
        let request = request.into_inner();
        self.manager
            .remove(username(request.user), request.path, request.recursive)
            .await
            .map_err(|error| rejected("Remove", &error))?;
        Ok(Response::new(RemoveResponse {}))
    }

    async fn watch_dir(
        &self,
        request: Request<WatchDirRequest>,
    ) -> Result<Response<Self::WatchDirStream>, Status> {
        let request = request.into_inner();
        let stream = self
            .manager
            .watch_dir(
                username(request.user),
                request.path,
                request.recursive,
                request.include_entry,
            )
            .await
            .map_err(|error| rejected("WatchDir", &error))?;
        let responses = SuspendableStream::new(
            stream,
            self.suspend.subscribe(),
            |item| match item {
                Ok(WatchItem::Started) => Ok(started_response()),
                Ok(WatchItem::Event { event, entry }) => Ok(event_response(event, entry)),
                Err(error) => Err(status_for(&error)),
            },
            || SuspendClose::Status,
        );
        Ok(Response::new(Box::pin(KeepAliveStream::new(
            responses,
            self.watch_keepalive_interval,
            || Ok(keepalive_response()),
        ))))
    }
}

fn username(user: Option<User>) -> Option<String> {
    user.map(|user| user.username)
        .filter(|name| !name.is_empty())
}

fn write_message(request: WriteRequest) -> WriteMessageWithChunk {
    WriteMessageWithChunk {
        message: WriteMessage {
            path: request.path,
            user: username(request.user),
            mode: request.mode,
            chunk_len: request.chunk.len(),
        },
        chunk: request.chunk,
    }
}

fn to_entry_info(entry: Entry) -> EntryInfo {
    EntryInfo {
        name: entry.name,
        r#type: i32::from(file_type(entry.kind)),
        path: entry.path,
        size: entry.size,
        mode: entry.mode,
        permissions: entry.permissions,
        owner: entry.owner,
        group: entry.group,
        modified_time_unix_ms: entry.modified_ms,
        symlink_target: entry.symlink_target,
    }
}

fn file_type(kind: EntryKind) -> FileType {
    match kind {
        EntryKind::File => FileType::File,
        EntryKind::Directory => FileType::Directory,
        EntryKind::Symlink => FileType::Symlink,
        EntryKind::Other => FileType::Unspecified,
    }
}

fn event_type(kind: WatchEventKind) -> FilesystemEventType {
    match kind {
        WatchEventKind::Create => FilesystemEventType::Create,
        WatchEventKind::Write => FilesystemEventType::Write,
        WatchEventKind::Remove => FilesystemEventType::Remove,
        WatchEventKind::Rename => FilesystemEventType::Rename,
        WatchEventKind::Chmod => FilesystemEventType::Chmod,
    }
}

fn started_response() -> WatchDirResponse {
    WatchDirResponse {
        event: Some(watch_dir_response::Event::Started(WatchStarted {})),
    }
}

fn event_response(event: WatchEvent, entry: Option<Entry>) -> WatchDirResponse {
    WatchDirResponse {
        event: Some(watch_dir_response::Event::Filesystem(FilesystemEvent {
            name: event.name,
            r#type: i32::from(event_type(event.kind)),
            entry: entry.map(to_entry_info),
        })),
    }
}

fn keepalive_response() -> WatchDirResponse {
    WatchDirResponse {
        event: Some(watch_dir_response::Event::Keepalive(KeepAlive {})),
    }
}

/// `NOT_FOUND` and `ALREADY_EXISTS` are the normal answers of `exists()`
/// and `make_dir()` polling, so they are logged at debug, the rest at warn.
fn rejected(rpc: &'static str, error: &FilesystemError) -> Status {
    let status = status_for(error);
    let outcome = code_name(status.code());
    match status.code() {
        Code::NotFound | Code::AlreadyExists => {
            tracing::debug!(rpc, outcome, reason = %error, "filesystem rpc rejected");
        }
        _ => tracing::warn!(rpc, outcome, reason = %error, "filesystem rpc rejected"),
    }
    status
}

fn status_for(error: &FilesystemError) -> Status {
    let message = error.to_string();
    match error {
        FilesystemError::InvalidPath(_)
        | FilesystemError::IsADirectory
        | FilesystemError::NotARegularFile
        | FilesystemError::IsSymlink
        | FilesystemError::ChunkTooLarge { .. }
        | FilesystemError::InvalidMode(_)
        | FilesystemError::MissingPath
        | FilesystemError::UserWithoutPath
        | FilesystemError::ModeWithoutPath
        | FilesystemError::NoFiles
        | FilesystemError::NotADirectory
        | FilesystemError::UnknownUser => Status::invalid_argument(message),
        FilesystemError::Denied
        | FilesystemError::PermissionDenied
        | FilesystemError::RootNotAllowed
        | FilesystemError::PrivilegedAccount => Status::permission_denied(message),
        FilesystemError::NotFound | FilesystemError::WatchRootGone => Status::not_found(message),
        FilesystemError::AlreadyExists => Status::already_exists(message),
        FilesystemError::NotEmpty
        | FilesystemError::DestinationConflict
        | FilesystemError::CrossDevice => Status::failed_precondition(message),
        FilesystemError::TooManyEntries { .. }
        | FilesystemError::TooManyWatches { .. }
        | FilesystemError::WatchLimitReached
        | FilesystemError::WatchOverflow
        | FilesystemError::DiskReserve
        | FilesystemError::DiskFull => Status::resource_exhausted(message),
        FilesystemError::NotAcceptingStreams { .. } => Status::unavailable(message),
        FilesystemError::Unsupported => Status::unimplemented(message),
        FilesystemError::Io { .. } | FilesystemError::UserLookupFailed(_) => {
            Status::internal(message)
        }
    }
}

fn code_name(code: Code) -> &'static str {
    match code {
        Code::InvalidArgument => "invalid_argument",
        Code::PermissionDenied => "permission_denied",
        Code::NotFound => "not_found",
        Code::AlreadyExists => "already_exists",
        Code::FailedPrecondition => "failed_precondition",
        Code::ResourceExhausted => "resource_exhausted",
        Code::Unavailable => "unavailable",
        Code::Unimplemented => "unimplemented",
        Code::Internal => "internal",
        _ => "other",
    }
}

#[cfg(test)]
mod tests {
    use rayd_core::filesystem::PathRejection;
    use rayd_core::lifecycle::HookPhase;

    use super::*;

    #[test]
    fn status_table_matches_the_design() {
        let cases = [
            (
                FilesystemError::InvalidPath(PathRejection::ParentReference),
                Code::InvalidArgument,
            ),
            (FilesystemError::IsADirectory, Code::InvalidArgument),
            (FilesystemError::NotARegularFile, Code::InvalidArgument),
            (FilesystemError::IsSymlink, Code::InvalidArgument),
            (
                FilesystemError::ChunkTooLarge { max: 1 },
                Code::InvalidArgument,
            ),
            (FilesystemError::InvalidMode(0o10000), Code::InvalidArgument),
            (FilesystemError::MissingPath, Code::InvalidArgument),
            (FilesystemError::UserWithoutPath, Code::InvalidArgument),
            (FilesystemError::NoFiles, Code::InvalidArgument),
            (FilesystemError::NotADirectory, Code::InvalidArgument),
            (FilesystemError::UnknownUser, Code::InvalidArgument),
            (FilesystemError::Denied, Code::PermissionDenied),
            (FilesystemError::PermissionDenied, Code::PermissionDenied),
            (FilesystemError::RootNotAllowed, Code::PermissionDenied),
            (FilesystemError::PrivilegedAccount, Code::PermissionDenied),
            (FilesystemError::NotFound, Code::NotFound),
            (FilesystemError::WatchRootGone, Code::NotFound),
            (FilesystemError::AlreadyExists, Code::AlreadyExists),
            (FilesystemError::NotEmpty, Code::FailedPrecondition),
            (
                FilesystemError::DestinationConflict,
                Code::FailedPrecondition,
            ),
            (FilesystemError::CrossDevice, Code::FailedPrecondition),
            (
                FilesystemError::TooManyEntries { max: 1 },
                Code::ResourceExhausted,
            ),
            (
                FilesystemError::TooManyWatches { max: 1 },
                Code::ResourceExhausted,
            ),
            (FilesystemError::WatchLimitReached, Code::ResourceExhausted),
            (FilesystemError::WatchOverflow, Code::ResourceExhausted),
            (FilesystemError::DiskReserve, Code::ResourceExhausted),
            (FilesystemError::DiskFull, Code::ResourceExhausted),
            (
                FilesystemError::NotAcceptingStreams {
                    phase: HookPhase::Suspending,
                },
                Code::Unavailable,
            ),
            (FilesystemError::Unsupported, Code::Unimplemented),
            (
                FilesystemError::Io {
                    operation: "read",
                    errno: "EIO".to_owned(),
                },
                Code::Internal,
            ),
            (
                FilesystemError::UserLookupFailed("x".to_owned()),
                Code::Internal,
            ),
        ];
        for (error, code) in cases {
            assert_eq!(status_for(&error).code(), code, "{error}");
        }
        assert_eq!(
            status_for(&FilesystemError::WatchOverflow).message(),
            "watch queue overflowed; re-open the watch"
        );
        assert_eq!(
            status_for(&FilesystemError::DiskReserve).message(),
            "disk_reserve"
        );
        assert_eq!(
            status_for(&FilesystemError::DiskFull).message(),
            "disk_full"
        );
    }

    #[test]
    fn entries_and_events_map_to_the_proto() {
        let entry = Entry {
            name: "link".to_owned(),
            kind: EntryKind::Symlink,
            path: "/home/user/link".to_owned(),
            size: 7,
            mode: 0o777,
            permissions: "lrwxrwxrwx".to_owned(),
            owner: "user".to_owned(),
            group: "user".to_owned(),
            modified_ms: 42,
            symlink_target: Some("big.bin".to_owned()),
        };
        let info = to_entry_info(entry.clone());
        assert_eq!(info.r#type, i32::from(FileType::Symlink));
        assert_eq!(info.symlink_target.as_deref(), Some("big.bin"));
        assert_eq!(info.modified_time_unix_ms, 42);
        assert_eq!(file_type(EntryKind::Other), FileType::Unspecified);
        let response = event_response(
            WatchEvent {
                name: "sub/n.txt".to_owned(),
                kind: WatchEventKind::Chmod,
            },
            Some(entry),
        );
        match response.event {
            Some(watch_dir_response::Event::Filesystem(event)) => {
                assert_eq!(event.name, "sub/n.txt");
                assert_eq!(event.r#type, i32::from(FilesystemEventType::Chmod));
                assert!(event.entry.is_some());
            }
            other => panic!("unexpected {other:?}"),
        }
        assert!(matches!(
            started_response().event,
            Some(watch_dir_response::Event::Started(_))
        ));
        assert!(matches!(
            keepalive_response().event,
            Some(watch_dir_response::Event::Keepalive(_))
        ));
    }

    #[test]
    fn write_requests_keep_only_the_chunk_length_in_the_domain_view() {
        let message = write_message(WriteRequest {
            path: Some("/tmp/a".to_owned()),
            user: Some(User {
                username: String::new(),
            }),
            mode: Some(0o600),
            chunk: vec![1, 2, 3],
        });
        assert_eq!(message.message.path.as_deref(), Some("/tmp/a"));
        assert_eq!(message.message.user, None);
        assert_eq!(message.message.mode, Some(0o600));
        assert_eq!(message.message.chunk_len, 3);
        assert_eq!(message.chunk, vec![1, 2, 3]);
    }
}
