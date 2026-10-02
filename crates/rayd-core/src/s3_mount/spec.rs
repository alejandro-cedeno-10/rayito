//! Desired state of one S3 mount, and the pure checks `apply()` runs
//! before touching `/dev/fuse` or spawning `mount-s3`: every path's own
//! shape, no two mounts in the same request overlapping, and every bucket
//! on the image's allowlist. The SDK already enforces the mount-path shape
//! before `ConfigureSandbox` is ever called (`_mount_path.py` /
//! `mount-path.ts`), but `rayd` never trusts a client to have run that
//! check itself: a non-SDK or buggy caller could otherwise steer
//! `FuseDevice::attach`'s own `create_dir_all`/`mount(2)` at an arbitrary
//! path (`/etc/cron.d`, `/usr/local/bin`, `/`), so `rayd_core::mount_path`
//! is re-run here, first, before anything else.

use super::error::MountErrorClass;
use crate::mount_path;

/// One desired mount, as carried by `S3MountsConfig.mounts` (proto field
/// numbers match one to one).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct S3Mount {
    pub mount_path: String,
    pub bucket: String,
    pub prefix: String,
    pub read_only: bool,
    pub allow_overwrite: bool,
    pub allow_delete: bool,
}

/// A request rejected before anything changed: `SECTION_CODE_INVALID` with
/// this `class` as `error_class`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MountValidationError {
    pub mount_path: String,
    pub class: MountErrorClass,
}

/// Image-level configuration (`RAYITO_ALLOWED_MOUNT_BUCKETS`, baked in at
/// `rayito image publish --env`, never a per-request activation switch —
/// ADR-014 rule 4): a comma-separated bucket list, trimmed, empty entries
/// dropped. An unset or empty variable parses to an empty list, which
/// `validate_mounts` treats as "no bucket allowed": a mount feature that
/// exists only on a hand-opted-into image variant fails closed rather than
/// silently permitting every bucket in the account.
#[must_use]
pub fn parse_allowed_buckets(raw: &str) -> Vec<String> {
    raw.split(',')
        .map(str::trim)
        .filter(|bucket| !bucket.is_empty())
        .map(str::to_owned)
        .collect()
}

/// Checked in request order, so the first offending mount is the one
/// reported: path shape and overlap first (`rayd_core::mount_path`,
/// `InvalidPath`), then the bucket allowlist (`NotAllowed`); `allowed`
/// empty rejects every bucket.
pub fn validate_mounts(mounts: &[S3Mount], allowed: &[String]) -> Result<(), MountValidationError> {
    let paths: Vec<&str> = mounts
        .iter()
        .map(|mount| mount.mount_path.as_str())
        .collect();
    if let Err((path, _reason)) = mount_path::validate_mount_paths(&paths) {
        return Err(MountValidationError {
            mount_path: path.to_owned(),
            class: MountErrorClass::InvalidPath,
        });
    }
    for mount in mounts {
        if !allowed.iter().any(|candidate| candidate == &mount.bucket) {
            return Err(MountValidationError {
                mount_path: mount.mount_path.clone(),
                class: MountErrorClass::NotAllowed,
            });
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn mount(path: &str, bucket: &str) -> S3Mount {
        S3Mount {
            mount_path: path.to_owned(),
            bucket: bucket.to_owned(),
            prefix: String::new(),
            read_only: true,
            allow_overwrite: false,
            allow_delete: false,
        }
    }

    #[test]
    fn parse_allowed_buckets_trims_and_drops_empty_entries() {
        assert_eq!(
            parse_allowed_buckets(" team-data , , runs-bucket ,"),
            vec!["team-data".to_owned(), "runs-bucket".to_owned()]
        );
        assert_eq!(parse_allowed_buckets(""), Vec::<String>::new());
    }

    #[test]
    fn an_empty_allowlist_rejects_every_bucket() {
        let error = validate_mounts(&[mount("/mnt/data", "team-data")], &[]).unwrap_err();
        assert_eq!(error.mount_path, "/mnt/data");
        assert_eq!(error.class, MountErrorClass::NotAllowed);
    }

    #[test]
    fn a_bucket_on_the_allowlist_passes() {
        let allowed = vec!["team-data".to_owned()];
        assert!(validate_mounts(&[mount("/mnt/data", "team-data")], &allowed).is_ok());
    }

    #[test]
    fn a_bucket_outside_the_allowlist_is_rejected() {
        let allowed = vec!["team-data".to_owned()];
        let error = validate_mounts(&[mount("/mnt/data", "other-bucket")], &allowed).unwrap_err();
        assert_eq!(error.class, MountErrorClass::NotAllowed);
    }

    #[test]
    fn duplicate_mount_paths_in_one_request_are_rejected_as_invalid_path() {
        let allowed = vec!["team-data".to_owned()];
        let mounts = vec![
            mount("/mnt/data", "team-data"),
            mount("/mnt/data", "team-data"),
        ];
        let error = validate_mounts(&mounts, &allowed).unwrap_err();
        assert_eq!(error.mount_path, "/mnt/data");
        assert_eq!(error.class, MountErrorClass::InvalidPath);
    }

    #[test]
    fn distinct_paths_against_the_same_allowed_bucket_both_pass() {
        let allowed = vec!["team-data".to_owned()];
        let mounts = vec![mount("/mnt/a", "team-data"), mount("/mnt/b", "team-data")];
        assert!(validate_mounts(&mounts, &allowed).is_ok());
    }

    #[test]
    fn a_path_outside_the_allowed_roots_is_rejected_as_invalid_path_before_the_allowlist_runs() {
        // Even an allowlisted bucket cannot rescue a malicious/buggy
        // caller that names a path outside `/mnt/`/`/home/user/`: shape
        // is checked first.
        let allowed = vec!["team-data".to_owned()];
        let error = validate_mounts(&[mount("/etc/cron.d", "team-data")], &allowed).unwrap_err();
        assert_eq!(error.class, MountErrorClass::InvalidPath);
    }
}
