//! What this boot of `rayd` may do, read once at startup: the `CapEff`
//! mask of `/proc/self/status` (parsed by `rayd_core::capabilities`) and
//! whether the cgroup2 root is mounted and readable. Logged as one
//! `capabilities` line so every published image records the platform
//! facts the hardening depends on. Off Linux everything is `false`.

use rayd_core::capabilities::{CapSet, Capability};

pub const PROC_SELF_STATUS: &str = "/proc/self/status";
pub const CGROUP2_CONTROLLERS: &str = "/sys/fs/cgroup/cgroup.controllers";
/// The filesystem types this kernel can mount (`proc(5)`).
pub const PROC_FILESYSTEMS: &str = "/proc/filesystems";

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

/// Whether an executable file called `name` exists in a directory of
/// `PATH`: an image-level fact (a binary the image ships) read once at
/// startup by the mount features' support checks.
#[must_use]
pub fn binary_on_path(name: &str) -> bool {
    std::env::var_os("PATH")
        .is_some_and(|path| std::env::split_paths(&path).any(|dir| dir.join(name).is_file()))
}

/// Whether this kernel lists `fs_type` in `/proc/filesystems` (`nfs4` for
/// EFS: compiled into the guest kernel, `AWS_API_NOTES.md` §16 Q79).
#[must_use]
pub fn kernel_supports_filesystem(fs_type: &str) -> bool {
    std::fs::read_to_string(PROC_FILESYSTEMS)
        .is_ok_and(|listing| filesystems_listing_has(&listing, fs_type))
}

/// `/proc/filesystems` lines are `[nodev]\t<type>`: the type is the last
/// whitespace-separated field.
fn filesystems_listing_has(listing: &str, fs_type: &str) -> bool {
    listing
        .lines()
        .any(|line| line.split_whitespace().last() == Some(fs_type))
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
    fn binary_on_path_finds_a_binary_known_to_exist_in_tests() {
        // `sh` exists on every Unix CI runner and in the Lima VM alike.
        assert!(binary_on_path("sh"));
        assert!(!binary_on_path("not-a-real-rayito-binary"));
    }

    #[test]
    fn the_filesystems_listing_matches_the_type_column_exactly() {
        let listing = "nodev\tsysfs\n\text4\nnodev\tnfs4\nnodev\tnfs\n";
        assert!(filesystems_listing_has(listing, "nfs4"));
        assert!(filesystems_listing_has(listing, "ext4"));
        assert!(!filesystems_listing_has(listing, "nfs41"));
        assert!(!filesystems_listing_has(listing, "efs"));
    }

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
