"""The deliverer's outbound HTTP client (T22): https-only, no redirects,
resolve-then-check-then-connect-to-the-checked-address (so a second DNS
answer after the SSRF check — DNS rebinding — can never be the one actually
used), stdlib only (no extra dependency to vendor into the Lambda zip).

`timeout` bounds the **whole** attempt, not each socket operation: DNS
resolution runs in a worker thread joined against the attempt's deadline
(`getaddrinfo` has no timeout of its own), and every later send and receive
is given only what is left of it, so a receiver (or its DNS) that answers
one byte at a time cannot hold the deliverer past its time budget.
"""

from __future__ import annotations

import http.client
import io
import socket
import ssl
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Final
from urllib.parse import urlsplit

from domain.ssrf import IpAddress, first_safe_address

#: Only the status line matters; a receiver's body is read up to this much
#: (and then dropped) so a hostile endpoint cannot stream an endless one
#: into the deliverer's memory or time budget.
MAX_RESPONSE_BYTES = 64 * 1024

#: Threads resolving hostnames in the background. A resolution that never
#: returns keeps its thread (Python cannot cancel `getaddrinfo`); once all
#: are stuck, later resolutions wait in the queue and time out against
#: their own deadline instead of piling up threads.
DNS_WORKERS: Final = 4
_dns_pool = ThreadPoolExecutor(max_workers=DNS_WORKERS, thread_name_prefix="webhook-dns")


class SsrfBlocked(RuntimeError):
    """Every candidate address for the upstream host was blocked, or the
    URL was not `https://` to begin with."""


class HttpsOnlySender:
    """Implements `ports.HttpSender`."""

    def post(self, url: str, headers: dict[str, str], body: bytes, *, timeout: float) -> int:
        """Raises `SsrfBlocked` for a non-https URL or a blocked host,
        `ValueError` (including `UnicodeError`) for a URL that cannot be
        parsed — an invalid port, a malformed IPv6 literal, a host IDNA
        cannot encode — and `TimeoutError` once `timeout` has passed."""
        deadline = time.monotonic() + timeout
        parts = urlsplit(url)
        if parts.scheme != "https":
            raise SsrfBlocked(f"esquema no permitido: {parts.scheme!r} (sólo https)")
        hostname = parts.hostname
        if not hostname:
            raise SsrfBlocked("URL sin host")
        port = parts.port or 443
        _require_idna_encodable(hostname)
        safe = first_safe_address(_resolve_before(hostname, port, deadline))
        if safe is None:
            raise SsrfBlocked(f"todas las direcciones de {hostname!r} están bloqueadas")
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        return _send(safe, port, hostname, path, headers, body, deadline)


def _require_idna_encodable(hostname: str) -> None:
    """`UnicodeError` (a `ValueError`) for a host `getaddrinfo` would refuse
    to encode (a label over 63 characters, for instance), raised here in
    the caller's thread rather than from inside the DNS worker."""
    hostname.encode("idna")


def _remaining(deadline: float) -> float:
    """What is left of the attempt, or `TimeoutError` when nothing is."""
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("plazo del intento agotado")
    return left


def _resolve_before(hostname: str, port: int, deadline: float) -> list[IpAddress]:
    """`_resolve` in a worker thread, waited for no later than `deadline`
    (`concurrent.futures.TimeoutError` is the builtin `TimeoutError`, an
    `OSError`, on every Python this Lambda runs)."""
    future = _dns_pool.submit(_resolve, hostname, port)
    try:
        return future.result(timeout=_remaining(deadline))
    except TimeoutError:
        future.cancel()
        raise


class _DeadlineSocket:
    """What `http.client` talks to: the TLS socket, re-armed with the time
    left before `deadline` before every send and every receive, so a slow
    receiver runs out of the attempt's time instead of resetting a per-read
    timeout each time it sends a byte."""

    def __init__(self, sock: Any, deadline: float) -> None:
        self._sock = sock
        self._deadline = deadline

    def _arm(self) -> None:
        self._sock.settimeout(_remaining(self._deadline))

    def sendall(self, data: bytes) -> None:
        self._arm()
        self._sock.sendall(data)

    def recv_into(self, buffer: Any, nbytes: int = 0, flags: int = 0) -> int:
        self._arm()
        return int(self._sock.recv_into(buffer, nbytes, flags))

    def makefile(self, mode: str = "rb", *_args: Any, **_kwargs: Any) -> io.BufferedReader:
        del mode
        return io.BufferedReader(socket.SocketIO(self, "rb"))  # type: ignore[arg-type]

    def _decref_socketios(self) -> None:
        """Called by `socket.SocketIO.close`; the TLS socket itself is
        closed by `_send`."""

    def close(self) -> None:
        self._sock.close()


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
    deadline: float,
) -> int:
    """Connects the raw TCP socket to `safe_address` (already checked by
    `domain.ssrf`, never re-resolved — that is the whole point), then does
    the TLS handshake with `server_hostname=hostname`: pin the address,
    still verify the name against a real certificate. `http.client`'s own
    `HTTPSConnection` cannot express this directly (it SNIs/verifies
    against whatever host you construct it with), so this builds the
    connection by hand from public `http.client`/`ssl`/`socket` APIs only
    — no private attribute of either stdlib class. Every step gets only
    what is left before `deadline` (`time.monotonic()`): the connect, the
    handshake (bounded as a whole by the socket timeout) and, through
    `_DeadlineSocket`, every send and receive of the request."""
    raw_socket = socket.create_connection((str(safe_address), port), timeout=_remaining(deadline))
    context = ssl.create_default_context()
    # `create_default_context()`'s own floor already excludes SSLv2/v3 on
    # any Python this project supports, but it does not *pin* a minimum —
    # stated explicitly here so a future OpenSSL/Python default change can
    # never silently reopen TLS 1.0/1.1 for this one outbound client.
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    raw_socket.settimeout(_remaining(deadline))
    tls_socket = context.wrap_socket(raw_socket, server_hostname=hostname)
    try:
        connection = http.client.HTTPConnection(hostname, port, timeout=_remaining(deadline))
        connection.sock = _DeadlineSocket(tls_socket, deadline)
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
