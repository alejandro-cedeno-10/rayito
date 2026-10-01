//! Real slot for `m15-secrets-gateway` (ADR-023, T24): one loopback HTTP
//! listener per configured route (`crate::secret_gateway::listener`),
//! forwarding through the shared HTTPS client
//! (`crate::secret_gateway::upstream`) after a method/path allowlist and a
//! rate limit (`rayd_core::secret_gateway`). No `LifecycleParticipant`:
//! the gateway holds no state that a `/suspend` flush would need to
//! persist (a dropped in-flight request on suspend is retried by the
//! sandbox's own HTTP client, same as any other connection cut by the
//! freeze), so `participant()` stays the trait's default `None`.

use std::sync::Arc;

use rayd_core::configure::{SectionCode, SectionOutcome};
use rayd_core::secret_gateway::vault::SecretValue;
use rayd_core::secret_gateway::{GatewaySpec, GatewaySpecError, RawRoute};
use rayito_proto::v1::{SecretGatewayConfig, SecretGatewayRouteState, SecretGatewayStatus};

use super::FeatureContext;
use super::slot::{ConfigurableFeature, Unsupported};
use crate::secret_gateway::{GatewayRuntime, GatewayUpstream};

pub struct SecretGatewayFeature {
    runtime: GatewayRuntime,
}

impl SecretGatewayFeature {
    fn new(client: Arc<GatewayUpstream>) -> Self {
        Self {
            runtime: GatewayRuntime::new(client),
        }
    }
}

#[tonic::async_trait]
impl ConfigurableFeature<SecretGatewayConfig, SecretGatewayStatus> for SecretGatewayFeature {
    fn supported(&self) -> bool {
        true
    }

    async fn apply(&self, cfg: SecretGatewayConfig) -> SectionOutcome {
        let spec = match parse_spec(cfg) {
            Ok(spec) => spec,
            Err(error) => {
                return SectionOutcome {
                    code: SectionCode::Invalid,
                    error_class: Some(error.as_str().to_owned()),
                };
            }
        };
        match self.runtime.apply(spec).await {
            Ok(()) => SectionOutcome::applied(),
            Err(error) => SectionOutcome {
                code: SectionCode::Failed,
                error_class: Some(error.as_str().to_owned()),
            },
        }
    }

    async fn status(&self) -> SecretGatewayStatus {
        SecretGatewayStatus {
            routes: self
                .runtime
                .status()
                .into_iter()
                .map(|status| rayito_proto::v1::SecretGatewayRouteStatus {
                    name: status.name,
                    port: u32::from(status.port),
                    // Every route `GatewayRuntime::status` reports already
                    // bound its listener (`apply` never commits a route
                    // that failed to start), so it is always `Listening`
                    // here; `last_error_class` is the last *request's*
                    // failure, not the route's own state.
                    state: i32::from(SecretGatewayRouteState::Listening),
                    last_error_class: status.last_error_class.unwrap_or_default().to_owned(),
                })
                .collect(),
        }
    }
}

/// Wire `SecretGatewayConfig` to the pure domain's input shape. Header
/// values are the only field this builds a `SecretValue` from (they
/// arrived over the authenticated `Configure` call, already resolved by
/// the SDK from Secrets Manager); `GatewaySpec::parse` does the rest of
/// the validation.
fn parse_spec(cfg: SecretGatewayConfig) -> Result<GatewaySpec, GatewaySpecError> {
    let mut raw = Vec::with_capacity(cfg.routes.len());
    for route in cfg.routes {
        let mut headers = Vec::with_capacity(route.headers.len());
        for (name, value) in route.headers {
            let value = SecretValue::header_safe(value)
                .map_err(|_| GatewaySpecError::InvalidHeaderValue)?;
            headers.push((name, value));
        }
        let allow = route
            .allow
            .into_iter()
            .map(|rule| (rule.method, rule.path))
            .collect();
        raw.push(RawRoute {
            name: route.name,
            upstream: route.upstream,
            headers,
            allow,
            rate_per_minute: route.rate_per_minute,
        });
    }
    GatewaySpec::parse(raw)
}

/// Mirrors `main::transfer_services`'s own fallback: without a readable OS
/// trust store no HTTPS client in this process can do anything useful, so
/// the slot degrades to `Unsupported` (every `Configure` section against it
/// answers `UNSUPPORTED`) rather than taking `rayd` down at startup.
#[must_use]
pub fn build(
    _ctx: &FeatureContext,
) -> Arc<dyn ConfigurableFeature<SecretGatewayConfig, SecretGatewayStatus>> {
    match GatewayUpstream::new() {
        Ok(client) => Arc::new(SecretGatewayFeature::new(Arc::new(client))),
        Err(error) => {
            tracing::warn!(feature = "secret_gateway", %error, "disabled: no TLS trust store");
            Arc::new(Unsupported)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rayito_proto::v1::{SecretGatewayAllowRule, SecretGatewayRoute};

    fn feature() -> SecretGatewayFeature {
        SecretGatewayFeature::new(Arc::new(GatewayUpstream::new().unwrap()))
    }

    fn route(name: &str) -> SecretGatewayRoute {
        SecretGatewayRoute {
            name: name.to_owned(),
            upstream: "https://example.com".to_owned(),
            headers: [("x-api-key".to_owned(), "sk-abc".to_owned())].into(),
            allow: vec![SecretGatewayAllowRule {
                method: "GET".to_owned(),
                path: "/x".to_owned(),
            }],
            rate_per_minute: 60,
        }
    }

    #[test]
    fn the_slot_is_supported() {
        assert!(feature().supported());
    }

    #[test]
    fn a_fresh_slot_runs_no_listener_before_any_configure_call() {
        // ADR-014 rule 4 / architecture §9.2: without `gateways=` ever
        // reaching this slot, `rayd` opens no loopback socket at all.
        let feature = feature();
        assert!(feature.runtime.status().is_empty());
    }

    #[test]
    fn participant_stays_the_default_none() {
        let feature: &dyn ConfigurableFeature<SecretGatewayConfig, SecretGatewayStatus> =
            &feature();
        assert!(feature.participant().is_none());
    }

    #[tokio::test]
    async fn applying_a_valid_route_reports_applied_and_a_listening_status() {
        let feature = feature();
        let outcome = feature
            .apply(SecretGatewayConfig {
                routes: vec![route("a")],
            })
            .await;
        assert_eq!(outcome.code, SectionCode::Applied);
        let status = feature.status().await;
        assert_eq!(status.routes.len(), 1);
        assert_eq!(status.routes[0].name, "a");
        assert_ne!(status.routes[0].port, 0);
    }

    #[tokio::test]
    async fn applying_an_empty_config_tears_every_route_down() {
        let feature = feature();
        feature
            .apply(SecretGatewayConfig {
                routes: vec![route("a")],
            })
            .await;
        let outcome = feature.apply(SecretGatewayConfig::default()).await;
        assert_eq!(outcome.code, SectionCode::Applied);
        assert!(feature.status().await.routes.is_empty());
    }

    #[tokio::test]
    async fn an_invalid_route_is_rejected_without_disturbing_the_previous_state() {
        let feature = feature();
        feature
            .apply(SecretGatewayConfig {
                routes: vec![route("a")],
            })
            .await;
        let mut bad = route("b");
        bad.upstream = "http://not-https.example.com".to_owned();
        let outcome = feature
            .apply(SecretGatewayConfig { routes: vec![bad] })
            .await;
        assert_eq!(outcome.code, SectionCode::Invalid);
        assert_eq!(outcome.error_class.as_deref(), Some("upstream_not_https"));
        // The previous, still-valid route is untouched (the section's own
        // contents were rejected before anything changed).
        let status = feature.status().await;
        assert_eq!(status.routes.len(), 1);
        assert_eq!(status.routes[0].name, "a");
    }

    #[tokio::test]
    async fn a_crlf_header_value_is_rejected_as_invalid_header_value() {
        let feature = feature();
        let mut bad = route("a");
        bad.headers = [("x-api-key".to_owned(), "sk\r\nX-Evil: 1".to_owned())].into();
        let outcome = feature
            .apply(SecretGatewayConfig { routes: vec![bad] })
            .await;
        assert_eq!(outcome.code, SectionCode::Invalid);
        assert_eq!(outcome.error_class.as_deref(), Some("invalid_header_value"));
    }
}
