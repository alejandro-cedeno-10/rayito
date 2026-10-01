//! Credential port for 0.6 features that need role credentials inside the
//! guest (s3-mounts, efs-volumes, rayd-otlp with role auth): one
//! IMDS-backed provider shared by every feature and by the existing S3
//! persistence store (`adapters::s3_store`, ADR-009, refactored to take it
//! with identical behaviour), so a rotating execution-role credential is
//! fetched and cached in exactly one place. `rayito-base-caps` is the only
//! image variant this ever runs on; that check belongs to the SDK's
//! `_role_policy.py` / `role-policy.ts` (`require_caps_for`), not here —
//! this port only models a lease's freshness, never an image or a feature.
//!
//! Credential *values* (access key id, secret, session token) never live in
//! `rayd-core`: it is pure, and those bytes are the adapter's concern
//! (`Zeroizing`, never `Debug`/`Display`). This module only answers "is this
//! lease still good enough to use".

use std::time::{Duration, SystemTime};

/// Where a lease's credentials came from: the execution role via IMDS
/// (shared across features), or pushed into the agent by the SDK through
/// `ConfigureSandbox` (`secret-gateway`'s vaulted header values, for
/// example — a lease of its own, not IMDS-backed).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CredentialKind {
    ExecutionRole,
    Pushed,
}

/// Refresh ahead of expiry: IMDS hands out role credentials with their
/// expiry roughly an hour out, and a lease this close to expiring is
/// refreshed eagerly by the adapter rather than risking a mid-request 403
/// from the signed call it backs.
pub const REFRESH_MARGIN: Duration = Duration::from_mins(10);

/// One set of live credentials, reduced to what the domain needs to judge:
/// where they came from and when they stop being good.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CredentialLease {
    pub kind: CredentialKind,
    pub expires_at: SystemTime,
}

impl CredentialLease {
    /// `true` once `now` is within `REFRESH_MARGIN` of `expires_at`, or past
    /// it: the adapter proactively re-fetches rather than wait for a 403.
    #[must_use]
    pub fn needs_refresh(&self, now: SystemTime) -> bool {
        match self.expires_at.duration_since(now) {
            Ok(remaining) => remaining <= REFRESH_MARGIN,
            Err(_already_past) => true,
        }
    }
}

/// What a feature asks for: the current lease, if any, refreshed by the
/// adapter (`adapters::credential_broker`) without the feature ever
/// touching IMDS itself. `None` means no lease has been fetched yet (never
/// attempted, or the last attempt failed); the caller decides whether that
/// is fatal.
pub trait CredentialProvider: Send + Sync {
    fn current(&self) -> Option<CredentialLease>;
}

#[cfg(test)]
mod tests {
    use super::*;

    fn lease(seconds_from_now: i64) -> (CredentialLease, SystemTime) {
        let now = SystemTime::UNIX_EPOCH + Duration::from_secs(1_700_000_000);
        let expires_at = if seconds_from_now >= 0 {
            now + Duration::from_secs(seconds_from_now.unsigned_abs())
        } else {
            now - Duration::from_secs(seconds_from_now.unsigned_abs())
        };
        (
            CredentialLease {
                kind: CredentialKind::ExecutionRole,
                expires_at,
            },
            now,
        )
    }

    #[test]
    fn a_lease_well_inside_its_expiry_needs_no_refresh() {
        let (credential_lease, now) = lease(3600);
        assert!(!credential_lease.needs_refresh(now));
    }

    #[test]
    fn a_lease_exactly_at_the_refresh_margin_is_refreshed() {
        let (credential_lease, now) = lease(600);
        assert!(credential_lease.needs_refresh(now));
    }

    #[test]
    fn a_lease_just_outside_the_margin_is_not_yet_refreshed() {
        let (credential_lease, now) = lease(601);
        assert!(!credential_lease.needs_refresh(now));
    }

    #[test]
    fn an_already_expired_lease_always_needs_refresh() {
        let (credential_lease, now) = lease(-1);
        assert!(credential_lease.needs_refresh(now));
    }
}
