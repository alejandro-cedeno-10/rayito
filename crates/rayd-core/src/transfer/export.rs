//! An export (design D10): the request checks, the part plan measured
//! against the size `fstat` gave when the file was opened (a single `PUT`
//! up to 5 GiB, or exactly the parts the SDK presigned), how a `PUT`
//! answer is classified, and the `PUT` itself through the `SignedHttp`
//! port with `Content-Length` and `Content-Type: application/octet-stream`.

use std::fmt;

use super::error::{FailureReason, RequestRejection, TransferError, TransferFailure};
use super::ports::{HttpErrorKind, HttpMethod, RequestBody, SignedHttp, SignedRequest};
use super::request::{ObjectRef, PresignedUrl, check_expiry, parse_object};
use super::s3_error::{ResponseLog, S3Error, read_s3_error};
use super::url_policy::{OCTET_STREAM, TransferDirection, UrlRole, validate_request};
use super::{
    S3_PART_MAX_BYTES, S3_PART_MIN_BYTES, TRANSFER_MAX_PARTS, TRANSFER_SINGLE_PUT_MAX_BYTES,
};

/// Where the bytes go: one `PUT`, or one `UploadPart` URL per part in
/// order (`PartNumber` 1..N).
pub enum ExportTarget {
    Put(PresignedUrl),
    Multipart {
        part_size: u64,
        parts: Vec<PresignedUrl>,
    },
}

impl ExportTarget {
    #[must_use]
    pub fn is_multipart(&self) -> bool {
        matches!(self, Self::Multipart { .. })
    }

    /// The URL of part `number` (1-based).
    #[must_use]
    pub fn url(&self, number: u32) -> Option<&PresignedUrl> {
        let index = usize::try_from(number).ok()?.checked_sub(1)?;
        match self {
            Self::Put(url) => (index == 0).then_some(url),
            Self::Multipart { parts, .. } => parts.get(index),
        }
    }
}

/// `StartExportRequest` once the transport is gone.
pub struct ExportInput {
    pub path: String,
    pub user: Option<String>,
    pub object: Option<ObjectRef>,
    pub target: Option<ExportTarget>,
    pub expires_at_unix_ms: i64,
}

pub struct ExportRequest {
    pub path: String,
    pub user: Option<String>,
    pub target: ExportTarget,
    pub expires_at_unix_ms: i64,
}

impl fmt::Debug for ExportRequest {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("ExportRequest")
            .field("multipart", &self.target.is_multipart())
            .finish_non_exhaustive()
    }
}

impl ExportRequest {
    /// The part bounds first (they bound the work of the policy), then the
    /// URL policy over every URL, then the expiry.
    pub fn validate(
        input: ExportInput,
        sandbox_id: &str,
        now_unix_ms: i64,
    ) -> Result<Self, TransferError> {
        let object = parse_object(input.object.as_ref())?;
        let target = input.target.ok_or(RequestRejection::MissingUrl)?;
        if let ExportTarget::Multipart { part_size, parts } = &target {
            if !(S3_PART_MIN_BYTES..=S3_PART_MAX_BYTES).contains(part_size) {
                return Err(RequestRejection::PartSize.into());
            }
            if !(1..=TRANSFER_MAX_PARTS).contains(&parts.len()) {
                return Err(RequestRejection::PartCount.into());
            }
        }
        {
            let urls: Vec<_> = match &target {
                ExportTarget::Put(url) => vec![url.as_request(UrlRole::Put)],
                ExportTarget::Multipart { parts, .. } => parts
                    .iter()
                    .zip(1u32..)
                    .map(|(url, number)| url.as_request(UrlRole::Part { number }))
                    .collect(),
            };
            validate_request(&object, &urls, sandbox_id, TransferDirection::Export)?;
        }
        check_expiry(input.expires_at_unix_ms, now_unix_ms)?;
        Ok(Self {
            path: input.path,
            user: input.user,
            target,
            expires_at_unix_ms: input.expires_at_unix_ms,
        })
    }
}

/// One `PUT` of the plan: part `number` covers `len` bytes from `offset`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PartRange {
    pub number: u32,
    pub offset: u64,
    pub len: u64,
}

/// The plan for a file of `size` bytes: a single `PUT` refuses more than
/// 5 GiB, a multipart plan must have exactly `max(1, ceil(size /
/// part_size))` URLs or the file changed since the SDK measured it.
pub fn plan_parts(size: u64, target: &ExportTarget) -> Result<Vec<PartRange>, TransferError> {
    match target {
        ExportTarget::Put(_) => {
            if size > TRANSFER_SINGLE_PUT_MAX_BYTES {
                return Err(TransferError::TooLargeForPut);
            }
            Ok(vec![PartRange {
                number: 1,
                offset: 0,
                len: size,
            }])
        }
        ExportTarget::Multipart { part_size, parts } => {
            let part_size = (*part_size).max(1);
            let expected = size.div_ceil(part_size).max(1);
            if u64::try_from(parts.len()).ok() != Some(expected) {
                return Err(TransferError::FileChanged);
            }
            Ok((0..expected)
                .zip(1u32..)
                .map(|(index, number)| {
                    let offset = index * part_size;
                    PartRange {
                        number,
                        offset,
                        len: part_size.min(size - offset),
                    }
                })
                .collect())
        }
    }
}

/// What S3 said to one `PUT`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PutOutcome {
    Stored { etag: Option<String> },
    Transient,
    Failed(TransferFailure),
}

/// Design D10: 200 stores (an `UploadPart` must return its `ETag`), an
/// expired URL or credential is `expired`, any other 403 `access_denied`,
/// 5xx is retried, a redirect is `wrong_region`.
#[must_use]
pub fn classify_put(
    status: u16,
    etag: Option<&str>,
    needs_etag: bool,
    error: &S3Error,
) -> PutOutcome {
    if status == 200 {
        return match etag {
            None if needs_etag => {
                PutOutcome::Failed(TransferFailure::of(FailureReason::UnexpectedResponse))
            }
            _ => PutOutcome::Stored {
                etag: etag.map(str::to_owned),
            },
        };
    }
    if let Some(failure) = error.terminal_failure(status) {
        return PutOutcome::Failed(failure);
    }
    if error.is_transient(status) {
        return PutOutcome::Transient;
    }
    let reason = if status == 403 {
        FailureReason::AccessDenied
    } else {
        FailureReason::UnexpectedResponse
    };
    PutOutcome::Failed(TransferFailure::of(reason))
}

#[must_use]
pub fn classify_put_error(kind: HttpErrorKind) -> PutOutcome {
    match kind {
        HttpErrorKind::ForbiddenAddress => {
            PutOutcome::Failed(TransferFailure::of(FailureReason::ForbiddenAddress))
        }
        HttpErrorKind::Redirect => {
            PutOutcome::Failed(TransferFailure::of(FailureReason::WrongRegion))
        }
        HttpErrorKind::Connect
        | HttpErrorKind::Tls
        | HttpErrorKind::Timeout
        | HttpErrorKind::Io => PutOutcome::Transient,
    }
}

/// One `PUT` of exactly `len` bytes produced by `body`.
pub async fn put_part<H: SignedHttp, B: RequestBody>(
    http: &H,
    url: &PresignedUrl,
    body: B,
    len: u64,
    needs_etag: bool,
) -> (PutOutcome, ResponseLog) {
    let request = SignedRequest {
        method: HttpMethod::Put,
        url: url.url.clone(),
        content_type: Some(OCTET_STREAM),
        content_length: Some(len),
    };
    match http.send(request, Some(body)).await {
        Err(error) => (classify_put_error(error.kind), ResponseLog::default()),
        Ok((head, _)) if head.status == 200 => {
            let outcome = classify_put(200, head.etag.as_deref(), needs_etag, &S3Error::default());
            (outcome, ResponseLog::of(&head, None))
        }
        Ok((head, mut body)) => {
            let error = read_s3_error(&head, &mut body).await;
            let outcome = classify_put(head.status, head.etag.as_deref(), needs_etag, &error);
            (outcome, ResponseLog::of(&head, Some(&error)))
        }
    }
}

#[cfg(test)]
mod tests {
    use std::collections::VecDeque;
    use std::future::Future;

    use bytes::Bytes;

    use super::*;
    use crate::transfer::fake::{FakeHttp, FakeResponse, block_on};
    use crate::transfer::ports::HttpError;
    use crate::transfer::url_policy::UrlPolicyError;

    const SANDBOX: &str = "microvm-0abc";
    const NOW: i64 = 1_790_000_000_000;
    const MIB: u64 = 1024 * 1024;
    const QUERY: &str = "X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=c&X-Amz-Date=d&X-Amz-Expires=60&X-Amz-SignedHeaders=host&X-Amz-Signature=s";

    fn key() -> String {
        format!(
            "rayito-transfer/{SANDBOX}/down/{}",
            "0123456789abcdef".repeat(2)
        )
    }

    fn url(extra: &str) -> PresignedUrl {
        PresignedUrl::new(
            format!(
                "https://amzn-s3-demo-bucket.s3.us-east-1.amazonaws.com/{}?{QUERY}{extra}",
                key()
            ),
            Vec::new(),
        )
    }

    fn parts(count: u32) -> Vec<PresignedUrl> {
        (1..=count)
            .map(|number| url(&format!("&partNumber={number}&uploadId=U1")))
            .collect()
    }

    fn input(target: ExportTarget) -> ExportInput {
        ExportInput {
            path: "/home/user/out.bin".to_owned(),
            user: None,
            object: Some(ObjectRef {
                bucket: "amzn-s3-demo-bucket".to_owned(),
                key: key(),
                region: "us-east-1".to_owned(),
            }),
            target: Some(target),
            expires_at_unix_ms: NOW + 60_000,
        }
    }

    fn multipart(part_size: u64, count: u32) -> ExportTarget {
        ExportTarget::Multipart {
            part_size,
            parts: parts(count),
        }
    }

    struct Chunks(VecDeque<Bytes>);

    impl RequestBody for Chunks {
        fn next_chunk(&mut self) -> impl Future<Output = Result<Option<Bytes>, HttpError>> + Send {
            std::future::ready(Ok(self.0.pop_front()))
        }
    }

    #[test]
    fn requests_are_checked_before_any_io() {
        assert!(ExportRequest::validate(input(ExportTarget::Put(url(""))), SANDBOX, NOW).is_ok());
        let request = ExportRequest::validate(input(multipart(8 * MIB, 3)), SANDBOX, NOW).unwrap();
        assert!(request.target.is_multipart());
        assert!(request.target.url(3).is_some());
        assert!(request.target.url(4).is_none());
        assert!(request.target.url(0).is_none());
        assert!(!format!("{request:?}").contains("amazonaws"));
        let reject = |target| ExportRequest::validate(input(target), SANDBOX, NOW).unwrap_err();
        assert_eq!(
            reject(multipart(5 * MIB - 1, 1)),
            TransferError::Request(RequestRejection::PartSize)
        );
        assert_eq!(
            reject(multipart(5 * 1024 * MIB + 1, 1)),
            TransferError::Request(RequestRejection::PartSize)
        );
        assert_eq!(
            reject(multipart(5 * MIB, 0)),
            TransferError::Request(RequestRejection::PartCount)
        );
        assert_eq!(
            reject(multipart(5 * MIB, 1_001)),
            TransferError::Request(RequestRejection::PartCount)
        );
        let misnumbered = ExportTarget::Multipart {
            part_size: 5 * MIB,
            parts: vec![url("&partNumber=2&uploadId=U1")],
        };
        assert_eq!(
            reject(misnumbered),
            TransferError::Policy(UrlPolicyError::PartParameters)
        );
        let mut missing = input(ExportTarget::Put(url("")));
        missing.target = None;
        assert_eq!(
            ExportRequest::validate(missing, SANDBOX, NOW).unwrap_err(),
            TransferError::Request(RequestRejection::MissingUrl)
        );
        let mut late = input(ExportTarget::Put(url("")));
        late.expires_at_unix_ms = NOW;
        assert_eq!(
            ExportRequest::validate(late, SANDBOX, NOW).unwrap_err(),
            TransferError::Request(RequestRejection::Expiry)
        );
    }

    #[test]
    fn a_single_put_covers_the_whole_file_up_to_five_gibibytes() {
        let put = ExportTarget::Put(url(""));
        assert_eq!(
            plan_parts(0, &put).unwrap(),
            vec![PartRange {
                number: 1,
                offset: 0,
                len: 0
            }]
        );
        assert_eq!(
            plan_parts(TRANSFER_SINGLE_PUT_MAX_BYTES, &put).unwrap()[0].len,
            TRANSFER_SINGLE_PUT_MAX_BYTES
        );
        assert_eq!(
            plan_parts(TRANSFER_SINGLE_PUT_MAX_BYTES + 1, &put).unwrap_err(),
            TransferError::TooLargeForPut
        );
    }

    #[test]
    fn multipart_plans_need_exactly_the_presigned_part_count() {
        let size = 8 * MIB;
        let one = multipart(size, 1);
        assert_eq!(plan_parts(0, &one).unwrap().len(), 1);
        assert_eq!(plan_parts(1, &one).unwrap()[0].len, 1);
        assert_eq!(plan_parts(size, &one).unwrap()[0].len, size);
        assert_eq!(
            plan_parts(size + 1, &one).unwrap_err(),
            TransferError::FileChanged
        );
        let three = multipart(size, 3);
        let plan = plan_parts(3 * size, &three).unwrap();
        assert_eq!(
            plan.iter()
                .map(|part| (part.number, part.offset, part.len))
                .collect::<Vec<_>>(),
            vec![(1, 0, size), (2, size, size), (3, 2 * size, size)]
        );
        let ragged = plan_parts(2 * size + 5, &three).unwrap();
        assert_eq!(ragged[2].len, 5);
        assert_eq!(
            plan_parts(2 * size, &three).unwrap_err(),
            TransferError::FileChanged
        );
    }

    #[test]
    fn put_answers_are_classified_per_design_d10() {
        let none = S3Error::default();
        let parse = |body: &str| S3Error::parse(body.as_bytes(), None);
        assert_eq!(
            classify_put(200, Some("\"e1\""), true, &none),
            PutOutcome::Stored {
                etag: Some("\"e1\"".to_owned())
            }
        );
        assert_eq!(
            classify_put(200, None, false, &none),
            PutOutcome::Stored { etag: None }
        );
        assert_eq!(
            classify_put(200, None, true, &none),
            PutOutcome::Failed(TransferFailure::of(FailureReason::UnexpectedResponse))
        );
        let expired =
            parse("<Error><Code>AccessDenied</Code><Message>Request has expired</Message></Error>");
        assert_eq!(
            classify_put(403, None, false, &expired),
            PutOutcome::Failed(TransferFailure::of(FailureReason::Expired))
        );
        let token = parse("<Error><Code>ExpiredToken</Code></Error>");
        assert_eq!(
            classify_put(400, None, false, &token),
            PutOutcome::Failed(TransferFailure::of(FailureReason::Expired))
        );
        let denied =
            parse("<Error><Code>AccessDenied</Code><Message>Access Denied</Message></Error>");
        assert_eq!(
            classify_put(403, None, false, &denied),
            PutOutcome::Failed(TransferFailure::of(FailureReason::AccessDenied))
        );
        assert_eq!(classify_put(502, None, false, &none), PutOutcome::Transient);
        assert_eq!(
            classify_put(301, None, false, &none),
            PutOutcome::Failed(TransferFailure::of(FailureReason::WrongRegion))
        );
        assert_eq!(
            classify_put_error(HttpErrorKind::Redirect),
            PutOutcome::Failed(TransferFailure::of(FailureReason::WrongRegion))
        );
        assert_eq!(classify_put_error(HttpErrorKind::Io), PutOutcome::Transient);
    }

    #[test]
    fn the_put_declares_its_length_and_octet_stream_and_streams_the_body() {
        let http = FakeHttp::with(vec![FakeResponse::etag("\"etag-1\"")]);
        let body = Chunks(VecDeque::from([
            Bytes::from_static(b"ab"),
            Bytes::from_static(b"cd"),
        ]));
        let (outcome, log) = block_on(put_part(&http, &parts(1)[0], body, 4, true));
        assert_eq!(
            outcome,
            PutOutcome::Stored {
                etag: Some("\"etag-1\"".to_owned())
            }
        );
        assert_eq!(log.http_status, Some(200));
        let calls = http.calls();
        assert_eq!(calls[0].method, HttpMethod::Put);
        assert_eq!(calls[0].content_type, Some(OCTET_STREAM));
        assert_eq!(calls[0].content_length, Some(4));
        assert_eq!(calls[0].body, b"abcd");
    }

    #[test]
    fn a_failed_put_reads_the_error_document() {
        let http = FakeHttp::with(vec![FakeResponse::error(
            403,
            "AccessDenied",
            "Request has expired",
        )]);
        let body = Chunks(VecDeque::new());
        let (outcome, log) = block_on(put_part(&http, &url(""), body, 0, false));
        assert_eq!(
            outcome,
            PutOutcome::Failed(TransferFailure::of(FailureReason::Expired))
        );
        assert_eq!(log.s3_error_code.as_deref(), Some("AccessDenied"));
    }
}
