"""Protocolo JSONL v1 de Rayito, lado del host del agente (`agent_host`,
ADR-028, design.md D3). Un evento es un diccionario `{"v": 1, "type": ...}`;
los drivers emiten los de progreso (`session`, `step_started`, `text_delta`,
`text`, `reasoning`, `tool_call`, `step_finished`) y el host cierra cada
ejecución con `done` o `agent_failed`.

Sólo biblioteca estándar y nada de `rayito`: dentro del sandbox el SDK no
está instalado. Los valores con significado compartido con el SDK
(`AGENT_PROTOCOL_VERSION`, `MAX_TOOL_OUTPUT_PREVIEW_BYTES` de `limits.json`)
se repiten aquí con nombre y un test comprueba que coinciden.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import IO, Any, Final, Protocol

#: Versión del protocolo JSONL (`AGENT_PROTOCOL_VERSION` de `limits.json`).
PROTOCOL_VERSION: Final = 1
#: Recorte de la salida de una herramienta en el evento `tool_call`
#: (`MAX_TOOL_OUTPUT_PREVIEW_BYTES` de `limits.json`).
MAX_TOOL_OUTPUT_BYTES: Final = 65536
#: Los tipos de evento de progreso que un driver puede emitir.
PROGRESS_EVENT_TYPES: Final = (
    "session",
    "step_started",
    "text_delta",
    "text",
    "reasoning",
    "tool_call",
    "step_finished",
)
#: Los dos eventos que cierran una ejecución.
TERMINAL_EVENT_TYPES: Final = ("done", "agent_failed")
#: Los motivos de fallo que un driver o el host pueden declarar; los que
#: decide el SDK (`timeout`, `max_steps`...) en modo servicio los impone
#: `limits.py` con los mismos nombres.
DRIVER_FAILURE_REASONS: Final = ("model_error", "runtime_error", "protocol_error")
#: Claves del uso de tokens de un paso (`TokenUsage` del SDK).
USAGE_KEYS: Final = ("input", "output", "reasoning", "cache_read", "cache_write")

Event = dict[str, Any]
Emit = Callable[[Event], None]


class AgentFailure(Exception):
    """Un fallo con `reason` y `detail_code` del protocolo, nunca con texto
    del proveedor ni contenido. `diagnostics` es la cola de la salida de
    error de la app del usuario en un arranque fallido: no viaja por el
    protocolo, sólo la lee el host para el marcador de fallo."""

    def __init__(self, reason: str, detail_code: str, diagnostics: str | None = None) -> None:
        super().__init__(detail_code)
        self.reason = reason
        self.detail_code = detail_code
        self.diagnostics = diagnostics


class LimitExceeded(BaseException):
    """Corta una ejecución desde el emisor cuando se rebasa un límite o se
    cancela. Hereda de `BaseException` para que ningún `except Exception` de
    un driver la convierta en un `runtime_error`."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Request:
    """La petición de una ejecución, ya validada. `aws_env` lleva las
    credenciales del rol del runtime y no sale en `repr`."""

    prompt: str
    session_id: str | None = None
    model: str | None = None
    reasoning: bool = False
    aws_env: Mapping[str, str] | None = field(default=None, repr=False)


@dataclass
class RunState:
    """Lo que un driver comparte con quien cierra la ejecución: el id de
    sesión, para que `agent_failed` lo lleve si ya se conocía."""

    session_id: str | None = None


def parse_request(raw: object) -> Request:
    """`{"v":1,"prompt","session_id","model","reasoning"}` a `Request`; una
    forma distinta es `protocol_error`/`invalid_request`."""
    if not isinstance(raw, Mapping) or raw.get("v") != PROTOCOL_VERSION:
        raise AgentFailure("protocol_error", "invalid_request")
    prompt = raw.get("prompt")
    session_id = raw.get("session_id")
    model = raw.get("model")
    reasoning = raw.get("reasoning", False)
    valid = (
        isinstance(prompt, str)
        and (session_id is None or isinstance(session_id, str))
        and (model is None or isinstance(model, str))
        and isinstance(reasoning, bool)
    )
    if not valid:
        raise AgentFailure("protocol_error", "invalid_request")
    return Request(
        prompt=str(prompt),
        session_id=session_id or None,
        model=model or None,
        reasoning=reasoning,
    )


def new_id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex


def truncate_output(text: str) -> tuple[str, bool]:
    """Recorta `text` a `MAX_TOOL_OUTPUT_BYTES` bytes UTF-8 sin partir un
    carácter; el `bool` dice si se recortó."""
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_TOOL_OUTPUT_BYTES:
        return text, False
    return encoded[:MAX_TOOL_OUTPUT_BYTES].decode("utf-8", errors="ignore"), True


def count(value: object) -> int:
    """Un entero no negativo, o 0 (un `bool` no es un contador)."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def usage_dict(raw: object) -> dict[str, int]:
    source = raw if isinstance(raw, Mapping) else {}
    return {key: count(source.get(key)) for key in USAGE_KEYS}


def build_event(kind: str, **fields: object) -> Event:
    return {"v": PROTOCOL_VERSION, "type": kind, **fields}


def encode_line(event: Mapping[str, object]) -> str:
    return json.dumps(event, ensure_ascii=False, separators=(",", ":"))


class Emitter:
    """Escribe una línea JSON por evento en el descriptor del protocolo."""

    def __init__(self, stream: IO[str]) -> None:
        self._stream = stream
        self._lock = threading.Lock()

    def emit(self, kind: str, **fields: object) -> None:
        line = encode_line(build_event(kind, **fields))
        with self._lock:
            self._stream.write(line + "\n")
            self._stream.flush()


def open_protocol_stream() -> IO[str]:
    """Duplica el stdout original a un descriptor privado para el protocolo
    y apunta el descriptor 1 y `sys.stdout` a stderr, para que un `print` del
    código del usuario nunca rompa el protocolo."""
    protocol_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return os.fdopen(protocol_fd, "w", encoding="utf-8", buffering=1)


class EventEmitter(Protocol):
    """Quien recibe el evento terminal: el `Emitter` de stdio o el entregador
    de una invocación HTTP."""

    def emit(self, kind: str, **fields: object) -> None: ...


def run_to_terminal(
    emitter: EventEmitter,
    body: Callable[[RunState], None],
    classify: Callable[[BaseException], tuple[str, str]],
) -> None:
    """Corre `body` y cierra la ejecución: `done` si termina, `agent_failed`
    con `reason` y `detail_code` si lanza (`AgentFailure` tal cual, el resto
    por `classify`, sin el texto de la excepción)."""
    state = RunState()
    try:
        body(state)
    except AgentFailure as exc:
        emitter.emit(
            "agent_failed",
            reason=exc.reason,
            detail_code=exc.detail_code,
            session_id=state.session_id,
        )
        return
    except Exception as exc:
        reason, detail = classify(exc)
        emitter.emit("agent_failed", reason=reason, detail_code=detail, session_id=state.session_id)
        return
    emitter.emit("done", session_id=state.session_id)


def classify_by_module(exc: BaseException, model_modules: tuple[str, ...]) -> tuple[str, str]:
    """`model_error` si la excepción viene de una biblioteca de modelo o de
    la pasarela; cualquier otra es `runtime_error`. El detalle es el nombre
    de la clase, nunca el mensaje."""
    module = type(exc).__module__.split(".", 1)[0]
    reason = "model_error" if module in model_modules else "runtime_error"
    return reason, type(exc).__name__


class Driver(Protocol):
    """Lo que el host espera de un driver (`drivers/<nombre>.py` expone
    `create(config) -> Driver`). `run()` emite los eventos de progreso por
    `emit`, deja el id de sesión en `state` y lanza si falla: el host cierra
    la ejecución con `done` o `agent_failed`. `cancel()` mata lo que el
    driver haya lanzado (procesos hijos); `warm()` prepara lo caro antes de
    la primera invocación."""

    def warm(self) -> None: ...

    def run(self, request: Request, emit: Emit, state: RunState) -> None: ...

    def cancel(self) -> None: ...
