"""`AgentTemplate`: la plantilla de imagen con los runtimes de agente de IA
(`ai-agent-fast-start`, design.md §5; ADR-028). Compone el DSL de `Template`
con los `install_steps()` de cada runtime (el puerto `InstallableRuntime`):
OpenCode y ripgrep fijados por versión y sha256 (`limits.json`), el venv de
deepagents con `--require-hashes` y todo de root con 0755, para que el
uid 1000 del sandbox no pueda reemplazar nada. Hornea además el manifiesto
`rayito.agent-template/1`, el código del usuario (`app=`) en
`AGENT_APP_DIR` y, con `prefetch=True`, el demonio de precarga como
`start_cmd` (opción A del diseño).

La composición es pura (`to_template()`, `manifest()`); sólo `build()`
escribe el contexto en un directorio temporal y llama a `Template.build`.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Final

from rayito._agent._deepagents import deepagents_requirements, deepagents_runner
from rayito._agent._opencode import OPENCODE_FLAG_ENVS
from rayito._agent._recipes import (
    AGENT_APP_CONTEXT_DIR,
    AGENT_APP_DIR,
    AGENT_BIN_DIR,
    AGENT_HOST_CONTEXT_DIR,
    AGENT_HOST_DIR,
    AGENT_INSTALL_DIR,
    PREFETCH_SCRIPT_NAME,
    PREFETCH_SCRIPT_PATH,
    agent_host_files,
    host_sha256,
    prefetch_script,
)
from rayito._agent._runtime import (
    STEP_COPY,
    STEP_RUN,
    AgentRuntime,
    InstallableRuntime,
    InstallTarget,
    TemplateStep,
    require_installable,
)
from rayito._agent._runtimes import RUNTIME_KINDS, resolve_runtime
from rayito._images import DEFAULT_BUILD_TIMEOUT_SECONDS
from rayito._limits import (
    AGENT_KERNEL_WARMUP_MARKER_PATH,
    AGENT_KERNEL_WARMUP_SLIM_VALUE,
    AGENT_MIN_MEMORY_MIB,
    AGENT_PREFETCH_BUILD_MARKER_PATH,
    AGENT_PREFETCH_BUILD_TIMEOUT_SECONDS,
    AGENT_PREFETCH_INTERVAL_SECONDS,
    AGENT_PREFETCH_RESTORE_JUMP_SECONDS,
    AGENT_PROTOCOL_VERSION,
    AGENT_TEMPLATE_MANIFEST_PATH,
    AGENT_TEMPLATE_MANIFEST_SCHEMA,
    AGENT_TOKEN_FILE_DEFAULT,
)
from rayito._templates._context import collect_context_files
from rayito._templates._dockerignore import DOCKERIGNORE_FILENAME, DockerIgnore
from rayito._templates._dsl import AsyncTemplate, Template
from rayito._templates._instructions import CopyStep
from rayito._templates._ready_cmds import ReadyCommand, wait_for_file
from rayito.exceptions import InvalidArgumentException, UnimplementedError

if TYPE_CHECKING:
    from rayito._templates._models import BuildInfo

#: Nombre por defecto de la imagen que construye `AgentTemplate`.
DEFAULT_AGENT_TEMPLATE_NAME: Final = "rayito-agent"
#: Imagen base: la variante con capabilities, la única donde `rayd` aplica
#: el deny-all de egress (`allow_internet_access=False`) que el agente
#: necesita.
DEFAULT_AGENT_TEMPLATE_BASE: Final = "rayito-base-caps"
#: Runtimes que la plantilla instala si no se dice otra cosa.
AGENT_TEMPLATE_RUNTIMES: Final = ("opencode", "deepagents")
MANIFEST_CONTEXT_NAME: Final = "rayito-agent.json"
#: El directorio de estado de la CLI (`.rayito/`: el access token de la flota y
#: las plazas calientes), a cualquier profundidad. Se excluye siempre del
#: código del usuario y va el último, así que ni su `.dockerignore` ni
#: `exclude` pueden volver a meterlo en la imagen ni en su hash.
AGENT_STATE_DIR_EXCLUDE: Final = f"**/{PurePosixPath(AGENT_TOKEN_FILE_DEFAULT).parts[0]}"
#: Las claves del manifiesto `rayito.agent-template/1` que existían antes de
#: ADR-028 y que el demonio de precarga y los SDK anteriores leen.
LEGACY_OPENCODE_RUNTIME: Final = "opencode"
LEGACY_DEEPAGENTS_RUNTIME: Final = "deepagents"
#: Los modos de un directorio y de un fichero de lo instalado: de root,
#: sin escritura para grupo ni otros.
_PERMISSIONS_CMD: Final = (
    f"chown -R root:root {AGENT_INSTALL_DIR} && chmod -R a+rX,go-w {AGENT_INSTALL_DIR}"
)


def _slim_kernel_cmd() -> str:
    """Apaga el calentamiento del kernel de `run_code` (el marcador que lee
    `0004_warmup.py` del sidecar, el mismo que escribe `image_zip.py
    --variant slim`)."""
    return (
        f"printf '%s\\n' {AGENT_KERNEL_WARMUP_SLIM_VALUE} > {AGENT_KERNEL_WARMUP_MARKER_PATH}"
        f" && chmod 0644 {AGENT_KERNEL_WARMUP_MARKER_PATH}"
    )


def prefetch_start_cmd() -> str:
    """El `start_cmd` del demonio de precarga, con sus cuatro argumentos: el
    cuarto le hace precargar una vez antes del snapshot del build."""
    return (
        f"{PREFETCH_SCRIPT_PATH} {AGENT_TEMPLATE_MANIFEST_PATH}"
        f" {AGENT_PREFETCH_RESTORE_JUMP_SECONDS} {AGENT_PREFETCH_INTERVAL_SECONDS}"
        f" {AGENT_PREFETCH_BUILD_MARKER_PATH}"
    )


def prefetch_ready_cmd() -> ReadyCommand:
    """El `ready_cmd` que retiene el snapshot del build hasta que el demonio
    terminó su precarga inicial."""
    return wait_for_file(AGENT_PREFETCH_BUILD_MARKER_PATH).timeout(
        AGENT_PREFETCH_BUILD_TIMEOUT_SECONDS
    )


def _installable(item: str | AgentRuntime | InstallableRuntime) -> InstallableRuntime:
    if isinstance(item, str):
        try:
            runtime: object = resolve_runtime(item)
        except UnimplementedError:
            if item in RUNTIME_KINDS:
                raise
            raise InvalidArgumentException(
                f"runtime de plantilla desconocido: {item!r} "
                f"(admitidos: {', '.join(RUNTIME_KINDS)})"
            ) from None
    else:
        runtime = item
    return require_installable(runtime)


@dataclass(frozen=True)
class AgentTemplate:
    """Plantilla de imagen con los runtimes del agente de IA.

    `runtimes` elige qué se instala: nombres del registro (`"opencode"`,
    `"deepagents"`, `"langgraph"`...) u objetos que implementen
    `InstallableRuntime`; la imagen se compone con los `install_steps()` de
    cada uno. `app` es un directorio local con el código del agente: se copia
    a `AGENT_APP_DIR` (de root, sin escritura para `user`) con las reglas de
    su `.dockerignore` más `app_exclude` (patrones de `.dockerignore`).
    `prefetch=True` hornea el demonio que precarga los binarios en la caché
    de páginas tras cada restauración del snapshot. `kernel_warmup=False`
    (por defecto) apaga el calentamiento del kernel de `run_code` (numpy,
    pandas, matplotlib, scipy y scikit-learn importados en cada arranque del
    kernel, también en la rotación que `create()` espera): medido en AWS
    (`AWS_API_NOTES.md` Q155), `create()` vuelve 4,4 s antes (p50) y el
    snapshot de memoria pesa 248 MB menos, con el mismo primer token del
    agente; a cambio, la primera celda de `run_code` que importe ese stack
    paga la importación. `kernel_warmup=True` deja el kernel como en la
    imagen base. `memory_mib` por debajo de `AGENT_MIN_MEMORY_MIB` (2048,
    el RSS medido de OpenCode) es `InvalidArgumentException`.

    Coste y activación
    -------------------
    Activa: `AgentTemplate(...).build(bucket=...)` o
        `rayito agent template build`.
    Recursos y llamadas AWS: los de `Template.build` (un build de imagen,
        `s3:PutObject` del contexto, una versión de imagen nueva); nada si
        no se construye.
    Coste aproximado: build de 260-320 s (medido en AWS, 2026-10-07 y
        2026-10-09); la versión almacenada ≈ 3,0 GB (código 2,10 + memoria
        0,86 + disco 0,03; memoria 0,92 con `kernel_warmup=True`) por
        $0,08/GB-mes, con el mínimo de una semana ≈ $0,056/semana
        (≈ $0,24/mes) por versión, y cada lanzamiento lee el snapshot de
        memoria (≈ 0,86 GB por $0,00155/GB ≈ $0,0013).
        Estimación con precios de lista de Lambda MicroVMs, us-east-1,
        consultados 2026-10-06 (https://aws.amazon.com/lambda/pricing/).
    IAM: la política `RayitoTemplateBuilder` (la misma que `Template.build`).
    Cómo apagarla: no la construyas; borra sus versiones con `rayito image`.
    Ejemplo:
        AgentTemplate(runtimes=("opencode",)).build(bucket="amzn-s3-demo-bucket")
    """

    name: str = DEFAULT_AGENT_TEMPLATE_NAME
    base: str = DEFAULT_AGENT_TEMPLATE_BASE
    runtimes: tuple[str | AgentRuntime, ...] = AGENT_TEMPLATE_RUNTIMES
    prefetch: bool = True
    kernel_warmup: bool = False
    memory_mib: int = AGENT_MIN_MEMORY_MIB
    base_version: str | None = None
    app: str | None = None
    app_exclude: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.memory_mib, bool) or not isinstance(self.memory_mib, int):
            raise InvalidArgumentException("memory_mib debe ser un entero")
        if self.memory_mib < AGENT_MIN_MEMORY_MIB:
            raise InvalidArgumentException(
                f"memory_mib debe ser >= {AGENT_MIN_MEMORY_MIB} para el agente de IA, "
                f"recibido {self.memory_mib}"
            )
        runtimes = tuple(self.runtimes)
        if not runtimes:
            raise InvalidArgumentException("runtimes no puede ser vacío")
        names = [_installable(item).name for item in runtimes]
        if len(set(names)) != len(names):
            raise InvalidArgumentException("runtimes no puede repetir un nombre")
        if not self.name or not self.base:
            raise InvalidArgumentException("name y base no pueden ser vacíos")
        if self.app is not None and not Path(self.app).is_dir():
            raise InvalidArgumentException("app debe ser un directorio existente")
        object.__setattr__(self, "runtimes", runtimes)
        object.__setattr__(self, "app_exclude", tuple(self.app_exclude))

    def installables(self) -> tuple[InstallableRuntime, ...]:
        """Los runtimes de la plantilla, ya resueltos."""
        return tuple(_installable(item) for item in self.runtimes)

    def manifest(self) -> dict[str, object]:
        """El manifiesto `rayito.agent-template/1` que se hornea en
        `AGENT_TEMPLATE_MANIFEST_PATH`: versiones y hashes de lo instalado,
        protocolo, las rutas que precarga el demonio y, desde ADR-028, la
        entrada de cada runtime (`runtimes`) y el sha256 del host del agente
        (`host_sha256`)."""
        runtimes = self.installables()
        entries = {runtime.name: dict(runtime.manifest_entry()) for runtime in runtimes}
        deepagents = entries.get(LEGACY_DEEPAGENTS_RUNTIME)
        return {
            "schema": AGENT_TEMPLATE_MANIFEST_SCHEMA,
            "protocol": AGENT_PROTOCOL_VERSION,
            "opencode": entries.get(LEGACY_OPENCODE_RUNTIME),
            "deepagents": None
            if deepagents is None
            else {"requirements_sha256": deepagents["requirements_sha256"]},
            "runner_sha256": None if deepagents is None else deepagents["runner_sha256"],
            "prefetch_paths": [path for runtime in runtimes for path in runtime.prefetch_paths()],
            "runtimes": entries,
            "host_sha256": host_sha256(agent_host_files()),
        }

    def context_files(self) -> dict[str, bytes]:
        """Los ficheros del contexto de build (nombre relativo -> bytes). El
        código del usuario (`app`) no entra aquí: lo escribe `build()`."""
        files = {MANIFEST_CONTEXT_NAME: _manifest_bytes(self.manifest())}
        for runtime in self.installables():
            files.update(runtime.context_files())
        for relative, data in agent_host_files().items():
            files[f"{AGENT_HOST_CONTEXT_DIR}/{relative}"] = data
        if self.prefetch:
            files[PREFETCH_SCRIPT_NAME] = prefetch_script()
        return files

    def to_template(self, template_class: type[Template] = Template) -> Template:
        """El `Template` equivalente (puro: no lee ni escribe nada)."""
        tpl = template_class().from_base_image(self.base, version=self.base_version)
        executables: list[str] = []
        target = InstallTarget()
        for runtime in self.installables():
            tpl, runtime_executables = _apply_steps(tpl, runtime.install_steps(target))
            executables += runtime_executables
        tpl = tpl.copy(MANIFEST_CONTEXT_NAME, AGENT_TEMPLATE_MANIFEST_PATH)
        if self.prefetch:
            tpl = tpl.copy(PREFETCH_SCRIPT_NAME, PREFETCH_SCRIPT_PATH)
            executables.append(PREFETCH_SCRIPT_PATH)
        if agent_host_files():
            tpl = tpl.copy(AGENT_HOST_CONTEXT_DIR, AGENT_HOST_DIR)
        if self.app is not None:
            tpl = tpl.copy(AGENT_APP_CONTEXT_DIR, AGENT_APP_DIR)
        tpl = tpl.run_cmd(
            f"mkdir -p {AGENT_BIN_DIR} && {_PERMISSIONS_CMD}"
            + (f" && chmod 0755 {' '.join(executables)}" if executables else "")
        )
        if not self.kernel_warmup:
            tpl = tpl.run_cmd(_slim_kernel_cmd())
        tpl = tpl.set_envs(dict(OPENCODE_FLAG_ENVS))
        smoke = " && ".join(
            check for runtime in self.installables() for check in runtime.smoke_checks()
        )
        if smoke:
            tpl = tpl.run_cmd(smoke)
        if self.prefetch:
            tpl = tpl.set_start_cmd(prefetch_start_cmd(), prefetch_ready_cmd())
        return tpl

    def to_dockerfile(self) -> str:
        return self.to_template().to_dockerfile()

    def build(
        self,
        *,
        bucket: str,
        force: bool = False,
        timeout: float = DEFAULT_BUILD_TIMEOUT_SECONDS,
        on_build_logs: Callable[[str], None] | None = None,
        region: str | None = None,
        session: Any = None,
    ) -> BuildInfo:
        """Construye la imagen `name` con `Template.build` (ver el bloque
        de coste de la clase)."""
        with tempfile.TemporaryDirectory(prefix="rayito-agent-") as tmp:
            context = _write_context(Path(tmp), self.context_files(), self.app, self.app_exclude)
            return Template.build(
                self.to_template(),
                self.name,
                bucket=bucket,
                memory_mb=self.memory_mib,
                force=force,
                timeout=timeout,
                on_build_logs=on_build_logs,
                region=region,
                session=session,
                context_dir=context,
            )


@dataclass(frozen=True)
class AsyncAgentTemplate(AgentTemplate):
    """`AgentTemplate` con `build` en versión `async` (`AsyncTemplate`)."""

    async def build(  # type: ignore[override]
        self,
        *,
        bucket: str,
        force: bool = False,
        timeout: float = DEFAULT_BUILD_TIMEOUT_SECONDS,
        on_build_logs: Callable[[str], None] | None = None,
        region: str | None = None,
        session: Any = None,
    ) -> BuildInfo:
        with tempfile.TemporaryDirectory(prefix="rayito-agent-") as tmp:
            context = _write_context(Path(tmp), self.context_files(), self.app, self.app_exclude)
            return await AsyncTemplate.build(
                self.to_template(AsyncTemplate),
                self.name,
                bucket=bucket,
                memory_mb=self.memory_mib,
                force=force,
                timeout=timeout,
                on_build_logs=on_build_logs,
                region=region,
                session=session,
                context_dir=context,
            )


def _apply_steps(tpl: Any, steps: Sequence[TemplateStep]) -> tuple[Any, list[str]]:
    """Aplica los pasos de un runtime al `Template`; devuelve también los
    destinos que deben quedar 0755."""
    executables: list[str] = []
    for step in steps:
        if step.op == STEP_RUN:
            tpl = tpl.run_cmd(str(step.args["cmd"]))
        elif step.op == STEP_COPY:
            destination = str(step.args["dst"])
            tpl = tpl.copy(str(step.args["src"]), destination)
            if step.args.get("executable") is True:
                executables.append(destination)
    return tpl, executables


def _manifest_bytes(manifest: Mapping[str, object]) -> bytes:
    return (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()


def _app_files(app: str, exclude: Sequence[str]) -> tuple[tuple[str, bytes], ...]:
    """Los ficheros del código del usuario, sin lo que ignoran su
    `.dockerignore`, `exclude` y `AGENT_STATE_DIR_EXCLUDE`. Todos filtran
    antes de leer ningún fichero: un `.venv` excluido ni se lee ni dispara el
    aviso de secretos."""
    source = Path(app)
    dockerignore = source / DOCKERIGNORE_FILENAME
    patterns = dockerignore.read_text(encoding="utf-8") if dockerignore.is_file() else ""
    return collect_context_files(
        source,
        [CopyStep(".", AGENT_APP_DIR)],
        ignore=DockerIgnore.from_text("\n".join([patterns, *exclude, AGENT_STATE_DIR_EXCLUDE])),
    )


def _write_context(
    directory: Path, files: Mapping[str, bytes], app: str | None, exclude: Sequence[str]
) -> Path:
    for name, data in files.items():
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    if app is not None:
        (directory / AGENT_APP_CONTEXT_DIR).mkdir()
        for relative, data in _app_files(app, exclude):
            target = directory / AGENT_APP_CONTEXT_DIR / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    return directory


__all__ = [
    "AGENT_STATE_DIR_EXCLUDE",
    "AGENT_TEMPLATE_RUNTIMES",
    "DEFAULT_AGENT_TEMPLATE_BASE",
    "DEFAULT_AGENT_TEMPLATE_NAME",
    "AgentTemplate",
    "AsyncAgentTemplate",
    "deepagents_requirements",
    "deepagents_runner",
    "prefetch_ready_cmd",
    "prefetch_script",
    "prefetch_start_cmd",
]
