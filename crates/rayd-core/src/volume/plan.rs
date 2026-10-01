//! A validated, deterministic set of volumes for one `ConfigureSandbox`
//! call (research doc §4.2 `VolumePlan`): no two mounts overlap, there are
//! at most `spec::MAX_VOLUMES_PER_SANDBOX` of them, and they are always
//! walked in the same order (by mount path) so retries and status reports
//! are reproducible.

use super::error::VolumeError;
use super::spec::{MAX_VOLUMES_PER_SANDBOX, VolumeSpec};

/// Why `VolumePlan::build` refused a set of specs, naming which one.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum VolumePlanError {
    TooMany { requested: usize },
    Overlap { first: String, second: String },
}

impl From<&VolumePlanError> for VolumeError {
    fn from(value: &VolumePlanError) -> Self {
        match value {
            VolumePlanError::TooMany { .. } => VolumeError::TooMany,
            VolumePlanError::Overlap { .. } => VolumeError::Overlap,
        }
    }
}

/// `specs()` is always sorted by `mount_path`, so two equivalent requests
/// (same mounts, different order) produce the same plan.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VolumePlan {
    specs: Vec<VolumeSpec>,
}

impl VolumePlan {
    /// Validates `specs` as a whole (count, then pairwise overlap) and
    /// returns them sorted by mount path. Each individual `VolumeSpec` is
    /// assumed already validated (`spec::MountPath::parse` and friends);
    /// this only checks properties of the *set*.
    pub fn build(mut specs: Vec<VolumeSpec>) -> Result<Self, VolumePlanError> {
        if specs.len() > MAX_VOLUMES_PER_SANDBOX {
            return Err(VolumePlanError::TooMany {
                requested: specs.len(),
            });
        }
        specs.sort_by(|a, b| a.mount_path.as_str().cmp(b.mount_path.as_str()));
        for pair in specs.windows(2) {
            let [first, second] = pair else {
                unreachable!()
            };
            if first.mount_path.overlaps(&second.mount_path) {
                return Err(VolumePlanError::Overlap {
                    first: first.mount_path.as_str().to_owned(),
                    second: second.mount_path.as_str().to_owned(),
                });
            }
        }
        Ok(Self { specs })
    }

    #[must_use]
    pub fn specs(&self) -> &[VolumeSpec] {
        &self.specs
    }

    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.specs.is_empty()
    }
}

#[cfg(test)]
mod tests {
    use super::super::spec::{AccessPointId, FileSystemId, MountPath};
    use super::*;

    fn spec(path: &str) -> VolumeSpec {
        VolumeSpec {
            file_system_id: FileSystemId::parse("fs-0123abcd").unwrap(),
            access_point_id: AccessPointId::parse("fsap-0123abcd").unwrap(),
            mount_path: MountPath::parse(path).unwrap(),
            read_only: false,
            mount_target_ip: None,
        }
    }

    #[test]
    fn an_empty_plan_builds() {
        let plan = VolumePlan::build(vec![]).unwrap();
        assert!(plan.is_empty());
    }

    #[test]
    fn specs_come_back_sorted_by_mount_path() {
        let plan = VolumePlan::build(vec![spec("/mnt/z"), spec("/mnt/a")]).unwrap();
        let paths: Vec<_> = plan.specs().iter().map(|s| s.mount_path.as_str()).collect();
        assert_eq!(paths, ["/mnt/a", "/mnt/z"]);
    }

    #[test]
    fn more_than_the_cap_is_rejected() {
        let specs = (0..5).map(|i| spec(&format!("/mnt/v{i}"))).collect();
        assert_eq!(
            VolumePlan::build(specs),
            Err(VolumePlanError::TooMany { requested: 5 })
        );
    }

    #[test]
    fn exactly_the_cap_is_accepted() {
        let specs = (0..4).map(|i| spec(&format!("/mnt/v{i}"))).collect();
        assert!(VolumePlan::build(specs).is_ok());
    }

    #[test]
    fn overlapping_mounts_are_rejected() {
        let err = VolumePlan::build(vec![spec("/mnt/data"), spec("/mnt/data/sub")]).unwrap_err();
        assert_eq!(
            err,
            VolumePlanError::Overlap {
                first: "/mnt/data".to_owned(),
                second: "/mnt/data/sub".to_owned(),
            }
        );
    }

    #[test]
    fn plan_error_maps_to_the_right_volume_error() {
        assert_eq!(
            VolumeError::from(&VolumePlanError::TooMany { requested: 5 }),
            VolumeError::TooMany
        );
        assert_eq!(
            VolumeError::from(&VolumePlanError::Overlap {
                first: "/mnt/a".to_owned(),
                second: "/mnt/b".to_owned(),
            }),
            VolumeError::Overlap
        );
    }
}
