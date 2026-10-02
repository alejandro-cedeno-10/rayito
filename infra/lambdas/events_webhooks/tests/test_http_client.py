"""`adapters.http_client._send`: against a real local TCP socket (TLS
faked as a transparent passthrough, since the point under test is the
plaintext HTTP request this builds, not the handshake) — regression for
the duplicate `Host` header RFC 9112 forbids (`putrequest(skip_host=...)`
plus a manual `putheader("Host", ...)`).
"""

from __future__ import annotations

import socket
import threading
from typing import Any

import pytest
from adapters import http_client
from adapters.http_client import _send


class _PassthroughTlsSocket:
    """Stands in for `ssl.SSLSocket`: proxies everything (`sendall`,
    `recv`, `makefile`, ...) to the plain socket `http.client.HTTPConnection`
    sends the request over, so the only thing this test fakes is the TLS
    handshake itself."""

    def __init__(self, raw: socket.socket) -> None:
        self._raw = raw

    def __getattr__(self, name: str) -> Any:
        return getattr(self._raw, name)

    def close(self) -> None:
        pass


class _FakeTlsContext:
    def __init__(self) -> None:
        self.minimum_version: Any = None

    def wrap_socket(self, raw: socket.socket, server_hostname: str) -> _PassthroughTlsSocket:
        del server_hostname
        return _PassthroughTlsSocket(raw)


_EMPTY_OK = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"


def _serve_one_request(
    server: socket.socket, received: list[bytes], response: bytes = _EMPTY_OK
) -> None:
    connection, _addr = server.accept()
    with connection:
        connection.settimeout(5.0)
        chunks: list[bytes] = []
        while b"\r\n\r\n" not in b"".join(chunks):
            chunk = connection.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
        received.append(b"".join(chunks))
        try:
            connection.sendall(response)
        except OSError:
            pass  # the client stopped reading (the point of the body cap)


_TIMEOUT_SECONDS = 5.0


def test_send_emits_exactly_one_host_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(http_client.ssl, "create_default_context", lambda: _FakeTlsContext())

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    _host, port = server.getsockname()
    received: list[bytes] = []
    thread = threading.Thread(target=_serve_one_request, args=(server, received))
    thread.start()
    try:
        import ipaddress

        status = _send(
            ipaddress.ip_address("127.0.0.1"),
            port,
            "example-webhook.test",
            "/hook",
            {"e2b-webhook-id": "wh-1"},
            b"{}",
            _TIMEOUT_SECONDS,
        )
    finally:
        thread.join(timeout=5.0)
        server.close()

    assert status == 200
    request_text = received[0].decode("ascii")
    header_lines = request_text.split("\r\n")[1:]
    host_headers = [line for line in header_lines if line.lower().startswith("host:")]
    assert host_headers == ["Host: example-webhook.test"], request_text


def test_send_reads_at_most_the_body_cap_of_an_oversized_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A receiver announcing (and streaming) far more than the cap: `_send`
    # returns the status after reading `MAX_RESPONSE_BYTES`, not the body.
    monkeypatch.setattr(http_client.ssl, "create_default_context", lambda: _FakeTlsContext())
    announced = http_client.MAX_RESPONSE_BYTES * 64
    response = (
        f"HTTP/1.1 503 Busy\r\nContent-Length: {announced}\r\n\r\n".encode("ascii")
        + b"x" * (http_client.MAX_RESPONSE_BYTES * 2)
    )
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    _host, port = server.getsockname()
    thread = threading.Thread(target=_serve_one_request, args=(server, [], response))
    thread.start()
    try:
        import ipaddress

        status = _send(
            ipaddress.ip_address("127.0.0.1"), port, "h.test", "/", {}, b"{}", _TIMEOUT_SECONDS
        )
    finally:
        thread.join(timeout=5.0)
        server.close()
    assert status == 503
