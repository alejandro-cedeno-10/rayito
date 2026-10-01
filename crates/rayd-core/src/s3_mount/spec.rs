//! Desired state of one S3 mount, and the two pure checks `apply()` runs
//! before touching `/dev/fuse` or spawning `mount-s3`: no two mounts in the
//! same request name the same path, and every bucket is on the image's
//! allowlist. The mount-path *shape* itself (absolute, canonical, under
//! `/mnt/` or `/home/user/`, no overlap between mounts) is already
//! enforced by the SDK before `ConfigureSandbox` is ever called
//! (`_mount_path.py` / `mount-path.ts`), so `rayd` only re-checks for
//! exact duplicates within one request — the one thing a buggy or
//! malicious client could still send that the SDK-side rule does not
//! cover on its own.

use super::error::MountErrorClass;

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
/// reported; `allowed` empty rejects every mount with `NotAllowed`.
pub fn validate_mounts(
    mounts: &[S3Mount],
    allowed: &[String],
) -> Result<(), MountValidationError> {
    let mut seen_paths: Vec<&str> = Vec::with_capacity(mounts.len());
    for mount in mounts {
        if seen_paths.contains(&mount.mount_path.as_str()) {
            return Err(MountValidationError {
                mount_path: mount.mount_path.clone(),
                class: MountErrorClass::NotAllowed,
            });
        }
        seen_paths.push(&mount.mount_path);
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
    fn duplicate_mount_paths_in_one_request_are_rejected() {
        let allowed = vec!["team-data".to_owned()];
        let mounts = vec![mount("/mnt/data", "team-data"), mount("/mnt/data", "team-data")];
        let error = validate_mounts(&mounts, &allowed).unwrap_err();
        assert_eq!(error.mount_path, "/mnt/data");
    }

    #[test]
    fn distinct_paths_against_the_same_allowed_bucket_both_pass() {
        let allowed = vec!["team-data".to_owned()];
        let mounts = vec![mount("/mnt/a", "team-data"), mount("/mnt/b", "team-data")];
        assert!(validate_mounts(&mounts, &allowed).is_ok());
    }
}
