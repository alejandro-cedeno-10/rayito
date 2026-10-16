"""Driver `http-app` del host (ADR-028, D2 y D3): arranca el servidor HTTP
del usuario (`CustomRuntime(app=HttpApp(...))`), espera a su ruta de salud y
traduce `/invocations` a eventos v1.

Configuración del driver:

- `command`: argv del servidor; `port`: puerto en el que escucha;
  `health_path`, `invoke_path`.
- `protocol`: `agentcore` (el servidor ya habla `/ping` + `/invocations`),
  `openai-chat` (Chat Completions con streaming) o `raw`.
- `workdir` y `start_timeout_seconds`. Las variables de entorno las hereda
  del host.

Con `raw` no hay traducción: el host reenvía tal cual todo lo que no es
`/ping` (`forward()`), y `run()` falla con `protocol_error`: un servidor MCP
o cualquier otro protocolo sólo se expone, no se invoca con `invoke()`.
"""

from __future__ import annotations

import atexit
import http.client
import json
import os
import threading
import time
from collections.abc import Iterator, Mapping
from http import HTTPStatus
from typing import Any, Final

from ..process import ChildProcess
from ..protocol import (
    AgentFailure,
    Emit,
    Request,
    RunState,
    build_event,
    count,
    new_id,
    usage_dict,
)

LOOPBACK: Final = "127.0.0.1"
SESSION_ID_PREFIX: Final = "app_"
#: Segundos que se espera a que la app conteste a su ruta de salud
#: (`AGENT_SERVICE_READY_TIMEOUT_SECONDS` de `limits.json` es el tope del
#: SDK; el de la app, algo menor).
DEFAULT_START_TIMEOUT_SECONDS: Final = 90.0
HEALTH_POLL_SECONDS: Final = 0.2
#: Plazo de una llamada a la app mientras no llegue el del host.
REQUEST_TIMEOUT_SECONDS: Final = 600.0
#: Claves de una respuesta JSON donde buscar el texto, en este orden.
TEXT_KEYS: Final = ("text", "response", "result", "output", "message", "content", "answer")
SESSION_HEADER: Final = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"
PROTOCOLS: Final = ("agentcore", "openai-chat", "raw")


def extract_text(payload: object) -> str:
    """El texto de una respuesta JSON de la app: una cadena tal cual, la
    primera clave de `TEXT_KEYS` que sea cadena, o el JSON entero."""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, Mapping):
        for key in TEXT_KEYS:
            value = payload.get(key)
            if isinstance(value, str):
                return value
    return json.dumps(payload, ensure_ascii=False)


def iter_sse_data(response: http.client.HTTPResponse) -> Iterator[str]:
    """Los campos `data` de un stream SSE, uno por mensaje."""
    data: list[str] = []
    for raw in response:
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            if data:
                yield "\n".join(data)
                data = []
        elif line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
    if data:
        yield "\n".join(data)


def _delta_of(data: str, protocol: str) -> tuple[str, dict[str, Any] | None]:
    """El texto de un mensaje SSE y, si lo trae, el uso de tokens."""
    try:
        payload = json.loads(data)
    except ValueError:
        return data, None
    if protocol == "openai-chat" and isinstance(payload, Mapping):
        choices = payload.get("choices")
        delta = ""
        if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
            inner = choices[0].get("delta")
            content = inner.get("content") if isinstance(inner, Mapping) else None
            delta = content if isinstance(content, str) else ""
        usage = payload.get("usage")
        return delta, dict(usage) if isinstance(usage, Mapping) else None
    return extract_text(payload), None


def _chat_usage(raw: Mapping[str, Any] | None) -> dict[str, int]:
    source = raw or {}
    return usage_dict(
        {"input": source.get("prompt_tokens"), "output": source.get("completion_tokens")}
    )


class Driver:
    """El driver `http-app`."""

    def __init__(self, config: Mapping[str, Any]) -> None:
        self._config = config
        self._protocol = str(config.get("protocol", "agentcore"))
        if self._protocol not in PROTOCOLS:
            raise AgentFailure("protocol_error", "invalid_protocol")
        self._child: ChildProcess | None = None
        self._lock = threading.Lock()
        self._request_timeout = REQUEST_TIMEOUT_SECONDS

    @property
    def raw(self) -> bool:
        return self._protocol == "raw"

    def warm(self) -> None:
        self._ensure_started()

    def _connection(self) -> http.client.HTTPConnection:
        return http.client.HTTPConnection(
            LOOPBACK, int(self._config["port"]), timeout=self._request_timeout
        )

    def _ensure_started(self) -> None:
        with self._lock:
            if self._child is not None:
                return
            env = dict(os.environ)
            self._child = ChildProcess(
                [str(part) for part in self._config["command"]],
                env=env,
                cwd=str(self._config["workdir"]) if self._config.get("workdir") else None,
                capture_stdout=False,
            )
            atexit.register(self._child.kill)
        self._wait_healthy()

    def _wait_healthy(self) -> None:
        deadline = time.monotonic() + float(
            self._config.get("start_timeout_seconds", DEFAULT_START_TIMEOUT_SECONDS)
        )
        while time.monotonic() < deadline:
            child = self._child
            if child is None or child.poll() is not None:
                raise AgentFailure("runtime_error", "app_exited")
            try:
                connection = self._connection()
                connection.request("GET", str(self._config.get("health_path", "/ping")))
                status = connection.getresponse().status
                connection.close()
                if status == HTTPStatus.OK:
                    return
            except OSError:
                pass
            time.sleep(HEALTH_POLL_SECONDS)
        raise AgentFailure("runtime_error", "app_not_ready")

    def forward(
        self, method: str, path: str, headers: Mapping[str, str], body: bytes | None
    ) -> http.client.HTTPResponse:
        """Reenvía una petición tal cual a la app (sólo `raw`)."""
        self._ensure_started()
        connection = self._connection()
        try:
            connection.request(method, path, body=body, headers=dict(headers))
            return connection.getresponse()
        except OSError:
            raise AgentFailure("runtime_error", "app_unreachable") from None

    def run(self, request: Request, emit: Emit, state: RunState) -> None:
        if self.raw:
            raise AgentFailure("protocol_error", "raw_app")
        self._ensure_started()
        state.session_id = request.session_id or new_id(SESSION_ID_PREFIX)
        emit(build_event("session", session_id=state.session_id))
        emit(build_event("step_started", index=1))
        response = self._post(request, state.session_id)
        text, usage = self._read(response, emit)
        emit(build_event("text", text=text))
        emit(build_event("step_finished", index=1, usage=_chat_usage(usage), finish_reason="stop"))

    def _post(self, request: Request, session_id: str) -> http.client.HTTPResponse:
        invoke_path = str(self._config.get("invoke_path", "/invocations"))
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream, application/json",
        }
        if self._protocol == "openai-chat":
            body: dict[str, Any] = {
                "model": request.model or self._config.get("model", ""),
                "messages": [{"role": "user", "content": request.prompt}],
                "stream": True,
                "stream_options": {"include_usage": True},
            }
        else:
            headers[SESSION_HEADER] = session_id
            body = {"prompt": request.prompt}
            if request.model is not None:
                body["model"] = request.model
        try:
            connection = self._connection()
            connection.request("POST", invoke_path, body=json.dumps(body), headers=headers)
            response = connection.getresponse()
        except OSError:
            raise AgentFailure("runtime_error", "app_unreachable") from None
        if response.status != HTTPStatus.OK:
            raise AgentFailure("runtime_error", f"app_http_{count(response.status)}")
        return response

    def _read(
        self, response: http.client.HTTPResponse, emit: Emit
    ) -> tuple[str, dict[str, Any] | None]:
        content_type = response.getheader("Content-Type", "")
        if "text/event-stream" not in content_type:
            try:
                return extract_text(json.loads(response.read())), None
            except ValueError:
                raise AgentFailure("protocol_error", "invalid_response") from None
        parts: list[str] = []
        usage: dict[str, Any] | None = None
        for data in iter_sse_data(response):
            if data.strip() == "[DONE]":
                break
            delta, found = _delta_of(data, self._protocol)
            usage = found or usage
            if delta:
                parts.append(delta)
                emit(build_event("text_delta", text=delta))
        return "".join(parts), usage

    def cancel(self) -> None:
        with self._lock:
            child = self._child
        if child is not None:
            child.kill()
            with self._lock:
                self._child = None


def create(config: Mapping[str, Any]) -> Driver:
    return Driver(config)
