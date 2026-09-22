//! What the guest lets `rayd` do: the `CapEff` mask of `/proc/self/status`
//! parsed into the four capabilities the hardening cares about. On the
//! default image root has none of them (measured: `CapEff` without
//! `sys_admin`, `net_admin`, `sys_ptrace`, `sys_resource`); the image
//! variant published with `additionalOsCapabilities: ["ALL"]` is what
//! makes the IMDS block possible. Pure parsing: reading the file is the
//! adapter's job.

use thiserror::Error;

/// Linux capability bit numbers (`include/uapi/linux/capability.h`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Capability {
    NetAdmin = 12,
    SysPtrace = 19,
    SysAdmin = 21,
    SysResource = 24,
}

impl Capability {
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::NetAdmin => "net_admin",
            Self::SysPtrace => "sys_ptrace",
            Self::SysAdmin => "sys_admin",
            Self::SysResource => "sys_resource",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct CapSet {
    mask: u64,
}

impl CapSet {
    #[must_use]
    pub fn from_mask(mask: u64) -> Self {
        Self { mask }
    }

    #[must_use]
    pub fn has(self, capability: Capability) -> bool {
        self.mask & (1u64 << (capability as u32)) != 0
    }

    #[must_use]
    pub fn mask(self) -> u64 {
        self.mask
    }
}

#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum CapabilityError {
    #[error("no CapEff line in the status text")]
    Missing,
    #[error("CapEff is not a hexadecimal mask")]
    Malformed,
}

/// The `CapEff:` line of a `/proc/<pid>/status` text.
pub fn parse_cap_eff(status: &str) -> Result<CapSet, CapabilityError> {
    let value = status
        .lines()
        .find_map(|line| line.strip_prefix("CapEff:"))
        .ok_or(CapabilityError::Missing)?;
    u64::from_str_radix(value.trim(), 16)
        .map(CapSet::from_mask)
        .map_err(|_| CapabilityError::Malformed)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The mask measured on the default image (M0 Q20): `chown`,
    /// `dac_override`, `fowner`, `fsetid`, `kill`, `setgid`, `setuid`,
    /// `setpcap`, `net_bind_service`, `net_raw`, `sys_chroot`, `mknod`,
    /// `audit_write`, `setfcap`.
    const DEFAULT_IMAGE_MASK: &str = "CapEff:\t00000000a80425fb\n";

    #[test]
    fn the_default_image_mask_has_none_of_the_hardening_capabilities() {
        let status = format!("Name:\trayd\nUmask:\t0022\n{DEFAULT_IMAGE_MASK}Seccomp:\t0\n");
        let caps = parse_cap_eff(&status).unwrap();
        assert!(!caps.has(Capability::NetAdmin));
        assert!(!caps.has(Capability::SysAdmin));
        assert!(!caps.has(Capability::SysPtrace));
        assert!(!caps.has(Capability::SysResource));
        assert_eq!(caps.mask(), 0x0000_0000_a804_25fb);
    }

    #[test]
    fn bit_twelve_is_net_admin() {
        let caps = parse_cap_eff("CapEff:\t0000000000001000\n").unwrap();
        assert!(caps.has(Capability::NetAdmin));
        assert!(!caps.has(Capability::SysAdmin));
        let all = parse_cap_eff("CapEff:\t000001ffffffffff\n").unwrap();
        assert!(all.has(Capability::NetAdmin));
        assert!(all.has(Capability::SysAdmin));
        assert!(all.has(Capability::SysPtrace));
        assert!(all.has(Capability::SysResource));
        assert_eq!(Capability::NetAdmin.as_str(), "net_admin");
    }

    #[test]
    fn missing_or_malformed_lines_are_errors() {
        assert_eq!(
            parse_cap_eff("Name:\trayd\n"),
            Err(CapabilityError::Missing)
        );
        assert_eq!(
            parse_cap_eff("CapEff:\tzz\n"),
            Err(CapabilityError::Malformed)
        );
        assert_eq!(CapSet::default().mask(), 0);
    }
}
