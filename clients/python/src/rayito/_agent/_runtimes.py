"""Registro `nombre -> AgentRuntime` (`ai-agent-core`, design.md §3 y
ADR-028): `"opencode"`, `"deepagents"` y `"langgraph"` (el mismo adaptador
de deepagents, con `entrypoint` obligatorio), `"strands"`,
`"strands-harness"` y `"hermes"`; `"custom"` no es un nombre del registro
porque `CustomRuntime` necesita configuración. Un nombre sin
adaptador falla con `UnimplementedError` en vez de con un `KeyError`
opaco. Un llamante siempre puede saltarse el registro pasando su propio
objeto `AgentRuntime` (lo que hacen los tests con un doble).

`runtime_from_options` y `runtime_to_options` traducen un runtime a y desde
la tabla `[runtime]` de `rayito-agent.toml` (`_project_file.py`)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Final

from rayito._agent._custom import CustomRuntime
from rayito._agent._deepagents import DeepAgents
from rayito._agent._hermes import HermesRuntime
from rayito._agent._opencode import OpenCodeRuntime
from rayito._agent._runtime import AgentRuntime
from rayito._agent._strands import STRANDS_HARNESS_NAME, STRANDS_NAME, StrandsRuntime
from rayito.exceptions import InvalidArgumentException, UnimplementedError

#: Quita y pon: cada adaptador añade su entrada aquí cuando aterriza, sin
#: tocar el resto de este módulo.
AGENT_RUNTIMES: dict[str, AgentRuntime] = {
    "opencode": OpenCodeRuntime(),
    "deepagents": DeepAgents(),
    "langgraph": DeepAgents(require_entrypoint=True),
    STRANDS_NAME: StrandsRuntime(),
    STRANDS_HARNESS_NAME: StrandsRuntime(harness=True),
    "hermes": HermesRuntime(),
}
#: Valores de `[runtime] kind`, en el orden de la documentación.
RUNTIME_KINDS: Final[tuple[str, ...]] = (
    "opencode",
    "deepagents",
    "langgraph",
    "strands",
    "strands-harness",
    "hermes",
    "custom",
)
#: El nombre que pide un objeto de configuración en vez de un nombre.
CUSTOM_RUNTIME_KIND: Final = "custom"
#: Las claves de `[runtime]` (además de `kind`) que admite cada `kind`.
RUNTIME_OPTION_KEYS: Final[Mapping[str, frozenset[str]]] = {
    "opencode": frozenset(),
    "deepagents": frozenset(),
    "langgraph": frozenset({"entrypoint"}),
    "strands": frozenset({"entrypoint", "language", "requirements"}),
    "strands-harness": frozenset({"entrypoint", "language", "requirements"}),
    "hermes": frozenset({"toolsets", "skills_dir"}),
    "custom": frozenset(
        {"install", "requirements", "command", "entrypoint", "app", "protocol", "python"}
    ),
}


def resolve_runtime(runtime: str | AgentRuntime) -> AgentRuntime:
    """`runtime` ya es un `AgentRuntime` (duck typing con los métodos del
    puerto) o un nombre de `AGENT_RUNTIMES`. Un nombre desconocido es
    `UnimplementedError` (puede llegar con un adaptador más adelante);
    `"custom"` y cualquier otro valor son `InvalidArgumentException`."""
    if isinstance(runtime, str):
        if runtime == CUSTOM_RUNTIME_KIND:
            raise InvalidArgumentException(
                "runtime='custom' no es un nombre del registro: pasa un objeto CustomRuntime(...)"
            )
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
    required = ("build_config", "command", "new_state", "parse_line", "finish", "warmup_steps")
    return all(callable(getattr(candidate, attr, None)) for attr in required)


def _langgraph(options: Mapping[str, object]) -> AgentRuntime:
    entrypoint = options.get("entrypoint")
    if entrypoint is None:
        raise InvalidArgumentException("entrypoint es obligatorio con kind='langgraph'")
    if not isinstance(entrypoint, str):
        raise InvalidArgumentException("entrypoint debe ser una cadena")
    return DeepAgents(entrypoint, require_entrypoint=True)


#: `kind` -> constructor desde la tabla `[runtime]`. Un `kind` sin entrada
#: se resuelve por el registro (y falla con `UnimplementedError` si aún no
#: tiene adaptador). Cada adaptador nuevo añade la suya.
_FROM_OPTIONS: dict[str, Callable[[Mapping[str, object]], AgentRuntime]] = {
    "langgraph": _langgraph,
    STRANDS_NAME: lambda options: StrandsRuntime.from_options(STRANDS_NAME, options),
    STRANDS_HARNESS_NAME: lambda options: StrandsRuntime.from_options(
        STRANDS_HARNESS_NAME, options
    ),
    "hermes": HermesRuntime.from_options,
    CUSTOM_RUNTIME_KIND: CustomRuntime.from_options,
}


def runtime_from_options(kind: str, options: Mapping[str, object]) -> AgentRuntime:
    """El runtime de `[runtime] kind = ...` con sus `options` (ya validadas
    contra `RUNTIME_OPTION_KEYS`)."""
    builder = _FROM_OPTIONS.get(kind)
    if builder is not None:
        return builder(options)
    return resolve_runtime(kind)


def runtime_to_options(runtime: str | AgentRuntime) -> dict[str, object]:
    """La tabla `[runtime]` canónica de `runtime`: `{"kind": ...}` más sus
    opciones. Un objeto que Rayito no conoce no se puede escribir en un
    fichero y falla con `InvalidArgumentException`."""
    if isinstance(runtime, str):
        return {"kind": runtime}
    if isinstance(runtime, OpenCodeRuntime):
        return {"kind": "opencode"}
    if isinstance(runtime, DeepAgents):
        if runtime.entrypoint is None:
            return {"kind": "deepagents"}
        return {"kind": "langgraph", "entrypoint": runtime.entrypoint}
    if isinstance(runtime, (StrandsRuntime, HermesRuntime, CustomRuntime)):
        return runtime.to_options()
    raise InvalidArgumentException(
        "este runtime no tiene forma de fichero: sólo los de Rayito se escriben en "
        "rayito-agent.toml"
    )


__all__ = [
    "AGENT_RUNTIMES",
    "CUSTOM_RUNTIME_KIND",
    "RUNTIME_KINDS",
    "RUNTIME_OPTION_KEYS",
    "resolve_runtime",
    "runtime_from_options",
    "runtime_to_options",
]
