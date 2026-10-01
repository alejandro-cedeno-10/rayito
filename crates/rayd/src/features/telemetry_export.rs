//! Stub slot for `m15-rayd-otlp` (M15 foundations). The feature that
//! implements rayd-otlp replaces `build()` with a real adapter
//! (`adapters::{otlp_codec,cloudwatch_otlp_sink}`);
//! `features::FeatureSet::telemetry_export` and this module's path never
//! change for that.

use std::sync::Arc;

use rayito_proto::v1::{TelemetryExportConfig, TelemetryExportStatus};

use super::FeatureContext;
use super::slot::{ConfigurableFeature, Unsupported};

#[must_use]
pub fn build(
    _ctx: &FeatureContext,
) -> Arc<dyn ConfigurableFeature<TelemetryExportConfig, TelemetryExportStatus>> {
    Arc::new(Unsupported)
}
