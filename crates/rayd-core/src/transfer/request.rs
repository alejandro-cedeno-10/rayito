//! What `StartImport` and `StartExport` carry once the transport is gone,
//! and the checks both share (design D6): the named object, the expiry
//! window, and the presigned URLs as bearer credentials that never show in
//! a `Debug`.

use std::fmt;

use zeroize::Zeroizing;

use super::TRANSFER_PRESIGN_MAX_SECONDS;
use super::error::{RequestRejection, TransferError};
use super::url_policy::{RequestUrl, S3ObjectSpec, UrlRole};

/// Accepted clock skew between the caller that signed and this VM.
pub const CLOCK_SKEW_ALLOWANCE_MS: i64 = 300_000;

/// One presigned request: the URL and the signed headers `rayd` must send.
pub struct PresignedUrl {
    pub url: Zeroizing<String>,
    pub headers: Vec<(String, String)>,
}

impl PresignedUrl {
    #[must_use]
    pub fn new(url: String, headers: Vec<(String, String)>) -> Self {
        Self {
            url: Zeroizing::new(url),
            headers,
        }
    }

    #[must_use]
    pub fn as_request(&self, role: UrlRole) -> RequestUrl<'_> {
        RequestUrl {
            role,
            url: self.url.as_str(),
            headers: &self.headers,
        }
    }
}

impl fmt::Debug for PresignedUrl {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("PresignedUrl(<redacted>)")
    }
}

/// `S3Object` as it came; parsed into an `S3ObjectSpec` by the checks.
#[derive(Clone, Default)]
pub struct ObjectRef {
    pub bucket: String,
    pub key: String,
    pub region: String,
}

impl fmt::Debug for ObjectRef {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("ObjectRef(<redacted>)")
    }
}

pub fn parse_object(object: Option<&ObjectRef>) -> Result<S3ObjectSpec, TransferError> {
    let object = object.ok_or(RequestRejection::MissingObject)?;
    Ok(S3ObjectSpec::parse(
        &object.bucket,
        &object.key,
        &object.region,
    )?)
}

/// `expires_at` in the future and at most the S3 presign ceiling (plus the
/// skew allowance) away.
pub fn check_expiry(expires_at_unix_ms: i64, now_unix_ms: i64) -> Result<(), RequestRejection> {
    let ceiling_ms = i64::try_from(TRANSFER_PRESIGN_MAX_SECONDS * 1_000)
        .unwrap_or(i64::MAX)
        .saturating_add(CLOCK_SKEW_ALLOWANCE_MS);
    let latest = now_unix_ms.saturating_add(ceiling_ms);
    if expires_at_unix_ms > now_unix_ms && expires_at_unix_ms <= latest {
        Ok(())
    } else {
        Err(RequestRejection::Expiry)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const NOW: i64 = 1_790_000_000_000;

    #[test]
    fn expiry_must_be_future_and_within_seven_days_plus_skew() {
        assert_eq!(check_expiry(NOW + 1, NOW), Ok(()));
        assert_eq!(check_expiry(NOW, NOW), Err(RequestRejection::Expiry));
        assert_eq!(check_expiry(NOW - 1, NOW), Err(RequestRejection::Expiry));
        let ceiling = NOW + 604_800_000 + 300_000;
        assert_eq!(check_expiry(ceiling, NOW), Ok(()));
        assert_eq!(
            check_expiry(ceiling + 1, NOW),
            Err(RequestRejection::Expiry)
        );
    }

    #[test]
    fn urls_and_objects_never_show_in_debug() {
        let url = PresignedUrl::new("https://h/k?X-Amz-Signature=s".to_owned(), Vec::new());
        assert_eq!(format!("{url:?}"), "PresignedUrl(<redacted>)");
        let object = ObjectRef {
            bucket: "amzn-s3-demo-bucket".to_owned(),
            ..ObjectRef::default()
        };
        assert!(!format!("{object:?}").contains("amzn"));
        assert!(matches!(
            parse_object(None),
            Err(TransferError::Request(RequestRejection::MissingObject))
        ));
    }
}
