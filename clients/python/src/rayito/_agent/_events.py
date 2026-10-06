"""Eventos y resultado de una ejecución del agente de IA (`ai-agent-core`,
ADR-025). Puros: el adaptador de cada runtime traduce sus líneas (JSONL de
OpenCode, protocolo Rayito de deepagents) a estos tipos, y `type` es el
discriminador que comparten Python, TypeScript y el runner de deepagents.

Ningún evento lleva el texto de un error del proveedor: `AgentFailed`
sólo lleva un `reason` de una lista cerrada y, como mucho, un
`detail_code` corto (`APIError`), y su mensaje sale de una tabla fija.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final, Literal, TypeAlias, get_args

from rayito._limits import MAX_TOOL_OUTPUT_PREVIEW_BYTES
from rayito.exceptions import AgentException, InvalidArgumentException

AgentFailureReason: TypeAlias = Literal[
    "model_error",
    "runtime_error",
    "runtime_missing",
    "runtime_version_mismatch",
    "protocol_error",
    "timeout",
    "max_steps",
    "token_budget",
    "output_limit",
    "aborted",
    "busy",
]
ToolCallStatus: TypeAlias = Literal["completed", "error"]

#: La lista cerrada de `AgentFailed.reason` / `AgentException.reason`.
AGENT_FAILURE_REASONS: Final[tuple[str, ...]] = get_args(AgentFailureReason)
#: El mensaje de cada `reason`: una tabla fija en español, nunca el texto
#: del proveedor, el prompt ni contenido del sandbox (misma tabla en
#: `agent/events.ts`; los dos SDKs la comprueban contra
#: `testdata/agent/domain-vectors.json`).
AGENT_FAILURE_MESSAGES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "model_error": "el modelo devolvió un error",
        "runtime_error": "el runtime del agente terminó con un error",
        "runtime_missing": (
            "el runtime del agente no está en la imagen o su servidor residente no responde"
        ),
        "runtime_version_mismatch": "la versión del runtime de la imagen no es la pedida",
        "protocol_error": "el runtime del agente emitió una salida fuera de protocolo",
        "timeout": "el agente superó su tiempo máximo",
        "max_steps": "el agente superó su número máximo de pasos",
        "token_budget": "el agente superó su presupuesto de tokens",
        "output_limit": "el agente superó su máximo de bytes de salida",
        "aborted": "la ejecución del agente se abortó",
        "busy": "ya hay otra ejecución del agente en curso en este sandbox",
    }
)
#: Un `detail_code` es un nombre de clase de error (`APIError`,
#: `ProviderAuthError`), nunca un mensaje: lo que no case con esto se
#: descarta en vez de copiarse a un mensaje o a un span.
_DETAIL_CODE_PATTERN: Final = re.compile(r"[A-Za-z0-9_.-]{1,64}")
#: Nombres de los discriminadores, en el orden del protocolo.
AGENT_EVENT_TYPES: Final[tuple[str, ...]] = (
    "text_delta",
    "text",
    "reasoning",
    "tool_call",
    "step_started",
    "step_finished",
    "agent_failed",
    "done",
)


def _non_negative(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidArgumentException(f"{label} debe ser un entero >= 0")
    return value


def safe_detail_code(detail_code: object) -> str | None:
    """`detail_code` si es un identificador corto; `None` si no lo es."""
    if isinstance(detail_code, str) and _DETAIL_CODE_PATTERN.fullmatch(detail_code):
        return detail_code
    return None


def failure_message(reason: str, detail_code: str | None = None) -> str:
    """El mensaje fijo de `reason`, con `detail_code` entre paréntesis si es
    un identificador seguro."""
    if reason not in AGENT_FAILURE_MESSAGES:
        raise InvalidArgumentException(
            f"reason de fallo del agente desconocido; uno de {list(AGENT_FAILURE_REASONS)}"
        )
    base = AGENT_FAILURE_MESSAGES[reason]
    code = safe_detail_code(detail_code)
    return base if code is None else f"{base} ({code})"


def truncate_tool_output(output: str) -> tuple[str, bool]:
    """Recorta `output` a `MAX_TOOL_OUTPUT_PREVIEW_BYTES` bytes UTF-8 sin
    partir un carácter; el `bool` dice si se recortó."""
    encoded = output.encode("utf-8")
    if len(encoded) <= MAX_TOOL_OUTPUT_PREVIEW_BYTES:
        return output, False
    return encoded[:MAX_TOOL_OUTPUT_PREVIEW_BYTES].decode("utf-8", errors="ignore"), True


@dataclass(frozen=True)
class TokenUsage:
    """Tokens de un paso o de una ejecución. `input` excluye los leídos de
    caché (`cache_read`) y los escritos en ella (`cache_write`), como
    Bedrock y Anthropic los facturan por separado; `total` suma los cinco.
    El SDK no da un coste: los precios dependen de región y perfil."""

    input: int = 0
    output: int = 0
    reasoning: int = 0
    cache_read: int = 0
    cache_write: int = 0

    def __post_init__(self) -> None:
        for name in ("input", "output", "reasoning", "cache_read", "cache_write"):
            _non_negative(getattr(self, name), f"TokenUsage.{name}")

    @property
    def total(self) -> int:
        return self.input + self.output + self.reasoning + self.cache_read + self.cache_write

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input=self.input + other.input,
            output=self.output + other.output,
            reasoning=self.reasoning + other.reasoning,
            cache_read=self.cache_read + other.cache_read,
            cache_write=self.cache_write + other.cache_write,
        )


@dataclass(frozen=True)
class TextDelta:
    """Un trozo de texto en curso (sólo deepagents; OpenCode emite `Text`
    entero)."""

    text: str
    type: Literal["text_delta"] = field(default="text_delta", init=False)


@dataclass(frozen=True)
class Text:
    """Un bloque de texto completo del asistente."""

    text: str
    type: Literal["text"] = field(default="text", init=False)


@dataclass(frozen=True)
class Reasoning:
    """Razonamiento del modelo; sólo con `run(..., reasoning=True)`."""

    text: str
    type: Literal["reasoning"] = field(default="reasoning", init=False)


@dataclass(frozen=True)
class ToolCall:
    """Una herramienta terminada. `output` llega recortado a
    `MAX_TOOL_OUTPUT_PREVIEW_BYTES` (`output_truncated`)."""

    call_id: str
    name: str
    status: ToolCallStatus
    input: Mapping[str, object] | None = None
    output: str | None = None
    output_truncated: bool = False
    type: Literal["tool_call"] = field(default="tool_call", init=False)

    def __post_init__(self) -> None:
        if self.status not in get_args(ToolCallStatus):
            raise InvalidArgumentException("ToolCall.status debe ser 'completed' o 'error'")


@dataclass(frozen=True)
class StepStarted:
    """Empieza el paso `index` (desde 1): una llamada al modelo."""

    index: int
    type: Literal["step_started"] = field(default="step_started", init=False)


@dataclass(frozen=True)
class StepFinished:
    """Termina el paso `index` con sus tokens."""

    index: int
    usage: TokenUsage
    finish_reason: str | None = None
    type: Literal["step_finished"] = field(default="step_finished", init=False)


@dataclass(frozen=True)
class AgentFailed:
    """Último evento de una ejecución fallida. `reason` es de
    `AGENT_FAILURE_REASONS`; `detail_code` se descarta si no es un
    identificador corto."""

    reason: AgentFailureReason
    exit_code: int | None = None
    detail_code: str | None = None
    session_id: str | None = None
    type: Literal["agent_failed"] = field(default="agent_failed", init=False)

    def __post_init__(self) -> None:
        if self.reason not in AGENT_FAILURE_REASONS:
            raise InvalidArgumentException(
                f"AgentFailed.reason debe ser uno de {list(AGENT_FAILURE_REASONS)}"
            )
        object.__setattr__(self, "detail_code", safe_detail_code(self.detail_code))

    @property
    def message(self) -> str:
        return failure_message(self.reason, self.detail_code)

    def to_exception(self, usage: TokenUsage | None = None) -> AgentException:
        """La `AgentException` que `sbx.agent.run()` lanza por este evento."""
        return AgentException(
            self.message,
            reason=self.reason,
            session_id=self.session_id,
            usage=usage,
            exit_code=self.exit_code,
            detail_code=self.detail_code,
        )


@dataclass(frozen=True)
class Done:
    """Último evento de una ejecución correcta, con los tokens acumulados."""

    session_id: str
    exit_code: int
    usage: TokenUsage
    type: Literal["done"] = field(default="done", init=False)


AgentEvent: TypeAlias = (
    TextDelta | Text | Reasoning | ToolCall | StepStarted | StepFinished | AgentFailed | Done
)


@dataclass(frozen=True)
class AgentResult:
    """El resultado de `sbx.agent.run()`. `text` es el último `Text` del
    asistente; `dropped_lines` cuenta las líneas del runtime descartadas
    (tipo desconocido o más largas que `MAX_AGENT_EVENT_LINE_BYTES`)."""

    session_id: str
    text: str
    steps: int
    usage: TokenUsage
    exit_code: int
    tool_calls: tuple[ToolCall, ...] = ()
    dropped_lines: int = 0
