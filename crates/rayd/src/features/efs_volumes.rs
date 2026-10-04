//! Slot for `m15-efs-volumes` (ADR-018, experimental), wired to a
//! `VolumeMounter` (`rayd_core::volume`). `supported()` is exactly the
//! mounter's own `support()`, so `Health.features.efs_volumes` comes from
//! the adapter and never from a hard-coded flag. This build's only adapter
//! is `adapters::efs_mount::UnavailableEfsMounter` (always
//! `Unsupported`, ahead of the EFS-1..EFS-20 measurement campaign,
//! `docs/research/2026-10-efs-persistence.md`), so in every shipped build
//! `apply()` answers `SECTION_CODE_UNSUPPORTED` before looking at the
//! section, exactly like `slot::Unsupported` did.
//!
//! Behind a mounter that does report support (only the fakes in this
//! module's tests today), a present `EfsVolumesConfig` replaces the whole
//! desired set (`configure.proto` semantics): the section is validated
//! into a `VolumePlan` first (`Invalid`, nothing touched, on any bad path,
//! id or overlap), previously mounted paths the new plan drops are
//! unmounted lazily, and each spec is mounted in plan order; the first
//! failure answers `Failed` with its closed class. Swapping in a real
//! `mount -t efs` adapter is then `build()`'s one line plus whatever
//! `/suspend`/`/resume` participation the campaign's EFS-11/EFS-13 call
//! for — this module's dispatch does not change.

use std::sync::{Arc, Mutex, PoisonError};

use rayd_core::configure::{SectionCode, SectionOutcome};
use rayd_core::volume::{
    AccessPointId, FileSystemId, MountFailureClass, MountPath, MountState, MountSupport,
    MountTargetIp, UnmountMode, VolumeError, VolumeMounter, VolumePlan, VolumeSpec,
};
use rayito_proto::v1::{
    EfsVolumeMount, EfsVolumeState, EfsVolumeStatus, EfsVolumesConfig, EfsVolumesStatus,
};

use super::FeatureContext;
use super::slot::ConfigurableFeature;
use crate::adapters::UnavailableEfsMounter;

#[must_use]
pub fn build(
    _ctx: &FeatureContext,
) -> Arc<dyn ConfigurableFeature<EfsVolumesConfig, EfsVolumesStatus>> {
    Arc::new(EfsVolumesSlot::new(Arc::new(UnavailableEfsMounter)))
}

/// One path of the last applied plan: what was asked and where it stands.
#[derive(Clone)]
struct Entry {
    spec: VolumeSpec,
    status: EfsVolumeStatus,
}

impl Entry {
    fn new(spec: &VolumeSpec, state: MountState, error: Option<&str>) -> Self {
        Self {
            spec: spec.clone(),
            status: EfsVolumeStatus {
                mount_path: spec.mount_path.as_str().to_owned(),
                state: wire_state(state) as i32,
                last_error_class: error.unwrap_or_default().to_owned(),
            },
        }
    }

    fn is_mounted(&self) -> bool {
        self.status.state == EfsVolumeState::Mounted as i32
    }
}

/// The `efs_volumes` slot over any `VolumeMounter`.
pub struct EfsVolumesSlot {
    mounter: Arc<dyn VolumeMounter>,
    /// The last applied plan, in plan order.
    entries: Mutex<Vec<Entry>>,
}

impl EfsVolumesSlot {
    #[must_use]
    pub fn new(mounter: Arc<dyn VolumeMounter>) -> Self {
        Self {
            mounter,
            entries: Mutex::new(Vec::new()),
        }
    }

    fn snapshot(&self) -> Vec<Entry> {
        self.entries
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .clone()
    }

    fn replace(&self, entries: Vec<Entry>) {
        *self.entries.lock().unwrap_or_else(PoisonError::into_inner) = entries;
    }

    /// Lazily unmounts every mounted entry whose exact spec the new plan
    /// no longer asks for (dropped, or changed and about to be remounted);
    /// a failure here never blocks the new plan.
    async fn unmount_dropped(&self, previous: &[Entry], plan: &VolumePlan) {
        for entry in previous.iter().filter(|entry| entry.is_mounted()) {
            if !plan.specs().contains(&entry.spec) {
                let _best_effort = self
                    .mounter
                    .unmount(&entry.spec.mount_path, UnmountMode::Lazy)
                    .await;
            }
        }
    }

    /// Mounts each spec in plan order, keeping an identical one that is
    /// already mounted as it is; stops at the first failure.
    async fn mount_all(&self, previous: &[Entry], plan: &VolumePlan) -> SectionOutcome {
        let mut entries: Vec<Entry> = plan
            .specs()
            .iter()
            .map(|spec| Entry::new(spec, MountState::Requested, None))
            .collect();
        for (index, spec) in plan.specs().iter().enumerate() {
            let unchanged = previous
                .iter()
                .any(|entry| entry.is_mounted() && entry.spec == *spec);
            if unchanged {
                entries[index] = Entry::new(spec, MountState::Mounted, None);
                continue;
            }
            match self.mounter.mount(spec).await {
                Ok(()) => entries[index] = Entry::new(spec, MountState::Mounted, None),
                Err(failure) => {
                    let class = failure_class(failure.class);
                    entries[index] = Entry::new(spec, MountState::Failed, Some(class));
                    self.replace(entries);
                    return outcome(SectionCode::Failed, Some(class));
                }
            }
        }
        self.replace(entries);
        SectionOutcome::applied()
    }
}

#[tonic::async_trait]
impl ConfigurableFeature<EfsVolumesConfig, EfsVolumesStatus> for EfsVolumesSlot {
    fn supported(&self) -> bool {
        self.mounter.support() == MountSupport::Supported
    }

    async fn apply(&self, cfg: EfsVolumesConfig) -> SectionOutcome {
        if !self.supported() {
            return SectionOutcome::unsupported();
        }
        let plan = match plan_from_wire(&cfg.mounts) {
            Ok(plan) => plan,
            Err(error) => return outcome(SectionCode::Invalid, Some(invalid_class(error))),
        };
        let previous = self.snapshot();
        self.unmount_dropped(&previous, &plan).await;
        self.mount_all(&previous, &plan).await
    }

    async fn status(&self) -> EfsVolumesStatus {
        EfsVolumesStatus {
            volumes: self
                .snapshot()
                .into_iter()
                .map(|entry| entry.status)
                .collect(),
        }
    }
}

/// The wire section as a validated plan: every field parsed by the domain
/// (`rayd_core::volume::spec`), then the set checked as a whole.
fn plan_from_wire(mounts: &[EfsVolumeMount]) -> Result<VolumePlan, VolumeError> {
    let specs = mounts
        .iter()
        .map(spec_from_wire)
        .collect::<Result<Vec<_>, _>>()?;
    VolumePlan::build(specs).map_err(|error| VolumeError::from(&error))
}

fn spec_from_wire(mount: &EfsVolumeMount) -> Result<VolumeSpec, VolumeError> {
    let mount_target_ip = if mount.mount_target_ip.is_empty() {
        None
    } else {
        Some(MountTargetIp::parse(&mount.mount_target_ip)?)
    };
    Ok(VolumeSpec {
        file_system_id: FileSystemId::parse(&mount.file_system_id)?,
        access_point_id: AccessPointId::parse(&mount.access_point_id)?,
        mount_path: MountPath::parse(&mount.mount_path)?,
        read_only: mount.read_only,
        mount_target_ip,
    })
}

fn outcome(code: SectionCode, error_class: Option<&str>) -> SectionOutcome {
    SectionOutcome {
        code,
        error_class: error_class.map(str::to_owned),
    }
}

fn wire_state(state: MountState) -> EfsVolumeState {
    match state {
        MountState::Requested => EfsVolumeState::Requested,
        MountState::Mounting => EfsVolumeState::Mounting,
        MountState::Mounted => EfsVolumeState::Mounted,
        MountState::Degraded => EfsVolumeState::Degraded,
        MountState::Remounting => EfsVolumeState::Remounting,
        MountState::Unmounted => EfsVolumeState::Unmounted,
        MountState::Failed => EfsVolumeState::Failed,
    }
}

/// `last_error_class` for `FAILED`, as `efs_volumes.proto` documents it.
fn failure_class(class: MountFailureClass) -> &'static str {
    match class {
        MountFailureClass::Network => "network",
        MountFailureClass::IamDenied => "iam_denied",
        MountFailureClass::NotFound => "not_found",
        MountFailureClass::Tls => "tls",
        MountFailureClass::HelperMissing => "helper_missing",
        MountFailureClass::Timeout => "timeout",
    }
}

/// `error_class` for an `Invalid` section: a closed snake string, never the
/// offending value.
fn invalid_class(error: VolumeError) -> &'static str {
    match error {
        VolumeError::InvalidPath => "invalid_path",
        VolumeError::Overlap => "overlap",
        VolumeError::TooMany => "too_many",
        VolumeError::InvalidIdentifier => "invalid_identifier",
        VolumeError::NotAllowed => "not_allowed",
        VolumeError::Unsupported => "unsupported",
    }
}

#[cfg(test)]
mod tests {
    use std::time::Duration;

    use rayd_core::volume::{BoxFuture, MountFailure, ProbeOutcome};

    use super::*;

    /// A mounter that claims support, records every call and fails the
    /// mount of `fail_path` (if any) with `HelperMissing`.
    #[derive(Default)]
    struct FakeMounter {
        fail_path: Option<&'static str>,
        calls: Mutex<Vec<String>>,
    }

    impl FakeMounter {
        fn calls(&self) -> Vec<String> {
            self.calls.lock().unwrap().clone()
        }
    }

    impl VolumeMounter for FakeMounter {
        fn support(&self) -> MountSupport {
            MountSupport::Supported
        }

        fn mount(&self, spec: &VolumeSpec) -> BoxFuture<'_, Result<(), MountFailure>> {
            let path = spec.mount_path.as_str().to_owned();
            self.calls.lock().unwrap().push(format!("mount {path}"));
            let fails = self.fail_path == Some(path.as_str());
            Box::pin(async move {
                if fails {
                    Err(MountFailure {
                        class: MountFailureClass::HelperMissing,
                    })
                } else {
                    Ok(())
                }
            })
        }

        fn unmount(
            &self,
            path: &MountPath,
            _mode: UnmountMode,
        ) -> BoxFuture<'_, Result<(), MountFailure>> {
            self.calls
                .lock()
                .unwrap()
                .push(format!("unmount {}", path.as_str()));
            Box::pin(async { Ok(()) })
        }

        fn probe(&self, _path: &MountPath, _budget: Duration) -> BoxFuture<'_, ProbeOutcome> {
            Box::pin(async { ProbeOutcome::Healthy })
        }
    }

    fn mount(path: &str) -> EfsVolumeMount {
        EfsVolumeMount {
            mount_path: path.to_owned(),
            file_system_id: "fs-0123abcd".to_owned(),
            access_point_id: "fsap-0123abcd".to_owned(),
            read_only: false,
            mount_target_ip: String::new(),
        }
    }

    fn config(paths: &[&str]) -> EfsVolumesConfig {
        EfsVolumesConfig {
            mounts: paths.iter().map(|path| mount(path)).collect(),
        }
    }

    #[tokio::test]
    async fn the_shipped_slot_follows_the_unavailable_mounter() {
        let slot = build(&FeatureContext::default());
        assert!(!slot.supported());
        let outcome = slot.apply(config(&["/mnt/data"])).await;
        assert_eq!(outcome.code, SectionCode::Unsupported);
        assert_eq!(slot.status().await, EfsVolumesStatus::default());
    }

    #[tokio::test]
    async fn a_supported_mounter_mounts_the_plan_in_path_order() {
        let mounter = Arc::new(FakeMounter::default());
        let slot = EfsVolumesSlot::new(mounter.clone());
        assert!(slot.supported());
        let outcome = slot.apply(config(&["/mnt/b", "/mnt/a"])).await;
        assert_eq!(outcome, SectionOutcome::applied());
        assert_eq!(mounter.calls(), ["mount /mnt/a", "mount /mnt/b"]);
        let states: Vec<_> = slot
            .status()
            .await
            .volumes
            .iter()
            .map(|v| (v.mount_path.clone(), v.state))
            .collect();
        assert_eq!(
            states,
            [
                ("/mnt/a".to_owned(), EfsVolumeState::Mounted as i32),
                ("/mnt/b".to_owned(), EfsVolumeState::Mounted as i32),
            ]
        );
    }

    #[tokio::test]
    async fn an_invalid_section_touches_nothing() {
        let mounter = Arc::new(FakeMounter::default());
        let slot = EfsVolumesSlot::new(mounter.clone());
        let outcome = slot.apply(config(&["/mnt/a", "/mnt/a/nested"])).await;
        assert_eq!(outcome.code, SectionCode::Invalid);
        assert_eq!(outcome.error_class.as_deref(), Some("overlap"));
        let outcome = slot.apply(config(&["relative"])).await;
        assert_eq!(outcome.error_class.as_deref(), Some("invalid_path"));
        assert!(mounter.calls().is_empty());
    }

    #[tokio::test]
    async fn a_failed_mount_answers_failed_with_its_class() {
        let mounter = Arc::new(FakeMounter {
            fail_path: Some("/mnt/b"),
            ..FakeMounter::default()
        });
        let slot = EfsVolumesSlot::new(mounter);
        let outcome = slot.apply(config(&["/mnt/a", "/mnt/b"])).await;
        assert_eq!(outcome.code, SectionCode::Failed);
        assert_eq!(outcome.error_class.as_deref(), Some("helper_missing"));
        let failed = &slot.status().await.volumes[1];
        assert_eq!(failed.state, EfsVolumeState::Failed as i32);
        assert_eq!(failed.last_error_class, "helper_missing");
    }

    #[tokio::test]
    async fn a_changed_spec_is_unmounted_then_mounted_again() {
        let mounter = Arc::new(FakeMounter::default());
        let slot = EfsVolumesSlot::new(mounter.clone());
        slot.apply(config(&["/mnt/a"])).await;
        let mut changed = config(&["/mnt/a"]);
        changed.mounts[0].read_only = true;
        slot.apply(changed).await;
        assert_eq!(
            mounter.calls(),
            ["mount /mnt/a", "unmount /mnt/a", "mount /mnt/a"]
        );
    }

    #[tokio::test]
    async fn a_new_plan_unmounts_only_the_paths_it_drops() {
        let mounter = Arc::new(FakeMounter::default());
        let slot = EfsVolumesSlot::new(mounter.clone());
        slot.apply(config(&["/mnt/a", "/mnt/b"])).await;
        slot.apply(config(&["/mnt/b"])).await;
        assert_eq!(
            mounter.calls(),
            ["mount /mnt/a", "mount /mnt/b", "unmount /mnt/a"]
        );
    }
}
