//! An import (design D9): the request checks that run before any network
//! I/O, how each answer of the presigned `GET` is classified for an armed
//! ticket and for a one-shot import, the admission checks that run before
//! a byte is written, the checksum and attempt rules, and the two requests
//! an import makes through the `SignedHttp` port (the probe and the
//! cleanup `DELETE`).

use std::fmt;

use super::error::{FailureReason, RequestRejection, TransferError, TransferFailure};
use super::ports::{HttpErrorKind, HttpHead, HttpMethod, NoBody, SignedHttp, SignedRequest};
use super::request::{ObjectRef, PresignedUrl, check_expiry, parse_object};
use super::s3_error::{ResponseLog, S3Error, read_s3_error};
use super::url_policy::{TransferDirection, UrlRole, validate_request};
use super::{TRANSFER_ATTEMPTS, TRANSFER_REQUEST_RETRIES};
use crate::filesystem::{DEFAULT_FILE_MODE, DISK_RESERVE_BYTES, FileMetadata, MODE_MASK};

const SHA256_HEX_LEN: usize = 64;

/// `StartImportRequest` once the transport is gone.
pub struct ImportInput {
    pub path: String,
    pub user: Option<String>,
    pub mode: Option<u32>,
    pub object: Option<ObjectRef>,
    pub get: Option<PresignedUrl>,
    pub delete: Option<PresignedUrl>,
    pub wait_for_object: bool,
    pub expires_at_unix_ms: i64,
    pub max_bytes: u64,
    pub expected_sha256: String,
    pub metadata: Vec<(String, String)>,
}

/// A checked import: its URLs passed the policy, its fields the rules.
pub struct ImportRequest {
    pub path: String,
    pub user: Option<String>,
    pub mode: u32,
    pub get: PresignedUrl,
    pub delete: Option<PresignedUrl>,
    pub wait_for_object: bool,
    pub expires_at_unix_ms: i64,
    pub max_bytes: u64,
    pub expected_sha256: Option<String>,
    pub metadata: FileMetadata,
}

impl fmt::Debug for ImportRequest {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("ImportRequest")
            .field("wait_for_object", &self.wait_for_object)
            .field("max_bytes", &self.max_bytes)
            .field("metadata_keys", &self.metadata.len())
            .finish_non_exhaustive()
    }
}

impl ImportRequest {
    /// The URL policy (rules 1 to 7 of design D6), then the non-URL checks;
    /// nothing here touches the network or the filesystem.
    pub fn validate(
        input: ImportInput,
        sandbox_id: &str,
        now_unix_ms: i64,
    ) -> Result<Self, TransferError> {
        let object = parse_object(input.object.as_ref())?;
        let get = input.get.ok_or(RequestRejection::MissingUrl)?;
        {
            let mut urls = vec![get.as_request(UrlRole::Get)];
            if let Some(delete) = &input.delete {
                urls.push(delete.as_request(UrlRole::Delete));
            }
            validate_request(&object, &urls, sandbox_id, TransferDirection::Import)?;
        }
        check_expiry(input.expires_at_unix_ms, now_unix_ms)?;
        let mode = input.mode.unwrap_or(DEFAULT_FILE_MODE);
        if mode > MODE_MASK {
            return Err(RequestRejection::Mode.into());
        }
        let expected_sha256 = parse_sha256(&input.expected_sha256)?;
        let metadata =
            FileMetadata::parse(input.metadata).map_err(|_| RequestRejection::Metadata)?;
        Ok(Self {
            path: input.path,
            user: input.user,
            mode,
            get,
            delete: input.delete,
            wait_for_object: input.wait_for_object,
            expires_at_unix_ms: input.expires_at_unix_ms,
            max_bytes: input.max_bytes,
            expected_sha256,
            metadata,
        })
    }
}

/// Empty means "not checked"; otherwise 64 lowercase hex characters.
fn parse_sha256(raw: &str) -> Result<Option<String>, RequestRejection> {
    if raw.is_empty() {
        return Ok(None);
    }
    let valid = raw.len() == SHA256_HEX_LEN
        && raw
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte));
    if valid {
        Ok(Some(raw.to_owned()))
    } else {
        Err(RequestRejection::Sha256)
    }
}

/// What one presigned `GET` said.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProbeOutcome {
    /// 200: the object is there.
    Found,
    /// Not there yet (armed tickets keep polling).
    Pending,
    /// S3 or the network failed in a way worth retrying.
    Transient,
    Failed(TransferFailure),
}

/// The classification table of design D9 for an answer that arrived.
#[must_use]
pub fn classify_probe(status: u16, error: &S3Error, wait_for_object: bool) -> ProbeOutcome {
    if status == 200 {
        return ProbeOutcome::Found;
    }
    if let Some(failure) = error.terminal_failure(status) {
        return ProbeOutcome::Failed(failure);
    }
    if error.is_transient(status) {
        return ProbeOutcome::Transient;
    }
    let missing = match status {
        404 => FailureReason::NoObject,
        403 if error.code.is_none() || error.is("AccessDenied") => FailureReason::AccessDenied,
        _ => return ProbeOutcome::Failed(TransferFailure::of(FailureReason::UnexpectedResponse)),
    };
    if wait_for_object {
        ProbeOutcome::Pending
    } else {
        ProbeOutcome::Failed(TransferFailure::of(missing))
    }
}

/// The same table for a request that never got an answer.
#[must_use]
pub fn classify_probe_error(kind: HttpErrorKind) -> ProbeOutcome {
    match kind {
        HttpErrorKind::ForbiddenAddress => {
            ProbeOutcome::Failed(TransferFailure::of(FailureReason::ForbiddenAddress))
        }
        HttpErrorKind::Redirect => {
            ProbeOutcome::Failed(TransferFailure::of(FailureReason::WrongRegion))
        }
        HttpErrorKind::Connect
        | HttpErrorKind::Tls
        | HttpErrorKind::Timeout
        | HttpErrorKind::Io => ProbeOutcome::Transient,
    }
}

/// A one-shot import retries a transient answer twice, then gives up.
#[must_use]
pub fn one_shot_verdict(outcome: ProbeOutcome, retries_done: u32) -> ProbeOutcome {
    match outcome {
        ProbeOutcome::Transient if retries_done >= TRANSFER_REQUEST_RETRIES => {
            ProbeOutcome::Failed(TransferFailure::of(FailureReason::S3Unavailable))
        }
        other => other,
    }
}

/// The checks between "the object is there" and the first byte written.
pub struct ImportPlan;

impl ImportPlan {
    /// The size to write, or why the object is refused: no
    /// `Content-Length`, above `max_bytes` (0 = no cap of its own), or not
    /// fitting the free space minus the disk reserve.
    pub fn admit(
        content_length: Option<u64>,
        max_bytes: u64,
        free_bytes: u64,
    ) -> Result<u64, TransferFailure> {
        let length = content_length.ok_or(TransferFailure::of(FailureReason::NoContentLength))?;
        if max_bytes > 0 && length > max_bytes {
            return Err(TransferFailure::of(FailureReason::TooLarge));
        }
        let fits = DISK_RESERVE_BYTES
            .checked_add(length)
            .is_some_and(|needed| free_bytes >= needed);
        if !fits {
            return Err(TransferFailure::of(FailureReason::DiskReserve));
        }
        Ok(length)
    }
}

/// `expected` is the lowercase hex the SDK computed, when it did.
pub fn verify_checksum(expected: Option<&str>, actual_hex: &str) -> Result<(), TransferFailure> {
    match expected {
        Some(expected) if expected != actual_hex => {
            Err(TransferFailure::of(FailureReason::ChecksumMismatch))
        }
        _ => Ok(()),
    }
}

/// The third interrupted byte move ends the transfer.
#[must_use]
pub fn attempts_exhausted(failed_attempts: u32) -> bool {
    failed_attempts >= TRANSFER_ATTEMPTS
}

/// A probe that found the object hands over the open response.
pub enum ProbeResponse<B> {
    Found { head: HttpHead, body: B },
    Answer(ProbeOutcome),
}

/// One presigned `GET`: the body is left unread when the object is there
/// (the caller decides whether it may move bytes now), and read up to the
/// S3 error cap otherwise.
pub async fn probe<H: SignedHttp>(
    http: &H,
    get: &PresignedUrl,
    wait_for_object: bool,
) -> (ProbeResponse<H::Body>, ResponseLog) {
    let request = SignedRequest {
        method: HttpMethod::Get,
        url: get.url.clone(),
        content_type: None,
        content_length: None,
    };
    match http.send::<NoBody>(request, None).await {
        Err(error) => (
            ProbeResponse::Answer(classify_probe_error(error.kind)),
            ResponseLog::default(),
        ),
        Ok((head, body)) if head.status == 200 => {
            let log = ResponseLog::of(&head, None);
            (ProbeResponse::Found { head, body }, log)
        }
        Ok((head, mut body)) => {
            let error = read_s3_error(&head, &mut body).await;
            let outcome = classify_probe(head.status, &error, wait_for_object);
            (
                ProbeResponse::Answer(outcome),
                ResponseLog::of(&head, Some(&error)),
            )
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DeleteOutcome {
    Deleted,
    Transient,
    Failed,
}

/// The cleanup `DELETE` of an observed object; its outcome is logged and
/// never changes the transfer's result.
pub async fn delete_object<H: SignedHttp>(
    http: &H,
    delete: &PresignedUrl,
) -> (DeleteOutcome, ResponseLog) {
    let request = SignedRequest {
        method: HttpMethod::Delete,
        url: delete.url.clone(),
        content_type: None,
        content_length: None,
    };
    match http.send::<NoBody>(request, None).await {
        Err(error) => {
            let outcome = match error.kind {
                HttpErrorKind::Connect
                | HttpErrorKind::Tls
                | HttpErrorKind::Timeout
                | HttpErrorKind::Io => DeleteOutcome::Transient,
                HttpErrorKind::ForbiddenAddress | HttpErrorKind::Redirect => DeleteOutcome::Failed,
            };
            (outcome, ResponseLog::default())
        }
        Ok((head, _)) if (200..300).contains(&head.status) => {
            (DeleteOutcome::Deleted, ResponseLog::of(&head, None))
        }
        Ok((head, mut body)) => {
            let error = read_s3_error(&head, &mut body).await;
            let outcome = if error.is_transient(head.status) {
                DeleteOutcome::Transient
            } else {
                DeleteOutcome::Failed
            };
            (outcome, ResponseLog::of(&head, Some(&error)))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::transfer::error::FailureCode;
    use crate::transfer::fake::{FakeHttp, FakeResponse, block_on};
    use crate::transfer::url_policy::UrlPolicyError;

    const SANDBOX: &str = "microvm-0abc";
    const NOW: i64 = 1_790_000_000_000;
    const QUERY: &str = "X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=c&X-Amz-Date=d&X-Amz-Expires=60&X-Amz-SignedHeaders=host&X-Amz-Signature=s";

    fn key() -> String {
        format!(
            "rayito-transfer/{SANDBOX}/up/{}",
            "0123456789abcdef".repeat(2)
        )
    }

    fn url_for(key: &str) -> String {
        format!("https://amzn-s3-demo-bucket.s3.us-east-1.amazonaws.com/{key}?{QUERY}")
    }

    fn input() -> ImportInput {
        ImportInput {
            path: "/home/user/in.bin".to_owned(),
            user: None,
            mode: None,
            object: Some(ObjectRef {
                bucket: "amzn-s3-demo-bucket".to_owned(),
                key: key(),
                region: "us-east-1".to_owned(),
            }),
            get: Some(PresignedUrl::new(url_for(&key()), Vec::new())),
            delete: Some(PresignedUrl::new(url_for(&key()), Vec::new())),
            wait_for_object: true,
            expires_at_unix_ms: NOW + 60_000,
            max_bytes: 0,
            expected_sha256: String::new(),
            metadata: Vec::new(),
        }
    }

    fn rejection(input: ImportInput) -> TransferError {
        ImportRequest::validate(input, SANDBOX, NOW).unwrap_err()
    }

    fn error(code: &str, message: &str) -> S3Error {
        S3Error::parse(
            format!("<Error><Code>{code}</Code><Message>{message}</Message></Error>").as_bytes(),
            None,
        )
    }

    fn failed(reason: FailureReason) -> ProbeOutcome {
        ProbeOutcome::Failed(TransferFailure::of(reason))
    }

    #[test]
    fn a_valid_request_keeps_its_fields_and_defaults_the_mode() {
        let mut raw = input();
        raw.expected_sha256 = "a".repeat(64);
        raw.metadata = vec![("Owner".to_owned(), "alice".to_owned())];
        let request = ImportRequest::validate(raw, SANDBOX, NOW).unwrap();
        assert_eq!(request.mode, 0o644);
        assert_eq!(
            request.expected_sha256.as_deref(),
            Some("a".repeat(64).as_str())
        );
        assert_eq!(
            request.metadata.iter().collect::<Vec<_>>(),
            vec![("owner", "alice")]
        );
        assert!(request.delete.is_some());
        assert!(!format!("{request:?}").contains("amazonaws"));
    }

    #[test]
    fn the_url_policy_runs_before_the_field_checks() {
        let mut raw = input();
        raw.get = Some(PresignedUrl::new(
            url_for(&key()).replace(
                "https://amzn-s3-demo-bucket.s3.us-east-1.amazonaws.com",
                "https://169.254.169.254",
            ),
            Vec::new(),
        ));
        raw.mode = Some(0o17777);
        assert_eq!(
            rejection(raw),
            TransferError::Policy(UrlPolicyError::IpLiteral)
        );
        let mut raw = input();
        raw.get = None;
        assert_eq!(
            rejection(raw),
            TransferError::Request(RequestRejection::MissingUrl)
        );
        let mut raw = input();
        raw.object = None;
        assert_eq!(
            rejection(raw),
            TransferError::Request(RequestRejection::MissingObject)
        );
    }

    #[test]
    fn field_rules_refuse_bad_modes_hashes_metadata_and_expiry() {
        let mut raw = input();
        raw.mode = Some(0o10000);
        assert_eq!(
            rejection(raw),
            TransferError::Request(RequestRejection::Mode)
        );
        for bad in ["A".repeat(64), "a".repeat(63), "g".repeat(64)] {
            let mut raw = input();
            raw.expected_sha256 = bad;
            assert_eq!(
                rejection(raw),
                TransferError::Request(RequestRejection::Sha256)
            );
        }
        let mut raw = input();
        raw.metadata = vec![("a b".to_owned(), "x".to_owned())];
        assert_eq!(
            rejection(raw),
            TransferError::Request(RequestRejection::Metadata)
        );
        let mut raw = input();
        raw.expires_at_unix_ms = NOW - 1;
        assert_eq!(
            rejection(raw),
            TransferError::Request(RequestRejection::Expiry)
        );
    }

    /// Design D9, one row per answer: status, S3 code and message, then
    /// the verdict for an armed ticket and for a one-shot import.
    const CLASSIFICATION: [(u16, &str, &str, &str, &str); 15] = [
        (200, "", "", "found", "found"),
        (404, "NoSuchKey", "x", "pending", "no_object"),
        (
            403,
            "AccessDenied",
            "Request has expired",
            "expired",
            "expired",
        ),
        (400, "ExpiredToken", "x", "expired", "expired"),
        (
            403,
            "AccessDenied",
            "Access Denied",
            "pending",
            "access_denied",
        ),
        (403, "", "", "pending", "access_denied"),
        (
            403,
            "SignatureDoesNotMatch",
            "x",
            "signature_rejected",
            "signature_rejected",
        ),
        (
            400,
            "AuthorizationQueryParametersError",
            "x",
            "signature_rejected",
            "signature_rejected",
        ),
        (
            301,
            "PermanentRedirect",
            "x",
            "wrong_region",
            "wrong_region",
        ),
        (307, "", "", "wrong_region", "wrong_region"),
        (404, "NoSuchBucket", "x", "bucket_missing", "bucket_missing"),
        (500, "", "", "transient", "transient"),
        (503, "SlowDown", "x", "transient", "transient"),
        (
            403,
            "InvalidAccessKeyId",
            "x",
            "unexpected_response",
            "unexpected_response",
        ),
        (418, "", "", "unexpected_response", "unexpected_response"),
    ];

    fn verdict_token(outcome: ProbeOutcome) -> &'static str {
        match outcome {
            ProbeOutcome::Found => "found",
            ProbeOutcome::Pending => "pending",
            ProbeOutcome::Transient => "transient",
            ProbeOutcome::Failed(failure) => failure.reason.token(),
        }
    }

    #[test]
    fn the_classification_table_of_armed_and_one_shot_imports() {
        for (status, code, message, armed, one_shot) in CLASSIFICATION {
            let s3 = if code.is_empty() {
                S3Error::default()
            } else {
                error(code, message)
            };
            let row = format!("{status} {code} {message}");
            assert_eq!(
                verdict_token(classify_probe(status, &s3, true)),
                armed,
                "{row}"
            );
            assert_eq!(
                verdict_token(classify_probe(status, &s3, false)),
                one_shot,
                "{row}"
            );
        }
        let no_object = TransferFailure::of(FailureReason::NoObject);
        assert_eq!(no_object.code, FailureCode::NotFound);
    }

    #[test]
    fn transport_failures_retry_except_forbidden_addresses_and_redirects() {
        assert_eq!(
            classify_probe_error(HttpErrorKind::ForbiddenAddress),
            failed(FailureReason::ForbiddenAddress)
        );
        assert_eq!(
            classify_probe_error(HttpErrorKind::Redirect),
            failed(FailureReason::WrongRegion)
        );
        for kind in [
            HttpErrorKind::Connect,
            HttpErrorKind::Tls,
            HttpErrorKind::Timeout,
            HttpErrorKind::Io,
        ] {
            assert_eq!(classify_probe_error(kind), ProbeOutcome::Transient);
        }
        assert_eq!(
            one_shot_verdict(ProbeOutcome::Transient, 1),
            ProbeOutcome::Transient
        );
        assert_eq!(
            one_shot_verdict(ProbeOutcome::Transient, 2),
            failed(FailureReason::S3Unavailable)
        );
        assert_eq!(
            one_shot_verdict(ProbeOutcome::Found, 9),
            ProbeOutcome::Found
        );
    }

    #[test]
    fn admission_checks_run_before_any_byte_is_written() {
        let plenty = DISK_RESERVE_BYTES + 10_000;
        assert_eq!(ImportPlan::admit(Some(10), 0, plenty), Ok(10));
        assert_eq!(ImportPlan::admit(Some(10), 10, plenty), Ok(10));
        assert_eq!(
            ImportPlan::admit(None, 0, plenty).unwrap_err().reason,
            FailureReason::NoContentLength
        );
        assert_eq!(
            ImportPlan::admit(Some(11), 10, plenty).unwrap_err().reason,
            FailureReason::TooLarge
        );
        assert_eq!(
            ImportPlan::admit(Some(10_001), 0, plenty)
                .unwrap_err()
                .reason,
            FailureReason::DiskReserve
        );
        assert_eq!(
            ImportPlan::admit(Some(u64::MAX), 0, u64::MAX)
                .unwrap_err()
                .reason,
            FailureReason::DiskReserve
        );
    }

    #[test]
    fn checksums_and_attempts() {
        assert_eq!(verify_checksum(None, "ab"), Ok(()));
        assert_eq!(verify_checksum(Some("ab"), "ab"), Ok(()));
        assert_eq!(
            verify_checksum(Some("ab"), "cd").unwrap_err().reason,
            FailureReason::ChecksumMismatch
        );
        assert!(!attempts_exhausted(2));
        assert!(attempts_exhausted(3));
    }

    #[test]
    fn the_probe_sends_a_bare_get_and_leaves_a_found_body_unread() {
        let http = FakeHttp::with(vec![FakeResponse::ok(b"payload")]);
        let get = PresignedUrl::new(url_for(&key()), Vec::new());
        let (response, log) = block_on(probe(&http, &get, true));
        let ProbeResponse::Found { head, body } = response else {
            panic!("expected the object");
        };
        assert_eq!(head.content_length, Some(7));
        assert_eq!(body.remaining(), 1);
        assert_eq!(log.http_status, Some(200));
        let calls = http.calls();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].method, HttpMethod::Get);
        assert_eq!(calls[0].content_type, None);
        assert_eq!(calls[0].content_length, None);
        assert!(calls[0].body.is_empty());
    }

    #[test]
    fn the_probe_classifies_error_documents_and_transport_failures() {
        let http = FakeHttp::with(vec![
            FakeResponse::error(404, "NoSuchKey", "The specified key does not exist."),
            FakeResponse::error(403, "AccessDenied", "Request has expired"),
            FakeResponse::Error(HttpErrorKind::ForbiddenAddress),
        ]);
        let get = PresignedUrl::new(url_for(&key()), Vec::new());
        let (pending, log) = block_on(probe(&http, &get, true));
        assert!(matches!(
            pending,
            ProbeResponse::Answer(ProbeOutcome::Pending)
        ));
        assert_eq!(log.s3_error_code.as_deref(), Some("NoSuchKey"));
        let (expired, _) = block_on(probe(&http, &get, true));
        assert!(matches!(
            expired,
            ProbeResponse::Answer(ProbeOutcome::Failed(TransferFailure {
                reason: FailureReason::Expired,
                ..
            }))
        ));
        let (forbidden, log) = block_on(probe(&http, &get, true));
        assert!(matches!(
            forbidden,
            ProbeResponse::Answer(ProbeOutcome::Failed(TransferFailure {
                reason: FailureReason::ForbiddenAddress,
                ..
            }))
        ));
        assert_eq!(log, ResponseLog::default());
    }

    #[test]
    fn the_cleanup_delete_reports_its_outcome() {
        let http = FakeHttp::with(vec![
            FakeResponse::Status {
                status: 204,
                etag: None,
                content_length: None,
                body: Vec::new(),
            },
            FakeResponse::error(503, "SlowDown", "x"),
            FakeResponse::error(403, "AccessDenied", "Request has expired"),
            FakeResponse::Error(HttpErrorKind::Timeout),
        ]);
        let delete = PresignedUrl::new(url_for(&key()), Vec::new());
        assert_eq!(
            block_on(delete_object(&http, &delete)).0,
            DeleteOutcome::Deleted
        );
        assert_eq!(
            block_on(delete_object(&http, &delete)).0,
            DeleteOutcome::Transient
        );
        assert_eq!(
            block_on(delete_object(&http, &delete)).0,
            DeleteOutcome::Failed
        );
        assert_eq!(
            block_on(delete_object(&http, &delete)).0,
            DeleteOutcome::Transient
        );
        assert!(
            http.calls()
                .iter()
                .all(|call| call.method == HttpMethod::Delete)
        );
    }
}
