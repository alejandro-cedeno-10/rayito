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
//!
//! `SigV4` comes from `aws-sigv4` (the signer `aws-sdk-s3` itself uses,
//! already locked at the same version), never a hand-rolled HMAC. Requests
//! are signed with the guest's wall clock shifted by the skew the last AWS
//! response's `Date` header revealed (`ClockSkew`), which is what the AWS
//! SDKs do on their own: a guest clock left behind by a long suspension
//! would otherwise get every export refused until it resyncs.

use std::fmt;
use std::io::{self, Write as _};
use std::sync::Arc;
use std::sync::atomic::{AtomicI64, Ordering};
use std::time::{Duration, SystemTime};

use aws_credential_types::Credentials;
use aws_sigv4::http_request::{SignableBody, SignableRequest, SigningSettings, sign};
use aws_sigv4::sign::v4;
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

use super::credential_broker::{GuestCredentials, ImdsCredentialBroker};
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
/// `aws-credential-types`' `provider_name`: a debugging label only, never
/// sent anywhere; names where the signing identity came from.
const CREDENTIALS_PROVIDER_NAME: &str = "rayd-imds-execution-role";
/// When the guest's wall clock and the `Date` an AWS response carries
/// disagree by more than this, signing shifts its timestamp by the
/// measured difference (`ClockSkew`). Well inside `SigV4`'s 5-minute window
/// (`SigV4-signing.html`: a request whose `x-amz-date` is more than 5
/// minutes off the server's time is refused), so the correction lands
/// before a request is ever rejected for it; far above `Date`'s 1-second
/// resolution plus one request's latency, so a healthy clock is never
/// "corrected".
const SKEW_CORRECTION_THRESHOLD: Duration = Duration::from_secs(60);

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

/// The offset, in milliseconds, between the guest's wall clock and AWS's
/// (server minus guest) that signing applies; zero while the two agree
/// within `SKEW_CORRECTION_THRESHOLD`. Re-measured from every response, so
/// it also returns to zero once the guest clock resyncs.
#[derive(Default)]
struct ClockSkew(AtomicI64);

impl ClockSkew {
    fn signing_time(&self, now: SystemTime) -> SystemTime {
        shifted(now, self.0.load(Ordering::Relaxed))
    }

    /// Records the skew `server_date` reveals against `received_at`;
    /// whether it changed what the next request will be signed with.
    fn observe(&self, server_date: SystemTime, received_at: SystemTime) -> bool {
        let measured = skew_to_apply(server_date, received_at);
        self.0.swap(measured, Ordering::Relaxed) != measured
    }
}

/// Server minus guest, in milliseconds, or zero when within
/// `SKEW_CORRECTION_THRESHOLD`.
fn skew_to_apply(server_date: SystemTime, local: SystemTime) -> i64 {
    let (magnitude, server_ahead) = match server_date.duration_since(local) {
        Ok(ahead) => (ahead, true),
        Err(behind) => (behind.duration(), false),
    };
    if magnitude <= SKEW_CORRECTION_THRESHOLD {
        return 0;
    }
    let millis = i64::try_from(magnitude.as_millis()).unwrap_or(i64::MAX);
    if server_ahead { millis } else { -millis }
}

fn shifted(now: SystemTime, offset_ms: i64) -> SystemTime {
    let offset = Duration::from_millis(offset_ms.unsigned_abs());
    if offset_ms >= 0 {
        now.checked_add(offset).unwrap_or(now)
    } else {
        now.checked_sub(offset).unwrap_or(now)
    }
}

fn server_date(headers: &http::HeaderMap) -> Option<SystemTime> {
    let value = headers.get(header::DATE)?.to_str().ok()?;
    httpdate::parse_http_date(value).ok()
}

pub struct CloudWatchOtlpSink {
    client: Client<
        hyper_rustls::HttpsConnector<HttpConnector<FilteringResolver<GaiResolver>>>,
        Full<Bytes>,
    >,
    host: String,
    region: String,
    user_agent: HeaderValue,
    skew: ClockSkew,
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
            skew: ClockSkew::default(),
        })
    }

    /// A brand-new sink (and therefore a brand-new connection pool) for the
    /// same region, carrying over the clock skew measured so far:
    /// `features::telemetry_export`'s `/resume` rebuild.
    pub fn rebuilt(&self) -> Result<Self, SinkInitError> {
        let fresh = Self::new(&self.region)?;
        fresh
            .skew
            .0
            .store(self.skew.0.load(Ordering::Relaxed), Ordering::Relaxed);
        Ok(fresh)
    }

    /// Signs `request` in place with `SigV4` (`aws-sigv4`, service
    /// `monitoring`) over every header it already carries except the ones
    /// `SigningSettings::default()` excludes (`user-agent`, ...); adds
    /// `x-amz-date`, `authorization` and, for temporary credentials,
    /// `x-amz-security-token`. The payload is the exact (gzipped) body sent.
    fn sign_execution_role(
        &self,
        request: &mut Request<Full<Bytes>>,
        body: &[u8],
        leased: &GuestCredentials,
    ) -> Result<(), SinkError> {
        let identity = Credentials::new(
            leased.access_key_id.as_str(),
            leased.secret_access_key.as_str(),
            leased
                .session_token
                .as_ref()
                .map(|token| token.as_str().to_owned()),
            Some(leased.expires_at),
            CREDENTIALS_PROVIDER_NAME,
        )
        .into();
        let params = v4::SigningParams::builder()
            .identity(&identity)
            .region(&self.region)
            .name(SIGV4_SERVICE)
            .time(self.skew.signing_time(SystemTime::now()))
            .settings(SigningSettings::default())
            .build()
            .map_err(|_incomplete_params| SinkError::Rejected)?
            .into();
        let uri = request.uri().to_string();
        let signable = SignableRequest::new(
            request.method().as_str(),
            uri,
            request
                .headers()
                .iter()
                .filter_map(|(name, value)| Some((name.as_str(), value.to_str().ok()?))),
            SignableBody::Bytes(body),
        )
        .map_err(|_unsignable| SinkError::Rejected)?;
        let (instructions, _signature) = sign(signable, &params)
            .map_err(|_unsignable| SinkError::Rejected)?
            .into_parts();
        instructions.apply_to_request_http1x(request);
        Ok(())
    }
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
    /// A non-2xx answer is `Rejected` (not retried until the next tick's
    /// backoff) unless its `Date` header just changed the clock skew this
    /// sink signs with: then the refusal was most likely the stale guest
    /// clock (`SigV4`'s 5-minute window) and the next attempt, signed with
    /// the corrected time, should succeed, so it is reported as a
    /// retryable `Network` failure instead.
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
        let skew_changed = server_date(response.headers())
            .is_some_and(|date| self.skew.observe(date, SystemTime::now()));
        // Drain the body so the connection returns to the pool; the OTLP
        // endpoint's response body (if any) is never inspected.
        let _ = response.into_body().collect().await;
        if status.is_success() {
            Ok(())
        } else if skew_changed {
            Err(SinkError::Network)
        } else {
            Err(SinkError::Rejected)
        }
    }

    /// Everything `send_with` does up to (not including) actually opening
    /// the connection: split out so a test can inspect exactly which
    /// headers a given `SinkCredentials` produces without a real network
    /// call.
    async fn build_request(
        &self,
        credentials: &SinkCredentials,
        payload: Vec<u8>,
    ) -> Result<Request<Full<Bytes>>, SinkError> {
        let compressed = Bytes::from(gzip(&payload));
        let uri: Uri = format!("https://{}{OTLP_METRICS_PATH}", self.host)
            .parse()
            .map_err(|_invalid_uri| SinkError::Rejected)?;
        let mut request = Request::post(uri)
            .header(header::HOST, self.host.as_str())
            .header(header::USER_AGENT, self.user_agent.clone())
            .header(header::CONTENT_TYPE, CONTENT_TYPE)
            .header(header::CONTENT_ENCODING, "gzip")
            .body(Full::new(compressed.clone()))
            .map_err(|_invalid_request| SinkError::Rejected)?;
        match credentials {
            SinkCredentials::Bearer(token) => {
                let value = HeaderValue::from_str(&format!("Bearer {}", token.as_str()))
                    .map_err(|_invalid_header_value| SinkError::Rejected)?;
                request.headers_mut().insert(header::AUTHORIZATION, value);
            }
            SinkCredentials::ExecutionRole(broker) => {
                let leased = broker
                    .ensure()
                    .await
                    .map_err(|_unavailable| SinkError::Network)?;
                self.sign_execution_role(&mut request, &compressed, &leased)?;
            }
        }
        Ok(request)
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

    const X_AMZ_DATE_HEADER: &str = "x-amz-date";
    const SESSION_TOKEN_HEADER: &str = "x-amz-security-token";

    fn header<'a>(request: &'a Request<Full<Bytes>>, name: &str) -> &'a str {
        request
            .headers()
            .get(name)
            .and_then(|value| value.to_str().ok())
            .unwrap_or_default()
    }

    /// Every header the signature names must actually be on the request
    /// `send_with` sends: a signature `CloudWatch` cannot recompute from the
    /// headers it receives is a guaranteed 403.
    #[tokio::test]
    async fn an_execution_role_request_carries_every_header_its_own_signature_names() {
        let request = sink()
            .build_request(&seeded_execution_role(), b"metrics".to_vec())
            .await
            .expect("a seeded, always-fresh broker never fails to sign");
        let authorization = header(&request, "authorization");
        assert!(authorization.starts_with("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/"));
        assert!(authorization.contains("/us-east-1/monitoring/aws4_request"));
        let signed = authorization
            .split("SignedHeaders=")
            .nth(1)
            .and_then(|rest| rest.split(',').next())
            .unwrap_or_default();
        for name in signed.split(';') {
            assert!(
                request.headers().contains_key(name),
                "{name} is signed but not sent"
            );
        }
        assert!(signed.contains("host"));
        assert!(signed.contains(X_AMZ_DATE_HEADER));
        assert!(signed.contains(SESSION_TOKEN_HEADER));
        assert!(!signed.contains("user-agent"));
        assert_eq!(header(&request, SESSION_TOKEN_HEADER), "a-session-token");
    }

    #[test]
    fn a_clock_within_the_threshold_is_never_corrected() {
        let local = SystemTime::UNIX_EPOCH + Duration::from_secs(1_000_000);
        assert_eq!(skew_to_apply(local + SKEW_CORRECTION_THRESHOLD, local), 0);
        assert_eq!(skew_to_apply(local - SKEW_CORRECTION_THRESHOLD, local), 0);
    }

    #[test]
    fn a_guest_clock_left_behind_is_shifted_forward_by_the_measured_skew() {
        let local = SystemTime::UNIX_EPOCH + Duration::from_secs(1_000_000);
        let ten_minutes = Duration::from_secs(600);
        let skew = ClockSkew::default();
        assert!(skew.observe(local + ten_minutes, local));
        assert_eq!(skew.signing_time(local), local + ten_minutes);
        // Same skew again: nothing changed, so a refusal now is genuine.
        assert!(!skew.observe(local + ten_minutes, local));
    }

    #[test]
    fn a_guest_clock_running_ahead_is_shifted_back() {
        let local = SystemTime::UNIX_EPOCH + Duration::from_secs(1_000_000);
        let ten_minutes = Duration::from_secs(600);
        let skew = ClockSkew::default();
        assert!(skew.observe(local - ten_minutes, local));
        assert_eq!(skew.signing_time(local), local - ten_minutes);
    }

    #[test]
    fn the_correction_returns_to_zero_once_the_guest_clock_resyncs() {
        let local = SystemTime::UNIX_EPOCH + Duration::from_secs(1_000_000);
        let skew = ClockSkew::default();
        skew.observe(local + Duration::from_secs(600), local);
        assert!(skew.observe(local, local));
        assert_eq!(skew.signing_time(local), local);
    }

    /// 2015-08-30T12:36:00Z, the timestamp of AWS's own `SigV4` examples.
    const AWS_EXAMPLE_DATE_SINCE_EPOCH: Duration = Duration::from_mins(24_015_636);

    #[test]
    fn the_date_header_is_parsed_as_an_http_date() {
        let mut headers = http::HeaderMap::new();
        headers.insert(
            header::DATE,
            HeaderValue::from_static("Sun, 30 Aug 2015 12:36:00 GMT"),
        );
        assert_eq!(
            server_date(&headers),
            Some(SystemTime::UNIX_EPOCH + AWS_EXAMPLE_DATE_SINCE_EPOCH)
        );
        assert_eq!(server_date(&http::HeaderMap::new()), None);
    }

    #[test]
    fn a_rebuilt_sink_keeps_the_measured_skew() {
        let original = sink();
        original.skew.0.store(600_000, Ordering::Relaxed);
        let rebuilt = original
            .rebuilt()
            .unwrap_or_else(|_| panic!("the native trust store must be available"));
        assert_eq!(rebuilt.skew.0.load(Ordering::Relaxed), 600_000);
        assert_eq!(rebuilt.host, original.host);
    }

    /// A bearer-authenticated request never claims a `SigV4` signature it
    /// never computed: no `x-amz-date`/`x-amz-security-token`, since
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
        assert!(!headers.contains_key(SESSION_TOKEN_HEADER));
        assert_eq!(
            headers
                .get(http::header::AUTHORIZATION)
                .and_then(|value| value.to_str().ok()),
            Some("Bearer sk-test")
        );
    }
}
