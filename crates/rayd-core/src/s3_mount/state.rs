//! What one mount currently is, as served by `ConfigureStatus`
//! (`S3MountState` of the proto) and by `Health` indirectly (a failed mount
//! never blocks `/ready`: s3-mounts has no `LifecycleParticipant::ready_gate`
//! override, so `/ready`'s existing decision is untouched — see
//! `rayd::features::s3_mounts`).

use super::error::MountErrorClass;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MountPhase {
    /// Accepted; the FUSE attach or the `mount-s3` daemon has not finished
    /// settling yet.
    Pending,
    Mounted,
    Failed,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MountState {
    pub mount_path: String,
    pub phase: MountPhase,
    /// `Some` only when `phase == Failed`.
    pub error_class: Option<MountErrorClass>,
}

impl MountState {
    #[must_use]
    pub fn pending(mount_path: impl Into<String>) -> Self {
        Self {
            mount_path: mount_path.into(),
            phase: MountPhase::Pending,
            error_class: None,
        }
    }

    #[must_use]
    pub fn mounted(mount_path: impl Into<String>) -> Self {
        Self {
            mount_path: mount_path.into(),
            phase: MountPhase::Mounted,
            error_class: None,
        }
    }

    #[must_use]
    pub fn failed(mount_path: impl Into<String>, class: MountErrorClass) -> Self {
        Self {
            mount_path: mount_path.into(),
            phase: MountPhase::Failed,
            error_class: Some(class),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_failed_carries_an_error_class() {
        assert_eq!(MountState::pending("/mnt/a").error_class, None);
        assert_eq!(MountState::mounted("/mnt/a").error_class, None);
        assert_eq!(
            MountState::failed("/mnt/a", MountErrorClass::Timeout).error_class,
            Some(MountErrorClass::Timeout)
        );
    }

    #[test]
    fn constructors_set_the_matching_phase() {
        assert_eq!(MountState::pending("/mnt/a").phase, MountPhase::Pending);
        assert_eq!(MountState::mounted("/mnt/a").phase, MountPhase::Mounted);
        assert_eq!(
            MountState::failed("/mnt/a", MountErrorClass::Network).phase,
            MountPhase::Failed
        );
    }
}
