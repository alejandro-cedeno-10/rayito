//! Stub slot for `m15-templates` (M15 foundations). Unlike the other five
//! features, `template_start` has no `ConfigureSandbox` section (reading
//! `/etc/rayito/template.json` happens at boot, not through
//! `ConfigureService`): this slot's `Cfg`/`Status` are `()` only to fit the
//! shared `ConfigurableFeature` shape for `supported()` and
//! `participant()` (a template's `ready_cmd` is a natural
//! `LifecycleParticipant::ready_gate`). The templates feature may narrow
//! this slot's type in its own PR; `ConfigureGrpc` never dispatches to it
//! (`configure.proto`'s `ConfigureRequest` has no `template_start` field).

use std::sync::Arc;

use super::FeatureContext;
use super::slot::{ConfigurableFeature, Unsupported};

#[must_use]
pub fn build(_ctx: &FeatureContext) -> Arc<dyn ConfigurableFeature<(), ()>> {
    Arc::new(Unsupported)
}
