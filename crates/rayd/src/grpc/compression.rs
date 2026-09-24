//! `CompressionOptInLayer` (design D16): grpcio and Connect-ES advertise
//! `gzip` in `grpc-accept-encoding` by default, and tonic 0.14.6 cannot
//! turn compression off for one server-stream response, so enabling
//! `send_compressed` alone would gzip every response to every existing
//! client. The layer keeps `grpc-accept-encoding` only when the call opts
//! in with `rayito-compress: gzip` and rewrites it to `identity`
//! otherwise; requests may always arrive compressed.

use std::task::{Context, Poll};

use http::{HeaderMap, HeaderValue, Request};
use tower::{Layer, Service};

/// The request metadata that opts a call's responses into gzip.
pub const COMPRESSION_OPT_IN_HEADER: &str = "rayito-compress";
const GRPC_ACCEPT_ENCODING: &str = "grpc-accept-encoding";
const GZIP: &str = "gzip";
const IDENTITY: &str = "identity";

#[derive(Debug, Clone, Copy, Default)]
pub struct CompressionOptInLayer;

impl<S> Layer<S> for CompressionOptInLayer {
    type Service = CompressionOptIn<S>;

    fn layer(&self, inner: S) -> Self::Service {
        CompressionOptIn { inner }
    }
}

#[derive(Debug, Clone)]
pub struct CompressionOptIn<S> {
    inner: S,
}

impl<S, B> Service<Request<B>> for CompressionOptIn<S>
where
    S: Service<Request<B>>,
{
    type Response = S::Response;
    type Error = S::Error;
    type Future = S::Future;

    fn poll_ready(&mut self, cx: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
        self.inner.poll_ready(cx)
    }

    fn call(&mut self, mut request: Request<B>) -> Self::Future {
        restrict_response_encoding(request.headers_mut());
        self.inner.call(request)
    }
}

/// `identity` unless the call carries `rayito-compress: gzip` (any case).
pub fn restrict_response_encoding(headers: &mut HeaderMap) {
    let opted_in = headers
        .get(COMPRESSION_OPT_IN_HEADER)
        .and_then(|value| value.to_str().ok())
        .is_some_and(|value| value.trim().eq_ignore_ascii_case(GZIP));
    if !opted_in {
        headers.insert(GRPC_ACCEPT_ENCODING, HeaderValue::from_static(IDENTITY));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn headers(pairs: &[(&'static str, &'static str)]) -> HeaderMap {
        let mut headers = HeaderMap::new();
        for (name, value) in pairs {
            headers.insert(*name, HeaderValue::from_static(value));
        }
        headers
    }

    fn accept(headers: &HeaderMap) -> Option<&str> {
        headers
            .get(GRPC_ACCEPT_ENCODING)
            .and_then(|value| value.to_str().ok())
    }

    #[test]
    fn without_the_opt_in_responses_are_identity() {
        let mut advertised = headers(&[(GRPC_ACCEPT_ENCODING, "identity,deflate,gzip")]);
        restrict_response_encoding(&mut advertised);
        assert_eq!(accept(&advertised), Some("identity"));
        let mut silent = HeaderMap::new();
        restrict_response_encoding(&mut silent);
        assert_eq!(accept(&silent), Some("identity"));
        let mut other = headers(&[
            (COMPRESSION_OPT_IN_HEADER, "deflate"),
            (GRPC_ACCEPT_ENCODING, "gzip"),
        ]);
        restrict_response_encoding(&mut other);
        assert_eq!(accept(&other), Some("identity"));
    }

    #[test]
    fn with_the_opt_in_the_advertised_encodings_are_kept() {
        for value in ["gzip", "GZIP", " Gzip "] {
            let mut opted = HeaderMap::new();
            opted.insert(COMPRESSION_OPT_IN_HEADER, HeaderValue::from_static(value));
            opted.insert(
                GRPC_ACCEPT_ENCODING,
                HeaderValue::from_static("identity,deflate,gzip"),
            );
            restrict_response_encoding(&mut opted);
            assert_eq!(accept(&opted), Some("identity,deflate,gzip"), "{value}");
        }
    }

    #[test]
    fn the_header_name_matches_limits_json() {
        let limits: serde_json::Value =
            serde_json::from_str(include_str!("../../../../limits.json")).unwrap();
        assert_eq!(
            limits["compressionOptInHeader"].as_str(),
            Some(COMPRESSION_OPT_IN_HEADER)
        );
    }
}
