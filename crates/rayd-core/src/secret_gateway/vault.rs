//! `SecretValue`: the only shape a gateway's header value ever takes in
//! `rayd` (ADR-023, SEC-3). It wraps a `Zeroizing<String>` and derives
//! nothing that could print it — no `Debug`, `Display` or `serde`
//! `Serialize` — so a stray `{:?}` on a struct that holds one is a compile
//! error, not a leak; every container that embeds a `SecretValue` writes
//! its own `Debug` by hand and skips the field.

use std::fmt;

use zeroize::Zeroizing;

/// A single header's value, held only long enough to build the outbound
/// request. Equality is field-wise (`Zeroizing<String>: PartialEq`); no
/// comparison here ever decides an auth outcome, so it does not need to be
/// constant-time.
#[derive(Clone, PartialEq, Eq)]
pub struct SecretValue(Zeroizing<String>);

/// Header values and request lines are both CRLF-delimited; without this
/// check a value containing `\r\n` could smuggle a second header or a
/// response-splitting payload into the forwarded request.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct HeaderInjectionError;

impl fmt::Display for HeaderInjectionError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("header_rejected")
    }
}

impl std::error::Error for HeaderInjectionError {}

impl SecretValue {
    /// `header_safe` must pass before this is ever called; kept separate
    /// so the caller decides whether an unsafe value is a bug (push
    /// resolution) or a request to fail (never reached today).
    #[must_use]
    pub fn new(value: String) -> Self {
        Self(Zeroizing::new(value))
    }

    /// `Err` when `value` could not go into an HTTP header line unescaped:
    /// a CR, LF or NUL byte.
    ///
    /// # Errors
    /// `HeaderInjectionError` when `value` contains a CR, LF or NUL byte.
    pub fn header_safe(value: String) -> Result<Self, HeaderInjectionError> {
        if value.bytes().any(|byte| matches!(byte, b'\r' | b'\n' | 0)) {
            return Err(HeaderInjectionError);
        }
        Ok(Self::new(value))
    }

    /// The one place the raw bytes leave this type: building the outbound
    /// header right before the request goes out. Never store what this
    /// returns; borrow it, use it, drop it.
    #[must_use]
    pub fn expose(&self) -> &str {
        &self.0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_clean_value_round_trips() {
        let value = SecretValue::header_safe("sk-abc123".to_owned()).unwrap();
        assert_eq!(value.expose(), "sk-abc123");
    }

    #[test]
    fn a_value_carrying_crlf_is_rejected() {
        assert!(matches!(
            SecretValue::header_safe("sk-abc\r\nX-Evil: 1".to_owned()),
            Err(HeaderInjectionError)
        ));
    }

    #[test]
    fn a_value_carrying_a_lone_lf_or_nul_is_rejected() {
        assert!(SecretValue::header_safe("a\nb".to_owned()).is_err());
        assert!(SecretValue::header_safe("a\0b".to_owned()).is_err());
    }

    #[test]
    fn equal_values_compare_equal_without_exposing_either() {
        let a = SecretValue::new("same".to_owned());
        let b = SecretValue::new("same".to_owned());
        assert!(a == b);
    }
}
