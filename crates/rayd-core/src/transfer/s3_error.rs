//! The S3 error document (design D7): at most the first 4 KiB of the body,
//! the text of the first `<Code>`, `<Message>` and `<RequestId>` elements
//! with the five XML entities decoded, each cut at 256 bytes. No XML crate:
//! S3's error body is flat, and nothing here is ever sent to a client.

use super::error::{FailureReason, TransferFailure};
use super::ports::{HttpHead, ResponseBody};

pub const S3_ERROR_BODY_MAX_BYTES: usize = 4096;
pub const S3_ERROR_FIELD_MAX_BYTES: usize = 256;
pub const REQUEST_EXPIRED_MESSAGE: &str = "Request has expired";

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct S3Error {
    pub code: Option<String>,
    pub message: Option<String>,
    pub request_id: Option<String>,
}

/// The fields of one S3 answer a transfer log line may carry (design
/// D21): never a URL, a bucket, a key or a message.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ResponseLog {
    pub http_status: Option<u16>,
    pub s3_error_code: Option<String>,
    pub s3_request_id: Option<String>,
}

impl ResponseLog {
    #[must_use]
    pub fn of(head: &HttpHead, error: Option<&S3Error>) -> Self {
        Self {
            http_status: Some(head.status),
            s3_error_code: error.and_then(|error| error.code.clone()),
            s3_request_id: error
                .and_then(|error| error.request_id.clone())
                .or_else(|| head.request_id.clone()),
        }
    }
}

impl S3Error {
    /// The answers that end a transfer whatever it was doing (design D9,
    /// D10): an expired URL or credential, a rejected signature, a bucket
    /// in another region, a missing bucket.
    #[must_use]
    pub fn terminal_failure(&self, status: u16) -> Option<TransferFailure> {
        let reason = if self.is_expired_request() || self.is("ExpiredToken") {
            FailureReason::Expired
        } else if self.is("SignatureDoesNotMatch") || self.is("AuthorizationQueryParametersError") {
            FailureReason::SignatureRejected
        } else if matches!(status, 301 | 307)
            || self.is("PermanentRedirect")
            || self.is("TemporaryRedirect")
        {
            FailureReason::WrongRegion
        } else if self.is("NoSuchBucket") {
            FailureReason::BucketMissing
        } else {
            return None;
        };
        Some(TransferFailure::of(reason))
    }

    /// S3 asks to retry: any 5xx, or `SlowDown` whatever its status.
    #[must_use]
    pub fn is_transient(&self, status: u16) -> bool {
        (500..=599).contains(&status) || self.is("SlowDown")
    }

    /// `header_request_id` (`x-amz-request-id`) fills the request id when
    /// the body carries none.
    #[must_use]
    pub fn parse(body: &[u8], header_request_id: Option<&str>) -> Self {
        let body = &body[..body.len().min(S3_ERROR_BODY_MAX_BYTES)];
        let text = String::from_utf8_lossy(body);
        Self {
            code: element(&text, "Code"),
            message: element(&text, "Message"),
            request_id: element(&text, "RequestId")
                .or_else(|| header_request_id.map(|id| truncate(id.to_owned()))),
        }
    }

    #[must_use]
    pub fn is(&self, code: &str) -> bool {
        self.code.as_deref() == Some(code)
    }

    /// The 403 an expired presigned URL answers with.
    #[must_use]
    pub fn is_expired_request(&self) -> bool {
        self.is("AccessDenied") && self.message.as_deref() == Some(REQUEST_EXPIRED_MESSAGE)
    }
}

/// Reads at most `S3_ERROR_BODY_MAX_BYTES` of an error response; a body
/// that fails midway is parsed as far as it got.
pub async fn read_s3_error<B: ResponseBody>(head: &HttpHead, body: &mut B) -> S3Error {
    let mut collected = Vec::new();
    while collected.len() < S3_ERROR_BODY_MAX_BYTES {
        match body.next_chunk().await {
            Ok(Some(chunk)) => collected.extend_from_slice(&chunk),
            Ok(None) | Err(_) => break,
        }
    }
    S3Error::parse(&collected, head.request_id.as_deref())
}

fn element(text: &str, name: &str) -> Option<String> {
    let open = format!("<{name}>");
    let close = format!("</{name}>");
    let start = text.find(&open)? + open.len();
    let end = text[start..]
        .find(&close)
        .map_or(text.len(), |offset| start + offset);
    Some(truncate(decode_entities(&text[start..end])))
}

fn decode_entities(raw: &str) -> String {
    raw.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", "\"")
        .replace("&apos;", "'")
        .replace("&amp;", "&")
}

fn truncate(mut value: String) -> String {
    if value.len() > S3_ERROR_FIELD_MAX_BYTES {
        let mut cut = S3_ERROR_FIELD_MAX_BYTES;
        while !value.is_char_boundary(cut) {
            cut -= 1;
        }
        value.truncate(cut);
    }
    value
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::transfer::fake::{FakeBody, block_on};

    const NO_SUCH_KEY: &str = r#"<?xml version="1.0" encoding="UTF-8"?>
<Error><Code>NoSuchKey</Code><Message>The specified key does not exist.</Message><Key>rayito-transfer/x</Key><RequestId>4Q8ZYJ2KXR7VEXAMPLE</RequestId><HostId>abc=</HostId></Error>"#;
    const EXPIRED: &str = r#"<?xml version="1.0" encoding="UTF-8"?>
<Error><Code>AccessDenied</Code><Message>Request has expired</Message><X-Amz-Expires>1</X-Amz-Expires><Expires>2026-09-22T00:00:01Z</Expires><ServerTime>2026-09-22T00:00:04Z</ServerTime><RequestId>R1</RequestId></Error>"#;
    const EXPIRED_TOKEN: &str = "<Error><Code>ExpiredToken</Code><Message>The provided token has expired.</Message><RequestId>R2</RequestId></Error>";
    const SIGNATURE: &str = "<Error><Code>SignatureDoesNotMatch</Code><Message>The request signature we calculated does not match the signature you provided.</Message></Error>";
    const REDIRECT: &str = "<Error><Code>PermanentRedirect</Code><Message>The bucket you are attempting to access must be addressed using the specified endpoint.</Message><Endpoint>b.s3.us-west-2.amazonaws.com</Endpoint></Error>";

    #[test]
    fn real_s3_bodies_are_classified_by_their_elements() {
        let missing = S3Error::parse(NO_SUCH_KEY.as_bytes(), None);
        assert!(missing.is("NoSuchKey"));
        assert_eq!(missing.request_id.as_deref(), Some("4Q8ZYJ2KXR7VEXAMPLE"));
        let expired = S3Error::parse(EXPIRED.as_bytes(), None);
        assert!(expired.is_expired_request());
        assert!(S3Error::parse(EXPIRED_TOKEN.as_bytes(), None).is("ExpiredToken"));
        let signature = S3Error::parse(SIGNATURE.as_bytes(), None);
        assert!(signature.is("SignatureDoesNotMatch"));
        assert!(!signature.is_expired_request());
        assert!(S3Error::parse(REDIRECT.as_bytes(), None).is("PermanentRedirect"));
    }

    #[test]
    fn the_header_request_id_fills_in_and_empty_bodies_parse_to_nothing() {
        let parsed = S3Error::parse(b"", Some("HDR1"));
        assert_eq!(parsed.code, None);
        assert_eq!(parsed.request_id.as_deref(), Some("HDR1"));
        let body_wins = S3Error::parse(EXPIRED.as_bytes(), Some("HDR1"));
        assert_eq!(body_wins.request_id.as_deref(), Some("R1"));
    }

    #[test]
    fn entities_are_decoded_and_long_fields_truncated() {
        let parsed = S3Error::parse(
            b"<Error><Code>A&amp;B</Code><Message>&lt;x&gt; &quot;q&quot; &apos;s&apos; &amp;lt;</Message></Error>",
            None,
        );
        assert_eq!(parsed.code.as_deref(), Some("A&B"));
        assert_eq!(parsed.message.as_deref(), Some("<x> \"q\" 's' &lt;"));
        let long = format!("<Error><Message>{}</Message></Error>", "m".repeat(1_000));
        let truncated = S3Error::parse(long.as_bytes(), None);
        assert_eq!(
            truncated.message.map(|m| m.len()),
            Some(S3_ERROR_FIELD_MAX_BYTES)
        );
    }

    #[test]
    fn only_the_first_four_kilobytes_are_read() {
        let late = format!("{}<Code>Late</Code>", " ".repeat(S3_ERROR_BODY_MAX_BYTES));
        assert_eq!(S3Error::parse(late.as_bytes(), None).code, None);
        let truncated_mid_element = "<Error><Code>NoSuchK";
        assert_eq!(
            S3Error::parse(truncated_mid_element.as_bytes(), None)
                .code
                .as_deref(),
            Some("NoSuchK")
        );
    }

    #[test]
    fn terminal_answers_end_a_transfer_whatever_it_was_doing() {
        let reason = |status: u16, body: &str| {
            S3Error::parse(body.as_bytes(), None)
                .terminal_failure(status)
                .map(|failure| failure.reason)
        };
        assert_eq!(reason(403, EXPIRED), Some(FailureReason::Expired));
        assert_eq!(reason(400, EXPIRED_TOKEN), Some(FailureReason::Expired));
        assert_eq!(
            reason(403, SIGNATURE),
            Some(FailureReason::SignatureRejected)
        );
        assert_eq!(
            reason(
                400,
                "<Error><Code>AuthorizationQueryParametersError</Code></Error>"
            ),
            Some(FailureReason::SignatureRejected)
        );
        assert_eq!(reason(301, REDIRECT), Some(FailureReason::WrongRegion));
        assert_eq!(reason(307, ""), Some(FailureReason::WrongRegion));
        assert_eq!(
            reason(404, "<Error><Code>NoSuchBucket</Code></Error>"),
            Some(FailureReason::BucketMissing)
        );
        assert_eq!(reason(404, NO_SUCH_KEY), None);
        assert_eq!(
            reason(
                403,
                "<Error><Code>AccessDenied</Code><Message>Access Denied</Message></Error>"
            ),
            None
        );
        let slow = S3Error::parse(b"<Error><Code>SlowDown</Code></Error>", None);
        assert!(slow.is_transient(400));
        assert!(S3Error::default().is_transient(503));
        assert!(!S3Error::default().is_transient(404));
    }

    #[test]
    fn response_logs_carry_status_code_and_request_id_only() {
        let head = HttpHead {
            status: 404,
            request_id: Some("HDR".to_owned()),
            ..HttpHead::default()
        };
        let error = S3Error::parse(NO_SUCH_KEY.as_bytes(), head.request_id.as_deref());
        let log = ResponseLog::of(&head, Some(&error));
        assert_eq!(log.http_status, Some(404));
        assert_eq!(log.s3_error_code.as_deref(), Some("NoSuchKey"));
        assert_eq!(log.s3_request_id.as_deref(), Some("4Q8ZYJ2KXR7VEXAMPLE"));
        let ok = ResponseLog::of(
            &HttpHead {
                status: 200,
                ..head
            },
            None,
        );
        assert_eq!(ok.s3_request_id.as_deref(), Some("HDR"));
        assert_eq!(ok.s3_error_code, None);
    }

    #[test]
    fn read_s3_error_stops_after_the_cap() {
        let mut body = FakeBody::new(vec![
            vec![b' '; 3_000],
            vec![b' '; 3_000],
            NO_SUCH_KEY.as_bytes().to_vec(),
        ]);
        let parsed = block_on(read_s3_error(&HttpHead::default(), &mut body));
        assert_eq!(parsed.code, None);
        assert_eq!(body.remaining(), 1, "the third chunk was never read");
        let mut short = FakeBody::new(vec![NO_SUCH_KEY.as_bytes().to_vec()]);
        let head = HttpHead {
            status: 404,
            ..HttpHead::default()
        };
        assert!(block_on(read_s3_error(&head, &mut short)).is("NoSuchKey"));
    }
}
