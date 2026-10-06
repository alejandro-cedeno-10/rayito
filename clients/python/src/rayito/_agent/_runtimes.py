"""Registro `nombre -> AgentRuntime` (`ai-agent-core`, design.md §3). Vacío
hasta que el adaptador de OpenCode (resto de `ai-agent-core`) registre
`"opencode"` y `ai-agent-deepagents` registre `"deepagents"`: hasta
entonces, `runtime="opencode"` falla con `UnimplementedError` en vez de con
un `KeyError` opaco. Un llamante siempre puede saltarse el registro pasando
su propio objeto `AgentRuntime` (lo que hacen los tests con un doble)."""

from __future__ import annotations

from rayito._agent._runtime import AgentRuntime
from rayito.exceptions import InvalidArgumentException, UnimplementedError

#: Quita y pon: cada adaptador añade su entrada aquí cuando aterriza, sin
#: tocar el resto de este módulo.
AGENT_RUNTIMES: dict[str, AgentRuntime] = {}


def resolve_runtime(runtime: str | AgentRuntime) -> AgentRuntime:
    """`runtime` ya es un `AgentRuntime` (duck typing con los métodos del
    puerto) o un nombre de `AGENT_RUNTIMES`. Un nombre desconocido es
    `UnimplementedError` (puede llegar con `ai-agent-core`/`-deepagents`
    más adelante); cualquier otro valor es `InvalidArgumentException`."""
    if isinstance(runtime, str):
        try:
            return AGENT_RUNTIMES[runtime]
        except KeyError:
            raise UnimplementedError(
                f"runtime={runtime!r}",
                "no hay ningún adaptador registrado con ese nombre",
                "https://rayito.dev/referencia/errores",
            ) from None
    if not _looks_like_runtime(runtime):
        raise InvalidArgumentException(
            "runtime debe ser un nombre registrado en AGENT_RUNTIMES o un objeto "
            "que implemente el Protocol AgentRuntime"
        )
    return runtime


def _looks_like_runtime(candidate: object) -> bool:
    required = ("build_config", "command", "new_state", "parse_line", "finish", "abort_command")
    return all(callable(getattr(candidate, attr, None)) for attr in required)
