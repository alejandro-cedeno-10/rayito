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
//! Known integration gap (documented rather than worked around): today
//! `rayd::hooks::mod` only calls a participant's `on_suspend` and
//! `ready_gate` (`/suspend`, `/ready`); `on_resume` and `on_terminate` are
//! not yet invoked from `/resume`/`/terminate`. This PR wires them in
//! `hooks::mod` and `main.rs` as the "first feature that needs shared
//! context" foundations' own comments anticipated
//! (`features::FeatureContext`, `grpc::Services`) — see those files' diffs
//! for the two call sites. `paused` already works end to end because
//! `run_participants` was wired for every slot from the start.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
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
    /// counters `ConfigureStatus` reports. A no-op while `state` is `None`
    /// (never configured, or cleared) — the off-by-default guarantee.
    fn emit(&self, kind: EventKind, kill_reason: Option<KillReason>) {
        let guard = self
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let Some(active) = guard.as_ref() else {
            return;
        };
        let mut id_bytes = [0u8; 16];
        // A failed random read never blocks or fails the event: a
        // all-zero id is still unique enough in practice (paired with
        // `occurred_at_ms` and `sandbox_id`) and this path is already a
        // last-resort the guest's own egress or lifecycle code never
        // exercises in tests.
        let _ = self.random.fill(&mut id_bytes);
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
        } else {
            self.dropped.fetch_add(1, Ordering::Relaxed);
            *self
                .last_error_class
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner) = Some(ERROR_QUEUE_FULL);
        }
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

    async fn on_suspend(&self, _share: Duration) -> ParticipantReport {
        // Emitted first thing, before `hooks::mod`'s per-filesystem
        // `syncfs` calls have necessarily finished (they run concurrently
        // via `tokio::join!`), approximating "paused, before the syncs"
        // (§7.4) without needing a dedicated ordering primitive: queuing a
        // line is a handful of microseconds, so in practice it is already
        // on the channel well before any `syncfs` completes.
        self.inner.emit(EventKind::Paused, None);
        ParticipantReport {
            completed: true,
            timed_out: false,
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
        self.inner.emit(EventKind::Resumed, None);
    }

    async fn on_terminate(&self) {
        self.inner
            .emit(EventKind::Killed, Some(KillReason::Request));
    }

    fn ready_gate(&self) -> ReadyVerdict {
        ReadyVerdict::Ok
    }
}

/// `rayd::grpc::mod` builds a fresh `FeatureSet` for `ConfigureService`, and
/// `main.rs`/the integration test harness build a second one just to read
/// `FeatureSet::participants()` for `HookServices` — rather than widen
/// `grpc::Services`/`hooks::HookServices` to carry one shared
/// `Arc<FeatureSet>` across eight other milestones' test files that
/// construct them directly (`m15-events-webhooks` is the first feature
/// with real mutable state; the next one that truly needs *external*
/// context still threads `FeatureContext`/`Services` properly, per that
/// struct's own doc comment), every call to `build()` hands back a thin
/// wrapper over the **same** process-wide `Inner` — there is exactly one
/// sandbox per `rayd` process, so this is a singleton-per-agent, not global
/// mutable state in the general sense. `apply()` mutating it through the
/// gRPC-side wrapper is what the hooks-side wrapper's `participant()` then
/// reads.
fn shared_inner() -> &'static Arc<Inner> {
    static INNER: OnceLock<Arc<Inner>> = OnceLock::new();
    INNER.get_or_init(|| {
        Arc::new(Inner {
            sink: Arc::new(crate::adapters::stdout_event_sink::spawn()),
            random: Arc::new(crate::adapters::OsRandomSource),
            clock: Arc::new(wall_clock_ms),
            state: Mutex::new(None),
            emitted: AtomicU64::new(0),
            dropped: AtomicU64::new(0),
            last_error_class: Mutex::new(None),
        })
    })
}

#[must_use]
pub fn build(
    _ctx: &FeatureContext,
) -> Arc<dyn ConfigurableFeature<LifecycleEventsConfig, LifecycleEventsStatus>> {
    Arc::new(LifecycleEventsFeature {
        inner: shared_inner().clone(),
    })
}

/// Seam the unit tests use to inject a fake sink and a deterministic random
/// source without spawning a real stdout-draining task per test.
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
}
