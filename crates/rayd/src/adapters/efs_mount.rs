//! EFS mount adapter (`m15-efs-volumes`, ADR-018, experimental). Ahead of
//! the measurement campaign (`docs/research/2026-10-efs-persistence.md`,
//! EFS-1..EFS-20) this crate ships exactly one implementation,
//! `UnavailableEfsMounter`: it never calls `mount(2)` and always reports
//! `MountSupport::Unsupported`, so `Health.features.efs_volumes` stays
//! `false` and `ConfigureSandbox`'s `efs_volumes` section always answers
//! `SECTION_CODE_UNSUPPORTED` (the `rayd_core::volume::VolumeMounter` port
//! itself is real and tested — see that module's docs). Once EFS-2
//! (mount in caps), EFS-3 (own connector reaches 2049) and EFS-8
//! (`efs-utils` with TLS+IAM+access point, no `systemd`) clear, a real
//! `mount -t efs` adapter replaces this one; `features::efs_volumes::build`
//! is the only call site that changes.

use std::time::Duration;

use rayd_core::volume::{
    BoxFuture, MountFailure, MountFailureClass, MountPath, MountSupport, ProbeOutcome, UnmountMode,
    VolumeMounter, VolumeSpec,
};

/// Always-unsupported `VolumeMounter`. Zero-cost by construction: it holds
/// no state, spawns no process and opens no socket.
#[derive(Debug, Default, Clone, Copy)]
pub struct UnavailableEfsMounter;

impl VolumeMounter for UnavailableEfsMounter {
    fn support(&self) -> MountSupport {
        MountSupport::Unsupported
    }

    fn mount(&self, _spec: &VolumeSpec) -> BoxFuture<'_, Result<(), MountFailure>> {
        Box::pin(async {
            Err(MountFailure {
                class: MountFailureClass::HelperMissing,
            })
        })
    }

    fn unmount(
        &self,
        _path: &MountPath,
        _mode: UnmountMode,
    ) -> BoxFuture<'_, Result<(), MountFailure>> {
        Box::pin(async {
            Err(MountFailure {
                class: MountFailureClass::HelperMissing,
            })
        })
    }

    fn probe(&self, _path: &MountPath, _budget: Duration) -> BoxFuture<'_, ProbeOutcome> {
        Box::pin(async { ProbeOutcome::Gone })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rayd_core::volume::{AccessPointId, FileSystemId};

    fn spec() -> VolumeSpec {
        VolumeSpec {
            file_system_id: FileSystemId::parse("fs-0123abcd").unwrap(),
            access_point_id: AccessPointId::parse("fsap-0123abcd").unwrap(),
            mount_path: MountPath::parse("/mnt/data").unwrap(),
            read_only: true,
            mount_target_ip: None,
        }
    }

    #[test]
    fn it_never_claims_support() {
        assert_eq!(UnavailableEfsMounter.support(), MountSupport::Unsupported);
    }

    #[tokio::test]
    async fn mount_always_fails_with_helper_missing() {
        let result = UnavailableEfsMounter.mount(&spec()).await;
        assert_eq!(
            result,
            Err(MountFailure {
                class: MountFailureClass::HelperMissing
            })
        );
    }

    #[tokio::test]
    async fn unmount_always_fails_with_helper_missing() {
        let path = MountPath::parse("/mnt/data").unwrap();
        let result = UnavailableEfsMounter
            .unmount(&path, UnmountMode::Lazy)
            .await;
        assert_eq!(
            result,
            Err(MountFailure {
                class: MountFailureClass::HelperMissing
            })
        );
    }

    #[tokio::test]
    async fn a_probe_always_finds_the_mount_gone() {
        let path = MountPath::parse("/mnt/data").unwrap();
        let outcome = UnavailableEfsMounter
            .probe(&path, Duration::from_secs(5))
            .await;
        assert_eq!(outcome, ProbeOutcome::Gone);
    }
}
