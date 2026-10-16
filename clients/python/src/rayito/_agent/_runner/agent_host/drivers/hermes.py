"""Driver `hermes` del host (ADR-028, D2, D3 y D9): arranca `hermes gateway
run` (Hermes Agent v0.22.2) con su API HTTP sólo en loopback y le habla por
`POST /v1/runs` y `GET /v1/runs/{id}/events`, traduciendo su SSE a eventos del
protocolo v1 de Rayito. El esquema de esos eventos está medido en
`testdata/agent/hermes-v0.22.2-events.jsonl`.

- **Clave de la API**: `secrets.token_urlsafe` en cada arranque, en memoria y
  sólo en el entorno del proceso hijo (`API_SERVER_KEY`): nunca en un
  fichero, en `envs` ni en argv. Hermes se niega a arrancar sin una de 16 o
  más caracteres.
- **Aislamiento**: `HERMES_HOME` propio, `HERMES_IGNORE_USER_CONFIG=1`,
  `TERMINAL_ENV=local` y `python -P` (el directorio de trabajo del agente no
  tapa los paquetes de Hermes). El `config.yaml` (en sintaxis JSON, que es
  YAML) lo escribe este driver con los toolsets pedidos y todo lo que llama
  a Internet apagado: actualizaciones, telemetría, inicios de sesión
  externos y títulos generados por un segundo modelo.
- **Prompt e instrucciones**: van en el cuerpo de la petición HTTP
  (`input` e `instructions`, el prompt de sistema efímero de Hermes), nunca
  en argv.

Hermes no expone los pasos del modelo ni los argumentos de las herramientas:
una ejecución es un paso (`step_started` al arrancar, `step_finished` con el
uso de `run.completed`) y un `tool_call` por pareja `tool.started` /
`tool.completed`, con el `preview` como entrada y salida.
"""

from __future__ import annotations

import atexit
import http.client
import json
import os
import secrets
import threading
import time
from collections.abc import Iterator, Mapping
from http import HTTPStatus
from pathlib import Path
from typing import Any, Final

from ..process import ChildProcess
from ..protocol import (
    AgentFailure,
    Emit,
    Request,
    RunState,
    build_event,
    count,
    truncate_output,
)
from ._common import load_config, resolve_session_id, sanitize_detail_code

LOOPBACK: Final = "127.0.0.1"
API_KEY_BYTES: Final = 32
HEALTH_PATH: Final = "/health"
RUNS_PATH: Final = "/v1/runs"
STOP_SUFFIX: Final = "/stop"
EVENTS_SUFFIX: Final = "/events"
DEFAULT_START_TIMEOUT_SECONDS: Final = 90.0
HEALTH_POLL_SECONDS: Final = 0.2
#: Hermes manda un comentario `: keepalive` cada 10 s; más de este silencio
#: sin un solo byte es un cuelgue.
STREAM_IDLE_TIMEOUT_SECONDS: Final = 120.0
REQUEST_TIMEOUT_SECONDS: Final = 30.0
CONFIG_FILE_NAME: Final = "config.yaml"
CONFIG_FILE_MODE: Final = 0o600
HOME_DIR_MODE: Final = 0o700
FINISH_REASON: Final = "stop"
FALLBACK_FAILURE_CODE: Final = "run_failed"
TERMINAL_EVENTS: Final = ("run.completed", "run.failed", "run.cancelled", "run.interrupted")
USAGE_KEYS: Final = {
    "input_tokens": "input",
    "output_tokens": "output",
    "cache_read_tokens": "cache_read",
    "cache_write_tokens": "cache_write",
}
#: Variables de Hermes que fija el driver en su proceso (las que no dependen
#: de la configuración del SDK).
STATIC_ENVS: Final[Mapping[str, str]] = {
    "HERMES_IGNORE_USER_CONFIG": "1",
    "TERMINAL_ENV": "local",
    "API_SERVER_ENABLED": "true",
    "API_SERVER_HOST": LOOPBACK,
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
}


def hermes_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """El `config.yaml` de Hermes (claves medidas en el spike): el modelo
    según el cliente, los toolsets del API server, las skills externas de
    sólo lectura y todo lo que sale a Internet apagado."""
    client = str(config["client"])
    model: dict[str, Any] = {"provider": client, "default": config["model"]}
    if client == "custom":
        model.update(base_url=config["base_url"], api_key=config["credential_placeholder"])
    skills: dict[str, Any] = {"creation_nudge_interval": 0}
    if config.get("skills_dir"):
        skills["external_dirs"] = [config["skills_dir"]]
    document: dict[str, Any] = {
        "model": model,
        "platform_toolsets": {"api_server": list(config["toolsets"])},
        "skills": skills,
        "terminal": {"backend": "local", "cwd": config["workdir"]},
        "updates": {"check": False},
        "telemetry": {"shared_metrics": {"enabled": False, "send": False}},
        "auth": {"adopt_external_logins": False},
        "auxiliary": {"title_generation": {"enabled": False}},
    }
    if client == "bedrock":
        document["bedrock"] = {"region": config["region"]}
    return document


def write_home(config: Mapping[str, Any]) -> None:
    """Crea `HERMES_HOME` (0700) y escribe su `config.yaml` (0600)."""
    home = Path(str(config["home_dir"]))
    home.mkdir(parents=True, exist_ok=True, mode=HOME_DIR_MODE)
    path = home / CONFIG_FILE_NAME
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, CONFIG_FILE_MODE)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(hermes_config(config), handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def child_env(config: Mapping[str, Any], api_key: str) -> dict[str, str]:
    """El entorno del proceso de Hermes: el del host (donde viven las
    variables del proveedor) más las de Hermes y la clave de la API."""
    env = dict(os.environ)
    env.update(STATIC_ENVS)
    env.update(
        {
            "HERMES_HOME": str(config["home_dir"]),
            "TERMINAL_CWD": str(config["workdir"]),
            "API_SERVER_PORT": str(config["api_port"]),
            "API_SERVER_KEY": api_key,
            "PYTHONPATH": str(config["source_dir"]),
        }
    )
    return env


def iter_sse_data(response: http.client.HTTPResponse) -> Iterator[str]:
    """Los campos `data` de un stream SSE, uno por mensaje; los comentarios
    (`: open`, `: keepalive`, `: stream closed`) se ignoran."""
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


def _usage_of(raw: object) -> dict[str, int]:
    source = raw if isinstance(raw, Mapping) else {}
    return {
        "input": count(source.get("input_tokens")),
        "output": count(source.get("output_tokens")),
        "reasoning": 0,
        "cache_read": count(source.get("cache_read_tokens")),
        "cache_write": count(source.get("cache_write_tokens")),
    }


class Translator:
    """Los eventos de `/v1/runs/{id}/events` a eventos v1. `feed()` devuelve
    los eventos a emitir; al llegar el evento terminal queda `terminal` con su
    nombre y, si falló, `failure`."""

    def __init__(self, *, reasoning: bool) -> None:
        self._reasoning = reasoning
        self._started: list[tuple[str, str | None]] = []
        self._calls = 0
        self._thinking: list[str] = []
        self.terminal: str | None = None
        self.failure: AgentFailure | None = None

    def feed(self, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        kind = payload.get("event")
        if kind == "message.delta" and isinstance(payload.get("delta"), str):
            return [build_event("text_delta", text=payload["delta"])]
        if kind == "message.interim":
            interim = payload.get("text")
            streamed = payload.get("already_streamed") is True
            return (
                [build_event("text_delta", text=interim)]
                if isinstance(interim, str) and not streamed
                else []
            )
        if kind == "tool.started":
            preview = payload.get("preview")
            self._started.append(
                (str(payload.get("tool", "")), preview if isinstance(preview, str) else None)
            )
            return []
        if kind == "tool.completed":
            return [self._tool_call(payload)]
        if kind == "reasoning.available" and isinstance(payload.get("text"), str):
            self._thinking.append(payload["text"])
            return []
        if kind in TERMINAL_EVENTS:
            return self._terminal(str(kind), payload)
        return []

    def _tool_call(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        self._calls += 1
        name = str(payload.get("tool", ""))
        started = self._started.pop(0) if self._started else (name, None)
        preview = payload.get("preview")
        output, truncated = truncate_output(preview if isinstance(preview, str) else "")
        return build_event(
            "tool_call",
            call_id=f"hermes_{self._calls}",
            name=started[0] or name,
            status="error" if payload.get("error") is True else "completed",
            input={"preview": started[1]} if started[1] is not None else None,
            output=output,
            output_truncated=truncated,
        )

    def _terminal(self, kind: str, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        self.terminal = kind
        if kind != "run.completed":
            fallback = FALLBACK_FAILURE_CODE if kind == "run.failed" else kind.replace(".", "_")
            self.failure = AgentFailure(
                "runtime_error", sanitize_detail_code(payload.get("turn_exit_reason"), fallback)
            )
            return []
        events: list[dict[str, Any]] = []
        output = payload.get("output")
        if isinstance(output, str) and output:
            events.append(build_event("text", text=output))
        if self._reasoning and self._thinking:
            events.append(build_event("reasoning", text="".join(self._thinking)))
        events.append(
            build_event(
                "step_finished",
                index=1,
                usage=_usage_of(payload.get("usage")),
                finish_reason=FINISH_REASON,
            )
        )
        return events


class Driver:
    """El driver `hermes`: un `hermes gateway run` por proceso del host."""

    def __init__(self, config: Mapping[str, Any]) -> None:
        self._config = config
        self._api_key = secrets.token_urlsafe(API_KEY_BYTES)
        self._child: ChildProcess | None = None
        self._run_id: str | None = None
        self._lock = threading.Lock()

    def _connection(self, timeout: float) -> http.client.HTTPConnection:
        return http.client.HTTPConnection(LOOPBACK, int(self._config["api_port"]), timeout=timeout)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

    def warm(self) -> None:
        self._ensure_started()

    def _ensure_started(self) -> None:
        with self._lock:
            if self._child is not None:
                return
            write_home(self._config)
            argv = [
                str(self._config["python"]),
                "-P",
                "-m",
                "hermes_cli.main",
                "gateway",
                "run",
            ]
            self._child = ChildProcess(
                argv,
                env=child_env(self._config, self._api_key),
                cwd=str(self._config["workdir"]),
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
                raise AgentFailure("runtime_error", "hermes_exited")
            try:
                connection = self._connection(REQUEST_TIMEOUT_SECONDS)
                connection.request("GET", HEALTH_PATH)
                status = connection.getresponse().status
                connection.close()
                if status == HTTPStatus.OK:
                    return
            except OSError:
                pass
            time.sleep(HEALTH_POLL_SECONDS)
        raise AgentFailure("runtime_error", "hermes_not_ready")

    def run(self, request: Request, emit: Emit, state: RunState) -> None:
        self._ensure_started()
        state.session_id = resolve_session_id(request.session_id)
        emit(build_event("session", session_id=state.session_id))
        emit(build_event("step_started", index=1))
        run_id = self._start_run(request, state.session_id)
        translator = Translator(reasoning=request.reasoning)
        for payload in self._events(run_id):
            for event in translator.feed(payload):
                emit(event)
            if translator.terminal is not None:
                break
        self._run_id = None
        if translator.failure is not None:
            raise translator.failure
        if translator.terminal is None:
            raise AgentFailure("protocol_error", "no_terminal_event")

    def _start_run(self, request: Request, session_id: str) -> str:
        fields: dict[str, Any] = {"input": request.prompt, "session_id": session_id}
        if self._config.get("instructions"):
            fields["instructions"] = self._config["instructions"]
        body = json.dumps(fields)
        try:
            connection = self._connection(REQUEST_TIMEOUT_SECONDS)
            connection.request("POST", RUNS_PATH, body=body, headers=self._headers())
            response = connection.getresponse()
            payload = json.loads(response.read() or b"{}")
        except (OSError, ValueError):
            raise AgentFailure("runtime_error", "hermes_unreachable") from None
        if response.status not in (HTTPStatus.OK, HTTPStatus.ACCEPTED) or not isinstance(
            payload, Mapping
        ):
            raise AgentFailure("runtime_error", f"hermes_http_{count(response.status)}")
        run_id = payload.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise AgentFailure("protocol_error", "no_run_id")
        self._run_id = run_id
        return run_id

    def _events(self, run_id: str) -> Iterator[Mapping[str, Any]]:
        try:
            connection = self._connection(STREAM_IDLE_TIMEOUT_SECONDS)
            connection.request(
                "GET", f"{RUNS_PATH}/{run_id}{EVENTS_SUFFIX}", headers=self._headers()
            )
            response = connection.getresponse()
            if response.status != HTTPStatus.OK:
                raise AgentFailure("runtime_error", f"hermes_http_{count(response.status)}")
            for data in iter_sse_data(response):
                try:
                    payload = json.loads(data)
                except ValueError:
                    continue
                if isinstance(payload, Mapping):
                    yield payload
        except TimeoutError:
            raise AgentFailure("runtime_error", "stream_idle_timeout") from None
        except OSError:
            raise AgentFailure("runtime_error", "hermes_unreachable") from None

    def cancel(self) -> None:
        """Pide a Hermes que pare la ejecución en curso y, si no contesta,
        mata el proceso."""
        run_id = self._run_id
        if run_id is not None:
            try:
                connection = self._connection(REQUEST_TIMEOUT_SECONDS)
                connection.request(
                    "POST", f"{RUNS_PATH}/{run_id}{STOP_SUFFIX}", headers=self._headers()
                )
                connection.getresponse().read()
                return
            except OSError:
                pass
        with self._lock:
            child = self._child
            self._child = None
        if child is not None:
            child.kill()


def create(options: Mapping[str, Any]) -> Driver:
    return Driver(load_config(options))
