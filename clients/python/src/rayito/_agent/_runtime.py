"""Puerto `AgentRuntime` (`ai-agent-core`, ADR-025, design.md §3): lo que un
adaptador de runtime (OpenCode, deepagents) traduce de un `AgentSpec` a
ficheros de configuración, a un comando y de vuelta a eventos. Puro: sin
`grpc`, `boto3` ni reloj. `_stream_base.py` es quien de verdad llama a estos
métodos contra el handle de comandos del sandbox.

Un adaptador concreto (`_opencode.py`) llega con el resto de
`ai-agent-core`; mientras tanto, un llamante puede pasar su propio objeto
que cumpla `AgentRuntime` como `runtime=` (lo que hacen los tests con un
runtime de doble), y `_runtimes.py` no necesita tener ninguno registrado
por nombre para que `sbx.agent.run()` funcione."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from rayito._agent._domain import AgentSpec
from rayito._agent._events import AgentEvent, AgentFailed, Done
from rayito._limits import DEFAULT_WARMUP_STEP_TIMEOUT_SECONDS


@dataclass(frozen=True)
class RuntimeFile:
    """Un fichero de configuración del runtime: ruta absoluta dentro del
    sandbox, contenido y permisos."""

    path: str
    data: bytes
    mode: int = 0o644


@dataclass(frozen=True)
class RuntimeFiles:
    """Lo que `build_config()` devuelve: los ficheros a escribir y un sha256
    de su contenido conjunto, para que el servicio de aplicación se salte
    `files.write` cuando ya está aplicado (mismo sha que la última vez)."""

    files: tuple[RuntimeFile, ...]
    config_sha256: str


@dataclass(frozen=True)
class RunRequest:
    """Lo que `command()` necesita para construir el script y los `envs`
    de una ejecución."""

    spec: AgentSpec
    prompt: str
    workdir: str
    session_id: str | None = None
    model: str | None = None
    reasoning: bool = False
    attach: bool | str = "auto"


@dataclass(frozen=True)
class RunCommand:
    """El comando que ejecuta la petición: texto de script de bash (nunca un
    secreto), sus variables de entorno y el stdin a mandarle (el prompt)."""

    script: str
    envs: Mapping[str, str] = field(default_factory=dict)
    stdin: bytes = b""


@dataclass(frozen=True)
class TemplateStep:
    """Un paso DSL que `AgentTemplate` (B2) añade a la plantilla."""

    op: str
    args: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class WarmupStep:
    """Un paso de calentamiento: `agent.prepare()` y el `warmup=` de un pool
    (B2) corren la misma lista. `background=True` no espera a que termine
    (un servidor residente); si no, se espera su salida con `timeout_seconds`."""

    cmd: str
    background: bool = False
    timeout_seconds: float = DEFAULT_WARMUP_STEP_TIMEOUT_SECONDS
    tag: str | None = None


@runtime_checkable
class RuntimeState(Protocol):
    """Estado mutable que un adaptador acumula entre llamadas a
    `parse_line()`; opaco para el servicio de aplicación, que sólo lo crea
    (`new_state()`) y lo pasa de vuelta."""


@runtime_checkable
class AgentRuntime(Protocol):
    """Lo que un adaptador de runtime implementa. `name` identifica el
    adaptador en los eventos de telemetría (`gen_ai.agent.name`)."""

    name: str

    def build_config(
        self, spec: AgentSpec, *, gateway_urls: Mapping[str, str], workdir: str
    ) -> RuntimeFiles: ...

    def command(self, request: RunRequest) -> RunCommand: ...

    def new_state(self) -> RuntimeState: ...

    def parse_line(self, line: bytes, state: RuntimeState) -> Sequence[AgentEvent]: ...

    def finish(self, state: RuntimeState, exit_code: int) -> Done | AgentFailed: ...

    def abort_command(self, state: RuntimeState) -> str | None: ...

    def template_steps(self) -> Sequence[TemplateStep]: ...

    def warmup_steps(self, *, serve: bool) -> Sequence[WarmupStep]: ...
