"""`AgentDeployment`: el cliente de un agente servido desde un MicroVM
(`AgentProject.deploy()`, ADR-028, D6). Habla `rayito.agent-service/1` por el
proxy de AWS con `http.client` y el JWE vigente del sandbox; una respuesta
`403` del proxy reacuña el token y reintenta una vez.

Construirlo no llama a nadie; el primer `invoke()`, `stream()` o `status()`
hace la primera petición. `connect()` reconstruye el cliente desde las
etiquetas del sandbox en cualquier proceso.
"""

from __future__ import annotations

import http.client
import ssl
from collections.abc import Iterator, Mapping
from typing import TYPE_CHECKING, Any, Final, Protocol, Self

from rayito._agent._deployment_base import (
    EVENT_STREAM_CONTENT_TYPE,
    HTTP_BAD_REQUEST,
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
from rayito._limits import (
    AGENT_RUN_SANDBOX_MARGIN_SECONDS,
    AGENT_SERVICE_HEALTH_PATH,
    AGENT_SERVICE_INVOKE_PATH,
    DEFAULT_AGENT_TIMEOUT_SECONDS,
    ENDPOINT_TLS_PORT,
)
from rayito.exceptions import AuthenticationException, SandboxException

if TYPE_CHECKING:
    from rayito._models import HostAccess

#: Plazo de una petición: el de una ejecución del agente más el margen del
#: sandbox (`AgentLimits.timeout_seconds` manda dentro del host).
DEFAULT_DEPLOYMENT_REQUEST_TIMEOUT_SECONDS: Final = (
    DEFAULT_AGENT_TIMEOUT_SECONDS + AGENT_RUN_SANDBOX_MARGIN_SECONDS
)
#: Plazo de `GET /ping`.
PING_TIMEOUT_SECONDS: Final = 10.0
#: Fragmento que se lee de un stream SSE.
STREAM_CHUNK_BYTES: Final = 8192
#: Reintentos tras reacuñar el token del proxy (el 403 se reintenta una vez).
PROXY_REMINT_ATTEMPTS: Final = 1


class DeploymentResponse(Protocol):
    """Lo que el cliente necesita de una respuesta HTTP."""

    @property
    def status(self) -> int: ...

    def read(self) -> bytes: ...

    def chunks(self) -> Iterator[bytes]: ...

    def close(self) -> None: ...


class DeploymentTransport(Protocol):
    """El transporte HTTP del cliente: un adaptador sobre `http.client` en
    producción y un doble en los tests."""

    def request(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> DeploymentResponse: ...


class HttpsResponse:
    """El cuerpo de una respuesta de `HttpsTransport`. Un cuerpo cortado o
    mal delimitado (`OSError`, `http.client.HTTPException`) es un
    `SandboxException`, como en `AsyncHttpsResponse`. `read1` no avisa de un
    cuerpo con `Content-Length` que acaba antes de tiempo, así que `chunks()`
    comprueba al final que no falten bytes."""

    def __init__(self, response: http.client.HTTPResponse, connection: http.client.HTTPConnection):
        self._response = response
        self._connection = connection

    @property
    def status(self) -> int:
        return self._response.status

    def read(self) -> bytes:
        try:
            return self._response.read()
        except (OSError, http.client.HTTPException):
            raise SandboxException("el agente desplegado cortó la respuesta") from None

    def chunks(self) -> Iterator[bytes]:
        try:
            while chunk := self._response.read1(STREAM_CHUNK_BYTES):
                yield chunk
        except (OSError, http.client.HTTPException):
            raise SandboxException("el agente desplegado cortó la respuesta") from None
        if self._response.length:
            raise SandboxException("el agente desplegado cortó la respuesta")

    def close(self) -> None:
        self._connection.close()


class HttpsTransport:
    """`http.client` sobre TLS hacia el endpoint del sandbox. Una conexión
    por petición (`Connection: close`): el proxy de AWS limita las
    conexiones simultáneas por MicroVM."""

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
        self._context = context or ssl.create_default_context()
        self._secure = secure

    def request(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> DeploymentResponse:
        connection: http.client.HTTPConnection = (
            http.client.HTTPSConnection(
                self._host, self._port, timeout=timeout, context=self._context
            )
            if self._secure
            else http.client.HTTPConnection(self._host, self._port, timeout=timeout)
        )
        try:
            connection.request(method, path, body=body, headers={**headers, "Connection": "close"})
            return HttpsResponse(connection.getresponse(), connection)
        except (OSError, http.client.HTTPException):
            connection.close()
            raise SandboxException("no se pudo contactar con el agente desplegado") from None


def default_transport(host_access: HostAccess) -> DeploymentTransport:
    return HttpsTransport(host_access.host)


class AgentDeploymentStream:
    """Iterable de `AgentEvent` sobre la respuesta SSE de una invocación. El
    último evento es `Done` o `AgentFailed`; nunca lanza por un fallo del
    agente (`result()` sí)."""

    def __init__(self, response: DeploymentResponse) -> None:
        self._response = response
        self._chunks = response.chunks()
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

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __iter__(self) -> Self:
        return self

    def __next__(self) -> AgentEvent:
        while not self._queue:
            if self._finished:
                raise StopIteration
            self._pump()
        return self._queue.pop(0)

    def _pump(self) -> None:
        try:
            chunk = next(self._chunks)
        except StopIteration:
            self._finished = True
            self.close()
            return
        for message in self._parser.feed(chunk):
            for event in self._decoder.feed(message):
                self._builder.add(event)
                self._queue.append(event)
                if self._builder.final is not None:
                    self._finished = True
                    self.close()

    def result(self) -> AgentResult:
        """Consume lo que falte y devuelve el `AgentResult`; si el agente
        falló lanza `AgentException`."""
        for _ in self:
            pass
        return self._builder.result(dropped_lines=self.dropped_lines)

    def close(self) -> None:
        self._response.close()


class AgentDeployment:
    """Un agente servido desde un MicroVM (`AgentProject.deploy()`).

    `url` y `headers` son lo que necesita cualquier cliente HTTP: las
    cabeceras llevan el JWE del proxy de AWS y se leen frescas en cada
    acceso (el token vive 60 minutos y el SDK lo renueva). `invoke()` y
    `stream()` hablan `rayito.agent-service/1`; `status()` lee `GET /ping`;
    `stop()` termina el sandbox. Un despliegue vive como mucho el `timeout`
    del sandbox (8 horas como máximo): para más tiempo, vuelve a llamar a
    `deploy()` y reconecta con `AgentDeployment.connect(sandbox_id)`.

    Coste y activación
    -------------------
    Activa: crear el cliente (`connect()` o el valor de `deploy()`) no llama
        a nadie; cada `invoke()`, `stream()` o `status()` es una petición
        HTTP a tu sandbox.
    Recursos y llamadas AWS: ninguno nuevo; el sandbox ya lo creó `deploy()`.
        Un sandbox suspendido se reanuda con la primera petición (factura
        cómputo, como cualquier reanudación) y el modelo lo paga la pasarela
        del sandbox, no este cliente.
    Coste aproximado: el del sandbox mientras vive, más el del modelo (ver
        `AgentProject.deploy()`).
    IAM: `GetMicrovm` y `CreateMicrovmAuthToken` para `connect()` y para el
        token del proxy; nada más.
    Cómo apagarla: `stop()` termina el sandbox y con él el despliegue;
        `pause()` deja de facturar cómputo sin perder el servicio.
    Ejemplo:
        deployment = AgentDeployment.connect(sandbox_id)
        deployment.invoke("Resume el README.").text
        deployment.stop()
    """

    def __init__(
        self,
        sandbox: Any,
        *,
        port: int,
        project: str | None = None,
        raw: bool = False,
        request_timeout: float = DEFAULT_DEPLOYMENT_REQUEST_TIMEOUT_SECONDS,
        transport_factory: Any = default_transport,
    ) -> None:
        self._sandbox = sandbox
        self._port = port
        self._project = project
        self._raw = raw
        self._request_timeout = request_timeout
        self._transport_factory = transport_factory

    @classmethod
    def connect(cls, sandbox_id: str, **sandbox_options: Any) -> Self:
        """Reconstruye el cliente de un despliegue desde las etiquetas de su
        sandbox, en cualquier proceso. `sandbox_options` son los de
        `Sandbox.connect` (`region`, `session`...)."""
        from rayito.sandbox_sync import Sandbox

        sandbox = Sandbox.connect(sandbox_id, **sandbox_options)
        labels = parse_labels(sandbox.metadata)
        return cls(sandbox, port=labels.port, project=labels.project, raw=labels.raw)

    @property
    def sandbox_id(self) -> str:
        return str(self._sandbox.sandbox_id)

    @property
    def access_token(self) -> str:
        """El access token del sandbox del despliegue. Con el que pasaste a
        `deploy(access_token=...)` o, si no pasaste ninguno, el que se generó:
        guárdalo (nunca lo imprimas ni lo versiones) para reconectarte desde
        otro proceso con `AgentDeployment.connect(sandbox_id, access_token=...)`
        o con `rayito agent invoke --token-file`."""
        return str(self._sandbox.access_token)

    @property
    def url(self) -> str:
        return str(self._sandbox.get_host(self._port).url)

    @property
    def headers(self) -> dict[str, str]:
        return dict(self._sandbox.get_host(self._port).headers)

    @property
    def port(self) -> int:
        return self._port

    def invoke(
        self,
        prompt: str | None = None,
        session_id: str | None = None,
        model: str | None = None,
        reasoning: bool = False,
        *,
        input: Mapping[str, Any] | None = None,
    ) -> AgentResult:
        """Una invocación sin streaming: el `AgentResult`, o `AgentException`
        si el agente falló (`busy` con el cupo lleno). Con `input` (un objeto
        JSON, en lugar de `prompt`) se invoca un grafo con esquema de entrada
        propio y `result.output` trae su salida."""
        require_invocable(raw=self._raw)
        body = invocation_body(
            prompt, session_id=session_id, model=model, reasoning=reasoning, input=input
        )
        response = self._exchange(
            "POST",
            AGENT_SERVICE_INVOKE_PATH,
            JSON_CONTENT_TYPE,
            body,
            sent_input=input is not None,
        )
        try:
            return result_from_body(response.read())
        finally:
            response.close()

    def stream(
        self,
        prompt: str | None = None,
        session_id: str | None = None,
        model: str | None = None,
        reasoning: bool = False,
        *,
        input: Mapping[str, Any] | None = None,
    ) -> AgentDeploymentStream:
        """Como `invoke()`, pero con los eventos a medida que llegan:
        `with deployment.stream("hola") as events: for event in events`."""
        require_invocable(raw=self._raw)
        body = invocation_body(
            prompt, session_id=session_id, model=model, reasoning=reasoning, input=input
        )
        response = self._exchange(
            "POST",
            AGENT_SERVICE_INVOKE_PATH,
            EVENT_STREAM_CONTENT_TYPE,
            body,
            sent_input=input is not None,
        )
        return AgentDeploymentStream(response)

    def status(self) -> DeploymentStatus:
        """`GET /ping`: `healthy`, `busy` o `unavailable`."""
        try:
            response = self._send(
                "GET", AGENT_SERVICE_HEALTH_PATH, JSON_CONTENT_TYPE, None, PING_TIMEOUT_SECONDS
            )
        except SandboxException:
            return DeploymentStatus("unavailable", False, False, self._port, self._project)
        try:
            return status_from_ping(
                response.status, response.read(), port=self._port, project=self._project
            )
        finally:
            response.close()

    def pause(self, *, wait: bool = True) -> bool:
        """Suspende el sandbox del despliegue (`Sandbox.pause()`): mientras
        duerme no factura cómputo y el servicio conserva su estado. `False`
        si ya estaba suspendido. La siguiente petición (o `resume()`) lo
        reanuda; con `idle` (activo por defecto) también se suspende solo."""
        return bool(self._sandbox.pause(wait=wait))

    def resume(self, *, wait: bool = True) -> None:
        """Reanuda el sandbox suspendido (`Sandbox.resume()`) y, con `wait`,
        espera a que esté listo. No hace falta antes de `invoke()`: una
        petición a un sandbox suspendido ya lo reanuda."""
        self._sandbox.resume(wait=wait)

    def set_timeout(self, timeout: int) -> None:
        """Fija la vida restante del sandbox del despliegue
        (`Sandbox.set_timeout`, segundos enteros): `rayd` lo termina solo al
        vencer. Una flota lo usa para que un VM sobrante desaparezca sin
        servidor propio."""
        self._sandbox.set_timeout(timeout)

    def refresh_gateways(self) -> None:
        """Vuelve a mandar las pasarelas del sandbox (`sbx.gateways.refresh()`):
        releen de Secrets Manager el valor actual de cada secreto. Una flota lo
        usa tras renovar una clave (`renew_gateway_keys`)."""
        self._sandbox.gateways.refresh()

    def stop(self) -> None:
        """Termina el sandbox del despliegue. Idempotente."""
        self._sandbox.kill()

    def _exchange(
        self,
        method: str,
        path: str,
        accept: str,
        body: bytes | None,
        *,
        sent_input: bool = False,
    ) -> DeploymentResponse:
        response = self._send(method, path, accept, body, self._request_timeout)
        if response.status == HTTP_OK:
            return response
        status = response.status
        body = _error_body(response) if status == HTTP_BAD_REQUEST and sent_input else None
        response.close()
        raise http_error(status, sent_input=sent_input, body=body)

    def _send(
        self, method: str, path: str, accept: str, body: bytes | None, timeout: float
    ) -> DeploymentResponse:
        """Una petición con las cabeceras vigentes; un `403` del proxy
        reacuña el token y reintenta una vez, y el segundo es
        `AuthenticationException`."""
        for attempt in range(PROXY_REMINT_ATTEMPTS + 1):
            host_access = self._sandbox.get_host(self._port)
            headers = invocation_headers(host_access.headers, accept=accept)
            transport: DeploymentTransport = self._transport_factory(host_access)
            response = transport.request(method, path, headers, body, timeout)
            if response.status != HTTP_FORBIDDEN:
                return response
            response.close()
            if attempt == PROXY_REMINT_ATTEMPTS:
                break
            self._sandbox._refresher.refresh_all()
        raise AuthenticationException(
            "el proxy de AWS rechazó el token del despliegue", proxy_rejected=True
        )


def _error_body(response: DeploymentResponse) -> bytes | None:
    """El cuerpo de un `400` (el `{"error": ...}` del host), o `None` si no
    se pudo leer: sólo explica el error, nunca lo sustituye."""
    try:
        return response.read()
    except Exception:
        return None
