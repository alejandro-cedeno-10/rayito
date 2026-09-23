//! How a transfer fails (design D13). A refusal before the transfer exists
//! is a unary status (`TransferError`); a transfer that ran and failed
//! carries a `StreamError{code, "<reason>: <frase>"}` in its state
//! (`TransferFailure`). Every message is a fixed sentence: never a path, a
//! URL, a bucket, a key or a header value.

use thiserror::Error;

use super::url_policy::UrlPolicyError;
use crate::filesystem::FilesystemError;
use crate::lifecycle::HookPhase;

/// The closed `StreamError` codes a transfer state can carry.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FailureCode {
    DeadlineExceeded,
    NotFound,
    PermissionDenied,
    InvalidArgument,
    ResourceExhausted,
    FailedPrecondition,
    Unavailable,
    Cancelled,
    Internal,
}

impl FailureCode {
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::DeadlineExceeded => "deadline_exceeded",
            Self::NotFound => "not_found",
            Self::PermissionDenied => "permission_denied",
            Self::InvalidArgument => "invalid_argument",
            Self::ResourceExhausted => "resource_exhausted",
            Self::FailedPrecondition => "failed_precondition",
            Self::Unavailable => "unavailable",
            Self::Cancelled => "cancelled",
            Self::Internal => "internal",
        }
    }
}

/// The reason token that opens every failure message; the SDK keys on it
/// (`str(exc).startswith("too_large")`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FailureReason {
    Expired,
    NoObject,
    AccessDenied,
    SignatureRejected,
    WrongRegion,
    BucketMissing,
    S3Unavailable,
    ForbiddenAddress,
    UnexpectedResponse,
    NoContentLength,
    TooLarge,
    DiskReserve,
    DiskFull,
    ChecksumMismatch,
    FileShrank,
    Cancelled,
    /// The destination stopped being writable (or became denied) between
    /// the request and the arrival of the object.
    DestinationRejected,
    MetadataUnsupported,
    MetadataTooLarge,
}

impl FailureReason {
    #[must_use]
    pub fn token(self) -> &'static str {
        match self {
            Self::Expired => "expired",
            Self::NoObject => "no_object",
            Self::AccessDenied => "access_denied",
            Self::SignatureRejected => "signature_rejected",
            Self::WrongRegion => "wrong_region",
            Self::BucketMissing => "bucket_missing",
            Self::S3Unavailable => "s3_unavailable",
            Self::ForbiddenAddress => "forbidden_address",
            Self::UnexpectedResponse => "unexpected_response",
            Self::NoContentLength => "no_content_length",
            Self::TooLarge => "too_large",
            Self::DiskReserve => "disk_reserve",
            Self::DiskFull => "disk_full",
            Self::ChecksumMismatch => "checksum_mismatch",
            Self::FileShrank => "file_shrank",
            Self::Cancelled => "cancelled",
            Self::DestinationRejected => "destination_rejected",
            Self::MetadataUnsupported => "metadata_unsupported",
            Self::MetadataTooLarge => "metadata_too_large",
        }
    }

    fn phrase(self) -> &'static str {
        match self {
            Self::Expired => "la URL o la credencial que la firmó caducó",
            Self::NoObject => "el objeto no existe",
            Self::AccessDenied => "S3 denegó el acceso",
            Self::SignatureRejected => "S3 rechazó la firma",
            Self::WrongRegion => "el bucket está en otra región",
            Self::BucketMissing => "el bucket no existe",
            Self::S3Unavailable => "S3 no respondió tras varios intentos",
            Self::ForbiddenAddress => "el host resuelve a una dirección prohibida",
            Self::UnexpectedResponse => "respuesta inesperada de S3",
            Self::NoContentLength => "la respuesta no trae Content-Length",
            Self::TooLarge => "el objeto supera max_bytes",
            Self::DiskReserve => "no queda espacio en disco para el fichero",
            Self::DiskFull => "el disco se llenó durante la escritura",
            Self::ChecksumMismatch => "el sha256 no coincide",
            Self::FileShrank => "el fichero se acortó durante la lectura",
            Self::Cancelled => "transferencia cancelada",
            Self::DestinationRejected => "el destino ya no admite la escritura",
            Self::MetadataUnsupported => "el sistema de ficheros no admite metadatos",
            Self::MetadataTooLarge => "los metadatos no caben en el fichero",
        }
    }

    /// The code a reason travels with when nothing else decides it.
    #[must_use]
    pub fn default_code(self) -> FailureCode {
        match self {
            Self::Expired => FailureCode::DeadlineExceeded,
            Self::NoObject => FailureCode::NotFound,
            Self::AccessDenied => FailureCode::PermissionDenied,
            Self::SignatureRejected
            | Self::WrongRegion
            | Self::BucketMissing
            | Self::ForbiddenAddress
            | Self::NoContentLength
            | Self::TooLarge
            | Self::MetadataTooLarge => FailureCode::InvalidArgument,
            Self::S3Unavailable => FailureCode::Unavailable,
            Self::UnexpectedResponse | Self::DestinationRejected => FailureCode::Internal,
            Self::DiskReserve | Self::DiskFull => FailureCode::ResourceExhausted,
            Self::ChecksumMismatch | Self::FileShrank | Self::MetadataUnsupported => {
                FailureCode::FailedPrecondition
            }
            Self::Cancelled => FailureCode::Cancelled,
        }
    }
}

/// Why a transfer that existed ended `FAILED` or `CANCELLED`.
#[derive(Debug, Error, Clone, Copy, PartialEq, Eq)]
#[error("{}: {}", reason.token(), reason.phrase())]
pub struct TransferFailure {
    pub code: FailureCode,
    pub reason: FailureReason,
}

impl TransferFailure {
    #[must_use]
    pub fn of(reason: FailureReason) -> Self {
        Self {
            code: reason.default_code(),
            reason,
        }
    }

    /// A filesystem refusal at the moment an import writes: disk and
    /// metadata failures keep their own reasons, everything else is
    /// `destination_rejected` with the code the unary table would give.
    #[must_use]
    pub fn from_filesystem(error: &FilesystemError) -> Self {
        match error {
            FilesystemError::DiskReserve => Self::of(FailureReason::DiskReserve),
            FilesystemError::DiskFull => Self::of(FailureReason::DiskFull),
            FilesystemError::MetadataUnsupported => Self::of(FailureReason::MetadataUnsupported),
            FilesystemError::MetadataTooLarge | FilesystemError::InvalidMetadata => {
                Self::of(FailureReason::MetadataTooLarge)
            }
            other => Self {
                code: filesystem_code(other),
                reason: FailureReason::DestinationRejected,
            },
        }
    }

    #[must_use]
    pub fn stream_code(&self) -> &'static str {
        self.code.as_str()
    }
}

fn filesystem_code(error: &FilesystemError) -> FailureCode {
    match error {
        FilesystemError::Denied
        | FilesystemError::PermissionDenied
        | FilesystemError::RootNotAllowed
        | FilesystemError::PrivilegedAccount => FailureCode::PermissionDenied,
        FilesystemError::NotFound => FailureCode::NotFound,
        FilesystemError::InvalidPath(_)
        | FilesystemError::IsADirectory
        | FilesystemError::NotADirectory
        | FilesystemError::NotARegularFile
        | FilesystemError::IsSymlink
        | FilesystemError::UnknownUser => FailureCode::InvalidArgument,
        _ => FailureCode::Internal,
    }
}

/// A request field outside the rules of design D6 (the non-URL checks).
#[derive(Debug, Error, Clone, Copy, PartialEq, Eq)]
pub enum RequestRejection {
    #[error("falta el objeto S3")]
    MissingObject,
    #[error("falta una URL prefirmada")]
    MissingUrl,
    #[error("expires_at_unix_ms debe estar en el futuro y a menos de 7 días")]
    Expiry,
    #[error("mode fuera de 0..=7777")]
    Mode,
    #[error("expected_sha256 debe ser hex en minúsculas de 64 caracteres")]
    Sha256,
    #[error("metadatos inválidos")]
    Metadata,
    #[error("part_size fuera de 5 MiB..=5 GiB")]
    PartSize,
    #[error("una exportación multiparte lleva entre 1 y 1000 partes")]
    PartCount,
}

/// A refusal answered as a unary gRPC status, before any network I/O.
#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum TransferError {
    #[error("{0}")]
    Policy(UrlPolicyError),
    #[error("{0}")]
    Request(RequestRejection),
    #[error("{0}")]
    Filesystem(FilesystemError),
    #[error("too_large_for_put: un PUT admite hasta 5 GiB; usa multiparte")]
    TooLargeForPut,
    #[error("file_changed: el fichero cambió desde que el SDK lo midió")]
    FileChanged,
    #[error("demasiadas transferencias activas")]
    Full,
    #[error("transferencia desconocida")]
    UnknownTransfer,
    #[error("el sandbox todavía no recibió el hook run")]
    NotRunning,
    #[error("{phase}")]
    NotAccepting { phase: HookPhase },
    #[error("este agente no tiene transferencias")]
    Unsupported,
    #[error("error interno del agente")]
    Internal,
}

/// The gRPC status family of a `TransferError` other than a filesystem
/// one (which keeps the filesystem table).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum UnaryKind {
    InvalidArgument,
    NotFound,
    FailedPrecondition,
    ResourceExhausted,
    Unavailable,
    Unimplemented,
    Internal,
    Filesystem,
}

impl TransferError {
    #[must_use]
    pub fn kind(&self) -> UnaryKind {
        match self {
            Self::Policy(_) | Self::Request(_) | Self::TooLargeForPut => UnaryKind::InvalidArgument,
            Self::Filesystem(_) => UnaryKind::Filesystem,
            Self::FileChanged | Self::NotRunning => UnaryKind::FailedPrecondition,
            Self::Full => UnaryKind::ResourceExhausted,
            Self::UnknownTransfer => UnaryKind::NotFound,
            Self::NotAccepting { .. } => UnaryKind::Unavailable,
            Self::Unsupported => UnaryKind::Unimplemented,
            Self::Internal => UnaryKind::Internal,
        }
    }
}

impl From<UrlPolicyError> for TransferError {
    fn from(error: UrlPolicyError) -> Self {
        Self::Policy(error)
    }
}

impl From<RequestRejection> for TransferError {
    fn from(rejection: RequestRejection) -> Self {
        Self::Request(rejection)
    }
}

impl From<FilesystemError> for TransferError {
    fn from(error: FilesystemError) -> Self {
        Self::Filesystem(error)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::filesystem::PathRejection;

    const REASONS: [FailureReason; 19] = [
        FailureReason::Expired,
        FailureReason::NoObject,
        FailureReason::AccessDenied,
        FailureReason::SignatureRejected,
        FailureReason::WrongRegion,
        FailureReason::BucketMissing,
        FailureReason::S3Unavailable,
        FailureReason::ForbiddenAddress,
        FailureReason::UnexpectedResponse,
        FailureReason::NoContentLength,
        FailureReason::TooLarge,
        FailureReason::DiskReserve,
        FailureReason::DiskFull,
        FailureReason::ChecksumMismatch,
        FailureReason::FileShrank,
        FailureReason::Cancelled,
        FailureReason::DestinationRejected,
        FailureReason::MetadataUnsupported,
        FailureReason::MetadataTooLarge,
    ];

    fn unary_errors() -> Vec<TransferError> {
        let mut errors: Vec<TransferError> = UrlPolicyError::ALL
            .iter()
            .copied()
            .map(TransferError::Policy)
            .collect();
        errors.extend(
            [
                RequestRejection::MissingObject,
                RequestRejection::MissingUrl,
                RequestRejection::Expiry,
                RequestRejection::Mode,
                RequestRejection::Sha256,
                RequestRejection::Metadata,
                RequestRejection::PartSize,
                RequestRejection::PartCount,
            ]
            .map(TransferError::Request),
        );
        errors.extend([
            TransferError::TooLargeForPut,
            TransferError::FileChanged,
            TransferError::Full,
            TransferError::UnknownTransfer,
            TransferError::NotRunning,
            TransferError::NotAccepting {
                phase: HookPhase::Suspending,
            },
            TransferError::Unsupported,
            TransferError::Internal,
        ]);
        errors
    }

    /// Design D13: no message ever carries a path, a URL or an S3 host.
    #[test]
    fn messages_are_path_free_and_url_free() {
        let mut messages: Vec<String> = REASONS
            .iter()
            .map(|reason| TransferFailure::of(*reason).to_string())
            .collect();
        messages.extend(unary_errors().iter().map(ToString::to_string));
        for message in messages {
            assert!(!message.contains('/'), "{message}");
            assert!(!message.contains("http"), "{message}");
            assert!(!message.contains("amazonaws"), "{message}");
            assert!(!message.is_empty());
        }
    }

    #[test]
    fn failure_messages_start_with_the_reason_token() {
        for reason in REASONS {
            let failure = TransferFailure::of(reason);
            assert!(
                failure
                    .to_string()
                    .starts_with(&format!("{}: ", reason.token())),
                "{failure}"
            );
        }
        assert_eq!(
            TransferFailure::of(FailureReason::TooLarge).stream_code(),
            "invalid_argument"
        );
        assert_eq!(
            TransferFailure::of(FailureReason::Expired).stream_code(),
            "deadline_exceeded"
        );
        assert_eq!(
            TransferFailure::of(FailureReason::ChecksumMismatch).stream_code(),
            "failed_precondition"
        );
    }

    #[test]
    fn filesystem_refusals_keep_disk_and_metadata_reasons() {
        assert_eq!(
            TransferFailure::from_filesystem(&FilesystemError::DiskFull),
            TransferFailure::of(FailureReason::DiskFull)
        );
        assert_eq!(
            TransferFailure::from_filesystem(&FilesystemError::MetadataUnsupported).code,
            FailureCode::FailedPrecondition
        );
        let denied = TransferFailure::from_filesystem(&FilesystemError::Denied);
        assert_eq!(denied.reason, FailureReason::DestinationRejected);
        assert_eq!(denied.code, FailureCode::PermissionDenied);
        assert_eq!(
            TransferFailure::from_filesystem(&FilesystemError::InvalidPath(
                PathRejection::ParentReference
            ))
            .code,
            FailureCode::InvalidArgument
        );
    }

    #[test]
    fn unary_kinds_follow_the_design_table() {
        assert_eq!(TransferError::Full.kind(), UnaryKind::ResourceExhausted);
        assert_eq!(TransferError::UnknownTransfer.kind(), UnaryKind::NotFound);
        assert_eq!(
            TransferError::FileChanged.kind(),
            UnaryKind::FailedPrecondition
        );
        assert_eq!(
            TransferError::NotRunning.kind(),
            UnaryKind::FailedPrecondition
        );
        assert_eq!(
            TransferError::TooLargeForPut.kind(),
            UnaryKind::InvalidArgument
        );
        assert_eq!(
            TransferError::NotAccepting {
                phase: HookPhase::Terminating
            }
            .to_string(),
            "terminating"
        );
        assert_eq!(TransferError::Unsupported.kind(), UnaryKind::Unimplemented);
        assert_eq!(TransferError::Internal.kind(), UnaryKind::Internal);
    }
}
