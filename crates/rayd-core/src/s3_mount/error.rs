//! Closed set of reasons an S3 mount is not `Mounted` (API contract of
//! §7.2 in the M15 architecture: `MountException`/`MountError` of both
//! SDKs carry the same `code`). Never an AWS message, a bucket name or a
//! path: `as_str()` is the only thing that crosses the wire
//! (`S3MountState.error_class`) or a log line.

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum MountErrorClass {
    /// The FUSE attach or the `mount-s3` daemon could not reach S3 (DNS,
    /// connect, or a timed-out request once mounted).
    Network,
    /// The execution role denied the operation (`AccessDenied` from S3).
    IamDenied,
    /// The bucket or prefix does not exist.
    NotFound,
    /// The bucket is not on the image's `RAYITO_ALLOWED_MOUNT_BUCKETS`
    /// allowlist: rejected before any FUSE or process call.
    NotAllowed,
    /// The mount path's own shape is invalid (not absolute/canonical, not
    /// under an allowed root) or it overlaps another path in the same
    /// request, or — found only at attach time — one of its components is
    /// a symlink or not a directory — distinct from `NotAllowed` (a
    /// bucket-allowlist decision) so a caller never confuses the two.
    InvalidPath,
    /// `/dev/fuse` or the `mount-s3` binary is missing from the image.
    HelperMissing,
    /// The attach or the daemon did not come up before rayd gave up
    /// waiting on it.
    Timeout,
}

impl MountErrorClass {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Network => "network",
            Self::IamDenied => "iam_denied",
            Self::NotFound => "not_found",
            Self::NotAllowed => "not_allowed",
            Self::InvalidPath => "invalid_path",
            Self::HelperMissing => "helper_missing",
            Self::Timeout => "timeout",
        }
    }
}

impl std::fmt::Display for MountErrorClass {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_class_renders_the_closed_snake_case_string_the_api_documents() {
        let cases = [
            (MountErrorClass::Network, "network"),
            (MountErrorClass::IamDenied, "iam_denied"),
            (MountErrorClass::NotFound, "not_found"),
            (MountErrorClass::NotAllowed, "not_allowed"),
            (MountErrorClass::InvalidPath, "invalid_path"),
            (MountErrorClass::HelperMissing, "helper_missing"),
            (MountErrorClass::Timeout, "timeout"),
        ];
        for (class, expected) in cases {
            assert_eq!(class.as_str(), expected);
            assert_eq!(class.to_string(), expected);
        }
    }
}
