"""`AsyncAgentDeployment`: el espejo asíncrono de `AgentDeployment`
(ADR-028, D6). Habla `rayito.agent-service/1` por el proxy de AWS con
`asyncio` sobre TLS y la delimitación HTTP/1.1 que ya usa `rayito sandbox
proxy` (`cli/_http_framing.py`); una respuesta `403` del proxy reacuña el
token y reintenta una vez.

`url` y `headers` son propiedades síncronas: `get_host()` de un
`AsyncSandbox` se espera una vez al crear o conectar el despliegue, y desde
entonces las cabeceras leen el JWE vigente del almacén de tokens, que el
refresco en segundo plano renueva.
"""

from __future__ import annotations

import asyncio
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from types import TracebackType
from typing import TYPE_CHECKING, Any, Final, Protocol, Self

from rayito._agent._deployment import (
    DEFAULT_DEPLOYMENT_REQUEST_TIMEOUT_SECONDS,
    PING_TIMEOUT_SECONDS,
    PROXY_REMINT_ATTEMPTS,
)
from rayito._agent._deployment_base import (
    EVENT_STREAM_CONTENT_TYPE,
    HTTP_FORBIDDEN,
    HTTP_OK,
    JSON_CONTENT_TYPE,
    DeploymentStatus,
    EventDecoder,
    ResultBuilder,
    http_error,
    invocation_body,
    invocation_headers,
    parse_labels,
    require_invocable,
    result_from_body,
    status_from_ping,
)
from rayito._agent._events import AgentEvent, AgentResult
from rayito._agent._sse import SseParser
from rayito._agent._stream_base import require_prompt
from rayito._limits import (
    AGENT_SERVICE_HEALTH_PATH,
    AGENT_SERVICE_INVOKE_PATH,
    ENDPOINT_TLS_PORT,
    PROXY_HEAD_MAX_BYTES,
)
from rayito.cli._http_framing import (
    CONTENT_LENGTH_HEADER_NAME,
    TRANSFER_ENCODING_HEADER_NAME,
    BodyKind,
    MalformedFramingError,
    chunk_size,
    response_body_framing,
    response_status,
)
from rayito.exceptions import AuthenticationException, SandboxException

if TYPE_CHECKING:
    from rayito._models import HostAccess

HEAD_TERMINATOR: Final = b"\r\n\r\n"
CRLF: Final = b"\r\n"
#: Fragmento que se lee del cuerpo de una respuesta.
READ_CHUNK_BYTES: Final = 8192


class AsyncDeploymentResponse(Protocol):
    """Lo que el cliente asíncrono necesita de una respuesta HTTP."""

    @property
    def status(self) -> int: ...

    async def read(self) -> bytes: ...

    def chunks(self) -> AsyncIterator[bytes]: ...

    async def close(self) -> None: ...


class AsyncDeploymentTransport(Protocol):
    async def request(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> AsyncDeploymentResponse: ...


def _header_values(headers: list[tuple[str, str]], name: str) -> list[str]:
    return [value for key, value in headers if key.lower() == name.lower()]


class AsyncHttpsResponse:
    """Una respuesta HTTP/1.1 leída de un `StreamReader` con la
    delimitación de `_http_framing`."""

    def __init__(
        self,
        status: int,
        headers: list[tuple[str, str]],
        method: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self._status = status
        self._reader = reader
        self._writer = writer
        self._framing = response_body_framing(
            status,
            request_method=method,
            transfer_encodings=_header_values(headers, TRANSFER_ENCODING_HEADER_NAME),
            content_lengths=_header_values(headers, CONTENT_LENGTH_HEADER_NAME),
        )

    @property
    def status(self) -> int:
        return self._status

    async def read(self) -> bytes:
        return b"".join([chunk async for chunk in self.chunks()])

    async def chunks(self) -> AsyncIterator[bytes]:
        kind = self._framing.kind
        try:
            if kind is BodyKind.LENGTH:
                remaining = self._framing.length
                while remaining:
                    data = await self._reader.read(min(READ_CHUNK_BYTES, remaining))
                    if not data:
                        raise SandboxException("el agente desplegado cortó la respuesta")
                    remaining -= len(data)
                    yield data
            elif kind is BodyKind.CHUNKED:
                async for data in self._chunked():
                    yield data
            elif kind is BodyKind.UNTIL_CLOSE:
                while data := await self._reader.read(READ_CHUNK_BYTES):
                    yield data
        except (OSError, asyncio.IncompleteReadError, MalformedFramingError):
            raise SandboxException("el agente desplegado cortó la respuesta") from None

    async def _chunked(self) -> AsyncIterator[bytes]:
        while True:
            size = chunk_size(await self._reader.readline())
            if size == 0:
                return
            data = await self._reader.readexactly(size)
            await self._reader.readexactly(len(CRLF))
            yield data

    async def close(self) -> None:
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except OSError:
            return


def _request_head(
    method: str, path: str, host: str, headers: Mapping[str, str], size: int
) -> bytes:
    lines = [f"{method} {path} HTTP/1.1", f"Host: {host}"]
    lines.extend(f"{name}: {value}" for name, value in headers.items())
    lines.extend([f"Content-Length: {size}", "Connection: close"])
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


def _parse_head(head: bytes) -> tuple[int, list[tuple[str, str]]]:
    status_line, *lines = head.decode("latin-1").split("\r\n")
    status = response_status(status_line)
    headers: list[tuple[str, str]] = []
    for line in lines:
        name, separator, value = line.partition(":")
        if separator:
            headers.append((name.strip(), value.strip()))
    return status, headers


class AsyncHttpsTransport:
    """`asyncio` sobre TLS hacia el endpoint del sandbox, una conexión por
    petición."""

    def __init__(
        self,
        host: str,
        *,
        port: int = ENDPOINT_TLS_PORT,
        context: ssl.SSLContext | None = None,
        secure: bool = True,
    ) -> None:
        self._host = host
        self._port = port
        self._context = context or ssl.create_default_context() if secure else None

    async def request(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> AsyncDeploymentResponse:
        payload = body or b""
        writer: asyncio.StreamWriter | None = None
        try:
            async with asyncio.timeout(timeout):
                reader, writer = await asyncio.open_connection(
                    self._host,
                    self._port,
                    ssl=self._context,
                    server_hostname=self._host if self._context else None,
                    limit=PROXY_HEAD_MAX_BYTES,
                )
                writer.write(_request_head(method, path, self._host, headers, len(payload)))
                writer.write(payload)
                await writer.drain()
                head = await reader.readuntil(HEAD_TERMINATOR)
            status, response_headers = _parse_head(head.removesuffix(HEAD_TERMINATOR))
        except (
            OSError,
            TimeoutError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            MalformedFramingError,
        ):
            if writer is not None:
                writer.close()
            raise SandboxException("no se pudo contactar con el agente desplegado") from None
        return AsyncHttpsResponse(status, response_headers, method, reader, writer)


def default_async_transport(host_access: HostAccess) -> AsyncDeploymentTransport:
    return AsyncHttpsTransport(host_access.host)


class AsyncAgentDeploymentStream:
    """Iterable asíncrono de `AgentEvent`: `async with deployment.stream(...)
    as events: async for event in events`. La petición se hace al entrar. El
    último evento es `Done` o `AgentFailed`; nunca lanza por un fallo del
    agente (`result()` sí)."""

    def __init__(self, open_response: Callable[[], Awaitable[AsyncDeploymentResponse]]) -> None:
        self._open = open_response
        self._response: AsyncDeploymentResponse | None = None
        self._chunks: AsyncIterator[bytes] | None = None
        self._parser = SseParser()
        self._decoder = EventDecoder()
        self._builder = ResultBuilder()
        self._queue: list[AgentEvent] = []
        self._finished = False

    @property
    def session_id(self) -> str | None:
        return self._decoder.session_id

    @property
    def dropped_lines(self) -> int:
        return self._decoder.state.ignored_lines

    async def __aenter__(self) -> Self:
        await self._ensure_open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> AgentEvent:
        await self._ensure_open()
        while not self._queue:
            if self._finished:
                raise StopAsyncIteration
            await self._pump()
        return self._queue.pop(0)

    async def _ensure_open(self) -> None:
        if self._response is None:
            self._response = await self._open()
            self._chunks = self._response.chunks()

    async def _pump(self) -> None:
        assert self._chunks is not None
        try:
            chunk = await anext(self._chunks)
        except StopAsyncIteration:
            self._finished = True
            await self.close()
            return
        for message in self._parser.feed(chunk):
            for event in self._decoder.feed(message):
                self._builder.add(event)
                self._queue.append(event)
                if self._builder.final is not None:
                    self._finished = True
                    await self.close()

    async def result(self) -> AgentResult:
        """Consume lo que falte y devuelve el `AgentResult`; si el agente
        falló lanza `AgentException`."""
        async for _ in self:
            pass
        return self._builder.result(dropped_lines=self.dropped_lines)

    async def close(self) -> None:
        if self._response is not None:
            await self._response.close()


class AsyncAgentDeployment:
    """El espejo asíncrono de `AgentDeployment`: las mismas operaciones como
    corrutinas (`invoke`, `status`, `stop`, `connect`), `stream()` como
    contexto asíncrono y `url`/`headers` como propiedades.

    Coste y activación
    -------------------
    Activa: crear el cliente (`connect()` o el valor de `deploy()`) no llama
        a nadie salvo a `GetMicrovm` y `CreateMicrovmAuthToken` para el token
        del proxy; cada `invoke()`, `stream()` o `status()` es una petición
        HTTP a tu sandbox.
    Recursos y llamadas AWS: ninguno nuevo; el sandbox ya lo creó `deploy()`.
        Un sandbox suspendido se reanuda con la primera petición (factura
        cómputo, como cualquier reanudación) y el modelo lo paga la pasarela
        del sandbox, no este cliente.
    Coste aproximado: el del sandbox mientras vive, más el del modelo (ver
        `AsyncAgentProject.deploy()`).
    IAM: `GetMicrovm` y `CreateMicrovmAuthToken`; nada más.
    Cómo apagarla: `await stop()` termina el sandbox y con él el despliegue.
    Ejemplo:
        deployment = await AsyncAgentDeployment.connect(sandbox_id)
        (await deployment.invoke("Resume el README.")).text
        await deployment.stop()
    """

    def __init__(
        self,
        sandbox: Any,
        *,
        port: int,
        host_access: HostAccess,
        project: str | None = None,
        raw: bool = False,
        request_timeout: float = DEFAULT_DEPLOYMENT_REQUEST_TIMEOUT_SECONDS,
        transport_factory: Any = default_async_transport,
    ) -> None:
        self._sandbox = sandbox
        self._port = port
        self._host_access = host_access
        self._project = project
        self._raw = raw
        self._request_timeout = request_timeout
        self._transport_factory = transport_factory

    @classmethod
    async def connect(cls, sandbox_id: str, **sandbox_options: Any) -> Self:
        """Reconstruye el cliente de un despliegue desde las etiquetas de su
        sandbox, en cualquier proceso. `sandbox_options` son los de
        `AsyncSandbox.connect` (`region`, `session`...)."""
        from rayito.sandbox_async import AsyncSandbox

        sandbox = await AsyncSandbox.connect(sandbox_id, **sandbox_options)
        labels = parse_labels(sandbox.metadata)
        host_access = await sandbox.get_host(labels.port)
        return cls(
            sandbox,
            port=labels.port,
            host_access=host_access,
            project=labels.project,
            raw=labels.raw,
        )

    @property
    def sandbox_id(self) -> str:
        return str(self._sandbox.sandbox_id)

    @property
    def url(self) -> str:
        return str(self._host_access.url)

    @property
    def headers(self) -> dict[str, str]:
        return dict(self._host_access.headers)

    @property
    def port(self) -> int:
        return self._port

    async def invoke(
        self,
        prompt: str,
        session_id: str | None = None,
        model: str | None = None,
        reasoning: bool = False,
    ) -> AgentResult:
        """Una invocación sin streaming: el `AgentResult`, o `AgentException`
        si el agente falló (`busy` con el cupo lleno)."""
        require_invocable(raw=self._raw)
        body = invocation_body(
            require_prompt(prompt), session_id=session_id, model=model, reasoning=reasoning
        )
        response = await self._exchange("POST", AGENT_SERVICE_INVOKE_PATH, JSON_CONTENT_TYPE, body)
        try:
            return result_from_body(await response.read())
        finally:
            await response.close()

    def stream(
        self,
        prompt: str,
        session_id: str | None = None,
        model: str | None = None,
        reasoning: bool = False,
    ) -> AsyncAgentDeploymentStream:
        """Como `invoke()`, pero con los eventos a medida que llegan:
        `async with deployment.stream("hola") as events: async for event in
        events`."""
        require_invocable(raw=self._raw)
        body = invocation_body(
            require_prompt(prompt), session_id=session_id, model=model, reasoning=reasoning
        )
        return AsyncAgentDeploymentStream(
            lambda: self._exchange(
                "POST", AGENT_SERVICE_INVOKE_PATH, EVENT_STREAM_CONTENT_TYPE, body
            )
        )

    async def status(self) -> DeploymentStatus:
        """`GET /ping`: `healthy`, `busy` o `unavailable`."""
        try:
            response = await self._send(
                "GET", AGENT_SERVICE_HEALTH_PATH, JSON_CONTENT_TYPE, None, PING_TIMEOUT_SECONDS
            )
        except SandboxException:
            return DeploymentStatus("unavailable", False, False, self._port, self._project)
        try:
            return status_from_ping(
                response.status, await response.read(), port=self._port, project=self._project
            )
        finally:
            await response.close()

    async def stop(self) -> None:
        """Termina el sandbox del despliegue. Idempotente."""
        await self._sandbox.kill()

    async def _exchange(
        self, method: str, path: str, accept: str, body: bytes | None
    ) -> AsyncDeploymentResponse:
        response = await self._send(method, path, accept, body, self._request_timeout)
        if response.status == HTTP_OK:
            return response
        status = response.status
        await response.close()
        raise http_error(status)

    async def _send(
        self, method: str, path: str, accept: str, body: bytes | None, timeout: float
    ) -> AsyncDeploymentResponse:
        """Una petición con las cabeceras vigentes; un `403` del proxy
        reacuña el token y reintenta una vez, y el segundo es
        `AuthenticationException`."""
        for attempt in range(PROXY_REMINT_ATTEMPTS + 1):
            headers = invocation_headers(self._host_access.headers, accept=accept)
            transport: AsyncDeploymentTransport = self._transport_factory(self._host_access)
            response = await transport.request(method, path, headers, body, timeout)
            if response.status != HTTP_FORBIDDEN:
                return response
            await response.close()
            if attempt == PROXY_REMINT_ATTEMPTS:
                break
            await self._sandbox._refresher.refresh_all()
        raise AuthenticationException(
            "el proxy de AWS rechazó el token del despliegue", proxy_rejected=True
        )
