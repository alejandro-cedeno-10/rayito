"""Decodificador del protocolo JSONL v1 de Rayito (ADR-025, ADR-028): el
formato en que el host del agente y el runner de deepagents escriben los
eventos de una ejecución (`session`, `text_delta`, `text`, `reasoning`,
`tool_call`, `step_started`, `step_finished`, `agent_failed`, `done`, más
las líneas `rayito.busy` y `rayito.runtime_missing` del script de
ejecución). Puro: una línea de bytes entra y salen eventos del dominio.

Lo comparten todos los adaptadores que hablan v1 (deepagents, Strands,
Hermes, `CustomRuntime`), de modo que hay un solo decodificador y los
vectores `testdata/agent/rayito-protocol-v1*.json[l]` lo prueban una vez.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from rayito._agent._events import (
    AGENT_FAILURE_REASONS,
    AgentEvent,
    AgentFailed,
    Done,
    Reasoning,
    StepFinished,
    StepStarted,
    Text,
    TextDelta,
    TokenUsage,
    ToolCall,
    truncate_tool_output,
)
from rayito._limits import AGENT_PROTOCOL_VERSION

#: Tipos del protocolo v1 que el decodificador acepta.
PROTOCOL_EVENT_TYPES: Final[tuple[str, ...]] = (
    "session",
    "text_delta",
    "text",
    "reasoning",
    "tool_call",
    "step_started",
    "step_finished",
    "agent_failed",
    "done",
)
#: Fallos que un runner o un host pueden declarar; el resto (`timeout`,
#: `max_steps`…) sólo los decide el SDK.
RUNNER_FAILURE_REASONS: Final[tuple[str, ...]] = (
    "model_error",
    "runtime_error",
    "protocol_error",
)
#: Líneas del script de ejecución (no del runner) que el decodificador
#: convierte en un fallo.
SCRIPT_FAILURE_TYPES: Final[tuple[str, ...]] = ("rayito.busy", "rayito.runtime_missing")
#: El identificador de sesión que emite el runner: `rda_` y 32 hex.
SESSION_ID_PATTERN: Final = re.compile(r"rda_[0-9a-f]{32}")
_USAGE_KEYS: Final = ("input", "output", "reasoning", "cache_read", "cache_write")


@dataclass
class ProtocolV1State:
    """Lo que el decodificador acumula de una ejecución."""

    session_id: str | None = None
    steps: int = 0
    usage: TokenUsage = field(default_factory=TokenUsage)
    done: bool = False
    failed: AgentFailed | None = None
    ignored_lines: int = 0


def parse_v1_line(line: bytes, state: ProtocolV1State) -> Sequence[AgentEvent]:
    """Una línea del protocolo v1 a cero o más eventos. Lo que no es JSON,
    otra versión o un tipo desconocido se ignora y se cuenta."""
    try:
        payload = json.loads(line)
    except ValueError:
        state.ignored_lines += 1
        return ()
    if not isinstance(payload, Mapping) or payload.get("v") != AGENT_PROTOCOL_VERSION:
        state.ignored_lines += 1
        return ()
    events = _map(payload, state)
    if events is None:
        state.ignored_lines += 1
        return ()
    return events


def finish_v1(state: ProtocolV1State, exit_code: int) -> Done | AgentFailed:
    """`Done` sólo con salida 0, el `done` del emisor y una sesión."""
    if state.failed is not None:
        return state.failed
    if exit_code != 0:
        return AgentFailed(reason="runtime_error", exit_code=exit_code, session_id=state.session_id)
    if not state.done or state.session_id is None:
        return AgentFailed(
            reason="protocol_error", exit_code=exit_code, session_id=state.session_id
        )
    return Done(session_id=state.session_id, exit_code=exit_code, usage=state.usage)


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _usage(raw: object) -> TokenUsage:
    if not isinstance(raw, Mapping):
        return TokenUsage()
    return TokenUsage(**{key: _int(raw.get(key)) for key in _USAGE_KEYS})


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _map(payload: Mapping[str, object], state: ProtocolV1State) -> list[AgentEvent] | None:
    kind = payload.get("type")
    if kind in SCRIPT_FAILURE_TYPES:
        state.failed = AgentFailed(reason=str(kind).removeprefix("rayito."))  # type: ignore[arg-type]
        return [state.failed]
    if kind not in PROTOCOL_EVENT_TYPES:
        return None
    if kind == "session":
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not SESSION_ID_PATTERN.fullmatch(session_id):
            return None
        state.session_id = session_id
        return []
    if kind == "step_started":
        state.steps += 1
        return [StepStarted(index=state.steps)]
    if kind == "step_finished":
        usage = _usage(payload.get("usage"))
        state.usage = state.usage + usage
        return [
            StepFinished(
                index=state.steps,
                usage=usage,
                finish_reason=_optional_str(payload.get("finish_reason")),
            )
        ]
    if kind in ("text_delta", "text", "reasoning"):
        return _text_event(str(kind), payload)
    if kind == "tool_call":
        return _tool_call(payload)
    if kind == "agent_failed":
        return [_failure(payload, state)]
    state.done = True
    return []


def _text_event(kind: str, payload: Mapping[str, object]) -> list[AgentEvent] | None:
    text = payload.get("text")
    if not isinstance(text, str):
        return None
    if kind == "text_delta":
        return [TextDelta(text=text)]
    return [Text(text=text) if kind == "text" else Reasoning(text=text)]


def _failure(payload: Mapping[str, object], state: ProtocolV1State) -> AgentFailed:
    reason = payload.get("reason")
    if reason not in RUNNER_FAILURE_REASONS or reason not in AGENT_FAILURE_REASONS:
        reason = "protocol_error"
    state.failed = AgentFailed(
        reason=reason,  # type: ignore[arg-type]
        detail_code=_optional_str(payload.get("detail_code")),
        session_id=state.session_id,
    )
    return state.failed


def _tool_call(payload: Mapping[str, object]) -> list[AgentEvent] | None:
    status = payload.get("status")
    if status not in ("completed", "error"):
        return None
    raw_input = payload.get("input")
    raw_output = payload.get("output")
    output, truncated = (None, False)
    if isinstance(raw_output, str):
        output, truncated = truncate_tool_output(raw_output)
    return [
        ToolCall(
            call_id=str(payload.get("call_id", "")),
            name=str(payload.get("name", "")),
            status="completed" if status == "completed" else "error",
            input=dict(raw_input) if isinstance(raw_input, Mapping) else None,
            output=output,
            output_truncated=truncated or payload.get("output_truncated") is True,
        )
    ]


__all__ = [
    "PROTOCOL_EVENT_TYPES",
    "RUNNER_FAILURE_REASONS",
    "SCRIPT_FAILURE_TYPES",
    "SESSION_ID_PATTERN",
    "ProtocolV1State",
    "finish_v1",
    "parse_v1_line",
]
