//! `FilesystemService.Checkpoint` / `Restore` (design D7/D8/D13): proto
//! <-> domain conversion, the status table before the first message, the
//! `StreamError` code after it, the suspend close (`StreamError
//! suspending` in-stream; a trailing `FAILED_PRECONDITION sandbox_timeout`
//! at the logical deadline, ADR-011) and the keepalive wrapper. Log lines carry the
//! rpc, the outcome and counts, never the bucket, the prefix or a path.

use std::sync::Arc;
use std::time::Duration;

use rayd_core::persistence::{
    CheckpointRequestInfo, LocationRequest, PersistenceError, RestoreRequestInfo, StatusKind,
};
use rayito_proto::v1::{
    CheckpointDone, CheckpointEvent, CheckpointProgress, CheckpointRequest, CheckpointStarted,
    KeepAlive, RestoreDone, RestoreEvent, RestoreProgress, RestoreRequest, RestoreStarted,
    S3Location, StreamError, checkpoint_event, restore_event,
};
use tonic::codegen::BoxStream;
use tonic::{Code, Request, Response, Status};

use super::keepalive::KeepAliveStream;
use crate::lifecycle::{StreamCloseReason, SuspendClose, SuspendSignal, SuspendableStream};
use crate::persistence::{CheckpointItem, PersistenceBackend, RestoreItem};

pub const SUSPENDING_CODE: &str = "suspending";

pub struct PersistenceGrpc {
    backend: Arc<dyn PersistenceBackend>,
    suspend: Arc<SuspendSignal>,
    keepalive_interval: Duration,
}

impl PersistenceGrpc {
    #[must_use]
    pub fn new(
        backend: Arc<dyn PersistenceBackend>,
        suspend: Arc<SuspendSignal>,
        keepalive_interval: Duration,
    ) -> Self {
        Self {
            backend,
            suspend,
            keepalive_interval,
        }
    }

    pub async fn checkpoint(
        &self,
        request: Request<CheckpointRequest>,
    ) -> Result<Response<BoxStream<CheckpointEvent>>, Status> {
        let request = request.into_inner();
        let info = CheckpointRequestInfo {
            location: location_of(request.target),
            user: username(request.user),
            exclude: request.exclude,
        };
        let stream = self
            .backend
            .checkpoint(info)
            .await
            .map_err(|error| rejected("Checkpoint", &error))?;
        tracing::info!(rpc = "Checkpoint", "checkpoint started");
        let responses = SuspendableStream::new(
            stream,
            self.suspend.subscribe(),
            |item| Ok(checkpoint_event(item)),
            |reason| match reason {
                StreamCloseReason::Suspending => {
                    SuspendClose::Terminal(checkpoint_error(SUSPENDING_CODE, SUSPENDING_CODE))
                }
                StreamCloseReason::SandboxTimeout => SuspendClose::Status,
            },
        );
        Ok(Response::new(Box::pin(KeepAliveStream::new(
            responses,
            self.keepalive_interval,
            || Ok(checkpoint_keepalive()),
        ))))
    }

    pub async fn restore(
        &self,
        request: Request<RestoreRequest>,
    ) -> Result<Response<BoxStream<RestoreEvent>>, Status> {
        let request = request.into_inner();
        let info = RestoreRequestInfo {
            location: location_of(request.source),
            user: username(request.user),
        };
        let stream = self
            .backend
            .restore(info)
            .await
            .map_err(|error| rejected("Restore", &error))?;
        tracing::info!(rpc = "Restore", "restore started");
        let responses = SuspendableStream::new(
            stream,
            self.suspend.subscribe(),
            |item| Ok(restore_event(item)),
            |reason| match reason {
                StreamCloseReason::Suspending => {
                    SuspendClose::Terminal(restore_error(SUSPENDING_CODE, SUSPENDING_CODE))
                }
                StreamCloseReason::SandboxTimeout => SuspendClose::Status,
            },
        );
        Ok(Response::new(Box::pin(KeepAliveStream::new(
            responses,
            self.keepalive_interval,
            || Ok(restore_keepalive()),
        ))))
    }
}

fn location_of(location: Option<S3Location>) -> LocationRequest {
    let location = location.unwrap_or_default();
    LocationRequest {
        bucket: location.bucket,
        key_prefix: location.key_prefix,
        region: location.region.filter(|region| !region.is_empty()),
    }
}

fn username(user: Option<rayito_proto::v1::User>) -> Option<String> {
    user.map(|user| user.username)
        .filter(|name| !name.is_empty())
}

fn checkpoint_event(item: CheckpointItem) -> CheckpointEvent {
    let event = match item {
        CheckpointItem::Started { files, bytes } => {
            checkpoint_event::Event::Started(CheckpointStarted { files, bytes })
        }
        CheckpointItem::Progress(snapshot) => {
            checkpoint_event::Event::Progress(CheckpointProgress {
                files_done: snapshot.files_done,
                bytes_read: snapshot.bytes_read,
                bytes_uploaded: snapshot.bytes_uploaded,
            })
        }
        CheckpointItem::Done(done) => checkpoint_event::Event::Done(CheckpointDone {
            files: done.files,
            bytes_read: done.bytes_read,
            archive_bytes: done.archive_bytes,
            sha256: done.sha256,
            skipped: done.skipped,
            duration_ms: done.duration_ms,
        }),
        CheckpointItem::Error(error) => {
            return checkpoint_error(error.stream_code(), &error.to_string());
        }
    };
    CheckpointEvent { event: Some(event) }
}

fn checkpoint_error(code: &str, message: &str) -> CheckpointEvent {
    CheckpointEvent {
        event: Some(checkpoint_event::Event::Error(StreamError {
            code: code.to_owned(),
            message: message.to_owned(),
        })),
    }
}

fn checkpoint_keepalive() -> CheckpointEvent {
    CheckpointEvent {
        event: Some(checkpoint_event::Event::Keepalive(KeepAlive {})),
    }
}

fn restore_event(item: RestoreItem) -> RestoreEvent {
    let event = match item {
        RestoreItem::Started {
            archive_bytes,
            files,
        } => restore_event::Event::Started(RestoreStarted {
            archive_bytes,
            files,
        }),
        RestoreItem::Progress(snapshot) => restore_event::Event::Progress(RestoreProgress {
            files_done: snapshot.files_done,
            bytes_downloaded: snapshot.bytes_downloaded,
        }),
        RestoreItem::Done(done) => restore_event::Event::Done(RestoreDone {
            files: done.files,
            bytes_written: done.bytes_written,
            archive_bytes: done.archive_bytes,
            sha256: done.sha256,
            skipped: done.skipped,
            duration_ms: done.duration_ms,
        }),
        RestoreItem::Error(error) => {
            return restore_error(error.stream_code(), &error.to_string());
        }
    };
    RestoreEvent { event: Some(event) }
}

fn restore_error(code: &str, message: &str) -> RestoreEvent {
    RestoreEvent {
        event: Some(restore_event::Event::Error(StreamError {
            code: code.to_owned(),
            message: message.to_owned(),
        })),
    }
}

fn restore_keepalive() -> RestoreEvent {
    RestoreEvent {
        event: Some(restore_event::Event::Keepalive(KeepAlive {})),
    }
}

/// `NOT_FOUND` is the normal answer of the first life of a name, so it
/// is logged at debug; everything else at warn.
fn rejected(rpc: &'static str, error: &PersistenceError) -> Status {
    let status = status_for(error);
    if status.code() == Code::NotFound {
        tracing::debug!(rpc, outcome = error.stream_code(), reason = %error, "persistence rpc rejected");
    } else {
        tracing::warn!(rpc, outcome = error.stream_code(), reason = %error, "persistence rpc rejected");
    }
    status
}

#[must_use]
pub fn status_for(error: &PersistenceError) -> Status {
    let message = error.to_string();
    match error.status_kind() {
        StatusKind::InvalidArgument => Status::invalid_argument(message),
        StatusKind::PermissionDenied => Status::permission_denied(message),
        StatusKind::NotFound => Status::not_found(message),
        StatusKind::FailedPrecondition => Status::failed_precondition(message),
        StatusKind::ResourceExhausted => Status::resource_exhausted(message),
        StatusKind::Cancelled => Status::cancelled(message),
        StatusKind::Unavailable => Status::unavailable(message),
        StatusKind::Unimplemented => Status::unimplemented(message),
        StatusKind::Internal => Status::internal(message),
    }
}

#[cfg(test)]
mod tests {
    use std::time::Duration;

    use rayd_core::clock::SystemClock;
    use rayd_core::persistence::{StoreError, StoreErrorKind};
    use rayd_core::process::{LookupError, ProcessIdentity, UserLookup, UserPolicy};
    use rayd_core::session::{RunHookInput, SandboxSession};
    use rayito_proto::v1::filesystem_service_client::FilesystemServiceClient;
    use rayito_proto::v1::filesystem_service_server::FilesystemServiceServer;
    use tokio::net::TcpListener;
    use tokio_util::sync::CancellationToken;
    use tonic::transport::Channel;
    use tonic::transport::server::TcpIncoming;
    use tonic::{Code, Streaming};

    use super::*;
    use crate::grpc::FilesystemGrpc;
    use crate::persistence::fakes::{FakeArchiver, FakeStore, seed_checkpoint};
    use crate::persistence::{PersistenceManager, PersistenceSettings};

    struct Users;

    impl UserLookup for Users {
        fn lookup(&self, username: &str) -> Result<ProcessIdentity, LookupError> {
            if username != "user" {
                return Err(LookupError::UnknownUser);
            }
            Ok(ProcessIdentity {
                uid: 1000,
                gid: 1000,
                groups: vec![1000],
                username: "user".to_owned(),
                home: "/home/user".to_owned(),
                shell: "/bin/sh".to_owned(),
            })
        }
    }

    struct Fixture {
        client: FilesystemServiceClient<Channel>,
        store: FakeStore,
        archiver: Arc<FakeArchiver>,
        suspend: Arc<SuspendSignal>,
        _shutdown: CancellationToken,
    }

    /// A session past `/run` (the stream gate is open) with `mvm-test` as
    /// the sandbox id the manifest records.
    fn running_session() -> Arc<SandboxSession> {
        let session = Arc::new(SandboxSession::new(Arc::new(SystemClock::new()), "test"));
        session.run(RunHookInput {
            sandbox_id: Some("mvm-test"),
            payload: Some(
                "{\"v\":1,\"token_sha256\":\"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\"}",
            ),
        });
        session
    }

    async fn fixture() -> Fixture {
        let session = running_session();
        let store = FakeStore::default();
        let archiver = Arc::new(FakeArchiver::default());
        let manager = PersistenceManager::new(
            session.clone(),
            Arc::new(store.clone()),
            archiver.clone(),
            Arc::new(Users),
            UserPolicy::default(),
            Some("us-east-1".to_owned()),
            PersistenceSettings {
                progress_interval: Duration::from_millis(20),
                part_bytes: 64,
            },
        );
        let suspend = Arc::new(SuspendSignal::new());
        let files = crate::filesystem::FilesystemManager::new(
            session,
            crate::filesystem::FilesystemPlatform {
                fs: Arc::new(crate::adapters::PlatformFileSystem::new(
                    crate::adapters::IdentitySwitch::KeepCurrent,
                )),
                watcher: Arc::new(crate::adapters::PlatformWatcher::new(
                    crate::adapters::IdentitySwitch::KeepCurrent,
                )),
                names: Arc::new(crate::adapters::PlatformNameResolver::default()),
                lookup: Arc::new(Users),
            },
            UserPolicy::default(),
            rayd_core::filesystem::DenyList::default(),
            crate::filesystem::FilesystemSettings::default(),
        );
        let service = FilesystemGrpc::with_keepalive_interval(
            files,
            suspend.clone(),
            Duration::from_secs(50),
        )
        .with_persistence(manager, Duration::from_millis(100));
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let shutdown = CancellationToken::new();
        let server = tonic::transport::Server::builder()
            .add_service(FilesystemServiceServer::new(service))
            .serve_with_incoming_shutdown(
                TcpIncoming::from(listener),
                shutdown.clone().cancelled_owned(),
            );
        tokio::spawn(server);
        let channel = Channel::from_shared(format!("http://{addr}"))
            .unwrap()
            .connect()
            .await
            .unwrap();
        Fixture {
            client: FilesystemServiceClient::new(channel),
            store,
            archiver,
            suspend,
            _shutdown: shutdown,
        }
    }

    fn target(name: &str) -> S3Location {
        S3Location {
            bucket: "my-bucket".to_owned(),
            key_prefix: format!("rayito/{name}"),
            region: None,
        }
    }

    fn checkpoint_request(name: &str, exclude: &[&str]) -> CheckpointRequest {
        CheckpointRequest {
            target: Some(target(name)),
            user: None,
            exclude: exclude.iter().map(|entry| (*entry).to_owned()).collect(),
        }
    }

    fn restore_request(name: &str) -> RestoreRequest {
        RestoreRequest {
            source: Some(target(name)),
            user: None,
        }
    }

    fn home() -> Vec<(String, Vec<u8>)> {
        vec![
            ("notes.txt".to_owned(), b"hola".to_vec()),
            ("data/blob.bin".to_owned(), vec![7u8; 300]),
            ("skipme/s".to_owned(), b"skip".to_vec()),
        ]
    }

    /// The upload is under way once the store consumed `n` parts; only
    /// then is there a multipart upload to abort.
    async fn wait_for_parts(store: &FakeStore, n: usize) {
        let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
        while store.parts_seen() < n {
            assert!(
                tokio::time::Instant::now() < deadline,
                "parts never arrived"
            );
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    }

    async fn collect_checkpoint(
        stream: &mut Streaming<CheckpointEvent>,
    ) -> Vec<checkpoint_event::Event> {
        let mut events = Vec::new();
        while let Some(event) = stream.message().await.unwrap() {
            if let Some(event) = event.event {
                let terminal = matches!(
                    event,
                    checkpoint_event::Event::Done(_) | checkpoint_event::Event::Error(_)
                );
                events.push(event);
                if terminal {
                    break;
                }
            }
        }
        events
    }

    async fn collect_restore(stream: &mut Streaming<RestoreEvent>) -> Vec<restore_event::Event> {
        let mut events = Vec::new();
        while let Some(event) = stream.message().await.unwrap() {
            if let Some(event) = event.event {
                let terminal = matches!(
                    event,
                    restore_event::Event::Done(_) | restore_event::Event::Error(_)
                );
                events.push(event);
                if terminal {
                    break;
                }
            }
        }
        events
    }

    #[tokio::test]
    async fn checkpoint_then_restore_round_trips_through_the_wire() {
        let mut fixture = fixture().await;
        fixture.archiver.set_home(home());
        let mut stream = fixture
            .client
            .checkpoint(checkpoint_request("a", &["skipme"]))
            .await
            .unwrap()
            .into_inner();
        let events = collect_checkpoint(&mut stream).await;
        assert!(matches!(
            events.first(),
            Some(checkpoint_event::Event::Started(CheckpointStarted {
                files: 2,
                bytes: 304
            }))
        ));
        let done = match events.last() {
            Some(checkpoint_event::Event::Done(done)) => done.clone(),
            other => panic!("unexpected {other:?}"),
        };
        assert_eq!(done.files, 2);
        assert_eq!(done.bytes_read, 304);
        assert_eq!(done.sha256.len(), 64);
        assert!(fixture.store.object("rayito/a/manifest.json").is_some());
        assert!(fixture.store.parts_seen() >= 2, "64-byte parts: several");
        let mut stream = fixture
            .client
            .restore(restore_request("a"))
            .await
            .unwrap()
            .into_inner();
        let events = collect_restore(&mut stream).await;
        assert!(matches!(
            events.first(),
            Some(restore_event::Event::Started(RestoreStarted {
                files: 2,
                ..
            }))
        ));
        match events.last() {
            Some(restore_event::Event::Done(restored)) => {
                assert_eq!(restored.files, 2);
                assert_eq!(restored.sha256, done.sha256);
            }
            other => panic!("unexpected {other:?}"),
        }
        assert_eq!(fixture.archiver.extracted().len(), 2);
    }

    #[tokio::test]
    async fn refusals_before_started_are_statuses() {
        let mut fixture = fixture().await;
        let missing = fixture
            .client
            .restore(restore_request("nobody"))
            .await
            .unwrap_err();
        assert_eq!(missing.code(), Code::NotFound);
        assert_eq!(missing.message(), "no checkpoint under the prefix");
        let mut bad = checkpoint_request("a", &[]);
        bad.target.as_mut().unwrap().bucket = "Bad".to_owned();
        let invalid = fixture.client.checkpoint(bad).await.unwrap_err();
        assert_eq!(invalid.code(), Code::InvalidArgument);
        let mut root = checkpoint_request("a", &[]);
        root.user = Some(rayito_proto::v1::User {
            username: "root".to_owned(),
        });
        let denied = fixture.client.checkpoint(root).await.unwrap_err();
        assert_eq!(denied.code(), Code::PermissionDenied);
        fixture
            .store
            .fail_credentials(StoreError::new(StoreErrorKind::NoCredentials));
        let no_role = fixture
            .client
            .checkpoint(checkpoint_request("a", &[]))
            .await
            .unwrap_err();
        assert_eq!(no_role.code(), Code::PermissionDenied);
        assert_eq!(no_role.message(), "no execution role credentials");
        assert!(
            !fixture
                .store
                .operations()
                .contains(&"put_multipart".to_owned()),
            "the network is never touched without credentials"
        );
    }

    #[tokio::test]
    async fn a_second_operation_while_one_runs_is_failed_precondition() {
        let mut fixture = fixture().await;
        fixture.archiver.set_home(home());
        fixture.store.slow_parts(Duration::from_millis(200));
        let mut first = fixture
            .client
            .checkpoint(checkpoint_request("a", &[]))
            .await
            .unwrap()
            .into_inner();
        let started = first.message().await.unwrap().unwrap();
        assert!(matches!(
            started.event,
            Some(checkpoint_event::Event::Started(_))
        ));
        let busy = fixture
            .client
            .checkpoint(checkpoint_request("b", &[]))
            .await
            .unwrap_err();
        assert_eq!(busy.code(), Code::FailedPrecondition);
        assert_eq!(busy.message(), "persistence busy");
        drop(first);
        tokio::time::sleep(Duration::from_millis(300)).await;
        let again = fixture
            .client
            .checkpoint(checkpoint_request("c", &[]))
            .await;
        assert!(again.is_ok(), "the lease is released after a cancel");
    }

    #[tokio::test]
    async fn store_failure_after_started_is_a_stream_error() {
        let mut fixture = fixture().await;
        fixture.archiver.set_home(home());
        fixture
            .store
            .fail_multipart(StoreError::new(StoreErrorKind::AccessDenied));
        let mut stream = fixture
            .client
            .checkpoint(checkpoint_request("a", &[]))
            .await
            .unwrap()
            .into_inner();
        let events = collect_checkpoint(&mut stream).await;
        match events.last() {
            Some(checkpoint_event::Event::Error(error)) => {
                assert_eq!(error.code, "permission_denied");
                assert_eq!(
                    error.message,
                    "access denied by the bucket or the execution role"
                );
            }
            other => panic!("unexpected {other:?}"),
        }
        assert_eq!(fixture.store.aborted(), 1);
        assert!(fixture.store.object("rayito/a/manifest.json").is_none());
    }

    #[tokio::test]
    async fn client_cancel_aborts_the_upload_and_writes_no_manifest() {
        let mut fixture = fixture().await;
        fixture.archiver.set_home(home());
        fixture.store.slow_parts(Duration::from_millis(150));
        let mut stream = fixture
            .client
            .checkpoint(checkpoint_request("a", &[]))
            .await
            .unwrap()
            .into_inner();
        let started = stream.message().await.unwrap().unwrap();
        assert!(matches!(
            started.event,
            Some(checkpoint_event::Event::Started(_))
        ));
        wait_for_parts(&fixture.store, 1).await;
        drop(stream);
        tokio::time::sleep(Duration::from_millis(500)).await;
        assert_eq!(fixture.store.aborted(), 1);
        assert!(fixture.store.object("rayito/a/manifest.json").is_none());
    }

    #[tokio::test]
    async fn suspend_closes_the_stream_with_suspending() {
        let mut fixture = fixture().await;
        fixture.archiver.set_home(home());
        fixture.store.slow_parts(Duration::from_millis(150));
        let mut stream = fixture
            .client
            .checkpoint(checkpoint_request("a", &[]))
            .await
            .unwrap()
            .into_inner();
        let started = stream.message().await.unwrap().unwrap();
        assert!(matches!(
            started.event,
            Some(checkpoint_event::Event::Started(_))
        ));
        wait_for_parts(&fixture.store, 1).await;
        fixture.suspend.broadcast(1);
        let events = collect_checkpoint(&mut stream).await;
        match events.last() {
            Some(checkpoint_event::Event::Error(error)) => assert_eq!(error.code, "suspending"),
            other => panic!("unexpected {other:?}"),
        }
        assert!(stream.message().await.unwrap().is_none());
        tokio::time::sleep(Duration::from_millis(300)).await;
        assert_eq!(fixture.store.aborted(), 1);
    }

    #[tokio::test]
    async fn progress_and_keepalives_are_emitted_while_uploading() {
        let mut fixture = fixture().await;
        fixture.archiver.set_home(home());
        fixture.store.slow_parts(Duration::from_millis(120));
        let mut stream = fixture
            .client
            .checkpoint(checkpoint_request("a", &[]))
            .await
            .unwrap()
            .into_inner();
        let mut progress = 0;
        let mut keepalives = 0;
        while let Some(event) = stream.message().await.unwrap() {
            match event.event {
                Some(checkpoint_event::Event::Progress(_)) => progress += 1,
                Some(checkpoint_event::Event::Keepalive(_)) => keepalives += 1,
                Some(checkpoint_event::Event::Done(_)) => break,
                Some(checkpoint_event::Event::Error(error)) => panic!("{error:?}"),
                _ => {}
            }
        }
        assert!(progress >= 1, "at least one progress sample");
        assert!(
            keepalives >= 1,
            "the 100 ms keepalive fired during the slow parts"
        );
    }

    #[tokio::test]
    async fn restore_of_a_seeded_checkpoint_reports_the_manifest_in_started() {
        let mut fixture = fixture().await;
        let sha = seed_checkpoint(&fixture.store, "rayito/seeded", &home());
        let mut stream = fixture
            .client
            .restore(restore_request("seeded"))
            .await
            .unwrap()
            .into_inner();
        let events = collect_restore(&mut stream).await;
        assert!(matches!(
            events.first(),
            Some(restore_event::Event::Started(RestoreStarted {
                files: 3,
                ..
            }))
        ));
        match events.last() {
            Some(restore_event::Event::Done(done)) => assert_eq!(done.sha256, sha),
            other => panic!("unexpected {other:?}"),
        }
    }

    #[test]
    fn status_table_matches_the_design() {
        assert_eq!(
            status_for(&PersistenceError::Busy).code(),
            Code::FailedPrecondition
        );
        assert_eq!(
            status_for(&PersistenceError::RegionUnknown).code(),
            Code::FailedPrecondition
        );
        assert_eq!(
            status_for(&PersistenceError::NotFound).code(),
            Code::NotFound
        );
        assert_eq!(
            status_for(&PersistenceError::NoCredentials).code(),
            Code::PermissionDenied
        );
        assert_eq!(
            status_for(&PersistenceError::DiskFull).code(),
            Code::ResourceExhausted
        );
        assert_eq!(
            status_for(&PersistenceError::Unsupported).code(),
            Code::Unimplemented
        );
        assert_eq!(
            status_for(&PersistenceError::Suspending).code(),
            Code::Unavailable
        );
        assert_eq!(status_for(&PersistenceError::Store).code(), Code::Internal);
        assert_eq!(
            status_for(&PersistenceError::WrongRegion).code(),
            Code::InvalidArgument
        );
    }
}
