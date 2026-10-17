"""Driver `command` del host (ADR-028, D2 y D3): lanza tu `argv`
(`CustomRuntime(command=[...])`) con la petición por stdin y traduce su
stdout a eventos del protocolo v1.

- `protocol="rayito-v1"`: el hijo recibe `{"v":1,"prompt","session_id",
  "model","reasoning"}` en una línea y escribe líneas v1 (`session`,
  `text_delta`, `text`, `tool_call`, `step_started`, `step_finished`...,
  `agent_failed` y `done`); el driver las reenvía tal cual.
- `protocol="text"`: el hijo recibe el prompt tal cual por stdin y la sesión,
  el modelo y el razonamiento por `RAYITO_SESSION_ID`, `RAYITO_MODEL` y
  `RAYITO_REASONING`; cada línea de stdout sale como `text_delta`, y el
  texto completo, como `text`.

Una salida distinta de 0 sin un `agent_failed` propio es `runtime_error`
(ADR-025). El prompt nunca va en argv. Configuración: `command` (lista),
`protocol`, `workdir`, `path_prepend` (directorios que van al principio del
`PATH`, el `bin` del venv de `requirements`) y `app_dir` (el código copiado,
que va al principio del `PYTHONPATH`). Con `protocol="text"` el hijo no
informa de tokens: el uso es desconocido, no cero.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from typing import Any, Final

from ..process import ChildProcess
from ..protocol import (
    DRIVER_FAILURE_REASONS,
    PROGRESS_EVENT_TYPES,
    AgentFailure,
    Emit,
    Request,
    RunState,
    build_event,
)
from ._common import (
    child_argv,
    child_environment,
    load_config,
    resolve_session_id,
    sanitize_detail_code,
)

#: Una línea más larga se descarta (`MAX_AGENT_EVENT_LINE_BYTES` de
#: `limits.json`).
MAX_LINE_BYTES: Final = 4_194_304
PROTOCOL_V1: Final = "rayito-v1"
PROTOCOL_TEXT: Final = "text"
FINISH_REASON: Final = "stop"
SESSION_ID_ENV: Final = "RAYITO_SESSION_ID"
MODEL_ENV: Final = "RAYITO_MODEL"
REASONING_ENV: Final = "RAYITO_REASONING"
FALLBACK_DETAIL_CODE: Final = "child_failed"


def _request_line(request: Request, session_id: str) -> bytes:
    payload = {
        "v": 1,
        "prompt": request.prompt,
        "session_id": session_id,
        "model": request.model,
        "reasoning": request.reasoning,
    }
    return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def _child_env(config: Mapping[str, Any], request: Request, session_id: str) -> dict[str, str]:
    env = child_environment(config)
    env[SESSION_ID_ENV] = session_id
    env[REASONING_ENV] = "1" if request.reasoning else "0"
    if request.model is not None:
        env[MODEL_ENV] = request.model
    return env


class Relay:
    """Lo que se acumula de una ejecución del hijo."""

    def __init__(self) -> None:
        self.done = False
        self.failure: AgentFailure | None = None
        self.ignored_lines = 0

    def relay(self, line: bytes, emit: Emit, state: RunState) -> None:
        """Una línea del hijo (protocolo v1): los eventos de progreso se
        reenvían, `agent_failed` y `done` se recuerdan y lo demás se ignora y
        se cuenta."""
        try:
            payload = json.loads(line)
        except ValueError:
            self.ignored_lines += 1
            return
        if not isinstance(payload, Mapping) or payload.get("v") != 1:
            self.ignored_lines += 1
            return
        kind = payload.get("type")
        if kind in PROGRESS_EVENT_TYPES:
            if kind == "session" and isinstance(payload.get("session_id"), str):
                state.session_id = str(payload["session_id"])
            emit(dict(payload))
        elif kind == "agent_failed":
            reason = payload.get("reason")
            self.failure = AgentFailure(
                str(reason) if reason in DRIVER_FAILURE_REASONS else "protocol_error",
                sanitize_detail_code(payload.get("detail_code"), FALLBACK_DETAIL_CODE),
            )
        elif kind == "done":
            self.done = True
        else:
            self.ignored_lines += 1


class Driver:
    """El driver `command`."""

    def __init__(self, config: Mapping[str, Any]) -> None:
        self._config = config
        self._protocol = str(config.get("protocol", PROTOCOL_V1))
        if self._protocol not in (PROTOCOL_V1, PROTOCOL_TEXT):
            raise AgentFailure("protocol_error", "invalid_protocol")
        self._child: ChildProcess | None = None
        self._lock = threading.Lock()

    def warm(self) -> None:
        """Un comando externo no tiene nada que calentar."""

    def run(self, request: Request, emit: Emit, state: RunState) -> None:
        state.session_id = resolve_session_id(request.session_id)
        emit(build_event("session", session_id=state.session_id))
        env = _child_env(self._config, request, state.session_id)
        with self._lock:
            self._child = ChildProcess(
                child_argv(self._config["command"], env),
                env=env,
                cwd=str(self._config["workdir"]) if self._config.get("workdir") else None,
                stdin=True,
            )
        child = self._child
        text_protocol = self._protocol == PROTOCOL_TEXT
        stdin = (
            request.prompt.encode("utf-8")
            if text_protocol
            else _request_line(request, state.session_id)
        )
        child.send_and_close(stdin)
        try:
            relay = self._read(child, emit, state, text_protocol)
            exit_code = child.wait()
        finally:
            child.kill()
        if relay is not None:
            self._finish_v1(relay, exit_code)
        elif exit_code != 0:
            raise AgentFailure("runtime_error", "exit_nonzero")

    def _read(
        self, child: ChildProcess, emit: Emit, state: RunState, text_protocol: bool
    ) -> Relay | None:
        if text_protocol:
            self._read_text(child, emit)
            return None
        relay = Relay()
        for line, discarded in child.lines(MAX_LINE_BYTES):
            if discarded:
                relay.ignored_lines += 1
            else:
                relay.relay(line, emit, state)
        return relay

    @staticmethod
    def _read_text(child: ChildProcess, emit: Emit) -> None:
        parts: list[str] = []
        emit(build_event("step_started", index=1))
        for line, discarded in child.lines(MAX_LINE_BYTES):
            if discarded:
                continue
            chunk = line.decode("utf-8", errors="replace") + "\n"
            parts.append(chunk)
            emit(build_event("text_delta", text=chunk))
        emit(build_event("text", text="".join(parts)))
        emit(build_event("step_finished", index=1, usage=None, finish_reason=FINISH_REASON))

    @staticmethod
    def _finish_v1(relay: Relay, exit_code: int) -> None:
        """Éxito sólo con salida 0, el `done` del hijo y ningún `agent_failed`."""
        if relay.failure is not None:
            raise relay.failure
        if exit_code != 0:
            raise AgentFailure("runtime_error", "exit_nonzero")
        if not relay.done:
            raise AgentFailure("protocol_error", "no_done")

    def cancel(self) -> None:
        with self._lock:
            child = self._child
        if child is not None:
            child.kill()


def create(options: Mapping[str, Any]) -> Driver:
    return Driver(load_config(options))
