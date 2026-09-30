//! Persistence failures and where they surface (design D8): as a gRPC
//! status before the first message, as a `StreamError` code after it.
//! Every message is a fixed sentence: no path, no key, no bucket, no S3
//! request id (those go to the log through the adapters' own fields).

use std::fmt;

use thiserror::Error;

use super::keys::{BucketRejection, KeyPrefixRejection};
use super::plan::ExcludeRejection;
use super::ports::{ArchiveError, StoreError, StoreErrorKind};
use crate::wire_tokens::SUSPENDING;

/// Why `manifest.json` was not accepted; never carries its content.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ManifestRejection {
    TooLarge,
    Malformed,
    Version,
    Checksum,
    Archive,
}

impl fmt::Display for ManifestRejection {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::TooLarge => "supera 64 KiB",
            Self::Malformed => "no es un JSON válido con la forma esperada",
            Self::Version => "tiene una versión distinta de 1",
            Self::Checksum => "no tiene sha256",
            Self::Archive => "nombra un archivo que rayd no escribe",
        })
    }
}

#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum PersistenceError {
    #[error("bucket inválido: {0}")]
    InvalidBucket(BucketRejection),
    #[error("key_prefix inválido: {0}")]
    InvalidKeyPrefix(KeyPrefixRejection),
    #[error("entrada de exclude inválida: {0}")]
    InvalidExclude(ExcludeRejection),
    #[error("más de {max} entradas en exclude")]
    TooManyExcludes { max: usize },
    #[error("manifest inválido: {0}")]
    InvalidManifest(ManifestRejection),
    #[error("usuario desconocido")]
    UnknownUser,
    #[error("falló la búsqueda del usuario: {0}")]
    UserLookupFailed(String),
    #[error("la persistencia nunca archiva el home de root")]
    RootNotAllowed,
    #[error("la persistencia sólo archiva el home de una cuenta sin privilegios")]
    PrivilegedAccount,
    #[error("no hay credenciales del rol de ejecución")]
    NoCredentials,
    #[error("se rechazaron las credenciales del rol de ejecución")]
    CredentialsRejected,
    #[error("el bucket o el rol de ejecución denegaron el acceso")]
    AccessDenied,
    #[error("no hay checkpoint bajo el prefijo")]
    NotFound,
    #[error("falta el archivo aunque el manifest existe")]
    ArchiveMissing,
    #[error("la persistencia está ocupada")]
    Busy,
    #[error("región desconocida")]
    RegionUnknown,
    #[error("el bucket está en otra región")]
    WrongRegion,
    #[error("falló la petición al almacén de objetos")]
    Store,
    #[error("falló el archivo: {operation}")]
    Archive { operation: &'static str },
    #[error("entrada del archivo rechazada")]
    EntryRefused,
    #[error("el checksum del archivo no coincide")]
    ChecksumMismatch,
    #[error("disco lleno")]
    DiskFull,
    #[error("operación cancelada")]
    Cancelled,
    #[error("{SUSPENDING}")]
    Suspending,
    #[error("la persistencia no se admite en esta plataforma")]
    Unsupported,
}

/// A gRPC status class without the transport crate.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StatusKind {
    InvalidArgument,
    PermissionDenied,
    NotFound,
    FailedPrecondition,
    ResourceExhausted,
    Cancelled,
    Unavailable,
    Unimplemented,
    Internal,
}

impl PersistenceError {
    /// The status a failure before the first message carries.
    #[must_use]
    pub fn status_kind(&self) -> StatusKind {
        match self {
            Self::InvalidBucket(_)
            | Self::InvalidKeyPrefix(_)
            | Self::InvalidExclude(_)
            | Self::TooManyExcludes { .. }
            | Self::InvalidManifest(_)
            | Self::UnknownUser
            | Self::WrongRegion
            | Self::EntryRefused => StatusKind::InvalidArgument,
            Self::RootNotAllowed
            | Self::PrivilegedAccount
            | Self::NoCredentials
            | Self::CredentialsRejected
            | Self::AccessDenied => StatusKind::PermissionDenied,
            Self::NotFound | Self::ArchiveMissing => StatusKind::NotFound,
            Self::Busy | Self::RegionUnknown => StatusKind::FailedPrecondition,
            Self::DiskFull => StatusKind::ResourceExhausted,
            Self::Cancelled => StatusKind::Cancelled,
            Self::Suspending => StatusKind::Unavailable,
            Self::Unsupported => StatusKind::Unimplemented,
            Self::UserLookupFailed(_)
            | Self::Store
            | Self::Archive { .. }
            | Self::ChecksumMismatch => StatusKind::Internal,
        }
    }

    /// The `StreamError.code` a failure after the first message carries.
    #[must_use]
    pub fn stream_code(&self) -> &'static str {
        match self.status_kind() {
            StatusKind::InvalidArgument => "invalid_argument",
            StatusKind::PermissionDenied => "permission_denied",
            StatusKind::NotFound => "not_found",
            StatusKind::Cancelled => "cancelled",
            StatusKind::Unavailable => "suspending",
            StatusKind::Unimplemented => "unimplemented",
            StatusKind::FailedPrecondition
            | StatusKind::ResourceExhausted
            | StatusKind::Internal => "internal",
        }
    }

    #[must_use]
    pub fn from_store(error: &StoreError) -> Self {
        match error.kind {
            StoreErrorKind::NotFound => Self::NotFound,
            StoreErrorKind::NoCredentials => Self::NoCredentials,
            StoreErrorKind::CredentialsRejected => Self::CredentialsRejected,
            StoreErrorKind::AccessDenied => Self::AccessDenied,
            StoreErrorKind::WrongRegion => Self::WrongRegion,
            StoreErrorKind::Interrupted => Self::Cancelled,
            StoreErrorKind::Other => Self::Store,
        }
    }

    #[must_use]
    pub fn from_archive(error: &ArchiveError) -> Self {
        match error {
            ArchiveError::Cancelled => Self::Cancelled,
            ArchiveError::DiskFull => Self::DiskFull,
            ArchiveError::EntryRefused => Self::EntryRefused,
            ArchiveError::PermissionDenied => Self::AccessDenied,
            ArchiveError::Unsupported => Self::Unsupported,
            ArchiveError::Io { operation, .. } => Self::Archive { operation },
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::filesystem::PathRejection;

    fn every_variant() -> Vec<PersistenceError> {
        vec![
            PersistenceError::InvalidBucket(BucketRejection::Length),
            PersistenceError::InvalidKeyPrefix(KeyPrefixRejection::Empty),
            PersistenceError::InvalidExclude(ExcludeRejection::Path(PathRejection::Empty)),
            PersistenceError::TooManyExcludes { max: 64 },
            PersistenceError::InvalidManifest(ManifestRejection::Version),
            PersistenceError::UnknownUser,
            PersistenceError::UserLookupFailed("nss".to_owned()),
            PersistenceError::RootNotAllowed,
            PersistenceError::PrivilegedAccount,
            PersistenceError::NoCredentials,
            PersistenceError::CredentialsRejected,
            PersistenceError::AccessDenied,
            PersistenceError::NotFound,
            PersistenceError::ArchiveMissing,
            PersistenceError::Busy,
            PersistenceError::RegionUnknown,
            PersistenceError::WrongRegion,
            PersistenceError::Store,
            PersistenceError::Archive { operation: "read" },
            PersistenceError::EntryRefused,
            PersistenceError::ChecksumMismatch,
            PersistenceError::DiskFull,
            PersistenceError::Cancelled,
            PersistenceError::Suspending,
            PersistenceError::Unsupported,
        ]
    }

    #[test]
    fn messages_are_fixed_and_path_free() {
        for error in every_variant() {
            let message = error.to_string();
            assert!(!message.contains('/'), "{message}");
            assert!(!message.is_empty());
        }
        assert_eq!(
            PersistenceError::Busy.to_string(),
            "la persistencia está ocupada"
        );
        assert_eq!(
            PersistenceError::RegionUnknown.to_string(),
            "región desconocida"
        );
        assert_eq!(
            PersistenceError::NoCredentials.to_string(),
            "no hay credenciales del rol de ejecución"
        );
        assert_eq!(
            PersistenceError::CredentialsRejected.to_string(),
            "se rechazaron las credenciales del rol de ejecución"
        );
        assert_eq!(
            PersistenceError::ChecksumMismatch.to_string(),
            "el checksum del archivo no coincide"
        );
        assert_eq!(PersistenceError::DiskFull.to_string(), "disco lleno");
    }

    #[test]
    fn status_table_matches_the_design() {
        let cases = [
            (
                PersistenceError::InvalidBucket(BucketRejection::Edge),
                StatusKind::InvalidArgument,
            ),
            (
                PersistenceError::InvalidManifest(ManifestRejection::Version),
                StatusKind::InvalidArgument,
            ),
            (PersistenceError::UnknownUser, StatusKind::InvalidArgument),
            (PersistenceError::WrongRegion, StatusKind::InvalidArgument),
            (
                PersistenceError::RootNotAllowed,
                StatusKind::PermissionDenied,
            ),
            (
                PersistenceError::PrivilegedAccount,
                StatusKind::PermissionDenied,
            ),
            (
                PersistenceError::NoCredentials,
                StatusKind::PermissionDenied,
            ),
            (PersistenceError::AccessDenied, StatusKind::PermissionDenied),
            (
                PersistenceError::CredentialsRejected,
                StatusKind::PermissionDenied,
            ),
            (PersistenceError::NotFound, StatusKind::NotFound),
            (PersistenceError::ArchiveMissing, StatusKind::NotFound),
            (PersistenceError::Busy, StatusKind::FailedPrecondition),
            (
                PersistenceError::RegionUnknown,
                StatusKind::FailedPrecondition,
            ),
            (PersistenceError::DiskFull, StatusKind::ResourceExhausted),
            (PersistenceError::Store, StatusKind::Internal),
            (PersistenceError::ChecksumMismatch, StatusKind::Internal),
            (PersistenceError::Suspending, StatusKind::Unavailable),
            (PersistenceError::Cancelled, StatusKind::Cancelled),
        ];
        for (error, kind) in cases {
            assert_eq!(error.status_kind(), kind, "{error}");
        }
        assert_eq!(PersistenceError::DiskFull.stream_code(), "internal");
        assert_eq!(PersistenceError::Busy.stream_code(), "internal");
        assert_eq!(
            PersistenceError::AccessDenied.stream_code(),
            "permission_denied"
        );
        assert_eq!(
            PersistenceError::PrivilegedAccount.stream_code(),
            "permission_denied"
        );
        assert_eq!(PersistenceError::NotFound.stream_code(), "not_found");
        assert_eq!(
            PersistenceError::WrongRegion.stream_code(),
            "invalid_argument"
        );
        assert_eq!(PersistenceError::Suspending.stream_code(), "suspending");
    }

    #[test]
    fn port_errors_map_to_domain_errors() {
        assert_eq!(
            PersistenceError::from_store(&StoreError::new(StoreErrorKind::AccessDenied)),
            PersistenceError::AccessDenied
        );
        assert_eq!(
            PersistenceError::from_store(&StoreError::new(StoreErrorKind::NotFound)),
            PersistenceError::NotFound
        );
        assert_eq!(
            PersistenceError::from_archive(&ArchiveError::DiskFull),
            PersistenceError::DiskFull
        );
        assert_eq!(
            PersistenceError::from_archive(&ArchiveError::Io {
                operation: "open",
                errno: "EIO".to_owned()
            }),
            PersistenceError::Archive { operation: "open" }
        );
    }
}
