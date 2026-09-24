//! The wire protocols of the local forward proxy (design D8, D10), as pure
//! parsers and encoders the adapter feeds with the bytes it has read:
//!
//! - HTTP/1.x: `CONNECT host:port` (authority form, `[v6]:port` accepted)
//!   and absolute-form `http://` requests of any method, rewritten to
//!   origin form without `Proxy-*` and `Connection` headers and with
//!   `Connection: close`, so one proxied request is served per client
//!   connection. `https://` absolute form and origin form are refused.
//!   The client's `Host` is replaced by the request-target's authority,
//!   the one the policy checked (RFC 9112 §3.2.2), so an allowed name
//!   cannot front another virtual host on the same address; obs-fold
//!   continuation lines are refused so none can re-attach to a dropped
//!   header.
//! - SOCKS5 server (RFC 1928): method `00` only, `CONNECT` only, ATYP
//!   IPv4, domain and IPv6.
//! - SOCKS5 client towards the operator's upstream, with RFC 1929
//!   username/password (the encoded credentials are zeroized on drop).
//!
//! Parsers return `Ok(None)` while more bytes are needed.

use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr};

use thiserror::Error;
use zeroize::Zeroizing;

use super::PROXY_HEAD_MAX_BYTES;
use super::policy::parse_port;

pub const SOCKS_VERSION: u8 = 0x05;
pub const SOCKS_METHOD_NO_AUTH: u8 = 0x00;
pub const SOCKS_METHOD_USERPASS: u8 = 0x02;
pub const SOCKS_METHOD_NONE_ACCEPTABLE: u8 = 0xFF;
pub const SOCKS_USERPASS_VERSION: u8 = 0x01;
pub const SOCKS_CMD_CONNECT: u8 = 0x01;
pub const SOCKS_ATYP_IPV4: u8 = 0x01;
pub const SOCKS_ATYP_DOMAIN: u8 = 0x03;
pub const SOCKS_ATYP_IPV6: u8 = 0x04;
pub const HTTP_DEFAULT_PORT: u16 = 80;

const FORBIDDEN_BODY: &str = "rayito: destino bloqueado por la política de egress\n";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Error)]
pub enum HttpRequestError {
    #[error("malformed proxy request")]
    Malformed,
    #[error("only http:// absolute-form and CONNECT are proxied")]
    UnsupportedTarget,
    #[error("request head over the size limit")]
    HeadTooLarge,
}

impl HttpRequestError {
    #[must_use]
    pub fn status(self) -> HttpStatus {
        match self {
            Self::Malformed | Self::UnsupportedTarget => HttpStatus::BadRequest,
            Self::HeadTooLarge => HttpStatus::HeaderFieldsTooLarge,
        }
    }
}

/// A proxy request after parsing; `host` is unbracketed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HttpProxyRequest {
    Connect {
        host: String,
        port: u16,
    },
    /// `head` is the rewritten origin-form request head, ready to send.
    Forward {
        host: String,
        port: u16,
        head: Vec<u8>,
    },
}

/// Where the head ends (`\r\n\r\n` included), or an error once the
/// buffer holds more than the limit without one.
pub fn http_head_end(buffer: &[u8]) -> Result<Option<usize>, HttpRequestError> {
    match buffer.windows(4).position(|window| window == b"\r\n\r\n") {
        Some(position) if position + 4 <= PROXY_HEAD_MAX_BYTES => Ok(Some(position + 4)),
        Some(_) => Err(HttpRequestError::HeadTooLarge),
        None if buffer.len() >= PROXY_HEAD_MAX_BYTES => Err(HttpRequestError::HeadTooLarge),
        None => Ok(None),
    }
}

/// Parses a complete head (as delimited by [`http_head_end`]).
pub fn parse_http_request(head: &[u8]) -> Result<HttpProxyRequest, HttpRequestError> {
    let text = std::str::from_utf8(head).map_err(|_| HttpRequestError::Malformed)?;
    let (request_line, headers) = text.split_once("\r\n").ok_or(HttpRequestError::Malformed)?;
    let mut parts = request_line.split(' ');
    let (Some(method), Some(target), Some(version), None) =
        (parts.next(), parts.next(), parts.next(), parts.next())
    else {
        return Err(HttpRequestError::Malformed);
    };
    if method.is_empty() || !is_token(method) || !version.starts_with("HTTP/1.") {
        return Err(HttpRequestError::Malformed);
    }
    if method.eq_ignore_ascii_case("CONNECT") {
        let (host, port) = parse_authority(target).ok_or(HttpRequestError::Malformed)?;
        return Ok(HttpProxyRequest::Connect { host, port });
    }
    let rest = strip_prefix_ignore_case(target, "http://").ok_or_else(|| {
        if target.contains("://") {
            HttpRequestError::UnsupportedTarget
        } else {
            HttpRequestError::Malformed
        }
    })?;
    let (authority, path) = match rest.find(['/', '?']) {
        Some(index) => rest.split_at(index),
        None => (rest, ""),
    };
    let (host, port) = parse_http_authority(authority).ok_or(HttpRequestError::Malformed)?;
    if headers
        .split("\r\n")
        .any(|line| line.starts_with([' ', '\t']))
    {
        return Err(HttpRequestError::Malformed);
    }
    let head = rewrite_head(method, path, version, headers, authority);
    Ok(HttpProxyRequest::Forward { host, port, head })
}

fn rewrite_head(
    method: &str,
    path: &str,
    version: &str,
    headers: &str,
    authority: &str,
) -> Vec<u8> {
    let path = path.split('#').next().unwrap_or_default();
    let origin = if path.starts_with('/') {
        path.to_owned()
    } else {
        format!("/{path}")
    };
    let mut head = format!("{method} {origin} {version}\r\nHost: {authority}\r\n");
    for line in headers.split("\r\n").filter(|line| !line.is_empty()) {
        let name = line.split(':').next().unwrap_or_default().trim();
        let lowered = name.to_ascii_lowercase();
        if lowered.starts_with("proxy-")
            || lowered == "connection"
            || lowered == "keep-alive"
            || lowered == "host"
        {
            continue;
        }
        head.push_str(line);
        head.push_str("\r\n");
    }
    head.push_str("Connection: close\r\n\r\n");
    head.into_bytes()
}

fn strip_prefix_ignore_case<'a>(text: &'a str, prefix: &str) -> Option<&'a str> {
    let head = text.get(..prefix.len())?;
    head.eq_ignore_ascii_case(prefix)
        .then(|| text.get(prefix.len()..))
        .flatten()
}

fn is_token(text: &str) -> bool {
    text.bytes()
        .all(|byte| byte.is_ascii_alphanumeric() || b"!#$%&'*+-.^_`|~".contains(&byte))
}

/// `host:port` or `[v6]:port`, port required.
fn parse_authority(authority: &str) -> Option<(String, u16)> {
    split_host_port(authority).and_then(|(host, port)| Some((host, parse_port(port?)?)))
}

/// Like [`parse_authority`] but the port defaults to 80.
fn parse_http_authority(authority: &str) -> Option<(String, u16)> {
    let (host, port) = split_host_port(authority)?;
    let port = match port {
        Some(port) => parse_port(port)?,
        None => HTTP_DEFAULT_PORT,
    };
    Some((host, port))
}

fn split_host_port(authority: &str) -> Option<(String, Option<&str>)> {
    if authority.contains('@') || authority.is_empty() {
        return None;
    }
    if let Some(rest) = authority.strip_prefix('[') {
        let (literal, after) = rest.split_once(']')?;
        literal.parse::<Ipv6Addr>().ok()?;
        let port = match after {
            "" => None,
            _ => Some(after.strip_prefix(':')?),
        };
        return Some((literal.to_owned(), port));
    }
    match authority.rsplit_once(':') {
        Some((host, _)) if host.contains(':') => None,
        Some((host, port)) if !host.is_empty() => Some((host.to_owned(), Some(port))),
        Some(_) => None,
        None => Some((authority.to_owned(), None)),
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HttpStatus {
    ConnectionEstablished,
    BadRequest,
    Forbidden,
    RequestTimeout,
    HeaderFieldsTooLarge,
    BadGateway,
    ServiceUnavailable,
    GatewayTimeout,
}

/// The fixed response for `status`; only `403` carries a body.
#[must_use]
pub fn http_response(status: HttpStatus) -> Vec<u8> {
    let status_line = match status {
        HttpStatus::ConnectionEstablished => {
            return b"HTTP/1.1 200 Connection established\r\n\r\n".to_vec();
        }
        HttpStatus::Forbidden => {
            return format!(
                "HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{FORBIDDEN_BODY}",
                FORBIDDEN_BODY.len()
            )
            .into_bytes();
        }
        HttpStatus::BadRequest => "400 Bad Request",
        HttpStatus::RequestTimeout => "408 Request Timeout",
        HttpStatus::HeaderFieldsTooLarge => "431 Request Header Fields Too Large",
        HttpStatus::BadGateway => "502 Bad Gateway",
        HttpStatus::ServiceUnavailable => "503 Service Unavailable",
        HttpStatus::GatewayTimeout => "504 Gateway Timeout",
    };
    format!("HTTP/1.1 {status_line}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n").into_bytes()
}

/// RFC 1928 reply codes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SocksReply {
    Succeeded = 0x00,
    GeneralFailure = 0x01,
    NotAllowedByRuleset = 0x02,
    HostUnreachable = 0x04,
    ConnectionRefused = 0x05,
    TtlExpired = 0x06,
    CommandNotSupported = 0x07,
    AddressTypeNotSupported = 0x08,
}

/// A SOCKS5 destination, on either side of the proxy.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SocksTarget {
    Ip(SocketAddr),
    Domain(String, u16),
}

impl SocksTarget {
    #[must_use]
    pub fn port(&self) -> u16 {
        match self {
            Self::Ip(address) => address.port(),
            Self::Domain(_, port) => *port,
        }
    }
}

/// The client's greeting: `Some((offers_no_auth, consumed))` once
/// complete, `Err` when the version is not 5.
pub fn parse_socks_greeting(buffer: &[u8]) -> Result<Option<(bool, usize)>, SocksReply> {
    let [version, count, rest @ ..] = buffer else {
        return Ok(None);
    };
    if *version != SOCKS_VERSION {
        return Err(SocksReply::GeneralFailure);
    }
    let count = usize::from(*count);
    let Some(methods) = rest.get(..count) else {
        return Ok(None);
    };
    Ok(Some((methods.contains(&SOCKS_METHOD_NO_AUTH), 2 + count)))
}

/// `05 00` when method `00` was offered, `05 FF` otherwise.
#[must_use]
pub fn socks_method_selection(no_auth_offered: bool) -> [u8; 2] {
    let method = if no_auth_offered {
        SOCKS_METHOD_NO_AUTH
    } else {
        SOCKS_METHOD_NONE_ACCEPTABLE
    };
    [SOCKS_VERSION, method]
}

/// A `CONNECT` request: `Some((target, consumed))` once complete; the
/// `Err` carries the reply code to answer with before closing.
pub fn parse_socks_request(buffer: &[u8]) -> Result<Option<(SocksTarget, usize)>, SocksReply> {
    let [version, command, _reserved, address_type, rest @ ..] = buffer else {
        return Ok(None);
    };
    if *version != SOCKS_VERSION {
        return Err(SocksReply::GeneralFailure);
    }
    if *command != SOCKS_CMD_CONNECT {
        return Err(SocksReply::CommandNotSupported);
    }
    let decoded = decode_address(*address_type, rest)?;
    Ok(decoded.map(|(target, used)| (target, 4 + used)))
}

/// ATYP + address + port as they follow the fixed header of a request or
/// a reply; `Ok(None)` while incomplete.
fn decode_address(
    address_type: u8,
    bytes: &[u8],
) -> Result<Option<(SocksTarget, usize)>, SocksReply> {
    let (host_len, prefix) = match address_type {
        SOCKS_ATYP_IPV4 => (4, 0),
        SOCKS_ATYP_IPV6 => (16, 0),
        SOCKS_ATYP_DOMAIN => match bytes.first() {
            None => return Ok(None),
            Some(0) => return Err(SocksReply::AddressTypeNotSupported),
            Some(length) => (usize::from(*length), 1),
        },
        _ => return Err(SocksReply::AddressTypeNotSupported),
    };
    let total = prefix + host_len + 2;
    let Some(field) = bytes.get(..total) else {
        return Ok(None);
    };
    let (host, port) = field.split_at(prefix + host_len);
    let port = u16::from_be_bytes([port[0], port[1]]);
    let host = &host[prefix..];
    let target = match address_type {
        SOCKS_ATYP_IPV4 => {
            let octets: [u8; 4] = host.try_into().map_err(|_| SocksReply::GeneralFailure)?;
            SocksTarget::Ip(SocketAddr::new(IpAddr::V4(Ipv4Addr::from(octets)), port))
        }
        SOCKS_ATYP_IPV6 => {
            let octets: [u8; 16] = host.try_into().map_err(|_| SocksReply::GeneralFailure)?;
            SocksTarget::Ip(SocketAddr::new(IpAddr::V6(Ipv6Addr::from(octets)), port))
        }
        _ => SocksTarget::Domain(String::from_utf8_lossy(host).into_owned(), port),
    };
    Ok(Some((target, total)))
}

/// A server reply with the bound address `0.0.0.0:0`.
#[must_use]
pub fn socks_reply(reply: SocksReply) -> [u8; 10] {
    [
        SOCKS_VERSION,
        reply as u8,
        0,
        SOCKS_ATYP_IPV4,
        0,
        0,
        0,
        0,
        0,
        0,
    ]
}

/// Why the upstream handshake failed; never carries a credential.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Error)]
pub enum UpstreamProtocolError {
    #[error("the upstream did not answer as a SOCKS5 server")]
    Protocol,
    #[error("the upstream refused every offered authentication method")]
    MethodRefused,
    #[error("the upstream rejected the credentials")]
    AuthenticationFailed,
    #[error("the upstream refused the connection (reply {0:#04x})")]
    ConnectRefused(u8),
    #[error("the target cannot be encoded for the upstream")]
    InvalidTarget,
    #[error("the credentials cannot be encoded for the upstream")]
    InvalidCredentials,
}

/// `05 01 00`, or `05 02 00 02` when credentials will be offered.
#[must_use]
pub fn socks_client_greeting(with_credentials: bool) -> Vec<u8> {
    if with_credentials {
        vec![
            SOCKS_VERSION,
            2,
            SOCKS_METHOD_NO_AUTH,
            SOCKS_METHOD_USERPASS,
        ]
    } else {
        vec![SOCKS_VERSION, 1, SOCKS_METHOD_NO_AUTH]
    }
}

/// Whether the upstream chose username/password (`true`) or no
/// authentication (`false`).
pub fn decode_method_selection(
    selection: [u8; 2],
    with_credentials: bool,
) -> Result<bool, UpstreamProtocolError> {
    match selection {
        [SOCKS_VERSION, SOCKS_METHOD_NO_AUTH] => Ok(false),
        [SOCKS_VERSION, SOCKS_METHOD_USERPASS] if with_credentials => Ok(true),
        [SOCKS_VERSION, _] => Err(UpstreamProtocolError::MethodRefused),
        _ => Err(UpstreamProtocolError::Protocol),
    }
}

/// RFC 1929 `01 ULEN USER PLEN PASS`; both fields are 1-255 bytes (an
/// empty password is sent as length 0, which RFC 1929 allows).
pub fn socks_userpass_request(
    username: &str,
    password: &str,
) -> Result<Zeroizing<Vec<u8>>, UpstreamProtocolError> {
    let user_len =
        u8::try_from(username.len()).map_err(|_| UpstreamProtocolError::InvalidCredentials)?;
    let pass_len =
        u8::try_from(password.len()).map_err(|_| UpstreamProtocolError::InvalidCredentials)?;
    let mut request = Zeroizing::new(Vec::with_capacity(3 + username.len() + password.len()));
    request.push(SOCKS_USERPASS_VERSION);
    request.push(user_len);
    request.extend_from_slice(username.as_bytes());
    request.push(pass_len);
    request.extend_from_slice(password.as_bytes());
    Ok(request)
}

pub fn decode_userpass_status(status: [u8; 2]) -> Result<(), UpstreamProtocolError> {
    match status {
        [SOCKS_USERPASS_VERSION, 0] => Ok(()),
        [SOCKS_USERPASS_VERSION, _] => Err(UpstreamProtocolError::AuthenticationFailed),
        _ => Err(UpstreamProtocolError::Protocol),
    }
}

/// `05 01 00 ATYP ADDR PORT` for the target.
pub fn socks_client_connect(target: &SocksTarget) -> Result<Vec<u8>, UpstreamProtocolError> {
    let mut request = vec![SOCKS_VERSION, SOCKS_CMD_CONNECT, 0];
    match target {
        SocksTarget::Ip(SocketAddr::V4(address)) => {
            request.push(SOCKS_ATYP_IPV4);
            request.extend_from_slice(&address.ip().octets());
        }
        SocksTarget::Ip(SocketAddr::V6(address)) => {
            request.push(SOCKS_ATYP_IPV6);
            request.extend_from_slice(&address.ip().octets());
        }
        SocksTarget::Domain(name, _) => {
            let length = u8::try_from(name.len())
                .ok()
                .filter(|length| *length > 0)
                .ok_or(UpstreamProtocolError::InvalidTarget)?;
            request.push(SOCKS_ATYP_DOMAIN);
            request.push(length);
            request.extend_from_slice(name.as_bytes());
        }
    }
    request.extend_from_slice(&target.port().to_be_bytes());
    Ok(request)
}

/// The upstream's `CONNECT` reply: `Ok(Some(consumed))` once complete and
/// successful, `Err` on a refusal or a malformed reply.
pub fn decode_client_connect_reply(buffer: &[u8]) -> Result<Option<usize>, UpstreamProtocolError> {
    let [version, reply, _reserved, address_type, rest @ ..] = buffer else {
        return Ok(None);
    };
    if *version != SOCKS_VERSION {
        return Err(UpstreamProtocolError::Protocol);
    }
    if *reply != SocksReply::Succeeded as u8 {
        return Err(UpstreamProtocolError::ConnectRefused(*reply));
    }
    match decode_address(*address_type, rest) {
        Ok(decoded) => Ok(decoded.map(|(_, used)| 4 + used)),
        Err(_) => Err(UpstreamProtocolError::Protocol),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn connect(target: &str) -> Result<HttpProxyRequest, HttpRequestError> {
        parse_http_request(
            format!("CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n").as_bytes(),
        )
    }

    fn forwarded(head: &str) -> (String, u16, String) {
        match parse_http_request(head.as_bytes()).unwrap() {
            HttpProxyRequest::Forward { host, port, head } => {
                (host, port, String::from_utf8(head).unwrap())
            }
            other @ HttpProxyRequest::Connect { .. } => panic!("{other:?}"),
        }
    }

    #[test]
    fn connect_authority_forms() {
        assert_eq!(
            connect("example.com:443"),
            Ok(HttpProxyRequest::Connect {
                host: "example.com".to_owned(),
                port: 443
            })
        );
        assert_eq!(
            connect("[2001:db8::1]:8443"),
            Ok(HttpProxyRequest::Connect {
                host: "2001:db8::1".to_owned(),
                port: 8443
            })
        );
        assert_eq!(
            connect("203.0.113.7:80"),
            Ok(HttpProxyRequest::Connect {
                host: "203.0.113.7".to_owned(),
                port: 80
            })
        );
        for bad in [
            "example.com",
            "example.com:0",
            "example.com:99999",
            "2001:db8::1:443",
            "[2001:db8::1]",
            "[not-v6]:443",
            "user@example.com:443",
            ":443",
        ] {
            assert_eq!(connect(bad), Err(HttpRequestError::Malformed), "{bad}");
        }
        assert!(matches!(
            parse_http_request(b"connect example.com:443 HTTP/1.0\r\n\r\n"),
            Ok(HttpProxyRequest::Connect { .. })
        ));
    }

    #[test]
    fn absolute_form_is_rewritten_to_origin_form() {
        let (host, port, head) = forwarded(
            "GET http://Example.com:8080/a/b?q=1#frag HTTP/1.1\r\nHost: Example.com:8080\r\nProxy-Authorization: Basic eA==\r\nProxy-Connection: keep-alive\r\nConnection: keep-alive\r\nKeep-Alive: 5\r\nAccept: */*\r\n\r\n",
        );
        assert_eq!((host.as_str(), port), ("Example.com", 8080));
        assert_eq!(
            head,
            "GET /a/b?q=1 HTTP/1.1\r\nHost: Example.com:8080\r\nAccept: */*\r\nConnection: close\r\n\r\n"
        );
        let (_, port, head) = forwarded("POST http://example.com HTTP/1.1\r\n\r\n");
        assert_eq!(port, 80);
        assert_eq!(
            head,
            "POST / HTTP/1.1\r\nHost: example.com\r\nConnection: close\r\n\r\n"
        );
        let (host, _, head) = forwarded("GET http://[2001:db8::1]?x HTTP/1.1\r\n\r\n");
        assert_eq!(host, "2001:db8::1");
        assert!(head.starts_with("GET /?x HTTP/1.1\r\n"));
    }

    /// RFC 9112 §3.2.2: a proxy replaces the received `Host` with the
    /// request-target's authority, which is what the policy checked, so an
    /// allowed name can never front another virtual host on the same IP.
    #[test]
    fn forward_mode_host_is_the_checked_authority() {
        let (host, port, head) = forwarded(
            "GET http://allowed.example/x HTTP/1.1\r\nhost: internal.example\r\nAccept: */*\r\nHOST : other.example\r\n\r\n",
        );
        assert_eq!((host.as_str(), port), ("allowed.example", 80));
        assert_eq!(
            head,
            "GET /x HTTP/1.1\r\nHost: allowed.example\r\nAccept: */*\r\nConnection: close\r\n\r\n"
        );
        let (_, _, head) =
            forwarded("GET http://[2001:db8::1]:8080/ HTTP/1.1\r\nHost: 203.0.113.9\r\n\r\n");
        assert_eq!(
            head,
            "GET / HTTP/1.1\r\nHost: [2001:db8::1]:8080\r\nConnection: close\r\n\r\n"
        );
    }

    /// An obs-fold continuation could otherwise re-attach a value to the
    /// header before a dropped `Host` line.
    #[test]
    fn obsolete_line_folding_is_refused() {
        for head in [
            "GET http://allowed.example/ HTTP/1.1\r\nHost: allowed.example\r\n internal.example\r\n\r\n",
            "GET http://allowed.example/ HTTP/1.1\r\nAccept: */*\r\n\tHost: internal.example\r\n\r\n",
        ] {
            assert_eq!(
                parse_http_request(head.as_bytes()),
                Err(HttpRequestError::Malformed),
                "{head:?}"
            );
        }
    }

    #[test]
    fn https_absolute_form_origin_form_and_garbage_are_refused() {
        for (head, expected) in [
            (
                "GET https://example.com/ HTTP/1.1\r\n\r\n",
                HttpRequestError::UnsupportedTarget,
            ),
            (
                "GET /index.html HTTP/1.1\r\n\r\n",
                HttpRequestError::Malformed,
            ),
            (
                "GET http://example.com/ SPDY/3\r\n\r\n",
                HttpRequestError::Malformed,
            ),
            (
                "GET  http://example.com/ HTTP/1.1\r\n\r\n",
                HttpRequestError::Malformed,
            ),
            (
                "G(T http://example.com/ HTTP/1.1\r\n\r\n",
                HttpRequestError::Malformed,
            ),
            ("GET http:// HTTP/1.1\r\n\r\n", HttpRequestError::Malformed),
        ] {
            assert_eq!(
                parse_http_request(head.as_bytes()),
                Err(expected),
                "{head:?}"
            );
            assert_eq!(expected.status(), HttpStatus::BadRequest);
        }
        assert_eq!(
            parse_http_request(&[0xff, 0xfe, b'\r', b'\n', b'\r', b'\n']),
            Err(HttpRequestError::Malformed)
        );
    }

    #[test]
    fn the_head_is_capped_at_16_kib() {
        assert_eq!(http_head_end(b"CONNECT a:1 HTTP/1.1\r\n"), Ok(None));
        assert_eq!(
            http_head_end(b"CONNECT a:1 HTTP/1.1\r\n\r\nrest"),
            Ok(Some(24))
        );
        let full = vec![b'a'; PROXY_HEAD_MAX_BYTES];
        assert_eq!(http_head_end(&full), Err(HttpRequestError::HeadTooLarge));
        let mut late = vec![b'a'; PROXY_HEAD_MAX_BYTES - 2];
        late.extend_from_slice(b"\r\n\r\n");
        assert_eq!(http_head_end(&late), Err(HttpRequestError::HeadTooLarge));
        assert_eq!(
            HttpRequestError::HeadTooLarge.status(),
            HttpStatus::HeaderFieldsTooLarge
        );
    }

    #[test]
    fn fixed_http_responses() {
        assert_eq!(
            http_response(HttpStatus::ConnectionEstablished),
            b"HTTP/1.1 200 Connection established\r\n\r\n"
        );
        let forbidden = String::from_utf8(http_response(HttpStatus::Forbidden)).unwrap();
        let (head, body) = forbidden.split_once("\r\n\r\n").unwrap();
        assert!(head.starts_with("HTTP/1.1 403 Forbidden\r\n"));
        assert!(head.contains(&format!("Content-Length: {}", body.len())));
        assert_eq!(
            body,
            "rayito: destino bloqueado por la política de egress\n"
        );
        for (status, line) in [
            (HttpStatus::BadRequest, "HTTP/1.1 400 "),
            (HttpStatus::RequestTimeout, "HTTP/1.1 408 "),
            (HttpStatus::HeaderFieldsTooLarge, "HTTP/1.1 431 "),
            (HttpStatus::BadGateway, "HTTP/1.1 502 "),
            (HttpStatus::ServiceUnavailable, "HTTP/1.1 503 "),
            (HttpStatus::GatewayTimeout, "HTTP/1.1 504 "),
        ] {
            let response = String::from_utf8(http_response(status)).unwrap();
            assert!(response.starts_with(line), "{response}");
            assert!(response.ends_with("Content-Length: 0\r\nConnection: close\r\n\r\n"));
        }
    }

    #[test]
    fn socks_greeting_needs_method_zero() {
        assert_eq!(parse_socks_greeting(&[5]), Ok(None));
        assert_eq!(parse_socks_greeting(&[5, 2, 0]), Ok(None));
        assert_eq!(parse_socks_greeting(&[5, 2, 2, 0, 9]), Ok(Some((true, 4))));
        assert_eq!(parse_socks_greeting(&[5, 1, 2]), Ok(Some((false, 3))));
        assert_eq!(
            parse_socks_greeting(&[4, 1, 0]),
            Err(SocksReply::GeneralFailure)
        );
        assert_eq!(socks_method_selection(true), [5, 0]);
        assert_eq!(socks_method_selection(false), [5, 0xFF]);
    }

    #[test]
    fn socks_requests_by_address_type() {
        assert_eq!(
            parse_socks_request(&[5, 1, 0, 1, 203, 0, 113, 7, 0x01, 0xBB]),
            Ok(Some((
                SocksTarget::Ip("203.0.113.7:443".parse().unwrap()),
                10
            )))
        );
        let mut domain = vec![5, 1, 0, 3, 11];
        domain.extend_from_slice(b"example.com");
        domain.extend_from_slice(&[0, 80, 0xAA]);
        assert_eq!(
            parse_socks_request(&domain),
            Ok(Some((
                SocksTarget::Domain("example.com".to_owned(), 80),
                18
            )))
        );
        let mut v6 = vec![5, 1, 0, 4];
        v6.extend_from_slice(&"2001:db8::1".parse::<Ipv6Addr>().unwrap().octets());
        v6.extend_from_slice(&[0x1F, 0x90]);
        assert_eq!(
            parse_socks_request(&v6),
            Ok(Some((
                SocksTarget::Ip("[2001:db8::1]:8080".parse().unwrap()),
                22
            )))
        );
        assert_eq!(parse_socks_request(&[5, 1, 0, 1, 203, 0]), Ok(None));
        assert_eq!(parse_socks_request(&[5, 1, 0, 3]), Ok(None));
        assert_eq!(
            parse_socks_request(&[5, 1, 0, 3, 0, 0, 80]),
            Err(SocksReply::AddressTypeNotSupported)
        );
        assert_eq!(
            parse_socks_request(&[5, 1, 0, 9, 1]),
            Err(SocksReply::AddressTypeNotSupported)
        );
        assert_eq!(
            parse_socks_request(&[5, 2, 0, 1]),
            Err(SocksReply::CommandNotSupported)
        );
        assert_eq!(
            parse_socks_request(&[5, 3, 0, 1]),
            Err(SocksReply::CommandNotSupported)
        );
        assert_eq!(
            parse_socks_request(&[4, 1, 0, 1]),
            Err(SocksReply::GeneralFailure)
        );
        let lossy = parse_socks_request(&[5, 1, 0, 3, 2, 0xC3, 0x28, 0, 80])
            .unwrap()
            .unwrap();
        assert!(matches!(lossy.0, SocksTarget::Domain(name, 80) if !name.is_ascii()));
    }

    #[test]
    fn socks_reply_encoding() {
        assert_eq!(
            socks_reply(SocksReply::Succeeded),
            [5, 0, 0, 1, 0, 0, 0, 0, 0, 0]
        );
        for (reply, code) in [
            (SocksReply::GeneralFailure, 1),
            (SocksReply::NotAllowedByRuleset, 2),
            (SocksReply::HostUnreachable, 4),
            (SocksReply::ConnectionRefused, 5),
            (SocksReply::TtlExpired, 6),
            (SocksReply::CommandNotSupported, 7),
            (SocksReply::AddressTypeNotSupported, 8),
        ] {
            assert_eq!(socks_reply(reply)[1], code);
        }
    }

    #[test]
    fn socks_client_handshake_encoders_and_decoders() {
        assert_eq!(socks_client_greeting(false), [5, 1, 0]);
        assert_eq!(socks_client_greeting(true), [5, 2, 0, 2]);
        assert_eq!(decode_method_selection([5, 0], true), Ok(false));
        assert_eq!(decode_method_selection([5, 2], true), Ok(true));
        assert_eq!(
            decode_method_selection([5, 2], false),
            Err(UpstreamProtocolError::MethodRefused)
        );
        assert_eq!(
            decode_method_selection([5, 0xFF], true),
            Err(UpstreamProtocolError::MethodRefused)
        );
        assert_eq!(
            decode_method_selection([4, 0], true),
            Err(UpstreamProtocolError::Protocol)
        );
        let auth = socks_userpass_request("user", "pw").unwrap();
        assert_eq!(auth.as_slice(), b"\x01\x04user\x02pw");
        assert_eq!(
            socks_userpass_request(&"u".repeat(256), "p"),
            Err(UpstreamProtocolError::InvalidCredentials)
        );
        assert_eq!(decode_userpass_status([1, 0]), Ok(()));
        assert_eq!(
            decode_userpass_status([1, 1]),
            Err(UpstreamProtocolError::AuthenticationFailed)
        );
        assert_eq!(
            decode_userpass_status([5, 0]),
            Err(UpstreamProtocolError::Protocol)
        );
    }

    #[test]
    fn socks_client_connect_and_reply() {
        assert_eq!(
            socks_client_connect(&SocksTarget::Domain("aws.amazon.com".to_owned(), 443)).unwrap(),
            [
                &[5u8, 1, 0, 3, 14][..],
                &b"aws.amazon.com"[..],
                &[1u8, 0xBB][..]
            ]
            .concat()
        );
        assert_eq!(
            socks_client_connect(&SocksTarget::Ip("203.0.113.7:80".parse().unwrap())).unwrap(),
            [5, 1, 0, 1, 203, 0, 113, 7, 0, 80]
        );
        let v6 =
            socks_client_connect(&SocksTarget::Ip("[2001:db8::1]:443".parse().unwrap())).unwrap();
        assert_eq!(v6.len(), 22);
        assert_eq!(v6[3], SOCKS_ATYP_IPV6);
        assert_eq!(
            socks_client_connect(&SocksTarget::Domain(String::new(), 443)),
            Err(UpstreamProtocolError::InvalidTarget)
        );
        assert_eq!(decode_client_connect_reply(&[5, 0, 0, 1, 0, 0]), Ok(None));
        assert_eq!(
            decode_client_connect_reply(&socks_reply(SocksReply::Succeeded)),
            Ok(Some(10))
        );
        assert_eq!(
            decode_client_connect_reply(&[5, 5, 0, 1, 0, 0, 0, 0, 0, 0]),
            Err(UpstreamProtocolError::ConnectRefused(5))
        );
        assert_eq!(
            decode_client_connect_reply(&[5, 0, 0, 7, 0]),
            Err(UpstreamProtocolError::Protocol)
        );
        assert_eq!(
            decode_client_connect_reply(&[1, 0, 0, 1]),
            Err(UpstreamProtocolError::Protocol)
        );
    }
}
