"""`AgentTemplate`: la plantilla de imagen con los runtimes de agente de IA
(`ai-agent-fast-start`, design.md §5). Compone el DSL de `Template` con la
receta del spike (docs/research/2026-10-agent-spike.md): OpenCode y
ripgrep fijados por versión y sha256 (`limits.json`), el venv de
deepagents con `--require-hashes` y todo de root con 0755, para que el
uid 1000 del sandbox no pueda reemplazar nada. Hornea además el manifiesto
`rayito.agent-template/1` y, con `prefetch=True`, el demonio de precarga
como `start_cmd` (opción A del diseño).

La composición es pura (`to_template()`, `manifest()`); sólo `build()`
escribe el contexto en un directorio temporal y llama a `Template.build`.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from rayito._agent._deepagents import DEEPAGENTS_RUNNER_PATH
from rayito._agent._opencode import OPENCODE_FLAG_ENVS
from rayito._images import DEFAULT_BUILD_TIMEOUT_SECONDS
from rayito._limits import (
    AGENT_DEEPAGENTS_REQUIREMENTS_SHA256,
    AGENT_MIN_MEMORY_MIB,
    AGENT_OPENCODE_SHA256,
    AGENT_OPENCODE_VERSION,
    AGENT_PREFETCH_INTERVAL_SECONDS,
    AGENT_PREFETCH_RESTORE_JUMP_SECONDS,
    AGENT_PROTOCOL_VERSION,
    AGENT_RIPGREP_SHA256,
    AGENT_RIPGREP_VERSION,
    AGENT_TEMPLATE_MANIFEST_PATH,
    AGENT_TEMPLATE_MANIFEST_SCHEMA,
)
from rayito._templates._dsl import AsyncTemplate, Template
from rayito.exceptions import InvalidArgumentException

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
#: Directorio raíz de todo lo que la plantilla instala (root, 0755).
AGENT_INSTALL_DIR: Final = "/opt/agents"
AGENT_BIN_DIR: Final = f"{AGENT_INSTALL_DIR}/bin"
OPENCODE_BINARY_PATH: Final = f"{AGENT_BIN_DIR}/opencode"
RIPGREP_BINARY_PATH: Final = f"{AGENT_BIN_DIR}/rg"
DEEPAGENTS_VENV_DIR: Final = f"{AGENT_INSTALL_DIR}/deepagents"
DEEPAGENTS_PYTHON_PATH: Final = f"{DEEPAGENTS_VENV_DIR}/bin/python"
DEEPAGENTS_REQUIREMENTS_NAME: Final = "requirements-deepagents.txt"
PREFETCH_SCRIPT_NAME: Final = "rayito-agent-prefetch"
PREFETCH_SCRIPT_PATH: Final = f"{AGENT_BIN_DIR}/{PREFETCH_SCRIPT_NAME}"
MANIFEST_CONTEXT_NAME: Final = "rayito-agent.json"
#: El runner de deepagents (dato de paquete de `_agent/_runner/`) en el
#: contexto de build; la plantilla lo instala en `DEEPAGENTS_RUNNER_PATH`.
DEEPAGENTS_RUNNER_NAME: Final = "deepagents_runner.py"
#: Asset de la release de OpenCode (linux arm64 glibc, binario único).
OPENCODE_RELEASE_URL: Final = (
    "https://github.com/anomalyco/opencode/releases/download/"
    f"v{AGENT_OPENCODE_VERSION}/opencode-linux-arm64.tar.gz"
)
RIPGREP_RELEASE_DIR: Final = f"ripgrep-{AGENT_RIPGREP_VERSION}-aarch64-unknown-linux-gnu"
RIPGREP_RELEASE_URL: Final = (
    "https://github.com/BurntSushi/ripgrep/releases/download/"
    f"{AGENT_RIPGREP_VERSION}/{RIPGREP_RELEASE_DIR}.tar.gz"
)
_ASSETS_PACKAGE: Final = "rayito._agent"
_ASSETS_DIR: Final = "_assets"
_RUNNER_DIR: Final = "_runner"


def _asset_bytes(name: str) -> bytes:
    return resources.files(_ASSETS_PACKAGE).joinpath(_ASSETS_DIR, name).read_bytes()


def deepagents_requirements() -> bytes:
    """El fichero de requisitos con hash del venv de deepagents (dato de
    paquete; su sha256 está en `limits.json`)."""
    return _asset_bytes(DEEPAGENTS_REQUIREMENTS_NAME)


def deepagents_runner() -> bytes:
    """El runner de deepagents que la plantilla instala (su sha256 va en el
    manifiesto como `runner_sha256`)."""
    return (
        resources.files(_ASSETS_PACKAGE).joinpath(_RUNNER_DIR, DEEPAGENTS_RUNNER_NAME).read_bytes()
    )


def prefetch_script() -> bytes:
    """El demonio de precarga (`rayito-agent-prefetch`)."""
    return _asset_bytes(PREFETCH_SCRIPT_NAME)


def _opencode_install() -> str:
    return (
        f"curl -fsSL --retry 3 -o /tmp/opencode.tar.gz {OPENCODE_RELEASE_URL}"
        f" && echo '{AGENT_OPENCODE_SHA256}  /tmp/opencode.tar.gz' | sha256sum -c -"
        f" && curl -fsSL --retry 3 -o /tmp/ripgrep.tar.gz {RIPGREP_RELEASE_URL}"
        f" && echo '{AGENT_RIPGREP_SHA256}  /tmp/ripgrep.tar.gz' | sha256sum -c -"
        f" && mkdir -p {AGENT_BIN_DIR}"
        f" && tar -xzf /tmp/opencode.tar.gz -C {AGENT_BIN_DIR} opencode"
        f" && tar -xzf /tmp/ripgrep.tar.gz -C /tmp"
        f" && mv /tmp/{RIPGREP_RELEASE_DIR}/rg {RIPGREP_BINARY_PATH}"
        f" && rm -rf /tmp/opencode.tar.gz /tmp/ripgrep.tar.gz /tmp/{RIPGREP_RELEASE_DIR}"
        f" && chown -R root:root {AGENT_INSTALL_DIR}"
        f" && chmod 0755 {AGENT_INSTALL_DIR} {AGENT_BIN_DIR} {OPENCODE_BINARY_PATH}"
        f" {RIPGREP_BINARY_PATH}"
        f" && ln -sf {OPENCODE_BINARY_PATH} /usr/local/bin/opencode"
        f" && ln -sf {RIPGREP_BINARY_PATH} /usr/local/bin/rg"
    )


def _deepagents_install() -> str:
    requirements = f"{AGENT_INSTALL_DIR}/{DEEPAGENTS_REQUIREMENTS_NAME}"
    return (
        f"echo '{AGENT_DEEPAGENTS_REQUIREMENTS_SHA256}  {requirements}' | sha256sum -c -"
        f" && python3 -m venv {DEEPAGENTS_VENV_DIR}"
        f" && {DEEPAGENTS_PYTHON_PATH} -m pip install --no-cache-dir"
        " --require-hashes --no-deps --only-binary=:all:"
        f" -r {requirements}"
        f" && {DEEPAGENTS_PYTHON_PATH} -m pip check"
    )


def _smoke_test(runtimes: Sequence[str]) -> str:
    checks = []
    if "opencode" in runtimes:
        checks += ["su user -c 'opencode --version'", "su user -c 'rg --version'"]
    if "deepagents" in runtimes:
        checks.append(
            f"su user -c \"{DEEPAGENTS_PYTHON_PATH} -c 'import deepagents, langchain_aws'\""
        )
        checks.append(f"su user -c 'test -r {DEEPAGENTS_RUNNER_PATH}'")
    return " && ".join(checks)


def prefetch_start_cmd() -> str:
    """El `start_cmd` del demonio de precarga, con sus tres argumentos."""
    return (
        f"{PREFETCH_SCRIPT_PATH} {AGENT_TEMPLATE_MANIFEST_PATH}"
        f" {AGENT_PREFETCH_RESTORE_JUMP_SECONDS} {AGENT_PREFETCH_INTERVAL_SECONDS}"
    )


@dataclass(frozen=True)
class AgentTemplate:
    """Plantilla de imagen con los runtimes del agente de IA.

    `runtimes` elige qué se instala (`"opencode"`, `"deepagents"`);
    `prefetch=True` hornea el demonio que precarga los binarios en la caché
    de páginas tras cada restauración del snapshot. `memory_mib` por debajo
    de `AGENT_MIN_MEMORY_MIB` (2048, el RSS medido de OpenCode) es
    `InvalidArgumentException`.

    Coste y activación
    -------------------
    Activa: `AgentTemplate(...).build(bucket=...)` o
        `rayito agent template build`.
    Recursos y llamadas AWS: los de `Template.build` (un build de imagen,
        `s3:PutObject` del contexto, una versión de imagen nueva); nada si
        no se construye.
    Coste aproximado: build de 271-320 s (medido en AWS, 2026-10-07); la
        versión almacenada ≈ 3,1 GB (código 2,10 + memoria 0,92 + disco
        0,04) por $0,08/GB-mes, con el mínimo de una semana ≈
        $0,057/semana (≈ $0,25/mes) por versión, y cada lanzamiento lee el
        snapshot de memoria (≈ 0,92 GB por $0,00155/GB ≈ $0,0014).
        Estimación con precios de lista de Lambda MicroVMs, us-east-1,
        consultados 2026-10-06 (https://aws.amazon.com/lambda/pricing/).
    IAM: la política `RayitoTemplateBuilder` (la misma que `Template.build`).
    Cómo apagarla: no la construyas; borra sus versiones con `rayito image`.
    Ejemplo:
        AgentTemplate(runtimes=("opencode",)).build(bucket="amzn-s3-demo-bucket")
    """

    name: str = DEFAULT_AGENT_TEMPLATE_NAME
    base: str = DEFAULT_AGENT_TEMPLATE_BASE
    runtimes: tuple[str, ...] = AGENT_TEMPLATE_RUNTIMES
    prefetch: bool = True
    memory_mib: int = AGENT_MIN_MEMORY_MIB
    base_version: str | None = None

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
        unknown = [name for name in runtimes if name not in AGENT_TEMPLATE_RUNTIMES]
        if unknown:
            raise InvalidArgumentException(
                f"runtime de plantilla desconocido: {unknown[0]!r} "
                f"(admitidos: {', '.join(AGENT_TEMPLATE_RUNTIMES)})"
            )
        if len(set(runtimes)) != len(runtimes):
            raise InvalidArgumentException("runtimes no puede repetir un nombre")
        if not self.name or not self.base:
            raise InvalidArgumentException("name y base no pueden ser vacíos")
        object.__setattr__(self, "runtimes", runtimes)

    def manifest(self) -> dict[str, object]:
        """El manifiesto `rayito.agent-template/1` que se hornea en
        `AGENT_TEMPLATE_MANIFEST_PATH`: versiones y hashes de lo instalado,
        protocolo y las rutas que precarga el demonio."""
        prefetch_paths: list[str] = []
        opencode: dict[str, str] | None = None
        deepagents: dict[str, str] | None = None
        runner_sha256: str | None = None
        if "opencode" in self.runtimes:
            opencode = {"version": AGENT_OPENCODE_VERSION, "sha256": AGENT_OPENCODE_SHA256}
            prefetch_paths += [OPENCODE_BINARY_PATH, RIPGREP_BINARY_PATH]
        if "deepagents" in self.runtimes:
            deepagents = {"requirements_sha256": AGENT_DEEPAGENTS_REQUIREMENTS_SHA256}
            runner_sha256 = hashlib.sha256(deepagents_runner()).hexdigest()
        return {
            "schema": AGENT_TEMPLATE_MANIFEST_SCHEMA,
            "protocol": AGENT_PROTOCOL_VERSION,
            "opencode": opencode,
            "deepagents": deepagents,
            "runner_sha256": runner_sha256,
            "prefetch_paths": prefetch_paths,
        }

    def context_files(self) -> dict[str, bytes]:
        """Los ficheros del contexto de build (nombre relativo -> bytes)."""
        files = {MANIFEST_CONTEXT_NAME: _manifest_bytes(self.manifest())}
        if "deepagents" in self.runtimes:
            files[DEEPAGENTS_REQUIREMENTS_NAME] = deepagents_requirements()
            files[DEEPAGENTS_RUNNER_NAME] = deepagents_runner()
        if self.prefetch:
            files[PREFETCH_SCRIPT_NAME] = prefetch_script()
        return files

    def to_template(self, template_class: type[Template] = Template) -> Template:
        """El `Template` equivalente (puro: no lee ni escribe nada)."""
        tpl = template_class().from_base_image(self.base, version=self.base_version)
        if "opencode" in self.runtimes:
            tpl = tpl.run_cmd(_opencode_install())
        if "deepagents" in self.runtimes:
            requirements = f"{AGENT_INSTALL_DIR}/{DEEPAGENTS_REQUIREMENTS_NAME}"
            tpl = (
                tpl.copy(DEEPAGENTS_REQUIREMENTS_NAME, requirements)
                .run_cmd(_deepagents_install())
                .copy(DEEPAGENTS_RUNNER_NAME, DEEPAGENTS_RUNNER_PATH)
            )
        tpl = tpl.copy(MANIFEST_CONTEXT_NAME, AGENT_TEMPLATE_MANIFEST_PATH)
        if self.prefetch:
            tpl = tpl.copy(PREFETCH_SCRIPT_NAME, PREFETCH_SCRIPT_PATH)
        executables = [DEEPAGENTS_RUNNER_PATH] if "deepagents" in self.runtimes else []
        if self.prefetch:
            executables.append(PREFETCH_SCRIPT_PATH)
        tpl = tpl.run_cmd(
            f"mkdir -p {AGENT_BIN_DIR} && chown -R root:root {AGENT_INSTALL_DIR}"
            f" && chmod -R a+rX,go-w {AGENT_INSTALL_DIR}"
            + (f" && chmod 0755 {' '.join(executables)}" if executables else "")
        )
        tpl = tpl.set_envs(dict(OPENCODE_FLAG_ENVS))
        tpl = tpl.run_cmd(_smoke_test(self.runtimes))
        if self.prefetch:
            tpl = tpl.set_start_cmd(prefetch_start_cmd())
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
            context = _write_context(Path(tmp), self.context_files())
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
            context = _write_context(Path(tmp), self.context_files())
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


def _manifest_bytes(manifest: Mapping[str, object]) -> bytes:
    return (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()


def _write_context(directory: Path, files: Mapping[str, bytes]) -> Path:
    for name, data in files.items():
        (directory / name).write_bytes(data)
    return directory


__all__ = [
    "AGENT_TEMPLATE_RUNTIMES",
    "DEFAULT_AGENT_TEMPLATE_BASE",
    "DEFAULT_AGENT_TEMPLATE_NAME",
    "AgentTemplate",
    "AsyncAgentTemplate",
    "deepagents_requirements",
    "prefetch_script",
    "prefetch_start_cmd",
]
