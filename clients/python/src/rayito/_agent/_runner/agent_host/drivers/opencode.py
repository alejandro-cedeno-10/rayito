"""Driver de OpenCode del host (ADR-028, D3): lanza `opencode run --format
json` con los mismos argumentos que construye el adaptador del SDK y traduce
su JSONL (v1.18.34, `packages/opencode/src/cli/cmd/run.ts`) a eventos del
protocolo v1 de Rayito. Es la tercera copia de esa traducción (el SDK de
Python y el de TypeScript tienen las suyas); los vectores compartidos
`testdata/agent/opencode-v1.18.34-events.jsonl` y `expected-events.json` la
mantienen alineada.

Opciones del driver (`HostConfig.options`):

- `workdir`: directorio de trabajo del agente (`--dir`).
- `session_title`: título fijo (`--title`; sin él OpenCode pide uno a otro
  modelo); por defecto `OPENCODE_SESSION_TITLE`.
- `binary`: ruta del binario; por defecto `opencode` del PATH.

Las variables de `HostConfig.envs` (`OPENCODE_CONFIG`, las banderas y las de
AWS) las pone quien lanza el host y el hijo las hereda. El id del proveedor
para `-m <proveedor>/<modelo>` sale del `model` del `opencode.json` que
apunta `OPENCODE_CONFIG`. El prompt va por stdin, nunca en argv.
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Mapping
from typing import Any, Final

from ..process import ChildProcess
from ..protocol import (
    AgentFailure,
    Emit,
    Request,
    RunState,
    build_event,
    truncate_output,
    usage_dict,
)

#: Una línea más larga se descarta (`MAX_AGENT_EVENT_LINE_BYTES` de
#: `limits.json`: OpenCode manda el `output` de una `tool_use` en la línea).
MAX_LINE_BYTES: Final = 4_194_304
#: Un `sessionID` de OpenCode (`ses_...`): sólo así se mete en un comando.
SESSION_ID_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]{1,128}")
DEFAULT_BINARY: Final = "opencode"
#: `OPENCODE_SESSION_TITLE` de `limits.json`.
SESSION_TITLE: Final = "rayito"
CONFIG_ENV: Final = "OPENCODE_CONFIG"


def _provider_id() -> str:
    """El proveedor del `model` (`<proveedor>/<modelo>`) del `opencode.json`
    que escribió el SDK."""
    try:
        with open(os.environ[CONFIG_ENV], encoding="utf-8") as handle:
            model = json.load(handle)["model"]
        return str(model).split("/", 1)[0]
    except (KeyError, OSError, ValueError):
        raise AgentFailure("runtime_error", "opencode_config_unreadable") from None


def build_args(config: Mapping[str, Any], request: Request) -> list[str]:
    """Los argumentos de `opencode run`, en el orden del adaptador del SDK
    (`testdata/agent/opencode-run-commands.json`)."""
    args = [
        str(config.get("binary", DEFAULT_BINARY)),
        "run",
        "--format",
        "json",
        "--auto",
        "--title",
        str(config.get("session_title", SESSION_TITLE)),
        "--dir",
        str(config["workdir"]),
    ]
    if request.model is not None:
        args += ["-m", f"{_provider_id()}/{request.model}"]
    if request.session_id is not None:
        if not SESSION_ID_PATTERN.fullmatch(request.session_id):
            raise AgentFailure("protocol_error", "invalid_session_id")
        args += ["-s", request.session_id]
    if request.reasoning:
        args.append("--thinking")
    return args


class Translation:
    """Lo que se acumula de una ejecución para traducir y cerrar."""

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.steps = 0
        self.failure: AgentFailure | None = None
        self.ignored_lines = 0


def _tool_call(part: Mapping[str, Any]) -> dict[str, Any] | None:
    tool_state = part.get("state")
    if not isinstance(tool_state, Mapping):
        return None
    status = tool_state.get("status")
    if status not in ("completed", "error"):
        return None
    raw_input = tool_state.get("input")
    raw_output = tool_state.get("output") if status == "completed" else tool_state.get("error")
    output, truncated = (None, False)
    if isinstance(raw_output, str):
        output, truncated = truncate_output(raw_output)
    return build_event(
        "tool_call",
        call_id=str(part.get("callID", "")),
        name=str(part.get("tool", "")),
        status=status,
        input=dict(raw_input) if isinstance(raw_input, Mapping) else None,
        output=output,
        output_truncated=truncated,
    )


def _opencode_usage(tokens: object) -> dict[str, int]:
    source = tokens if isinstance(tokens, Mapping) else {}
    cache = source.get("cache")
    cache = cache if isinstance(cache, Mapping) else {}
    return usage_dict(
        {
            "input": source.get("input"),
            "output": source.get("output"),
            "reasoning": source.get("reasoning"),
            "cache_read": cache.get("read"),
            "cache_write": cache.get("write"),
        }
    )


def _step_events(
    kind: object, part: Mapping[str, Any], translation: Translation
) -> list[dict[str, Any]] | None:
    if kind == "step_start":
        translation.steps += 1
        return [build_event("step_started", index=translation.steps)]
    if kind == "step_finish":
        reason = part.get("reason")
        return [
            build_event(
                "step_finished",
                index=translation.steps,
                usage=_opencode_usage(part.get("tokens")),
                finish_reason=reason if isinstance(reason, str) else None,
            )
        ]
    if kind in ("text", "reasoning"):
        text = part.get("text")
        return [build_event(str(kind), text=text)] if isinstance(text, str) else None
    if kind == "tool_use":
        call = _tool_call(part)
        return None if call is None else [call]
    return None


def translate_line(line: bytes, translation: Translation) -> list[dict[str, Any]]:
    """Una línea JSONL de OpenCode a cero o más eventos v1. Lo que no es
    JSON o tiene un `type` desconocido se ignora y se cuenta. El primer
    `sessionID` visto produce el evento `session`."""
    try:
        payload = json.loads(line)
    except ValueError:
        translation.ignored_lines += 1
        return []
    if not isinstance(payload, Mapping):
        translation.ignored_lines += 1
        return []
    events: list[dict[str, Any]] = []
    session_id = payload.get("sessionID")
    if isinstance(session_id, str) and session_id and translation.session_id is None:
        translation.session_id = session_id
        events.append(build_event("session", session_id=session_id))
    part = payload.get("part")
    mapped = _step_events(
        payload.get("type"), part if isinstance(part, Mapping) else {}, translation
    )
    if mapped is None and payload.get("type") == "error":
        error = payload.get("error")
        name = error.get("name") if isinstance(error, Mapping) else None
        translation.failure = AgentFailure("model_error", name if isinstance(name, str) else "")
        mapped = []
    if mapped is None:
        translation.ignored_lines += 1
        return events
    return [*events, *mapped]


class Driver:
    """El driver de OpenCode del host."""

    def __init__(self, config: Mapping[str, Any]) -> None:
        self._config = config
        self._child: ChildProcess | None = None
        self._lock = threading.Lock()

    def warm(self) -> None:
        """OpenCode es un binario único: no hay nada que calentar."""

    def run(self, request: Request, emit: Emit, state: RunState) -> None:
        args = build_args(self._config, request)
        env = dict(os.environ)
        translation = Translation()
        with self._lock:
            self._child = ChildProcess(args, env=env, cwd=str(self._config["workdir"]), stdin=True)
        child = self._child
        child.send_and_close(request.prompt.encode("utf-8"))
        try:
            for line, discarded in child.lines(MAX_LINE_BYTES):
                if discarded:
                    translation.ignored_lines += 1
                    continue
                for event in translate_line(line, translation):
                    state.session_id = translation.session_id
                    emit(event)
            exit_code = child.wait()
        finally:
            child.kill()
        self._finish(translation, exit_code)

    @staticmethod
    def _finish(translation: Translation, exit_code: int) -> None:
        """Éxito sólo con salida 0, ningún `error` y una sesión vista."""
        if translation.failure is not None:
            raise translation.failure
        if exit_code != 0:
            raise AgentFailure("runtime_error", "exit_nonzero")
        if translation.session_id is None:
            raise AgentFailure("protocol_error", "no_session")

    def cancel(self) -> None:
        with self._lock:
            child = self._child
        if child is not None:
            child.kill()


def create(config: Mapping[str, Any]) -> Driver:
    return Driver(config)
