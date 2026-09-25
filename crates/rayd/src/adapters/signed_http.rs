//! `SignedHttp` over hyper-util's legacy client (design D7, ADR-010):
//! HTTPS only, HTTP/1.1 only, the `aws-lc-rs` provider named explicitly so
//! no process-wide default is installed (the S3 store's client is
//! unaffected), the OS trust store, no redirects, a 5 s connect timeout
//! and a 30 s idle timeout shared by both directions (a long `PUT` is never
//! cut by a total-duration timer). `FilteringResolver` drops every
//! resolved address `is_forbidden_address` refuses (loopback, link-local,
//! IMDS, ...) and the connector only dials what it returned, so there is no
//! check-then-connect gap; an IP-literal host, which the connector would
//! dial without resolving, goes through the same predicate. The request
//! carries `host`, `user-agent` and, when set, `content-type` and
//! `content-length`: nothing else. No URL, host or address ever reaches an
//! error or a log line.

use std::error::Error as StdError;
use std::fmt;
use std::future::Future;
use std::io;
use std::net::{IpAddr, SocketAddr};
use std::pin::Pin;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::task::{Context, Poll};
use std::time::Duration;

use bytes::Bytes;
use http::{HeaderMap, HeaderValue, Method, Request, Uri, header};
use http_body::{Body, Frame, SizeHint};
use http_body_util::BodyExt;
use hyper::body::Incoming;
use hyper_rustls::{ConfigBuilderExt, HttpsConnector, HttpsConnectorBuilder};
use hyper_util::client::legacy::Client;
use hyper_util::client::legacy::connect::HttpConnector;
use hyper_util::client::legacy::connect::dns::{GaiResolver, Name};
use hyper_util::rt::{TokioExecutor, TokioTimer};
use rayd_core::transfer::{
    HttpError, HttpErrorKind, HttpHead, HttpMethod, RequestBody, ResponseBody, SignedHttp,
    SignedRequest, TRANSFER_CONNECT_TIMEOUT, TRANSFER_IDLE_TIMEOUT, is_forbidden_address,
};
use rustls::ClientConfig;
use tokio::sync::mpsc;
use tokio::time::Instant;
use tower::Service;

pub const POOL_IDLE_TIMEOUT: Duration = Duration::from_secs(30);
pub const POOL_MAX_IDLE_PER_HOST: usize = 2;
/// Request-body chunks buffered between the producer and hyper.
const BODY_QUEUE: usize = 2;
const AMZ_REQUEST_ID: &str = "x-amz-request-id";

/// Every address the host resolved to is one `rayd` refuses to reach.
#[derive(Debug, Clone, Copy)]
pub struct ForbiddenAddressError;

impl fmt::Display for ForbiddenAddressError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("forbidden_address")
    }
}

impl StdError for ForbiddenAddressError {}

/// A resolver that keeps only the addresses `forbidden` allows and fails
/// when none is left.
#[derive(Clone)]
pub struct FilteringResolver<R> {
    inner: R,
    forbidden: fn(IpAddr) -> bool,
}

impl FilteringResolver<GaiResolver> {
    /// `getaddrinfo` behind the production predicate.
    #[must_use]
    pub fn system() -> Self {
        Self::new(GaiResolver::new(), is_forbidden_address)
    }
}

impl<R> FilteringResolver<R> {
    #[must_use]
    pub fn new(inner: R, forbidden: fn(IpAddr) -> bool) -> Self {
        Self { inner, forbidden }
    }
}

type ResolveFuture =
    Pin<Box<dyn Future<Output = Result<std::vec::IntoIter<SocketAddr>, BoxError>> + Send>>;
type BoxError = Box<dyn StdError + Send + Sync>;

impl<R> Service<Name> for FilteringResolver<R>
where
    R: Service<Name>,
    R::Response: Iterator<Item = SocketAddr>,
    R::Error: Into<BoxError>,
    R::Future: Send + 'static,
{
    type Response = std::vec::IntoIter<SocketAddr>;
    type Error = BoxError;
    type Future = ResolveFuture;

    fn poll_ready(&mut self, cx: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
        self.inner.poll_ready(cx).map_err(Into::into)
    }

    fn call(&mut self, name: Name) -> Self::Future {
        let resolving = self.inner.call(name);
        let forbidden = self.forbidden;
        Box::pin(async move {
            let allowed: Vec<SocketAddr> = resolving
                .await
                .map_err(Into::into)?
                .filter(|address| !forbidden(address.ip()))
                .collect();
            if allowed.is_empty() {
                Err(Box::new(ForbiddenAddressError) as BoxError)
            } else {
                Ok(allowed.into_iter())
            }
        })
    }
}

/// The native trust store could not be loaded.
#[derive(Debug)]
pub struct SignedHttpInitError(io::Error);

impl fmt::Display for SignedHttpInitError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "trust store unavailable: {}", self.0.kind())
    }
}

impl StdError for SignedHttpInitError {}

type Connector<R> = HttpsConnector<HttpConnector<FilteringResolver<R>>>;

pub struct HyperSignedHttp<R = GaiResolver> {
    client: Client<Connector<R>, OutgoingBody>,
    forbidden: fn(IpAddr) -> bool,
    idle_timeout: Duration,
    user_agent: HeaderValue,
}

impl HyperSignedHttp {
    /// The production client: OS roots, `getaddrinfo` behind the address
    /// filter, the D7 timeouts.
    pub fn new() -> Result<Self, SignedHttpInitError> {
        let config = ClientConfig::builder_with_provider(Arc::new(
            rustls::crypto::aws_lc_rs::default_provider(),
        ))
        .with_safe_default_protocol_versions()
        .map_err(|error| SignedHttpInitError(io::Error::other(error)))?
        .with_native_roots()
        .map_err(SignedHttpInitError)?
        .with_no_client_auth();
        Ok(Self::build(
            config,
            FilteringResolver::system(),
            TRANSFER_IDLE_TIMEOUT,
        ))
    }
}

impl<R> HyperSignedHttp<R>
where
    R: Service<Name> + Clone + Send + Sync + 'static,
    R::Response: Iterator<Item = SocketAddr>,
    R::Error: Into<BoxError>,
    R::Future: Send + 'static,
{
    fn build(config: ClientConfig, resolver: FilteringResolver<R>, idle_timeout: Duration) -> Self {
        let forbidden = resolver.forbidden;
        let mut http = HttpConnector::new_with_resolver(resolver);
        http.enforce_http(false);
        http.set_connect_timeout(Some(TRANSFER_CONNECT_TIMEOUT));
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
        Self {
            client,
            forbidden,
            idle_timeout,
            user_agent: HeaderValue::from_static(concat!("rayd/", env!("CARGO_PKG_VERSION"))),
        }
    }

    /// The connector dials an IP-literal host without asking the resolver.
    fn check_literal_host(&self, uri: &Uri) -> Result<(), HttpError> {
        let literal = uri
            .host()
            .map(|host| host.trim_start_matches('[').trim_end_matches(']'))
            .and_then(|host| host.parse::<IpAddr>().ok());
        match literal {
            Some(address) if (self.forbidden)(address) => {
                Err(HttpError::new(HttpErrorKind::ForbiddenAddress))
            }
            _ => Ok(()),
        }
    }

    fn build_request(
        &self,
        request: &SignedRequest,
        uri: Uri,
        body: OutgoingBody,
    ) -> Result<Request<OutgoingBody>, HttpError> {
        let mut built = Request::builder()
            .method(method(request.method))
            .uri(uri)
            .header(header::USER_AGENT, self.user_agent.clone());
        if let Some(content_type) = request.content_type {
            built = built.header(header::CONTENT_TYPE, content_type);
        }
        if let Some(length) = request.content_length {
            built = built.header(header::CONTENT_LENGTH, length);
        }
        built
            .body(body)
            .map_err(|_| HttpError::new(HttpErrorKind::Io))
    }
}

impl<R> SignedHttp for HyperSignedHttp<R>
where
    R: Service<Name> + Clone + Send + Sync + 'static,
    R::Response: Iterator<Item = SocketAddr>,
    R::Error: Into<BoxError>,
    R::Future: Send + 'static,
{
    type Body = HyperResponseBody;

    /// A 3xx is `Redirect` (never followed); the idle timer runs from the
    /// first byte of the connection to the response head.
    async fn send<B: RequestBody>(
        &self,
        request: SignedRequest,
        body: Option<B>,
    ) -> Result<(HttpHead, Self::Body), HttpError> {
        let activity = Activity::new();
        let uri =
            Uri::try_from(request.url.as_str()).map_err(|_| HttpError::new(HttpErrorKind::Io))?;
        self.check_literal_host(&uri)?;
        let outgoing = match body {
            Some(body) => OutgoingBody::pumped(body, request.content_length, activity.clone()),
            None => OutgoingBody::Empty,
        };
        let http_request = self.build_request(&request, uri, outgoing)?;
        drop(request);
        let response = tokio::select! {
            response = self.client.request(http_request) => {
                response.map_err(|error| classify_client_error(&error))?
            }
            () = activity.stalled(self.idle_timeout) => {
                return Err(HttpError::new(HttpErrorKind::Timeout));
            }
        };
        activity.touch();
        if response.status().is_redirection() {
            return Err(HttpError::new(HttpErrorKind::Redirect));
        }
        let head = response_head(response.status().as_u16(), response.headers());
        Ok((
            head,
            HyperResponseBody {
                incoming: response.into_body(),
                idle_timeout: self.idle_timeout,
            },
        ))
    }
}

fn method(method: HttpMethod) -> Method {
    match method {
        HttpMethod::Get => Method::GET,
        HttpMethod::Put => Method::PUT,
        HttpMethod::Delete => Method::DELETE,
    }
}

fn response_head(status: u16, headers: &HeaderMap) -> HttpHead {
    let text = |name: &str| {
        headers
            .get(name)
            .and_then(|value| value.to_str().ok())
            .map(str::to_owned)
    };
    HttpHead {
        status,
        content_length: text(header::CONTENT_LENGTH.as_str()).and_then(|value| value.parse().ok()),
        etag: text(header::ETAG.as_str()),
        request_id: text(AMZ_REQUEST_ID),
    }
}

/// The chain of a client error names what failed: the address filter, the
/// TLS handshake, the connect timer, or else the connection or the I/O.
fn classify_client_error(error: &hyper_util::client::legacy::Error) -> HttpError {
    let mut source: Option<&(dyn StdError + 'static)> = Some(error);
    while let Some(current) = source {
        if let Some(kind) = kind_of(current) {
            return HttpError::new(kind);
        }
        source = current.source();
    }
    HttpError::new(if error.is_connect() {
        HttpErrorKind::Connect
    } else {
        HttpErrorKind::Io
    })
}

fn kind_of(error: &(dyn StdError + 'static)) -> Option<HttpErrorKind> {
    if error.is::<ForbiddenAddressError>() {
        return Some(HttpErrorKind::ForbiddenAddress);
    }
    if error.is::<rustls::Error>() {
        return Some(HttpErrorKind::Tls);
    }
    let io_error = error.downcast_ref::<io::Error>()?;
    if io_error.kind() == io::ErrorKind::TimedOut {
        return Some(HttpErrorKind::Timeout);
    }
    io_error.get_ref().and_then(|inner| kind_of(inner))
}

/// The last moment a byte moved in either direction, shared by the request
/// body, the wait for the head and the response body.
#[derive(Clone)]
struct Activity {
    origin: Instant,
    last_ms: Arc<AtomicU64>,
}

impl Activity {
    fn new() -> Self {
        Self {
            origin: Instant::now(),
            last_ms: Arc::new(AtomicU64::new(0)),
        }
    }

    fn touch(&self) {
        let elapsed = u64::try_from(self.origin.elapsed().as_millis()).unwrap_or(u64::MAX);
        self.last_ms.fetch_max(elapsed, Ordering::Relaxed);
    }

    fn last(&self) -> Instant {
        self.origin + Duration::from_millis(self.last_ms.load(Ordering::Relaxed))
    }

    /// Completes once `idle` passed without a `touch`.
    async fn stalled(&self, idle: Duration) {
        loop {
            let deadline = self.last() + idle;
            if Instant::now() >= deadline {
                return;
            }
            tokio::time::sleep_until(deadline).await;
        }
    }
}

/// An empty body (no `content-length` goes out for it), or chunks pumped
/// from a `RequestBody` by a task that stops when hyper drops the body.
enum OutgoingBody {
    Empty,
    Pumped {
        chunks: mpsc::Receiver<Result<Bytes, HttpError>>,
        length: Option<u64>,
        activity: Activity,
    },
}

impl OutgoingBody {
    fn pumped<B: RequestBody>(mut body: B, length: Option<u64>, activity: Activity) -> Self {
        let (sender, chunks) = mpsc::channel(BODY_QUEUE);
        tokio::spawn(async move {
            loop {
                let next = body.next_chunk().await;
                let finished = !matches!(next, Ok(Some(_)));
                let sent = match next {
                    Ok(Some(chunk)) => sender.send(Ok(chunk)).await,
                    Ok(None) => Ok(()),
                    Err(error) => sender.send(Err(error)).await,
                };
                if finished || sent.is_err() {
                    return;
                }
            }
        });
        Self::Pumped {
            chunks,
            length,
            activity,
        }
    }
}

/// A body chunk that could not be produced; hyper aborts the request.
#[derive(Debug)]
struct BodyError(HttpErrorKind);

impl fmt::Display for BodyError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.0.as_str())
    }
}

impl StdError for BodyError {}

impl Body for OutgoingBody {
    type Data = Bytes;
    type Error = BodyError;

    fn poll_frame(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Bytes>, BodyError>>> {
        match self.get_mut() {
            Self::Empty => Poll::Ready(None),
            Self::Pumped {
                chunks, activity, ..
            } => match chunks.poll_recv(cx) {
                Poll::Ready(Some(Ok(chunk))) => {
                    activity.touch();
                    Poll::Ready(Some(Ok(Frame::data(chunk))))
                }
                Poll::Ready(Some(Err(error))) => Poll::Ready(Some(Err(BodyError(error.kind)))),
                Poll::Ready(None) => Poll::Ready(None),
                Poll::Pending => Poll::Pending,
            },
        }
    }

    fn is_end_stream(&self) -> bool {
        match self {
            Self::Empty => true,
            Self::Pumped { length, .. } => *length == Some(0),
        }
    }

    fn size_hint(&self) -> SizeHint {
        match self {
            Self::Empty => SizeHint::with_exact(0),
            Self::Pumped {
                length: Some(length),
                ..
            } => SizeHint::with_exact(*length),
            Self::Pumped { length: None, .. } => SizeHint::default(),
        }
    }
}

/// The response body, each frame under the idle timeout.
pub struct HyperResponseBody {
    incoming: Incoming,
    idle_timeout: Duration,
}

impl ResponseBody for HyperResponseBody {
    async fn next_chunk(&mut self) -> Result<Option<Bytes>, HttpError> {
        loop {
            let frame = tokio::time::timeout(self.idle_timeout, self.incoming.frame())
                .await
                .map_err(|_| HttpError::new(HttpErrorKind::Timeout))?;
            match frame {
                None => return Ok(None),
                Some(Err(_)) => return Err(HttpError::new(HttpErrorKind::Io)),
                Some(Ok(frame)) => {
                    if let Ok(data) = frame.into_data() {
                        return Ok(Some(data));
                    }
                }
            }
        }
    }
}

/// A resolver that answers one address for every name: the tests point the
/// S3 host of the committed certificate at a loopback listener.
#[cfg(test)]
#[derive(Clone, Copy)]
struct StaticResolver(IpAddr);

#[cfg(test)]
impl Service<Name> for StaticResolver {
    type Response = std::vec::IntoIter<SocketAddr>;
    type Error = io::Error;
    type Future = std::future::Ready<Result<Self::Response, io::Error>>;

    fn poll_ready(&mut self, _cx: &mut Context<'_>) -> Poll<Result<(), io::Error>> {
        Poll::Ready(Ok(()))
    }

    fn call(&mut self, _name: Name) -> Self::Future {
        std::future::ready(Ok(vec![SocketAddr::new(self.0, 0)].into_iter()))
    }
}

#[cfg(test)]
impl HyperSignedHttp<StaticResolver> {
    /// The test CA as the only root, every name resolved to loopback and
    /// loopback allowed.
    fn for_tests(root_pem: &[u8], idle_timeout: Duration) -> Self {
        use rustls::pki_types::CertificateDer;
        use rustls::pki_types::pem::PemObject;
        let mut roots = rustls::RootCertStore::empty();
        for certificate in CertificateDer::pem_slice_iter(root_pem) {
            roots.add(certificate.unwrap()).unwrap();
        }
        let config = ClientConfig::builder_with_provider(Arc::new(
            rustls::crypto::aws_lc_rs::default_provider(),
        ))
        .with_safe_default_protocol_versions()
        .unwrap()
        .with_root_certificates(roots)
        .with_no_client_auth();
        Self::build(
            config,
            FilteringResolver::new(StaticResolver(IpAddr::from([127, 0, 0, 1])), |_| false),
            idle_timeout,
        )
    }
}

#[cfg(test)]
mod tests {
    use std::sync::{LazyLock, Mutex};

    use rayd_core::transfer::{NoBody, OCTET_STREAM, PresignedUrl};
    use rcgen::{BasicConstraints, CertificateParams, DnType, IsCa, Issuer, KeyPair};
    use rustls::ServerConfig;
    use rustls::pki_types::pem::PemObject;
    use rustls::pki_types::{CertificateDer, PrivateKeyDer};
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;
    use tokio_rustls::TlsAcceptor;

    use super::*;

    const HOST: &str = "amzn-s3-demo-bucket.s3.us-east-1.amazonaws.com";

    /// A test-only P-256 CA and a leaf for `HOST` it signed, as PEM. Made
    /// once per test process, so no private key lives in the repository.
    struct Pki {
        ca: Vec<u8>,
        leaf: Vec<u8>,
        leaf_key: Vec<u8>,
    }

    static PKI: LazyLock<Pki> = LazyLock::new(|| {
        let ca_key = KeyPair::generate().unwrap();
        let mut ca_params = CertificateParams::default();
        ca_params
            .distinguished_name
            .push(DnType::CommonName, "rayito test-only CA");
        ca_params.is_ca = IsCa::Ca(BasicConstraints::Unconstrained);
        let ca = ca_params.self_signed(&ca_key).unwrap();
        let leaf_key = KeyPair::generate().unwrap();
        let mut leaf_params = CertificateParams::new(vec![HOST.to_owned()]).unwrap();
        leaf_params
            .distinguished_name
            .push(DnType::CommonName, HOST);
        let leaf = leaf_params
            .signed_by(&leaf_key, &Issuer::new(ca_params, ca_key))
            .unwrap();
        Pki {
            ca: ca.pem().into_bytes(),
            leaf: leaf.pem().into_bytes(),
            leaf_key: leaf_key.serialize_pem().into_bytes(),
        }
    });

    /// What the fake S3 does with each connection it accepts.
    #[derive(Clone)]
    enum Script {
        Respond(&'static str),
        Stall,
    }

    struct Server {
        port: u16,
        requests: Arc<Mutex<Vec<Vec<u8>>>>,
        accepted: Arc<AtomicU64>,
    }

    fn acceptor() -> TlsAcceptor {
        let chain: Vec<CertificateDer<'static>> = CertificateDer::pem_slice_iter(&PKI.leaf)
            .map(Result::unwrap)
            .collect();
        let key = PrivateKeyDer::from_pem_slice(&PKI.leaf_key).unwrap();
        let config = ServerConfig::builder_with_provider(Arc::new(
            rustls::crypto::aws_lc_rs::default_provider(),
        ))
        .with_safe_default_protocol_versions()
        .unwrap()
        .with_no_client_auth()
        .with_single_cert(chain, key)
        .unwrap();
        TlsAcceptor::from(Arc::new(config))
    }

    async fn serve(script: Script) -> Server {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let requests = Arc::new(Mutex::new(Vec::new()));
        let accepted = Arc::new(AtomicU64::new(0));
        let (seen, count) = (requests.clone(), accepted.clone());
        let acceptor = acceptor();
        tokio::spawn(async move {
            while let Ok((tcp, _)) = listener.accept().await {
                count.fetch_add(1, Ordering::SeqCst);
                let (acceptor, seen, script) = (acceptor.clone(), seen.clone(), script.clone());
                tokio::spawn(async move {
                    let Ok(mut tls) = acceptor.accept(tcp).await else {
                        return;
                    };
                    let request = read_request(&mut tls).await;
                    seen.lock().unwrap().push(request);
                    match script {
                        Script::Respond(response) => {
                            let _ = tls.write_all(response.as_bytes()).await;
                            let _ = tls.shutdown().await;
                        }
                        Script::Stall => tokio::time::sleep(Duration::from_secs(60)).await,
                    }
                });
            }
        });
        Server {
            port,
            requests,
            accepted,
        }
    }

    /// The head up to the blank line plus exactly `content-length` bytes.
    async fn read_request<S: tokio::io::AsyncRead + Unpin>(stream: &mut S) -> Vec<u8> {
        let mut raw = Vec::new();
        let mut byte = [0u8; 1];
        while !raw.ends_with(b"\r\n\r\n") {
            if stream.read(&mut byte).await.unwrap_or(0) == 0 {
                return raw;
            }
            raw.push(byte[0]);
        }
        let head = String::from_utf8_lossy(&raw).to_ascii_lowercase();
        let length = head
            .lines()
            .find_map(|line| line.strip_prefix("content-length: "))
            .and_then(|value| value.trim().parse::<usize>().ok())
            .unwrap_or(0);
        let mut body = vec![0u8; length];
        stream.read_exact(&mut body).await.unwrap();
        raw.extend_from_slice(&body);
        raw
    }

    fn signed(method: HttpMethod, url: String, length: Option<u64>) -> SignedRequest {
        SignedRequest {
            method,
            url: PresignedUrl::new(url, Vec::new()).url,
            content_type: length.map(|_| OCTET_STREAM),
            content_length: length,
        }
    }

    fn request(method: HttpMethod, port: u16, length: Option<u64>) -> SignedRequest {
        signed(
            method,
            format!("https://{HOST}:{port}/rayito-transfer/k?X-Amz-Signature=abc"),
            length,
        )
    }

    struct Chunks(Vec<Bytes>);

    impl RequestBody for Chunks {
        fn next_chunk(&mut self) -> impl Future<Output = Result<Option<Bytes>, HttpError>> + Send {
            std::future::ready(Ok((!self.0.is_empty()).then(|| self.0.remove(0))))
        }
    }

    async fn read_all(body: &mut HyperResponseBody) -> Vec<u8> {
        let mut collected = Vec::new();
        while let Some(chunk) = body.next_chunk().await.unwrap() {
            collected.extend_from_slice(&chunk);
        }
        collected
    }

    fn header_names(raw: &[u8]) -> Vec<String> {
        let text = String::from_utf8_lossy(raw);
        let head = text.split("\r\n\r\n").next().unwrap_or_default();
        let mut names: Vec<String> = head
            .lines()
            .skip(1)
            .filter_map(|line| line.split_once(':'))
            .map(|(name, _)| name.to_ascii_lowercase())
            .collect();
        names.sort();
        names
    }

    #[tokio::test]
    async fn a_get_round_trips_its_body_and_sends_only_host_and_user_agent() {
        let server = serve(Script::Respond(
            "HTTP/1.1 200 OK\r\ncontent-length: 5\r\netag: \"e1\"\r\nx-amz-request-id: R1\r\nconnection: close\r\n\r\nhello",
        ))
        .await;
        let http = HyperSignedHttp::for_tests(&PKI.ca, TRANSFER_IDLE_TIMEOUT);
        let (head, mut body) = http
            .send::<NoBody>(request(HttpMethod::Get, server.port, None), None)
            .await
            .unwrap();
        assert_eq!(head.status, 200);
        assert_eq!(head.content_length, Some(5));
        assert_eq!(head.etag.as_deref(), Some("\"e1\""));
        assert_eq!(head.request_id.as_deref(), Some("R1"));
        assert_eq!(read_all(&mut body).await, b"hello");
        let requests = server.requests.lock().unwrap().clone();
        let text = String::from_utf8_lossy(&requests[0]).into_owned();
        assert!(text.starts_with("GET /rayito-transfer/k?X-Amz-Signature=abc HTTP/1.1\r\n"));
        assert_eq!(header_names(&requests[0]), vec!["host", "user-agent"]);
        assert!(text.contains(&format!("user-agent: rayd/{}", crate::AGENT_VERSION)));
    }

    #[tokio::test]
    async fn a_put_declares_its_exact_length_and_octet_stream() {
        let server = serve(Script::Respond(
            "HTTP/1.1 200 OK\r\ncontent-length: 0\r\netag: \"p1\"\r\nconnection: close\r\n\r\n",
        ))
        .await;
        let http = HyperSignedHttp::for_tests(&PKI.ca, TRANSFER_IDLE_TIMEOUT);
        let body = Chunks(vec![Bytes::from_static(b"abc"), Bytes::from_static(b"de")]);
        let (head, _) = http
            .send(request(HttpMethod::Put, server.port, Some(5)), Some(body))
            .await
            .unwrap();
        assert_eq!(head.etag.as_deref(), Some("\"p1\""));
        let requests = server.requests.lock().unwrap().clone();
        assert!(requests[0].ends_with(b"\r\n\r\nabcde"));
        assert_eq!(
            header_names(&requests[0]),
            vec!["content-length", "content-type", "host", "user-agent"]
        );
        let text = String::from_utf8_lossy(&requests[0]).into_owned();
        assert!(text.contains("content-length: 5\r\n"));
        assert!(text.contains("content-type: application/octet-stream\r\n"));
    }

    #[tokio::test]
    async fn a_redirect_is_never_followed() {
        let server = serve(Script::Respond(
            "HTTP/1.1 302 Found\r\nlocation: https://169.254.169.254/\r\ncontent-length: 0\r\nconnection: close\r\n\r\n",
        ))
        .await;
        let http = HyperSignedHttp::for_tests(&PKI.ca, TRANSFER_IDLE_TIMEOUT);
        let error = http
            .send::<NoBody>(request(HttpMethod::Get, server.port, None), None)
            .await
            .err()
            .unwrap();
        assert_eq!(error.kind, HttpErrorKind::Redirect);
        assert_eq!(server.accepted.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn a_stalled_server_times_out_on_the_idle_timer() {
        let server = serve(Script::Stall).await;
        let http = HyperSignedHttp::for_tests(&PKI.ca, Duration::from_millis(300));
        let started = std::time::Instant::now();
        let error = http
            .send::<NoBody>(request(HttpMethod::Get, server.port, None), None)
            .await
            .err()
            .unwrap();
        assert_eq!(error.kind, HttpErrorKind::Timeout);
        assert!(started.elapsed() < Duration::from_secs(10));
    }

    #[tokio::test]
    async fn an_untrusted_certificate_is_a_tls_error() {
        let server = serve(Script::Respond("HTTP/1.1 200 OK\r\n\r\n")).await;
        let http = HyperSignedHttp::for_tests(unrelated_root(), TRANSFER_IDLE_TIMEOUT);
        let error = http
            .send::<NoBody>(request(HttpMethod::Get, server.port, None), None)
            .await
            .err()
            .unwrap();
        assert_eq!(error.kind, HttpErrorKind::Tls);
    }

    /// A root that did not sign the server's leaf: the leaf itself, whose
    /// subject is not the leaf's issuer, so the chain cannot validate.
    fn unrelated_root() -> &'static [u8] {
        &PKI.leaf
    }

    #[tokio::test]
    async fn the_production_client_refuses_a_name_that_resolves_to_loopback_without_connecting() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let accepted = Arc::new(AtomicU64::new(0));
        let count = accepted.clone();
        tokio::spawn(async move {
            while listener.accept().await.is_ok() {
                count.fetch_add(1, Ordering::SeqCst);
            }
        });
        let http = HyperSignedHttp::new().unwrap();
        for url in [
            format!("https://localhost:{port}/k"),
            format!("https://127.0.0.1:{port}/k"),
            "https://169.254.169.254/latest/meta-data/".to_owned(),
            format!("https://[::1]:{port}/k"),
        ] {
            let request = signed(HttpMethod::Get, url.clone(), None);
            let error = http.send::<NoBody>(request, None).await.err().unwrap();
            assert_eq!(error.kind, HttpErrorKind::ForbiddenAddress, "{url}");
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
        assert_eq!(accepted.load(Ordering::SeqCst), 0);
    }

    #[tokio::test]
    async fn the_filtering_resolver_keeps_only_allowed_addresses() {
        let mut both = FilteringResolver::new(
            StaticResolver(IpAddr::from([10, 0, 0, 5])),
            is_forbidden_address,
        );
        let name: Name = HOST.parse().unwrap();
        let kept: Vec<SocketAddr> = both.call(name.clone()).await.unwrap().collect();
        assert_eq!(kept, vec![SocketAddr::new(IpAddr::from([10, 0, 0, 5]), 0)]);
        let mut imds = FilteringResolver::new(
            StaticResolver(IpAddr::from([169, 254, 169, 254])),
            is_forbidden_address,
        );
        let refused = imds.call(name).await.err().unwrap();
        assert!(refused.is::<ForbiddenAddressError>());
        assert_eq!(refused.to_string(), "forbidden_address");
    }
}
