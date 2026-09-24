//! The local forward proxy (design D8, D9): one listener on
//! `127.0.0.1:0`, at most 128 concurrent clients, the first byte telling
//! SOCKS5 (`0x05`) from HTTP/1.x, the policy decision before any byte
//! leaves, and a dial to exactly the checked `SocketAddr` (never
//! re-resolved, so no DNS-rebinding window). Established tunnels are
//! relayed with `copy_bidirectional` and no idle timeout.
//!
//! The policy is read once per connection, so `UpdateNetwork` affects new
//! connections only. Resolution and dialing go through the `Resolve` and
//! `Dial` seams so the tests can use documentation addresses. Nothing
//! about a target is logged; only `DecisionCounters` move.

use std::io;
use std::net::{IpAddr, Ipv4Addr, SocketAddr};
use std::sync::{Arc, PoisonError, RwLock};
use std::time::Duration;

use rayd_core::network::proxy_protocol::{
    HttpProxyRequest, HttpStatus, SocksReply, SocksTarget, http_head_end, http_response,
    parse_http_request, parse_socks_greeting, parse_socks_request, socks_method_selection,
    socks_reply,
};
use rayd_core::network::{
    DenyReason, EgressPolicy, LOCAL_PROXY_MAX_CONNECTIONS, PROXY_CONNECT_TIMEOUT,
    PROXY_HEAD_TIMEOUT, ResolveKind, TargetDecision, TargetGuard, TargetHost, UpstreamProxy,
};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{OwnedSemaphorePermit, Semaphore};
use tokio::task::JoinHandle;

use super::stats::{DecisionCounters, STATS_INTERVAL, spawn_stats_logger};
use super::upstream::connect_via_upstream;

/// How long a client refused for lack of a slot may take to say which
/// protocol it speaks before it is dropped.
const BUSY_REPLY_BUDGET: Duration = Duration::from_secs(2);
const SOCKS_BUFFER_BYTES: usize = 4 + 1 + 255 + 2;

/// Name resolution as the proxy sees it (the system resolver in
/// production).
#[tonic::async_trait]
pub trait Resolve: Send + Sync {
    async fn resolve(&self, host: &str, port: u16) -> io::Result<Vec<IpAddr>>;
}

/// The TCP connect as the proxy sees it; the caller bounds it with the
/// 10 s connect timeout.
#[tonic::async_trait]
pub trait Dial: Send + Sync {
    async fn dial(&self, address: SocketAddr) -> io::Result<TcpStream>;
}

#[derive(Debug, Clone, Copy, Default)]
pub struct SystemResolver;

#[tonic::async_trait]
impl Resolve for SystemResolver {
    async fn resolve(&self, host: &str, port: u16) -> io::Result<Vec<IpAddr>> {
        Ok(tokio::net::lookup_host((host, port))
            .await?
            .map(|address| address.ip())
            .collect())
    }
}

#[derive(Debug, Clone, Copy, Default)]
pub struct SystemDialer;

#[tonic::async_trait]
impl Dial for SystemDialer {
    async fn dial(&self, address: SocketAddr) -> io::Result<TcpStream> {
        TcpStream::connect(address).await
    }
}

#[derive(Clone)]
pub struct ProxySeams {
    pub resolver: Arc<dyn Resolve>,
    pub dialer: Arc<dyn Dial>,
}

impl Default for ProxySeams {
    fn default() -> Self {
        Self {
            resolver: Arc::new(SystemResolver),
            dialer: Arc::new(SystemDialer),
        }
    }
}

/// What one connection is decided with: the policy and the guard built
/// from the guest's current interface addresses.
#[derive(Debug, Clone)]
pub struct ProxyPolicy {
    pub policy: EgressPolicy,
    pub guard: TargetGuard,
}

struct ProxyShared {
    policy: RwLock<Arc<ProxyPolicy>>,
    seams: ProxySeams,
    counters: Arc<DecisionCounters>,
    slots: Arc<Semaphore>,
}

impl ProxyShared {
    fn current_policy(&self) -> Arc<ProxyPolicy> {
        self.policy
            .read()
            .unwrap_or_else(PoisonError::into_inner)
            .clone()
    }
}

/// The running proxy; it lives until `rayd` exits.
pub struct LocalProxy {
    port: u16,
    shared: Arc<ProxyShared>,
    accept: JoinHandle<()>,
    stats: JoinHandle<()>,
}

impl LocalProxy {
    pub async fn start(initial: ProxyPolicy, seams: ProxySeams) -> io::Result<Self> {
        Self::start_with_slots(initial, seams, LOCAL_PROXY_MAX_CONNECTIONS).await
    }

    /// `start` with a smaller connection cap (tests).
    pub async fn start_with_slots(
        initial: ProxyPolicy,
        seams: ProxySeams,
        slots: usize,
    ) -> io::Result<Self> {
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).await?;
        let port = listener.local_addr()?.port();
        let counters = Arc::new(DecisionCounters::default());
        let shared = Arc::new(ProxyShared {
            policy: RwLock::new(Arc::new(initial)),
            seams,
            counters: counters.clone(),
            slots: Arc::new(Semaphore::new(slots)),
        });
        let accept = tokio::spawn(accept_loop(listener, shared.clone()));
        let stats = spawn_stats_logger(counters, STATS_INTERVAL);
        Ok(Self {
            port,
            shared,
            accept,
            stats,
        })
    }

    #[must_use]
    pub fn port(&self) -> u16 {
        self.port
    }

    /// New connections are decided with `policy` from now on.
    pub fn set_policy(&self, policy: ProxyPolicy) {
        *self
            .shared
            .policy
            .write()
            .unwrap_or_else(PoisonError::into_inner) = Arc::new(policy);
    }

    #[must_use]
    pub fn counters(&self) -> Arc<DecisionCounters> {
        self.shared.counters.clone()
    }
}

impl Drop for LocalProxy {
    fn drop(&mut self) {
        self.accept.abort();
        self.stats.abort();
    }
}

async fn accept_loop(listener: TcpListener, shared: Arc<ProxyShared>) {
    loop {
        match listener.accept().await {
            Ok((stream, _)) => {
                let permit = shared.slots.clone().try_acquire_owned().ok();
                tokio::spawn(serve(stream, permit, shared.clone()));
            }
            Err(_) => tokio::time::sleep(Duration::from_millis(50)).await,
        }
    }
}

async fn serve(
    mut client: TcpStream,
    permit: Option<OwnedSemaphorePermit>,
    shared: Arc<ProxyShared>,
) {
    let Some(_permit) = permit else {
        shared.counters.rejected_busy();
        let _ = tokio::time::timeout(BUSY_REPLY_BUDGET, refuse_busy(&mut client)).await;
        return;
    };
    let _active = shared.counters.open_connection();
    let policy = shared.current_policy();
    let Ok(Ok(first)) = tokio::time::timeout(PROXY_HEAD_TIMEOUT, read_byte(&mut client)).await
    else {
        return;
    };
    let mut buffer = vec![first];
    if first == 0x05 {
        serve_socks(client, &mut buffer, &shared, &policy).await;
    } else {
        serve_http(client, &mut buffer, &shared, &policy).await;
    }
}

async fn read_byte<R: AsyncRead + Unpin>(stream: &mut R) -> io::Result<u8> {
    let mut byte = [0u8; 1];
    stream.read_exact(&mut byte).await?;
    Ok(byte[0])
}

/// Reads into `buffer` until `parse` yields a value; `Ok(None)` from the
/// parser means "more bytes", EOF is an error.
async fn read_until<T, E>(
    stream: &mut TcpStream,
    buffer: &mut Vec<u8>,
    limit: usize,
    parse: impl Fn(&[u8]) -> Result<Option<T>, E>,
) -> Result<Result<T, E>, io::Error> {
    loop {
        match parse(buffer) {
            Ok(Some(value)) => return Ok(Ok(value)),
            Err(error) => return Ok(Err(error)),
            Ok(None) => {}
        }
        if buffer.len() >= limit {
            return Err(io::ErrorKind::InvalidData.into());
        }
        let mut chunk = [0u8; 1024];
        let room = (limit - buffer.len()).min(chunk.len());
        let read = stream.read(&mut chunk[..room]).await?;
        if read == 0 {
            return Err(io::ErrorKind::UnexpectedEof.into());
        }
        buffer.extend_from_slice(&chunk[..read]);
    }
}

/// A 503 for an HTTP client once its head is read (closing with unread
/// bytes would reset the connection before the reply is seen); for a
/// SOCKS client the greeting is accepted so the refusal can be the `0x01`
/// reply its request expects.
async fn refuse_busy(client: &mut TcpStream) -> io::Result<()> {
    let first = read_byte(client).await?;
    if first != 0x05 {
        let mut head = vec![first];
        let _ = read_until(client, &mut head, usize::MAX, http_head_end).await?;
        client
            .write_all(&http_response(HttpStatus::ServiceUnavailable))
            .await?;
        return client.shutdown().await;
    }
    let mut buffer = vec![first];
    if read_until(
        client,
        &mut buffer,
        SOCKS_BUFFER_BYTES,
        parse_socks_greeting,
    )
    .await?
    .is_ok()
    {
        client.write_all(&socks_method_selection(true)).await?;
        let mut request = Vec::new();
        let _ = read_until(
            client,
            &mut request,
            SOCKS_BUFFER_BYTES,
            parse_socks_request,
        )
        .await?;
    }
    client
        .write_all(&socks_reply(SocksReply::GeneralFailure))
        .await
}

/// Why a target could not be reached.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ConnectFailure {
    Denied(DenyReason),
    Unresolved,
    Refused,
    Unreachable,
    TimedOut,
    Upstream,
}

impl ConnectFailure {
    fn http_status(self) -> HttpStatus {
        match self {
            Self::Denied(DenyReason::Invalid) => HttpStatus::BadRequest,
            Self::Denied(_) => HttpStatus::Forbidden,
            Self::TimedOut => HttpStatus::GatewayTimeout,
            Self::Unresolved | Self::Refused | Self::Unreachable | Self::Upstream => {
                HttpStatus::BadGateway
            }
        }
    }

    fn socks_reply(self) -> SocksReply {
        match self {
            Self::Denied(_) => SocksReply::NotAllowedByRuleset,
            Self::Unresolved | Self::Unreachable => SocksReply::HostUnreachable,
            Self::Refused => SocksReply::ConnectionRefused,
            Self::TimedOut => SocksReply::TtlExpired,
            Self::Upstream => SocksReply::GeneralFailure,
        }
    }
}

async fn serve_http(
    mut client: TcpStream,
    buffer: &mut Vec<u8>,
    shared: &ProxyShared,
    policy: &ProxyPolicy,
) {
    let head = tokio::time::timeout(
        PROXY_HEAD_TIMEOUT,
        read_until(&mut client, buffer, usize::MAX, http_head_end),
    )
    .await;
    let end = match head {
        Err(_) => return reply_http(&mut client, HttpStatus::RequestTimeout).await,
        Ok(Err(_)) => return,
        Ok(Ok(Err(error))) => return reply_http(&mut client, error.status()).await,
        Ok(Ok(Ok(end))) => end,
    };
    let request = match parse_http_request(&buffer[..end]) {
        Ok(request) => request,
        Err(error) => {
            shared.counters.denied(DenyReason::Invalid);
            return reply_http(&mut client, error.status()).await;
        }
    };
    let leftover = buffer.split_off(end);
    let (host, port, forward_head) = match request {
        HttpProxyRequest::Connect { host, port } => (host, port, None),
        HttpProxyRequest::Forward { host, port, head } => (host, port, Some(head)),
    };
    let decision = policy
        .policy
        .decide(&TargetHost::parse(&host), port, &policy.guard);
    let mut target = match connect(shared, policy, decision, port).await {
        Ok(target) => target,
        Err(failure) => return reply_http(&mut client, failure.http_status()).await,
    };
    let opened = match &forward_head {
        None => {
            client
                .write_all(&http_response(HttpStatus::ConnectionEstablished))
                .await
        }
        Some(head) => target.write_all(head).await,
    };
    if opened.is_err() || target.write_all(&leftover).await.is_err() {
        return;
    }
    let _ = tokio::io::copy_bidirectional(&mut client, &mut target).await;
}

async fn reply_http(client: &mut TcpStream, status: HttpStatus) {
    let _ = client.write_all(&http_response(status)).await;
    let _ = client.shutdown().await;
}

async fn serve_socks(
    mut client: TcpStream,
    buffer: &mut Vec<u8>,
    shared: &ProxyShared,
    policy: &ProxyPolicy,
) {
    let greeting = tokio::time::timeout(
        PROXY_HEAD_TIMEOUT,
        read_until(
            &mut client,
            buffer,
            SOCKS_BUFFER_BYTES,
            parse_socks_greeting,
        ),
    )
    .await;
    let Ok(Ok(Ok((no_auth_offered, used)))) = greeting else {
        return;
    };
    if client
        .write_all(&socks_method_selection(no_auth_offered))
        .await
        .is_err()
        || !no_auth_offered
    {
        return;
    }
    let mut request = buffer.split_off(used);
    let parsed = tokio::time::timeout(
        PROXY_HEAD_TIMEOUT,
        read_until(
            &mut client,
            &mut request,
            SOCKS_BUFFER_BYTES,
            parse_socks_request,
        ),
    )
    .await;
    let (target, used) = match parsed {
        Ok(Ok(Ok(parsed))) => parsed,
        Ok(Ok(Err(reply))) => {
            shared.counters.denied(DenyReason::Invalid);
            let _ = client.write_all(&socks_reply(reply)).await;
            return;
        }
        _ => return,
    };
    let leftover = request.split_off(used);
    let (host, port) = match &target {
        SocksTarget::Ip(address) => (TargetHost::Ip(address.ip()), address.port()),
        SocksTarget::Domain(name, port) => (TargetHost::parse(name), *port),
    };
    let decision = policy.policy.decide(&host, port, &policy.guard);
    let mut upstream = match connect(shared, policy, decision, port).await {
        Ok(upstream) => upstream,
        Err(failure) => {
            let _ = client.write_all(&socks_reply(failure.socks_reply())).await;
            return;
        }
    };
    if client
        .write_all(&socks_reply(SocksReply::Succeeded))
        .await
        .is_err()
        || upstream.write_all(&leftover).await.is_err()
    {
        return;
    }
    let _ = tokio::io::copy_bidirectional(&mut client, &mut upstream).await;
}

/// Carries out the decision; every outcome moves exactly one counter.
async fn connect(
    shared: &ProxyShared,
    policy: &ProxyPolicy,
    decision: TargetDecision,
    port: u16,
) -> Result<TcpStream, ConnectFailure> {
    let result = open_decided(shared, policy, decision, port).await;
    match &result {
        Ok(_) => shared.counters.allowed(),
        Err(ConnectFailure::Denied(reason)) => shared.counters.denied(*reason),
        Err(ConnectFailure::Upstream) => shared.counters.upstream_failed(),
        Err(_) => shared.counters.dial_failed(),
    }
    result
}

async fn open_decided(
    shared: &ProxyShared,
    policy: &ProxyPolicy,
    decision: TargetDecision,
    port: u16,
) -> Result<TcpStream, ConnectFailure> {
    let upstream = policy.policy.upstream();
    match decision {
        TargetDecision::Deny(reason) => Err(ConnectFailure::Denied(reason)),
        TargetDecision::ConnectIp(ip) => open(shared, upstream, &[ip], port).await,
        TargetDecision::ForwardByName(name) => {
            let upstream = upstream.ok_or(ConnectFailure::Upstream)?;
            connect_via_upstream(upstream, &SocksTarget::Domain(name, port), &shared.seams)
                .await
                .map_err(|_| ConnectFailure::Upstream)
        }
        TargetDecision::ResolveByName(name) => {
            resolve_and_open(shared, policy, &name, port, ResolveKind::ByName).await
        }
        TargetDecision::ResolveChecked(name) => {
            resolve_and_open(shared, policy, &name, port, ResolveKind::Checked).await
        }
    }
}

async fn resolve_and_open(
    shared: &ProxyShared,
    policy: &ProxyPolicy,
    name: &str,
    port: u16,
    kind: ResolveKind,
) -> Result<TcpStream, ConnectFailure> {
    let resolved = tokio::time::timeout(
        PROXY_CONNECT_TIMEOUT,
        shared.seams.resolver.resolve(name, port),
    )
    .await
    .map_err(|_| ConnectFailure::Unresolved)?
    .map_err(|_| ConnectFailure::Unresolved)?;
    if resolved.is_empty() {
        return Err(ConnectFailure::Unresolved);
    }
    let kept = policy
        .policy
        .select_addresses(kind, &resolved, &policy.guard)
        .map_err(ConnectFailure::Denied)?;
    open(shared, policy.policy.upstream(), &kept, port).await
}

/// Tries the checked addresses in order: directly, or through the
/// upstream as ATYP IPv4/IPv6 so it cannot resolve to a denied address.
async fn open(
    shared: &ProxyShared,
    upstream: Option<&UpstreamProxy>,
    addresses: &[IpAddr],
    port: u16,
) -> Result<TcpStream, ConnectFailure> {
    let mut last = ConnectFailure::Unreachable;
    for ip in addresses {
        let target = SocketAddr::new(*ip, port);
        let attempt = match upstream {
            Some(upstream) => {
                connect_via_upstream(upstream, &SocksTarget::Ip(target), &shared.seams)
                    .await
                    .map_err(|_| ConnectFailure::Upstream)
            }
            None => dial(shared, target).await,
        };
        match attempt {
            Ok(stream) => return Ok(stream),
            Err(failure) => last = failure,
        }
    }
    Err(last)
}

async fn dial(shared: &ProxyShared, target: SocketAddr) -> Result<TcpStream, ConnectFailure> {
    match tokio::time::timeout(PROXY_CONNECT_TIMEOUT, shared.seams.dialer.dial(target)).await {
        Err(_) => Err(ConnectFailure::TimedOut),
        Ok(Ok(stream)) => Ok(stream),
        Ok(Err(error)) if error.kind() == io::ErrorKind::ConnectionRefused => {
            Err(ConnectFailure::Refused)
        }
        Ok(Err(_)) => Err(ConnectFailure::Unreachable),
    }
}
