//! Stub slot for `m15-efs-volumes` (M15 foundations). The feature that
//! implements efs-volumes replaces `build()` with a real adapter
//! (`adapters::efs_mount`, `UnavailableEfsMounter` until the EFS
//! measurement campaign lands); `features::FeatureSet::efs_volumes` and
//! this module's path never change for that.

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
