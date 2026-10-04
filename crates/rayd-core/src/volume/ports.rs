//! What `rayd` needs from the outside to mount an EFS volume (research doc
//! §4.2 `VolumeMounter`). `dyn`-safe (the adapter is held behind
//! `Arc<dyn VolumeMounter>` the same way other ports in this codebase are),
//! so it returns boxed futures rather than using `async fn` in the trait; no
//! tokio, nix or process type crosses into `rayd-core` itself.
//! `rayd::adapters::efs_mount::EfsUtilsMounter` is the real implementation
//! (`mount -t efs` through `amazon-efs-utils`); `features::efs_volumes`
//! holds it as the slot's mounter, so `Health.features.efs_volumes` is this
//! port's `support()`.

use std::future::Future;
use std::pin::Pin;
use std::time::{Duration, SystemTime};

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
    /// The mount path stopped naming a plain directory chain (a component
    /// became a symlink) between the request and the mount: the adapter
    /// walks it without following links and refuses instead of mounting
    /// over whatever the link points to.
    InvalidPath,
}

impl MountFailureClass {
    /// `EfsVolumeStatus.last_error_class` and `ConfigureResponse.error_class`
    /// for this failure, as `efs_volumes.proto` documents them.
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Network => "network",
            Self::IamDenied => "iam_denied",
            Self::NotFound => "not_found",
            Self::Tls => "tls",
            Self::HelperMissing => "helper_missing",
            Self::Timeout => "timeout",
            Self::InvalidPath => "invalid_path",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct MountFailure {
    pub class: MountFailureClass,
}

/// What a successful `mount` hands back for the slot to keep alongside the
/// mount. `credentials_expire_at` is the expiry of the execution-role
/// credentials the mount helper signed the TLS tunnel with (`None` when the
/// adapter could not learn it): `efs-proxy` keeps using them for every
/// reconnection, and nothing renews them without `systemd`'s watchdog, so a
/// suspension that crosses this instant leaves the volume answering
/// `Permission denied` until it is remounted (`AWS_API_NOTES.md` §16 Q129,
/// EFS-12). `super::resume::plan_resume` reads it at `/resume`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct MountReceipt {
    pub credentials_expire_at: Option<SystemTime>,
}

/// Whether this build of `rayd` can mount EFS at all, decided once at
/// startup (research doc §4.1 rule 1: `CAP_SYS_ADMIN`, `nfs4` in
/// `/proc/filesystems`, the mount helper and `efs-proxy` installed).
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

/// How one volume's bounded `/suspend` flush ended (`AWS_API_NOTES.md` §16
/// Q130, EFS-13).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum FlushOutcome {
    /// Every dirty page of the volume reached EFS before the deadline.
    Flushed,
    /// `syncfs` returned an error: the kernel could not write some pages.
    Failed,
    /// The deadline expired with `syncfs` still running (an unreachable
    /// mount target): whatever it had not written yet is lost if the
    /// platform terminates the `MicroVM`, which Q130 measured it does.
    TimedOut,
    /// The `syncfs` of an earlier `/suspend` on this volume never
    /// returned; another one would only queue behind it.
    InFlight,
}

impl FlushOutcome {
    /// `true` when the volume may still hold writes EFS never received.
    #[must_use]
    pub fn left_unflushed(self) -> bool {
        matches!(self, Self::Failed | Self::TimedOut | Self::InFlight)
    }
}

/// How `unmount` detaches a mount point. One variant today, kept as an enum
/// (not a bool) because `/terminate` and a failed `/resume` remount may
/// need a forced `umount2(MNT_FORCE)` variant later, once an unreachable
/// mount target at unmount time has been measured.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum UnmountMode {
    /// `umount -l`: detaches immediately, finishes once nothing still has
    /// the path open (research doc §4.3 `/terminate`, `/resume` remount).
    Lazy,
}

/// Mounts, unmounts, probes and flushes one EFS access point. Every method
/// runs as root (research doc §4.1 rule 5); the adapter never blocks the
/// async runtime on a hung NFS call — a probe, mount or flush this port
/// returns a future for runs inside a budget the caller passes or enforces
/// (`suspend_sync::SuspendShares` for `/suspend`, the participant caps of
/// `/resume`/`/terminate`, a fixed timeout for the mount helper).
pub trait VolumeMounter: Send + Sync {
    fn support(&self) -> MountSupport;

    fn mount(&self, spec: &VolumeSpec) -> BoxFuture<'_, Result<MountReceipt, MountFailure>>;

    /// Detaches `path` and stops whatever the mount left running for it
    /// (each `mount -t efs` starts its own `efs-proxy`, and `umount` does
    /// not stop it: `AWS_API_NOTES.md` §16 Q128).
    fn unmount(
        &self,
        path: &MountPath,
        mode: UnmountMode,
    ) -> BoxFuture<'_, Result<(), MountFailure>>;

    fn probe(&self, path: &MountPath, budget: Duration) -> BoxFuture<'_, ProbeOutcome>;

    /// Writes the volume's dirty pages back to EFS, waiting at most
    /// `deadline`.
    fn flush(&self, path: &MountPath, deadline: Duration) -> BoxFuture<'_, FlushOutcome>;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_failure_class_has_a_distinct_wire_string() {
        let classes = [
            MountFailureClass::Network,
            MountFailureClass::IamDenied,
            MountFailureClass::NotFound,
            MountFailureClass::Tls,
            MountFailureClass::HelperMissing,
            MountFailureClass::Timeout,
            MountFailureClass::InvalidPath,
        ];
        let mut names: Vec<_> = classes.iter().map(|class| class.as_str()).collect();
        names.sort_unstable();
        names.dedup();
        assert_eq!(names.len(), classes.len());
    }

    #[test]
    fn only_a_completed_flush_leaves_nothing_behind() {
        assert!(!FlushOutcome::Flushed.left_unflushed());
        assert!(FlushOutcome::Failed.left_unflushed());
        assert!(FlushOutcome::TimedOut.left_unflushed());
        assert!(FlushOutcome::InFlight.left_unflushed());
    }
}
