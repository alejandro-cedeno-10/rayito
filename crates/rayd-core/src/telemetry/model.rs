//! The closed vocabulary of what rayd-otlp can ever export (M15,
//! architecture §7.5): exactly 7 gauges, reusing the same samples the 5 s
//! `MetricsHistory` sampler (M9) already computes, and exactly 4 resource
//! attributes. Both are closed enums rather than open strings/maps on
//! purpose: a command, a path, an env value or `metadata` can never reach
//! an exported attribute by construction, because there is no field here
//! that could carry one.

use std::time::{Duration, SystemTime};

use crate::metrics_history::MetricsSample;

/// Metric-name family a `Metric.name` is built from (`GaugeKind::metric_name`):
/// `rayito.sandbox.*` by default, or `e2b.sandbox.*` for the E2B shim's
/// `names="e2b"` alias (research §6.4). The suffix after the family prefix
/// is identical either way; only the top-level name changes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NameStyle {
    Rayito,
    E2b,
}

/// The 7 gauges `rayd-otlp` exports, one `MetricPoint` each per sampling
/// tick (research §7.5; the 5 s `MetricsHistory` sample, not a separate
/// probe). Adding an 8th gauge is a deliberate, reviewed change to this
/// enum, never a free-form key.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum GaugeKind {
    CpuUsedPct,
    MemoryUsedBytes,
    MemoryTotalBytes,
    MemoryCacheBytes,
    DiskUsedBytes,
    DiskTotalBytes,
    CpuCount,
}

/// All 7 kinds, in the fixed order a batch is built in
/// (`batcher::Batcher::sample`); never reordered, so a diff of two batches'
/// gauge lists is stable.
pub const ALL_GAUGES: [GaugeKind; 7] = [
    GaugeKind::CpuUsedPct,
    GaugeKind::MemoryUsedBytes,
    GaugeKind::MemoryTotalBytes,
    GaugeKind::MemoryCacheBytes,
    GaugeKind::DiskUsedBytes,
    GaugeKind::DiskTotalBytes,
    GaugeKind::CpuCount,
];

impl GaugeKind {
    /// `Metric.name`: `<family>.sandbox.<suffix>`. The suffix is the same
    /// in both families; only the family prefix changes (research §6.4:
    /// "nombres `rayito.sandbox.*` con el mismo shape que `e2b.sandbox.*`").
    #[must_use]
    pub fn metric_name(self, names: NameStyle) -> String {
        let family = match names {
            NameStyle::Rayito => "rayito",
            NameStyle::E2b => "e2b",
        };
        format!("{family}.sandbox.{}", self.suffix())
    }

    const fn suffix(self) -> &'static str {
        match self {
            Self::CpuUsedPct => "cpu.used_pct",
            Self::MemoryUsedBytes => "memory.used_bytes",
            Self::MemoryTotalBytes => "memory.total_bytes",
            Self::MemoryCacheBytes => "memory.cache_bytes",
            Self::DiskUsedBytes => "disk.used_bytes",
            Self::DiskTotalBytes => "disk.total_bytes",
            Self::CpuCount => "cpu.count",
        }
    }
}

/// The 4 closed resource attributes every exported batch carries (research
/// §7.5): the sandbox id `rayd` already knows from its own session, plus
/// the 3 image facts only the SDK knows (`rayd` has no local notion of its
/// own image ARN or version, Q68: the guest also sees 4x the image's
/// declared memory, hence `image_memory_mib` is reported explicitly rather
/// than read from `/proc/meminfo`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResourceAttrs {
    pub sandbox_id: String,
    pub image_arn: String,
    pub image_version: String,
    pub image_memory_mib: u32,
}

/// One closed attribute key; `AttrKey::name` is what the OTLP `KeyValue.key`
/// carries, never a value that could leak a path, a host or a credential.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AttrKey {
    SandboxId,
    ImageArn,
    ImageVersion,
    ImageMemoryMib,
}

impl AttrKey {
    #[must_use]
    pub const fn name(self) -> &'static str {
        match self {
            Self::SandboxId => "sandbox_id",
            Self::ImageArn => "image_arn",
            Self::ImageVersion => "image_version",
            Self::ImageMemoryMib => "image_memory_mib",
        }
    }
}

/// One sample of one gauge, timestamped when the sampler read it (not when
/// the batch is sent: `time_unix_nano` on the wire reflects the real
/// sampling instant even if the batch sits in the queue for a while).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct MetricPoint {
    pub kind: GaugeKind,
    pub value: f64,
    pub timestamp: SystemTime,
}

/// The 7 gauges of one `MetricsSample` (the same sample `MetricsHistory`
/// already holds for `HealthService.MetricsHistory`), as the `MetricPoint`s
/// one export tick enqueues. `unix_ms` is milliseconds; `MetricPoint`
/// wants a `SystemTime`, so a negative or absurdly large timestamp
/// (a misbehaving clock, never expected in practice) saturates to the
/// epoch rather than panicking.
// OTLP's `NumberDataPoint` only has a `double` value (never a `u64`), and a
// byte count has to cross that bridge somewhere; `f64`'s 52-bit mantissa
// represents every integer up to 2^53 (≈ 8 PiB) exactly, far past any
// sandbox's memory or disk, so the precision clippy warns about here never
// actually happens.
#[allow(clippy::cast_precision_loss)]
#[must_use]
pub fn points_from_sample(sample: &MetricsSample) -> [MetricPoint; 7] {
    let timestamp =
        SystemTime::UNIX_EPOCH + Duration::from_millis(u64::try_from(sample.unix_ms).unwrap_or(0));
    let point = |kind: GaugeKind, value: f64| MetricPoint {
        kind,
        value,
        timestamp,
    };
    [
        point(GaugeKind::CpuUsedPct, sample.cpu_used_pct),
        point(GaugeKind::MemoryUsedBytes, sample.mem_used as f64),
        point(GaugeKind::MemoryTotalBytes, sample.mem_total as f64),
        point(GaugeKind::MemoryCacheBytes, sample.mem_cache as f64),
        point(GaugeKind::DiskUsedBytes, sample.disk_used as f64),
        point(GaugeKind::DiskTotalBytes, sample.disk_total as f64),
        point(GaugeKind::CpuCount, f64::from(sample.cpu_count)),
    ]
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_gauge_has_a_distinct_rayito_name() {
        let names: Vec<String> = ALL_GAUGES
            .iter()
            .map(|kind| kind.metric_name(NameStyle::Rayito))
            .collect();
        let mut sorted = names.clone();
        sorted.sort();
        sorted.dedup();
        assert_eq!(sorted.len(), names.len());
        assert!(names.iter().all(|name| name.starts_with("rayito.sandbox.")));
    }

    #[test]
    fn the_e2b_alias_only_swaps_the_family_prefix() {
        for kind in ALL_GAUGES {
            let rayito = kind.metric_name(NameStyle::Rayito);
            let e2b = kind.metric_name(NameStyle::E2b);
            assert_eq!(
                rayito.strip_prefix("rayito.sandbox."),
                e2b.strip_prefix("e2b.sandbox.")
            );
        }
    }

    // The gauge values here are exact `f64` literals carried through
    // unchanged, never computed or rounded, so an exact comparison is
    // correct rather than the usual float rounding risk.
    #[allow(clippy::float_cmp)]
    #[test]
    fn points_from_sample_covers_all_7_gauges_with_the_samples_timestamp() {
        let sample = MetricsSample {
            unix_ms: 1_000,
            cpu_used_pct: 12.5,
            mem_used: 1_000_000,
            mem_total: 2_000_000,
            mem_cache: 500_000,
            disk_used: 10_000_000,
            disk_total: 20_000_000,
            cpu_count: 4,
        };
        let points = points_from_sample(&sample);
        assert_eq!(points.len(), 7);
        for point in &points {
            assert_eq!(
                point.timestamp,
                SystemTime::UNIX_EPOCH + Duration::from_millis(1_000)
            );
        }
        assert_eq!(points[0].kind, GaugeKind::CpuUsedPct);
        assert_eq!(points[0].value, 12.5);
        assert_eq!(points[6].kind, GaugeKind::CpuCount);
        assert_eq!(points[6].value, 4.0);
    }

    #[test]
    fn attr_keys_never_carry_a_value() {
        for key in [
            AttrKey::SandboxId,
            AttrKey::ImageArn,
            AttrKey::ImageVersion,
            AttrKey::ImageMemoryMib,
        ] {
            assert!(!key.name().is_empty());
        }
    }
}
