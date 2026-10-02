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
//!
//! `/suspend`'s bounded flush (`TelemetryParticipant`, `LifecycleParticipant`)
//! and the background task share one `SharedState` so a flush never races a
//! re-`apply()`: both go through the same `tokio::sync::Mutex`.

use std::sync::{Arc, Mutex as StdMutex, PoisonError};
use std::time::Duration;

use rayd_core::configure::{SectionCode, SectionOutcome};
use rayd_core::metrics_history::MetricsHistory;
use rayd_core::session::SandboxSession;
use rayd_core::suspend_sync::{ParticipantDemand, ParticipantReport};
use rayd_core::telemetry::{
    self as domain, Batcher, NameStyle as DomainNameStyle, OtlpEncoder as _, ResourceAttrs,
    TelemetryAuth, TelemetryConfig, jittered_backoff, points_from_sample,
};
use rayito_proto::v1::telemetry_export_config::Auth as WireAuth;
use rayito_proto::v1::{
    BearerAuth, ExecutionRoleAuth, NameStyle as WireNameStyle, TelemetryExportConfig,
    TelemetryExportStatus,
};
use tokio::sync::{Mutex as AsyncMutex, Notify};
use tokio::task::JoinHandle;
use zeroize::Zeroizing;

use super::FeatureContext;
use super::slot::{ConfigurableFeature, Unsupported};
use crate::adapters::cloudwatch_otlp_sink::{CloudWatchOtlpSink, SinkCredentials};
use crate::adapters::otlp_codec::ProstOtlpEncoder;
use crate::adapters::random::OsRandomSource;
use crate::adapters::{ImdsCredentialBroker, PushedCredentials};
use crate::lifecycle::LifecycleParticipant;

/// Key `PushedCredentials` stores a bearer token under: fixed and
/// feature-private, so it can never collide with another feature's pushed
/// value (secret-gateway's vaulted headers use their own route-scoped
/// keys).
const PUSHED_BEARER_KEY: &str = "m15-rayd-otlp.bearer";
pub const PARTICIPANT_NAME: &str = "telemetry_export";
/// `/suspend` share ceiling (architecture §7.5: "flush acotado... máximo
/// 2 s"); the actual share `SuspendShares::allocate` hands out is at most
/// this *and* at most the hook's own `sync_deadline`.
const SUSPEND_FLUSH_MAX: Duration = Duration::from_secs(2);
/// Caps one export POST so a hung endpoint cannot stall the exporter task
/// and delay the next sampling tick indefinitely.
const EXPORT_ATTEMPT_TIMEOUT: Duration = Duration::from_secs(8);

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
    pushed: Arc<PushedCredentials>,
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
            self.pushed.clear(PUSHED_BEARER_KEY);
            return SectionOutcome::applied();
        }
        let names = match WireNameStyle::try_from(cfg.names) {
            Ok(WireNameStyle::E2b) => DomainNameStyle::E2b,
            _ => DomainNameStyle::Rayito,
        };
        let auth = match &cfg.auth {
            Some(WireAuth::ExecutionRole(ExecutionRoleAuth {})) => {
                Some(TelemetryAuth::ExecutionRole)
            }
            Some(WireAuth::Bearer(BearerAuth { token })) => {
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
        if let TelemetryAuth::Bearer(token) = &config.auth {
            self.pushed.set(PUSHED_BEARER_KEY, token.clone());
        } else {
            self.pushed.clear(PUSHED_BEARER_KEY);
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
        let pending = lock_batcher(&running.batcher).drain();
        if pending.is_empty() {
            return ParticipantReport {
                completed: true,
                timed_out: false,
            };
        }
        let payload = ProstOtlpEncoder.encode(
            &running.config.resource,
            &running.config.service_name,
            running.config.names,
            &pending,
        );
        let exported = u64::try_from(pending.len()).unwrap_or(u64::MAX);
        match tokio::time::timeout(share, running.sink.send_with(&running.credentials, payload))
            .await
        {
            Ok(Ok(())) => {
                lock_batcher(&running.batcher).record_exported(exported);
                ParticipantReport {
                    completed: true,
                    timed_out: false,
                }
            }
            Ok(Err(error)) => {
                lock_batcher(&running.batcher).enqueue(pending);
                lock_batcher(&running.batcher).record_failure(sink_error_class(error));
                ParticipantReport {
                    completed: false,
                    timed_out: false,
                }
            }
            Err(_elapsed) => {
                lock_batcher(&running.batcher).enqueue(pending);
                ParticipantReport {
                    completed: false,
                    timed_out: true,
                }
            }
        }
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
    let state = Arc::new(SharedState {
        region,
        session: ctx.session.clone(),
        history: ctx.history.clone(),
        credentials: ctx.credentials.clone(),
        pushed: ctx.pushed.clone(),
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

    // `on_run`/`on_resume`/`on_terminate`/`ready_gate` keep the trait's
    // defaults: telemetry never gates readiness, and resuming a pooled
    // connection that outlived a long pause is already handled by
    // `CloudWatchOtlpSink`'s own idle timeout (`POOL_IDLE_TIMEOUT`, 30 s)
    // closing it well before any pause research measures (60 s, 10 min,
    // 70 min) ends.
}

/// One tick per `config.interval`: sample `MetricsHistory`'s latest point,
/// enqueue it, and attempt to drain and export whatever is pending (which
/// may include points from earlier failed attempts). A failed send
/// backs off (`jittered_backoff`) before the next real attempt, but
/// sampling itself never stops — a slow network only delays exporting,
/// never observing.
fn spawn_exporter(
    config: TelemetryConfig,
    history: Arc<MetricsHistory>,
    sink: Arc<CloudWatchOtlpSink>,
    credentials: SinkCredentials,
    batcher: Arc<StdMutex<Batcher>>,
    stop: Arc<Notify>,
) -> JoinHandle<()> {
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
            let pending = lock_batcher(&batcher).drain();
            if pending.is_empty() {
                continue;
            }
            let payload = ProstOtlpEncoder.encode(
                &config.resource,
                &config.service_name,
                config.names,
                &pending,
            );
            let outcome = tokio::time::timeout(
                EXPORT_ATTEMPT_TIMEOUT,
                sink.send_with(&credentials, payload),
            )
            .await;
            match outcome {
                Ok(Ok(())) => {
                    attempt = 0;
                    retry_not_before = None;
                    lock_batcher(&batcher)
                        .record_exported(u64::try_from(pending.len()).unwrap_or(u64::MAX));
                }
                Ok(Err(error)) => {
                    attempt = attempt.saturating_add(1);
                    retry_not_before = Some(
                        tokio::time::Instant::now() + jittered_backoff(attempt, &OsRandomSource),
                    );
                    lock_batcher(&batcher).enqueue(pending);
                    lock_batcher(&batcher).record_failure(sink_error_class(error));
                }
                Err(_elapsed) => {
                    attempt = attempt.saturating_add(1);
                    retry_not_before = Some(
                        tokio::time::Instant::now() + jittered_backoff(attempt, &OsRandomSource),
                    );
                    lock_batcher(&batcher).enqueue(pending);
                    lock_batcher(&batcher).record_failure("network");
                }
            }
        }
    })
}

#[cfg(test)]
mod tests {
    use rayd_core::clock::SystemClock;

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
        let feature = build(&context_with_region());
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
        let feature = build(&context_with_region());
        let outcome = feature
            .apply(TelemetryExportConfig {
                interval_s: 5,
                service_name: "agente".to_owned(),
                auth: Some(WireAuth::ExecutionRole(ExecutionRoleAuth {})),
                ..Default::default()
            })
            .await;
        assert_eq!(outcome.code, SectionCode::Invalid);
        assert_eq!(outcome.error_class.as_deref(), Some("invalid_interval"));
    }

    #[tokio::test]
    async fn a_valid_section_applies_and_status_starts_at_zero() {
        let feature = build(&context_with_region());
        let outcome = feature
            .apply(TelemetryExportConfig {
                interval_s: 60,
                service_name: "agente".to_owned(),
                auth: Some(WireAuth::ExecutionRole(ExecutionRoleAuth {})),
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
        let feature = build(&context_with_region());
        feature
            .apply(TelemetryExportConfig {
                interval_s: 60,
                service_name: "agente".to_owned(),
                auth: Some(WireAuth::ExecutionRole(ExecutionRoleAuth {})),
                ..Default::default()
            })
            .await;
        let outcome = feature.apply(TelemetryExportConfig::default()).await;
        assert_eq!(outcome.code, SectionCode::Applied);
    }

    #[tokio::test]
    async fn a_bearer_token_is_pushed_and_cleared_on_stop() {
        let ctx = context_with_region();
        let pushed = ctx.pushed.clone();
        let feature = build(&ctx);
        feature
            .apply(TelemetryExportConfig {
                interval_s: 60,
                service_name: "agente".to_owned(),
                auth: Some(WireAuth::Bearer(BearerAuth {
                    token: "sk-test".to_owned(),
                })),
                ..Default::default()
            })
            .await;
        assert_eq!(
            pushed.get(PUSHED_BEARER_KEY).as_deref().map(String::as_str),
            Some("sk-test")
        );
        feature.apply(TelemetryExportConfig::default()).await;
        assert!(pushed.get(PUSHED_BEARER_KEY).is_none());
    }

    #[tokio::test]
    async fn a_participant_with_nothing_running_reports_a_completed_noop_flush() {
        let feature = build(&context_with_region());
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
        let feature = build(&context_with_region());
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
            pushed: Arc::new(PushedCredentials::new()),
            running: AsyncMutex::new(None),
        };
        assert_eq!(state.sandbox_id(), "");
    }
}
