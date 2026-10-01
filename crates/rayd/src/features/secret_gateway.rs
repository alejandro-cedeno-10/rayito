//! Stub slot for `m15-secrets-gateway` (M15 foundations). The feature that
//! implements secrets-gateway replaces `build()` with a real adapter
//! (`secret_gateway::{listener,upstream}`);
//! `features::FeatureSet::secret_gateway` and this module's path never
//! change for that.

use std::sync::Arc;

use rayito_proto::v1::{SecretGatewayConfig, SecretGatewayStatus};

use super::FeatureContext;
use super::slot::{ConfigurableFeature, Unsupported};

#[must_use]
pub fn build(
    _ctx: &FeatureContext,
) -> Arc<dyn ConfigurableFeature<SecretGatewayConfig, SecretGatewayStatus>> {
    Arc::new(Unsupported)
}
