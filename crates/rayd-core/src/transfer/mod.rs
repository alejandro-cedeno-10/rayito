//! Presigned S3 transfers (ADR-010), the pure half: `rayd` holds no
//! credentials and only moves bytes to and from URLs the SDK signed with
//! the caller's. This module owns the SSRF guard over those URLs
//! (`url_policy`), the request checks, the poll schedule of an armed
//! import, the S3 error document, how a probe or a `PUT` answer is
//! classified, the admission checks before a byte is written, the part
//! plan of an export, the registry of transfers with its bounds and
//! retention, the selection of the read-after-upload barrier, and the
//! `SignedHttp` port the adapter implements. No HTTP, TLS or executor type
//! lives here.

pub mod barrier;
pub mod error;
pub mod export;
#[cfg(test)]
pub(crate) mod fake;
pub mod import;
pub mod poll;
pub mod ports;
pub mod registry;
pub mod request;
pub mod s3_error;
pub mod url_policy;

use std::time::Duration;

pub use barrier::{BarrierOutcome, BarrierQuery, BarrierTicket, is_component_ancestor, select};
pub use error::{
    FailureCode, FailureReason, RequestRejection, TransferError, TransferFailure, UnaryKind,
};
pub use export::{
    ExportInput, ExportRequest, ExportTarget, PartRange, PutOutcome, classify_put,
    classify_put_error, plan_parts, put_part,
};
pub use import::{
    DeleteOutcome, ImportInput, ImportPlan, ImportRequest, ProbeOutcome, ProbeResponse,
    attempts_exhausted, classify_probe, classify_probe_error, delete_object, one_shot_verdict,
    probe, verify_checksum,
};
pub use poll::{PollSchedule, PollStep, is_expired, time_left, unix_millis};
pub use ports::{
    HttpError, HttpErrorKind, HttpHead, HttpMethod, NoBody, RequestBody, ResponseBody, SignedHttp,
    SignedRequest,
};
pub use registry::{
    CancelOutcome, DoneOutcome, NewTransfer, RegistryError, RegistryLimits, TransferId,
    TransferPhase, TransferRegistry, TransferSnapshot,
};
pub use request::{CLOCK_SKEW_ALLOWANCE_MS, ObjectRef, PresignedUrl, check_expiry};
pub use s3_error::{REQUEST_EXPIRED_MESSAGE, ResponseLog, S3Error, read_s3_error};
pub use url_policy::{
    OCTET_STREAM, RequestUrl, S3ObjectSpec, TransferDirection, UrlPolicyError, UrlRole,
    is_forbidden_address, validate_request,
};

/// S3's ceiling on `X-Amz-Expires` (`AWS_API_NOTES.md` Q62).
pub const TRANSFER_PRESIGN_MAX_SECONDS: u64 = 604_800;
/// The largest object one `PUT` may store.
pub const TRANSFER_SINGLE_PUT_MAX_BYTES: u64 = 5 * 1024 * 1024 * 1024;
/// Parts one `StartExportRequest` may carry: 1 000 URLs of about 1.6 KB
/// stay under tonic's 4 MiB decoding limit (design D22).
pub const TRANSFER_MAX_PARTS: usize = 1_000;
/// S3's bounds on the size of an `UploadPart` (the last part may be
/// smaller than the minimum).
pub const S3_PART_MIN_BYTES: u64 = 5 * 1024 * 1024;
pub const S3_PART_MAX_BYTES: u64 = 5 * 1024 * 1024 * 1024;
/// Transfers waiting or running at once per sandbox.
pub const TRANSFER_MAX_ACTIVE: usize = 16;
/// Transfers moving bytes at once (the adapter's permits).
pub const TRANSFER_MAX_RUNNING: usize = 2;
/// Finished transfers kept for `GetTransfer`/`WatchTransfer`.
pub const TRANSFER_RETAINED_MAX: usize = 64;
pub const TRANSFER_RETAINED: Duration = Duration::from_mins(30);
/// One budget per barrier call for the probes it asks for.
pub const TRANSFER_PROBE_BUDGET: Duration = Duration::from_secs(2);
pub const TRANSFER_POLL_FAST_INTERVAL: Duration = Duration::from_secs(1);
pub const TRANSFER_POLL_SLOW_INTERVAL: Duration = Duration::from_secs(5);
pub const TRANSFER_POLL_FAST_WINDOW: Duration = Duration::from_mins(10);
pub const TRANSFER_URL_MAX_BYTES: usize = 8_192;
pub const TRANSFER_CONNECT_TIMEOUT: Duration = Duration::from_secs(5);
pub const TRANSFER_IDLE_TIMEOUT: Duration = Duration::from_secs(30);
/// Interrupted byte moves an import or an export survives: the third one
/// ends it `unavailable`.
pub const TRANSFER_ATTEMPTS: u32 = 3;
/// Retries of one request after a 5xx or a connection failure (the
/// single `GET` of an import without wait, each part of an export, the
/// cleanup `DELETE` gets one).
pub const TRANSFER_REQUEST_RETRIES: u32 = 2;
pub const TRANSFER_RETRY_DELAY: Duration = Duration::from_secs(1);
/// Export reads: 1 MiB chunks, four in flight.
pub const EXPORT_CHUNK_BYTES: usize = 1024 * 1024;
pub const EXPORT_CHUNK_QUEUE: usize = 4;
/// `WatchTransfer` sends progress at most this often.
pub const TRANSFER_PROGRESS_INTERVAL: Duration = Duration::from_secs(1);

#[cfg(test)]
mod tests {
    use super::*;
    use crate::filesystem::{METADATA_MAX_BYTES, METADATA_MAX_KEYS, METADATA_XATTR_PREFIX};

    fn millis(duration: Duration) -> u64 {
        u64::try_from(duration.as_millis()).unwrap()
    }

    #[test]
    fn transfer_limits_match_limits_json() {
        let limits: serde_json::Value =
            serde_json::from_str(include_str!("../../../../limits.json")).unwrap();
        let key = |name: &str| limits[name].as_u64().unwrap();
        let size = |name: &str| usize::try_from(key(name)).unwrap();
        assert_eq!(
            key("transferPresignMaxSeconds"),
            TRANSFER_PRESIGN_MAX_SECONDS
        );
        assert_eq!(
            key("transferSinglePutMaxBytes"),
            TRANSFER_SINGLE_PUT_MAX_BYTES
        );
        assert_eq!(size("transferMaxParts"), TRANSFER_MAX_PARTS);
        assert_eq!(size("transferMaxActive"), TRANSFER_MAX_ACTIVE);
        assert_eq!(size("transferMaxRunning"), TRANSFER_MAX_RUNNING);
        assert_eq!(size("transferRetainedMax"), TRANSFER_RETAINED_MAX);
        assert_eq!(key("transferRetainedSeconds"), TRANSFER_RETAINED.as_secs());
        assert_eq!(key("transferProbeBudgetMs"), millis(TRANSFER_PROBE_BUDGET));
        assert_eq!(
            key("transferPollFastIntervalMs"),
            millis(TRANSFER_POLL_FAST_INTERVAL)
        );
        assert_eq!(
            key("transferPollSlowIntervalMs"),
            millis(TRANSFER_POLL_SLOW_INTERVAL)
        );
        assert_eq!(
            key("transferPollFastWindowSeconds"),
            TRANSFER_POLL_FAST_WINDOW.as_secs()
        );
        assert_eq!(size("transferUrlMaxBytes"), TRANSFER_URL_MAX_BYTES);
        assert_eq!(
            key("transferConnectTimeoutMs"),
            millis(TRANSFER_CONNECT_TIMEOUT)
        );
        assert_eq!(key("transferIdleTimeoutMs"), millis(TRANSFER_IDLE_TIMEOUT));
        assert_eq!(size("metadataMaxBytes"), METADATA_MAX_BYTES);
        assert_eq!(size("metadataMaxKeys"), METADATA_MAX_KEYS);
        assert_eq!(
            limits["metadataXattrPrefix"].as_str().unwrap(),
            METADATA_XATTR_PREFIX
        );
    }

    #[test]
    fn part_bounds_contain_the_sdk_part_size() {
        let limits: serde_json::Value =
            serde_json::from_str(include_str!("../../../../limits.json")).unwrap();
        let sdk_part = limits["transferPartSizeMinBytes"].as_u64().unwrap();
        assert!((S3_PART_MIN_BYTES..=S3_PART_MAX_BYTES).contains(&sdk_part));
        assert_eq!(S3_PART_MAX_BYTES, TRANSFER_SINGLE_PUT_MAX_BYTES);
    }
}
