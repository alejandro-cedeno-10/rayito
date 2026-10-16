"""Modo `serve` del host (ADR-028, D3 y D4): sirve `rayito.agent-service/1`,
el contrato HTTP de AgentCore, con `http.server.ThreadingHTTPServer`.

- `GET /ping` -> `200 {"status": "Healthy" | "HealthyBusy", "time_of_last_update"}`.
- `POST /invocations` con `{"prompt", "session_id"?, "model"?, "reasoning"?}`.
  Con `Accept: text/event-stream`, un mensaje SSE por evento v1 hasta `done`
  o `agent_failed`; si no, `{"v", "text", "session_id", "usage", "steps",
  "failed"}`. `400` cuerpo inválido, `413` demasiado grande, `429` con
  `Retry-After` por encima de `max_concurrency`, `503` antes de estar listo
  y `401` si hay token y no coincide.

El host no loguea contenido: sólo método, ruta sin consulta, estado, bytes y
duración. Nunca el prompt, una cabecera ni el token.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import logging
import os
import queue
import threading
import time
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
#: Segundos que `Retry-After` pide esperar con el agente ocupado o sin
#: calentar.
RETRY_AFTER_SECONDS: Final = 1
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
    max_request_bytes: int = MAX_REQUEST_BYTES
    token: str | None = None
    session_header: str = SESSION_HEADER
    ready_marker: str | None = None
    io_timeout_seconds: float = IO_TIMEOUT_SECONDS


def write_failed_marker(config: ServiceConfig, code: str) -> None:
    """Deja el motivo de un arranque fallido (el nombre de una clase de
    excepción, nunca su mensaje) junto al marcador de listo, para que
    `deploy()` falle enseguida y no al agotar su plazo."""
    Service._write_marker(
        None if config.ready_marker is None else config.ready_marker + FAILED_SUFFIX, code
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
        self._lock = threading.Lock()
        self._active = 0
        self._ready = False
        self._last_update = int(clock())

    @property
    def ready(self) -> bool:
        return self._ready

    def mark_ready(self) -> None:
        self._ready = True
        self._write_marker(self.config.ready_marker, "ready")

    def mark_failed(self, code: str) -> None:
        write_failed_marker(self.config, code)

    @staticmethod
    def _write_marker(path: str | None, content: str) -> None:
        if path is not None:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content + "\n")

    def ping(self) -> dict[str, object]:
        with self._lock:
            busy = self._active >= self.config.max_concurrency
            return {
                "status": STATUS_BUSY if busy else STATUS_HEALTHY,
                "time_of_last_update": self._last_update,
            }

    def acquire(self) -> bool:
        with self._lock:
            if self._active >= self.config.max_concurrency:
                return False
            self._active += 1
            if self._active >= self.config.max_concurrency:
                self._last_update = int(self._clock())
            return True

    def release(self) -> None:
        with self._lock:
            was_busy = self._active >= self.config.max_concurrency
            self._active -= 1
            if was_busy:
                self._last_update = int(self._clock())

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

    def __init__(self, service: Service, request: Request) -> None:
        self._service = service
        self._request = request
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

    def _fail(self, reason: str) -> None:
        self._queue.put(
            build_event("agent_failed", reason=reason, detail_code=None, session_id=self.session_id)
        )
        self._closed.set()

    def _work(self) -> None:
        emitter = _QueueEmitter(self._deliver)

        def body(state: RunState) -> None:
            self._service.driver.run(self._request, self._deliver, state)

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
        "steps": invocation.tracker.steps,
        "failed": (
            {"reason": terminal.get("reason"), "detail_code": terminal.get("detail_code")}
            if failed
            else None
        ),
    }


def parse_invocation(body: bytes, header_session: str | None) -> Request:
    """El cuerpo de `POST /invocations` a `Request`; `AgentFailure` si no es
    válido. Sin `session_id` en el cuerpo, vale el de la cabecera."""
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
    return parse_request({**data, "v": PROTOCOL_VERSION, "session_id": session_id})


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

        def do_GET(self) -> None:
            self._started = time.monotonic()
            if self.path.split("?", 1)[0] == HEALTH_PATH:
                self._send_json(HTTPStatus.OK, service.ping())
            elif self._is_raw():
                self._forward()
            else:
                self._error(HTTPStatus.NOT_FOUND, "not_found")

        def do_PUT(self) -> None:
            self._forward_raw()

        do_DELETE = do_PATCH = do_OPTIONS = do_PUT

        def _forward_raw(self) -> None:
            self._started = time.monotonic()
            if self._is_raw():
                self._forward()
            else:
                self._error(HTTPStatus.NOT_FOUND, "not_found")

        def _forward(self) -> None:
            """Un servidor `raw` (MCP u otro protocolo): se reenvía tal cual,
            con la misma autenticación y el mismo tope de cuerpo."""
            if not service.authorized(self.headers.get("Authorization")):
                self._error(HTTPStatus.UNAUTHORIZED, "unauthorized", {"WWW-Authenticate": "Bearer"})
            elif not service.ready:
                self._error(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "not_ready",
                    {"Retry-After": str(RETRY_AFTER_SECONDS)},
                )
            else:
                body = self._read_body()
                if body is not None:
                    self._relay(body)

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
                self._error(HTTPStatus.NOT_FOUND, "not_found")
                return
            if not service.authorized(self.headers.get("Authorization")):
                self._error(HTTPStatus.UNAUTHORIZED, "unauthorized", {"WWW-Authenticate": "Bearer"})
                return
            if not service.ready:
                self._error(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "not_ready",
                    {"Retry-After": str(RETRY_AFTER_SECONDS)},
                )
                return
            request = self._read_request()
            if request is None:
                return
            if not service.acquire():
                self._error(
                    HTTPStatus.TOO_MANY_REQUESTS, "busy", {"Retry-After": str(RETRY_AFTER_SECONDS)}
                )
                return
            invocation = Invocation(service, request)
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

        def _read_request(self) -> Request | None:
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
