"""Puerto `AgentRuntime` (`ai-agent-core`, ADR-025, design.md §3): lo que un
adaptador de runtime (OpenCode, deepagents) traduce de un `AgentSpec` a
ficheros de configuración, a un comando y de vuelta a eventos. Puro: sin
`grpc`, `boto3` ni reloj. `_stream_base.py` es quien de verdad llama a estos
métodos contra el handle de comandos del sandbox.

El adaptador de OpenCode vive en `_opencode.py`; un llamante también puede
pasar su propio objeto que cumpla `AgentRuntime` como `runtime=` (lo que
hacen los tests con un runtime de doble).

Junto a `AgentRuntime`, ADR-028 añade dos puertos opcionales que un
adaptador implementa si quiere ir más allá de `sbx.agent.run`:
`InstallableRuntime` (qué entra en la imagen) y `ServableRuntime` (cómo se
sirve). La firma de `AgentRuntime` no cambia, así que un objeto escrito para
0.13 sigue funcionando."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final, Literal, Protocol, TypeAlias, get_args, runtime_checkable

from rayito._agent._domain import AgentSpec
from rayito._agent._events import AgentEvent, AgentFailed, Done
from rayito._limits import DEFAULT_WARMUP_STEP_TIMEOUT_SECONDS
from rayito.exceptions import InvalidArgumentException

#: Operaciones que un `TemplateStep` puede pedir a `AgentTemplate`.
STEP_RUN: Final = "run"
STEP_COPY: Final = "copy"
#: Los drivers del host del agente (ADR-028, design.md D3): lista cerrada.
#: `strands-ts` es el nombre antiguo de `node-callable` con el adaptador
#: `strands` y sólo se mantiene para leer una configuración ya escrita.
HostDriver: TypeAlias = Literal[
    "deepagents",
    "opencode",
    "strands",
    "strands-ts",
    "node-callable",
    "hermes",
    "callable",
    "command",
    "http-app",
]
HOST_DRIVERS: Final[tuple[str, ...]] = get_args(HostDriver)
#: Dónde se instala: un MicroVM de Rayito o un contenedor exportado.
InstallPlatform: TypeAlias = Literal["microvm", "container"]
INSTALL_PLATFORMS: Final[tuple[str, ...]] = get_args(InstallPlatform)
#: El intérprete con el que `InstallTarget` crea los venvs por defecto.
DEFAULT_INSTALL_PYTHON: Final = "python3"
#: Raíz de todo lo que una plantilla instala (root, 0755).
DEFAULT_INSTALL_DIR: Final = "/opt/agents"


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


@dataclass(frozen=True)
class RunCommand:
    """El comando que ejecuta la petición: texto de script de bash (nunca un
    secreto), sus variables de entorno y el stdin a mandarle (el prompt)."""

    script: str
    envs: Mapping[str, str] = field(default_factory=dict)
    stdin: bytes = b""


@dataclass(frozen=True)
class TemplateStep:
    """Un paso DSL que `AgentTemplate` añade a la plantilla. `op` es
    `STEP_RUN` (`args={"cmd": str}`, un `RUN`) o `STEP_COPY` (`args={"src":
    str, "dst": str}` más `"executable": True` si el destino debe quedar
    0755, un fichero del contexto de build)."""

    op: str
    args: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.op not in (STEP_RUN, STEP_COPY):
            raise InvalidArgumentException(
                f"TemplateStep.op debe ser {STEP_RUN!r} o {STEP_COPY!r}, recibido {self.op!r}"
            )
        object.__setattr__(self, "args", MappingProxyType(dict(self.args)))


def run_step(cmd: str) -> TemplateStep:
    """Un paso `RUN`."""
    return TemplateStep(STEP_RUN, {"cmd": cmd})


def copy_step(src: str, dst: str, *, executable: bool = False) -> TemplateStep:
    """Un paso `COPY` de un fichero o directorio del contexto de build."""
    args: dict[str, object] = {"src": src, "dst": dst}
    if executable:
        args["executable"] = True
    return TemplateStep(STEP_COPY, args)


@dataclass(frozen=True)
class WarmupStep:
    """Un paso de calentamiento: `agent.prepare()` y el `warmup=` de un pool
    (B2) corren la misma lista. `background=True` no espera a que termine
    (un proceso que sigue vivo); si no, se espera su salida con `timeout_seconds`."""

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

    def template_steps(self) -> Sequence[TemplateStep]: ...

    def warmup_steps(self) -> Sequence[WarmupStep]: ...


@dataclass(frozen=True)
class InstallTarget:
    """Dónde instala un `InstallableRuntime`: descripción pura, sin I/O.
    `python` es el intérprete con el que se crean los venvs y `install_dir`
    la raíz de lo instalado."""

    platform: InstallPlatform = "microvm"
    python: str = DEFAULT_INSTALL_PYTHON
    install_dir: str = DEFAULT_INSTALL_DIR

    def __post_init__(self) -> None:
        if self.platform not in INSTALL_PLATFORMS:
            raise InvalidArgumentException(
                f"InstallTarget.platform debe ser uno de {list(INSTALL_PLATFORMS)}"
            )
        if not self.python or not self.install_dir.startswith("/"):
            raise InvalidArgumentException(
                "InstallTarget.python no puede ser vacío e install_dir debe ser una ruta absoluta"
            )


@dataclass(frozen=True)
class GatewayWiring:
    """El modelo llega por las pasarelas del sandbox: `urls` por nombre de
    pasarela (MicroVM). El agente sólo ve un marcador como clave."""

    urls: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "urls", MappingProxyType(dict(self.urls)))


@dataclass(frozen=True)
class DirectWiring:
    """Sin pasarela (contenedor exportado): el host lee la credencial de la
    fuente estándar del proveedor. La garantía de la pasarela no aplica."""


ModelWiring: TypeAlias = GatewayWiring | DirectWiring


def wiring_urls(wiring: ModelWiring) -> Mapping[str, str] | None:
    """Las URLs de pasarela de `wiring`, o `None` con `DirectWiring`."""
    return wiring.urls if isinstance(wiring, GatewayWiring) else None


@dataclass(frozen=True)
class HostConfig:
    """Lo que el host del agente necesita para servir un runtime: el
    `driver` (lista cerrada `HOST_DRIVERS`), el intérprete `python` que
    ejecuta el host, los ficheros a escribir antes de arrancar, las
    variables de entorno y las opciones propias del driver."""

    driver: str
    python: str
    files: RuntimeFiles
    envs: Mapping[str, str] = field(default_factory=dict)
    options: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.driver not in HOST_DRIVERS:
            raise InvalidArgumentException(
                f"HostConfig.driver debe ser uno de {list(HOST_DRIVERS)}, recibido {self.driver!r}"
            )
        object.__setattr__(self, "envs", MappingProxyType(dict(self.envs)))
        object.__setattr__(self, "options", MappingProxyType(dict(self.options)))


@runtime_checkable
class InstallableRuntime(Protocol):
    """Puerto opcional: lo que un runtime pone en la imagen. `AgentTemplate`
    compone la imagen con los `install_steps()` de cada runtime."""

    name: str

    def install_steps(self, target: InstallTarget) -> Sequence[TemplateStep]: ...

    def context_files(self) -> Mapping[str, bytes]: ...

    def manifest_entry(self) -> Mapping[str, object]: ...

    def smoke_checks(self) -> Sequence[str]: ...

    def prefetch_paths(self) -> Sequence[str]: ...


@runtime_checkable
class ServableRuntime(Protocol):
    """Puerto opcional: cómo el host sirve un runtime (`host_config`)."""

    name: str

    def host_config(self, spec: AgentSpec, *, wiring: ModelWiring, workdir: str) -> HostConfig: ...


def is_installable(runtime: object) -> bool:
    """`runtime` implementa `InstallableRuntime`."""
    return isinstance(runtime, InstallableRuntime)


def is_servable(runtime: object) -> bool:
    """`runtime` implementa `ServableRuntime`."""
    return isinstance(runtime, ServableRuntime)


def require_installable(runtime: object) -> InstallableRuntime:
    """Falla con `InvalidArgumentException` nombrando el puerto que falta
    (`AgentTemplate`, `AgentProject.build`)."""
    if not isinstance(runtime, InstallableRuntime):
        raise InvalidArgumentException(
            "el runtime no implementa InstallableRuntime: no puede entrar en una plantilla"
        )
    return runtime


def require_servable(runtime: object) -> ServableRuntime:
    """Falla con `InvalidArgumentException` nombrando el puerto que falta
    (`AgentProject.deploy`, `AgentProject.export`)."""
    if not isinstance(runtime, ServableRuntime):
        raise InvalidArgumentException(
            "el runtime no implementa ServableRuntime: no puede desplegarse ni exportarse"
        )
    return runtime
