"""The deliverer's outbound HTTP client (T22): https-only, no redirects,
resolve-then-check-then-connect-to-the-checked-address (so a second DNS
answer after the SSRF check — DNS rebinding — can never be the one actually
used), stdlib only (no extra dependency to vendor into the Lambda zip).
"""

from __future__ import annotations

import http.client
import socket
import ssl
from urllib.parse import urlsplit

from domain.ssrf import IpAddress, first_safe_address

#: Only the status line matters; a receiver's body is read up to this much
#: (and then dropped) so a hostile endpoint cannot stream an endless one
#: into the deliverer's memory or time budget.
MAX_RESPONSE_BYTES = 64 * 1024


class SsrfBlocked(RuntimeError):
    """Every candidate address for the upstream host was blocked, or the
    URL was not `https://` to begin with."""


class HttpsOnlySender:
    """Implements `ports.HttpSender`."""

    def post(self, url: str, headers: dict[str, str], body: bytes, *, timeout: float) -> int:
        parts = urlsplit(url)
        if parts.scheme != "https":
            raise SsrfBlocked(f"esquema no permitido: {parts.scheme!r} (sólo https)")
        hostname = parts.hostname
        if not hostname:
            raise SsrfBlocked("URL sin host")
        port = parts.port or 443
        safe = first_safe_address(_resolve(hostname, port))
        if safe is None:
            raise SsrfBlocked(f"todas las direcciones de {hostname!r} están bloqueadas")
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        return _send(safe, port, hostname, path, headers, body, timeout)


def _resolve(hostname: str, port: int) -> list[IpAddress]:
    import ipaddress

    addresses = []
    for family, _type, _proto, _canon, sockaddr in socket.getaddrinfo(
        hostname, port, proto=socket.IPPROTO_TCP
    ):
        # `sockaddr[0]` is always the address as a string (its type is a
        # union only because `getaddrinfo`'s stub covers every address
        # family generically); `str(...)` satisfies the type checker
        # without changing the value.
        host = str(sockaddr[0])
        addresses.append(
            ipaddress.ip_address(host.split("%")[0] if family == socket.AF_INET6 else host)
        )
    return addresses


def _send(
    safe_address: IpAddress,
    port: int,
    hostname: str,
    path: str,
    headers: dict[str, str],
    body: bytes,
    timeout: float,
) -> int:
    """Connects the raw TCP socket to `safe_address` (already checked by
    `domain.ssrf`, never re-resolved — that is the whole point), then does
    the TLS handshake with `server_hostname=hostname`: pin the address,
    still verify the name against a real certificate. `http.client`'s own
    `HTTPSConnection` cannot express this directly (it SNIs/verifies
    against whatever host you construct it with), so this builds the
    connection by hand from public `http.client`/`ssl`/`socket` APIs only
    — no private attribute of either stdlib class. `timeout` bounds the
    connect and every later socket read/write (the deliverer derives it
    from its remaining time budget)."""
    raw_socket = socket.create_connection((str(safe_address), port), timeout=timeout)
    context = ssl.create_default_context()
    # `create_default_context()`'s own floor already excludes SSLv2/v3 on
    # any Python this project supports, but it does not *pin* a minimum —
    # stated explicitly here so a future OpenSSL/Python default change can
    # never silently reopen TLS 1.0/1.1 for this one outbound client.
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    tls_socket = context.wrap_socket(raw_socket, server_hostname=hostname)
    try:
        connection = http.client.HTTPConnection(hostname, port, timeout=timeout)
        connection.sock = tls_socket
        # `HTTPConnection.putrequest(skip_host=False)` would add its own
        # `Host` header too — computed off `self.port` against `HTTPConnection
        # .default_port` (80, since this is a plain `HTTPConnection` carrying
        # a TLS socket, not an `HTTPSConnection`), so for port 443 it would
        # read `Host: <host>:443` instead of the explicit one-header form
        # below, and a server would see two `Host` headers (RFC 9112 requires
        # it to reject that). `skip_host=True` leaves adding it to us.
        connection.putrequest("POST", path, skip_host=True)
        connection.putheader("Host", hostname)
        for name, value in headers.items():
            connection.putheader(name, value)
        connection.putheader("Content-Length", str(len(body)))
        connection.endheaders(message_body=body)
        response = connection.getresponse()
        response.read(MAX_RESPONSE_BYTES)
        return response.status
    finally:
        tls_socket.close()
