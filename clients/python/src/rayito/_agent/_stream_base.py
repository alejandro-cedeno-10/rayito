"""Lógica de aplicación de `sbx.agent.run/stream/prepare`, compartida por
`sandbox_sync/agent.py` y `sandbox_async/agent.py` (`ai-agent-core`,
design.md §4). Lo único que difiere entre las dos cáscaras es cómo
consumen el `CommandHandle` (bloqueante vs `asyncio`); todo lo demás —
construir la petición, decidir si hace falta `files.write`, trocear stdout
en líneas, imponer `AgentLimits` sobre los eventos del runtime y montar el
`AgentResult` final — vive aquí.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any, Final, Protocol

from rayito._agent._domain import AgentLimits, AgentSpec
from rayito._agent._events import (
    AgentEvent,
    AgentFailed,
    Done,
    StepFinished,
    StepStarted,
    Text,
    TokenUsage,
    ToolCall,
)
from rayito._agent._runtime import AgentRuntime, RunRequest, RuntimeFiles, RuntimeState
from rayito._limits import MAX_AGENT_EVENT_LINE_BYTES
from rayito._otel import Instrumentation
from rayito.exceptions import InvalidArgumentException


class AgentSandbox(Protocol):
    """Lo que `Agent`/`AsyncAgent` necesitan de un `Sandbox`/`AsyncSandbox`:
    cualquier objeto con estos cuatro atributos sirve, que es exactamente lo
    que los tests le dan con `FakeSandbox` (`fake_agent.py`). `commands` y
    `files` quedan en `Any` porque sus firmas difieren entre la cáscara
    síncrona y la asíncrona (una devuelve directo, la otra una corrutina)."""

    @property
    def commands(self) -> Any: ...

    @property
    def files(self) -> Any: ...

    @property
    def gateways(self) -> Mapping[str, Any]: ...

    @property
    def _instrumentation(self) -> Instrumentation: ...


#: Tag que `commands.run(tag=...)` lleva en cada ejecución del agente: útil
#: para filtrar en `commands.list()` o en logs de `rayd`.
AGENT_RUN_TAG: Final = "rayito-agent-run"
#: Los `reason` con los que el SDK, no el runtime, corta una ejecución
#: (`LimitTracker`): el runtime sigue vivo y hay que pararlo.
SDK_LIMIT_REASONS: Final = frozenset({"max_steps", "token_budget"})
#: Plazo de la orden de `stop_tree_command`.
AGENT_STOP_TREE_TIMEOUT_SECONDS: Final = 30


def stop_tree_command(pid: int) -> str:
    """Congela (`SIGSTOP`) el proceso del runtime y, de padres a hijos, cada
    descendiente suyo, y luego mata (`SIGKILL`) a los descendientes. Hace
    falta porque OpenCode y deepagents lanzan cada orden de su herramienta
    de shell en una sesión propia (`setsid`): el `SIGKILL` de `rayd` al
    grupo del proceso no las alcanza y, sin esto, un `sleep` o un servidor
    lanzado por el agente sobreviviría a `abort()` y a los límites del SDK
    (medido con `make local-e2e`, `test_local_agents.py`). El propio
    runtime queda congelado para el `kill()` del handle. Corre como el
    mismo `user` que el agente, así que no puede tocar procesos ajenos."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        raise InvalidArgumentException("pid de agente no válido")
    return (
        't() { local c; for c in $(pgrep -P "$1"); do '
        'kill -STOP "$c" 2>/dev/null; t "$c"; kill -KILL "$c" 2>/dev/null; done; }; '
        f"kill -STOP {pid} 2>/dev/null; t {pid}; true"
    )


def is_sdk_limit_failure(event: AgentEvent) -> bool:
    """Si `event` es el `AgentFailed` con el que el SDK corta una ejecución
    que el runtime aún no terminó."""
    return isinstance(event, AgentFailed) and event.reason in SDK_LIMIT_REASONS


def gateway_urls_for(spec: AgentSpec, gateways: Mapping[str, object]) -> dict[str, str]:
    """Las URLs de las pasarelas que `spec` necesita (`spec.gateway_names()`),
    leídas de `sbx.gateways` (un `GatewayStatus.url` por nombre). Falla con
    `InvalidArgumentException` **antes de cualquier RPC** si falta alguna
    (`AgentSpec.require_gateways`)."""
    spec.require_gateways(gateways.keys())
    return {name: gateways[name].url for name in spec.gateway_names()}  # type: ignore[attr-defined]


class LineBuffer:
    """Trocea el stdout del comando del agente en líneas completas. Una línea
    más larga que `MAX_AGENT_EVENT_LINE_BYTES` (el protocolo de OpenCode
    manda el `output` de una `tool_use` dentro de la línea) se descarta
    entera y cuenta en `dropped`, en vez de partirse o de crecer sin límite."""

    __slots__ = ("_buffer", "dropped")

    def __init__(self) -> None:
        self._buffer = ""
        self.dropped = 0

    def feed(self, text: str) -> list[bytes]:
        if not text:
            return []
        self._buffer += text
        lines: list[bytes] = []
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            encoded = line.encode("utf-8")
            if len(encoded) > MAX_AGENT_EVENT_LINE_BYTES:
                self.dropped += 1
                continue
            lines.append(encoded)
        return lines


class LimitTracker:
    """Impone `AgentLimits` sobre la secuencia de eventos que un adaptador
    produce, y acumula lo que necesita el `AgentResult` final: el `text` del
    último `Text`, los `ToolCall` vistos y los tokens por paso.

    `max_steps` se comprueba al empezar el paso `max_steps + 1`
    (`StepStarted`); `max_total_tokens` se comprueba tras cada
    `StepFinished`, así que puede pasarse en un único paso (design.md,
    nota de §3: límite blando de OpenCode, duro del SDK)."""

    def __init__(self, limits: AgentLimits, *, session_id: str | None = None) -> None:
        self._limits = limits
        self.session_id = session_id
        self.steps = 0
        self.usage = TokenUsage()
        self.last_text = ""
        self.tool_calls: list[ToolCall] = []

    def track(self, event: AgentEvent) -> AgentEvent:
        """Devuelve `event` tal cual, o un `AgentFailed` si rebasa un
        límite del SDK; nunca modifica eventos previos ya entregados."""
        if isinstance(event, StepStarted):
            self.steps = event.index
            if self.steps > self._limits.max_steps:
                return self._failed("max_steps")
        elif isinstance(event, StepFinished):
            self.usage = self.usage + event.usage
            if (
                self._limits.max_total_tokens is not None
                and self.usage.total > self._limits.max_total_tokens
            ):
                return self._failed("token_budget")
        elif isinstance(event, Text):
            self.last_text = event.text
        elif isinstance(event, ToolCall):
            self.tool_calls.append(event)
        elif isinstance(event, Done):
            self.session_id = event.session_id
        elif isinstance(event, AgentFailed) and event.session_id is None and self.session_id:
            return dataclasses.replace(event, session_id=self.session_id)
        return event

    def _failed(self, reason: str) -> AgentFailed:
        return AgentFailed(reason=reason, session_id=self.session_id)  # type: ignore[arg-type]


def require_prompt(prompt: object) -> str:
    if not isinstance(prompt, str) or not prompt:
        raise InvalidArgumentException(
            "el prompt de sbx.agent.run()/stream() debe ser una cadena no vacía"
        )
    return prompt


class ConfigCache:
    """Guarda el sha256 de la configuración del runtime ya escrita en el
    sandbox, por runtime (`AgentRuntime.name`). Vive en la instancia de
    `Agent`, así que sobrevive entre llamadas del mismo handle; una
    reconexión (`Sandbox.connect`) empieza con la caché vacía y
    `files.write` corre de nuevo una vez."""

    def __init__(self) -> None:
        self._applied: dict[str, str] = {}

    def needs_write(self, runtime_name: str, config: RuntimeFiles) -> bool:
        return self._applied.get(runtime_name) != config.config_sha256

    def mark_applied(self, runtime_name: str, config: RuntimeFiles) -> None:
        self._applied[runtime_name] = config.config_sha256


def build_run_request(
    *,
    spec: AgentSpec,
    prompt: str,
    workdir: str,
    session_id: str | None,
    model: str | None,
    reasoning: bool,
) -> RunRequest:
    return RunRequest(
        spec=spec,
        prompt=require_prompt(prompt),
        workdir=workdir,
        session_id=session_id,
        model=model,
        reasoning=reasoning,
    )


__all__ = [
    "AGENT_RUN_TAG",
    "AgentRuntime",
    "AgentSandbox",
    "ConfigCache",
    "LimitTracker",
    "LineBuffer",
    "RuntimeState",
    "build_run_request",
    "gateway_urls_for",
    "require_prompt",
]
