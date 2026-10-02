//! `rayd_core::telemetry::TelemetrySink` over `CloudWatch`'s OTLP/HTTP
//! endpoint (research §6, options B1/B1'; `AWS_API_NOTES.md` §26): one
//! HTTPS POST per batch, gzip-compressed, authenticated either by `SigV4`
//! over the execution role's IMDS credentials or by a bearer token the SDK
//! pushed through `ConfigureSandbox`. A dedicated client rather than
//! reusing `HyperSignedHttp` (ADR-010's presigned-URL client, `signed_http`):
//! that one's request shape has no room for `Authorization`/`x-amz-*`
//! headers. `FilteringResolver` (`signed_http::is_forbidden_address`) still
//! guards DNS resolution even though the host is a fixed AWS endpoint, not
//! attacker-controlled input: belt and suspenders against a poisoned
//! resolver.

use std::fmt;
use std::io::{self, Write as _};
use std::sync::Arc;
use std::time::{Duration, SystemTime};

use bytes::Bytes;
use flate2::Compression;
use flate2::write::GzEncoder;
use http::{HeaderValue, Request, Uri, header};
use http_body_util::{BodyExt, Full};
use hyper_rustls::{ConfigBuilderExt, HttpsConnectorBuilder};
use hyper_util::client::legacy::Client;
use hyper_util::client::legacy::connect::HttpConnector;
use hyper_util::client::legacy::connect::dns::GaiResolver;
use hyper_util::rt::{TokioExecutor, TokioTimer};
use rayd_core::telemetry::{SinkError, TelemetrySink};
use rustls::ClientConfig;
use zeroize::Zeroizing;

use rayd_core::transfer::is_forbidden_address;

use super::aws_sigv4::{SigningRequest, sign};
use super::credential_broker::ImdsCredentialBroker;
use super::signed_http::FilteringResolver;

/// `AWS_API_NOTES.md` §26: the fixed `SigV4` service name `CloudWatch`'s OTLP
/// endpoint signs under (distinct from the classic `cloudwatch` API).
const SIGV4_SERVICE: &str = "monitoring";
const OTLP_METRICS_PATH: &str = "/v1/metrics";
const CONTENT_TYPE: &str = "application/x-protobuf";
/// Not AWS-mandated: a generous, round number for one TCP+TLS handshake to
/// a fixed, well-connected AWS regional endpoint, well under
/// `REQUEST_TIMEOUT` so a stuck handshake still leaves room for the POST
/// itself to be attempted and time out on its own.
const CONNECT_TIMEOUT: Duration = Duration::from_secs(5);
/// Caps the HTTP request itself (connect + send + response), independent
/// of `interval_s`: a hung endpoint must never pile up requests across
/// ticks. Deliberately *shorter* than the caller's own whole-attempt
/// timeout (`features::telemetry_export::EXPORT_ATTEMPT_TIMEOUT`, 8 s,
/// which also covers the `ImdsCredentialBroker` lease fetch
/// `authorization_header` may need first): a nested timeout that can never
/// fire first is dead code, so this one must stay below that 8 s, with
/// room to spare for the lease fetch, rather than merely under it.
const REQUEST_TIMEOUT: Duration = Duration::from_secs(6);
/// Comfortably above `interval_s`'s default (60 s) and minimum (15 s)
/// would be too long to matter either way: this only governs how soon an
/// idle pooled connection is dropped between ticks, not correctness, and
/// hyper's own default is 90 s.
const POOL_IDLE_TIMEOUT: Duration = Duration::from_secs(30);
/// One sandbox, one exporter, at most one in-flight POST at a time: a
/// second idle connection only matters if a `/suspend` flush and a regular
/// tick's send ever raced, which `SharedState`'s single `AsyncMutex`
/// already rules out.
const POOL_MAX_IDLE_PER_HOST: usize = 2;
/// AWS's own header name for the `SigV4` session-token companion to a
/// temporary credential's `Authorization` header (`SigV4-signing.html`).
const SESSION_TOKEN_HEADER: &str = "x-amz-security-token";
const X_AMZ_DATE_HEADER: &str = "x-amz-date";
const X_AMZ_CONTENT_SHA256_HEADER: &str = "x-amz-content-sha256";

/// Where this sink's credentials come from, mirroring
/// `rayd_core::telemetry::TelemetryAuth` one to one but holding the live
/// adapter instead of the wire auth choice.
#[derive(Clone)]
pub enum SinkCredentials {
    ExecutionRole(Arc<ImdsCredentialBroker>),
    Bearer(Zeroizing<String>),
}

/// The client could not be built (no native trust store) or a send failed
/// before an HTTP status was even available.
#[derive(Debug)]
pub struct SinkInitError(io::Error);

impl fmt::Display for SinkInitError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "trust store unavailable: {}", self.0.kind())
    }
}

impl std::error::Error for SinkInitError {}

pub struct CloudWatchOtlpSink {
    client: Client<
        hyper_rustls::HttpsConnector<HttpConnector<FilteringResolver<GaiResolver>>>,
        Full<Bytes>,
    >,
    host: String,
    region: String,
    user_agent: HeaderValue,
}

impl CloudWatchOtlpSink {
    pub fn new(region: &str) -> Result<Self, SinkInitError> {
        let config = ClientConfig::builder_with_provider(Arc::new(
            rustls::crypto::aws_lc_rs::default_provider(),
        ))
        .with_safe_default_protocol_versions()
        .map_err(|error| SinkInitError(io::Error::other(error)))?
        .with_native_roots()
        .map_err(SinkInitError)?
        .with_no_client_auth();
        let resolver = FilteringResolver::new(GaiResolver::new(), is_forbidden_address);
        let mut http = HttpConnector::new_with_resolver(resolver);
        http.enforce_http(false);
        http.set_connect_timeout(Some(CONNECT_TIMEOUT));
        http.set_nodelay(true);
        let connector = HttpsConnectorBuilder::new()
            .with_tls_config(config)
            .https_only()
            .enable_http1()
            .wrap_connector(http);
        let client = Client::builder(TokioExecutor::new())
            .pool_idle_timeout(POOL_IDLE_TIMEOUT)
            .pool_max_idle_per_host(POOL_MAX_IDLE_PER_HOST)
            .pool_timer(TokioTimer::new())
            .build(connector);
        Ok(Self {
            client,
            host: format!("monitoring.{region}.amazonaws.com"),
            region: region.to_owned(),
            user_agent: HeaderValue::from_static(concat!("rayd/", env!("CARGO_PKG_VERSION"))),
        })
    }

    /// The headers `send_with` must set beyond `Authorization` itself:
    /// `SigV4` (`ExecutionRole`) signs `x-amz-date`/`x-amz-content-sha256`
    /// (and, with a session token, `x-amz-security-token`) as part of
    /// `SignedHeaders`, so every one of them has to actually reach
    /// `CloudWatch` or the signature it sent never matches what it
    /// receives (a guaranteed 403). `Bearer` signs nothing -- its
    /// `Authorization` header is the whole credential -- so these stay
    /// `None`.
    async fn authorization_header(
        &self,
        credentials: &SinkCredentials,
        payload: &[u8],
    ) -> Result<RequestAuthHeaders, SinkError> {
        match credentials {
            SinkCredentials::Bearer(token) => {
                let value = HeaderValue::from_str(&format!("Bearer {}", token.as_str()))
                    .map_err(|_invalid_header_value| SinkError::Rejected)?;
                Ok(RequestAuthHeaders {
                    authorization: value,
                    session_token: None,
                    sigv4_dates: None,
                })
            }
            SinkCredentials::ExecutionRole(broker) => {
                let leased = broker
                    .ensure()
                    .await
                    .map_err(|_unavailable| SinkError::Network)?;
                let signed = sign(&SigningRequest {
                    method: "POST",
                    host: &self.host,
                    path: OTLP_METRICS_PATH,
                    payload,
                    content_type: CONTENT_TYPE,
                    region: &self.region,
                    service: SIGV4_SERVICE,
                    access_key_id: &leased.access_key_id,
                    secret_access_key: leased.secret_access_key.as_str(),
                    session_token: leased.session_token.as_ref().map(|token| token.as_str()),
                    timestamp_unix: unix_seconds(SystemTime::now()),
                });
                let authorization = HeaderValue::from_str(&signed.authorization)
                    .map_err(|_invalid_header_value| SinkError::Rejected)?;
                let token_header = leased
                    .session_token
                    .as_ref()
                    .map(|token| HeaderValue::from_str(token.as_str()))
                    .transpose()
                    .map_err(|_invalid_header_value| SinkError::Rejected)?;
                let x_amz_date = HeaderValue::from_str(&signed.x_amz_date)
                    .map_err(|_invalid_header_value| SinkError::Rejected)?;
                let x_amz_content_sha256 = HeaderValue::from_str(&signed.x_amz_content_sha256)
                    .map_err(|_invalid_header_value| SinkError::Rejected)?;
                Ok(RequestAuthHeaders {
                    authorization,
                    session_token: token_header,
                    sigv4_dates: Some((x_amz_date, x_amz_content_sha256)),
                })
            }
        }
    }
}

/// What `authorization_header` computes and `send_with` sets on the
/// outgoing request; see `authorization_header`'s own doc comment for why
/// `sigv4_dates` is only ever `Some` for `ExecutionRole`.
struct RequestAuthHeaders {
    authorization: HeaderValue,
    session_token: Option<HeaderValue>,
    sigv4_dates: Option<(HeaderValue, HeaderValue)>,
}

fn unix_seconds(timestamp: SystemTime) -> u64 {
    timestamp
        .duration_since(SystemTime::UNIX_EPOCH)
        .map_or(0, |elapsed| elapsed.as_secs())
}

fn gzip(payload: &[u8]) -> Vec<u8> {
    let mut encoder = GzEncoder::new(Vec::new(), Compression::default());
    // Writing to a `Vec<u8>`-backed encoder cannot fail; the lints deny
    // `unwrap`/`expect`/`panic`, so a failure (which never happens here)
    // just yields an empty body rather than being asserted away.
    let _ = encoder.write_all(payload);
    encoder.finish().unwrap_or_default()
}

/// One send attempt: this struct is generic over its credentials, not over
/// `SinkCredentials` being `Clone`-cheap — `send` takes `&self` so the same
/// sink instance is reused across ticks, but which credentials to sign
/// with can change if the section is re-applied, so the caller
/// (`features::telemetry_export`) holds the current `SinkCredentials`
/// alongside this sink and passes it fresh each call via `send_with`.
impl CloudWatchOtlpSink {
    pub async fn send_with(
        &self,
        credentials: &SinkCredentials,
        payload: Vec<u8>,
    ) -> Result<(), SinkError> {
        let request = self.build_request(credentials, payload).await?;
        let response = tokio::time::timeout(REQUEST_TIMEOUT, self.client.request(request))
            .await
            .map_err(|_elapsed| SinkError::Network)?
            .map_err(|_connect_or_io_error| SinkError::Network)?;
        let status = response.status();
        // Drain the body so the connection returns to the pool; the OTLP
        // endpoint's response body (if any) is never inspected.
        let _ = response.into_body().collect().await;
        if status.is_success() {
            Ok(())
        } else {
            Err(SinkError::Rejected)
        }
    }

    /// Everything `send_with` does up to (not including) actually opening
    /// the connection: split out so a test can inspect exactly which
    /// headers a given `SinkCredentials` produces without a real network
    /// call (the critical bug this guards against: a signature computed
    /// over headers the request never actually carried).
    async fn build_request(
        &self,
        credentials: &SinkCredentials,
        payload: Vec<u8>,
    ) -> Result<Request<Full<Bytes>>, SinkError> {
        let compressed = gzip(&payload);
        let auth = self.authorization_header(credentials, &compressed).await?;
        let uri: Uri = format!("https://{}{OTLP_METRICS_PATH}", self.host)
            .parse()
            .map_err(|_invalid_uri| SinkError::Rejected)?;
        let mut builder = Request::post(uri)
            .header(header::HOST, self.host.as_str())
            .header(header::USER_AGENT, self.user_agent.clone())
            .header(header::CONTENT_TYPE, CONTENT_TYPE)
            .header(header::CONTENT_ENCODING, "gzip")
            .header(header::AUTHORIZATION, auth.authorization);
        if let Some(token) = auth.session_token {
            builder = builder.header(SESSION_TOKEN_HEADER, token);
        }
        if let Some((x_amz_date, x_amz_content_sha256)) = auth.sigv4_dates {
            builder = builder
                .header(X_AMZ_DATE_HEADER, x_amz_date)
                .header(X_AMZ_CONTENT_SHA256_HEADER, x_amz_content_sha256);
        }
        builder
            .body(Full::new(Bytes::from(compressed)))
            .map_err(|_invalid_request| SinkError::Rejected)
    }
}

/// Adapts one fixed `SinkCredentials` choice to `rayd_core::telemetry::TelemetrySink`
/// (the port is generic, not `dyn`-based, so `features::telemetry_export`
/// holds the concrete pair `(CloudWatchOtlpSink, Arc<Mutex<SinkCredentials>>)`
/// if credentials can change, or this fixed-credentials wrapper otherwise).
pub struct FixedCredentialsSink {
    sink: Arc<CloudWatchOtlpSink>,
    credentials: SinkCredentials,
}

impl FixedCredentialsSink {
    #[must_use]
    pub fn new(sink: Arc<CloudWatchOtlpSink>, credentials: SinkCredentials) -> Self {
        Self { sink, credentials }
    }
}

impl TelemetrySink for FixedCredentialsSink {
    async fn send(&self, payload: Vec<u8>) -> Result<(), SinkError> {
        self.sink.send_with(&self.credentials, payload).await
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn gzip_round_trips_through_flate2s_own_reader() {
        use std::io::Read as _;

        use flate2::read::GzDecoder;

        let compressed = gzip(b"hola mundo");
        let mut decoder = GzDecoder::new(compressed.as_slice());
        let mut decompressed = Vec::new();
        decoder.read_to_end(&mut decompressed).unwrap_or_default();
        assert_eq!(decompressed, b"hola mundo");
    }

    #[test]
    fn gzip_of_an_empty_payload_is_still_valid_gzip() {
        use std::io::Read as _;

        use flate2::read::GzDecoder;

        let compressed = gzip(b"");
        let mut decoder = GzDecoder::new(compressed.as_slice());
        let mut decompressed = Vec::new();
        decoder.read_to_end(&mut decompressed).unwrap_or_default();
        assert!(decompressed.is_empty());
    }

    #[test]
    fn the_host_is_built_from_the_region() {
        let sink = CloudWatchOtlpSink::new("us-east-1").unwrap_or_else(|_| {
            panic!("the native trust store must be available in CI and in the Lima VM")
        });
        assert_eq!(sink.host, "monitoring.us-east-1.amazonaws.com");
    }

    fn sink() -> CloudWatchOtlpSink {
        CloudWatchOtlpSink::new("us-east-1").unwrap_or_else(|_| {
            panic!("the native trust store must be available in CI and in the Lima VM")
        })
    }

    fn seeded_execution_role() -> SinkCredentials {
        use crate::adapters::credential_broker::GuestCredentials;

        SinkCredentials::ExecutionRole(Arc::new(ImdsCredentialBroker::seeded_for_test(
            GuestCredentials {
                access_key_id: "AKIDEXAMPLE".to_owned(),
                secret_access_key: Zeroizing::new(
                    "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY".to_owned(),
                ),
                session_token: Some(Zeroizing::new("a-session-token".to_owned())),
                expires_at: SystemTime::now() + Duration::from_secs(3600),
            },
        )))
    }

    /// The critical bug this guards against: `sign()` computes a signature
    /// whose `SignedHeaders` names `host`, `x-amz-content-sha256`,
    /// `x-amz-date` and (with a token) `x-amz-security-token`, so every one
    /// of those headers must actually be set on the request `send_with`
    /// builds -- a signature `CloudWatch` cannot recompute from the headers
    /// it actually receives is a guaranteed 403.
    #[tokio::test]
    async fn an_execution_role_request_carries_every_header_its_own_signature_names() {
        let request = sink()
            .build_request(&seeded_execution_role(), b"metrics".to_vec())
            .await
            .expect("a seeded, always-fresh broker never fails to sign");
        let headers = request.headers();
        assert!(headers.contains_key(http::header::HOST));
        assert!(headers.contains_key(X_AMZ_DATE_HEADER));
        assert!(headers.contains_key(X_AMZ_CONTENT_SHA256_HEADER));
        assert!(headers.contains_key(SESSION_TOKEN_HEADER));
        let authorization = headers
            .get(http::header::AUTHORIZATION)
            .expect("authorization header")
            .to_str()
            .unwrap_or_default();
        assert!(
            authorization.contains(
                "SignedHeaders=host;x-amz-content-sha256;x-amz-date;x-amz-security-token"
            )
        );
    }

    /// A bearer-authenticated request never claims a `SigV4` signature it
    /// never computed: no `x-amz-date`/`x-amz-content-sha256`, since
    /// nothing signs them.
    #[tokio::test]
    async fn a_bearer_request_carries_no_sigv4_date_headers() {
        let request = sink()
            .build_request(
                &SinkCredentials::Bearer(Zeroizing::new("sk-test".to_owned())),
                b"metrics".to_vec(),
            )
            .await
            .expect("bearer auth never fails to build a header value from a plain token");
        let headers = request.headers();
        assert!(!headers.contains_key(X_AMZ_DATE_HEADER));
        assert!(!headers.contains_key(X_AMZ_CONTENT_SHA256_HEADER));
        assert!(!headers.contains_key(SESSION_TOKEN_HEADER));
        assert_eq!(
            headers
                .get(http::header::AUTHORIZATION)
                .and_then(|value| value.to_str().ok()),
            Some("Bearer sk-test")
        );
    }
}
