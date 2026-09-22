//! Per-process resource limits applied in the child before the privilege
//! drop, and how the child's stdin is wired.

/// Seconds between `SIGXCPU` at the soft `RLIMIT_CPU` and `SIGKILL` at the
/// hard one, so a process that ignores the first signal still dies.
pub const CPU_LIMIT_KILL_GRACE_SECONDS: u64 = 5;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ResourceLimits {
    pub nproc: u64,
    pub nofile: u64,
    pub core: u64,
    /// `RLIMIT_CPU` soft limit in CPU seconds (wall clock is irrelevant: a
    /// sleeping process never reaches it); `None` = unlimited.
    pub cpu_seconds: Option<u64>,
}

impl Default for ResourceLimits {
    fn default() -> Self {
        Self {
            nproc: 512,
            nofile: 4096,
            core: 0,
            cpu_seconds: None,
        }
    }
}

impl ResourceLimits {
    /// `(soft, hard)` for `RLIMIT_CPU`, or `None` when unlimited.
    #[must_use]
    pub fn cpu_rlimit(&self) -> Option<(u64, u64)> {
        self.cpu_seconds.map(|seconds| {
            (
                seconds,
                seconds.saturating_add(CPU_LIMIT_KILL_GRACE_SECONDS),
            )
        })
    }
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum StdinMode {
    /// `/dev/null`: the default, so a child never blocks waiting for input.
    #[default]
    Null,
    /// A pipe kept open until `CloseStdin`.
    Pipe,
}

impl StdinMode {
    #[must_use]
    pub fn from_flag(stdin: bool) -> Self {
        if stdin { Self::Pipe } else { Self::Null }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_match_the_security_posture() {
        let limits = ResourceLimits::default();
        assert_eq!((limits.nproc, limits.nofile, limits.core), (512, 4096, 0));
        assert_eq!(limits.cpu_seconds, None);
        assert_eq!(limits.cpu_rlimit(), None);
    }

    #[test]
    fn cpu_rlimit_derives_the_hard_limit_from_the_grace() {
        let limits = ResourceLimits {
            cpu_seconds: Some(2),
            ..ResourceLimits::default()
        };
        assert_eq!(limits.cpu_rlimit(), Some((2, 7)));
        let huge = ResourceLimits {
            cpu_seconds: Some(u64::MAX),
            ..ResourceLimits::default()
        };
        assert_eq!(huge.cpu_rlimit(), Some((u64::MAX, u64::MAX)));
    }

    #[test]
    fn stdin_flag_maps_to_the_mode() {
        assert_eq!(StdinMode::from_flag(false), StdinMode::Null);
        assert_eq!(StdinMode::from_flag(true), StdinMode::Pipe);
    }
}
