//! Stub slot for `m15-events-webhooks` (M15 foundations). The feature that
//! implements events-webhooks replaces `build()` with a real adapter
//! (`adapters::stdout_event_sink`); `features::FeatureSet::lifecycle_events`
//! and this module's path never change for that.

use std::sync::Arc;

use rayito_proto::v1::{LifecycleEventsConfig, LifecycleEventsStatus};

use super::FeatureContext;
use super::slot::{ConfigurableFeature, Unsupported};

#[must_use]
pub fn build(
    _ctx: &FeatureContext,
) -> Arc<dyn ConfigurableFeature<LifecycleEventsConfig, LifecycleEventsStatus>> {
    Arc::new(Unsupported)
}
