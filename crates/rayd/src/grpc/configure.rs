//! `ConfigureService` (M15 foundations, ADR-015): dispatches each present
//! section of a `ConfigureRequest` to its slot in `FeatureSet`, in
//! `rayd_core::configure::APPLY_ORDER`. Never logs or `Debug`-prints the
//! request (a future section may carry a pushed secret-gateway value).
//! Accepted only while the sandbox is `RUNNING`/`RESUMED`
//! (`rayd_core::configure::is_configurable`); otherwise
//! `FAILED_PRECONDITION not_running`, like every other phase-gated RPC's
//! own detail string.

use std::sync::Arc;

use rayd_core::configure::{self, ConfigSection, NOT_RUNNING_DETAIL, SectionCode, SectionOutcome};
use rayd_core::session::SandboxSession;
use rayito_proto::v1::configure_service_server::ConfigureService;
use rayito_proto::v1::{
    ConfigureRequest, ConfigureResponse, ConfigureStatusRequest, ConfigureStatusResponse,
    SectionResult,
};
use tonic::{Request, Response, Status};

use crate::features::FeatureSet;

pub struct ConfigureGrpc {
    session: Arc<SandboxSession>,
    features: Arc<FeatureSet>,
}

impl ConfigureGrpc {
    #[must_use]
    pub fn new(session: Arc<SandboxSession>, features: Arc<FeatureSet>) -> Self {
        Self { session, features }
    }

    /// `None` when `request` leaves this section untouched; `Some` applies
    /// it to the matching `FeatureSet` slot. Written as one match per
    /// section rather than a generic helper: each arm names a different
    /// concrete `Cfg`/`Status` pair, so there is no shared body to extract
    /// without boxing every request first.
    async fn apply_section(
        &self,
        section: ConfigSection,
        request: &ConfigureRequest,
    ) -> Option<SectionOutcome> {
        match section {
            ConfigSection::LifecycleEvents => {
                // Unlike the other sections (still empty `{}` messages,
                // hence `Copy`), `LifecycleEventsConfig` now carries owned
                // bytes/strings (`m15-events-webhooks`), so it cannot be
                // moved out of `request: &ConfigureRequest` — cloned
                // instead, once per `Configure` call.
                let cfg = request.lifecycle_events.clone()?;
                Some(self.features.lifecycle_events.apply(cfg).await)
            }
            ConfigSection::TelemetryExport => {
                // Unlike its five sibling stub messages, `TelemetryExportConfig`
                // (m15-rayd-otlp) has real fields (a `String` service name, a
                // `oneof auth`), so it is not `Copy`; `request` is a shared
                // reference, so this clones rather than moving out of it.
                let cfg = request.telemetry_export.clone()?;
                Some(self.features.telemetry_export.apply(cfg).await)
            }
            ConfigSection::SecretGateway => {
                // Unlike the other four sections (today still empty, `Copy`
                // stub messages), `SecretGatewayConfig` carries real fields
                // (`routes`), so it is not `Copy` and must be cloned out of
                // `request`, which this method only ever borrows.
                let cfg = request.secret_gateway.clone()?;
                Some(self.features.secret_gateway.apply(cfg).await)
            }
            ConfigSection::S3Mounts => {
                // `.clone()`: unlike the other sections, `S3MountsConfig`
                // now carries real fields (`m15-s3-mounts`), so it is no
                // longer `Copy` and `request` is only borrowed here.
                let cfg = request.s3_mounts.clone()?;
                Some(self.features.s3_mounts.apply(cfg).await)
            }
            ConfigSection::EfsVolumes => {
                // `EfsVolumesConfig` carries a `repeated` field (unlike the
                // other four sections' still-empty stubs), so prost does not
                // derive `Copy` for it; an explicit clone is the only change
                // this arm needs.
                let cfg = request.efs_volumes.clone()?;
                Some(self.features.efs_volumes.apply(cfg).await)
            }
        }
    }
}

#[tonic::async_trait]
impl ConfigureService for ConfigureGrpc {
    async fn configure(
        &self,
        request: Request<ConfigureRequest>,
    ) -> Result<Response<ConfigureResponse>, Status> {
        if !configure::is_configurable(self.session.phase()) {
            return Err(Status::failed_precondition(NOT_RUNNING_DETAIL));
        }
        let request = request.into_inner();
        let mut results = Vec::new();
        for section in configure::APPLY_ORDER {
            if let Some(outcome) = self.apply_section(section, &request).await {
                results.push(to_section_result(section, outcome));
            }
        }
        tracing::info!(
            rpc = "Configure",
            sections_applied = results.len(),
            "configure"
        );
        Ok(Response::new(ConfigureResponse {
            results,
            config_generation: 1,
        }))
    }

    async fn configure_status(
        &self,
        _request: Request<ConfigureStatusRequest>,
    ) -> Result<Response<ConfigureStatusResponse>, Status> {
        Ok(Response::new(ConfigureStatusResponse {
            s3_mounts: Some(self.features.s3_mounts.status().await),
            efs_volumes: Some(self.features.efs_volumes.status().await),
            lifecycle_events: Some(self.features.lifecycle_events.status().await),
            telemetry_export: Some(self.features.telemetry_export.status().await),
            secret_gateway: Some(self.features.secret_gateway.status().await),
        }))
    }
}

fn to_section_result(section: ConfigSection, outcome: SectionOutcome) -> SectionResult {
    SectionResult {
        section: i32::from(proto_section(section)),
        code: i32::from(proto_code(outcome.code)),
        error_class: outcome.error_class.unwrap_or_default(),
    }
}

fn proto_section(section: ConfigSection) -> rayito_proto::v1::ConfigSection {
    match section {
        ConfigSection::S3Mounts => rayito_proto::v1::ConfigSection::S3Mounts,
        ConfigSection::EfsVolumes => rayito_proto::v1::ConfigSection::EfsVolumes,
        ConfigSection::LifecycleEvents => rayito_proto::v1::ConfigSection::LifecycleEvents,
        ConfigSection::TelemetryExport => rayito_proto::v1::ConfigSection::TelemetryExport,
        ConfigSection::SecretGateway => rayito_proto::v1::ConfigSection::SecretGateway,
    }
}

fn proto_code(code: SectionCode) -> rayito_proto::v1::SectionCode {
    match code {
        SectionCode::Applied => rayito_proto::v1::SectionCode::Applied,
        SectionCode::Pending => rayito_proto::v1::SectionCode::Pending,
        SectionCode::Unsupported => rayito_proto::v1::SectionCode::Unsupported,
        SectionCode::Invalid => rayito_proto::v1::SectionCode::Invalid,
        SectionCode::Failed => rayito_proto::v1::SectionCode::Failed,
    }
}

#[cfg(test)]
mod tests {
    use rayd_core::clock::SystemClock;
    use rayd_core::session::RunHookInput;
    use rayito_proto::v1::{EfsVolumesConfig, S3MountsConfig};

    use super::*;
    use crate::features::{self, FeatureContext};

    const RUN_PAYLOAD: &str = "{\"v\":1,\"token_sha256\":\"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\"}";

    fn running_service() -> ConfigureGrpc {
        let session = Arc::new(SandboxSession::new(Arc::new(SystemClock::new()), "test"));
        session.run(RunHookInput {
            sandbox_id: Some("mvm-test"),
            payload: Some(RUN_PAYLOAD),
        });
        ConfigureGrpc::new(
            session,
            Arc::new(features::build(&FeatureContext::default())),
        )
    }

    #[tokio::test]
    async fn configure_before_run_is_failed_precondition_not_running() {
        let session = Arc::new(SandboxSession::new(Arc::new(SystemClock::new()), "test"));
        let service = ConfigureGrpc::new(
            session,
            Arc::new(features::build(&FeatureContext::default())),
        );
        let status = service
            .configure(Request::new(ConfigureRequest::default()))
            .await
            .unwrap_err();
        assert_eq!(status.code(), tonic::Code::FailedPrecondition);
        assert_eq!(status.message(), "not_running");
    }

    #[tokio::test]
    async fn an_absent_section_is_left_out_of_the_results() {
        let service = running_service();
        let response = service
            .configure(Request::new(ConfigureRequest::default()))
            .await
            .unwrap()
            .into_inner();
        assert!(response.results.is_empty());
    }

    #[tokio::test]
    async fn a_present_section_against_a_slot_still_unsupported_reports_unsupported() {
        // `s3_mounts` has a real adapter since `m15-s3-mounts`; `efs_volumes`
        // is still a stub (`features::efs_volumes::build` -> `Unsupported`)
        // and makes the same point.
        let service = running_service();
        let request = ConfigureRequest {
            efs_volumes: Some(EfsVolumesConfig::default()),
            ..Default::default()
        };
        let response = service
            .configure(Request::new(request))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(response.results.len(), 1);
        assert_eq!(
            response.results[0].section,
            i32::from(rayito_proto::v1::ConfigSection::EfsVolumes)
        );
        assert_eq!(
            response.results[0].code,
            i32::from(rayito_proto::v1::SectionCode::Unsupported)
        );
    }

    #[tokio::test]
    async fn a_present_s3_mounts_section_now_applies_instead_of_reporting_unsupported() {
        let service = running_service();
        let request = ConfigureRequest {
            s3_mounts: Some(S3MountsConfig::default()),
            ..Default::default()
        };
        let response = service
            .configure(Request::new(request))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(response.results.len(), 1);
        assert_eq!(
            response.results[0].section,
            i32::from(rayito_proto::v1::ConfigSection::S3Mounts)
        );
        assert_eq!(
            response.results[0].code,
            i32::from(rayito_proto::v1::SectionCode::Applied)
        );
    }

    #[tokio::test]
    async fn configure_status_reports_every_slots_default_status() {
        let service = running_service();
        let response = service
            .configure_status(Request::new(ConfigureStatusRequest {}))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(
            response.s3_mounts,
            Some(rayito_proto::v1::S3MountsStatus::default())
        );
        assert_eq!(
            response.efs_volumes,
            Some(rayito_proto::v1::EfsVolumesStatus::default())
        );
    }

    /// `ConfigureGrpc` must never log or `Debug`-print the request: a
    /// future section may carry a pushed secret-gateway header value.
    /// Captures every log line emitted during a real `Configure` call
    /// (JSON formatter, same shape `rayd::logging::init` uses) and asserts
    /// none of them mention the request type or its field names.
    #[tokio::test]
    async fn configure_never_logs_the_request() {
        use std::io;
        use std::sync::{Arc as StdArc, Mutex};

        use tracing_subscriber::fmt::MakeWriter;

        #[derive(Clone, Default)]
        struct Capture(StdArc<Mutex<Vec<u8>>>);

        impl io::Write for Capture {
            fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
                self.0
                    .lock()
                    .unwrap_or_else(std::sync::PoisonError::into_inner)
                    .extend_from_slice(buf);
                Ok(buf.len())
            }

            fn flush(&mut self) -> io::Result<()> {
                Ok(())
            }
        }

        impl<'a> MakeWriter<'a> for Capture {
            type Writer = Capture;

            fn make_writer(&'a self) -> Self::Writer {
                self.clone()
            }
        }

        let capture = Capture::default();
        let subscriber = tracing_subscriber::fmt()
            .json()
            .with_writer(capture.clone())
            .with_target(false)
            .finish();
        let service = running_service();
        let request = ConfigureRequest {
            s3_mounts: Some(S3MountsConfig::default()),
            request_id: "a-request-id".to_owned(),
            ..Default::default()
        };
        {
            let _guard = tracing::subscriber::set_default(subscriber);
            service.configure(Request::new(request)).await.unwrap();
        }
        let logged = String::from_utf8_lossy(
            &capture
                .0
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner),
        )
        .into_owned();
        for forbidden in ["ConfigureRequest", "a-request-id", "s3_mounts"] {
            assert!(
                !logged.contains(forbidden),
                "log line must never mention {forbidden:?}: {logged}"
            );
        }
    }
}
