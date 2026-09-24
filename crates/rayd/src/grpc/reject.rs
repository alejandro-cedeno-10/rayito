//! How a tower layer refuses a gRPC call before tonic decodes it: the
//! request body is read to its end (bounded) before the status goes out. A
//! trailers-only response on a half-open HTTP/2 stream makes hyper reset
//! the stream, and the AWS proxy forwards that reset to the client as
//! `RST_STREAM(CANCEL)`, which grpc surfaces as `CANCELLED` instead of the
//! intended status (measured 2026-09-15, `AWS_API_NOTES.md` §16 Q29).

use std::time::Duration;

use bytes::Buf;
use http_body::Body;
use http_body_util::BodyExt;

/// A rejected unary or server-stream request carries one small message; a
/// client that keeps streaming past these bounds is answered anyway and the
/// resulting reset is its own problem.
pub const REJECTED_BODY_DRAIN_TIMEOUT: Duration = Duration::from_secs(2);
pub const REJECTED_BODY_DRAIN_MAX_BYTES: usize = 1 << 20;

pub async fn drain_rejected_body<B>(body: B)
where
    B: Body + Send,
{
    let drained =
        tokio::time::timeout(REJECTED_BODY_DRAIN_TIMEOUT, read_to_end_bounded(body)).await;
    if drained.is_err() {
        tracing::debug!(
            timeout_ms = REJECTED_BODY_DRAIN_TIMEOUT.as_millis(),
            "rejected request body still open; answering anyway"
        );
    }
}

async fn read_to_end_bounded<B>(body: B)
where
    B: Body + Send,
{
    let mut body = Box::pin(body);
    let mut seen = 0usize;
    while let Some(Ok(frame)) = body.frame().await {
        seen += frame.data_ref().map_or(0, Buf::remaining);
        if seen > REJECTED_BODY_DRAIN_MAX_BYTES {
            return;
        }
    }
}
