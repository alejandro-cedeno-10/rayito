//! A validated request to mount one EFS access point inside the guest
//! (research doc §4.2, `VolumeSpec`). Validation never touches the network
//! or the filesystem: it is string- and pattern-level only, the same split
//! the SDK's `_mount_path.py`/`mount-path.ts` already use for `mounts=`.
//! The mount-path shape itself lives in `rayd_core::mount_path`, shared
//! with `s3_mount` (m15-s3-mounts), so the two sections can never drift
//! apart on what a valid path is or how many a sandbox may have.

use super::error::VolumeError;
use crate::mount_path;

/// Re-exported so existing call sites and doc comments in this module
/// keep their name; the canonical definitions live in
/// `rayd_core::mount_path`, shared with `s3_mount`.
pub use crate::mount_path::ALLOWED_ROOTS as ALLOWED_MOUNT_ROOTS;
/// A sandbox has at most this many volumes in one `EfsVolumesConfig`
/// (research doc §5 "Volúmenes por sandbox") — the same joint cap
/// `mount_path::MAX_MOUNTS` enforces across `mounts=`/`volumes=` combined,
/// never a second independent number that could silently drift from it.
pub use crate::mount_path::MAX_MOUNTS as MAX_VOLUMES_PER_SANDBOX;

/// Shortest and longest hex suffix AWS issues for an EFS file-system or
/// access-point id (research doc R3: `fs-[0-9a-f]{8,40}`,
/// `fsap-[0-9a-f]{8,40}`).
const ID_SUFFIX_MIN: usize = 8;
const ID_SUFFIX_MAX: usize = 40;

fn validate_hex_id<'a>(value: &'a str, prefix: &str) -> Result<&'a str, VolumeError> {
    let suffix = value
        .strip_prefix(prefix)
        .ok_or(VolumeError::InvalidIdentifier)?;
    let len = suffix.len();
    if !(ID_SUFFIX_MIN..=ID_SUFFIX_MAX).contains(&len) {
        return Err(VolumeError::InvalidIdentifier);
    }
    if !suffix
        .bytes()
        .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
    {
        return Err(VolumeError::InvalidIdentifier);
    }
    Ok(value)
}

/// A validated `fs-<hex>` identifier.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct FileSystemId(String);

impl FileSystemId {
    pub fn parse(value: &str) -> Result<Self, VolumeError> {
        validate_hex_id(value, "fs-").map(|v| Self(v.to_owned()))
    }

    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

/// A validated `fsap-<hex>` identifier.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct AccessPointId(String);

impl AccessPointId {
    pub fn parse(value: &str) -> Result<Self, VolumeError> {
        validate_hex_id(value, "fsap-").map(|v| Self(v.to_owned()))
    }

    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

/// A validated, canonical mount path under `ALLOWED_MOUNT_ROOTS`. Its shape
/// check and `overlaps` are `rayd_core::mount_path`'s, not reimplemented
/// here (see the module doc comment).
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct MountPath(String);

impl MountPath {
    pub fn parse(value: &str) -> Result<Self, VolumeError> {
        mount_path::validate_one(value).map_err(|_| VolumeError::InvalidPath)?;
        Ok(Self(value.to_owned()))
    }

    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }

    /// `true` if `self` and `other` are the same path, or one is an
    /// ancestor directory of the other (research doc §4.1 rule 2: "sin
    /// solapamientos ni anidamiento").
    #[must_use]
    pub fn overlaps(&self, other: &MountPath) -> bool {
        mount_path::overlaps(&self.0, &other.0)
    }
}

/// A validated IPv4 dotted-quad, used for `efs-utils`' `mounttargetip=`
/// (research doc R3: avoids depending on the guest resolving the VPC's
/// private DNS zone for the mount target, EFS-5).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct MountTargetIp([u8; 4]);

impl MountTargetIp {
    pub fn parse(value: &str) -> Result<Self, VolumeError> {
        let mut octets = [0u8; 4];
        let mut parts = value.split('.');
        for octet in &mut octets {
            let part = parts.next().ok_or(VolumeError::InvalidIdentifier)?;
            if part.is_empty() || (part.len() > 1 && part.starts_with('0')) {
                return Err(VolumeError::InvalidIdentifier);
            }
            *octet = part
                .parse::<u8>()
                .map_err(|_| VolumeError::InvalidIdentifier)?;
        }
        if parts.next().is_some() {
            return Err(VolumeError::InvalidIdentifier);
        }
        Ok(Self(octets))
    }

    #[must_use]
    pub fn octets(&self) -> [u8; 4] {
        self.0
    }
}

/// One validated `volumes=` entry, resolved from the SDK's `EfsVolume`
/// (research doc §4.5). `read_only` is enforced server-side by the
/// execution role's IAM condition, never by the mount option alone
/// (research doc §4.6): the field here only tells the adapter which mount
/// option to pass.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VolumeSpec {
    pub file_system_id: FileSystemId,
    pub access_point_id: AccessPointId,
    pub mount_path: MountPath,
    pub read_only: bool,
    pub mount_target_ip: Option<MountTargetIp>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_well_formed_file_system_id_parses() {
        assert!(FileSystemId::parse("fs-0123abcd").is_ok());
    }

    #[test]
    fn a_file_system_id_without_the_prefix_is_rejected() {
        assert_eq!(
            FileSystemId::parse("0123abcd"),
            Err(VolumeError::InvalidIdentifier)
        );
    }

    #[test]
    fn a_file_system_id_with_uppercase_hex_is_rejected() {
        assert_eq!(
            FileSystemId::parse("fs-0123ABCD"),
            Err(VolumeError::InvalidIdentifier)
        );
    }

    #[test]
    fn a_file_system_id_shorter_than_the_minimum_is_rejected() {
        assert_eq!(
            FileSystemId::parse("fs-123"),
            Err(VolumeError::InvalidIdentifier)
        );
    }

    #[test]
    fn an_access_point_id_uses_its_own_prefix() {
        assert!(AccessPointId::parse("fsap-0123abcd").is_ok());
        assert_eq!(
            AccessPointId::parse("fs-0123abcd"),
            Err(VolumeError::InvalidIdentifier)
        );
    }

    #[test]
    fn a_mount_path_under_mnt_is_accepted() {
        assert!(MountPath::parse("/mnt/data").is_ok());
    }

    #[test]
    fn a_mount_path_under_home_user_is_accepted() {
        assert!(MountPath::parse("/home/user/data").is_ok());
    }

    #[test]
    fn a_relative_path_is_rejected() {
        assert_eq!(MountPath::parse("mnt/data"), Err(VolumeError::InvalidPath));
    }

    #[test]
    fn a_path_outside_the_allowed_roots_is_rejected() {
        assert_eq!(MountPath::parse("/etc/data"), Err(VolumeError::InvalidPath));
    }

    #[test]
    fn the_allowed_root_itself_is_rejected() {
        assert_eq!(MountPath::parse("/mnt/"), Err(VolumeError::InvalidPath));
        assert_eq!(MountPath::parse("/mnt"), Err(VolumeError::InvalidPath));
    }

    #[test]
    fn a_path_with_a_trailing_slash_is_not_canonical() {
        assert_eq!(
            MountPath::parse("/mnt/data/"),
            Err(VolumeError::InvalidPath)
        );
    }

    #[test]
    fn a_path_with_dot_dot_is_not_canonical() {
        assert_eq!(
            MountPath::parse("/mnt/../etc"),
            Err(VolumeError::InvalidPath)
        );
    }

    #[test]
    fn a_path_with_a_double_slash_is_not_canonical() {
        assert_eq!(
            MountPath::parse("/mnt//data"),
            Err(VolumeError::InvalidPath)
        );
    }

    #[test]
    fn identical_paths_overlap() {
        let path = MountPath::parse("/mnt/data").unwrap();
        assert!(path.overlaps(&path.clone()));
    }

    #[test]
    fn a_nested_path_overlaps_its_ancestor() {
        let parent = MountPath::parse("/mnt/data").unwrap();
        let child = MountPath::parse("/mnt/data/sub").unwrap();
        assert!(parent.overlaps(&child));
        assert!(child.overlaps(&parent));
    }

    #[test]
    fn sibling_paths_do_not_overlap() {
        let a = MountPath::parse("/mnt/data").unwrap();
        let b = MountPath::parse("/mnt/other").unwrap();
        assert!(!a.overlaps(&b));
    }

    #[test]
    fn a_well_formed_mount_target_ip_parses() {
        let ip = MountTargetIp::parse("10.0.1.25").unwrap();
        assert_eq!(ip.octets(), [10, 0, 1, 25]);
    }

    #[test]
    fn a_mount_target_ip_with_a_leading_zero_octet_is_rejected() {
        assert_eq!(
            MountTargetIp::parse("10.0.01.25"),
            Err(VolumeError::InvalidIdentifier)
        );
    }

    #[test]
    fn a_mount_target_ip_with_too_few_octets_is_rejected() {
        assert_eq!(
            MountTargetIp::parse("10.0.1"),
            Err(VolumeError::InvalidIdentifier)
        );
    }

    #[test]
    fn a_mount_target_ip_with_an_out_of_range_octet_is_rejected() {
        assert_eq!(
            MountTargetIp::parse("10.0.1.256"),
            Err(VolumeError::InvalidIdentifier)
        );
    }
}
