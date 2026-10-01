//! What `rayd` needs from the outside to mount an EFS volume (research doc
//! §4.2 `VolumeMounter`). `dyn`-safe (the `features::efs_volumes` slot
//! stores it behind `Arc<dyn VolumeMounter>`), so it returns boxed futures
//! rather than using `async fn` in the trait; no tokio, nix or process type
//! crosses into `rayd-core` itself.

use std::future::Future;
use std::pin::Pin;
use std::time::Duration;

use super::spec::{MountPath, VolumeSpec};

/// A future a `dyn VolumeMounter` can return.
pub type BoxFuture<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

/// Closed classes a mount failure falls into (research doc §4.2
/// `VolumeError::MountFailed{class}`, §8 "Modos de fallo"); never an AWS
/// message, a host or a path.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum MountFailureClass {
    Network,
    IamDenied,
    NotFound,
    Tls,
    HelperMissing,
    Timeout,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct MountFailure {
    pub class: MountFailureClass,
}

/// Whether this build of `rayd` can mount EFS at all, decided once at
/// startup (research doc §4.1 rule 1: `CAP_SYS_ADMIN`, `nfs4` in
/// `/proc/filesystems`, the mount helper installed). `UnavailableEfsMounter`
/// always answers `Unsupported`, ahead of the EFS-1..EFS-20 campaign.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum MountSupport {
    Supported,
    Unsupported,
}

/// What a liveness probe of an already-mounted path finds (research doc
/// §4.3 `/resume`): `Healthy` needs nothing, `Stale`/`Hung` trigger a
/// remount, `Gone` means the mount point itself disappeared.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ProbeOutcome {
    Healthy,
    Stale,
    Hung,
    Gone,
}

/// Whether a mount should reject writes or allow them, as a raw mount
/// option — not the source of truth for isolation, which is the execution
/// role's IAM condition on `elasticfilesystem:AccessPointArn`
/// (research doc §4.6).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum UnmountMode {
    /// `umount -l`: detaches immediately, finishes once nothing still has
    /// the path open (research doc §4.3 `/terminate`, `/resume` remount).
    Lazy,
}

/// Mounts, unmounts and probes one EFS access point. Every method runs as
/// root (research doc §4.1 rule 5); the adapter never blocks the async
/// runtime on a hung NFS call — a probe or mount this port returns a future
/// for runs inside a budget the caller enforces (`suspend_sync::SuspendShares`
/// for `/suspend`, a fixed timeout for `/run`).
pub trait VolumeMounter: Send + Sync {
    fn support(&self) -> MountSupport;

    fn mount(&self, spec: &VolumeSpec) -> BoxFuture<'_, Result<(), MountFailure>>;

    fn unmount(&self, path: &MountPath, mode: UnmountMode) -> BoxFuture<'_, Result<(), MountFailure>>;

    fn probe(&self, path: &MountPath, budget: Duration) -> BoxFuture<'_, ProbeOutcome>;
}
