//! What a transfer needs from the outside (design D7): one HTTPS request to
//! a presigned URL, with a streamed request body for uploads and a
//! streamed response body. Native `async fn` in traits like the
//! persistence ports, wired as a generic in `rayd`; no executor, TLS or
//! HTTP type crosses. The URL is a bearer credential: it travels in a
//! `Zeroizing<String>`, never appears in a `Debug` or an error, and the
//! adapter drops every copy right after the request is sent.

use std::fmt;
use std::future::Future;

use bytes::Bytes;
use thiserror::Error;
use zeroize::Zeroizing;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HttpMethod {
    Get,
    Put,
    Delete,
}

impl HttpMethod {
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Get => "GET",
            Self::Put => "PUT",
            Self::Delete => "DELETE",
        }
    }
}

/// One request to a presigned URL. The only headers the adapter sends
/// besides `host` and `user-agent` are `content-type` and
/// `content-length`, and only when set here.
pub struct SignedRequest {
    pub method: HttpMethod,
    pub url: Zeroizing<String>,
    pub content_type: Option<&'static str>,
    pub content_length: Option<u64>,
}

impl fmt::Debug for SignedRequest {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("SignedRequest")
            .field("method", &self.method)
            .field("url", &"<redacted>")
            .field("content_type", &self.content_type)
            .field("content_length", &self.content_length)
            .finish()
    }
}

/// The parts of a response a transfer looks at; `request_id` is the
/// `x-amz-request-id` header, for the log line.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct HttpHead {
    pub status: u16,
    pub content_length: Option<u64>,
    pub etag: Option<String>,
    pub request_id: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HttpErrorKind {
    /// Every address the host resolved to is one `rayd` refuses to reach
    /// (loopback, link-local, IMDS, ...).
    ForbiddenAddress,
    Connect,
    Tls,
    /// No byte moved in either direction for the idle timeout.
    Timeout,
    /// A 3xx: redirects are never followed.
    Redirect,
    Io,
}

impl HttpErrorKind {
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::ForbiddenAddress => "forbidden_address",
            Self::Connect => "connect",
            Self::Tls => "tls",
            Self::Timeout => "timeout",
            Self::Redirect => "redirect",
            Self::Io => "io",
        }
    }
}

/// A transport failure; carries no URL, host or address.
#[derive(Debug, Error, Clone, Copy, PartialEq, Eq)]
#[error("{}", kind.as_str())]
pub struct HttpError {
    pub kind: HttpErrorKind,
}

impl HttpError {
    #[must_use]
    pub fn new(kind: HttpErrorKind) -> Self {
        Self { kind }
    }
}

/// A response body read chunk by chunk; `None` at the end.
pub trait ResponseBody: Send {
    fn next_chunk(&mut self) -> impl Future<Output = Result<Option<Bytes>, HttpError>> + Send;
}

/// A request body produced chunk by chunk; `None` at the end. An error
/// aborts the request.
pub trait RequestBody: Send + 'static {
    fn next_chunk(&mut self) -> impl Future<Output = Result<Option<Bytes>, HttpError>> + Send;
}

/// The body of a `GET` or a `DELETE`.
#[derive(Debug, Clone, Copy, Default)]
pub struct NoBody;

impl RequestBody for NoBody {
    fn next_chunk(&mut self) -> impl Future<Output = Result<Option<Bytes>, HttpError>> + Send {
        std::future::ready(Ok(None))
    }
}

/// HTTPS to presigned S3 URLs and nothing else: no redirects, no proxy, no
/// credentials, only addresses the resolver filter allows.
pub trait SignedHttp: Send + Sync + 'static {
    type Body: ResponseBody;

    fn send<B: RequestBody>(
        &self,
        request: SignedRequest,
        body: Option<B>,
    ) -> impl Future<Output = Result<(HttpHead, Self::Body), HttpError>> + Send;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn debug_never_shows_the_url() {
        let request = SignedRequest {
            method: HttpMethod::Get,
            url: Zeroizing::new("https://x/?X-Amz-Signature=abc".to_owned()),
            content_type: None,
            content_length: None,
        };
        let shown = format!("{request:?}");
        assert!(!shown.contains("X-Amz-Signature"), "{shown}");
        assert!(shown.contains("<redacted>"));
        assert_eq!(
            HttpError::new(HttpErrorKind::Timeout).to_string(),
            "timeout"
        );
        assert_eq!(HttpMethod::Delete.as_str(), "DELETE");
    }
}
