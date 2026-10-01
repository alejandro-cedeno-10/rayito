//! Credential adapters for the 0.6 optional features (M15 foundations,
//! ADR-015/016 context): one `ImdsCredentialsProvider` instance (the same
//! type `adapters::s3_store` already builds for ADR-009 persistence) meant
//! to be shared by every feature that needs the execution role inside the
//! guest (s3-mounts, efs-volumes, rayd-otlp with role auth), plus
//! `PushedCredentials`, the in-memory holder for values the SDK delivers
//! through `ConfigureSandbox` instead of IMDS (secret-gateway's vaulted
//! header values). Neither type is wired into a real feature yet — each
//! feature's own adapter will take an `Arc<ImdsCredentialBroker>` once it
//! exists.
//!
//! `S3ObjectStore` still builds its own `ImdsCredentialsProvider` instance
//! (ADR-009, unchanged by this change): collapsing the two into one shared
//! instance is a follow-up for whichever feature first needs role
//! credentials, tracked as an open question in
//! `openspec/changes/v06-foundations/design.md`, not done speculatively
//! here.

use std::collections::HashMap;
use std::sync::{Mutex, PoisonError};
use std::time::SystemTime;

use aws_config::imds::credentials::ImdsCredentialsProvider;
use aws_sdk_s3::config::ProvideCredentials;
use rayd_core::credentials::{CredentialKind, CredentialLease, CredentialProvider};
use zeroize::Zeroizing;

use super::s3_store::EXECUTION_ROLE_PROFILE;

#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
pub enum CredentialBrokerError {
    #[error("imds credentials unavailable")]
    Unavailable,
}

/// The execution-role credentials themselves, held only here: the secret
/// fields are `Zeroizing` and this type is never `Debug`/`Display`.
pub struct GuestCredentials {
    pub access_key_id: String,
    pub secret_access_key: Zeroizing<String>,
    pub session_token: Option<Zeroizing<String>>,
    pub expires_at: SystemTime,
}

/// One IMDS-backed provider, cached behind `rayd_core::credentials`'
/// refresh-margin rule so a feature's hot path never calls IMDS directly.
pub struct ImdsCredentialBroker {
    provider: ImdsCredentialsProvider,
    live: Mutex<Option<GuestCredentials>>,
}

impl Default for ImdsCredentialBroker {
    fn default() -> Self {
        Self::new()
    }
}

impl ImdsCredentialBroker {
    #[must_use]
    pub fn new() -> Self {
        Self {
            provider: ImdsCredentialsProvider::builder()
                .profile(EXECUTION_ROLE_PROFILE)
                .build(),
            live: Mutex::new(None),
        }
    }

    /// The cached credentials, fetching a fresh set from IMDS first only
    /// when none are cached yet or the cached ones need refresh
    /// (`CredentialLease::needs_refresh`).
    pub async fn ensure(&self) -> Result<GuestCredentials, CredentialBrokerError> {
        let now = SystemTime::now();
        let still_fresh = self
            .current()
            .is_some_and(|lease| !lease.needs_refresh(now));
        if still_fresh {
            // `current()` returning `Some` means `live` holds a value; only
            // another `ensure()` could have cleared it since, and that
            // would have replaced it with an equally fresh one.
            if let Some(credentials) = self.lock().as_ref() {
                return Ok(clone_credentials(credentials));
            }
        }
        let fetched = self
            .provider
            .provide_credentials()
            .await
            .map_err(|_source| CredentialBrokerError::Unavailable)?;
        let credentials = GuestCredentials {
            access_key_id: fetched.access_key_id().to_owned(),
            secret_access_key: Zeroizing::new(fetched.secret_access_key().to_owned()),
            session_token: fetched
                .session_token()
                .map(|token| Zeroizing::new(token.to_owned())),
            expires_at: fetched
                .expiry()
                .unwrap_or_else(|| now + rayd_core::credentials::REFRESH_MARGIN),
        };
        let clone = clone_credentials(&credentials);
        *self.lock() = Some(credentials);
        Ok(clone)
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, Option<GuestCredentials>> {
        self.live.lock().unwrap_or_else(PoisonError::into_inner)
    }
}

fn clone_credentials(credentials: &GuestCredentials) -> GuestCredentials {
    GuestCredentials {
        access_key_id: credentials.access_key_id.clone(),
        secret_access_key: credentials.secret_access_key.clone(),
        session_token: credentials.session_token.clone(),
        expires_at: credentials.expires_at,
    }
}

impl CredentialProvider for ImdsCredentialBroker {
    fn current(&self) -> Option<CredentialLease> {
        self.lock().as_ref().map(|credentials| CredentialLease {
            kind: CredentialKind::ExecutionRole,
            expires_at: credentials.expires_at,
        })
    }
}

/// Credentials pushed into the agent by the SDK through `ConfigureSandbox`
/// (secret-gateway's vaulted header values, for example): never touches
/// IMDS, held only in process memory, zeroized on replacement or drop.
#[derive(Default)]
pub struct PushedCredentials {
    values: Mutex<HashMap<String, Zeroizing<String>>>,
}

impl PushedCredentials {
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    pub fn set(&self, name: &str, value: Zeroizing<String>) {
        self.lock().insert(name.to_owned(), value);
    }

    #[must_use]
    pub fn get(&self, name: &str) -> Option<Zeroizing<String>> {
        self.lock().get(name).cloned()
    }

    pub fn clear(&self, name: &str) {
        self.lock().remove(name);
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, HashMap<String, Zeroizing<String>>> {
        self.values.lock().unwrap_or_else(PoisonError::into_inner)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pushed_credentials_round_trip_by_name() {
        let pushed = PushedCredentials::new();
        assert!(pushed.get("anthropic").is_none());
        pushed.set("anthropic", Zeroizing::new("sk-test".to_owned()));
        assert_eq!(
            pushed.get("anthropic").as_deref().map(String::as_str),
            Some("sk-test")
        );
    }

    #[test]
    fn clearing_a_pushed_credential_removes_it() {
        let pushed = PushedCredentials::new();
        pushed.set("anthropic", Zeroizing::new("sk-test".to_owned()));
        pushed.clear("anthropic");
        assert!(pushed.get("anthropic").is_none());
    }

    #[test]
    fn setting_the_same_name_again_replaces_the_value() {
        let pushed = PushedCredentials::new();
        pushed.set("anthropic", Zeroizing::new("sk-old".to_owned()));
        pushed.set("anthropic", Zeroizing::new("sk-new".to_owned()));
        assert_eq!(
            pushed.get("anthropic").as_deref().map(String::as_str),
            Some("sk-new")
        );
    }

    #[test]
    fn a_broker_with_nothing_cached_yet_reports_no_current_lease() {
        let broker = ImdsCredentialBroker::new();
        assert!(broker.current().is_none());
    }
}
