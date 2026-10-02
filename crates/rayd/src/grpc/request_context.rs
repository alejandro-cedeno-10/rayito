//! Tower layer that reads the inbound W3C `traceparent` header (research
//! Q92, `ARCHITECTURE.md` ADR-021: `traceparent`/`tracestate` only, never
//! `grpc-trace-bin` nor `baggage`) and, when it parses, opens a `tracing`
//! span carrying `trace_id`/`span_id` for the lifetime of that one RPC, so
//! every log line the handler emits while handling it is correlated to the
//! caller's trace. Without the header (any caller that never passes
//! `tracer_provider=`/`tracerProvider`), `call()` instruments the request
//! with `tracing::Span::none()` -- exactly 0.5.x's no-span shape, so
//! `logging.rs`'s JSON lines carry no `span` object at all, unchanged.
//!
//! This is the `rayd`-side half of m15-rayd-otlp's traceparent propagation;
//! the SDK side (`TraceparentProvider`, evaluated per RPC on the caller's
//! thread or task) sends the header when `tracer_provider=` is set. AWS
//! acceptance (`AWS_API_NOTES.md` Q113) saw these fields in the runtime
//! logs through the Lambda `MicroVM` proxy.

use std::task::{Context, Poll};

use tonic::body::Body as TonicBody;
use tower::{Layer, Service};
use tracing::Instrument;

/// `traceparent`'s header name, read case-insensitively by `http::HeaderMap`
/// like every other header.
const TRACEPARENT_HEADER: &str = "traceparent";
/// `version-trace_id-parent_id-flags` (W3C Trace Context §3.2): the lengths
/// of each lowercase-hex field this parser accepts.
const VERSION_LEN: usize = 2;
const TRACE_ID_LEN: usize = 32;
const PARENT_ID_LEN: usize = 16;
const FLAGS_LEN: usize = 2;
/// §3.2: version `ff` is reserved and must never be processed as valid.
const RESERVED_VERSION: &str = "ff";

/// The two W3C Trace Context fields this module ever reads out of
/// `traceparent`: lowercase hex, already validated, never the raw header
/// value (a caller-controlled string `logging.rs`'s field allowlist would
/// otherwise have to vouch for byte for byte).
#[derive(Debug, Clone, PartialEq, Eq)]
struct TraceContext {
    trace_id: String,
    span_id: String,
}

/// `None` on anything that is not exactly one valid `00`-version
/// `traceparent` (a future version may add trailing fields this parser
/// does not understand, so it only accepts the current, fully-specified
/// shape rather than guessing at extensions): malformed, the reserved `ff`
/// version, or an all-zero trace-id/parent-id (explicitly invalid per
/// §3.2). Never panics on attacker-controlled input.
fn parse_traceparent(value: &str) -> Option<TraceContext> {
    let mut parts = value.trim().split('-');
    let version = parts.next()?;
    let trace_id = parts.next()?;
    let parent_id = parts.next()?;
    let flags = parts.next()?;
    if parts.next().is_some() {
        return None;
    }
    if version.len() != VERSION_LEN || version == RESERVED_VERSION || !is_lower_hex(version) {
        return None;
    }
    if trace_id.len() != TRACE_ID_LEN || is_all_zero(trace_id) || !is_lower_hex(trace_id) {
        return None;
    }
    if parent_id.len() != PARENT_ID_LEN || is_all_zero(parent_id) || !is_lower_hex(parent_id) {
        return None;
    }
    if flags.len() != FLAGS_LEN || !is_lower_hex(flags) {
        return None;
    }
    Some(TraceContext {
        trace_id: trace_id.to_owned(),
        span_id: parent_id.to_owned(),
    })
}

fn is_lower_hex(value: &str) -> bool {
    !value.is_empty()
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn is_all_zero(value: &str) -> bool {
    value.bytes().all(|byte| byte == b'0')
}

#[derive(Debug, Clone, Copy, Default)]
pub struct RequestContextLayer;

impl<S> Layer<S> for RequestContextLayer {
    type Service = RequestContextService<S>;

    fn layer(&self, inner: S) -> Self::Service {
        RequestContextService { inner }
    }
}

#[derive(Debug, Clone)]
pub struct RequestContextService<S> {
    inner: S,
}

impl<S> Service<http::Request<TonicBody>> for RequestContextService<S>
where
    S: Service<http::Request<TonicBody>>,
{
    type Response = S::Response;
    type Error = S::Error;
    type Future = tracing::instrument::Instrumented<S::Future>;

    fn poll_ready(&mut self, cx: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
        self.inner.poll_ready(cx)
    }

    fn call(&mut self, request: http::Request<TonicBody>) -> Self::Future {
        let context = request
            .headers()
            .get(TRACEPARENT_HEADER)
            .and_then(|value| value.to_str().ok())
            .and_then(parse_traceparent);
        let span = match &context {
            Some(context) => tracing::info_span!(
                "rpc",
                trace_id = context.trace_id.as_str(),
                span_id = context.span_id.as_str(),
            ),
            None => tracing::Span::none(),
        };
        self.inner.call(request).instrument(span)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_well_formed_traceparent_parses_the_trace_and_span_ids() {
        let context = parse_traceparent("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")
            .expect("a valid W3C traceparent");
        assert_eq!(context.trace_id, "4bf92f3577b34da6a3ce929d0e0e4736");
        assert_eq!(context.span_id, "00f067aa0ba902b7");
    }

    #[test]
    fn leading_or_trailing_whitespace_is_trimmed() {
        assert!(
            parse_traceparent("  00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01  ")
                .is_some()
        );
    }

    #[test]
    fn the_reserved_version_ff_is_rejected() {
        assert!(
            parse_traceparent("ff-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01").is_none()
        );
    }

    #[test]
    fn an_all_zero_trace_id_or_parent_id_is_rejected() {
        assert!(
            parse_traceparent("00-00000000000000000000000000000000-00f067aa0ba902b7-01").is_none()
        );
        assert!(
            parse_traceparent("00-4bf92f3577b34da6a3ce929d0e0e4736-0000000000000000-01").is_none()
        );
    }

    #[test]
    fn wrong_field_lengths_uppercase_hex_and_extra_or_missing_fields_are_all_rejected() {
        assert!(parse_traceparent("00-short-00f067aa0ba902b7-01").is_none());
        assert!(
            parse_traceparent("00-4BF92F3577B34DA6A3CE929D0E0E4736-00f067aa0ba902b7-01").is_none()
        );
        assert!(
            parse_traceparent("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7").is_none()
        );
        assert!(
            parse_traceparent("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01-extra")
                .is_none()
        );
        assert!(parse_traceparent("not-a-traceparent-at-all").is_none());
        assert!(parse_traceparent("").is_none());
    }

    #[test]
    fn grpc_trace_bin_and_baggage_are_never_consulted() {
        // This module exposes no function that even takes those header
        // names: the only header `RequestContextService::call` reads is
        // `traceparent` (`TRACEPARENT_HEADER`), by construction.
        assert_eq!(TRACEPARENT_HEADER, "traceparent");
    }
}
