"""`testdata/custom-domain/websocket_echo_server.py`, el eco que suben los e2e
de dominio propio al sandbox: se ejecuta en local, con la misma orden que el
e2e (`python3 <script> <puerto>`), y repite una trama enmascarada; una
conexión que cierra sin mandar la trama no lo mata."""

from __future__ import annotations

import base64
import hashlib
import secrets
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[4] / "testdata" / "custom-domain" / "websocket_echo_server.py"
)
WEBSOCKET_ACCEPT_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
LISTEN_TIMEOUT_SECONDS = 10.0
SOCKET_TIMEOUT_SECONDS = 5.0
POLL_SECONDS = 0.05


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _connect(port: int) -> socket.socket:
    deadline = time.monotonic() + LISTEN_TIMEOUT_SECONDS
    while True:
        try:
            return socket.create_connection(("127.0.0.1", port), timeout=SOCKET_TIMEOUT_SECONDS)
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(POLL_SECONDS)


@pytest.fixture
def port() -> Iterator[int]:
    chosen = _free_port()
    server = subprocess.Popen([sys.executable, "-I", str(SCRIPT), str(chosen)])
    try:
        _connect(chosen).close()
        yield chosen
    finally:
        server.kill()
        server.wait()


def _masked_text_frame(message: bytes) -> bytes:
    mask = secrets.token_bytes(4)
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(message))
    return bytes([0x81, 0x80 | len(message)]) + mask + masked


def test_the_script_is_valid_python() -> None:
    compile(SCRIPT.read_text(encoding="utf-8"), str(SCRIPT), "exec")


def test_it_accepts_the_upgrade_and_echoes_the_frame(port: int) -> None:
    key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
    with _connect(port) as connection:
        connection.sendall(
            (
                "GET / HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode("ascii")
        )
        head = b""
        while b"\r\n\r\n" not in head:
            head += connection.recv(4096)
        expected = base64.b64encode(
            hashlib.sha1((key + WEBSOCKET_ACCEPT_GUID).encode("ascii")).digest()
        ).decode("ascii")
        assert b" 101 " in head.split(b"\r\n", 1)[0]
        assert f"Sec-WebSocket-Accept: {expected}".encode("ascii") in head
        connection.sendall(_masked_text_frame(b"ping"))
        reply = connection.recv(4096)
    assert reply[2 : 2 + (reply[1] & 0x7F)] == b"ping"


def test_a_connection_closed_before_the_frame_does_not_stop_the_server(port: int) -> None:
    key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
    with _connect(port) as connection:
        connection.sendall(
            (f"GET / HTTP/1.1\r\nUpgrade: websocket\r\nSec-WebSocket-Key: {key}\r\n\r\n").encode(
                "ascii"
            )
        )
        connection.recv(4096)
    _connect(port).close()
