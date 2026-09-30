//! Domain errors of the filesystem module. Their `Display` strings are what
//! the gRPC adapter sends to clients and writes to logs, so none of them
//! ever quotes a path, an entry name, a symlink target or file bytes.

use thiserror::Error;

use super::path::PathRejection;
use super::ports::{FsIoError, WatchError};
use crate::lifecycle::HookPhase;
use crate::wire_tokens::{DISK_FULL, DISK_RESERVE, METADATA_TOO_LARGE, METADATA_UNSUPPORTED};

#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum FilesystemError {
    #[error("ruta inválida: {0}")]
    InvalidPath(PathRejection),
    #[error("la política deniega la ruta")]
    Denied,
    #[error("no existe el fichero o directorio")]
    NotFound,
    #[error("el directorio ya existe")]
    AlreadyExists,
    #[error("no es un directorio")]
    NotADirectory,
    #[error("la ruta es un directorio")]
    IsADirectory,
    #[error("la ruta no es un fichero regular")]
    NotARegularFile,
    #[error("la ruta es un enlace simbólico")]
    IsSymlink,
    #[error("el directorio no está vacío; usa recursive")]
    NotEmpty,
    #[error("el destino choca con una entrada existente")]
    DestinationConflict,
    #[error("movimiento entre dispositivos")]
    CrossDevice,
    #[error("permiso denegado")]
    PermissionDenied,
    #[error("el fragmento supera {max} bytes")]
    ChunkTooLarge { max: usize },
    #[error("mode {0:o} está fuera de 0..=7777")]
    InvalidMode(u32),
    #[error("el primer mensaje de un fichero debe llevar path")]
    MissingPath,
    #[error("user sólo puede ir en un mensaje con path")]
    UserWithoutPath,
    #[error("mode sólo puede ir en un mensaje con path")]
    ModeWithoutPath,
    #[error("metadata sólo puede ir en un mensaje con path")]
    MetadataWithoutPath,
    /// A key or value outside the rules of `FileMetadata`; the message is
    /// fixed so it never quotes either.
    #[error("metadatos inválidos")]
    InvalidMetadata,
    /// The destination filesystem has no user xattrs; the message is the
    /// status detail the SDK keys on.
    #[error("{METADATA_UNSUPPORTED}")]
    MetadataUnsupported,
    /// The set did not fit the file's xattr space.
    #[error("{METADATA_TOO_LARGE}")]
    MetadataTooLarge,
    #[error("el stream no trajo ficheros")]
    NoFiles,
    #[error("el listado supera {max} entradas; reduce depth")]
    TooManyEntries { max: usize },
    #[error("como máximo {max} watches activos")]
    TooManyWatches { max: usize },
    #[error("se alcanzó el límite de watches de inotify")]
    WatchLimitReached,
    #[error("la cola del watch se desbordó; vuelve a abrir el watch")]
    WatchOverflow,
    #[error("se borró el directorio vigilado")]
    WatchRootGone,
    /// Fewer than `DISK_RESERVE_BYTES` free before a file's temporary is
    /// created; the message is the status detail the SDK keys on.
    #[error("{DISK_RESERVE}")]
    DiskReserve,
    /// `ENOSPC` from a write or a commit; same status, other detail.
    #[error("{DISK_FULL}")]
    DiskFull,
    #[error("esta imagen no permite ejecutar como root")]
    RootNotAllowed,
    #[error(
        "sólo las cuentas sin privilegios de esta imagen pueden tocar ficheros (uid y gid >= 1000, nunca en el grupo 0)"
    )]
    PrivilegedAccount,
    #[error("usuario desconocido")]
    UnknownUser,
    #[error("falló la búsqueda del usuario: {0}")]
    UserLookupFailed(String),
    #[error("{phase}")]
    NotAcceptingStreams { phase: HookPhase },
    #[error("las operaciones de ficheros no se admiten en esta plataforma")]
    Unsupported,
    #[error("falló {operation}: {errno}")]
    Io {
        operation: &'static str,
        errno: String,
    },
}

impl FilesystemError {
    /// The default port-to-domain mapping; callers override the variants
    /// whose meaning depends on the operation (a `NotADirectory` from
    /// `realpath` is `NotFound` for `Stat`, a rename conflict is
    /// `DestinationConflict`).
    #[must_use]
    pub fn from_io(operation: &'static str, error: FsIoError) -> Self {
        match error {
            FsIoError::NotFound => Self::NotFound,
            FsIoError::PermissionDenied => Self::PermissionDenied,
            FsIoError::AlreadyExists => Self::AlreadyExists,
            FsIoError::NotADirectory => Self::NotADirectory,
            FsIoError::IsADirectory => Self::IsADirectory,
            FsIoError::NotEmpty => Self::NotEmpty,
            FsIoError::NotARegularFile => Self::NotARegularFile,
            FsIoError::IsSymlink => Self::IsSymlink,
            FsIoError::CrossDevice => Self::CrossDevice,
            FsIoError::NoSpace => Self::DiskFull,
            FsIoError::Unsupported => Self::Unsupported,
            FsIoError::Other { errno } => Self::Io { operation, errno },
        }
    }

    #[must_use]
    pub fn from_watch(error: WatchError) -> Self {
        match error {
            WatchError::NotFound => Self::NotFound,
            WatchError::NotADirectory => Self::NotADirectory,
            WatchError::PermissionDenied => Self::PermissionDenied,
            WatchError::LimitReached => Self::WatchLimitReached,
            WatchError::Unsupported => Self::Unsupported,
            WatchError::Other(errno) => Self::Io {
                operation: "watch",
                errno,
            },
        }
    }
}

impl From<PathRejection> for FilesystemError {
    fn from(rejection: PathRejection) -> Self {
        Self::InvalidPath(rejection)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn messages_never_quote_input() {
        assert_eq!(
            FilesystemError::InvalidPath(PathRejection::ParentReference).to_string(),
            "ruta inválida: contiene una referencia al directorio padre"
        );
        assert_eq!(
            FilesystemError::Denied.to_string(),
            "la política deniega la ruta"
        );
        assert_eq!(
            FilesystemError::ChunkTooLarge { max: 1_048_576 }.to_string(),
            "el fragmento supera 1048576 bytes"
        );
        assert_eq!(
            FilesystemError::InvalidMode(0o10000).to_string(),
            "mode 10000 está fuera de 0..=7777"
        );
        assert_eq!(
            FilesystemError::TooManyEntries { max: 10_000 }.to_string(),
            "el listado supera 10000 entradas; reduce depth"
        );
        assert_eq!(
            FilesystemError::NotAcceptingStreams {
                phase: HookPhase::Suspending
            }
            .to_string(),
            "suspending"
        );
        assert_eq!(
            FilesystemError::Io {
                operation: "read",
                errno: "EIO".to_owned()
            }
            .to_string(),
            "falló read: EIO"
        );
    }

    #[test]
    fn port_errors_map_to_domain_errors() {
        assert_eq!(
            FilesystemError::from_io("lstat", FsIoError::NotFound),
            FilesystemError::NotFound
        );
        assert_eq!(
            FilesystemError::from_io(
                "open",
                FsIoError::Other {
                    errno: "EIO".to_owned()
                }
            ),
            FilesystemError::Io {
                operation: "open",
                errno: "EIO".to_owned()
            }
        );
        assert_eq!(
            FilesystemError::from_watch(WatchError::LimitReached),
            FilesystemError::WatchLimitReached
        );
        assert_eq!(
            FilesystemError::from_io("write", FsIoError::NoSpace),
            FilesystemError::DiskFull
        );
        assert_eq!(FilesystemError::DiskFull.to_string(), "disk_full");
        assert_eq!(FilesystemError::DiskReserve.to_string(), "disk_reserve");
    }
}
