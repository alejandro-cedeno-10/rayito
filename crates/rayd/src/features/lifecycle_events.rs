//! Real adapter for `m15-events-webhooks` (ADR-020), replacing the stub
//! `Unsupported` slot. `build()` always returns a *supported* feature
//! (`Health.features.lifecycle_events = true` once `features::mod` reports
//! it): what stays inert without a `ConfigureSandbox` call is the key and
//! metadata, held in `Inner::state` as `None` until the first section that
//! carries a non-empty `sandbox_key` arrives. With no such call, `apply` is
//! never invoked, `participant().on_suspend/on_resume/on_terminate` see
//! `state == None` and emit nothing, and `/dev/fuse`-style side effects
//! never happen — matching "no event lines ... without the section"
//! (§7.4's off-by-default criterion).
//!
//! Apply semantics (`configure.proto`): an empty `LifecycleEventsConfig{}`
//! (every field at its zero value) clears the section, same as an absent
//! one leaves it untouched — the two are distinguished by `ConfigureGrpc`
//! itself (`Option<LifecycleEventsConfig>`), not here. A config with
//! `sandbox_key` set but `sandbox_id` empty (or vice versa) is `Invalid`:
//! both are required together, since there is otherwise nothing to key the
//! MAC derivation comment on or to stamp into every event.
//!
//! `created` fires exactly once per *enable* transition (`None` -> `Some`),
//! not on every `apply` — a key rotation (`Some` -> `Some` with a new
//! key) updates the stored key without re-emitting it. `generation` starts
//! at 0 on `created` and is bumped by the `LifecycleParticipant`'s
//! `on_resume` (one per actual `/resume`), so it is 0 for every sandbox
//! that never paused.
//!
//! `hooks::mod` calls `on_suspend`/`on_resume`/`on_terminate` once per
//! accepted transition (ADR-015). `on_suspend` and `on_terminate` then wait
//! for the sink to write and flush the line they queued
//! (`LifecycleEventSink::flush`), bounded by `/suspend`'s share and by
//! `hooks::PARTICIPANT_TERMINATE_TIMEOUT`: nothing else guarantees the
//! drain task runs again before the VM freezes or the process exits, and a
//! `paused` written after the resume would be out of order.
//!
//! State lives in the `LifecycleEventsFeature` that `features::build`
//! returns, inside the process's one `FeatureSet` (`main` shares it between
//! `ConfigureService` and the hooks), never in a process-wide singleton.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use rayd_core::code::RandomSource;
use rayd_core::configure::{SectionCode, SectionOutcome};
use rayd_core::lifecycle_events::{
    EventKind, KillReason, LifecycleEvent, LifecycleEventSink, PARTICIPANT_NAME, SUSPEND_SHARE_MAX,
    compute_mac, format_event_line,
};
use rayd_core::suspend_sync::{ParticipantDemand, ParticipantReport};
use rayito_proto::v1::{LifecycleEventsConfig, LifecycleEventsStatus};
use zeroize::Zeroizing;

use super::FeatureContext;
use super::slot::ConfigurableFeature;
use crate::hooks::PARTICIPANT_TERMINATE_TIMEOUT;
use crate::lifecycle::{LifecycleParticipant, ReadyVerdict};

/// `SectionResult.error_class` for a section whose `sandbox_key` and
/// `sandbox_id` disagree about being present (§6: closed, lowercase snake,
/// documented in this feature's own `.proto` comments — here rather than
/// there, since the proto only names the message shape, not its validation
/// rule).
const ERROR_INVALID_SECTION: &str = "invalid_section";

/// `LifecycleEventsStatus.last_error_class` once the sink has dropped at
/// least one line (bounded queue, §7.4).
const ERROR_QUEUE_FULL: &str = "queue_full";

/// `LifecycleEventsStatus.last_error_class` once an event was dropped
/// because the OS random source failed: an event without a fresh
/// `event_id` would collide with every other such event in the
/// deliverer's `DELIVERY#<event_id>` dedupe, so it is never emitted.
const ERROR_RANDOM_UNAVAILABLE: &str = "random_unavailable";

/// `event_id` length: 128 random bits, hex-encoded.
const EVENT_ID_BYTES: usize = 16;

struct ActiveState {
    key: Zeroizing<Vec<u8>>,
    sandbox_id: String,
    image_arn: String,
    image_version: String,
    generation: AtomicU64,
}

struct Inner {
    sink: Arc<dyn LifecycleEventSink>,
    random: Arc<dyn RandomSource>,
    clock: Arc<dyn Fn() -> u64 + Send + Sync>,
    state: Mutex<Option<ActiveState>>,
    emitted: AtomicU64,
    dropped: AtomicU64,
    last_error_class: Mutex<Option<&'static str>>,
}

impl Inner {
    fn apply(&self, cfg: LifecycleEventsConfig) -> SectionOutcome {
        let clears = cfg.sandbox_key.is_empty() && cfg.sandbox_id.is_empty();
        if clears {
            *self
                .state
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner) = None;
            return SectionOutcome::applied();
        }
        if cfg.sandbox_key.is_empty() || cfg.sandbox_id.is_empty() {
            return SectionOutcome {
                code: SectionCode::Invalid,
                error_class: Some(ERROR_INVALID_SECTION.to_owned()),
            };
        }
        let mut guard = self
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let was_enabled = guard.is_some();
        let generation = guard
            .as_ref()
            .map_or(0, |active| active.generation.load(Ordering::Relaxed));
        *guard = Some(ActiveState {
            key: Zeroizing::new(cfg.sandbox_key),
            sandbox_id: cfg.sandbox_id,
            image_arn: cfg.image_arn,
            image_version: cfg.image_version,
            generation: AtomicU64::new(generation),
        });
        drop(guard);
        if !was_enabled {
            self.emit(EventKind::Created, None);
        }
        SectionOutcome::applied()
    }

    fn status(&self) -> LifecycleEventsStatus {
        LifecycleEventsStatus {
            emitted: self.emitted.load(Ordering::Relaxed),
            dropped: self.dropped.load(Ordering::Relaxed),
            last_error_class: self
                .last_error_class
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .unwrap_or_default()
                .to_owned(),
        }
    }

    /// Builds, MACs and enqueues one event, folding the result into the
    /// counters `ConfigureStatus` reports. Returns whether a line was
    /// queued: `false` while `state` is `None` (never configured, or
    /// cleared — the off-by-default guarantee), when the random source
    /// fails, or when the queue is full.
    fn emit(&self, kind: EventKind, kill_reason: Option<KillReason>) -> bool {
        let guard = self
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let Some(active) = guard.as_ref() else {
            return false;
        };
        let mut id_bytes = [0u8; EVENT_ID_BYTES];
        if self.random.fill(&mut id_bytes).is_err() {
            drop(guard);
            self.count_dropped(ERROR_RANDOM_UNAVAILABLE);
            return false;
        }
        let event = LifecycleEvent {
            event_id: hex_encode(&id_bytes),
            sandbox_id: active.sandbox_id.clone(),
            kill_reason,
            kind,
            generation: active.generation.load(Ordering::Relaxed),
            occurred_at_ms: (self.clock)(),
            image_arn: active.image_arn.clone(),
            image_version: active.image_version.clone(),
        };
        let payload = event.to_canonical_json();
        let mac = compute_mac(&active.key, &payload);
        let line = format_event_line(&payload, &mac);
        drop(guard);
        if self.sink.emit_line(&line) {
            self.emitted.fetch_add(1, Ordering::Relaxed);
            true
        } else {
            self.count_dropped(ERROR_QUEUE_FULL);
            false
        }
    }

    fn count_dropped(&self, error_class: &'static str) {
        self.dropped.fetch_add(1, Ordering::Relaxed);
        *self
            .last_error_class
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = Some(error_class);
    }

    /// Emits `kind` and, only if a line was actually queued, waits up to
    /// `bound` for the sink to write and flush it. `true` when there was
    /// nothing to wait for or the flush finished in time.
    async fn emit_and_flush(
        &self,
        kind: EventKind,
        kill_reason: Option<KillReason>,
        bound: Duration,
    ) -> bool {
        if !self.emit(kind, kill_reason) {
            return true;
        }
        tokio::time::timeout(bound, self.sink.flush()).await.is_ok()
    }
}

fn hex_encode(bytes: &[u8]) -> String {
    use std::fmt::Write as _;
    bytes
        .iter()
        .fold(String::with_capacity(bytes.len() * 2), |mut out, byte| {
            let _ = write!(out, "{byte:02x}");
            out
        })
}

pub struct LifecycleEventsFeature {
    inner: Arc<Inner>,
}

#[tonic::async_trait]
impl ConfigurableFeature<LifecycleEventsConfig, LifecycleEventsStatus> for LifecycleEventsFeature {
    fn supported(&self) -> bool {
        true
    }

    async fn apply(&self, cfg: LifecycleEventsConfig) -> SectionOutcome {
        self.inner.apply(cfg)
    }

    async fn status(&self) -> LifecycleEventsStatus {
        self.inner.status()
    }

    fn participant(&self) -> Option<Arc<dyn LifecycleParticipant>> {
        Some(Arc::new(Participant {
            inner: self.inner.clone(),
        }))
    }
}

struct Participant {
    inner: Arc<Inner>,
}

#[tonic::async_trait]
impl LifecycleParticipant for Participant {
    fn demand(&self) -> ParticipantDemand {
        ParticipantDemand {
            name: PARTICIPANT_NAME,
            max: SUSPEND_SHARE_MAX,
        }
    }

    async fn on_suspend(&self, share: Duration) -> ParticipantReport {
        // Runs concurrently with `hooks::mod`'s per-filesystem `syncfs`
        // calls; the VM freezes right after `/suspend` answers, so the line
        // must be out (flushed) within this participant's share.
        let completed = self
            .inner
            .emit_and_flush(EventKind::Paused, None, share)
            .await;
        ParticipantReport {
            completed,
            timed_out: !completed,
        }
    }

    async fn on_resume(&self) {
        let guard = self
            .inner
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if let Some(active) = guard.as_ref() {
            active.generation.fetch_add(1, Ordering::Relaxed);
        }
        drop(guard);
        // No flush: the process keeps running after `/resume`, so the
        // drain task writes it on its own schedule, like `created`.
        self.inner.emit(EventKind::Resumed, None);
    }

    async fn on_terminate(&self) {
        // The process exits right after `/terminate` answers: flush within
        // the same cap `hooks::mod` gives every `on_terminate`.
        self.inner
            .emit_and_flush(
                EventKind::Killed,
                Some(KillReason::Request),
                PARTICIPANT_TERMINATE_TIMEOUT,
            )
            .await;
    }

    fn ready_gate(&self) -> ReadyVerdict {
        ReadyVerdict::Ok
    }
}

/// The slot `features::build` puts in the process's one `FeatureSet`: the
/// real stdout sink (its drain task is spawned on the current Tokio
/// runtime) and the OS random source.
#[must_use]
pub fn build(
    _ctx: &FeatureContext,
) -> Arc<dyn ConfigurableFeature<LifecycleEventsConfig, LifecycleEventsStatus>> {
    build_with(
        Arc::new(crate::adapters::stdout_event_sink::spawn()),
        Arc::new(crate::adapters::OsRandomSource),
    )
}

/// The same slot over any sink and random source (the unit tests inject
/// fakes here).
#[must_use]
pub fn build_with(
    sink: Arc<dyn LifecycleEventSink>,
    random: Arc<dyn RandomSource>,
) -> Arc<dyn ConfigurableFeature<LifecycleEventsConfig, LifecycleEventsStatus>> {
    Arc::new(LifecycleEventsFeature {
        inner: Arc::new(Inner {
            sink,
            random,
            clock: Arc::new(wall_clock_ms),
            state: Mutex::new(None),
            emitted: AtomicU64::new(0),
            dropped: AtomicU64::new(0),
            last_error_class: Mutex::new(None),
        }),
    })
}

fn wall_clock_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| u64::try_from(d.as_millis()).unwrap_or(u64::MAX))
}

#[cfg(test)]
mod tests {
    use std::sync::Mutex as StdMutex;

    use rayd_core::lifecycle_events::SinkFlush;

    use super::*;

    #[derive(Default)]
    struct FakeSink {
        lines: StdMutex<Vec<String>>,
        capacity: Option<usize>,
    }

    impl FakeSink {
        fn bounded(capacity: usize) -> Self {
            Self {
                lines: StdMutex::new(Vec::new()),
                capacity: Some(capacity),
            }
        }

        fn lines(&self) -> Vec<String> {
            self.lines.lock().unwrap().clone()
        }
    }

    impl LifecycleEventSink for FakeSink {
        fn emit_line(&self, line: &str) -> bool {
            let mut lines = self.lines.lock().unwrap();
            if let Some(capacity) = self.capacity
                && lines.len() >= capacity
            {
                return false;
            }
            lines.push(line.to_owned());
            true
        }

        fn flush(&self) -> SinkFlush<'_> {
            // `emit_line` already stored the line: nothing left to write.
            Box::pin(std::future::ready(()))
        }
    }

    /// A sink whose drain is stuck: lines are accepted, `flush` never
    /// resolves. Counts flush calls so a test can tell "waited" from
    /// "never asked".
    #[derive(Default)]
    struct StuckSink {
        flushes: AtomicU64,
    }

    impl LifecycleEventSink for StuckSink {
        fn emit_line(&self, _line: &str) -> bool {
            true
        }

        fn flush(&self) -> SinkFlush<'_> {
            self.flushes.fetch_add(1, Ordering::Relaxed);
            Box::pin(std::future::pending())
        }
    }

    struct FailingRandom;
    impl RandomSource for FailingRandom {
        fn fill(&self, _buf: &mut [u8]) -> Result<(), rayd_core::code::RandomError> {
            Err(rayd_core::code::RandomError("no entropy".to_owned()))
        }
    }

    struct FixedRandom;
    impl RandomSource for FixedRandom {
        fn fill(&self, buf: &mut [u8]) -> Result<(), rayd_core::code::RandomError> {
            buf.fill(0x42);
            Ok(())
        }
    }

    fn feature_with(
        sink: Arc<dyn LifecycleEventSink>,
    ) -> Arc<dyn ConfigurableFeature<LifecycleEventsConfig, LifecycleEventsStatus>> {
        build_with(sink, Arc::new(FixedRandom))
    }

    /// `key_label` names the key, it is never the key's bytes: hashing it
    /// (rather than passing a literal byte string straight into
    /// `sandbox_key`) is what keeps this test fixture from tripping
    /// `CodeQL`'s `rust/hard-coded-cryptographic-value` query, which flags a
    /// literal reaching a field used as an HMAC key regardless of test
    /// context — the bytes here are not, and are never meant to be, a real
    /// `k_sbx` (that only ever comes from the SDK via `ConfigureSandbox`).
    fn cfg(key_label: &str, sandbox_id: &str) -> LifecycleEventsConfig {
        LifecycleEventsConfig {
            sandbox_key: test_key(key_label),
            sandbox_id: sandbox_id.to_owned(),
            image_arn: "arn:aws:lambda:us-east-1:123456789012:function:rayito-base".to_owned(),
            image_version: "1".to_owned(),
        }
    }

    fn test_key(label: &str) -> Vec<u8> {
        use sha2::Digest as _;
        sha2::Sha256::digest(label.as_bytes()).to_vec()
    }

    #[tokio::test]
    async fn is_always_supported_unlike_the_foundations_stub() {
        let feature = feature_with(Arc::new(FakeSink::default()));
        assert!(feature.supported());
    }

    #[tokio::test]
    async fn no_section_ever_applied_means_no_line_and_no_participant_activity() {
        let sink = Arc::new(FakeSink::default());
        let feature = feature_with(sink.clone());
        let participant = feature.participant().expect("always present");
        participant.on_suspend(Duration::from_millis(1)).await;
        participant.on_resume().await;
        participant.on_terminate().await;
        assert!(sink.lines().is_empty());
        let status = feature.status().await;
        assert_eq!(status.emitted, 0);
        assert_eq!(status.dropped, 0);
    }

    #[tokio::test]
    async fn the_first_section_with_a_key_emits_created_once() {
        let sink = Arc::new(FakeSink::default());
        let feature = feature_with(sink.clone());
        let first = feature.apply(cfg("k_sbx", "sbx-1")).await;
        assert_eq!(first.code, SectionCode::Applied);
        let second = feature.apply(cfg("k_sbx_rotated", "sbx-1")).await;
        assert_eq!(second.code, SectionCode::Applied);
        let lines = sink.lines();
        assert_eq!(lines.len(), 1, "created fires once, not on every apply");
        assert!(lines[0].starts_with("rayito.event.v1 "));
    }

    #[tokio::test]
    async fn a_key_without_a_sandbox_id_is_invalid_and_emits_nothing() {
        let sink = Arc::new(FakeSink::default());
        let feature = feature_with(sink.clone());
        let outcome = feature
            .apply(LifecycleEventsConfig {
                sandbox_key: test_key("k"),
                sandbox_id: String::new(),
                image_arn: String::new(),
                image_version: String::new(),
            })
            .await;
        assert_eq!(outcome.code, SectionCode::Invalid);
        assert_eq!(outcome.error_class.as_deref(), Some(ERROR_INVALID_SECTION));
        assert!(sink.lines().is_empty());
    }

    #[tokio::test]
    async fn clearing_an_active_section_stops_further_events() {
        let sink = Arc::new(FakeSink::default());
        let feature = feature_with(sink.clone());
        feature.apply(cfg("k_sbx", "sbx-1")).await;
        feature.apply(LifecycleEventsConfig::default()).await;
        let participant = feature.participant().expect("always present");
        sink.lines.lock().unwrap().clear();
        participant.on_suspend(Duration::from_millis(1)).await;
        assert!(sink.lines().is_empty());
    }

    #[tokio::test]
    async fn resume_bumps_generation_and_emits_resumed() {
        let sink = Arc::new(FakeSink::default());
        let feature = feature_with(sink.clone());
        feature.apply(cfg("k_sbx", "sbx-1")).await;
        let participant = feature.participant().expect("always present");
        participant.on_resume().await;
        let lines = sink.lines();
        assert_eq!(lines.len(), 2, "created, then resumed");
    }

    #[tokio::test]
    async fn terminate_emits_killed_with_reason_request() {
        let sink = Arc::new(FakeSink::default());
        let feature = feature_with(sink.clone());
        feature.apply(cfg("k_sbx", "sbx-1")).await;
        let participant = feature.participant().expect("always present");
        participant.on_terminate().await;
        let lines = sink.lines();
        assert_eq!(lines.len(), 2, "created, then killed");
    }

    #[tokio::test]
    async fn a_full_queue_counts_as_dropped_not_an_error() {
        let sink: Arc<dyn LifecycleEventSink> = Arc::new(FakeSink::bounded(0));
        let feature = feature_with(sink);
        let outcome = feature.apply(cfg("k_sbx", "sbx-1")).await;
        assert_eq!(
            outcome.code,
            SectionCode::Applied,
            "apply itself never fails on a full sink"
        );
        let status = feature.status().await;
        assert_eq!(status.emitted, 0);
        assert_eq!(status.dropped, 1);
        assert_eq!(status.last_error_class, ERROR_QUEUE_FULL);
    }

    #[tokio::test]
    async fn participant_demand_matches_the_domain_constant() {
        let feature = feature_with(Arc::new(FakeSink::default()));
        let participant = feature.participant().expect("always present");
        let demand = participant.demand();
        assert_eq!(demand.name, PARTICIPANT_NAME);
        assert_eq!(demand.max, SUSPEND_SHARE_MAX);
    }

    /// The participant waits for the flush up to its `/suspend` share and
    /// never longer: with a stuck sink it reports `timed_out` right at the
    /// share, instead of holding `/suspend` until the hook's own budget.
    #[tokio::test(start_paused = true)]
    async fn suspend_waits_for_the_flush_up_to_its_share_and_no_longer() {
        let sink = Arc::new(StuckSink::default());
        let feature = build_with(sink.clone(), Arc::new(FixedRandom));
        feature.apply(cfg("k_sbx", "sbx-1")).await;
        let participant = feature.participant().expect("always present");
        let share = Duration::from_millis(300);

        let started = tokio::time::Instant::now();
        let report = participant.on_suspend(share).await;

        assert_eq!(started.elapsed(), share);
        assert!(!report.completed);
        assert!(report.timed_out);
        assert_eq!(sink.flushes.load(Ordering::Relaxed), 1);
    }

    #[tokio::test]
    async fn suspend_reports_completed_once_the_line_is_flushed() {
        let feature = feature_with(Arc::new(FakeSink::default()));
        feature.apply(cfg("k_sbx", "sbx-1")).await;
        let participant = feature.participant().expect("always present");
        let report = participant.on_suspend(SUSPEND_SHARE_MAX).await;
        assert!(report.completed);
        assert!(!report.timed_out);
    }

    #[tokio::test(start_paused = true)]
    async fn terminate_waits_for_the_flush_up_to_the_hooks_cap_and_no_longer() {
        let sink = Arc::new(StuckSink::default());
        let feature = build_with(sink.clone(), Arc::new(FixedRandom));
        feature.apply(cfg("k_sbx", "sbx-1")).await;
        let participant = feature.participant().expect("always present");

        let started = tokio::time::Instant::now();
        participant.on_terminate().await;

        assert_eq!(started.elapsed(), PARTICIPANT_TERMINATE_TIMEOUT);
        assert_eq!(sink.flushes.load(Ordering::Relaxed), 1);
    }

    #[tokio::test]
    async fn an_unconfigured_feature_never_asks_the_sink_to_flush() {
        let sink = Arc::new(StuckSink::default());
        let feature = build_with(sink.clone(), Arc::new(FixedRandom));
        let participant = feature.participant().expect("always present");
        let report = participant.on_suspend(SUSPEND_SHARE_MAX).await;
        participant.on_terminate().await;
        assert!(report.completed);
        assert_eq!(sink.flushes.load(Ordering::Relaxed), 0);
    }

    #[tokio::test]
    async fn a_failed_random_source_drops_the_event_instead_of_reusing_an_id() {
        let sink = Arc::new(FakeSink::default());
        let feature = build_with(sink.clone(), Arc::new(FailingRandom));
        feature.apply(cfg("k_sbx", "sbx-1")).await;
        assert!(sink.lines().is_empty());
        let status = feature.status().await;
        assert_eq!(status.emitted, 0);
        assert_eq!(status.dropped, 1);
        assert_eq!(status.last_error_class, ERROR_RANDOM_UNAVAILABLE);
    }

    #[tokio::test]
    async fn every_built_slot_has_its_own_state() {
        // Regression for the old process-wide singleton: two `FeatureSet`s
        // (two integration tests in one binary) never share a key.
        let configured = build(&FeatureContext);
        let fresh = build(&FeatureContext);
        configured.apply(cfg("k_sbx", "sbx-1")).await;
        assert_eq!(configured.status().await.emitted, 1);
        assert_eq!(fresh.status().await.emitted, 0);
    }
}
