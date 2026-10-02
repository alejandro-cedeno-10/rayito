//! The generic seam every 0.6 feature's agent-side slot implements (M15
//! foundations, ADR-015). `features::build` wires one of these per feature
//! from `main`; `grpc::configure::ConfigureGrpc` dispatches each present
//! `ConfigureRequest` section to its slot, in
//! `rayd_core::configure::APPLY_ORDER`. A feature replaces its own stub
//! `build()` with a real adapter in its own PR; neither this trait nor
//! `FeatureSet`'s field list changes for that.

use std::sync::Arc;

use rayd_core::configure::SectionOutcome;
use rayd_core::root_egress::RootEgressClass;

use crate::lifecycle::LifecycleParticipant;

#[tonic::async_trait]
pub trait ConfigurableFeature<Cfg, Status>: Send + Sync {
    /// Whether this agent build has a real adapter for the feature (as
    /// opposed to `Unsupported`). Mirrored in `Health.features` by
    /// `rayd_core::features::AgentFeatures`.
    fn supported(&self) -> bool;

    /// Applies `cfg` as the feature's complete new desired state. Called
    /// only when `ConfigureRequest` carries this feature's section.
    async fn apply(&self, cfg: Cfg) -> SectionOutcome;

    /// Served by `ConfigureService.ConfigureStatus`.
    async fn status(&self) -> Status;

    /// `Some` opts this slot into `/suspend`'s bounded flush and `/ready`'s
    /// combined verdict (`rayd::lifecycle::participants`). `None` (the
    /// default, and always what `Unsupported` returns) means the feature
    /// never runs anything of its own around those hooks.
    fn participant(&self) -> Option<Arc<dyn LifecycleParticipant>> {
        None
    }

    /// The root-egress path this slot opens when it is `supported()`
    /// (`rayd_core::root_egress`), reported in `Health.features.root_egress`
    /// by `FeatureSet::root_egress`. `None` (the default, and always what
    /// `Unsupported` returns) means the feature never sends traffic as
    /// anything other than the uid-1000 routes ADR-012 governs.
    fn root_egress_class(&self) -> Option<RootEgressClass> {
        None
    }
}

/// What every feature's slot starts as before that feature builds a real
/// adapter: unsupported, `apply` always answers `SectionCode::Unsupported`
/// and `status` is the section's zero value.
pub struct Unsupported;

#[tonic::async_trait]
impl<Cfg, Status> ConfigurableFeature<Cfg, Status> for Unsupported
where
    Cfg: Send + 'static,
    Status: Default + Send + 'static,
{
    fn supported(&self) -> bool {
        false
    }

    async fn apply(&self, _cfg: Cfg) -> SectionOutcome {
        SectionOutcome::unsupported()
    }

    async fn status(&self) -> Status {
        Status::default()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rayd_core::configure::SectionCode;

    #[derive(Default, PartialEq, Eq, Debug)]
    struct FakeStatus {
        active: bool,
    }

    #[tokio::test]
    async fn unsupported_never_claims_support_and_answers_unsupported() {
        let slot: Unsupported = Unsupported;
        let feature: &dyn ConfigurableFeature<(), FakeStatus> = &slot;
        assert!(!feature.supported());
        assert_eq!(feature.apply(()).await.code, SectionCode::Unsupported);
        assert_eq!(feature.status().await, FakeStatus::default());
        assert!(feature.participant().is_none());
        assert!(feature.root_egress_class().is_none());
    }
}
