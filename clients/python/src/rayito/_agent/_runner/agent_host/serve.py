"""Modo `serve` del host (ADR-028, D3 y D4): sirve `rayito.agent-service/1`,
el contrato HTTP de AgentCore, con `http.server.ThreadingHTTPServer`.

- `GET /ping` -> `200 {"status": "Healthy" | "HealthyBusy", "time_of_last_update"}`.
- `POST /invocations` con `{"prompt", "session_id"?, "model"?, "reasoning"?}`.
  Con `Accept: text/event-stream`, un mensaje SSE por evento v1 hasta `done`
  o `agent_failed`; si no, `{"v", "text", "session_id", "usage",
  "usage_reported", "steps", "failed"}`. `400` cuerpo inválido, `413` demasiado grande, `429` con
  `Retry-After` por encima de `max_concurrency` (con `queue_max` > 0 la
  petición espera su turno en una cola FIFO acotada hasta
  `queue_timeout_seconds` y sólo entonces recibe el `429`), `503` antes de
  estar listo o mientras la app se relanza (con `Retry-After`) y `401` si
  hay token y no coincide.

El id de sesión de AgentCore (la cabecera) lo elige quien llama, con cualquier
forma; los drivers sólo continúan sesiones suyas (`rda_` y 32 hex, o las que
guarda OpenCode). `SessionBridge` traduce uno al otro dentro de la microVM: la
primera llamada de una sesión de AgentCore arranca una sesión nueva del driver
y las siguientes la continúan, y la respuesta lleva siempre el id de la
cabecera.

El host no loguea contenido: sólo método, ruta sin consulta, estado, bytes y
duración. Nunca el prompt, una cabecera ni el token.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hmac
import json
import logging
import os
import queue
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Final

from .limits import Limits, LimitTracker
from .protocol import (
    PROTOCOL_VERSION,
    AgentFailure,
    Driver,
    Event,
    LimitExceeded,
    Request,
    RunState,
    build_event,
    classify_by_module,
    encode_line,
    parse_request,
    run_to_terminal,
)

SERVICE_SCHEMA: Final = "rayito.agent-service/1"
HEALTH_PATH: Final = "/ping"
INVOKE_PATH: Final = "/invocations"
#: Cabecera de AgentCore con el id de sesión (`AGENT_SERVICE_SESSION_HEADER`).
SESSION_HEADER: Final = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"
#: Tope del cuerpo de una petición (`AGENT_SERVICE_MAX_REQUEST_BYTES`).
MAX_REQUEST_BYTES: Final = 1_048_576
MAX_CONCURRENCY: Final = 1
STATUS_HEALTHY: Final = "Healthy"
STATUS_BUSY: Final = "HealthyBusy"
STATUS_UNHEALTHY: Final = "Unhealthy"
#: Los estados de `availability()` de un driver supervisado.
AVAILABILITY_RESTARTING: Final = "restarting"
AVAILABILITY_FAILED: Final = "failed"
#: El `error` del `503` según por qué no se sirve.
NOT_READY_CODE: Final = "not_ready"
UNAVAILABLE_CODES: Final = {
    AVAILABILITY_RESTARTING: "app_restarting",
    AVAILABILITY_FAILED: "app_crashloop",
}
#: Resultados de `Service.acquire()`.
ACQUIRED: Final = "acquired"
QUEUE_FULL: Final = "busy"
QUEUE_TIMEOUT: Final = "queue_timeout"
#: Cola de espera: sin cola por defecto (0.14 contestaba `429`), con el
#: tope y el plazo máximo que acepta el SDK (`AGENT_SERVICE_QUEUE_MAX_DEFAULT`,
#: `AGENT_SERVICE_QUEUE_MAX`, `AGENT_SERVICE_QUEUE_TIMEOUT_SECONDS_DEFAULT` y
#: `AGENT_SERVICE_QUEUE_TIMEOUT_SECONDS_MAX` de `limits.json`).
QUEUE_MAX_DEFAULT: Final = 0
QUEUE_MAX: Final = 256
QUEUE_TIMEOUT_SECONDS_DEFAULT: Final = 30.0
QUEUE_TIMEOUT_SECONDS_MAX: Final = 900.0
#: Segundos que `Retry-After` pide esperar con el agente ocupado o sin
#: calentar.
RETRY_AFTER_SECONDS: Final = 1
#: Segundos que una petición espera a que el host termine de calentar antes de
#: recibir el `503`: AgentCore enruta la primera llamada de una sesión nueva en
#: cuanto arranca la microVM, antes de que el agente esté caliente.
READY_WAIT_SECONDS_DEFAULT: Final = 25.0
#: Segundos que un manejador espera una lectura o una escritura del socket
#: antes de soltar la conexión: un cliente que anuncia un cuerpo y no lo manda
#: no retiene un hilo para siempre.
IO_TIMEOUT_SECONDS: Final = 60.0
TOKEN_ENV: Final = "RAYITO_AGENT_SERVICE_TOKEN"
#: Sufijo del marcador que deja un arranque fallido.
FAILED_SUFFIX: Final = ".failed"
EVENT_STREAM: Final = "text/event-stream"
LOGGER_NAME: Final = "rayito.agent.host"
#: Cada cuánto despierta el consumidor para comprobar el plazo.
POLL_SECONDS: Final = 0.25
#: Fragmento que se copia de la respuesta de una app `raw` al cliente.
RELAY_CHUNK_BYTES: Final = 65536
MILLISECONDS_PER_SECOND: Final = 1000
#: Cabeceras que un proxy no reenvía (RFC 9110, sección 7.6.1).
HOP_BY_HOP: Final = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "content-length",
    }
)

logger = logging.getLogger(LOGGER_NAME)


@dataclass
class ServiceConfig:
    """Lo que el servicio necesita; todo lo carga `__main__.py` de la
    configuración y el entorno."""

    limits: Limits = field(default_factory=Limits)
    max_concurrency: int = MAX_CONCURRENCY
    queue_max: int = QUEUE_MAX_DEFAULT
    queue_timeout_seconds: float = QUEUE_TIMEOUT_SECONDS_DEFAULT
    max_request_bytes: int = MAX_REQUEST_BYTES
    token: str | None = None
    session_header: str = SESSION_HEADER
    ready_marker: str | None = None
    io_timeout_seconds: float = IO_TIMEOUT_SECONDS
    ready_wait_seconds: float = READY_WAIT_SECONDS_DEFAULT
    aws_credentials: Callable[[], Mapping[str, str]] | None = None


#: El id de sesión propio de los drivers (`SESSION_ID_PATTERN` de `drivers/_common.py`):
#: una cabecera que ya lo tiene es de una sesión de esta máquina y se usa tal cual.
DRIVER_SESSION_PATTERN: Final = re.compile(r"rda_[0-9a-f]{32}")
#: Las claves de un evento de una ejecución que llevan el id de sesión.
SESSION_EVENT_TYPES: Final = ("session", "done", "agent_failed")


class SessionBridge:
    """Traduce el id de sesión de la cabecera de AgentCore al del driver y
    de vuelta. Un id de cabecera con la forma de los drivers se usa tal cual;
    cualquier otro empieza una sesión del driver la primera vez (`None`) y la
    continúa después, con el id del driver que aprendió del evento `session`."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._driver_ids: dict[str, str] = {}

    def to_driver(self, request: Request, header_session: str | None) -> Request:
        if header_session is None or DRIVER_SESSION_PATTERN.fullmatch(header_session):
            return request
        with self._lock:
            driver_session = self._driver_ids.get(header_session)
        return dataclasses.replace(request, session_id=driver_session)

    def learn(self, header_session: str, driver_session: str) -> None:
        with self._lock:
            self._driver_ids[header_session] = driver_session


def write_failed_marker(config: ServiceConfig, code: str, diagnostics: str | None = None) -> None:
    """Deja el motivo de un arranque fallido junto al marcador de listo, para
    que `deploy()` falle enseguida y no al agotar su plazo. La primera línea
    es el código (el `detail_code` del fallo o el nombre de una clase de
    excepción) y, si hay, el resto es el diagnóstico ya saneado
    (`diagnostics.sanitize_diagnostics`)."""
    Service._write_marker(
        None if config.ready_marker is None else config.ready_marker + FAILED_SUFFIX,
        code if not diagnostics else f"{code}\n{diagnostics}",
    )


class Service:
    """El estado compartido de los manejadores: el driver, el cupo de
    ejecuciones y el estado de `/ping`."""

    def __init__(
        self, driver: Driver, config: ServiceConfig, clock: Callable[[], float] = time.time
    ) -> None:
        self.driver = driver
        self.config = config
        self._clock = clock
        self._turns = threading.Condition()
        self._active = 0
        self._waiting: deque[object] = deque()
        self._ready = False
        self._warmed = threading.Event()
        self._last_update = int(clock())
        self.sessions = SessionBridge()
        listen = getattr(driver, "add_failure_listener", None)
        if listen is not None:
            listen(self.mark_failed)

    @property
    def ready(self) -> bool:
        return self._ready

    def wait_ready(self, timeout_seconds: float) -> bool:
        """Espera hasta `timeout_seconds` a que el host esté listo (o falle el
        calentamiento) y dice si lo está."""
        self._warmed.wait(timeout_seconds)
        return self._ready

    def mark_ready(self) -> None:
        self._ready = True
        self._warmed.set()
        self._write_marker(self.config.ready_marker, "ready")

    def mark_failed(self, code: str, diagnostics: str | None = None) -> None:
        write_failed_marker(self.config, code, diagnostics)
        self._warmed.set()

    @staticmethod
    def _write_marker(path: str | None, content: str) -> None:
        if path is not None:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content + "\n")

    def availability(self) -> str | None:
        """`restarting` o `failed` si el driver supervisa una app que murió;
        `None` si sirve."""
        report = getattr(self.driver, "availability", None)
        state = None if report is None else report()
        return state if state in (AVAILABILITY_RESTARTING, AVAILABILITY_FAILED) else None

    def ping(self) -> tuple[int, dict[str, object]]:
        """El estado HTTP y el cuerpo de `/ping`: `HealthyBusy` con todas las
        ejecuciones ocupadas o con la app relanzándose, y `503` `Unhealthy`
        cuando agotó sus relanzamientos."""
        availability = self.availability()
        with self._turns:
            busy = self._active >= self.config.max_concurrency
            if availability == AVAILABILITY_FAILED:
                status, code = STATUS_UNHEALTHY, HTTPStatus.SERVICE_UNAVAILABLE
            elif busy or availability == AVAILABILITY_RESTARTING:
                status, code = STATUS_BUSY, HTTPStatus.OK
            else:
                status, code = STATUS_HEALTHY, HTTPStatus.OK
            return code, {"status": status, "time_of_last_update": self._last_update}

    def acquire(self) -> str:
        """Pide un cupo de ejecución: `acquired`; `busy` si no hay cupo y la
        cola está llena (o apagada); `queue_timeout` si esperó
        `queue_timeout_seconds` sin turno. La cola es FIFO: una petición
        nueva no adelanta a las que esperan."""
        with self._turns:
            if not self._waiting and self._active < self.config.max_concurrency:
                self._take()
                return ACQUIRED
            if len(self._waiting) >= self.config.queue_max:
                return QUEUE_FULL
            return self._wait_for_turn()

    def _wait_for_turn(self) -> str:
        ticket = object()
        self._waiting.append(ticket)
        deadline = time.monotonic() + self.config.queue_timeout_seconds
        try:
            while True:
                if self._waiting[0] is ticket and self._active < self.config.max_concurrency:
                    self._take()
                    return ACQUIRED
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return QUEUE_TIMEOUT
                self._turns.wait(remaining)
        finally:
            self._waiting.remove(ticket)
            self._turns.notify_all()

    def _take(self) -> None:
        self._active += 1
        if self._active >= self.config.max_concurrency:
            self._last_update = int(self._clock())

    def release(self) -> None:
        with self._turns:
            was_busy = self._active >= self.config.max_concurrency
            self._active -= 1
            if was_busy:
                self._last_update = int(self._clock())
            self._turns.notify_all()

    def with_credentials(self, request: Request) -> Request:
        """La petición con las credenciales del rol de AgentCore, si el
        adaptador del proyecto sólo las lee del entorno; un fallo al leerlas
        es un `AgentFailure` y termina la invocación."""
        read = self.config.aws_credentials
        if read is None:
            return request
        return dataclasses.replace(request, aws_env=read())

    def authorized(self, header: str | None) -> bool:
        """Sin token configurado todo pasa (en un MicroVM la autenticación es
        el JWE del proxy de AWS); con él, `Authorization: Bearer <token>`
        comparado en tiempo constante."""
        token = self.config.token
        if token is None:
            return True
        prefix = "Bearer "
        if header is None or not header.startswith(prefix):
            return False
        return hmac.compare_digest(header[len(prefix) :].encode("utf-8"), token.encode("utf-8"))


class Invocation:
    """Una ejecución del driver en su hilo, con sus eventos en una cola. Los
    topes se aplican al entregar cada evento (`LimitTracker`); el plazo lo
    vigila `events()`."""

    def __init__(
        self, service: Service, request: Request, header_session: str | None = None
    ) -> None:
        self._service = service
        self._request = service.sessions.to_driver(request, header_session)
        self._header_session = (
            None
            if header_session is not None and DRIVER_SESSION_PATTERN.fullmatch(header_session)
            else header_session
        )
        self._queue: queue.Queue[Event | None] = queue.Queue()
        self._closed = threading.Event()
        self.tracker = LimitTracker(service.config.limits)
        self.session_id: str | None = None
        self._thread = threading.Thread(target=self._work, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _deliver(self, event: Event) -> None:
        if self._closed.is_set():
            raise LimitExceeded("aborted")
        event = self._presented(event)
        kind = event.get("type")
        if kind in ("done", "agent_failed"):
            self._queue.put(event)
            return
        reason = self.tracker.observe(event)
        if reason is not None:
            self._fail(reason)
            raise LimitExceeded(reason)
        if kind == "session" and isinstance(event.get("session_id"), str):
            self.session_id = event["session_id"]
        self._queue.put(event)

    def _presented(self, event: Event) -> Event:
        """El evento con el id de sesión de la cabecera de AgentCore, si la
        sesión viene de ahí: el del driver es privado de la microVM."""
        header = self._header_session
        if header is None or event.get("type") not in SESSION_EVENT_TYPES:
            return event
        driver_session = event.get("session_id")
        if event.get("type") == "session" and isinstance(driver_session, str):
            self._service.sessions.learn(header, driver_session)
        return {**event, "session_id": header}

    def _fail(self, reason: str) -> None:
        self._queue.put(
            build_event("agent_failed", reason=reason, detail_code=None, session_id=self.session_id)
        )
        self._closed.set()

    def _work(self) -> None:
        emitter = _QueueEmitter(self._deliver)

        def body(state: RunState) -> None:
            request = self._service.with_credentials(self._request)
            self._service.driver.run(request, self._deliver, state)

        try:
            run_to_terminal(emitter, body, lambda exc: classify_by_module(exc, ()))
        except LimitExceeded:
            pass
        finally:
            self._queue.put(None)

    def abort(self, reason: str) -> None:
        """Corta la ejecución: el consumidor ya no recibe más eventos y el
        driver mata lo que lanzó."""
        if self._closed.is_set():
            return
        self._fail(reason)
        with contextlib.suppress(Exception):
            self._service.driver.cancel()

    def events(self) -> Iterator[Event]:
        """Los eventos hasta el terminal (`done` o `agent_failed`); si se
        agota `timeout_seconds`, el terminal es `agent_failed` con
        `timeout`."""
        deadline = time.monotonic() + self._service.config.limits.timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.abort("timeout")
                remaining = POLL_SECONDS
            try:
                event = self._queue.get(timeout=min(remaining, POLL_SECONDS))
            except queue.Empty:
                continue
            if event is None:
                return
            yield event
            if event.get("type") in ("done", "agent_failed"):
                return

    def close(self) -> None:
        """Libera la ejecución: si el hilo sigue vivo (cliente que se fue)
        la cancela."""
        if self._thread.is_alive():
            self.abort("aborted")


class _QueueEmitter:
    """La interfaz `emit(kind, **fields)` que usa `run_to_terminal`, hacia
    el entregador de la invocación."""

    def __init__(self, deliver: Callable[[Event], None]) -> None:
        self._deliver = deliver

    def emit(self, kind: str, **fields: object) -> None:
        self._deliver(build_event(kind, **fields))


def format_sse(event: Mapping[str, object]) -> bytes:
    return f"event: {event['type']}\ndata: {encode_line(event)}\n\n".encode()


def result_body(invocation: Invocation, terminal: Event) -> dict[str, Any]:
    """El cuerpo JSON de una invocación sin streaming."""
    failed = terminal.get("type") == "agent_failed"
    session_id = terminal.get("session_id") or invocation.session_id
    return {
        "v": PROTOCOL_VERSION,
        "text": "" if failed else invocation.tracker.text,
        "session_id": session_id,
        "usage": dict(invocation.tracker.usage),
        "usage_reported": invocation.tracker.usage_reported,
        "steps": invocation.tracker.steps,
        "failed": (
            {"reason": terminal.get("reason"), "detail_code": terminal.get("detail_code")}
            if failed
            else None
        ),
    }


def parse_invocation(body: bytes, header_session: str | None) -> tuple[Request, str | None]:
    """El cuerpo de `POST /invocations` a `Request`; `AgentFailure` si no es
    válido. Sin `session_id` en el cuerpo, vale el de la cabecera; el segundo
    valor es el id de la cabecera cuando es de donde sale la sesión."""
    try:
        data = json.loads(body)
    except ValueError:
        raise AgentFailure("protocol_error", "invalid_request") from None
    if not isinstance(data, dict) or not isinstance(data.get("prompt"), str) or not data["prompt"]:
        raise AgentFailure("protocol_error", "invalid_request")
    unknown = set(data) - {"prompt", "session_id", "model", "reasoning"}
    if unknown:
        raise AgentFailure("protocol_error", "invalid_request")
    session_id = data.get("session_id") or header_session
    request = parse_request({**data, "v": PROTOCOL_VERSION, "session_id": session_id})
    return request, None if data.get("session_id") else header_session


def make_handler(service: Service) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "rayito-agent-host"
        sys_version = ""
        protocol_version = "HTTP/1.1"
        timeout = service.config.io_timeout_seconds

        def log_request(self, code: object = "-", size: object = "-") -> None:
            path = self.path.split("?", 1)[0]
            elapsed = int(
                (time.monotonic() - getattr(self, "_started", time.monotonic()))
                * MILLISECONDS_PER_SECOND
            )
            logger.info("%s %s -> %s %sB %dms", self.command, path, code, size, elapsed)

        def log_error(self, format: str, *args: object) -> None:
            logger.warning("http_error code=%s", args[0] if args else "-")

        def log_message(self, format: str, *args: object) -> None:
            return

        def _send_json(
            self, status: int, body: Mapping[str, object], extra: Mapping[str, str] | None = None
        ) -> None:
            payload = encode_line(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            for name, value in (extra or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(payload)

        def _error(self, status: int, code: str, extra: Mapping[str, str] | None = None) -> None:
            self.close_connection = True
            self._send_json(status, {"error": code}, {"Connection": "close", **(extra or {})})

        def _is_raw(self) -> bool:
            return bool(getattr(service.driver, "raw", False))

        def _forwards(self) -> bool:
            """El driver quiere las rutas que no son del host (`raw` y
            `json`)."""
            return self._is_raw() or bool(getattr(service.driver, "forwards", False))

        def do_GET(self) -> None:
            self._started = time.monotonic()
            if self.path.split("?", 1)[0] == HEALTH_PATH:
                status, body = service.ping()
                self._send_json(status, body)
            elif self._forwards():
                self._forward()
            else:
                self._error(HTTPStatus.NOT_FOUND, "not_found")

        def do_PUT(self) -> None:
            self._forward_other()

        do_DELETE = do_PATCH = do_OPTIONS = do_PUT

        def _forward_other(self) -> None:
            self._started = time.monotonic()
            if self._forwards():
                self._forward()
            else:
                self._error(HTTPStatus.NOT_FOUND, "not_found")

        def _forward(self) -> None:
            """Un servidor `raw` (MCP u otro protocolo): se reenvía tal cual,
            con la misma autenticación y el mismo tope de cuerpo."""
            if not service.authorized(self.headers.get("Authorization")):
                self._error(HTTPStatus.UNAUTHORIZED, "unauthorized", {"WWW-Authenticate": "Bearer"})
            elif not self._available():
                return
            else:
                body = self._read_body()
                if body is not None:
                    self._relay(body)

        def _available(self) -> bool:
            """`False` (tras contestar `503` con `Retry-After`) si el host no
            está listo o la app se relanza o cayó para siempre."""
            code = (
                NOT_READY_CODE
                if not service.wait_ready(service.config.ready_wait_seconds)
                else UNAVAILABLE_CODES.get(service.availability() or "")
            )
            if code is not None:
                self._error(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    code,
                    {"Retry-After": str(RETRY_AFTER_SECONDS)},
                )
            return code is None

        def _read_body(self) -> bytes | None:
            return self._read_limited_body(empty_ok=True)

        def _relay(self, body: bytes) -> None:
            skip = {"host", "content-length", "connection", "authorization"}
            headers = {k: v for k, v in self.headers.items() if k.lower() not in skip}
            try:
                response = service.driver.forward(self.command, self.path, headers, body or None)  # type: ignore[attr-defined]
            except AgentFailure:
                self._error(HTTPStatus.BAD_GATEWAY, "app_unreachable")
                return
            self.close_connection = True
            self.send_response(response.status)
            for name, value in response.getheaders():
                if name.lower() not in HOP_BY_HOP:
                    self.send_header(name, value)
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for chunk in iter(lambda: response.read1(RELAY_CHUNK_BYTES), b""):
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return

        def do_POST(self) -> None:
            self._started = time.monotonic()
            if self._is_raw():
                self._forward()
                return
            if self.path.split("?", 1)[0] != INVOKE_PATH:
                self._forward_other()
                return
            if not service.authorized(self.headers.get("Authorization")):
                self._error(HTTPStatus.UNAUTHORIZED, "unauthorized", {"WWW-Authenticate": "Bearer"})
                return
            if not self._available():
                return
            parsed = self._read_request()
            if parsed is None:
                return
            request, header_session = parsed
            outcome = service.acquire()
            if outcome != ACQUIRED:
                self._error(
                    HTTPStatus.TOO_MANY_REQUESTS,
                    outcome,
                    {"Retry-After": str(RETRY_AFTER_SECONDS)},
                )
                return
            invocation = Invocation(service, request, header_session)
            try:
                invocation.start()
                if EVENT_STREAM in (self.headers.get("Accept") or ""):
                    self._stream(invocation)
                else:
                    self._respond(invocation)
            finally:
                invocation.close()
                service.release()

        def _read_limited_body(self, *, empty_ok: bool) -> bytes | None:
            """Lee el cuerpo respetando `max_request_bytes`. Un
            `Content-Length` ausente sólo se admite con `empty_ok`; uno
            negativo o no numérico es una petición inválida y nunca llega a
            `rfile.read`."""
            raw = self.headers.get("Content-Length")
            try:
                length = int(raw) if raw not in (None, "") else (0 if empty_ok else -1)
            except ValueError:
                length = -1
            if length < 0:
                self._error(HTTPStatus.BAD_REQUEST, "invalid_request")
                return None
            if length > service.config.max_request_bytes:
                self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request_too_large")
                return None
            return self.rfile.read(length) if length else b""

        def _read_request(self) -> tuple[Request, str | None] | None:
            body = self._read_limited_body(empty_ok=False)
            if body is None:
                return None
            try:
                return parse_invocation(body, self.headers.get(service.config.session_header))
            except AgentFailure:
                self._error(HTTPStatus.BAD_REQUEST, "invalid_request")
                return None

        def _respond(self, invocation: Invocation) -> None:
            terminal: Event = build_event("agent_failed", reason="runtime_error", detail_code=None)
            for event in invocation.events():
                terminal = event
            self._send_json(HTTPStatus.OK, result_body(invocation, terminal))

        def _stream(self, invocation: Invocation) -> None:
            self.close_connection = True
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", EVENT_STREAM)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for event in invocation.events():
                    self.wfile.write(format_sse(event))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                invocation.abort("aborted")

    return Handler


def build_server(service: Service, port: int, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(service))
    server.daemon_threads = True
    return server
