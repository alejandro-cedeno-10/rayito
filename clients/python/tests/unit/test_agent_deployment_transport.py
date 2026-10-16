"""Los transportes reales de `AgentDeployment` (`http.client`) y
`AsyncAgentDeployment` (`asyncio` + `_http_framing`) contra el host del
agente de verdad en un puerto del loopback (sin TLS: es lo único que cambia
respecto al endpoint de AWS), y contra un servidor `asyncio` que responde con
cada delimitación de cuerpo HTTP/1.1."""

from __future__ import annotations

import asyncio
import json
import socket
import threading

import pytest

from rayito._agent._deployment import AgentDeployment, HttpsTransport
from rayito._agent._deployment_async import AsyncAgentDeployment, AsyncHttpsTransport
from rayito._agent._events import Done
from rayito._limits import AGENT_SERVICE_PORT
from rayito.exceptions import AgentException, SandboxException
from tests.unit.agent_host_support import LOOPBACK, FakeDriver, running_service, step
from tests.unit.fake_agent_project import AsyncFakeSandbox, FakeSandbox

SESSION = "rda_" + "0" * 32


def _sync_deployment(port: int) -> AgentDeployment:
    return AgentDeployment(
        FakeSandbox(),
        port=AGENT_SERVICE_PORT,
        project="support-bot",
        request_timeout=10.0,
        transport_factory=lambda access: HttpsTransport(LOOPBACK, port=port, secure=False),
    )


def _async_deployment(port: int) -> AsyncAgentDeployment:
    sandbox = AsyncFakeSandbox()
    return AsyncAgentDeployment(
        sandbox,
        port=AGENT_SERVICE_PORT,
        host_access=FakeSandbox().get_host(AGENT_SERVICE_PORT),
        project="support-bot",
        request_timeout=10.0,
        transport_factory=lambda access: AsyncHttpsTransport(LOOPBACK, port=port, secure=False),
    )


def test_sync_client_against_the_real_host() -> None:
    driver = FakeDriver([*step(1, tokens=3, text="hola"), *step(2, tokens=2, text="adiós")])
    with running_service(driver) as (_, port):
        deployment = _sync_deployment(port)
        assert deployment.status().state == "healthy"
        result = deployment.invoke("saluda", session_id=SESSION)
        assert (result.text, result.steps, result.session_id) == ("adiós", 2, SESSION)
        assert result.usage.input == 5
        with deployment.stream("saluda") as events:
            kinds = [event.type for event in events]
        assert kinds[0] == "step_started"
        assert kinds[-1] == "done"
        assert deployment.stream("saluda").result().text == "adiós"


def test_sync_client_reports_busy_and_unreachable() -> None:
    gate = threading.Event()
    driver = FakeDriver(step(1, text="a"), gate=gate)
    with running_service(driver) as (_, port):
        deployment = _sync_deployment(port)
        first = threading.Thread(target=lambda: deployment.invoke("x"))
        first.start()
        assert driver.started.wait(5)
        assert deployment.status().state == "busy"
        with pytest.raises(AgentException) as raised:
            deployment.invoke("y")
        assert raised.value.reason == "busy"
        gate.set()
        first.join(5)
    assert deployment.status().state == "unavailable"
    with pytest.raises(SandboxException):
        deployment.invoke("z")


def test_async_client_against_the_real_host() -> None:
    driver = FakeDriver([*step(1, tokens=3, text="hola"), *step(2, tokens=2, text="adiós")])

    async def scenario(port: int) -> None:
        deployment = _async_deployment(port)
        assert (await deployment.status()).state == "healthy"
        result = await deployment.invoke("saluda", session_id=SESSION)
        assert (result.text, result.steps, result.session_id) == ("adiós", 2, SESSION)
        async with deployment.stream("saluda") as events:
            kinds = [event.type async for event in events]
        assert kinds[0] == "step_started" and kinds[-1] == "done"
        stream = deployment.stream("saluda")
        assert (await stream.result()).text == "adiós"

    with running_service(driver) as (_, port):
        asyncio.run(scenario(port))


def test_async_client_unreachable_is_a_sandbox_error() -> None:
    with socket.socket() as sock:
        sock.bind((LOOPBACK, 0))
        port = sock.getsockname()[1]

    async def scenario() -> None:
        deployment = _async_deployment(port)
        assert (await deployment.status()).state == "unavailable"
        with pytest.raises(SandboxException):
            await deployment.invoke("hola")

    asyncio.run(scenario())


RESULT = json.dumps(
    {
        "v": 1,
        "text": "listo",
        "session_id": "ses_a",
        "usage": {},
        "steps": 1,
        "failed": None,
    }
).encode()
SSE_BODY = (
    b'event: text\ndata: {"v":1,"type":"text","text":"x"}\n\n'
    b'event: done\ndata: {"v":1,"type":"done","session_id":"ses_a"}\n\n'
)


def _chunked(body: bytes, size: int) -> bytes:
    pieces = [body[i : i + size] for i in range(0, len(body), size)]
    encoded = b"".join(f"{len(piece):x};ext=1\r\n".encode() + piece + b"\r\n" for piece in pieces)
    return encoded + b"0\r\n\r\n"


HEAD = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
RESPONSES: dict[str, bytes] = {
    "length": HEAD
    + f"Content-Length: {len(RESULT)}\r\nConnection: close\r\n\r\n".encode()
    + RESULT,
    "chunked": HEAD + b"Transfer-Encoding: chunked\r\n\r\n" + _chunked(RESULT, 7),
    "until-close": HEAD + b"Connection: close\r\n\r\n" + RESULT,
}


async def _serve_raw(payload: bytes, seen: list[bytes]) -> tuple[asyncio.Server, int]:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        seen.append(head)
        length = int(
            next(
                line.split(b":")[1]
                for line in head.split(b"\r\n")
                if line.lower().startswith(b"content-length")
            )
        )
        await reader.readexactly(length)
        writer.write(payload)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, LOOPBACK, 0)
    return server, int(server.sockets[0].getsockname()[1])


@pytest.mark.parametrize("framing", list(RESPONSES))
def test_async_client_reads_every_body_framing(framing: str) -> None:
    seen: list[bytes] = []

    async def scenario() -> None:
        server, port = await _serve_raw(RESPONSES[framing], seen)
        async with server:
            result = await _async_deployment(port).invoke("hola")
        assert result.text == "listo"

    asyncio.run(scenario())
    head = seen[0].decode().lower()
    assert head.startswith("post /invocations http/1.1")
    assert "x-aws-proxy-auth: jwe-1" in head and "connection: close" in head
    assert "accept: application/json" in head


def test_async_client_streams_a_chunked_sse_body() -> None:
    payload = (
        b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n"
        + _chunked(SSE_BODY, 5)
    )

    async def scenario() -> None:
        server, port = await _serve_raw(payload, [])
        async with server, _async_deployment(port).stream("hola") as events:
            final = [event async for event in events][-1]
        assert isinstance(final, Done) and final.session_id == "ses_a"

    asyncio.run(scenario())


def test_async_client_truncated_body_is_a_sandbox_error() -> None:
    payload = HEAD + b"Content-Length: 500\r\nConnection: close\r\n\r\n" + RESULT

    async def scenario() -> None:
        server, port = await _serve_raw(payload, [])
        async with server:
            with pytest.raises(SandboxException):
                await _async_deployment(port).invoke("hola")

    asyncio.run(scenario())


def test_async_client_garbage_head_is_a_sandbox_error() -> None:
    async def scenario() -> None:
        server, port = await _serve_raw(b"esto no es http\r\n\r\n", [])
        async with server:
            with pytest.raises(SandboxException):
                await _async_deployment(port).invoke("hola")

    asyncio.run(scenario())
