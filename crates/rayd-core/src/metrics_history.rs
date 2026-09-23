//! What `HealthService.MetricsHistory` serves: a fixed-capacity ring of
//! procfs samples taken every 5 s while the sandbox runs, the inclusive range
//! and downsampling rules applied to it, and the pure sampler state that
//! turns two CPU readings into one sample. The only synchronisation is a
//! `std::sync::Mutex`; the runtime task that feeds the ring lives in the
//! `rayd` adapter.

use std::collections::VecDeque;
use std::num::NonZeroUsize;
use std::sync::{Mutex, MutexGuard, PoisonError};
use std::time::{Duration, SystemTime};

use thiserror::Error;

use crate::metrics::{CpuTimes, DiskUsage, MemoryInfo, MetricsSnapshot, snapshot, unix_millis};

pub const HISTORY_SAMPLE_INTERVAL: Duration = Duration::from_secs(5);
/// 28 800 s (the non-adjustable `MicroVM` cap, `AWS_API_NOTES.md` §11) / 5 s.
pub const HISTORY_CAPACITY: usize = 5_760;

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct MetricsSample {
    pub unix_ms: i64,
    pub cpu_used_pct: f64,
    pub mem_used: u64,
    pub mem_total: u64,
    pub mem_cache: u64,
    pub disk_used: u64,
    pub disk_total: u64,
    pub cpu_count: u32,
}

impl MetricsSample {
    #[must_use]
    pub fn from_snapshot(snapshot: &MetricsSnapshot) -> Self {
        Self {
            unix_ms: unix_millis(snapshot.wall),
            cpu_used_pct: snapshot.cpu_used_pct,
            mem_used: snapshot.mem_used,
            mem_total: snapshot.mem_total,
            mem_cache: snapshot.mem_cache,
            disk_used: snapshot.disk_used,
            disk_total: snapshot.disk_total,
            cpu_count: snapshot.cpu_count,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PushOutcome {
    Appended,
    AppendedEvictingOldest,
    RejectedOutOfOrder,
}

/// Samples in strictly ascending `unix_ms`, oldest first.
#[derive(Debug)]
pub struct MetricsRing {
    samples: VecDeque<MetricsSample>,
    capacity: usize,
}

impl MetricsRing {
    #[must_use]
    pub fn with_capacity(capacity: usize) -> Self {
        let capacity = capacity.max(1);
        Self {
            samples: VecDeque::with_capacity(capacity),
            capacity,
        }
    }

    /// Appends only a sample strictly newer than the last one: the wall
    /// clock can step back when AWS corrects it at resume, and dropping a
    /// sample is honest where re-sorting the ring is not.
    pub fn push(&mut self, sample: MetricsSample) -> PushOutcome {
        if self
            .samples
            .back()
            .is_some_and(|last| sample.unix_ms <= last.unix_ms)
        {
            return PushOutcome::RejectedOutOfOrder;
        }
        let evicting = self.samples.len() >= self.capacity;
        if evicting {
            self.samples.pop_front();
        }
        self.samples.push_back(sample);
        if evicting {
            PushOutcome::AppendedEvictingOldest
        } else {
            PushOutcome::Appended
        }
    }

    /// Inclusive on both ends; `None` bounds are unbounded. The ring is
    /// sorted, so both bounds are binary searches.
    #[must_use]
    pub fn range(&self, query: &RangeQuery) -> Vec<MetricsSample> {
        let first = query.start_unix_ms.map_or(0, |start| {
            self.samples
                .partition_point(|sample| sample.unix_ms < start)
        });
        let past_last = query.end_unix_ms.map_or(self.samples.len(), |end| {
            self.samples.partition_point(|sample| sample.unix_ms <= end)
        });
        let selected: Vec<MetricsSample> = self
            .samples
            .range(first..past_last.max(first))
            .copied()
            .collect();
        match query.max_points {
            Some(max_points) => downsample(&selected, max_points.get()),
            None => selected,
        }
    }

    /// The oldest retained sample of the whole ring, not of any range.
    #[must_use]
    pub fn oldest_unix_ms(&self) -> Option<i64> {
        self.samples.front().map(|sample| sample.unix_ms)
    }

    #[must_use]
    pub fn len(&self) -> usize {
        self.samples.len()
    }

    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.samples.is_empty()
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RangeQuery {
    pub start_unix_ms: Option<i64>,
    pub end_unix_ms: Option<i64>,
    pub max_points: Option<NonZeroUsize>,
}

impl RangeQuery {
    /// Wire form: 0 means "unbounded" / "no reduction".
    pub fn from_wire(
        start_unix_ms: i64,
        end_unix_ms: i64,
        max_points: u32,
    ) -> Result<Self, MetricsHistoryError> {
        if start_unix_ms < 0 || end_unix_ms < 0 {
            return Err(MetricsHistoryError::NegativeBound);
        }
        if end_unix_ms != 0 && start_unix_ms > end_unix_ms {
            return Err(MetricsHistoryError::InvertedRange);
        }
        Ok(Self {
            start_unix_ms: (start_unix_ms != 0).then_some(start_unix_ms),
            end_unix_ms: (end_unix_ms != 0).then_some(end_unix_ms),
            max_points: usize::try_from(max_points).ok().and_then(NonZeroUsize::new),
        })
    }
}

#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum MetricsHistoryError {
    #[error("start_unix_ms and end_unix_ms must be >= 0")]
    NegativeBound,
    #[error("start_unix_ms is after end_unix_ms")]
    InvertedRange,
}

/// At most `max_points` points, unchanged when there are not more samples
/// than that. Bucket `i` covers indices `[i*n/m, (i+1)*n/m)`; its point is
/// the last sample of the bucket (a real timestamp and real gauges) with
/// `cpu_used_pct` replaced by the bucket mean, because the CPU of each
/// sample is already a rate over its own window. `max_points == 0` yields
/// no points.
#[must_use]
pub fn downsample(samples: &[MetricsSample], max_points: usize) -> Vec<MetricsSample> {
    if max_points == 0 {
        return Vec::new();
    }
    let len = samples.len();
    if len <= max_points {
        return samples.to_vec();
    }
    (0..max_points)
        .filter_map(|bucket| {
            let start = bucket * len / max_points;
            let end = (bucket + 1) * len / max_points;
            samples.get(start..end).and_then(bucket_point)
        })
        .collect()
}

fn bucket_point(bucket: &[MetricsSample]) -> Option<MetricsSample> {
    let last = bucket.last()?;
    let total: f64 = bucket.iter().map(|sample| sample.cpu_used_pct).sum();
    let count = f64::from(u32::try_from(bucket.len()).unwrap_or(u32::MAX));
    Some(MetricsSample {
        cpu_used_pct: total / count,
        ..*last
    })
}

#[derive(Debug, Clone, PartialEq)]
pub struct HistoryPage {
    pub samples: Vec<MetricsSample>,
    pub oldest_unix_ms: Option<i64>,
}

#[derive(Debug)]
pub struct MetricsHistory {
    ring: Mutex<MetricsRing>,
}

impl MetricsHistory {
    #[must_use]
    pub fn new(capacity: usize) -> Self {
        Self {
            ring: Mutex::new(MetricsRing::with_capacity(capacity)),
        }
    }

    pub fn record(&self, sample: MetricsSample) -> PushOutcome {
        self.ring().push(sample)
    }

    #[must_use]
    pub fn query(&self, query: &RangeQuery) -> HistoryPage {
        let ring = self.ring();
        HistoryPage {
            samples: ring.range(query),
            oldest_unix_ms: ring.oldest_unix_ms(),
        }
    }

    fn ring(&self) -> MutexGuard<'_, MetricsRing> {
        self.ring.lock().unwrap_or_else(PoisonError::into_inner)
    }
}

impl Default for MetricsHistory {
    fn default() -> Self {
        Self::new(HISTORY_CAPACITY)
    }
}

/// One reading of the probe and the session, taken by the sampler task.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct SamplerTick {
    pub resume_generation: u64,
    pub cpu: CpuTimes,
    pub memory: MemoryInfo,
    pub disk: DiskUsage,
    pub cpu_count: u32,
    pub wall: SystemTime,
}

#[derive(Debug, Default)]
pub struct MetricsSampler {
    baseline: Option<SamplerBaseline>,
}

#[derive(Debug, Clone, Copy)]
struct SamplerBaseline {
    cpu: CpuTimes,
    resume_generation: u64,
}

impl MetricsSampler {
    /// First tick after the gate opened (or after a resume) only arms the
    /// CPU baseline: a window that straddles a freeze would average real
    /// work with frozen jiffies.
    pub fn observe(&mut self, tick: &SamplerTick) -> Option<MetricsSample> {
        let previous = self.baseline.replace(SamplerBaseline {
            cpu: tick.cpu,
            resume_generation: tick.resume_generation,
        });
        let baseline =
            previous.filter(|baseline| baseline.resume_generation == tick.resume_generation)?;
        Some(MetricsSample::from_snapshot(&snapshot(
            baseline.cpu,
            tick.cpu,
            tick.memory,
            tick.disk,
            tick.cpu_count,
            tick.wall,
        )))
    }

    /// Drops the baseline, so the next observed tick only re-arms it.
    pub fn close_gate(&mut self) {
        self.baseline = None;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::UNIX_EPOCH;

    fn sample(unix_ms: i64, cpu_used_pct: f64) -> MetricsSample {
        MetricsSample {
            unix_ms,
            cpu_used_pct,
            mem_used: u64::try_from(unix_ms).unwrap(),
            mem_total: 4_000_000,
            mem_cache: 7,
            disk_used: 10,
            disk_total: 100,
            cpu_count: 2,
        }
    }

    fn ring_of(capacity: usize, stamps: impl IntoIterator<Item = i64>) -> MetricsRing {
        let mut ring = MetricsRing::with_capacity(capacity);
        for stamp in stamps {
            assert_ne!(
                ring.push(sample(stamp, 0.0)),
                PushOutcome::RejectedOutOfOrder
            );
        }
        ring
    }

    fn stamps(samples: &[MetricsSample]) -> Vec<i64> {
        samples.iter().map(|sample| sample.unix_ms).collect()
    }

    fn everything() -> RangeQuery {
        RangeQuery {
            start_unix_ms: None,
            end_unix_ms: None,
            max_points: None,
        }
    }

    fn assert_close(actual: &[f64], expected: &[f64]) {
        assert_eq!(actual.len(), expected.len(), "{actual:?} vs {expected:?}");
        for (left, right) in actual.iter().zip(expected) {
            assert!((left - right).abs() < 1e-9, "{actual:?} vs {expected:?}");
        }
    }

    fn tick(second: u64, generation: u64, busy: u64, idle: u64) -> SamplerTick {
        SamplerTick {
            resume_generation: generation,
            cpu: CpuTimes { busy, idle },
            memory: MemoryInfo {
                total: 4_000,
                available: 1_000,
                cached: 500,
            },
            disk: DiskUsage {
                total: 100,
                used: 10,
            },
            cpu_count: 2,
            wall: UNIX_EPOCH + Duration::from_secs(second),
        }
    }

    #[test]
    fn ring_appends_in_order_and_evicts_the_oldest_at_capacity() {
        let mut ring = MetricsRing::with_capacity(3);
        assert!(ring.is_empty());
        assert_eq!(ring.push(sample(1_000, 0.0)), PushOutcome::Appended);
        assert_eq!(ring.push(sample(2_000, 0.0)), PushOutcome::Appended);
        assert_eq!(ring.push(sample(3_000, 0.0)), PushOutcome::Appended);
        assert_eq!(
            ring.push(sample(4_000, 0.0)),
            PushOutcome::AppendedEvictingOldest
        );
        assert_eq!(ring.len(), 3);
        assert_eq!(stamps(&ring.range(&everything())), [2_000, 3_000, 4_000]);
        assert_eq!(ring.oldest_unix_ms(), Some(2_000));
        let mut minimal = MetricsRing::with_capacity(0);
        assert_eq!(minimal.push(sample(1, 0.0)), PushOutcome::Appended);
        assert_eq!(
            minimal.push(sample(2, 0.0)),
            PushOutcome::AppendedEvictingOldest
        );
        assert_eq!(minimal.len(), 1);
    }

    #[test]
    fn ring_rejects_out_of_order_samples() {
        let mut ring = ring_of(8, [1_000, 2_000]);
        assert_eq!(
            ring.push(sample(2_000, 0.0)),
            PushOutcome::RejectedOutOfOrder
        );
        assert_eq!(
            ring.push(sample(1_500, 0.0)),
            PushOutcome::RejectedOutOfOrder
        );
        assert_eq!(stamps(&ring.range(&everything())), [1_000, 2_000]);
        assert_eq!(ring.push(sample(2_001, 0.0)), PushOutcome::Appended);
    }

    #[test]
    fn range_is_inclusive_and_zero_bounds_are_unbounded() {
        let ring = ring_of(16, (1..=10).map(|second| second * 1_000));
        let window = RangeQuery::from_wire(3_000, 7_000, 0).unwrap();
        assert_eq!(
            stamps(&ring.range(&window)),
            [3_000, 4_000, 5_000, 6_000, 7_000]
        );
        let between = RangeQuery::from_wire(3_500, 6_500, 0).unwrap();
        assert_eq!(stamps(&ring.range(&between)), [4_000, 5_000, 6_000]);
        let open_end = RangeQuery::from_wire(9_000, 0, 0).unwrap();
        assert_eq!(stamps(&ring.range(&open_end)), [9_000, 10_000]);
        let open_start = RangeQuery::from_wire(0, 2_000, 0).unwrap();
        assert_eq!(stamps(&ring.range(&open_start)), [1_000, 2_000]);
        let unbounded = RangeQuery::from_wire(0, 0, 0).unwrap();
        assert_eq!(ring.range(&unbounded).len(), 10);
        let after_the_last = RangeQuery::from_wire(20_000, 0, 0).unwrap();
        assert!(ring.range(&after_the_last).is_empty());
        let limit_above_the_selection = RangeQuery::from_wire(0, 0, 50).unwrap();
        assert_eq!(ring.range(&limit_above_the_selection).len(), 10);
    }

    #[test]
    fn range_query_rejects_negative_and_inverted_bounds() {
        assert_eq!(
            RangeQuery::from_wire(-1, 0, 0),
            Err(MetricsHistoryError::NegativeBound)
        );
        assert_eq!(
            RangeQuery::from_wire(0, -5, 0),
            Err(MetricsHistoryError::NegativeBound)
        );
        assert_eq!(
            RangeQuery::from_wire(5_000, 1_000, 0),
            Err(MetricsHistoryError::InvertedRange)
        );
        assert_eq!(
            RangeQuery::from_wire(5_000, 5_000, 3),
            Ok(RangeQuery {
                start_unix_ms: Some(5_000),
                end_unix_ms: Some(5_000),
                max_points: NonZeroUsize::new(3),
            })
        );
        assert_eq!(
            RangeQuery::from_wire(5_000, 0, 0),
            Ok(RangeQuery {
                start_unix_ms: Some(5_000),
                end_unix_ms: None,
                max_points: None,
            })
        );
    }

    #[test]
    fn downsample_keeps_the_last_sample_of_each_bucket_and_averages_cpu() {
        let cpu = [0.0, 10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0];
        let samples: Vec<MetricsSample> = cpu
            .iter()
            .zip(1_i64..)
            .map(|(pct, second)| sample(second * 1_000, *pct))
            .collect();
        let reduced = downsample(&samples, 3);
        assert_eq!(stamps(&reduced), [3_000, 6_000, 10_000]);
        let means: Vec<f64> = reduced.iter().map(|point| point.cpu_used_pct).collect();
        assert_close(&means, &[10.0, 40.0, 75.0]);
        let gauges: Vec<u64> = reduced.iter().map(|point| point.mem_used).collect();
        assert_eq!(gauges, [3_000, 6_000, 10_000]);
        let mut ring = MetricsRing::with_capacity(16);
        for point in &samples {
            ring.push(*point);
        }
        let through_the_ring = ring.range(&RangeQuery::from_wire(0, 0, 3).unwrap());
        assert_eq!(through_the_ring, reduced);
    }

    #[test]
    fn downsample_is_identity_under_the_limit() {
        let samples: Vec<MetricsSample> =
            (1..=4).map(|second| sample(second * 1_000, 5.0)).collect();
        assert_eq!(downsample(&samples, 4), samples);
        assert_eq!(downsample(&samples, 10), samples);
        assert!(downsample(&samples, 0).is_empty());
        assert!(downsample(&[], 3).is_empty());
    }

    #[test]
    fn capacity_covers_eight_hours_within_the_memory_budget() {
        let covered = HISTORY_SAMPLE_INTERVAL * u32::try_from(HISTORY_CAPACITY).unwrap();
        assert_eq!(covered, Duration::from_secs(28_800));
        assert!(std::mem::size_of::<MetricsSample>() <= 64);
        let full = ring_of(
            HISTORY_CAPACITY,
            (1..=i64::try_from(HISTORY_CAPACITY).unwrap() + 1).map(|stamp| stamp * 5_000),
        );
        assert_eq!(full.len(), HISTORY_CAPACITY);
        assert_eq!(full.oldest_unix_ms(), Some(10_000));
    }

    #[test]
    fn sampler_arms_first_then_emits_the_window_average() {
        let mut sampler = MetricsSampler::default();
        assert_eq!(sampler.observe(&tick(5, 0, 100, 100)), None);
        let first = sampler.observe(&tick(10, 0, 110, 130)).unwrap();
        assert_eq!(first.unix_ms, 10_000);
        assert_close(&[first.cpu_used_pct], &[25.0]);
        assert_eq!(first.mem_used, 3_000);
        assert_eq!(first.mem_total, 4_000);
        assert_eq!(first.mem_cache, 500);
        assert_eq!((first.disk_used, first.disk_total), (10, 100));
        assert_eq!(first.cpu_count, 2);
        let second = sampler.observe(&tick(15, 0, 150, 130)).unwrap();
        assert_eq!(second.unix_ms, 15_000);
        assert_close(&[second.cpu_used_pct], &[100.0]);
    }

    #[test]
    fn sampler_rearms_after_a_closed_gate_across_a_suspend_jump() {
        let mut sampler = MetricsSampler::default();
        assert_eq!(sampler.observe(&tick(5, 0, 100, 100)), None);
        assert!(sampler.observe(&tick(10, 0, 110, 130)).is_some());
        assert!(sampler.observe(&tick(15, 0, 120, 160)).is_some());
        sampler.close_gate();
        assert_eq!(sampler.observe(&tick(320, 1, 5_000, 170)), None);
        let resumed = sampler.observe(&tick(325, 1, 5_010, 200)).unwrap();
        assert_eq!(resumed.unix_ms, 325_000);
        assert_close(&[resumed.cpu_used_pct], &[25.0]);
    }

    #[test]
    fn sampler_rearms_when_the_generation_changes_without_a_closed_gate() {
        let mut sampler = MetricsSampler::default();
        assert_eq!(sampler.observe(&tick(5, 0, 100, 100)), None);
        assert_eq!(sampler.observe(&tick(320, 1, 5_000, 170)), None);
        let resumed = sampler.observe(&tick(325, 1, 5_010, 200)).unwrap();
        assert_close(&[resumed.cpu_used_pct], &[25.0]);
    }

    #[test]
    fn history_query_reports_the_oldest_retained_sample() {
        let history = MetricsHistory::new(3);
        assert_eq!(
            history.query(&everything()),
            HistoryPage {
                samples: Vec::new(),
                oldest_unix_ms: None,
            }
        );
        for stamp in [1_000, 2_000, 3_000, 4_000] {
            history.record(sample(stamp, 0.0));
        }
        let page = history.query(&RangeQuery::from_wire(4_000, 0, 0).unwrap());
        assert_eq!(stamps(&page.samples), [4_000]);
        assert_eq!(page.oldest_unix_ms, Some(2_000));
        assert_eq!(
            history.record(sample(1_500, 0.0)),
            PushOutcome::RejectedOutOfOrder
        );
        assert_eq!(
            MetricsHistory::default()
                .query(&everything())
                .oldest_unix_ms,
            None
        );
    }

    #[test]
    fn history_keeps_serving_after_a_poisoned_lock() {
        let history = MetricsHistory::new(4);
        history.record(sample(1_000, 0.0));
        std::thread::scope(|scope| {
            let poisoner = scope.spawn(|| {
                let _guard = history.ring.lock();
                panic!("poison the metrics lock");
            });
            assert!(poisoner.join().is_err());
        });
        assert!(history.ring.is_poisoned());
        assert_eq!(history.record(sample(2_000, 0.0)), PushOutcome::Appended);
        assert_eq!(
            stamps(&history.query(&everything()).samples),
            [1_000, 2_000]
        );
    }
}
