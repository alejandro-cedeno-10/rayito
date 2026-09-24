//! What `HealthService.Metrics` reports and how the procfs text is read. The
//! parsers are pure so they run against fixtures on any host; the probe port
//! is what the Linux adapter implements.

use std::time::{SystemTime, UNIX_EPOCH};

use thiserror::Error;

#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum MetricsError {
    #[error("metrics are not supported on this platform")]
    Unsupported,
    #[error("malformed {source_name}: {reason}")]
    Malformed {
        source_name: &'static str,
        reason: &'static str,
    },
    #[error("reading {source_name} failed: {reason}")]
    Io {
        source_name: &'static str,
        reason: String,
    },
}

/// Jiffies from the aggregate `cpu` line of `/proc/stat`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CpuTimes {
    pub busy: u64,
    pub idle: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MemoryInfo {
    pub total: u64,
    pub available: u64,
    /// `Cached:` (page cache); `0` when the kernel does not report it.
    pub cached: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DiskUsage {
    pub total: u64,
    pub used: u64,
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct MetricsSnapshot {
    pub cpu_used_pct: f64,
    pub mem_used: u64,
    pub mem_total: u64,
    pub mem_cache: u64,
    pub disk_used: u64,
    pub disk_total: u64,
    pub cpu_count: u32,
    pub wall: SystemTime,
}

pub trait MetricsProbe: Send + Sync {
    fn cpu_times(&self) -> Result<CpuTimes, MetricsError>;
    fn memory(&self) -> Result<MemoryInfo, MetricsError>;
    fn disk_root(&self) -> Result<DiskUsage, MetricsError>;
    fn cpu_count(&self) -> u32;
}

/// `busy = user + nice + system + irq + softirq + steal`, `idle = idle +
/// iowait`; guest columns are already counted inside `user`/`nice`.
pub fn parse_proc_stat(text: &str) -> Result<CpuTimes, MetricsError> {
    let line = text
        .lines()
        .find(|line| line.starts_with("cpu "))
        .ok_or(malformed("/proc/stat", "no aggregate cpu line"))?;
    let fields: Vec<u64> = line
        .split_whitespace()
        .skip(1)
        .map(str::parse)
        .collect::<Result<_, _>>()
        .map_err(|_| malformed("/proc/stat", "non-numeric cpu column"))?;
    if fields.len() < 8 {
        return Err(malformed("/proc/stat", "fewer than 8 cpu columns"));
    }
    let column = |index: usize| fields.get(index).copied().unwrap_or(0);
    Ok(CpuTimes {
        busy: column(0) + column(1) + column(2) + column(5) + column(6) + column(7),
        idle: column(3) + column(4),
    })
}

pub fn parse_meminfo(text: &str) -> Result<MemoryInfo, MetricsError> {
    let total = meminfo_field(text, "MemTotal:")?;
    let available = meminfo_field(text, "MemAvailable:")?;
    let cached = optional_meminfo_field(text, "Cached:")?.unwrap_or(0);
    Ok(MemoryInfo {
        total,
        available,
        cached,
    })
}

/// Milliseconds since the Unix epoch: `0` before the epoch, saturating at
/// `i64::MAX`.
#[must_use]
pub fn unix_millis(wall: SystemTime) -> i64 {
    wall.duration_since(UNIX_EPOCH).map_or(0, |since| {
        i64::try_from(since.as_millis()).unwrap_or(i64::MAX)
    })
}

/// Share of the sampling window spent busy, clamped to `0..=100`; `0` when
/// no jiffy elapsed between the samples.
#[must_use]
pub fn cpu_used_pct(prev: CpuTimes, next: CpuTimes) -> f64 {
    let busy = next.busy.saturating_sub(prev.busy);
    let idle = next.idle.saturating_sub(prev.idle);
    let total = busy + idle;
    if total == 0 {
        return 0.0;
    }
    let ratio = f64::from(u32::try_from(busy).unwrap_or(u32::MAX))
        / f64::from(u32::try_from(total).unwrap_or(u32::MAX));
    (ratio * 100.0).clamp(0.0, 100.0)
}

#[must_use]
pub fn snapshot(
    first: CpuTimes,
    second: CpuTimes,
    memory: MemoryInfo,
    disk: DiskUsage,
    cpu_count: u32,
    wall: SystemTime,
) -> MetricsSnapshot {
    MetricsSnapshot {
        cpu_used_pct: cpu_used_pct(first, second),
        mem_used: memory.total.saturating_sub(memory.available),
        mem_total: memory.total,
        mem_cache: memory.cached,
        disk_used: disk.used,
        disk_total: disk.total,
        cpu_count: cpu_count.max(1),
        wall,
    }
}

fn meminfo_field(text: &str, label: &'static str) -> Result<u64, MetricsError> {
    optional_meminfo_field(text, label)?.ok_or(malformed("/proc/meminfo", "missing field"))
}

/// `None` when the line is absent; the label match is a prefix, so
/// `Cached:` never picks up `SwapCached:`.
fn optional_meminfo_field(text: &str, label: &'static str) -> Result<Option<u64>, MetricsError> {
    let Some(line) = text.lines().find(|line| line.starts_with(label)) else {
        return Ok(None);
    };
    let kib: u64 = line
        .split_whitespace()
        .nth(1)
        .and_then(|value| value.parse().ok())
        .ok_or(malformed("/proc/meminfo", "non-numeric field"))?;
    Ok(Some(kib.saturating_mul(1024)))
}

fn malformed(source_name: &'static str, reason: &'static str) -> MetricsError {
    MetricsError::Malformed {
        source_name,
        reason,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    const PROC_STAT: &str = "cpu  4705 150 1120 16250 1 0 45 3 0 0\n\
cpu0 1200 40 300 4100 0 0 12 1 0 0\n\
intr 12345\n\
ctxt 6789\n";

    const MEMINFO: &str = "MemTotal:        2005248 kB\n\
MemFree:          812000 kB\n\
MemAvailable:    1500000 kB\n\
Buffers:           12000 kB\n";

    #[test]
    fn proc_stat_sums_busy_and_idle_columns() {
        let times = parse_proc_stat(PROC_STAT).unwrap();
        assert_eq!(times.busy, 4705 + 150 + 1120 + 45 + 3);
        assert_eq!(times.idle, 16250 + 1);
    }

    #[test]
    fn proc_stat_rejects_missing_or_short_cpu_line() {
        assert!(matches!(
            parse_proc_stat("intr 1\n"),
            Err(MetricsError::Malformed { .. })
        ));
        assert!(matches!(
            parse_proc_stat("cpu 1 2 3\n"),
            Err(MetricsError::Malformed { .. })
        ));
        assert!(matches!(
            parse_proc_stat("cpu 1 2 3 x 5 6 7 8\n"),
            Err(MetricsError::Malformed { .. })
        ));
    }

    #[test]
    fn meminfo_reads_total_and_available_in_bytes() {
        let memory = parse_meminfo(MEMINFO).unwrap();
        assert_eq!(memory.total, 2_005_248 * 1024);
        assert_eq!(memory.available, 1_500_000 * 1024);
        assert!(matches!(
            parse_meminfo("MemTotal: 1 kB\n"),
            Err(MetricsError::Malformed { .. })
        ));
    }

    #[test]
    fn meminfo_reads_cached_and_defaults_it_to_zero() {
        let with_cache = "MemTotal:  4000 kB\n\
MemAvailable:  3000 kB\n\
SwapCached:  7 kB\n\
Cached:  512 kB\n";
        assert_eq!(parse_meminfo(with_cache).unwrap().cached, 512 * 1024);
        let swap_cached_only = "MemTotal:  4000 kB\n\
MemAvailable:  3000 kB\n\
SwapCached:  7 kB\n";
        assert_eq!(parse_meminfo(swap_cached_only).unwrap().cached, 0);
        assert_eq!(parse_meminfo(MEMINFO).unwrap().cached, 0);
    }

    #[test]
    fn snapshot_carries_mem_cache() {
        let snapshot = snapshot(
            CpuTimes { busy: 0, idle: 0 },
            CpuTimes { busy: 1, idle: 1 },
            MemoryInfo {
                total: 1000,
                available: 400,
                cached: 300,
            },
            DiskUsage { total: 1, used: 0 },
            2,
            UNIX_EPOCH,
        );
        assert_eq!(snapshot.mem_cache, 300);
        assert_eq!(snapshot.mem_used, 600);
    }

    /// The saturation case only exists where `SystemTime` can hold more than
    /// `i64::MAX` milliseconds (Unix `timespec`); a Windows `FILETIME` tops
    /// out long before, so `checked_add` yields `None` there.
    #[test]
    fn unix_millis_saturates_and_clamps_pre_epoch() {
        assert_eq!(unix_millis(UNIX_EPOCH), 0);
        assert_eq!(
            unix_millis(UNIX_EPOCH + Duration::from_millis(1_790_000_000_123)),
            1_790_000_000_123
        );
        assert_eq!(unix_millis(UNIX_EPOCH - Duration::from_secs(1)), 0);
        let past_i64_millis = Duration::from_secs(9_223_372_036_854_776);
        if let Some(far_future) = UNIX_EPOCH.checked_add(past_i64_millis) {
            assert_eq!(unix_millis(far_future), i64::MAX);
        }
    }

    #[test]
    fn cpu_percentage_edge_cases() {
        let prev = CpuTimes {
            busy: 100,
            idle: 100,
        };
        assert!((cpu_used_pct(prev, prev) - 0.0).abs() < f64::EPSILON);
        let half = CpuTimes {
            busy: 150,
            idle: 150,
        };
        assert!((cpu_used_pct(prev, half) - 50.0).abs() < f64::EPSILON);
        let all_busy = CpuTimes {
            busy: 200,
            idle: 100,
        };
        assert!((cpu_used_pct(prev, all_busy) - 100.0).abs() < f64::EPSILON);
        let went_backwards = CpuTimes { busy: 50, idle: 50 };
        assert!((cpu_used_pct(prev, went_backwards) - 0.0).abs() < f64::EPSILON);
    }

    #[test]
    fn snapshot_derives_used_memory_and_clamps_cpu_count() {
        let snapshot = snapshot(
            CpuTimes { busy: 0, idle: 0 },
            CpuTimes { busy: 1, idle: 3 },
            MemoryInfo {
                total: 1000,
                available: 250,
                cached: 0,
            },
            DiskUsage {
                total: 8000,
                used: 100,
            },
            0,
            UNIX_EPOCH,
        );
        assert_eq!(snapshot.mem_used, 750);
        assert_eq!(snapshot.cpu_count, 1);
        assert_eq!(snapshot.disk_total, 8000);
        assert!((snapshot.cpu_used_pct - 25.0).abs() < f64::EPSILON);
    }
}
