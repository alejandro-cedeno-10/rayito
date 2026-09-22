//! Persistence of the user's home beyond one `MicroVM` (ADR-009): what a
//! checkpoint archives and where it goes in S3, how a restore reads it
//! back, the one-operation-per-sandbox lease, the progress accounting and
//! the error table. Pure rules over the `HomeArchiver`, `ObjectStore`,
//! `PartSource`/`ChunkSink` and `BlockingRunner` ports; tar, gzip, S3 and
//! the blocking pool live in the `rayd` adapters.

pub mod checkpoint;
pub mod error;
pub mod keys;
pub mod manifest;
pub mod plan;
pub mod ports;
pub mod progress;
pub mod restore;

#[cfg(test)]
pub(crate) mod fake;

use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

pub use checkpoint::{
    CheckpointDone, CheckpointRequestInfo, ManifestMeta, PreparedCheckpoint, prepare_checkpoint,
    run_checkpoint,
};
pub use error::{ManifestRejection, PersistenceError, StatusKind};
pub use keys::{
    BUCKET_NAME_MAX, BUCKET_NAME_MIN, BucketName, BucketRejection, KeyPrefix, KeyPrefixRejection,
    ObjectKeys,
};
pub use manifest::{MANIFEST_MAX_BYTES, Manifest, ManifestInput};
pub use plan::{
    ArchivePlan, EXCLUDE_MAX_BYTES, ExcludeList, ExcludeRejection, IGNORED_NAMES, IGNORED_PATHS,
    IgnoreRules,
};
pub use ports::{
    ArchiveError, ArchiveSink, ArchiveSummary, BlockingRunner, ChunkSink, ExtractSummary,
    HomeArchiver, JoinFailure, ObjectBody, ObjectStore, PartSource, PutSummary, SinkClosed,
    StoreError, StoreErrorKind, StoreTarget,
};
pub use progress::{Counters, ProgressSampler, Snapshot};
pub use restore::{PreparedRestore, RestoreDone, RestoreRequestInfo, prepare_restore, run_restore};

use crate::filesystem::{FilesystemError, FsIdentity, resolve_identity};
use crate::process::{UserLookup, UserPolicy};

pub const ARCHIVE_KEY: &str = "home.tar.gz";
pub const MANIFEST_KEY: &str = "manifest.json";
pub const MANIFEST_VERSION: u32 = 1;
pub const ARCHIVE_CONTENT_TYPE: &str = "application/gzip";
pub const MANIFEST_CONTENT_TYPE: &str = "application/json";
/// Every multipart part but the last, and the threshold below which the
/// archive travels in one `PutObject`.
pub const PART_BYTES: usize = 8 * 1024 * 1024;
pub const PERSIST_KEY_PREFIX_MAX_BYTES: usize = 900;
pub const PERSIST_EXCLUDE_MAX: usize = 64;

/// The S3 location a request names, before validation.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct LocationRequest {
    pub bucket: String,
    pub key_prefix: String,
    pub region: Option<String>,
}

/// A validated location: the store target plus the two keys.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResolvedLocation {
    pub target: StoreTarget,
    pub key_prefix: KeyPrefix,
    pub keys: ObjectKeys,
}

/// D2 validation plus D5's region rule: the request's region, else the
/// agent's default (`AWS_REGION`), else `RegionUnknown`.
pub fn resolve_location(
    request: &LocationRequest,
    default_region: Option<&str>,
) -> Result<ResolvedLocation, PersistenceError> {
    let bucket = BucketName::parse(&request.bucket).map_err(PersistenceError::InvalidBucket)?;
    let key_prefix =
        KeyPrefix::parse(&request.key_prefix).map_err(PersistenceError::InvalidKeyPrefix)?;
    let region = request
        .region
        .as_deref()
        .filter(|region| !region.is_empty())
        .or(default_region)
        .map(str::to_owned)
        .ok_or(PersistenceError::RegionUnknown)?;
    Ok(ResolvedLocation {
        target: StoreTarget {
            bucket,
            region: Some(region),
        },
        keys: key_prefix.object_keys(),
        key_prefix,
    })
}

/// Whose home: the filesystem identity rules, minus root, whatever the
/// image's `RAYITO_ALLOW_ROOT` says (persistence never archives `/root`).
/// Dropping the opt-in before the shared gate is what keeps that promise now
/// that the gate itself, and not a second `uid == 0` check in this module,
/// refuses every privileged account.
pub fn resolve_home_identity(
    request_user: Option<&str>,
    default_user: Option<&str>,
    policy: UserPolicy,
    lookup: &dyn UserLookup,
) -> Result<FsIdentity, PersistenceError> {
    resolve_identity(request_user, default_user, policy.without_root(), lookup).map_err(|error| {
        match error {
            FilesystemError::RootNotAllowed => PersistenceError::RootNotAllowed,
            FilesystemError::PrivilegedAccount => PersistenceError::PrivilegedAccount,
            FilesystemError::UnknownUser => PersistenceError::UnknownUser,
            other => PersistenceError::UserLookupFailed(other.to_string()),
        }
    })
}

/// One checkpoint or restore at a time per sandbox (design D8).
#[derive(Debug, Default)]
pub struct PersistenceGate {
    busy: AtomicBool,
}

impl PersistenceGate {
    #[must_use]
    pub fn new() -> Arc<Self> {
        Arc::new(Self::default())
    }

    pub fn acquire(self: &Arc<Self>) -> Result<PersistenceLease, PersistenceError> {
        if self.busy.swap(true, Ordering::SeqCst) {
            return Err(PersistenceError::Busy);
        }
        Ok(PersistenceLease { gate: self.clone() })
    }

    #[must_use]
    pub fn is_busy(&self) -> bool {
        self.busy.load(Ordering::SeqCst)
    }
}

/// Held for the whole operation; released by `Drop` on every path.
#[must_use = "the lease is released when it drops"]
#[derive(Debug)]
pub struct PersistenceLease {
    gate: Arc<PersistenceGate>,
}

impl Drop for PersistenceLease {
    fn drop(&mut self) {
        self.gate.busy.store(false, Ordering::SeqCst);
    }
}

/// What both flows are built from.
pub struct PersistenceDeps<S, A, R> {
    pub store: Arc<S>,
    pub archiver: Arc<A>,
    pub runner: R,
    pub gate: Arc<PersistenceGate>,
    pub lookup: Arc<dyn UserLookup>,
    pub policy: UserPolicy,
    pub default_region: Option<String>,
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::process::{LookupError, ProcessIdentity};

    struct Users;

    impl UserLookup for Users {
        fn lookup(&self, username: &str) -> Result<ProcessIdentity, LookupError> {
            let (uid, gid, home) = match username {
                "user" => (1000, 1000, "/home/user"),
                "root" | "toor" => (0, 0, "/root"),
                "operator" => (11, 0, "/root"),
                _ => return Err(LookupError::UnknownUser),
            };
            Ok(ProcessIdentity {
                uid,
                gid,
                groups: vec![gid],
                username: username.to_owned(),
                home: home.to_owned(),
                shell: "/bin/sh".to_owned(),
            })
        }
    }

    #[test]
    fn location_resolution_applies_d2_and_the_region_rule() {
        let request = LocationRequest {
            bucket: "my-bucket".to_owned(),
            key_prefix: "rayito/x".to_owned(),
            region: None,
        };
        assert_eq!(
            resolve_location(&request, None).unwrap_err(),
            PersistenceError::RegionUnknown
        );
        let resolved = resolve_location(&request, Some("us-east-1")).unwrap();
        assert_eq!(resolved.target.region.as_deref(), Some("us-east-1"));
        assert_eq!(resolved.keys.manifest, "rayito/x/manifest.json");
        let explicit = resolve_location(
            &LocationRequest {
                region: Some("eu-west-1".to_owned()),
                ..request.clone()
            },
            Some("us-east-1"),
        )
        .unwrap();
        assert_eq!(explicit.target.region.as_deref(), Some("eu-west-1"));
        let empty_region = resolve_location(
            &LocationRequest {
                region: Some(String::new()),
                ..request.clone()
            },
            Some("us-east-1"),
        )
        .unwrap();
        assert_eq!(empty_region.target.region.as_deref(), Some("us-east-1"));
        assert!(matches!(
            resolve_location(
                &LocationRequest {
                    bucket: "B".to_owned(),
                    ..request.clone()
                },
                Some("us-east-1")
            ),
            Err(PersistenceError::InvalidBucket(_))
        ));
        assert!(matches!(
            resolve_location(
                &LocationRequest {
                    key_prefix: "/x".to_owned(),
                    ..request
                },
                Some("us-east-1")
            ),
            Err(PersistenceError::InvalidKeyPrefix(_))
        ));
    }

    #[test]
    fn root_is_never_a_persistence_identity() {
        let permissive = UserPolicy { allow_root: true };
        assert_eq!(
            resolve_home_identity(Some("root"), None, permissive, &Users).unwrap_err(),
            PersistenceError::RootNotAllowed
        );
        assert_eq!(
            resolve_home_identity(Some("toor"), None, permissive, &Users).unwrap_err(),
            PersistenceError::RootNotAllowed
        );
        assert_eq!(
            resolve_home_identity(Some("root"), None, UserPolicy::default(), &Users).unwrap_err(),
            PersistenceError::RootNotAllowed
        );
        assert_eq!(
            resolve_home_identity(Some("nobody"), None, UserPolicy::default(), &Users).unwrap_err(),
            PersistenceError::UnknownUser
        );
        let user = resolve_home_identity(None, None, UserPolicy::default(), &Users).unwrap();
        assert_eq!(user.home, "/home/user");
    }

    #[test]
    fn a_system_account_is_never_a_persistence_identity() {
        for policy in [UserPolicy::default(), UserPolicy { allow_root: true }] {
            assert_eq!(
                resolve_home_identity(Some("operator"), None, policy, &Users).unwrap_err(),
                PersistenceError::PrivilegedAccount
            );
        }
    }

    #[test]
    fn gate_admits_one_lease_at_a_time() {
        let gate = PersistenceGate::new();
        let lease = gate.acquire().unwrap();
        assert!(gate.is_busy());
        assert_eq!(gate.acquire().unwrap_err(), PersistenceError::Busy);
        drop(lease);
        assert!(!gate.is_busy());
        let _again = gate.acquire().unwrap();
    }
}
