//! The wire tokens: the status messages, stream error codes and message
//! prefixes that the SDKs (0.3.x and 0.4.0) parse byte for byte. Every
//! other agent message is free Spanish text; these stay frozen, and the
//! error types and adapters take them from here instead of repeating the
//! literal. The golden test below pins each value.

/// Hook phases that close streams and gate new ones (`is_phase_gate`).
pub const SUSPENDING: &str = "suspending";
pub const TERMINATING: &str = "terminating";

/// The logical sandbox deadline (ADR-011).
pub const SANDBOX_TIMEOUT: &str = "sandbox_timeout";
pub const LIFECYCLE_UNMANAGED: &str = "lifecycle_unmanaged";
/// `SetTimeout` beyond the cap: `"<prefix>; cap_unix_ms=<n>"`.
pub const BEYOND_CAP_PREFIX: &str = "timeout beyond cap";
pub const CAP_UNIX_MS: &str = "cap_unix_ms";

/// The kernel gate: `"<prefix>: <reason>"`.
pub const KERNEL_NOT_READY_PREFIX: &str = "kernel not ready";

/// Filesystem status details.
pub const DISK_RESERVE: &str = "disk_reserve";
pub const DISK_FULL: &str = "disk_full";
pub const METADATA_UNSUPPORTED: &str = "metadata_unsupported";
pub const METADATA_TOO_LARGE: &str = "metadata_too_large";

/// Egress failures of `/run` and `/resume`: `"<token>: <step>"` and the
/// bare verification token.
pub const EGRESS_UPDATE_FAILED: &str = "egress_update_failed";
pub const EGRESS_VERIFY_FAILED: &str = "egress_verify_failed";

/// The `"<reason>:"` tokens that open every transfer failure message
/// (design D13); the SDK splits the message at the first `:`.
pub mod transfer {
    pub const TOO_LARGE_FOR_PUT: &str = "too_large_for_put";
    pub const FILE_CHANGED: &str = "file_changed";
    pub const EXPIRED: &str = "expired";
    pub const NO_OBJECT: &str = "no_object";
    pub const ACCESS_DENIED: &str = "access_denied";
    pub const SIGNATURE_REJECTED: &str = "signature_rejected";
    pub const WRONG_REGION: &str = "wrong_region";
    pub const BUCKET_MISSING: &str = "bucket_missing";
    pub const S3_UNAVAILABLE: &str = "s3_unavailable";
    pub const FORBIDDEN_ADDRESS: &str = "forbidden_address";
    pub const UNEXPECTED_RESPONSE: &str = "unexpected_response";
    pub const NO_CONTENT_LENGTH: &str = "no_content_length";
    pub const TOO_LARGE: &str = "too_large";
    pub const CHECKSUM_MISMATCH: &str = "checksum_mismatch";
    pub const FILE_SHRANK: &str = "file_shrank";
    pub const CANCELLED: &str = "cancelled";
    pub const DESTINATION_REJECTED: &str = "destination_rejected";
    pub use super::{DISK_FULL, DISK_RESERVE, METADATA_TOO_LARGE, METADATA_UNSUPPORTED};
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::code::CodeError;
    use crate::filesystem::FilesystemError;
    use crate::lifecycle::HookPhase;
    use crate::network::NetworkError;
    use crate::persistence::PersistenceError;
    use crate::process::{EndStatus, ProcessEnd};
    use crate::sandbox_timeout::SandboxTimeoutError;
    use crate::transfer::{FailureReason, TransferError, TransferFailure};

    /// The exact bytes the SDKs parse; changing one breaks every released
    /// client.
    #[test]
    fn frozen_tokens_keep_their_bytes() {
        let frozen = [
            (SUSPENDING, "suspending"),
            (TERMINATING, "terminating"),
            (SANDBOX_TIMEOUT, "sandbox_timeout"),
            (LIFECYCLE_UNMANAGED, "lifecycle_unmanaged"),
            (BEYOND_CAP_PREFIX, "timeout beyond cap"),
            (CAP_UNIX_MS, "cap_unix_ms"),
            (KERNEL_NOT_READY_PREFIX, "kernel not ready"),
            (DISK_RESERVE, "disk_reserve"),
            (DISK_FULL, "disk_full"),
            (METADATA_UNSUPPORTED, "metadata_unsupported"),
            (METADATA_TOO_LARGE, "metadata_too_large"),
            (EGRESS_UPDATE_FAILED, "egress_update_failed"),
            (EGRESS_VERIFY_FAILED, "egress_verify_failed"),
            (transfer::TOO_LARGE_FOR_PUT, "too_large_for_put"),
            (transfer::FILE_CHANGED, "file_changed"),
        ];
        for (token, bytes) in frozen {
            assert_eq!(token, bytes);
        }
    }

    #[test]
    fn domain_messages_carry_the_frozen_tokens() {
        assert_eq!(
            SandboxTimeoutError::BeyondCap { cap_unix_ms: 42 }.to_string(),
            "timeout beyond cap; cap_unix_ms=42"
        );
        assert_eq!(
            SandboxTimeoutError::Unmanaged.to_string(),
            "lifecycle_unmanaged"
        );
        assert_eq!(SandboxTimeoutError::Expired.to_string(), "sandbox_timeout");
        let kernel = CodeError::KernelNotReady {
            reason: "warming".to_owned(),
        };
        assert_eq!(kernel.to_string(), "kernel not ready: warming");
        assert!(
            CodeError::SidecarUnavailable
                .to_string()
                .starts_with("kernel not ready: ")
        );
        assert_eq!(HookPhase::Suspending.to_string(), "suspending");
        assert_eq!(HookPhase::Terminating.to_string(), "terminating");
        assert_eq!(EndStatus::Suspending.as_str(), "suspending");
        assert_eq!(EndStatus::SandboxTimeout.as_str(), "sandbox_timeout");
        let suspended = ProcessEnd::suspending().error.unwrap();
        assert_eq!(suspended.code, "suspending");
        assert_eq!(PersistenceError::Suspending.to_string(), "suspending");
        assert_eq!(FilesystemError::DiskReserve.to_string(), "disk_reserve");
        assert_eq!(FilesystemError::DiskFull.to_string(), "disk_full");
        assert_eq!(
            FilesystemError::MetadataUnsupported.to_string(),
            "metadata_unsupported"
        );
        assert_eq!(
            FilesystemError::MetadataTooLarge.to_string(),
            "metadata_too_large"
        );
        assert_eq!(
            NetworkError::InstallFailed { step: "nft" }.to_string(),
            "egress_update_failed: nft"
        );
        assert_eq!(
            NetworkError::VerifyFailed.to_string(),
            "egress_verify_failed"
        );
        assert!(
            TransferError::TooLargeForPut
                .to_string()
                .starts_with("too_large_for_put: ")
        );
        assert!(
            TransferError::FileChanged
                .to_string()
                .starts_with("file_changed: ")
        );
    }

    #[test]
    fn transfer_reasons_keep_their_tokens() {
        let reasons = [
            (FailureReason::Expired, "expired"),
            (FailureReason::NoObject, "no_object"),
            (FailureReason::AccessDenied, "access_denied"),
            (FailureReason::SignatureRejected, "signature_rejected"),
            (FailureReason::WrongRegion, "wrong_region"),
            (FailureReason::BucketMissing, "bucket_missing"),
            (FailureReason::S3Unavailable, "s3_unavailable"),
            (FailureReason::ForbiddenAddress, "forbidden_address"),
            (FailureReason::UnexpectedResponse, "unexpected_response"),
            (FailureReason::NoContentLength, "no_content_length"),
            (FailureReason::TooLarge, "too_large"),
            (FailureReason::DiskReserve, "disk_reserve"),
            (FailureReason::DiskFull, "disk_full"),
            (FailureReason::ChecksumMismatch, "checksum_mismatch"),
            (FailureReason::FileShrank, "file_shrank"),
            (FailureReason::Cancelled, "cancelled"),
            (FailureReason::DestinationRejected, "destination_rejected"),
            (FailureReason::MetadataUnsupported, "metadata_unsupported"),
            (FailureReason::MetadataTooLarge, "metadata_too_large"),
        ];
        for (reason, token) in reasons {
            assert_eq!(reason.token(), token);
            assert!(
                TransferFailure::of(reason)
                    .to_string()
                    .starts_with(&format!("{token}: "))
            );
        }
    }
}
