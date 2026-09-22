//! What this boot of `rayd` may do, read once at startup: the `CapEff`
//! mask of `/proc/self/status` (parsed by `rayd_core::capabilities`) and
//! whether the cgroup2 root is mounted and readable. Logged as one
//! `capabilities` line so every published image records the platform
//! facts the hardening depends on. Off Linux everything is `false`.

use rayd_core::capabilities::{CapSet, Capability};

pub const PROC_SELF_STATUS: &str = "/proc/self/status";
pub const CGROUP2_CONTROLLERS: &str = "/sys/fs/cgroup/cgroup.controllers";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct GuestCapabilities {
    pub caps: CapSet,
    pub cgroup2_root: bool,
}

impl GuestCapabilities {
    #[must_use]
    pub fn net_admin(&self) -> bool {
        self.caps.has(Capability::NetAdmin)
    }

    #[must_use]
    pub fn sys_admin(&self) -> bool {
        self.caps.has(Capability::SysAdmin)
    }

    #[must_use]
    pub fn sys_resource(&self) -> bool {
        self.caps.has(Capability::SysResource)
    }

    #[must_use]
    pub fn sys_ptrace(&self) -> bool {
        self.caps.has(Capability::SysPtrace)
    }
}

/// Never fails: a guest whose status file cannot be read is reported as
/// having no capability at all, which is the fail-open side.
#[must_use]
pub fn detect_guest_capabilities() -> GuestCapabilities {
    GuestCapabilities {
        caps: read_cap_eff().unwrap_or_default(),
        cgroup2_root: cgroup2_root_readable(),
    }
}

#[cfg(unix)]
fn read_cap_eff() -> Option<CapSet> {
    let status = std::fs::read_to_string(PROC_SELF_STATUS).ok()?;
    rayd_core::capabilities::parse_cap_eff(&status).ok()
}

#[cfg(not(unix))]
fn read_cap_eff() -> Option<CapSet> {
    None
}

#[cfg(unix)]
fn cgroup2_root_readable() -> bool {
    std::fs::read_to_string(CGROUP2_CONTROLLERS).is_ok()
}

#[cfg(not(unix))]
fn cgroup2_root_readable() -> bool {
    false
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn detection_never_panics_and_flags_follow_the_mask() {
        let detected = detect_guest_capabilities();
        assert_eq!(
            detected.net_admin(),
            detected.caps.has(Capability::NetAdmin)
        );
        let none = GuestCapabilities::default();
        assert!(!none.net_admin());
        assert!(!none.sys_admin());
        assert!(!none.sys_resource());
        assert!(!none.sys_ptrace());
        assert!(!none.cgroup2_root);
        let all = GuestCapabilities {
            caps: CapSet::from_mask(u64::MAX),
            cgroup2_root: true,
        };
        assert!(all.net_admin() && all.sys_admin() && all.sys_resource() && all.sys_ptrace());
    }
}
