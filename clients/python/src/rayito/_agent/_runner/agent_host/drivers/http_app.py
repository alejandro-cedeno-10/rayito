"""Driver `http-app` del host (ADR-028, D2 y D3): arranca el servidor HTTP
del usuario (`CustomRuntime(app=HttpApp(...))`), espera a su ruta de salud y
traduce `/invocations` a eventos v1.

Configuración del driver:

- `command`: argv del servidor; `port`: puerto en el que escucha;
  `health_path`, `invoke_path`.
- `protocol`: `agentcore` (el servidor ya habla `/ping` + `/invocations`),
  `json` (una app con su propio contrato: el prompt va en el campo
  `request_field` y el texto sale de `response_field`, o del primer campo de
  texto de `JSON_TEXT_KEYS`; sus demás rutas siguen alcanzables),
  `openai-chat` (Chat Completions con streaming), `raw` o `langgraph-api`
  (el LangGraph Platform API de `langgraph dev`; ver abajo).
- `port_env`: la variable de entorno con la que se le dice a la app en qué
  `port` escuchar (por ejemplo `PORT`); sin ella no se toca el entorno.
- Supervisión: una app que muere después de estar lista se relanza con una
  espera creciente (`restart_max`, `restart_window_seconds`,
  `restart_backoff_seconds`, `restart_backoff_max_seconds`; ver
  `process.RestartPolicy`). Mientras se relanza `availability()` es
  `restarting`; pasado el presupuesto es `failed` y el host deja el marcador
  `app_crashloop`.
- `app_dir`: el código copiado; es el directorio de trabajo de la app y va al
  principio de su `PYTHONPATH`, así que `python -m paquete.modulo` funciona.
  `path_prepend`: directorios que van al principio de su `PATH` (el `bin` del
  venv de `requirements`, de modo que `python` es el del venv). `workdir`
  (sin `app_dir`) y `start_timeout_seconds`. El resto del entorno lo hereda
  del host.
- Un arranque fallido (`app_exited`, `app_not_ready`) lleva el final de la
  salida de error de la app, saneado (`diagnostics.py`).
- Una app que no informa de `usage` tiene uso desconocido, no cero.

Con `raw` no hay traducción: el host reenvía tal cual todo lo que no es
`/ping` (`forward()`), y `run()` falla con `protocol_error`: un servidor MCP
o cualquier otro protocolo sólo se expone, no se invoca con `invoke()`. Con
`json` el host traduce `/invocations` y además reenvía tal cual el resto de
rutas (`forwards`).

Con `langgraph-api` (ADR-033 D5, experimental) cada invocación es un
`POST /threads/{thread_id}/runs/wait` con `assistant_id` (obligatorio en la
configuración), la entrada del grafo (el prompt como un mensaje de usuario, o
el `input` estructurado de la petición tal cual: `accepts_input`),
`if_not_exists="create"` y `multitask_strategy="reject"`. El hilo es
`uuid5(LANGGRAPH_THREAD_NAMESPACE, session_id)`, también para la sesión
`app_<hex>` que crea el driver, así que la misma sesión sigue el mismo hilo
tras un relanzamiento de la app. El texto sale de `response_field` o del
último mensaje del estado final (`langgraph_text`), el uso de su
`usage_metadata`, y un grafo detenido en un `interrupt` es
`protocol_error`/`graph_interrupted`. La salud es `GET /ok`, la espera de
arranque es `LANGGRAPH_READY_WAIT_SECONDS` y el hijo arranca sin la
telemetría de la CLI ni las trazas a LangSmith salvo que el entorno las fije
(`LANGGRAPH_CHILD_ENV_DEFAULTS`). Fuentes, consultadas el 2026-10-21:
https://docs.langchain.com/langsmith/agent-server-api/thread-runs/create-run-wait-for-output.md,
https://docs.langchain.com/langsmith/agent-server-api/system/health-check.md y
https://docs.langchain.com/langsmith/data-storage-and-privacy.
"""

from __future__ import annotations

import http.client
import json
import threading
import uuid
from collections.abc import Iterator, Mapping
from http import HTTPStatus
from typing import Any, Final

from ..process import (
    DEFAULT_START_TIMEOUT_SECONDS,
    STATE_FAILED,
    STATE_RESTARTING,
    ChildProcess,
    FailureListener,
    RestartPolicy,
    Supervisor,
)
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
from ._common import child_argv, child_environment

LOOPBACK: Final = "127.0.0.1"
SESSION_ID_PREFIX: Final = "app_"
#: Plazo de una llamada a la app mientras no llegue el del host.
REQUEST_TIMEOUT_SECONDS: Final = 600.0
#: Claves de una respuesta JSON donde buscar el texto, en este orden.
TEXT_KEYS: Final = ("text", "response", "result", "output", "message", "content", "answer")
SESSION_HEADER: Final = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"
PROTOCOL_LANGGRAPH_API: Final = "langgraph-api"
PROTOCOLS: Final = ("agentcore", "json", "openai-chat", "raw", PROTOCOL_LANGGRAPH_API)
#: Rutas por defecto de la app si la configuración no trae `health_path` o
#: `invoke_path` (el contrato de AgentCore).
DEFAULT_HEALTH_PATH: Final = "/ping"
DEFAULT_INVOKE_PATH: Final = "/invocations"
#: Lo que el host usa con `langgraph-api`: `AGENT_LANGGRAPH_HEALTH_PATH`,
#: `AGENT_LANGGRAPH_INVOKE_PATH`, `AGENT_LANGGRAPH_THREAD_NAMESPACE` y
#: `AGENT_LANGGRAPH_READY_WAIT_SECONDS` de `limits.json` (el host no importa
#: `rayito`; una prueba comprueba que coinciden). La espera es provisional
#: hasta medir el arranque de `langgraph dev` (`AWS_API_NOTES.md` Q189).
LANGGRAPH_HEALTH_PATH: Final = "/ok"
LANGGRAPH_INVOKE_PATH: Final = "/threads/{thread_id}/runs/wait"
LANGGRAPH_THREAD_PLACEHOLDER: Final = "{thread_id}"
LANGGRAPH_THREAD_NAMESPACE: Final = uuid.UUID("9f31c836-3dec-4f92-9808-a4a78225e7b9")
LANGGRAPH_READY_WAIT_SECONDS: Final = 60.0
#: Un hilo que no existe se crea, y una segunda ejecución sobre un hilo
#: ocupado falla (`409`) en vez de encolarse sin plazo (por defecto el
#: servidor usa `reject` y `enqueue`).
LANGGRAPH_IF_NOT_EXISTS: Final = "create"
LANGGRAPH_MULTITASK_STRATEGY: Final = "reject"
LANGGRAPH_MESSAGES_KEY: Final = "messages"
LANGGRAPH_INTERRUPT_KEY: Final = "__interrupt__"
LANGGRAPH_USAGE_KEY: Final = "usage_metadata"
LANGGRAPH_TEXT_BLOCK: Final = "text"
USER_ROLE: Final = "user"
#: El entorno que el hijo de `langgraph-api` recibe si el del host no lo
#: fija: sin la telemetría de la CLI de LangGraph ni trazas a LangSmith.
LANGGRAPH_CHILD_ENV_DEFAULTS: Final = (
    ("LANGGRAPH_CLI_NO_ANALYTICS", "1"),
    ("LANGSMITH_TRACING", "false"),
)
#: El campo del cuerpo que lleva el prompt con `json`, si la
#: configuración no trae `request_field`.
DEFAULT_REQUEST_FIELD: Final = "prompt"
#: Dónde buscar el texto de una respuesta `json` cuando no hay
#: `response_field`, en este orden (los candidatos de la detección).
JSON_TEXT_KEYS: Final = ("answer", "output", "text", "response", "result", "content")
#: Los estados de `availability()`.
AVAILABLE: Final = "ready"
RESTARTING: Final = "restarting"
FAILED: Final = "failed"
#: Cómo llama una app a cada contador de `usage`, en este orden: los nombres
#: de Rayito, los de Anthropic y los de Chat Completions. Una app que no
#: informa de ninguno tiene uso desconocido, no cero.
USAGE_ALIASES: Final = (
    ("input", ("input", "input_tokens", "prompt_tokens")),
    ("output", ("output", "output_tokens", "completion_tokens")),
    ("reasoning", ("reasoning", "reasoning_tokens")),
    ("cache_read", ("cache_read", "cache_read_input_tokens")),
    ("cache_write", ("cache_write", "cache_creation_input_tokens")),
)


def app_usage(raw: object) -> dict[str, int] | None:
    """El uso de tokens que informa la app (`usage` de su respuesta) en las
    claves de Rayito, o `None` si no informa de ninguno."""
    if not isinstance(raw, Mapping):
        return None
    found: dict[str, Any] = {}
    for key, aliases in USAGE_ALIASES:
        for alias in aliases:
            value = raw.get(alias)
            if isinstance(value, int) and not isinstance(value, bool):
                found[key] = value
                break
    return usage_dict(found) if found else None


def extract_text(payload: object, keys: tuple[str, ...] = TEXT_KEYS) -> str:
    """El texto de una respuesta JSON de la app: una cadena tal cual, la
    primera clave de `keys` que sea cadena, o el JSON entero."""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, Mapping):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, str):
                return value
    return json.dumps(payload, ensure_ascii=False)


def field_text(payload: object, path: str) -> str | None:
    """El texto en `path` (claves separadas por puntos) de la respuesta, o
    `None` si falta o no es una cadena."""
    current = payload
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current if isinstance(current, str) else None


def langgraph_thread_id(session_id: str) -> str:
    """El hilo del LangGraph Platform API de una sesión: determinista y sin
    estado, así que sobrevive a un relanzamiento de la app."""
    return str(uuid.uuid5(LANGGRAPH_THREAD_NAMESPACE, session_id))


def langgraph_invoke_path(template: str, session_id: str) -> str:
    """La ruta de la ejecución con el hilo de la sesión en `{thread_id}`."""
    return template.replace(LANGGRAPH_THREAD_PLACEHOLDER, langgraph_thread_id(session_id))


def langgraph_prompt_input(prompt: str) -> dict[str, Any]:
    """La entrada del grafo para un prompt: un único mensaje de usuario."""
    return {LANGGRAPH_MESSAGES_KEY: [{"role": USER_ROLE, "content": prompt}]}


def langgraph_input(request: Request) -> dict[str, Any]:
    """La entrada del grafo de una petición: su `input` estructurado tal cual
    (ADR-033 D3) o, si no lo trae, el prompt como un mensaje de usuario."""
    if request.input is not None:
        return dict(request.input)
    return langgraph_prompt_input(request.prompt)


def langgraph_body(assistant_id: str, graph_input: Mapping[str, Any]) -> dict[str, Any]:
    """El cuerpo de `POST /threads/{thread_id}/runs/wait`."""
    return {
        "assistant_id": assistant_id,
        "input": dict(graph_input),
        "if_not_exists": LANGGRAPH_IF_NOT_EXISTS,
        "multitask_strategy": LANGGRAPH_MULTITASK_STRATEGY,
    }


def langgraph_last_message(state: object) -> Mapping[str, Any] | None:
    """El último mensaje de `messages` del estado final, o `None`."""
    messages = state.get(LANGGRAPH_MESSAGES_KEY) if isinstance(state, Mapping) else None
    if not isinstance(messages, list) or not messages:
        return None
    last = messages[-1]
    return last if isinstance(last, Mapping) else None


def langgraph_message_text(message: Mapping[str, Any]) -> str:
    """El `content` de un mensaje: una cadena tal cual, o la concatenación de
    sus bloques de texto (los de herramientas se descartan)."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        block["text"]
        for block in content
        if isinstance(block, Mapping)
        and block.get("type") == LANGGRAPH_TEXT_BLOCK
        and isinstance(block.get("text"), str)
    )


def langgraph_text(state: object) -> str:
    """El texto del estado final: el del último mensaje o, sin mensajes, el
    JSON entero."""
    message = langgraph_last_message(state)
    if message is None:
        return json.dumps(state, ensure_ascii=False)
    return langgraph_message_text(message)


def langgraph_usage(state: object) -> dict[str, int] | None:
    """El uso de tokens de `usage_metadata` del último mensaje, o `None`
    (desconocido, no cero)."""
    message = langgraph_last_message(state)
    return None if message is None else app_usage(message.get(LANGGRAPH_USAGE_KEY))


def check_langgraph_interrupt(state: object) -> None:
    """Un grafo detenido en un `interrupt` (human-in-the-loop) no se
    reanuda desde el host: es `protocol_error`/`graph_interrupted`."""
    if isinstance(state, Mapping) and state.get(LANGGRAPH_INTERRUPT_KEY):
        raise AgentFailure("protocol_error", "graph_interrupted")


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


def _delta_of(
    data: str, protocol: str, text_keys: tuple[str, ...] = TEXT_KEYS
) -> tuple[str, dict[str, int] | None]:
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
        return delta, app_usage(payload.get("usage"))
    usage = app_usage(payload.get("usage")) if isinstance(payload, Mapping) else None
    return extract_text(payload, text_keys), usage


class Driver:
    """El driver `http-app`."""

    def __init__(self, config: Mapping[str, Any]) -> None:
        self._config = config
        self._protocol = str(config.get("protocol", "agentcore"))
        if self._protocol not in PROTOCOLS:
            raise AgentFailure("protocol_error", "invalid_protocol")
        if self._langgraph and not config.get("assistant_id"):
            raise AgentFailure("protocol_error", "invalid_protocol")
        self._supervisor: Supervisor | None = None
        self._listeners: list[FailureListener] = []
        self._lock = threading.Lock()
        self._request_timeout = REQUEST_TIMEOUT_SECONDS

    @property
    def _langgraph(self) -> bool:
        return self._protocol == PROTOCOL_LANGGRAPH_API

    @property
    def accepts_input(self) -> bool:
        """Sólo `langgraph-api` entiende `Request.input`: va como la entrada
        del grafo. Con cualquier otro protocolo un `input` es
        `protocol_error`/`invalid_input` (`require_input_support`)."""
        return self._langgraph

    @property
    def raw(self) -> bool:
        """Sin traducción: `/invocations` tampoco existe, todo se reenvía."""
        return self._protocol == "raw"

    @property
    def forwards(self) -> bool:
        """El host reenvía a la app las rutas que no son `/ping` ni
        `/invocations` (`raw` y `json`)."""
        return self._protocol in ("raw", "json")

    def warm(self) -> None:
        self._ensure_started()

    def add_failure_listener(self, listener: FailureListener) -> None:
        """Quien se entera de un bucle de caídas (`app_crashloop`)."""
        self._listeners.append(listener)
        if self._supervisor is not None:
            self._supervisor.add_failure_listener(listener)

    def availability(self) -> str:
        """`ready`, `restarting` (la app murió y se relanza) o `failed`
        (agotó su presupuesto de relanzamientos)."""
        supervisor = self._supervisor
        state = AVAILABLE if supervisor is None else supervisor.state
        if state == STATE_RESTARTING:
            return RESTARTING
        return FAILED if state == STATE_FAILED else AVAILABLE

    def _connection(self) -> http.client.HTTPConnection:
        return http.client.HTTPConnection(
            LOOPBACK, int(self._config["port"]), timeout=self._request_timeout
        )

    def _ensure_started(self) -> None:
        with self._lock:
            if self._supervisor is not None:
                return
            supervisor = Supervisor(
                self._spawn,
                self._healthy,
                failure_codes=("app_exited", "app_not_ready"),
                policy=RestartPolicy.from_config(self._config),
                start_timeout_seconds=float(
                    self._config.get("start_timeout_seconds", self._default_start_timeout())
                ),
            )
            for listener in self._listeners:
                supervisor.add_failure_listener(listener)
            self._supervisor = supervisor
        try:
            supervisor.start()
        except AgentFailure:
            with self._lock:
                self._supervisor = None
            raise

    def _default_start_timeout(self) -> float:
        """La espera de arranque si la configuración no la trae: la propia de
        `langgraph dev` con `langgraph-api` y la general en otro caso."""
        return LANGGRAPH_READY_WAIT_SECONDS if self._langgraph else DEFAULT_START_TIMEOUT_SECONDS

    def _path(self, key: str, default: str, langgraph_default: str) -> str:
        """`health_path` o `invoke_path` de la configuración, o su valor por
        defecto según el protocolo."""
        return str(self._config.get(key, langgraph_default if self._langgraph else default))

    def _spawn(self) -> ChildProcess:
        env = child_environment(self._config)
        if self._langgraph:
            for name, value in LANGGRAPH_CHILD_ENV_DEFAULTS:
                env.setdefault(name, value)
        port_env = self._config.get("port_env")
        if port_env:
            env[str(port_env)] = str(self._config["port"])
        return ChildProcess(
            child_argv(self._config["command"], env),
            env=env,
            cwd=self._working_directory(),
            capture_stdout=False,
            capture_stderr_tail=True,
        )

    def _working_directory(self) -> str | None:
        """El directorio del código copiado (`app_dir`), de modo que
        `python -m paquete.modulo` y las rutas relativas funcionen; sin él,
        el directorio de trabajo del agente."""
        directory = self._config.get("app_dir") or self._config.get("workdir")
        return str(directory) if directory else None

    def _healthy(self) -> bool:
        try:
            connection = self._connection()
            connection.request(
                "GET", self._path("health_path", DEFAULT_HEALTH_PATH, LANGGRAPH_HEALTH_PATH)
            )
            status = connection.getresponse().status
            connection.close()
        except OSError:
            return False
        return status == HTTPStatus.OK

    def forward(
        self, method: str, path: str, headers: Mapping[str, str], body: bytes | None
    ) -> http.client.HTTPResponse:
        """Reenvía una petición tal cual a la app (`raw` y `json`)."""
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
        emit(build_event("step_finished", index=1, usage=usage, finish_reason="stop"))

    def _body(self, request: Request, session_id: str) -> dict[str, Any]:
        if self._langgraph:
            assistant_id = str(self._config["assistant_id"])
            return langgraph_body(assistant_id, langgraph_input(request))
        if self._protocol == "openai-chat":
            return {
                "model": request.model or self._config.get("model", ""),
                "messages": [{"role": "user", "content": request.prompt}],
                "stream": True,
                "stream_options": {"include_usage": True},
            }
        if self._protocol == "json":
            field = str(self._config.get("request_field") or DEFAULT_REQUEST_FIELD)
            return {field: request.prompt, "session_id": session_id}
        body: dict[str, Any] = {"prompt": request.prompt}
        if request.model is not None:
            body["model"] = request.model
        return body

    def _post(self, request: Request, session_id: str) -> http.client.HTTPResponse:
        invoke_path = self._invoke_path(session_id)
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream, application/json",
        }
        if self._protocol not in ("openai-chat", PROTOCOL_LANGGRAPH_API):
            headers[SESSION_HEADER] = session_id
        body = self._body(request, session_id)
        try:
            connection = self._connection()
            connection.request("POST", invoke_path, body=json.dumps(body), headers=headers)
            response = connection.getresponse()
        except OSError:
            raise AgentFailure("runtime_error", "app_unreachable") from None
        if response.status != HTTPStatus.OK:
            raise AgentFailure("runtime_error", f"app_http_{count(response.status)}")
        return response

    def _invoke_path(self, session_id: str) -> str:
        """La ruta de la invocación; con `langgraph-api`, la del hilo de la
        sesión."""
        path = self._path("invoke_path", DEFAULT_INVOKE_PATH, LANGGRAPH_INVOKE_PATH)
        return langgraph_invoke_path(path, session_id) if self._langgraph else path

    def _json_text(self, payload: object) -> str:
        """El texto de una respuesta `json`: `response_field` si se
        configuró (y es un error que falte) o el primer candidato."""
        field = self._config.get("response_field")
        if not field:
            return extract_text(payload, JSON_TEXT_KEYS)
        return self._field_text(payload, str(field))

    def _langgraph_result(self, state: object) -> tuple[str, dict[str, int] | None]:
        """El texto y el uso del estado final de un grafo: `response_field`
        si se configuró o el último mensaje (`langgraph_text`)."""
        check_langgraph_interrupt(state)
        field = self._config.get("response_field")
        text = self._field_text(state, str(field)) if field else langgraph_text(state)
        return text, langgraph_usage(state)

    @staticmethod
    def _field_text(payload: object, field: str) -> str:
        """El texto en `field`; que falte es `invalid_response`."""
        text = field_text(payload, field)
        if text is None:
            raise AgentFailure("protocol_error", "invalid_response")
        return text

    def _text_keys(self) -> tuple[str, ...]:
        """Las claves donde buscar el texto de un mensaje SSE: las de la
        detección con `json`, o `response_field` si se configuró."""
        if self._protocol != "json":
            return TEXT_KEYS
        field = self._config.get("response_field")
        return (str(field),) if field else JSON_TEXT_KEYS

    def _read(
        self, response: http.client.HTTPResponse, emit: Emit
    ) -> tuple[str, dict[str, int] | None]:
        content_type = response.getheader("Content-Type", "")
        if "text/event-stream" not in content_type:
            try:
                payload = json.loads(response.read())
            except ValueError:
                raise AgentFailure("protocol_error", "invalid_response") from None
            if self._langgraph:
                return self._langgraph_result(payload)
            reported = app_usage(payload.get("usage")) if isinstance(payload, Mapping) else None
            text = self._json_text(payload) if self._protocol == "json" else extract_text(payload)
            return text, reported
        parts: list[str] = []
        usage: dict[str, int] | None = None
        for data in iter_sse_data(response):
            if data.strip() == "[DONE]":
                break
            delta, found = _delta_of(data, self._protocol, self._text_keys())
            usage = found or usage
            if delta:
                parts.append(delta)
                emit(build_event("text_delta", text=delta))
        return "".join(parts), usage

    def cancel(self) -> None:
        with self._lock:
            supervisor, self._supervisor = self._supervisor, None
        if supervisor is not None:
            supervisor.stop()


def create(config: Mapping[str, Any]) -> Driver:
    return Driver(config)
