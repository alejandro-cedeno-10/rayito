//! The five transfer RPCs of `FilesystemService` (ADR-010): proto <->
//! domain conversion, the unary status table of design D13 and
//! `WatchTransfer` wrapped in the suspend close and the 30 s keepalive.
//! Presigned URLs go straight into `Zeroizing` buffers and are never
//! logged; status messages are fixed sentences without a path, URL, bucket
//! or key.

use std::sync::Arc;
use std::time::Duration;

use rayd_core::transfer::{
    ExportInput, ExportTarget, ImportInput, ObjectRef, PresignedUrl, TransferDirection,
    TransferError, TransferPhase, TransferSnapshot, UnaryKind,
};
use rayito_proto::v1::{
    self, CancelTransferRequest, CancelTransferResponse, GetTransferRequest, KeepAlive,
    PresignedRequest, S3Object, StartExportRequest, StartImportRequest, StartTransferResponse,
    StreamError, TransferEvent, TransferState, WatchTransferRequest, start_export_request,
    transfer_event,
};
use tonic::codegen::BoxStream;
use tonic::{Code, Request, Response, Status};

use super::filesystem::{status_for, to_entry_info, username};
use super::keepalive::{DEFAULT_KEEPALIVE_INTERVAL, KeepAliveStream};
use crate::lifecycle::{SuspendClose, SuspendSignal, SuspendableStream};
use crate::transfer::{TransferBackend, UnavailableTransfers};

pub struct TransferGrpc {
    backend: Arc<dyn TransferBackend>,
    suspend: Arc<SuspendSignal>,
    keepalive_interval: Duration,
}

impl TransferGrpc {
    /// Without a backend every transfer RPC answers `UNIMPLEMENTED`.
    #[must_use]
    pub fn unavailable(suspend: Arc<SuspendSignal>) -> Self {
        Self::new(
            Arc::new(UnavailableTransfers),
            suspend,
            DEFAULT_KEEPALIVE_INTERVAL,
        )
    }

    #[must_use]
    pub fn new(
        backend: Arc<dyn TransferBackend>,
        suspend: Arc<SuspendSignal>,
        keepalive_interval: Duration,
    ) -> Self {
        Self {
            backend,
            suspend,
            keepalive_interval,
        }
    }

    pub async fn start_import(
        &self,
        request: Request<StartImportRequest>,
    ) -> Result<Response<StartTransferResponse>, Status> {
        let id = self
            .backend
            .start_import(import_input(request.into_inner()))
            .await
            .map_err(|error| rejected("StartImport", &error))?;
        Ok(Response::new(StartTransferResponse {
            transfer_id: id.as_str().to_owned(),
        }))
    }

    pub async fn start_export(
        &self,
        request: Request<StartExportRequest>,
    ) -> Result<Response<StartTransferResponse>, Status> {
        let id = self
            .backend
            .start_export(export_input(request.into_inner()))
            .await
            .map_err(|error| rejected("StartExport", &error))?;
        Ok(Response::new(StartTransferResponse {
            transfer_id: id.as_str().to_owned(),
        }))
    }

    pub fn get_transfer(
        &self,
        request: &Request<GetTransferRequest>,
    ) -> Result<Response<TransferState>, Status> {
        let snapshot = self
            .backend
            .get(&request.get_ref().transfer_id)
            .map_err(|error| rejected("GetTransfer", &error))?;
        Ok(Response::new(to_state(snapshot)))
    }

    pub fn watch_transfer(
        &self,
        request: &Request<WatchTransferRequest>,
    ) -> Result<Response<BoxStream<TransferEvent>>, Status> {
        let snapshots = self
            .backend
            .watch(&request.get_ref().transfer_id)
            .map_err(|error| rejected("WatchTransfer", &error))?;
        let events = SuspendableStream::new(
            snapshots,
            self.suspend.subscribe(),
            |snapshot| Ok(state_event(snapshot)),
            |_| SuspendClose::Status,
        );
        Ok(Response::new(Box::pin(KeepAliveStream::new(
            events,
            self.keepalive_interval,
            || Ok(keepalive_event()),
        ))))
    }

    pub fn cancel_transfer(
        &self,
        request: &Request<CancelTransferRequest>,
    ) -> Result<Response<CancelTransferResponse>, Status> {
        self.backend
            .cancel(&request.get_ref().transfer_id)
            .map_err(|error| rejected("CancelTransfer", &error))?;
        Ok(Response::new(CancelTransferResponse {}))
    }
}

fn import_input(request: StartImportRequest) -> ImportInput {
    ImportInput {
        path: request.path,
        user: username(request.user),
        mode: request.mode,
        object: request.object.map(object_ref),
        get: request.get.map(presigned),
        delete: request.delete.map(presigned),
        wait_for_object: request.wait_for_object,
        expires_at_unix_ms: request.expires_at_unix_ms,
        max_bytes: request.max_bytes,
        expected_sha256: request.expected_sha256,
        metadata: request.metadata.into_iter().collect(),
    }
}

fn export_input(request: StartExportRequest) -> ExportInput {
    ExportInput {
        path: request.path,
        user: username(request.user),
        object: request.object.map(object_ref),
        target: request.target.map(|target| match target {
            start_export_request::Target::Put(put) => ExportTarget::Put(presigned(put)),
            start_export_request::Target::Multipart(multipart) => ExportTarget::Multipart {
                part_size: multipart.part_size,
                parts: multipart.parts.into_iter().map(presigned).collect(),
            },
        }),
        expires_at_unix_ms: request.expires_at_unix_ms,
    }
}

fn object_ref(object: S3Object) -> ObjectRef {
    ObjectRef {
        bucket: object.bucket,
        key: object.key,
        region: object.region,
    }
}

fn presigned(request: PresignedRequest) -> PresignedUrl {
    PresignedUrl::new(request.url, request.headers.into_iter().collect())
}

fn to_state(snapshot: TransferSnapshot) -> TransferState {
    TransferState {
        transfer_id: snapshot.id.as_str().to_owned(),
        direction: i32::from(direction(snapshot.direction)),
        phase: i32::from(phase(snapshot.phase)),
        bytes_done: snapshot.bytes_done,
        bytes_total: snapshot.bytes_total,
        probes: snapshot.probes,
        entry: snapshot.entry.map(to_entry_info),
        sha256: snapshot.sha256,
        part_etags: snapshot.part_etags,
        duration_ms: u32::try_from(snapshot.duration.as_millis()).unwrap_or(u32::MAX),
        error: snapshot.failure.map(|failure| StreamError {
            code: failure.stream_code().to_owned(),
            message: failure.to_string(),
        }),
    }
}

fn direction(direction: TransferDirection) -> v1::TransferDirection {
    match direction {
        TransferDirection::Import => v1::TransferDirection::Import,
        TransferDirection::Export => v1::TransferDirection::Export,
    }
}

fn phase(phase: TransferPhase) -> v1::TransferPhase {
    match phase {
        TransferPhase::Waiting => v1::TransferPhase::Waiting,
        TransferPhase::Running => v1::TransferPhase::Running,
        TransferPhase::Done => v1::TransferPhase::Done,
        TransferPhase::Failed => v1::TransferPhase::Failed,
        TransferPhase::Cancelled => v1::TransferPhase::Cancelled,
    }
}

fn state_event(snapshot: TransferSnapshot) -> TransferEvent {
    TransferEvent {
        event: Some(transfer_event::Event::State(to_state(snapshot))),
    }
}

fn keepalive_event() -> TransferEvent {
    TransferEvent {
        event: Some(transfer_event::Event::Keepalive(KeepAlive {})),
    }
}

/// `NOT_FOUND` is the normal answer of the SDK's capability probe, so it is
/// logged at debug; the rest at warn. Neither carries a value.
fn rejected(rpc: &'static str, error: &TransferError) -> Status {
    let status = status_of(error);
    match status.code() {
        Code::NotFound => {
            tracing::debug!(rpc, outcome = "not_found", reason = %error, "transfer rpc rejected");
        }
        code => tracing::warn!(rpc, outcome = ?code, reason = %error, "transfer rpc rejected"),
    }
    status
}

fn status_of(error: &TransferError) -> Status {
    if let TransferError::Filesystem(filesystem) = error {
        return status_for(filesystem);
    }
    let message = error.to_string();
    match error.kind() {
        UnaryKind::InvalidArgument => Status::invalid_argument(message),
        UnaryKind::NotFound => Status::not_found(message),
        UnaryKind::FailedPrecondition => Status::failed_precondition(message),
        UnaryKind::ResourceExhausted => Status::resource_exhausted(message),
        UnaryKind::Unavailable => Status::unavailable(message),
        UnaryKind::Unimplemented => Status::unimplemented(message),
        UnaryKind::Internal | UnaryKind::Filesystem => Status::internal(message),
    }
}

#[cfg(test)]
mod tests {
    use std::collections::HashMap;

    use rayd_core::filesystem::FilesystemError;
    use rayd_core::lifecycle::HookPhase;
    use rayd_core::transfer::{
        FailureReason, RequestRejection, TransferFailure, TransferId, UrlPolicyError,
    };
    use rayito_proto::v1::PresignedMultipart;

    use super::*;

    #[test]
    fn unary_errors_follow_the_design_table() {
        let cases = [
            (
                TransferError::Policy(UrlPolicyError::Host),
                Code::InvalidArgument,
            ),
            (
                TransferError::Request(RequestRejection::Expiry),
                Code::InvalidArgument,
            ),
            (TransferError::TooLargeForPut, Code::InvalidArgument),
            (
                TransferError::Filesystem(FilesystemError::Denied),
                Code::PermissionDenied,
            ),
            (
                TransferError::Filesystem(FilesystemError::NotFound),
                Code::NotFound,
            ),
            (
                TransferError::Filesystem(FilesystemError::IsSymlink),
                Code::InvalidArgument,
            ),
            (TransferError::UnknownTransfer, Code::NotFound),
            (TransferError::Full, Code::ResourceExhausted),
            (TransferError::FileChanged, Code::FailedPrecondition),
            (TransferError::NotRunning, Code::FailedPrecondition),
            (
                TransferError::NotAccepting {
                    phase: HookPhase::Suspending,
                },
                Code::Unavailable,
            ),
            (TransferError::Unsupported, Code::Unimplemented),
            (TransferError::Internal, Code::Internal),
        ];
        for (error, code) in cases {
            assert_eq!(status_of(&error).code(), code, "{error}");
        }
        assert_eq!(
            status_of(&TransferError::NotAccepting {
                phase: HookPhase::Suspending
            })
            .message(),
            "suspending"
        );
    }

    #[test]
    fn requests_become_domain_inputs_with_their_urls_and_headers() {
        let get = PresignedRequest {
            url: "https://host/key?X-Amz-Signature=s".to_owned(),
            headers: HashMap::from([(
                "content-type".to_owned(),
                "application/octet-stream".to_owned(),
            )]),
        };
        let input = import_input(StartImportRequest {
            path: "/home/user/a".to_owned(),
            object: Some(S3Object {
                bucket: "amzn-s3-demo-bucket".to_owned(),
                key: "k".to_owned(),
                region: "us-east-1".to_owned(),
            }),
            get: Some(get.clone()),
            wait_for_object: true,
            max_bytes: 7,
            metadata: HashMap::from([("Owner".to_owned(), "alice".to_owned())]),
            ..StartImportRequest::default()
        });
        assert_eq!(input.path, "/home/user/a");
        assert_eq!(
            input.get.as_ref().map(|url| url.url.as_str()),
            Some(get.url.as_str())
        );
        assert_eq!(input.get.map(|url| url.headers.len()), Some(1));
        assert!(input.delete.is_none());
        assert!(input.wait_for_object);
        assert_eq!(input.max_bytes, 7);
        assert_eq!(
            input.metadata,
            vec![("Owner".to_owned(), "alice".to_owned())]
        );
        let export = export_input(StartExportRequest {
            target: Some(start_export_request::Target::Multipart(
                PresignedMultipart {
                    part_size: 8,
                    parts: vec![get.clone(), get],
                },
            )),
            ..StartExportRequest::default()
        });
        assert!(matches!(
            export.target,
            Some(ExportTarget::Multipart { part_size: 8, ref parts }) if parts.len() == 2
        ));
    }

    #[test]
    fn snapshots_become_transfer_states_with_the_reason_first() {
        let snapshot = TransferSnapshot {
            id: TransferId::parse(&"a".repeat(32)).unwrap(),
            direction: TransferDirection::Import,
            phase: TransferPhase::Failed,
            bytes_done: 0,
            bytes_total: 5,
            probes: 3,
            entry: None,
            sha256: String::new(),
            part_etags: Vec::new(),
            duration: Duration::from_millis(1_500),
            failure: Some(TransferFailure::of(FailureReason::TooLarge)),
            object_seen: true,
        };
        let state = to_state(snapshot);
        assert_eq!(state.transfer_id, "a".repeat(32));
        assert_eq!(state.direction, i32::from(v1::TransferDirection::Import));
        assert_eq!(state.phase, i32::from(v1::TransferPhase::Failed));
        assert_eq!(state.probes, 3);
        assert_eq!(state.duration_ms, 1_500);
        let error = state.error.unwrap();
        assert_eq!(error.code, "invalid_argument");
        assert!(error.message.starts_with("too_large: "));
        assert!(matches!(
            keepalive_event().event,
            Some(transfer_event::Event::Keepalive(_))
        ));
    }
}
