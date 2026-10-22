"""Topes de una ejecución en modo servicio (`agent_host`, ADR-028, D3). Tiene
la misma semántica que el `LimitTracker` del SDK
(`rayito._agent._stream_base`): `max_steps` se comprueba al empezar el paso
`max_steps + 1`; `max_total_tokens` tras cada `step_finished`, así que puede
pasarse en un único paso; `max_output_bytes` cuenta los bytes de los eventos
ya entregados. Los tres comparten los vectores de
`testdata/agent/limit-vectors.json` con las pruebas de los dos SDK.

El plazo (`timeout_seconds`) no es un evento: lo vigila `serve.py` con un
reloj, y aquí sólo se guarda para dejarlo junto a los otros topes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from .protocol import USAGE_KEYS, Event, count, encode_line

#: Valores por defecto de `AgentLimits` en el SDK
#: (`DEFAULT_AGENT_MAX_STEPS`, `DEFAULT_AGENT_TIMEOUT_SECONDS`,
#: `DEFAULT_AGENT_MAX_OUTPUT_BYTES` y `DEFAULT_AGENT_MAX_TOTAL_TOKENS` de
#: `limits.json`); el host los usa cuando la configuración no trae `limits`.
DEFAULT_MAX_STEPS: Final = 50
DEFAULT_TIMEOUT_SECONDS: Final = 600.0
DEFAULT_MAX_OUTPUT_BYTES: Final = 16_777_216
DEFAULT_MAX_TOTAL_TOKENS: Final[int | None] = 1_000_000
#: Tope de la salida estructurada (`output`) de una ejecución, ya en JSON
#: (`AGENT_SERVICE_MAX_OUTPUT_BYTES` de `limits.json`): una salida mayor es
#: `protocol_error`/`output_not_serializable`.
MAX_SERVICE_OUTPUT_BYTES: Final = 1_048_576


def output_fits(encoded: str) -> bool:
    """Si la salida estructurada ya codificada cabe en
    `MAX_SERVICE_OUTPUT_BYTES` bytes UTF-8."""
    return len(encoded.encode("utf-8")) <= MAX_SERVICE_OUTPUT_BYTES


@dataclass(frozen=True)
class Limits:
    """Los topes de `AgentLimits` que el host impone."""

    max_steps: int = DEFAULT_MAX_STEPS
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    max_total_tokens: int | None = DEFAULT_MAX_TOTAL_TOKENS

    @classmethod
    def from_config(cls, raw: object) -> Limits:
        """`{"max_steps","timeout_seconds","max_output_bytes","max_total_tokens"}`;
        una clave ausente usa el valor por defecto y `null` en
        `max_total_tokens` lo desactiva."""
        if not isinstance(raw, Mapping):
            return cls()
        defaults = cls()
        tokens = raw.get("max_total_tokens", defaults.max_total_tokens)
        ceiling = None if tokens is None else _positive(tokens, defaults.max_total_tokens)
        return cls(
            max_steps=_positive(raw.get("max_steps"), defaults.max_steps),
            timeout_seconds=float(_positive(raw.get("timeout_seconds"), defaults.timeout_seconds)),
            max_output_bytes=_positive(raw.get("max_output_bytes"), defaults.max_output_bytes),
            max_total_tokens=ceiling,
        )


def _positive(value: Any, default: Any) -> Any:
    valid = isinstance(value, int | float) and not isinstance(value, bool) and value > 0
    return value if valid else default


@dataclass
class LimitTracker:
    """Observa los eventos que se entregan y dice qué tope rebasan.
    `observe()` devuelve el `reason` del fallo o `None`; los eventos
    posteriores a un fallo ya no se cuentan."""

    limits: Limits
    steps: int = 0
    output_bytes: int = 0
    last_text: str = ""
    deltas: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=lambda: dict.fromkeys(USAGE_KEYS, 0))
    usage_reported: bool = False
    tool_calls: int = 0

    @property
    def total_tokens(self) -> int:
        return sum(self.usage.values())

    def observe(self, event: Event) -> str | None:
        kind = event.get("type")
        if kind == "step_started":
            self.steps += 1
            if self.steps > self.limits.max_steps:
                return "max_steps"
        elif kind == "step_finished":
            self._add_usage(event.get("usage"))
            ceiling = self.limits.max_total_tokens
            if ceiling is not None and self.total_tokens > ceiling:
                return "token_budget"
        elif kind == "text":
            self.last_text = str(event.get("text", ""))
        elif kind == "text_delta":
            self.deltas.append(str(event.get("text", "")))
        elif kind == "tool_call":
            self.tool_calls += 1
        self.output_bytes += len(encode_line(event).encode("utf-8")) + 1
        if self.output_bytes > self.limits.max_output_bytes:
            return "output_limit"
        return None

    def _add_usage(self, raw: object) -> None:
        self.usage_reported = self.usage_reported or isinstance(raw, Mapping)
        source = raw if isinstance(raw, Mapping) else {}
        for key in USAGE_KEYS:
            self.usage[key] += count(source.get(key))

    @property
    def text(self) -> str:
        """El último `text` del asistente o, si el runtime sólo mandó
        deltas, su concatenación."""
        return self.last_text or "".join(self.deltas)
