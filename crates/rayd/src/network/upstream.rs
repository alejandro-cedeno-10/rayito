//! The operator's SOCKS5 upstream (design D10): where it is, whether it
//! may be used, and the client handshake (RFC 1928 with RFC 1929
//! username/password), bounded to 10 s. The proxy never falls back to a
//! direct connection when the upstream fails (fail closed, as E2B).
//!
//! A hostname upstream is resolved at `UpdateNetwork` (to refuse an
//! unresolvable or guarded one) and again at every dial, using the first
//! address the upstream guard allows. Neither the address nor the
//! credentials are ever logged.

use std::net::SocketAddr;
use std::time::Duration;

use rayd_core::network::proxy_protocol::{
    SocksTarget, UpstreamProtocolError, decode_client_connect_reply, decode_method_selection,
    decode_userpass_status, socks_client_connect, socks_client_greeting, socks_userpass_request,
};
use rayd_core::network::{
    NetworkError, PROXY_CONNECT_TIMEOUT, ProxyCredentials, UPSTREAM_RESOLVE_TIMEOUT, UpstreamGuard,
    UpstreamHost, UpstreamProxy,
};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;

use super::proxy::ProxySeams;

/// The whole client handshake, after the TCP connect.
pub const UPSTREAM_HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(10);
/// Enough for the longest `CONNECT` reply (domain of 255 bytes).
const REPLY_BUFFER_BYTES: usize = 4 + 1 + 255 + 2;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum UpstreamFailure {
    Unresolved,
    Forbidden,
    Unreachable,
    TimedOut,
    Protocol(UpstreamProtocolError),
}

/// `UpdateNetwork`'s check of a hostname upstream: it must resolve within
/// 5 s to at least one address the upstream guard allows.
pub async fn check_upstream(
    upstream: &UpstreamProxy,
    seams: &ProxySeams,
) -> Result<(), NetworkError> {
    match upstream_address(upstream, seams).await {
        Ok(_) => Ok(()),
        Err(UpstreamFailure::Forbidden) => Err(NetworkError::ProxyForbiddenAddress),
        Err(_) => Err(NetworkError::ProxyUnresolvable),
    }
}

/// The upstream's socket address: the literal, or the first resolved
/// address the guard allows (pinned for this connection).
pub async fn upstream_address(
    upstream: &UpstreamProxy,
    seams: &ProxySeams,
) -> Result<SocketAddr, UpstreamFailure> {
    let port = upstream.port();
    let name = match upstream.host() {
        UpstreamHost::Ip(ip) => return Ok(SocketAddr::new(*ip, port)),
        UpstreamHost::Name(name) => name,
    };
    let resolved =
        tokio::time::timeout(UPSTREAM_RESOLVE_TIMEOUT, seams.resolver.resolve(name, port))
            .await
            .map_err(|_| UpstreamFailure::Unresolved)?
            .map_err(|_| UpstreamFailure::Unresolved)?;
    if resolved.is_empty() {
        return Err(UpstreamFailure::Unresolved);
    }
    resolved
        .into_iter()
        .find(|ip| !UpstreamGuard.blocks(*ip))
        .map(|ip| SocketAddr::new(ip, port))
        .ok_or(UpstreamFailure::Forbidden)
}

/// A tunnel to `target` through the upstream, ready for relaying.
pub async fn connect_via_upstream(
    upstream: &UpstreamProxy,
    target: &SocksTarget,
    seams: &ProxySeams,
) -> Result<TcpStream, UpstreamFailure> {
    let address = upstream_address(upstream, seams).await?;
    let mut stream = tokio::time::timeout(PROXY_CONNECT_TIMEOUT, seams.dialer.dial(address))
        .await
        .map_err(|_| UpstreamFailure::TimedOut)?
        .map_err(|_| UpstreamFailure::Unreachable)?;
    tokio::time::timeout(
        UPSTREAM_HANDSHAKE_TIMEOUT,
        handshake(&mut stream, upstream.credentials(), target),
    )
    .await
    .map_err(|_| UpstreamFailure::TimedOut)??;
    Ok(stream)
}

async fn handshake(
    stream: &mut TcpStream,
    credentials: Option<&ProxyCredentials>,
    target: &SocksTarget,
) -> Result<(), UpstreamFailure> {
    let protocol = |_| UpstreamFailure::Protocol(UpstreamProtocolError::Protocol);
    stream
        .write_all(&socks_client_greeting(credentials.is_some()))
        .await
        .map_err(protocol)?;
    let mut selection = [0u8; 2];
    stream.read_exact(&mut selection).await.map_err(protocol)?;
    let wants_credentials = decode_method_selection(selection, credentials.is_some())
        .map_err(UpstreamFailure::Protocol)?;
    if let (true, Some(credentials)) = (wants_credentials, credentials) {
        authenticate(stream, credentials).await?;
    }
    let request = socks_client_connect(target).map_err(UpstreamFailure::Protocol)?;
    stream.write_all(&request).await.map_err(protocol)?;
    read_connect_reply(stream).await
}

async fn authenticate(
    stream: &mut TcpStream,
    credentials: &ProxyCredentials,
) -> Result<(), UpstreamFailure> {
    let protocol = |_| UpstreamFailure::Protocol(UpstreamProtocolError::Protocol);
    let request = socks_userpass_request(credentials.username(), credentials.password())
        .map_err(UpstreamFailure::Protocol)?;
    stream.write_all(&request).await.map_err(protocol)?;
    let mut status = [0u8; 2];
    stream.read_exact(&mut status).await.map_err(protocol)?;
    decode_userpass_status(status).map_err(UpstreamFailure::Protocol)
}

async fn read_connect_reply(stream: &mut TcpStream) -> Result<(), UpstreamFailure> {
    let mut reply = Vec::with_capacity(REPLY_BUFFER_BYTES);
    loop {
        if decode_client_connect_reply(&reply)
            .map_err(UpstreamFailure::Protocol)?
            .is_some()
        {
            return Ok(());
        }
        if reply.len() >= REPLY_BUFFER_BYTES {
            return Err(UpstreamFailure::Protocol(UpstreamProtocolError::Protocol));
        }
        let mut byte = [0u8; 1];
        stream
            .read_exact(&mut byte)
            .await
            .map_err(|_| UpstreamFailure::Protocol(UpstreamProtocolError::Protocol))?;
        reply.push(byte[0]);
    }
}
