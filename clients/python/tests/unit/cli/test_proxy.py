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
import threading
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import cast

import boto3
import pytest
from typer.testing import CliRunner

from rayito._aws import PortSpec
from rayito._models import SandboxInfo
from rayito._transport import TokenRefresher, TokenStore
from rayito.cli import _proxy
from rayito.cli._console import EXIT_USAGE
from rayito.cli._session import Clients
from rayito.cli.app import app
from rayito.exceptions import InvalidArgumentException, SandboxStateException

from .conftest import REGION, FakeControlPlane, sandbox_info

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


def test_upgrade_header_without_connection_upgrade_is_not_an_upgrade() -> None:
    """RFC 9110 §7.8: `Upgrade` sólo cuenta con el token `upgrade` en
    `Connection`. Con `Connection: keep-alive` la conexión no es un upgrade:
    se quita el `Connection` del cliente y se fuerza `close`, para que no
    queden peticiones posteriores sin reescribir en la misma conexión."""
    request = head(
        [
            "GET / HTTP/1.1",
            "Host: localhost",
            "Upgrade: x",
            "Connection: keep-alive",
        ]
    )
    assert request.is_upgrade() is False
    rewritten = _proxy.rewrite_head(
        request, endpoint="abc.lambda-microvm.us-east-1.on.aws", jwe="JWE-1", port=8080
    ).decode("latin-1")
    assert "keep-alive" not in rewritten
    assert rewritten.count("Connection:") == 1
    assert "Connection: close" in rewritten


# --------------------------------------------------------------------------
# `parse_http_head`: HTTP/1.1 estricto, contra contrabando de cabeceras
# (`request smuggling` con un CR o LF suelto)
# --------------------------------------------------------------------------


def test_parse_http_head_rejects_a_bare_lf_that_smuggles_a_fake_header() -> None:
    """Repro exacto del hallazgo: un `\\n` suelto en el valor de `Foo` no debe
    colar una línea `X-aws-proxy-port` que `rewrite_head` no vería (su nombre
    exterior, `Foo`, no empieza por `x-aws-proxy-`)."""
    raw = b"GET / HTTP/1.1\r\nFoo: a\nX-aws-proxy-port: 9000\r\n\r\n"
    with pytest.raises(_proxy.MalformedHttpHeadError):
        _proxy.parse_http_head(raw)


@pytest.mark.parametrize(
    "raw",
    [
        b"GET / HTTP/1.1\r\nFoo: a\nX-aws-proxy-port: 9000\r\n\r\n",  # LF suelto
        b"GET / HTTP/1.1\r\nFoo: a\r\nBar\r\n\r\n",  # línea de cabecera sin ':'
        b"GET / HTTP/1.1\r\nFoo: a\r\n continuada\r\n\r\n",  # obs-fold (espacio)
        b"GET / HTTP/1.1\r\nFoo: a\r\n\tcontinuada\r\n\r\n",  # obs-fold (tab)
        b"GET / HTTP/1.1\r\nFo o: a\r\n\r\n",  # nombre con espacio
        b"GET / HTTP/1.1\r\nFoo\x01: a\r\n\r\n",  # nombre fuera de los tchar
        b"",  # sin terminador
        b"GET / HTTP/1.1\r\nFoo: a\r\n",  # sin línea en blanco final
    ],
)
def test_parse_http_head_rejects_malformed_heads(raw: bytes) -> None:
    with pytest.raises(_proxy.MalformedHttpHeadError):
        _proxy.parse_http_head(raw)


def test_parse_http_head_accepts_a_well_formed_head_with_no_headers() -> None:
    parsed = _proxy.parse_http_head(b"GET / HTTP/1.1\r\n\r\n")
    assert parsed.request_line == "GET / HTTP/1.1"
    assert parsed.headers == ()


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


@pytest.mark.parametrize("port", [0, -1, 65536, 1_000_000])
def test_validate_local_port_rejects_out_of_range(port: int) -> None:
    with pytest.raises(InvalidArgumentException):
        _proxy.validate_local_port(port)


def test_validate_local_port_accepts_the_hooks_port() -> None:
    """`--local-port` es un puerto del operador, no del guest: 9000 no tiene
    nada de especial aquí (a diferencia de `--port`)."""
    assert _proxy.validate_local_port(9000) == 9000


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
    connector: _proxy.Connector,
    jwe: str | None,
    *,
    port: int = 8080,
    head_timeout: float = _proxy.HEAD_READ_TIMEOUT_SECONDS,
    connect_timeout: float = _proxy.UPSTREAM_CONNECT_TIMEOUT_SECONDS,
) -> tuple[asyncio.Task[None], int]:
    """Los clientes de estos tests mandan `Host: localhost` sin puerto, que el
    proxy sólo admite como `--allowed-host` (sin puerto explícito)."""
    listener = _proxy.bind_listener_socket("127.0.0.1", 0)
    local_port = int(listener.getsockname()[1])
    spec = _proxy.ProxySpec(
        sandbox_id=SANDBOX_ID,
        port=port,
        endpoint="ignored.example.on.aws",
        bind="127.0.0.1",
        local_port=local_port,
        access=_proxy.build_access(
            bind="127.0.0.1",
            local_port=local_port,
            sandbox_id=SANDBOX_ID,
            allowed_hosts=["localhost"],
        ),
    )
    ready = asyncio.Event()
    box: dict[str, int] = {}

    def on_ready(server: asyncio.Server) -> None:
        box["port"] = server.sockets[0].getsockname()[1]
        ready.set()

    task = asyncio.create_task(
        _proxy.serve_proxy(
            spec,
            lambda: jwe,
            connector,
            listener=listener,
            ready=on_ready,
            head_timeout=head_timeout,
            connect_timeout=connect_timeout,
        )
    )
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


async def test_proxy_server_responds_400_and_closes_on_a_malformed_head() -> None:
    """Cabecera con un `\\n` suelto que intenta colar un `X-aws-proxy-port`
    falso: `400`, cierra, y nunca abre conexión al upstream (ni gasta el
    JWE)."""

    async def connector_never_called() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        raise AssertionError("no debería conectar al upstream con una cabecera inválida")

    task, proxy_port = await _start_proxy(connector_never_called, "JWE-BAD")
    try:
        client_reader, client_writer = await asyncio.open_connection("127.0.0.1", proxy_port)
        client_writer.write(b"GET / HTTP/1.1\r\nFoo: a\nX-aws-proxy-port: 9000\r\n\r\n")
        await client_writer.drain()
        response = await client_reader.read(-1)
        assert response == _proxy.BAD_REQUEST_RESPONSE
    finally:
        await _stop_proxy(task)


async def test_proxy_answers_502_when_there_is_no_jwe(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """El refresher lleva fallando más allá del TTL: `502` + `Connection:
    close` (no un cierre mudo) y una línea en stderr sin JWE, cabeceras ni
    ruta; nunca se abre conexión al upstream."""

    async def connector_never_called() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        raise AssertionError("no debería conectar al upstream sin JWE")

    task, proxy_port = await _start_proxy(connector_never_called, None)
    try:
        client_reader, client_writer = await asyncio.open_connection("127.0.0.1", proxy_port)
        client_writer.write(b"GET /private-path HTTP/1.1\r\nHost: localhost\r\n\r\n")
        await client_writer.drain()
        response = await client_reader.read(-1)
        assert response == _proxy.BAD_GATEWAY_RESPONSE
    finally:
        await _stop_proxy(task)
    err = capsys.readouterr().err
    assert "JWE" in err
    assert "private-path" not in err
    assert "localhost" not in err


async def test_proxy_answers_502_when_the_upstream_connect_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def connector_refused() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        raise ConnectionRefusedError("refused")

    task, proxy_port = await _start_proxy(connector_refused, "JWE-REFUSED")
    try:
        client_reader, client_writer = await asyncio.open_connection("127.0.0.1", proxy_port)
        client_writer.write(b"GET /private-path HTTP/1.1\r\nHost: localhost\r\n\r\n")
        await client_writer.drain()
        response = await client_reader.read(-1)
        assert response == _proxy.BAD_GATEWAY_RESPONSE
    finally:
        await _stop_proxy(task)
    err = capsys.readouterr().err
    assert "JWE-REFUSED" not in err
    assert "private-path" not in err
    assert "upstream" in err


async def test_proxy_answers_502_when_the_upstream_connect_times_out() -> None:
    async def connector_stalled() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        await asyncio.sleep(3600)
        raise AssertionError("inalcanzable")

    task, proxy_port = await _start_proxy(connector_stalled, "JWE-SLOW", connect_timeout=0.05)
    try:
        client_reader, client_writer = await asyncio.open_connection("127.0.0.1", proxy_port)
        client_writer.write(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
        await client_writer.drain()
        response = await asyncio.wait_for(client_reader.read(-1), 5)
        assert response == _proxy.BAD_GATEWAY_RESPONSE
    finally:
        await _stop_proxy(task)


async def test_proxy_closes_an_idle_client_after_the_header_timeout() -> None:
    async def connector_never_called() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        raise AssertionError("no debería conectar al upstream sin cabecera")

    task, proxy_port = await _start_proxy(connector_never_called, "JWE-IDLE", head_timeout=0.05)
    try:
        client_reader, client_writer = await asyncio.open_connection("127.0.0.1", proxy_port)
        client_writer.write(b"GET / HTTP/1.1\r\n")
        await client_writer.drain()
        response = await asyncio.wait_for(client_reader.read(-1), 5)
        assert response == b""
    finally:
        await _stop_proxy(task)


async def test_run_until_stopped_reraises_a_server_failure_instead_of_hanging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Con `stop_event` (el e2e), si `serve_proxy` falla (p. ej.
    `start_server` lanza), el error sale en vez de quedarse esperando a un
    `stop_event` que nadie marcará."""

    async def failing_serve_proxy(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("start_server falló")

    monkeypatch.setattr(_proxy, "serve_proxy", failing_serve_proxy)
    spec = _proxy.ProxySpec(
        sandbox_id=SANDBOX_ID,
        port=8080,
        endpoint="e",
        bind="127.0.0.1",
        local_port=0,
        access=_proxy.build_access(bind="127.0.0.1", local_port=0, sandbox_id=SANDBOX_ID),
    )
    stop_event = threading.Event()

    async def connector_never_called() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        raise AssertionError("inalcanzable")

    listener = _proxy.bind_listener_socket("127.0.0.1", 0)
    try:
        with pytest.raises(RuntimeError, match="start_server falló"):
            await asyncio.wait_for(
                _proxy._run_until_stopped(
                    spec,
                    lambda: "JWE",
                    connector_never_called,
                    listener,
                    lambda _server: None,
                    stop_event,
                ),
                5,
            )
    finally:
        stop_event.set()
        listener.close()


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


# --------------------------------------------------------------------------
# `run_proxy` valida (`--port`, `--local-port`) y reserva el socket local
# ANTES de tocar AWS: un puerto malo o ya ocupado no debe gastar ni
# `GetMicrovm` ni `CreateMicrovmAuthToken`
# --------------------------------------------------------------------------


def test_run_proxy_rejects_the_hooks_port_without_calling_get_microvm() -> None:
    """`FakeControlPlane()` no conoce `SANDBOX_ID`: si `get_microvm` se
    llamara, explotaría con `SandboxNotFoundException`, no con
    `InvalidArgumentException`."""
    plane = FakeControlPlane()
    with pytest.raises(InvalidArgumentException):
        _proxy.run_proxy(
            plane,
            sandbox_id=SANDBOX_ID,
            port=9000,
            local_port=9000,
            bind="127.0.0.1",
            on_ready=lambda _message: None,
        )
    assert plane.tokens == []


def test_run_proxy_rejects_an_invalid_local_port_without_calling_get_microvm() -> None:
    plane = FakeControlPlane()
    with pytest.raises(InvalidArgumentException):
        _proxy.run_proxy(
            plane,
            sandbox_id=SANDBOX_ID,
            port=8080,
            local_port=70000,
            bind="127.0.0.1",
            on_ready=lambda _message: None,
        )
    assert plane.tokens == []


def test_run_proxy_translates_a_bind_failure_without_calling_get_microvm() -> None:
    """Un `--local-port` ya ocupado falla en el `bind`, no en un traceback
    crudo de `asyncio.start_server`, y sin haber llamado a `GetMicrovm` ni
    `CreateMicrovmAuthToken`. `busy` tiene que estar en `listen()`, no sólo
    `bind()`-eado: con `SO_REUSEADDR` en ambos sockets, dos `bind()` al mismo
    puerto pueden convivir en Linux mientras ninguno escuche (el conflicto no
    sale hasta el primer `listen()`); un socket ya escuchando sí es un
    conflicto real en cualquier plataforma."""
    plane = FakeControlPlane()
    busy = _proxy.bind_listener_socket("127.0.0.1", 0)
    busy.listen(1)
    try:
        busy_port = busy.getsockname()[1]
        with pytest.raises(InvalidArgumentException, match=f"--local-port {busy_port}"):
            _proxy.run_proxy(
                plane,
                sandbox_id=SANDBOX_ID,
                port=8080,
                local_port=busy_port,
                bind="127.0.0.1",
                on_ready=lambda _message: None,
            )
        assert plane.tokens == []
    finally:
        busy.close()


@dataclass
class _CallTrackingControlPlane(FakeControlPlane):
    """Un `FakeControlPlane` que explota si algo llama a `GetMicrovm` o
    `CreateMicrovmAuthToken`: prueba de que la CLI valida `--port`/
    `--local-port` antes de tocar AWS."""

    def get_microvm(self, sandbox_id: str) -> SandboxInfo:
        raise AssertionError("no debería llamarse a GetMicrovm")

    def create_auth_token(self, sandbox_id: str, ports: Sequence[PortSpec]) -> str:
        raise AssertionError("no debería llamarse a CreateMicrovmAuthToken")


def _clients_with_no_call_control_plane() -> Clients:
    session = boto3.session.Session(
        region_name=REGION, aws_access_key_id="testing", aws_secret_access_key="testing"
    )
    return Clients(
        session=session, region=REGION, control_plane_override=_CallTrackingControlPlane()
    )


def test_proxy_command_rejects_the_hooks_port_without_touching_the_control_plane(
    runner: CliRunner,
) -> None:
    result = runner.invoke(
        app,
        ["sandbox", "proxy", SANDBOX_ID, "--port", "9000"],
        obj=_clients_with_no_call_control_plane(),
    )
    assert result.exit_code == 1, result.stderr


def test_proxy_command_rejects_an_invalid_local_port_without_touching_the_control_plane(
    runner: CliRunner,
) -> None:
    result = runner.invoke(
        app,
        ["sandbox", "proxy", SANDBOX_ID, "--port", "8080", "--local-port", "70000"],
        obj=_clients_with_no_call_control_plane(),
    )
    assert result.exit_code == 1, result.stderr


def test_proxy_command_reports_a_busy_local_port_cleanly(runner: CliRunner) -> None:
    """Un `--local-port` ocupado: mensaje limpio que nombra el puerto,
    salida 1, sin traceback y sin tocar AWS."""
    busy = _proxy.bind_listener_socket("127.0.0.1", 0)
    busy.listen(1)
    try:
        busy_port = busy.getsockname()[1]
        result = runner.invoke(
            app,
            ["sandbox", "proxy", SANDBOX_ID, "--port", "8080", "--local-port", str(busy_port)],
            obj=_clients_with_no_call_control_plane(),
        )
    finally:
        busy.close()
    assert result.exit_code == 1, result.output
    assert f"--local-port {busy_port}" in result.stderr
    assert "Traceback" not in result.output
    assert not isinstance(result.exception, OSError)


# --------------------------------------------------------------------------
# `Host`/`Origin` y tope de conexiones: una web cualquiera (DNS rebinding,
# POST entre sitios, WebSocket entre orígenes) no llega al guest con el JWE
# del operador. Se prueba `handle_connection` directamente, con un conector
# falso que registra si se llegó a abrir el upstream.
# --------------------------------------------------------------------------

LOCAL_PORT = 8080
ENDPOINT = "abc.lambda-microvm.us-east-1.on.aws"


class _RecordingWriter:
    """Lo mínimo de `asyncio.StreamWriter` que usa `handle_connection`."""

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.buffer += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None

    def can_write_eof(self) -> bool:
        return True

    def write_eof(self) -> None:
        return None


@dataclass
class _FakeConnector:
    """Un upstream en memoria: guarda la cabecera reescrita y responde `200`."""

    calls: int = 0
    upstream_writer: _RecordingWriter | None = None

    async def __call__(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        self.calls += 1
        reader = asyncio.StreamReader()
        reader.feed_data(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        reader.feed_eof()
        self.upstream_writer = _RecordingWriter()
        return reader, cast(asyncio.StreamWriter, self.upstream_writer)


def _access(
    *,
    bind: str = "127.0.0.1",
    allowed_hosts: Sequence[str] = (),
    allowed_origins: Sequence[str] = (),
) -> _proxy.ProxyAccess:
    return _proxy.build_access(
        bind=bind,
        local_port=LOCAL_PORT,
        sandbox_id=SANDBOX_ID,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )


async def _drive(
    raw: bytes,
    *,
    access: _proxy.ProxyAccess | None = None,
    slots: asyncio.Semaphore | None = None,
) -> tuple[bytes, _FakeConnector]:
    reader = asyncio.StreamReader()
    reader.feed_data(raw)
    reader.feed_eof()
    writer = _RecordingWriter()
    connector = _FakeConnector()
    await _proxy.handle_connection(
        reader,
        cast(asyncio.StreamWriter, writer),
        endpoint=ENDPOINT,
        port=8080,
        jwe_provider=lambda: "JWE-OPERATOR",
        connector=connector,
        access=access if access is not None else _access(),
        slots=slots if slots is not None else asyncio.Semaphore(1),
    )
    return bytes(writer.buffer), connector


@pytest.mark.parametrize(
    "host",
    [f"127.0.0.1:{LOCAL_PORT}", f"localhost:{LOCAL_PORT}", f"[::1]:{LOCAL_PORT}", "LocalHost:8080"],
)
async def test_loopback_hosts_reach_the_upstream(host: str) -> None:
    response, connector = await _drive(f"GET / HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())
    assert connector.calls == 1
    assert response.endswith(b"ok")


async def test_a_foreign_host_is_421_and_never_reaches_the_upstream() -> None:
    """DNS rebinding: `attacker.example` resuelve a 127.0.0.1 y el navegador
    manda su propio nombre en `Host`."""
    response, connector = await _drive(b"GET / HTTP/1.1\r\nHost: attacker.example:8080\r\n\r\n")
    assert response == _proxy.MISDIRECTED_REQUEST_RESPONSE
    assert connector.calls == 0


async def test_a_missing_host_is_421() -> None:
    response, connector = await _drive(b"GET / HTTP/1.1\r\nAccept: */*\r\n\r\n")
    assert response == _proxy.MISDIRECTED_REQUEST_RESPONSE
    assert connector.calls == 0


async def test_a_loopback_host_on_another_port_is_421() -> None:
    response, connector = await _drive(b"GET / HTTP/1.1\r\nHost: 127.0.0.1:9999\r\n\r\n")
    assert response == _proxy.MISDIRECTED_REQUEST_RESPONSE
    assert connector.calls == 0


async def test_a_foreign_origin_on_a_plain_post_is_403() -> None:
    """Un POST "simple" entre sitios (CSRF a ciegas): `Host` es el bueno
    porque el navegador conecta a 127.0.0.1, pero `Origin` delata la web."""
    response, connector = await _drive(
        b"POST /api/run HTTP/1.1\r\n"
        b"Host: 127.0.0.1:8080\r\n"
        b"Origin: http://attacker.example\r\n"
        b"Content-Length: 0\r\n\r\n"
    )
    assert response == _proxy.FORBIDDEN_RESPONSE
    assert connector.calls == 0


async def test_a_foreign_origin_on_a_websocket_upgrade_is_403() -> None:
    response, connector = await _drive(
        b"GET /ws HTTP/1.1\r\n"
        b"Host: 127.0.0.1:8080\r\n"
        b"Connection: Upgrade\r\n"
        b"Upgrade: websocket\r\n"
        b"Origin: http://attacker.example:8080\r\n\r\n"
    )
    assert response == _proxy.FORBIDDEN_RESPONSE
    assert connector.calls == 0


async def test_an_opaque_null_origin_is_403() -> None:
    response, connector = await _drive(
        b"POST / HTTP/1.1\r\nHost: 127.0.0.1:8080\r\nOrigin: null\r\n\r\n"
    )
    assert response == _proxy.FORBIDDEN_RESPONSE
    assert connector.calls == 0


async def test_a_same_origin_request_passes() -> None:
    response, connector = await _drive(
        b"POST / HTTP/1.1\r\nHost: localhost:8080\r\nOrigin: http://localhost:8080\r\n\r\n"
    )
    assert connector.calls == 1
    assert response.endswith(b"ok")


async def test_allow_origin_admits_an_explicit_origin() -> None:
    access = _access(allowed_origins=["https://notebook.example.com/"])
    response, connector = await _drive(
        b"GET / HTTP/1.1\r\nHost: 127.0.0.1:8080\r\nOrigin: https://notebook.example.com\r\n\r\n",
        access=access,
    )
    assert connector.calls == 1
    assert response.endswith(b"ok")


async def test_allowed_host_admits_the_name_with_and_without_the_listener_port() -> None:
    access = _access(bind="0.0.0.0", allowed_hosts=["devbox.example.com"])
    for host in ("devbox.example.com:8080", "devbox.example.com"):
        response, connector = await _drive(
            f"GET / HTTP/1.1\r\nHost: {host}\r\n\r\n".encode(), access=access
        )
        assert connector.calls == 1, host
        assert response.endswith(b"ok")


async def test_the_sandbox_localhost_name_is_admitted_on_a_loopback_bind() -> None:
    """`<id>.localhost` resuelve a loopback en los navegadores y tiene su
    propio tarro de cookies, separado del de `127.0.0.1`/`localhost`."""
    response, connector = await _drive(
        f"GET / HTTP/1.1\r\nHost: {SANDBOX_ID}.localhost:8080\r\n\r\n".encode()
    )
    assert connector.calls == 1
    assert response.endswith(b"ok")


async def test_a_full_connection_budget_is_503() -> None:
    slots = asyncio.Semaphore(1)
    await slots.acquire()
    response, connector = await _drive(
        b"GET / HTTP/1.1\r\nHost: 127.0.0.1:8080\r\n\r\n", slots=slots
    )
    assert response == _proxy.SERVICE_UNAVAILABLE_RESPONSE
    assert connector.calls == 0


async def test_the_slot_is_released_after_the_connection_ends() -> None:
    slots = asyncio.Semaphore(1)
    await _drive(b"GET / HTTP/1.1\r\nHost: 127.0.0.1:8080\r\n\r\n", slots=slots)
    assert not slots.locked()


def test_a_wildcard_bind_admits_no_host_by_itself() -> None:
    access = _access(bind="0.0.0.0")
    assert not access.admits_host("0.0.0.0:8080")


def test_a_concrete_bind_address_is_admitted() -> None:
    access = _access(bind="192.168.1.5")
    assert access.admits_host("192.168.1.5:8080")
    assert not access.admits_host(f"{SANDBOX_ID}.localhost:8080")


@pytest.mark.parametrize(
    "value", ["", "http://devbox.example.com", "devbox.example.com/x", "user@devbox", "a:b:c:"]
)
def test_invalid_allowed_host_values_are_rejected(value: str) -> None:
    with pytest.raises(InvalidArgumentException):
        _access(allowed_hosts=[value])


@pytest.mark.parametrize("value", ["", "devbox.example.com", "ftp://x", "null"])
def test_invalid_allow_origin_values_are_rejected(value: str) -> None:
    with pytest.raises(InvalidArgumentException):
        _access(allowed_origins=[value])


def test_wildcard_bind_without_allowed_host_is_a_usage_error(
    runner: CliRunner, clients: Clients
) -> None:
    result = runner.invoke(
        app,
        ["sandbox", "proxy", SANDBOX_ID, "--port", "8080", "--bind", "0.0.0.0", "--allow-remote"],
        obj=clients,
    )
    assert result.exit_code == EXIT_USAGE, result.stderr
    assert "--allowed-host" in result.stderr
