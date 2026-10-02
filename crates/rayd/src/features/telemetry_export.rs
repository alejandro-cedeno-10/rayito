//! Real adapter for m15-rayd-otlp (ADR-021): wires `rayd_core::telemetry`
//! into the `telemetry_export` `ConfigureService` slot. `build()` reports
//! `Unsupported` whenever `FeatureContext::region` is `None`: with no
//! region there is no `CloudWatch` OTLP endpoint host to build and nothing
//! to sign against, so `AgentFeatures.telemetry_export` stays `false`
//! exactly like before this feature existed.
//!
//! A present, non-empty section spawns one background task that samples
//! `MetricsHistory`'s latest point every `interval_s` and exports it;
//! re-applying tears the old task down first (every section replaces the
//! previous state, never merges with it). A present but empty section
//! (`TelemetryExportConfig::default()`) stops the exporter — the same
//! "present clears, absent leaves untouched" rule as every 0.6 section.
//! `OtlpAuth.execution_role()` additionally needs this guest to actually
//! have an execution role: `apply()` probes it (`ExecutionRoleProbe`)
//! before starting anything, rejecting the section (`role_not_permitted`)
//! rather than accepting it and letting every export fail silently later.
//!
//! `/suspend`'s bounded flush (`TelemetryParticipant`, `LifecycleParticipant`)
//! and the background task share one `SharedState` so a flush never races a
//! re-`apply()`: both go through the same `tokio::sync::Mutex`, and both
//! drive the same `export_pending` (drain → encode → send → record)
//! through the `TelemetrySink` port, never `CloudWatchOtlpSink` directly —
//! which is also what lets this module's own tests swap in a fake sink
//! instead of a real network call.

use std::sync::{Arc, Mutex as StdMutex, PoisonError};
use std::time::Duration;

use rayd_core::configure::{SectionCode, SectionOutcome};
use rayd_core::metrics_history::MetricsHistory;
use rayd_core::session::SandboxSession;
use rayd_core::suspend_sync::{ParticipantDemand, ParticipantReport};
use rayd_core::telemetry::{
    self as domain, Batcher, NameStyle as DomainNameStyle, OtlpEncoder as _, ResourceAttrs,
    TelemetryAuth, TelemetryConfig, TelemetrySink, jittered_backoff, points_from_sample,
};
use rayito_proto::v1::telemetry_export_config::Auth as WireAuth;
use rayito_proto::v1::{
    TelemetryExportBearerAuth, TelemetryExportConfig, TelemetryExportExecutionRoleAuth,
    TelemetryExportNameStyle as WireNameStyle, TelemetryExportStatus,
};
use tokio::sync::{Mutex as AsyncMutex, Notify};
use tokio::task::JoinHandle;
use zeroize::Zeroizing;

use super::FeatureContext;
use super::slot::{ConfigurableFeature, Unsupported};
use crate::adapters::otlp_codec::ProstOtlpEncoder;
use crate::adapters::random::OsRandomSource;
use crate::adapters::{
    CloudWatchOtlpSink, CredentialBrokerError, FixedCredentialsSink, ImdsCredentialBroker,
    SinkCredentials,
};
use crate::lifecycle::LifecycleParticipant;

pub const PARTICIPANT_NAME: &str = "telemetry_export";
/// `/suspend` share ceiling (architecture §7.5: "flush acotado... máximo
/// 2 s"); the actual share `SuspendShares::allocate` hands out is at most
/// this *and* at most the hook's own `sync_deadline`.
const SUSPEND_FLUSH_MAX: Duration = Duration::from_secs(2);
/// Caps one export POST so a hung endpoint cannot stall the exporter task
/// and delay the next sampling tick indefinitely. Covers the whole attempt
/// (including, for `ExecutionRole`, the `ImdsCredentialBroker` lease fetch
/// `CloudWatchOtlpSink::send_with` may need first), so
/// `cloudwatch_otlp_sink::REQUEST_TIMEOUT` (the HTTP request alone) must
/// stay below this or it can never fire first.
const EXPORT_ATTEMPT_TIMEOUT: Duration = Duration::from_secs(8);

/// `apply()`'s gate for `OtlpAuth.execution_role()`: whether this guest can
/// currently get execution-role credentials at all, decoupled from
/// `ImdsCredentialBroker` so a fake can answer it in this module's own
/// tests without a real IMDS endpoint (this crate's convention: no unit
/// test below the acceptance stage touches a real network —
/// `adapters::s3_store`'s own IMDS path is likewise untested here).
#[tonic::async_trait]
trait ExecutionRoleProbe: Send + Sync {
    async fn probe(&self) -> Result<(), CredentialBrokerError>;
}

/// The real adapter: `ensure()`'s cache means a lease fetched moments ago
/// by the sink itself (or by a previous `apply()`) answers this without a
/// second IMDS round trip.
struct ImdsRoleProbe(Arc<ImdsCredentialBroker>);

#[tonic::async_trait]
impl ExecutionRoleProbe for ImdsRoleProbe {
    async fn probe(&self) -> Result<(), CredentialBrokerError> {
        self.0.ensure().await.map(|_credentials| ())
    }
}

struct Running {
    config: TelemetryConfig,
    credentials: SinkCredentials,
    sink: Arc<CloudWatchOtlpSink>,
    batcher: Arc<StdMutex<Batcher>>,
    stop: Arc<Notify>,
    task: JoinHandle<()>,
}

struct SharedState {
    region: String,
    session: Arc<SandboxSession>,
    history: Arc<MetricsHistory>,
    credentials: Arc<ImdsCredentialBroker>,
    role_probe: Arc<dyn ExecutionRoleProbe>,
    running: AsyncMutex<Option<Running>>,
}

impl SharedState {
    fn sandbox_id(&self) -> String {
        self.session.health().sandbox_id.unwrap_or_default()
    }

    fn sink_credentials(&self, auth: &TelemetryAuth) -> SinkCredentials {
        match auth {
            TelemetryAuth::ExecutionRole => {
                SinkCredentials::ExecutionRole(self.credentials.clone())
            }
            TelemetryAuth::Bearer(token) => SinkCredentials::Bearer(token.clone()),
        }
    }

    /// Stops and awaits whatever is currently running, leaving `running`
    /// empty; a no-op when nothing was running.
    async fn stop_running(&self) {
        let previous = self.running.lock().await.take();
        if let Some(running) = previous {
            running.stop.notify_one();
            let _ = running.task.await;
        }
    }

    async fn apply(&self, cfg: TelemetryExportConfig) -> SectionOutcome {
        if cfg == TelemetryExportConfig::default() {
            self.stop_running().await;
            return SectionOutcome::applied();
        }
        let names = match WireNameStyle::try_from(cfg.names) {
            Ok(WireNameStyle::E2b) => DomainNameStyle::E2b,
            _ => DomainNameStyle::Rayito,
        };
        let auth = match &cfg.auth {
            Some(WireAuth::ExecutionRole(TelemetryExportExecutionRoleAuth {})) => {
                Some(TelemetryAuth::ExecutionRole)
            }
            Some(WireAuth::Bearer(TelemetryExportBearerAuth { token })) => {
                Some(TelemetryAuth::Bearer(Zeroizing::new(token.clone())))
            }
            None => None,
        };
        let resource = ResourceAttrs {
            sandbox_id: self.sandbox_id(),
            image_arn: cfg.image_arn.clone(),
            image_version: cfg.image_version.clone(),
            image_memory_mib: cfg.image_memory_mib,
        };
        let config = match TelemetryConfig::validate(
            Duration::from_secs(u64::from(cfg.interval_s)),
            &cfg.service_name,
            names,
            resource,
            auth,
        ) {
            Ok(config) => config,
            Err(error) => {
                return SectionOutcome {
                    code: SectionCode::Invalid,
                    error_class: Some(error.error_class().to_owned()),
                };
            }
        };
        if matches!(config.auth, TelemetryAuth::ExecutionRole)
            && let Err(error) = self.role_probe.probe().await
        {
            let (code, error_class) = match error {
                CredentialBrokerError::RoleNotAttached => {
                    (SectionCode::Invalid, "role_not_permitted")
                }
                CredentialBrokerError::Unavailable => {
                    (SectionCode::Failed, "credentials_unavailable")
                }
            };
            return SectionOutcome {
                code,
                error_class: Some(error_class.to_owned()),
            };
        }
        self.stop_running().await;
        let Ok(sink) = CloudWatchOtlpSink::new(&self.region) else {
            return SectionOutcome {
                code: SectionCode::Failed,
                error_class: Some("sink_init_failed".to_owned()),
            };
        };
        let sink = Arc::new(sink);
        let credentials = self.sink_credentials(&config.auth);
        let batcher = Arc::new(StdMutex::new(Batcher::default()));
        let stop = Arc::new(Notify::new());
        let task = spawn_exporter(
            config.clone(),
            self.history.clone(),
            sink.clone(),
            credentials.clone(),
            batcher.clone(),
            stop.clone(),
        );
        *self.running.lock().await = Some(Running {
            config,
            credentials,
            sink,
            batcher,
            stop,
            task,
        });
        SectionOutcome::applied()
    }

    async fn status(&self) -> TelemetryExportStatus {
        match self.running.lock().await.as_ref() {
            Some(running) => {
                let stats = lock_batcher(&running.batcher).stats();
                TelemetryExportStatus {
                    exported: stats.exported,
                    dropped: stats.dropped,
                    last_error_class: stats.last_error_class.unwrap_or_default(),
                }
            }
            None => TelemetryExportStatus::default(),
        }
    }

    /// One best-effort flush within `share`; never blocks past it (the
    /// caller, `hooks::mod`, also wraps this in its own
    /// `tokio::time::timeout`). A failed or timed-out flush puts the
    /// points back so the next regular tick (or the next `/suspend`)
    /// retries them, rather than losing them silently.
    async fn flush(&self, share: Duration) -> ParticipantReport {
        let guard = self.running.lock().await;
        let Some(running) = guard.as_ref() else {
            return ParticipantReport {
                completed: true,
                timed_out: false,
            };
        };
        let sink = FixedCredentialsSink::new(running.sink.clone(), running.credentials.clone());
        let outcome = export_pending(&running.batcher, &sink, &running.config, share).await;
        report_from_outcome(outcome)
    }

    /// `/resume` (ADR-021, OT5 research): hyper's own pool-idle timer runs
    /// on the monotonic clock, which does not advance while the `MicroVM`
    /// is suspended, so a connection opened before a long suspension can
    /// be handed back to the exporter as if it were still warm and fail on
    /// the very first post-resume export. Tearing the running exporter
    /// down and respawning it with a brand-new `CloudWatchOtlpSink` (and
    /// therefore a brand-new connection pool) side-steps that; the batcher
    /// (and any points queued since the last successful export) moves to
    /// the new task unchanged. A no-op when nothing is running, or when a
    /// fresh sink cannot be built (the next explicit re-`apply()` or
    /// `/resume` can retry; `apply()`'s own `sink_init_failed` handling
    /// never panics on this either).
    async fn rebuild_sink_on_resume(&self) {
        let mut guard = self.running.lock().await;
        let Some(previous) = guard.take() else {
            return;
        };
        previous.stop.notify_one();
        let _ = previous.task.await;
        let Ok(sink) = CloudWatchOtlpSink::new(&self.region) else {
            return;
        };
        let sink = Arc::new(sink);
        let stop = Arc::new(Notify::new());
        let task = spawn_exporter(
            previous.config.clone(),
            self.history.clone(),
            sink.clone(),
            previous.credentials.clone(),
            previous.batcher.clone(),
            stop.clone(),
        );
        *guard = Some(Running {
            config: previous.config,
            credentials: previous.credentials,
            sink,
            batcher: previous.batcher,
            stop,
            task,
        });
    }
}

fn lock_batcher(batcher: &StdMutex<Batcher>) -> std::sync::MutexGuard<'_, Batcher> {
    batcher.lock().unwrap_or_else(PoisonError::into_inner)
}

fn sink_error_class(error: domain::SinkError) -> &'static str {
    match error {
        domain::SinkError::Network => "network",
        domain::SinkError::Rejected => "rejected",
    }
}

/// What one export attempt (a tick's regular send, or `/suspend`'s bounded
/// flush) did, once `export_pending` has already applied it to `batcher`
/// (drained, encoded, sent, and recorded the exported count / re-enqueued
/// on failure): callers only need this to drive their own bookkeeping
/// (the tick loop's backoff attempts, or a `ParticipantReport`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ExportOutcome {
    NothingPending,
    Exported,
    Failed,
    TimedOut,
}

fn report_from_outcome(outcome: ExportOutcome) -> ParticipantReport {
    match outcome {
        ExportOutcome::NothingPending | ExportOutcome::Exported => ParticipantReport {
            completed: true,
            timed_out: false,
        },
        ExportOutcome::Failed => ParticipantReport {
            completed: false,
            timed_out: false,
        },
        ExportOutcome::TimedOut => ParticipantReport {
            completed: false,
            timed_out: true,
        },
    }
}

/// Drains whatever `batcher` holds and attempts one encode-and-send within
/// `timeout` through the `TelemetrySink` port — never `CloudWatchOtlpSink`
/// directly — so the exact same drain → encode → send → record sequence
/// backs both the background tick (`spawn_exporter`) and `/suspend`'s
/// bounded flush (`SharedState::flush`), and so this module's own tests
/// can drive it with a fake sink instead of a real network call. A send
/// failure or a timeout puts the drained points back (`Batcher::enqueue`)
/// so the next attempt (the next tick, or the next `/suspend`) retries
/// them rather than losing them silently.
async fn export_pending<S: TelemetrySink>(
    batcher: &StdMutex<Batcher>,
    sink: &S,
    config: &TelemetryConfig,
    timeout: Duration,
) -> ExportOutcome {
    let pending = lock_batcher(batcher).drain();
    if pending.is_empty() {
        return ExportOutcome::NothingPending;
    }
    let payload = ProstOtlpEncoder.encode(
        &config.resource,
        &config.service_name,
        config.names,
        &pending,
    );
    let exported = u64::try_from(pending.len()).unwrap_or(u64::MAX);
    match tokio::time::timeout(timeout, sink.send(payload)).await {
        Ok(Ok(())) => {
            lock_batcher(batcher).record_exported(exported);
            ExportOutcome::Exported
        }
        Ok(Err(error)) => {
            lock_batcher(batcher).enqueue(pending);
            lock_batcher(batcher).record_failure(sink_error_class(error));
            ExportOutcome::Failed
        }
        Err(_elapsed) => {
            lock_batcher(batcher).enqueue(pending);
            lock_batcher(batcher).record_failure("network");
            ExportOutcome::TimedOut
        }
    }
}

/// The `ConfigurableFeature` role: `ConfigureGrpc` dispatches here.
pub struct TelemetryFeature {
    state: Arc<SharedState>,
    participant: Arc<TelemetryParticipant>,
}

/// The `LifecycleParticipant` role, sharing `state` with `TelemetryFeature`
/// so a `/suspend` flush and a background export tick never disagree about
/// what is currently running.
struct TelemetryParticipant {
    state: Arc<SharedState>,
}

#[must_use]
pub fn build(
    ctx: &FeatureContext,
) -> Arc<dyn ConfigurableFeature<TelemetryExportConfig, TelemetryExportStatus>> {
    let Some(region) = ctx.region.clone() else {
        return Arc::new(Unsupported);
    };
    let role_probe: Arc<dyn ExecutionRoleProbe> = Arc::new(ImdsRoleProbe(ctx.credentials.clone()));
    build_with(ctx, region, role_probe)
}

/// Shared by `build()` (the real `ImdsRoleProbe`) and this module's own
/// tests (a fake `ExecutionRoleProbe`, so `OtlpAuth.execution_role()` can
/// be exercised end to end without a real IMDS endpoint).
fn build_with(
    ctx: &FeatureContext,
    region: String,
    role_probe: Arc<dyn ExecutionRoleProbe>,
) -> Arc<dyn ConfigurableFeature<TelemetryExportConfig, TelemetryExportStatus>> {
    let state = Arc::new(SharedState {
        region,
        session: ctx.session.clone(),
        history: ctx.history.clone(),
        credentials: ctx.credentials.clone(),
        role_probe,
        running: AsyncMutex::new(None),
    });
    Arc::new(TelemetryFeature {
        participant: Arc::new(TelemetryParticipant {
            state: state.clone(),
        }),
        state,
    })
}

#[tonic::async_trait]
impl ConfigurableFeature<TelemetryExportConfig, TelemetryExportStatus> for TelemetryFeature {
    fn supported(&self) -> bool {
        true
    }

    async fn apply(&self, cfg: TelemetryExportConfig) -> SectionOutcome {
        self.state.apply(cfg).await
    }

    async fn status(&self) -> TelemetryExportStatus {
        self.state.status().await
    }

    fn participant(&self) -> Option<Arc<dyn LifecycleParticipant>> {
        Some(self.participant.clone())
    }
}

#[tonic::async_trait]
impl LifecycleParticipant for TelemetryParticipant {
    fn demand(&self) -> ParticipantDemand {
        ParticipantDemand {
            name: PARTICIPANT_NAME,
            max: SUSPEND_FLUSH_MAX,
        }
    }

    async fn on_suspend(&self, share: Duration) -> ParticipantReport {
        self.state.flush(share).await
    }

    async fn on_resume(&self) {
        self.state.rebuild_sink_on_resume().await;
    }

    // `on_run`/`on_terminate`/`ready_gate` keep the trait's defaults:
    // telemetry never gates readiness, and a connection opened after
    // `on_resume`'s rebuild is never older than this boot's current
    // generation.
}

/// One tick per `config.interval`: sample `MetricsHistory`'s latest point,
/// enqueue it, and attempt to drain and export whatever is pending (which
/// may include points from earlier failed attempts) through
/// `export_pending`. A failed send backs off (`jittered_backoff`) before
/// the next real attempt, but sampling itself never stops — a slow network
/// only delays exporting, never observing.
fn spawn_exporter(
    config: TelemetryConfig,
    history: Arc<MetricsHistory>,
    sink: Arc<CloudWatchOtlpSink>,
    credentials: SinkCredentials,
    batcher: Arc<StdMutex<Batcher>>,
    stop: Arc<Notify>,
) -> JoinHandle<()> {
    let sink = FixedCredentialsSink::new(sink, credentials);
    tokio::spawn(async move {
        let mut ticker = tokio::time::interval(config.interval);
        ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        let mut attempt: u32 = 0;
        let mut retry_not_before: Option<tokio::time::Instant> = None;
        loop {
            tokio::select! {
                () = stop.notified() => return,
                _ = ticker.tick() => {}
            }
            if let Some(sample) = history.latest() {
                lock_batcher(&batcher).enqueue(points_from_sample(&sample));
            }
            if let Some(not_before) = retry_not_before
                && tokio::time::Instant::now() < not_before
            {
                continue;
            }
            match export_pending(&batcher, &sink, &config, EXPORT_ATTEMPT_TIMEOUT).await {
                ExportOutcome::NothingPending => {}
                ExportOutcome::Exported => {
                    attempt = 0;
                    retry_not_before = None;
                }
                ExportOutcome::Failed | ExportOutcome::TimedOut => {
                    attempt = attempt.saturating_add(1);
                    retry_not_before = Some(
                        tokio::time::Instant::now() + jittered_backoff(attempt, &OsRandomSource),
                    );
                }
            }
        }
    })
}

#[cfg(test)]
mod tests {
    use std::sync::atomic::{AtomicU32, Ordering};

    use rayd_core::clock::SystemClock;
    use rayd_core::telemetry::model::{GaugeKind, MetricPoint};
    use rayd_core::telemetry::{NameStyle, SinkError};

    use super::*;

    fn context_without_region() -> FeatureContext {
        FeatureContext::default()
    }

    fn context_with_region() -> FeatureContext {
        FeatureContext {
            region: Some("us-east-1".to_owned()),
            ..FeatureContext::default()
        }
    }

    /// Always succeeds: what every test that is not specifically about the
    /// probe itself uses, so `OtlpAuth.execution_role()` can be exercised
    /// without a real IMDS endpoint.
    struct AlwaysAvailable;

    #[tonic::async_trait]
    impl ExecutionRoleProbe for AlwaysAvailable {
        async fn probe(&self) -> Result<(), CredentialBrokerError> {
            Ok(())
        }
    }

    struct AlwaysFails(CredentialBrokerError);

    #[tonic::async_trait]
    impl ExecutionRoleProbe for AlwaysFails {
        async fn probe(&self) -> Result<(), CredentialBrokerError> {
            Err(self.0)
        }
    }

    fn build_for_test(
        ctx: &FeatureContext,
        role_probe: Arc<dyn ExecutionRoleProbe>,
    ) -> Arc<dyn ConfigurableFeature<TelemetryExportConfig, TelemetryExportStatus>> {
        let region = ctx.region.clone().expect("tests always set a region");
        build_with(ctx, region, role_probe)
    }

    fn build_with_available_role(
        ctx: &FeatureContext,
    ) -> Arc<dyn ConfigurableFeature<TelemetryExportConfig, TelemetryExportStatus>> {
        build_for_test(ctx, Arc::new(AlwaysAvailable))
    }

    #[test]
    fn without_a_region_the_slot_is_unsupported() {
        let feature = build(&context_without_region());
        assert!(!feature.supported());
    }

    #[test]
    fn with_a_region_the_slot_is_supported() {
        let feature = build(&context_with_region());
        assert!(feature.supported());
    }

    #[tokio::test]
    async fn an_absent_auth_is_rejected_as_missing_auth_before_anything_starts() {
        let feature = build_with_available_role(&context_with_region());
        let outcome = feature
            .apply(TelemetryExportConfig {
                interval_s: 60,
                service_name: "agente".to_owned(),
                ..Default::default()
            })
            .await;
        assert_eq!(outcome.code, SectionCode::Invalid);
        assert_eq!(outcome.error_class.as_deref(), Some("missing_auth"));
    }

    #[tokio::test]
    async fn an_interval_out_of_range_is_rejected_before_anything_starts() {
        let feature = build_with_available_role(&context_with_region());
        let outcome = feature
            .apply(TelemetryExportConfig {
                interval_s: 5,
                service_name: "agente".to_owned(),
                auth: Some(WireAuth::ExecutionRole(TelemetryExportExecutionRoleAuth {})),
                ..Default::default()
            })
            .await;
        assert_eq!(outcome.code, SectionCode::Invalid);
        assert_eq!(outcome.error_class.as_deref(), Some("invalid_interval"));
    }

    #[tokio::test]
    async fn a_valid_section_applies_and_status_starts_at_zero() {
        let feature = build_with_available_role(&context_with_region());
        let outcome = feature
            .apply(TelemetryExportConfig {
                interval_s: 60,
                service_name: "agente".to_owned(),
                auth: Some(WireAuth::ExecutionRole(TelemetryExportExecutionRoleAuth {})),
                ..Default::default()
            })
            .await;
        assert_eq!(outcome.code, SectionCode::Applied);
        let status = feature.status().await;
        assert_eq!(status.exported, 0);
        assert_eq!(status.dropped, 0);
    }

    #[tokio::test]
    async fn an_empty_section_stops_a_previously_applied_one() {
        let feature = build_with_available_role(&context_with_region());
        feature
            .apply(TelemetryExportConfig {
                interval_s: 60,
                service_name: "agente".to_owned(),
                auth: Some(WireAuth::ExecutionRole(TelemetryExportExecutionRoleAuth {})),
                ..Default::default()
            })
            .await;
        let outcome = feature.apply(TelemetryExportConfig::default()).await;
        assert_eq!(outcome.code, SectionCode::Applied);
    }

    #[tokio::test]
    async fn a_guest_with_no_execution_role_is_rejected_as_role_not_permitted() {
        let feature = build_for_test(
            &context_with_region(),
            Arc::new(AlwaysFails(CredentialBrokerError::RoleNotAttached)),
        );
        let outcome = feature
            .apply(TelemetryExportConfig {
                interval_s: 60,
                service_name: "agente".to_owned(),
                auth: Some(WireAuth::ExecutionRole(TelemetryExportExecutionRoleAuth {})),
                ..Default::default()
            })
            .await;
        assert_eq!(outcome.code, SectionCode::Invalid);
        assert_eq!(outcome.error_class.as_deref(), Some("role_not_permitted"));
        // Nothing was started: `status()` is still the untouched default.
        assert_eq!(feature.status().await, TelemetryExportStatus::default());
    }

    #[tokio::test]
    async fn a_broker_hiccup_is_rejected_as_credentials_unavailable_not_role_not_permitted() {
        let feature = build_for_test(
            &context_with_region(),
            Arc::new(AlwaysFails(CredentialBrokerError::Unavailable)),
        );
        let outcome = feature
            .apply(TelemetryExportConfig {
                interval_s: 60,
                service_name: "agente".to_owned(),
                auth: Some(WireAuth::ExecutionRole(TelemetryExportExecutionRoleAuth {})),
                ..Default::default()
            })
            .await;
        assert_eq!(outcome.code, SectionCode::Failed);
        assert_eq!(
            outcome.error_class.as_deref(),
            Some("credentials_unavailable")
        );
    }

    #[tokio::test]
    async fn bearer_auth_never_probes_for_an_execution_role() {
        // `AlwaysFails` would reject an `ExecutionRole` section outright;
        // a `Bearer` one must apply regardless, since it never needs IMDS.
        let feature = build_for_test(
            &context_with_region(),
            Arc::new(AlwaysFails(CredentialBrokerError::RoleNotAttached)),
        );
        let outcome = feature
            .apply(TelemetryExportConfig {
                interval_s: 60,
                service_name: "agente".to_owned(),
                auth: Some(WireAuth::Bearer(TelemetryExportBearerAuth {
                    token: "sk-test".to_owned(),
                })),
                ..Default::default()
            })
            .await;
        assert_eq!(outcome.code, SectionCode::Applied);
    }

    #[tokio::test]
    async fn a_participant_with_nothing_running_reports_a_completed_noop_flush() {
        let feature = build_with_available_role(&context_with_region());
        let participant = feature
            .participant()
            .expect("telemetry always participates");
        let report = participant.on_suspend(Duration::from_secs(1)).await;
        assert!(report.completed);
        assert!(!report.timed_out);
    }

    #[test]
    fn without_a_region_there_is_no_participant_to_register_either() {
        let feature = build(&context_without_region());
        assert!(feature.participant().is_none());
    }

    #[tokio::test]
    async fn configure_status_default_matches_the_proto_zero_value() {
        let feature = build_with_available_role(&context_with_region());
        assert_eq!(feature.status().await, TelemetryExportStatus::default());
    }

    /// `SandboxSession::health().sandbox_id` is `None` before `/run`; the
    /// resource attribute then falls back to an empty string rather than
    /// panicking (the real path always configures after `/run`, which this
    /// guards against a future caller forgetting that precondition).
    #[tokio::test]
    async fn no_sandbox_id_yet_degrades_to_an_empty_attribute_not_a_panic() {
        let state = SharedState {
            region: "us-east-1".to_owned(),
            session: Arc::new(SandboxSession::new(Arc::new(SystemClock::new()), "test")),
            history: Arc::new(MetricsHistory::default()),
            credentials: Arc::new(ImdsCredentialBroker::new()),
            role_probe: Arc::new(AlwaysAvailable),
            running: AsyncMutex::new(None),
        };
        assert_eq!(state.sandbox_id(), "");
    }

    // ------------------------------------------------------- export_pending

    #[derive(Clone)]
    struct FakeSink {
        calls: Arc<AtomicU32>,
        outcome: Arc<StdMutex<Result<(), SinkError>>>,
    }

    impl FakeSink {
        fn new(outcome: Result<(), SinkError>) -> Self {
            Self {
                calls: Arc::new(AtomicU32::new(0)),
                outcome: Arc::new(StdMutex::new(outcome)),
            }
        }

        fn succeeding() -> Self {
            Self::new(Ok(()))
        }

        fn failing_with(error: SinkError) -> Self {
            Self::new(Err(error))
        }

        fn calls(&self) -> u32 {
            self.calls.load(Ordering::SeqCst)
        }
    }

    impl TelemetrySink for FakeSink {
        fn send(
            &self,
            _payload: Vec<u8>,
        ) -> impl std::future::Future<Output = Result<(), SinkError>> + Send {
            self.calls.fetch_add(1, Ordering::SeqCst);
            std::future::ready(*self.outcome.lock().unwrap_or_else(PoisonError::into_inner))
        }
    }

    fn test_config() -> TelemetryConfig {
        let resource = ResourceAttrs {
            sandbox_id: "mvm-test".to_owned(),
            image_arn: "rayito-base".to_owned(),
            image_version: "1".to_owned(),
            image_memory_mib: 1024,
        };
        TelemetryConfig::validate(
            Duration::from_secs(60),
            "agente",
            NameStyle::Rayito,
            resource,
            Some(TelemetryAuth::ExecutionRole),
        )
        .expect("a fixed, valid config for export_pending's own tests")
    }

    fn one_point() -> MetricPoint {
        MetricPoint {
            kind: GaugeKind::CpuUsedPct,
            value: 1.0,
            timestamp: std::time::SystemTime::UNIX_EPOCH,
        }
    }

    #[tokio::test]
    async fn export_pending_is_a_noop_when_the_batcher_is_empty() {
        let batcher = StdMutex::new(Batcher::default());
        let sink = FakeSink::succeeding();
        let outcome = export_pending(&batcher, &sink, &test_config(), Duration::from_secs(1)).await;
        assert_eq!(outcome, ExportOutcome::NothingPending);
        assert_eq!(sink.calls(), 0);
    }

    #[tokio::test]
    async fn export_pending_records_a_success_and_drains_the_batcher() {
        let batcher = StdMutex::new(Batcher::default());
        lock_batcher(&batcher).enqueue([one_point()]);
        let sink = FakeSink::succeeding();
        let outcome = export_pending(&batcher, &sink, &test_config(), Duration::from_secs(1)).await;
        assert_eq!(outcome, ExportOutcome::Exported);
        assert_eq!(sink.calls(), 1);
        let stats = lock_batcher(&batcher).stats();
        assert_eq!(stats.exported, 1);
        assert!(lock_batcher(&batcher).is_empty());
    }

    #[tokio::test]
    async fn export_pending_re_enqueues_and_records_the_class_on_failure() {
        let batcher = StdMutex::new(Batcher::default());
        lock_batcher(&batcher).enqueue([one_point()]);
        let sink = FakeSink::failing_with(SinkError::Rejected);
        let outcome = export_pending(&batcher, &sink, &test_config(), Duration::from_secs(1)).await;
        assert_eq!(outcome, ExportOutcome::Failed);
        assert_eq!(lock_batcher(&batcher).len(), 1);
        assert_eq!(
            lock_batcher(&batcher).stats().last_error_class.as_deref(),
            Some("rejected")
        );
    }

    #[tokio::test]
    async fn export_pending_re_enqueues_and_records_network_on_timeout() {
        struct NeverResponds;

        impl TelemetrySink for NeverResponds {
            async fn send(&self, _payload: Vec<u8>) -> Result<(), SinkError> {
                std::future::pending().await
            }
        }

        let batcher = StdMutex::new(Batcher::default());
        lock_batcher(&batcher).enqueue([one_point()]);
        let outcome = export_pending(
            &batcher,
            &NeverResponds,
            &test_config(),
            Duration::from_millis(5),
        )
        .await;
        assert_eq!(outcome, ExportOutcome::TimedOut);
        assert_eq!(lock_batcher(&batcher).len(), 1);
        assert_eq!(
            lock_batcher(&batcher).stats().last_error_class.as_deref(),
            Some("network")
        );
    }

    #[test]
    fn report_from_outcome_matches_every_outcome() {
        assert_eq!(
            report_from_outcome(ExportOutcome::NothingPending),
            ParticipantReport {
                completed: true,
                timed_out: false
            }
        );
        assert_eq!(
            report_from_outcome(ExportOutcome::Exported),
            ParticipantReport {
                completed: true,
                timed_out: false
            }
        );
        assert_eq!(
            report_from_outcome(ExportOutcome::Failed),
            ParticipantReport {
                completed: false,
                timed_out: false
            }
        );
        assert_eq!(
            report_from_outcome(ExportOutcome::TimedOut),
            ParticipantReport {
                completed: false,
                timed_out: true
            }
        );
    }
}
