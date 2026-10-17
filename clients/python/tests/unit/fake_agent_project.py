"""Dobles de prueba de `AgentProject.deploy()` y `AgentDeployment`
(ADR-028): un sandbox que anota todo lo que se le pide, un transporte HTTP
con respuestas guiadas y una clase `Sandbox` con `create()` y `connect()`.
Nunca tocan red, `grpc` ni AWS."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from rayito._agent._domain import AgentModel, AgentSpec
from rayito._agent._gateways import bedrock_gateway
from rayito._agent._project_domain import AgentProject, ServeOptions
from rayito._limits import AGENT_SERVICE_READY_MARKER_PATH
from rayito._models import HostAccess
from rayito._secret_gateway._domain import GatewayStatus

SANDBOX_ID = "microvm-00000000-0000-0000-0000-000000000001"
ENDPOINT = "abc.microvm.example.com"
MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
GATEWAY_PORT = 8180
GATEWAY_URL = f"http://127.0.0.1:{GATEWAY_PORT}"
FAILED_MARKER = AGENT_SERVICE_READY_MARKER_PATH + ".failed"


def project(runtime: Any = "deepagents", **overrides: Any) -> AgentProject:
    spec = AgentSpec(
        model=AgentModel(provider="bedrock", id=MODEL_ID, gateway="bedrock", region="us-east-1")
    )
    arguments: dict[str, Any] = {
        "name": "support-bot",
        "runtime": runtime,
        "spec": spec,
        "gateways": {
            "bedrock": bedrock_gateway("bedrock-key", region="us-east-1", models=[MODEL_ID])
        },
        "serve": ServeOptions(),
        **overrides,
    }
    return AgentProject(**arguments)


class FakeToken:
    """El JWE vigente del proxy: `bump()` simula el reacuñado."""

    def __init__(self) -> None:
        self.generation = 1

    @property
    def jwe(self) -> str:
        return f"jwe-{self.generation}"


class FakeRefresher:
    def __init__(self, token: FakeToken) -> None:
        self._token = token
        self.refreshed = 0

    def refresh_all(self) -> None:
        self.refreshed += 1
        self._token.generation += 1


class AsyncFakeRefresher(FakeRefresher):
    async def refresh_all(self) -> None:  # type: ignore[override]
        FakeRefresher.refresh_all(self)


@dataclass
class FakeHandle:
    disconnected: bool = False
    exit_code: int | None = None

    def disconnect(self) -> None:
        self.disconnected = True


class FakeFiles:
    """`exists()` contesta según `ready_after` consultas del marcador de
    listo, y `failed_code` simula un arranque fallido del host."""

    def __init__(
        self,
        *,
        ready_after: int | None = 0,
        failed_code: str | None = None,
        failed_marker: str | None = None,
    ) -> None:
        self.written: list[Any] = []
        self.ready_after = ready_after
        self.failed_code = failed_code
        self.failed_marker = failed_marker
        self.ready_polls = 0

    def write_files(self, entries: Sequence[Any]) -> None:
        self.written.extend(entries)

    def exists(self, path: str) -> bool:
        if path == AGENT_SERVICE_READY_MARKER_PATH:
            self.ready_polls += 1
            return self.ready_after is not None and self.ready_polls > self.ready_after
        has_failed = self.failed_code is not None or self.failed_marker is not None
        return path == FAILED_MARKER and has_failed

    def read(self, path: str) -> str:
        assert path == FAILED_MARKER
        return self.failed_marker or f"{self.failed_code}\n"


class AsyncFakeFiles(FakeFiles):
    async def write_files(self, entries: Sequence[Any]) -> None:  # type: ignore[override]
        FakeFiles.write_files(self, entries)

    async def exists(self, path: str) -> bool:  # type: ignore[override]
        return FakeFiles.exists(self, path)

    async def read(self, path: str) -> str:  # type: ignore[override]
        return FakeFiles.read(self, path)


class FakeCommands:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.handle = FakeHandle()

    def run(self, cmd: str, **kwargs: Any) -> FakeHandle:
        self.calls.append((cmd, kwargs))
        return self.handle


class AsyncFakeCommands(FakeCommands):
    async def run(self, cmd: str, **kwargs: Any) -> FakeHandle:  # type: ignore[override]
        return FakeCommands.run(self, cmd, **kwargs)


class FakeStream:
    """El `AgentStream` de doble: itera `events` y `result()` devuelve el
    resultado o lanza el error guiado, como el real ante un `AgentFailed`."""

    def __init__(self, events: Sequence[Any], error: Exception | None, result: Any) -> None:
        self._events = iter(events)
        self._error = error
        self._result = result
        self.closed = False

    def __enter__(self) -> FakeStream:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.closed = True

    def __iter__(self) -> Iterator[Any]:
        return self._events

    def result(self) -> Any:
        for _ in self._events:
            pass
        if self._error is not None:
            raise self._error
        return self._result


class AsyncFakeStream(FakeStream):
    def __aiter__(self) -> AsyncFakeStream:
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._events)
        except StopIteration:
            raise StopAsyncIteration from None

    async def result(self) -> Any:
        return FakeStream.result(self)

    async def aclose(self) -> None:
        self.closed = True


class FakeAgent:
    stream_class: type[FakeStream] = FakeStream

    def __init__(
        self,
        error: Exception | None = None,
        result: Any = "resultado",
        events: Sequence[Any] = (),
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.error = error
        self.result = result
        self.events = tuple(events)
        self.streams: list[FakeStream] = []

    def stream(self, prompt: str, **kwargs: Any) -> Any:
        self.calls.append((prompt, kwargs))
        stream = self.stream_class(self.events, self.error, self.result)
        self.streams.append(stream)
        return stream


class AsyncFakeAgent(FakeAgent):
    stream_class = AsyncFakeStream

    async def stream(self, prompt: str, **kwargs: Any) -> Any:
        return FakeAgent.stream(self, prompt, **kwargs)


class FakeSandbox:
    """Un sandbox de doble. `kills` cuenta los `kill()`."""

    files_class = FakeFiles
    commands_class = FakeCommands
    agent_class = FakeAgent
    refresher_class = FakeRefresher

    def __init__(
        self,
        *,
        metadata: Mapping[str, str] | None = None,
        files: FakeFiles | None = None,
        agent: FakeAgent | None = None,
        kill_error: Exception | None = None,
    ) -> None:
        self.sandbox_id = SANDBOX_ID
        self.access_token = "fake-access-token"
        self.pause_calls: list[dict[str, Any]] = []
        self.resume_calls: list[dict[str, Any]] = []
        self.gateways = {"bedrock": GatewayStatus(port=GATEWAY_PORT)}
        self.metadata = dict(metadata or {})
        self.files = files or self.files_class()
        self.commands = self.commands_class()
        self.agent = agent or self.agent_class()
        self.token = FakeToken()
        self._refresher = self.refresher_class(self.token)
        self.kills = 0
        self.kill_error = kill_error
        self.host_ports: list[int] = []

    def get_host(self, port: int) -> HostAccess:
        self.host_ports.append(port)
        return HostAccess(ENDPOINT, port=port, token_provider=lambda: self.token.jwe)

    def kill(self) -> bool:
        self.kills += 1
        if self.kill_error is not None:
            raise self.kill_error
        return True

    def pause(self, **kwargs: Any) -> bool:
        self.pause_calls.append(kwargs)
        return True

    def resume(self, **kwargs: Any) -> None:
        self.resume_calls.append(kwargs)


class AsyncFakeSandbox(FakeSandbox):
    files_class = AsyncFakeFiles
    commands_class = AsyncFakeCommands
    agent_class = AsyncFakeAgent
    refresher_class = AsyncFakeRefresher

    async def get_host(self, port: int) -> HostAccess:  # type: ignore[override]
        return FakeSandbox.get_host(self, port)

    async def kill(self) -> bool:  # type: ignore[override]
        return FakeSandbox.kill(self)

    async def pause(self, **kwargs: Any) -> bool:  # type: ignore[override]
        return FakeSandbox.pause(self, **kwargs)

    async def resume(self, **kwargs: Any) -> None:  # type: ignore[override]
        FakeSandbox.resume(self, **kwargs)


class FakeSandboxClass:
    """`Sandbox.create()` de doble: anota los argumentos y devuelve el
    sandbox preparado (o lanza)."""

    def __init__(self, sandbox: FakeSandbox, *, error: Exception | None = None) -> None:
        self.sandbox = sandbox
        self.error = error
        self.created: list[tuple[str, dict[str, Any]]] = []

    def create(self, template: str, **kwargs: Any) -> FakeSandbox:
        self.created.append((template, kwargs))
        if self.error is not None:
            raise self.error
        return self.sandbox


class AsyncFakeSandboxClass(FakeSandboxClass):
    async def create(self, template: str, **kwargs: Any) -> FakeSandbox:  # type: ignore[override]
        return FakeSandboxClass.create(self, template, **kwargs)


@dataclass
class Scripted:
    """Una respuesta guiada: estado y cuerpo (en trozos si es un stream)."""

    status: int = 200
    body: bytes = b""
    chunks: Sequence[bytes] = ()


@dataclass
class Recorded:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes | None
    timeout: float


class FakeResponse:
    def __init__(self, scripted: Scripted) -> None:
        self.status = scripted.status
        self._body = scripted.body
        self._chunks = list(scripted.chunks) or ([scripted.body] if scripted.body else [])
        self.closed = False

    def read(self) -> bytes:
        return self._body or b"".join(self._chunks)

    def chunks(self) -> Iterator[bytes]:
        yield from self._chunks

    def close(self) -> None:
        self.closed = True


class AsyncFakeResponse(FakeResponse):
    async def read(self) -> bytes:  # type: ignore[override]
        return FakeResponse.read(self)

    async def _aiter(self) -> AsyncIterator[bytes]:
        for chunk in FakeResponse.chunks(self):
            yield chunk

    def chunks(self) -> AsyncIterator[bytes]:  # type: ignore[override]
        return self._aiter()

    async def close(self) -> None:  # type: ignore[override]
        FakeResponse.close(self)


class ScriptedTransport:
    """Un transporte con una lista de respuestas, una por petición, y
    el registro de lo enviado."""

    response_class: type[FakeResponse] = FakeResponse

    def __init__(self, *responses: Scripted | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[Recorded] = []
        self.opened: list[FakeResponse] = []

    def _next(
        self, method: str, path: str, headers: Mapping[str, str], body: bytes | None, timeout: float
    ) -> FakeResponse:
        self.requests.append(Recorded(method, path, dict(headers), body, timeout))
        scripted = self.responses.pop(0)
        if isinstance(scripted, Exception):
            raise scripted
        response = self.response_class(scripted)
        self.opened.append(response)
        return response

    def request(
        self, method: str, path: str, headers: Mapping[str, str], body: bytes | None, timeout: float
    ) -> FakeResponse:
        return self._next(method, path, headers, body, timeout)

    def factory(self, host_access: HostAccess) -> ScriptedTransport:
        return self


class AsyncScriptedTransport(ScriptedTransport):
    response_class = AsyncFakeResponse

    async def request(  # type: ignore[override]
        self, method: str, path: str, headers: Mapping[str, str], body: bytes | None, timeout: float
    ) -> FakeResponse:
        return self._next(method, path, headers, body, timeout)


def json_body(payload: object) -> bytes:
    return json.dumps(payload).encode()


def sse(*messages: tuple[str, dict[str, Any]]) -> bytes:
    return b"".join(
        f"event: {kind}\ndata: {json.dumps({'v': 1, 'type': kind, **data})}\n\n".encode()
        for kind, data in messages
    )


RESULT_BODY: dict[str, Any] = {
    "v": 1,
    "text": "hola mundo",
    "session_id": "ses_0000000000000000000000000a",
    "usage": {"input": 10, "output": 5, "reasoning": 0, "cache_read": 0, "cache_write": 0},
    "steps": 2,
    "failed": None,
}


@dataclass
class Capture:
    """Para pruebas que quieren inspeccionar varias llamadas."""

    items: list[Any] = field(default_factory=list)
