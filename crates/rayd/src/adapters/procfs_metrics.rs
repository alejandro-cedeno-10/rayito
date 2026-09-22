//! `MetricsProbe` over procfs and `statvfs("/")` (design D12); anything that
//! is not Linux answers `Unsupported`, which the gRPC layer maps to
//! `UNAVAILABLE`.

#[cfg(unix)]
pub use unix::ProcfsMetricsProbe;
#[cfg(unix)]
pub type PlatformMetricsProbe = unix::ProcfsMetricsProbe;

#[cfg(not(unix))]
pub use unsupported::UnsupportedProbe;
#[cfg(not(unix))]
pub type PlatformMetricsProbe = unsupported::UnsupportedProbe;

/// `available_parallelism` never reports zero; a failure counts as one CPU.
fn detected_cpu_count() -> u32 {
    std::thread::available_parallelism()
        .map_or(1, |count| u32::try_from(count.get()).unwrap_or(u32::MAX))
}

#[cfg(unix)]
mod unix {
    use rayd_core::metrics::{
        CpuTimes, DiskUsage, MemoryInfo, MetricsError, MetricsProbe, parse_meminfo, parse_proc_stat,
    };

    const PROC_STAT: &str = "/proc/stat";
    const PROC_MEMINFO: &str = "/proc/meminfo";
    const ROOT: &str = "/";

    #[derive(Debug, Default, Clone, Copy)]
    pub struct ProcfsMetricsProbe;

    impl MetricsProbe for ProcfsMetricsProbe {
        fn cpu_times(&self) -> Result<CpuTimes, MetricsError> {
            parse_proc_stat(&read(PROC_STAT)?)
        }

        fn memory(&self) -> Result<MemoryInfo, MetricsError> {
            parse_meminfo(&read(PROC_MEMINFO)?)
        }

        fn disk_root(&self) -> Result<DiskUsage, MetricsError> {
            let stat = nix::sys::statvfs::statvfs(ROOT).map_err(|error| MetricsError::Io {
                source_name: "statvfs",
                reason: format!("{error:?}"),
            })?;
            let fragment = widen(stat.fragment_size());
            let blocks = widen(stat.blocks());
            let free = widen(stat.blocks_free());
            Ok(DiskUsage {
                total: blocks.saturating_mul(fragment),
                used: blocks.saturating_sub(free).saturating_mul(fragment),
            })
        }

        fn cpu_count(&self) -> u32 {
            super::detected_cpu_count()
        }
    }

    fn read(path: &'static str) -> Result<String, MetricsError> {
        std::fs::read_to_string(path).map_err(|error| MetricsError::Io {
            source_name: path,
            reason: error.kind().to_string(),
        })
    }

    /// libc's block-count and fragment-size types differ per target; going
    /// through `Into<u64>` keeps the arithmetic portable without casts.
    fn widen(value: impl Into<u64>) -> u64 {
        value.into()
    }
}

#[cfg(not(unix))]
mod unsupported {
    use rayd_core::metrics::{CpuTimes, DiskUsage, MemoryInfo, MetricsError, MetricsProbe};

    #[derive(Debug, Default, Clone, Copy)]
    pub struct UnsupportedProbe;

    impl MetricsProbe for UnsupportedProbe {
        fn cpu_times(&self) -> Result<CpuTimes, MetricsError> {
            Err(MetricsError::Unsupported)
        }

        fn memory(&self) -> Result<MemoryInfo, MetricsError> {
            Err(MetricsError::Unsupported)
        }

        fn disk_root(&self) -> Result<DiskUsage, MetricsError> {
            Err(MetricsError::Unsupported)
        }

        fn cpu_count(&self) -> u32 {
            super::detected_cpu_count()
        }
    }
}
