"""`rayito sandbox proxy`: reescritura de cabeceras (función pura), rechazo
del puerto de hooks y de rangos inválidos, el JWE se sirve del `TokenStore`
sin reacuñar en cada lectura y sólo se renueva al vencer el TTL, un servidor
de extremo a extremo contra un upstream falso en loopback (GET y Upgrade,
sin TLS, con una fábrica de conectores inyectada), `--bind` fuera de
loopback sin `--allow-remote` es un error de uso, y el JWE nunca aparece en
ningún log."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence

import pytest
from typer.testing import CliRunner

from rayito._aws import PortSpec
from rayito._transport import TokenRefresher, TokenStore
from rayito.cli import _proxy
from rayito.cli._console import EXIT_USAGE
from rayito.cli._session import Clients
from rayito.cli.app import app
from rayito.exceptions import InvalidArgumentException, SandboxStateException

from .conftest import FakeControlPlane, sandbox_info

SANDBOX_ID = "microvm-x"


def head(lines: list[str]) -> _proxy.HttpHead:
    return _proxy.parse_http_head(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))


# --------------------------------------------------------------------------
# Reescritura de cabeceras (función pura)
# --------------------------------------------------------------------------


def test_rewrite_head_strips_client_proxy_headers_sets_host_and_auth() -> None:
    request = head(
        [
            "GET /status HTTP/1.1",
            "Host: localhost:8000",
            "X-aws-proxy-auth: forged-by-client",
            "x-aws-proxy-port: 1",
            "X-Aws-Proxy-Force-H2: true",
            "Accept: */*",
        ]
    )
    rewritten = _proxy.rewrite_head(
        request, endpoint="abc.lambda-microvm.us-east-1.on.aws", jwe="JWE-1", port=8080
    ).decode("latin-1")

    assert "forged-by-client" not in rewritten
    assert rewritten.count("X-aws-proxy-auth") == 1
    assert "X-aws-proxy-auth: JWE-1" in rewritten
    assert "X-aws-proxy-port: 8080" in rewritten
    assert "Host: abc.lambda-microvm.us-east-1.on.aws" in rewritten
    assert "localhost:8000" not in rewritten
    assert "Connection: close" in rewritten
    assert "Accept: */*" in rewritten
    assert rewritten.startswith("GET /status HTTP/1.1\r\n")
    assert rewritten.endswith("\r\n\r\n")


def test_rewrite_head_keeps_upgrade_and_connection_intact() -> None:
    request = head(
        [
            "GET /ws HTTP/1.1",
            "Host: localhost:8000",
            "Connection: Upgrade",
            "Upgrade: websocket",
            "X-aws-proxy-auth: forged",
        ]
    )
    rewritten = _proxy.rewrite_head(
        request, endpoint="abc.lambda-microvm.us-east-1.on.aws", jwe="JWE-1", port=8080
    ).decode("latin-1")

    assert "Connection: Upgrade" in rewritten
    assert "Connection: close" not in rewritten
    assert "Upgrade: websocket" in rewritten
    assert "forged" not in rewritten


# --------------------------------------------------------------------------
# Validación del puerto
# --------------------------------------------------------------------------


def test_validate_proxy_port_rejects_hooks_port() -> None:
    with pytest.raises(InvalidArgumentException):
        _proxy.validate_proxy_port(9000)


@pytest.mark.parametrize("port", [0, -1, 65536, 1_000_000])
def test_validate_proxy_port_rejects_out_of_range(port: int) -> None:
    with pytest.raises(InvalidArgumentException):
        _proxy.validate_proxy_port(port)


@pytest.mark.parametrize("port", [1, 8080, 65535])
def test_validate_proxy_port_accepts_valid_ports(port: int) -> None:
    assert _proxy.validate_proxy_port(port) == port


# --------------------------------------------------------------------------
# `resolve_endpoint` / `build_refresher`: nunca `allPorts`, nunca termina un
# sandbox ya terminado
# --------------------------------------------------------------------------


def test_resolve_endpoint_rejects_a_terminated_sandbox() -> None:
    plane = FakeControlPlane(infos={SANDBOX_ID: sandbox_info(SANDBOX_ID, "TERMINATED")})
    with pytest.raises(SandboxStateException):
        _proxy.resolve_endpoint(plane, SANDBOX_ID)


def test_resolve_endpoint_returns_the_endpoint_of_a_running_sandbox() -> None:
    plane = FakeControlPlane(
        infos={SANDBOX_ID: sandbox_info(SANDBOX_ID, "RUNNING", endpoint="e.example.on.aws")}
    )
    assert _proxy.resolve_endpoint(plane, SANDBOX_ID) == "e.example.on.aws"


def test_build_refresher_mints_a_single_port_never_all_ports() -> None:
    plane = FakeControlPlane()
    _proxy.build_refresher(plane, SANDBOX_ID, 8080)
    assert plane.tokens == [(SANDBOX_ID, (PortSpec.single(8080),))]


# --------------------------------------------------------------------------
# El JWE viene del `TokenStore`: nunca se reacuña en cada lectura, sólo al
# vencer el TTL (reloj falso) — el requisito de "nunca cada rato"
# --------------------------------------------------------------------------


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def test_jwe_provider_does_not_remint_on_every_read_only_on_ttl_expiry() -> None:
    clock = FakeClock()
    minted: list[tuple[PortSpec, ...]] = []

    def mint(ports: Sequence[PortSpec]) -> str:
        minted.append(tuple(ports))
        return f"jwe-{len(minted)}"

    store = TokenStore()
    refresher = TokenRefresher(store, mint, clock=clock)
    refresher.mint((PortSpec.single(4000),))

    def provider() -> str | None:
        return store.jwe_for(4000)

    assert provider() == "jwe-1"
    for _ in range(10):
        assert provider() == "jwe-1"
    assert len(minted) == 1, "leer el JWE no debe acuñar uno nuevo"

    clock.advance(46 * 60)
    assert refresher.refresh_due() is True
    assert len(minted) == 2
    assert provider() == "jwe-2"


# --------------------------------------------------------------------------
# Servidor de extremo a extremo contra un upstream falso en loopback (sin
# TLS, fábrica de conectores inyectada)
# --------------------------------------------------------------------------


UpstreamHandler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


class _FakeUpstream:
    """Un servidor TCP en loopback que graba la cabecera recibida."""

    def __init__(self, handle: UpstreamHandler) -> None:
        self._handle = handle
        self.received_head: bytes | None = None
        self.server: asyncio.Server | None = None

    @property
    def port(self) -> int:
        assert self.server is not None
        return int(self.server.sockets[0].getsockname()[1])

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)

    def connector(self) -> _proxy.Connector:
        async def connect() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
            return await asyncio.open_connection("127.0.0.1", self.port)

        return connect

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()


async def _echo_upstream(record: dict[str, bytes]) -> UpstreamHandler:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        record["head"] = await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi")
        await writer.drain()
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

    return handle


async def _start_proxy(
    connector: _proxy.Connector, jwe: str, *, port: int = 8080
) -> tuple[asyncio.Task[None], int]:
    spec = _proxy.ProxySpec(
        sandbox_id=SANDBOX_ID,
        port=port,
        endpoint="ignored.example.on.aws",
        bind="127.0.0.1",
        local_port=0,
    )
    ready = asyncio.Event()
    box: dict[str, int] = {}

    def on_ready(server: asyncio.Server) -> None:
        box["port"] = server.sockets[0].getsockname()[1]
        ready.set()

    task = asyncio.create_task(_proxy.serve_proxy(spec, lambda: jwe, connector, ready=on_ready))
    await ready.wait()
    return task, box["port"]


async def _stop_proxy(task: asyncio.Task[None]) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_proxy_server_rewrites_headers_and_pipes_a_get_request() -> None:
    record: dict[str, bytes] = {}
    upstream = _FakeUpstream(await _echo_upstream(record))
    await upstream.start()
    task, proxy_port = await _start_proxy(upstream.connector(), "JWE-GET")
    try:
        client_reader, client_writer = await asyncio.open_connection("127.0.0.1", proxy_port)
        client_writer.write(
            b"GET /status HTTP/1.1\r\nHost: localhost\r\nX-aws-proxy-auth: forged\r\n\r\n"
        )
        await client_writer.drain()
        response = await client_reader.read(-1)
        client_writer.close()
        with contextlib.suppress(Exception):
            await client_writer.wait_closed()

        assert response == b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi"
        head_text = record["head"].decode("latin-1")
        assert "forged" not in head_text
        assert "X-aws-proxy-auth: JWE-GET" in head_text
        assert "X-aws-proxy-port: 8080" in head_text
        assert "Host: ignored.example.on.aws" in head_text
        assert "Connection: close" in head_text
    finally:
        await _stop_proxy(task)
        await upstream.close()


async def test_proxy_server_keeps_upgrade_connections_open_both_ways() -> None:
    record: dict[str, bytes] = {}

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        record["head"] = await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n"
        )
        await writer.drain()
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

    upstream = _FakeUpstream(handle)
    await upstream.start()
    task, proxy_port = await _start_proxy(upstream.connector(), "JWE-WS")
    try:
        client_reader, client_writer = await asyncio.open_connection("127.0.0.1", proxy_port)
        client_writer.write(
            b"GET /ws HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Connection: Upgrade\r\n"
            b"Upgrade: websocket\r\n"
            b"\r\n"
        )
        await client_writer.drain()
        handshake = await client_reader.readuntil(b"\r\n\r\n")
        assert handshake == (
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n"
        )

        client_writer.write(b"ping-frame")
        await client_writer.drain()
        echoed = await client_reader.readexactly(len(b"ping-frame"))
        assert echoed == b"ping-frame"

        client_writer.close()
        with contextlib.suppress(Exception):
            await client_writer.wait_closed()

        head_text = record["head"].decode("latin-1")
        assert "Connection: Upgrade" in head_text
        assert "Upgrade: websocket" in head_text
        assert "Connection: close" not in head_text
    finally:
        await _stop_proxy(task)
        await upstream.close()


async def test_proxy_never_logs_the_jwe(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    secret = "SUPER-SECRET-JWE-DO-NOT-LOG"
    record: dict[str, bytes] = {}
    upstream = _FakeUpstream(await _echo_upstream(record))
    await upstream.start()
    task, proxy_port = await _start_proxy(upstream.connector(), secret)
    try:
        client_reader, client_writer = await asyncio.open_connection("127.0.0.1", proxy_port)
        client_writer.write(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
        await client_writer.drain()
        await client_reader.read(-1)
        client_writer.close()
        with contextlib.suppress(Exception):
            await client_writer.wait_closed()
    finally:
        await _stop_proxy(task)
        await upstream.close()

    for logged in caplog.records:
        assert secret not in logged.getMessage()


# --------------------------------------------------------------------------
# `--bind` fuera de loopback sin `--allow-remote`: error de uso
# --------------------------------------------------------------------------


def test_bind_outside_loopback_without_allow_remote_is_a_usage_error(
    runner: CliRunner, clients: Clients
) -> None:
    result = runner.invoke(
        app,
        ["sandbox", "proxy", SANDBOX_ID, "--port", "8080", "--bind", "0.0.0.0"],
        obj=clients,
    )
    assert result.exit_code == EXIT_USAGE, result.stderr
    assert "allow-remote" in result.stderr


def test_is_loopback_bind() -> None:
    assert _proxy.is_loopback_bind("127.0.0.1")
    assert _proxy.is_loopback_bind("::1")
    assert _proxy.is_loopback_bind("localhost")
    assert not _proxy.is_loopback_bind("0.0.0.0")
    assert not _proxy.is_loopback_bind("192.168.1.5")
