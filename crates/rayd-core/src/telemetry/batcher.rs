//! The bounded queue between the 5 s sampler and the network (research
//! §6.5): `Batcher` decouples "a point was sampled" from "a batch was
//! exported", so a slow or unreachable `CloudWatch` endpoint never blocks the
//! sampler or a lifecycle hook. `jittered_backoff` spreads retries per
//! sandbox so a mass `/resume` doesn't synchronize every exporter's retry
//! on the same AWS-side TPS window (research §6.4). `TelemetrySink` and
//! `OtlpEncoder` are the two ports that keep this crate free of
//! `opentelemetry-proto` and any HTTP client; both adapters live in `rayd`
//! (`adapters::{otlp_codec,cloudwatch_otlp_sink}`).

use std::collections::VecDeque;
use std::future::Future;
use std::time::Duration;

use thiserror::Error;

use crate::code::ports::RandomSource;

use super::model::{MetricPoint, NameStyle, ResourceAttrs};

/// Upper bound on buffered points. At the shortest allowed interval (15 s)
/// and 7 gauges per tick, this holds roughly 9 minutes of samples
/// (`500 / 7 / (60/15)` ticks) before the oldest point is dropped rather
/// than growing without bound while the network is unreachable.
pub const QUEUE_CAPACITY: usize = 500;

/// First retry delay; doubles per failed attempt up to `MAX_BACKOFF`.
pub const BASE_BACKOFF: Duration = Duration::from_millis(500);
/// Ceiling on the retry delay, regardless of how many attempts failed.
pub const MAX_BACKOFF: Duration = Duration::from_secs(30);
/// Doublings before the delay is already at `MAX_BACKOFF`
/// (`500ms * 2^6 = 32s`); caps the exponent so it never overflows.
const MAX_BACKOFF_DOUBLINGS: u32 = 6;

/// What `ConfigureStatus`'s `TelemetryExportStatus` reports: counts only,
/// never a host, a payload or a credential.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct BatcherStats {
    pub exported: u64,
    pub dropped: u64,
    pub last_error_class: Option<String>,
}

/// The bounded FIFO of sampled points awaiting export. Dropping the
/// *oldest* point when full (rather than refusing the newest) keeps the
/// most recent state in the queue, which is what a dashboard catching up
/// after an outage cares about.
pub struct Batcher {
    queue: VecDeque<MetricPoint>,
    capacity: usize,
    stats: BatcherStats,
}

impl Batcher {
    #[must_use]
    pub fn new(capacity: usize) -> Self {
        Self {
            queue: VecDeque::with_capacity(capacity.min(QUEUE_CAPACITY)),
            capacity,
            stats: BatcherStats::default(),
        }
    }

    pub fn enqueue(&mut self, points: impl IntoIterator<Item = MetricPoint>) {
        for point in points {
            if self.queue.len() >= self.capacity {
                self.queue.pop_front();
                self.stats.dropped += 1;
            }
            self.queue.push_back(point);
        }
    }

    /// Takes every buffered point out for one export attempt; the caller
    /// (`features::telemetry_export`'s background task) puts them back
    /// with `enqueue` on failure only if it chooses to retry before the
    /// next sampling tick overtakes it.
    #[must_use]
    pub fn drain(&mut self) -> Vec<MetricPoint> {
        self.queue.drain(..).collect()
    }

    pub fn record_exported(&mut self, count: u64) {
        self.stats.exported += count;
    }

    pub fn record_failure(&mut self, error_class: &str) {
        self.stats.last_error_class = Some(error_class.to_owned());
    }

    #[must_use]
    pub fn stats(&self) -> BatcherStats {
        self.stats.clone()
    }

    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.queue.is_empty()
    }

    #[must_use]
    pub fn len(&self) -> usize {
        self.queue.len()
    }
}

impl Default for Batcher {
    fn default() -> Self {
        Self::new(QUEUE_CAPACITY)
    }
}

/// `base * 2^attempt` (capped), then scaled into the top half of its own
/// range by one random byte: never less than 50 % of the capped delay, so
/// jitter spreads retries without ever making one suspiciously short.
/// `random.fill` failing (no entropy source wired, e.g. in a unit test
/// fake) falls back to the midpoint rather than panicking: backoff jitter
/// is a kindness to the AWS-side quota, never a correctness requirement.
#[must_use]
pub fn jittered_backoff(attempt: u32, random: &dyn RandomSource) -> Duration {
    let doublings = attempt.min(MAX_BACKOFF_DOUBLINGS);
    let capped = BASE_BACKOFF
        .saturating_mul(1u32 << doublings)
        .min(MAX_BACKOFF);
    let mut byte = [0u8; 1];
    let fraction = match random.fill(&mut byte) {
        Ok(()) => f64::from(byte[0]) / f64::from(u8::MAX),
        Err(_unavailable) => 0.5,
    };
    capped.mul_f64(0.5 + fraction * 0.5)
}

/// What one `/suspend` flush (the exporter's `LifecycleParticipant` share)
/// should attempt: `None` when there is nothing to send or no time to send
/// it in, so the caller never opens a connection for an empty flush.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct BatchPlan {
    pub points: usize,
}

#[must_use]
pub fn plan_suspend_flush(share: Duration, pending: usize) -> Option<BatchPlan> {
    if share.is_zero() || pending == 0 {
        return None;
    }
    Some(BatchPlan { points: pending })
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Error)]
pub enum SinkError {
    #[error("network")]
    Network,
    #[error("rejected")]
    Rejected,
}

/// Turns one batch into the bytes `TelemetrySink::send` ships: pure
/// (`rayd`'s `otlp_codec` builds an `ExportMetricsServiceRequest` and
/// serializes + gzips it), so this crate never depends on
/// `opentelemetry-proto`.
pub trait OtlpEncoder: Send + Sync {
    fn encode(
        &self,
        resource: &ResourceAttrs,
        service_name: &str,
        names: NameStyle,
        points: &[MetricPoint],
    ) -> Vec<u8>;
}

/// One POST of an already-encoded batch. `rayd`'s
/// `adapters::cloudwatch_otlp_sink` signs it (`SigV4` or bearer) and sends it
/// over the shared outbound HTTPS client; this port never sees a host, a
/// credential or the encoded bytes' shape.
pub trait TelemetrySink: Send + Sync + 'static {
    fn send(&self, payload: Vec<u8>) -> impl Future<Output = Result<(), SinkError>> + Send;
}

#[cfg(test)]
mod tests {
    use std::time::SystemTime;

    use super::*;
    use crate::telemetry::model::GaugeKind;

    struct FixedRandom(u8);

    impl RandomSource for FixedRandom {
        fn fill(&self, buf: &mut [u8]) -> Result<(), crate::code::ports::RandomError> {
            buf.fill(self.0);
            Ok(())
        }
    }

    struct FailingRandom;

    impl RandomSource for FailingRandom {
        fn fill(&self, _buf: &mut [u8]) -> Result<(), crate::code::ports::RandomError> {
            Err(crate::code::ports::RandomError("no entropy".to_owned()))
        }
    }

    fn point() -> MetricPoint {
        MetricPoint {
            kind: GaugeKind::CpuUsedPct,
            value: 1.0,
            timestamp: SystemTime::UNIX_EPOCH,
        }
    }

    #[test]
    fn enqueue_and_drain_round_trip_in_order() {
        let mut batcher = Batcher::new(10);
        batcher.enqueue([point(), point()]);
        assert_eq!(batcher.len(), 2);
        let drained = batcher.drain();
        assert_eq!(drained.len(), 2);
        assert!(batcher.is_empty());
    }

    #[test]
    fn a_full_queue_drops_the_oldest_point_and_counts_it() {
        let mut batcher = Batcher::new(2);
        batcher.enqueue([point(), point(), point()]);
        assert_eq!(batcher.len(), 2);
        assert_eq!(batcher.stats().dropped, 1);
    }

    #[test]
    fn stats_track_exports_and_the_last_failure_class() {
        let mut batcher = Batcher::new(10);
        batcher.record_exported(7);
        batcher.record_failure("network");
        let stats = batcher.stats();
        assert_eq!(stats.exported, 7);
        assert_eq!(stats.last_error_class.as_deref(), Some("network"));
    }

    #[test]
    fn backoff_never_exceeds_the_ceiling_however_many_attempts() {
        let random = FixedRandom(u8::MAX);
        for attempt in 0..20 {
            assert!(jittered_backoff(attempt, &random) <= MAX_BACKOFF);
        }
    }

    #[test]
    fn backoff_grows_with_attempts_before_it_caps() {
        let random = FixedRandom(0);
        let first = jittered_backoff(0, &random);
        let third = jittered_backoff(3, &random);
        assert!(third > first);
    }

    #[test]
    fn backoff_is_never_less_than_half_its_capped_delay() {
        let random = FixedRandom(0);
        let delay = jittered_backoff(2, &random);
        let capped = BASE_BACKOFF.saturating_mul(1 << 2);
        assert!(delay >= capped.mul_f64(0.5));
    }

    #[test]
    fn a_broken_entropy_source_falls_back_to_the_midpoint_not_a_panic() {
        let delay = jittered_backoff(0, &FailingRandom);
        assert_eq!(delay, BASE_BACKOFF.mul_f64(0.75));
    }

    #[test]
    fn nothing_pending_or_no_share_plans_no_flush() {
        assert_eq!(plan_suspend_flush(Duration::from_secs(1), 0), None);
        assert_eq!(plan_suspend_flush(Duration::ZERO, 5), None);
    }

    #[test]
    fn pending_points_with_a_real_share_plan_a_flush() {
        assert_eq!(
            plan_suspend_flush(Duration::from_millis(500), 5),
            Some(BatchPlan { points: 5 })
        );
    }
}
