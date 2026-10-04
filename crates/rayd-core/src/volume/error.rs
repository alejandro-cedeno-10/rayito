//! The closed error table a volume mount can fail with. Variants never
//! carry an AWS message, a file-system id, an access-point id or a path
//! (research doc §4.1 rule 8, §8 "Modos de fallo"): `rayd`'s adapter
//! classifies the mount helper's stderr into one of these before it ever
//! reaches a log line or `ConfigureResponse.error_class`.

use thiserror::Error;

/// Rejected before any mount attempt: the request itself is malformed.
#[derive(Debug, Error, Clone, Copy, PartialEq, Eq)]
pub enum VolumeError {
    /// The mount path fails `spec::validate_mount_path` (not absolute, not
    /// canonical, or outside `/mnt/`·`/home/user/`).
    #[error("ruta de montaje inválida")]
    InvalidPath,
    /// Two requested mounts share or nest under the same path.
    #[error("rutas de montaje solapadas")]
    Overlap,
    /// More than `plan::MAX_VOLUMES_PER_SANDBOX` volumes in one plan.
    #[error("demasiados volúmenes")]
    TooMany,
    /// A `file_system_id` or `access_point_id` does not match the EFS
    /// identifier pattern (research doc R3 / §4.1 rule 3).
    #[error("identificador de EFS inválido")]
    InvalidIdentifier,
    /// The file system is not on the image's allow-list
    /// (`RAYITO_EFS_ALLOWED_FILE_SYSTEMS`, research doc §4.1 rule 4):
    /// a sandbox's access token cannot point `volumes=` at an arbitrary
    /// file system.
    #[error("sistema de ficheros no permitido por la imagen")]
    NotAllowed,
    /// This agent build has no working `VolumeMounter`
    /// (`UnavailableEfsMounter`, ahead of the measurement campaign).
    #[error("volúmenes EFS no soportados en este build")]
    Unsupported,
}
