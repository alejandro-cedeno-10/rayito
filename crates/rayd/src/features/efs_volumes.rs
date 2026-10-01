//! Slot for `m15-efs-volumes` (ADR-018, experimental). The domain
//! (`rayd_core::volume`) and the `VolumeMounter` port are real, and this
//! crate ships a real, tested adapter for it
//! (`adapters::efs_mount::UnavailableEfsMounter`) — but that adapter always
//! reports `MountSupport::Unsupported`, ahead of the EFS-1..EFS-20
//! measurement campaign
//! (`docs/research/2026-10-efs-persistence.md`). Wiring a real
//! `ConfigSection::EfsVolumes` dispatch (proto `EfsVolumesConfig` ->
//! `VolumeSpec`) onto an adapter that can only ever refuse would just be
//! dead code with no behaviour to show for it, so this slot stays
//! `slot::Unsupported`: `Health.features.efs_volumes` is `false` and the
//! `efs_volumes` section of `ConfigureRequest` always answers
//! `SECTION_CODE_UNSUPPORTED`. The campaign's first stop criterion that
//! passes (EFS-2, EFS-3 or EFS-8) is what justifies replacing `build()`
//! here with one that wraps a real mounter; `features::FeatureSet::efs_volumes`
//! and this module's path do not change for that.

use std::sync::Arc;

use rayito_proto::v1::{EfsVolumesConfig, EfsVolumesStatus};

use super::FeatureContext;
use super::slot::{ConfigurableFeature, Unsupported};

#[must_use]
pub fn build(
    _ctx: &FeatureContext,
) -> Arc<dyn ConfigurableFeature<EfsVolumesConfig, EfsVolumesStatus>> {
    Arc::new(Unsupported)
}
