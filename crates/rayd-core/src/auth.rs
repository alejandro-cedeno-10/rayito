//! Access-token gate. `rayd` never holds the sandbox secret: it keeps the
//! SHA-256 digest delivered by the `/run` hook, installs it once per boot and
//! compares presented secrets against it in constant time. `Health` is the
//! only RPC that skips the gate (ADR-004).

use std::fmt;
use std::sync::{Mutex, MutexGuard, PoisonError};

use sha2::{Digest, Sha256};
use subtle::ConstantTimeEq;
use thiserror::Error;
use zeroize::{Zeroize, ZeroizeOnDrop};

/// gRPC metadata key carrying the sandbox secret on every authenticated RPC.
pub const ACCESS_TOKEN_METADATA_KEY: &str = "x-access-token";
/// The only RPC path served without a token.
pub const ANONYMOUS_RPC_PATH: &str = "/rayito.v1.HealthService/Health";

const DIGEST_LEN: usize = 32;
const DIGEST_HEX_LEN: usize = DIGEST_LEN * 2;

#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum AuthError {
    #[error("no access token installed: /run has not delivered a valid token digest")]
    TokenNotInstalled,
    #[error("access token missing from request metadata")]
    TokenMissing,
    #[error("access token does not match the installed digest")]
    TokenMismatch,
    #[error("token digest must be {DIGEST_HEX_LEN} lowercase hex characters, got {actual}")]
    MalformedDigest { actual: usize },
}

/// SHA-256 of the sandbox secret. Zeroized when dropped or replaced.
#[derive(Zeroize, ZeroizeOnDrop, PartialEq, Eq)]
pub struct TokenDigest([u8; DIGEST_LEN]);

impl TokenDigest {
    #[must_use]
    pub fn of_secret(secret: &[u8]) -> Self {
        Self(Sha256::digest(secret).into())
    }

    pub fn from_hex(hex: &str) -> Result<Self, AuthError> {
        let malformed = AuthError::MalformedDigest { actual: hex.len() };
        if hex.len() != DIGEST_HEX_LEN {
            return Err(malformed);
        }
        let mut bytes = [0u8; DIGEST_LEN];
        for (slot, pair) in bytes.iter_mut().zip(hex.as_bytes().as_chunks::<2>().0) {
            *slot = decode_hex_pair(pair).ok_or_else(|| malformed.clone())?;
        }
        Ok(Self(bytes))
    }

    fn matches(&self, other: &TokenDigest) -> bool {
        self.0.ct_eq(&other.0).into()
    }
}

impl fmt::Debug for TokenDigest {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("TokenDigest(..)")
    }
}

fn decode_hex_pair(pair: &[u8]) -> Option<u8> {
    let high = hex_value(*pair.first()?)?;
    let low = hex_value(*pair.get(1)?)?;
    Some((high << 4) | low)
}

fn hex_value(byte: u8) -> Option<u8> {
    match byte {
        b'0'..=b'9' => Some(byte - b'0'),
        b'a'..=b'f' => Some(byte - b'a' + 10),
        _ => None,
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum InstallOutcome {
    Installed,
    AlreadyInstalled,
}

/// Holds at most one digest per boot. The first `install_once` wins; later
/// digests are dropped (and zeroized) without touching the installed one.
#[derive(Default)]
pub struct AccessTokenGate {
    installed: Mutex<Option<TokenDigest>>,
}

impl AccessTokenGate {
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    pub fn install_once(&self, digest: TokenDigest) -> InstallOutcome {
        let mut slot = self.lock();
        if slot.is_some() {
            return InstallOutcome::AlreadyInstalled;
        }
        *slot = Some(digest);
        InstallOutcome::Installed
    }

    #[must_use]
    pub fn is_installed(&self) -> bool {
        self.lock().is_some()
    }

    /// Constant-time comparison of `sha256(presented_secret)` with the
    /// installed digest.
    pub fn verify(&self, presented_secret: &[u8]) -> Result<(), AuthError> {
        let presented = TokenDigest::of_secret(presented_secret);
        let slot = self.lock();
        let installed = slot.as_ref().ok_or(AuthError::TokenNotInstalled)?;
        if installed.matches(&presented) {
            Ok(())
        } else {
            Err(AuthError::TokenMismatch)
        }
    }

    fn lock(&self) -> MutexGuard<'_, Option<TokenDigest>> {
        self.installed
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
    }
}

#[must_use]
pub fn requires_access_token(rpc_path: &str) -> bool {
    rpc_path != ANONYMOUS_RPC_PATH
}

/// Authorization rule for one RPC: `Health` is anonymous; everything else must
/// present a secret whose digest matches the installed one.
pub fn authorize(
    gate: &AccessTokenGate,
    rpc_path: &str,
    presented_secret: Option<&[u8]>,
) -> Result<(), AuthError> {
    if !requires_access_token(rpc_path) {
        return Ok(());
    }
    let secret = presented_secret.ok_or(AuthError::TokenMissing)?;
    gate.verify(secret)
}

#[cfg(test)]
mod tests {
    use super::*;

    const SECRET: &[u8] = b"correct horse battery staple";

    fn digest_hex(secret: &[u8]) -> String {
        use std::fmt::Write as _;
        Sha256::digest(secret)
            .iter()
            .fold(String::new(), |mut hex, byte| {
                write!(hex, "{byte:02x}").unwrap();
                hex
            })
    }

    fn gate_with(secret: &[u8]) -> AccessTokenGate {
        let gate = AccessTokenGate::new();
        assert_eq!(
            gate.install_once(TokenDigest::of_secret(secret)),
            InstallOutcome::Installed
        );
        gate
    }

    #[test]
    fn digest_round_trips_through_hex() {
        let parsed = TokenDigest::from_hex(&digest_hex(SECRET)).unwrap();
        assert_eq!(parsed, TokenDigest::of_secret(SECRET));
    }

    #[test]
    fn digest_rejects_wrong_length_and_non_hex() {
        assert_eq!(
            TokenDigest::from_hex("abc").unwrap_err(),
            AuthError::MalformedDigest { actual: 3 }
        );
        let uppercase = digest_hex(SECRET).to_uppercase();
        assert!(matches!(
            TokenDigest::from_hex(&uppercase),
            Err(AuthError::MalformedDigest { .. })
        ));
        let non_hex = "zz".repeat(DIGEST_LEN);
        assert!(matches!(
            TokenDigest::from_hex(&non_hex),
            Err(AuthError::MalformedDigest { .. })
        ));
    }

    #[test]
    fn verify_accepts_the_installed_secret() {
        let gate = gate_with(SECRET);
        assert_eq!(gate.verify(SECRET), Ok(()));
    }

    #[test]
    fn verify_rejects_a_wrong_secret() {
        let gate = gate_with(SECRET);
        assert_eq!(gate.verify(b"wrong"), Err(AuthError::TokenMismatch));
    }

    #[test]
    fn verify_rejects_before_any_install() {
        let gate = AccessTokenGate::new();
        assert!(!gate.is_installed());
        assert_eq!(gate.verify(SECRET), Err(AuthError::TokenNotInstalled));
    }

    #[test]
    fn only_the_first_install_counts() {
        let gate = gate_with(SECRET);
        assert_eq!(
            gate.install_once(TokenDigest::of_secret(b"attacker")),
            InstallOutcome::AlreadyInstalled
        );
        assert_eq!(gate.verify(SECRET), Ok(()));
        assert_eq!(gate.verify(b"attacker"), Err(AuthError::TokenMismatch));
    }

    #[test]
    fn mismatch_anywhere_in_the_digest_is_rejected() {
        let installed = TokenDigest::of_secret(SECRET);
        let mut first_byte_differs = installed.0;
        first_byte_differs[0] ^= 0x01;
        let mut last_byte_differs = installed.0;
        last_byte_differs[DIGEST_LEN - 1] ^= 0x01;
        assert!(!installed.matches(&TokenDigest(first_byte_differs)));
        assert!(!installed.matches(&TokenDigest(last_byte_differs)));
        assert!(installed.matches(&TokenDigest(installed.0)));
    }

    #[test]
    fn health_is_the_only_anonymous_rpc() {
        assert!(!requires_access_token(ANONYMOUS_RPC_PATH));
        assert!(requires_access_token("/rayito.v1.HealthService/Metrics"));
        assert!(requires_access_token("/rayito.v1.ProcessService/List"));
    }

    #[test]
    fn authorize_lets_health_through_without_a_token() {
        let gate = AccessTokenGate::new();
        assert_eq!(authorize(&gate, ANONYMOUS_RPC_PATH, None), Ok(()));
    }

    #[test]
    fn authorize_requires_a_token_for_every_other_rpc() {
        let gate = gate_with(SECRET);
        let path = "/rayito.v1.ProcessService/List";
        assert_eq!(authorize(&gate, path, None), Err(AuthError::TokenMissing));
        assert_eq!(
            authorize(&gate, path, Some(b"nope")),
            Err(AuthError::TokenMismatch)
        );
        assert_eq!(authorize(&gate, path, Some(SECRET)), Ok(()));
    }
}
