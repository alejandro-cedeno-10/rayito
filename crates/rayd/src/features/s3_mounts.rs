//! Stub slot for `m15-s3-mounts` (M15 foundations). The feature that
//! implements s3-mounts replaces `build()` with a real adapter
//! (`adapters::{fuse_device,mount_s3}`); `features::FeatureSet::s3_mounts`
//! and this module's path never change for that.

use std::sync::Arc;

use rayito_proto::v1::{S3MountsConfig, S3MountsStatus};

use super::FeatureContext;
use super::slot::{ConfigurableFeature, Unsupported};

#[must_use]
pub fn build(
    _ctx: &FeatureContext,
) -> Arc<dyn ConfigurableFeature<S3MountsConfig, S3MountsStatus>> {
    Arc::new(Unsupported)
}
