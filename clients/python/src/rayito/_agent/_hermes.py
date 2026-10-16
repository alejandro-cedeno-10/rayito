"""Adaptador de Hermes Agent v0.22.2 (Nous Research, MIT) de los puertos
`AgentRuntime`, `InstallableRuntime` y `ServableRuntime` (ADR-028, D2, D7 y
D9). Puro: escribe la configuración que lee el driver `hermes` del host,
compone la receta de instalación y deja que `_host_runtime.HostBackedRuntime`
arme el contrato de tareas.

Hermes no está en PyPI y su lock sólo cubre CPython 3.14, así que la imagen
instala tres cosas bajo `AGENT_INSTALL_DIR/hermes`: un CPython 3.14
independiente (python-build-standalone, con su sha256), un venv con los
requisitos con hash (`requirements-hermes.txt`, el `uv.lock` de Hermes
exportado) y el checkout del tag, verificado contra su commit. Hermes corre
como `hermes gateway run` con su API HTTP sólo en loopback, y el driver le
habla por `POST /v1/runs` (medido en `testdata/agent/hermes-v0.22.2-spike.json`).

Valores seguros (D9): un `HERMES_HOME` propio, `HERMES_IGNORE_USER_CONFIG=1`,
`TERMINAL_ENV=local`, sólo los toolsets pedidos (y los que los permisos no
deniegan), sin telemetría, sin comprobación de actualizaciones, sin títulos
generados por un segundo modelo y la clave de la API generada por el driver en
cada arranque (nunca en ficheros, `envs` ni argv).
"""

from __future__ import annotations

import dataclasses
import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Final

from rayito._agent._domain import AgentSpec
from rayito._agent._host_runtime import (
    PYTHON_FLAG_ENVS,
    HostBackedRuntime,
    HostSetup,
    ProviderClient,
    container_packages_step,
    provider_base_url,
    require_supported_provider,
    standard_model_envs,
)
from rayito._agent._opencode import shell_quote
from rayito._agent._recipes import (
    AGENT_APP_DIR,
    AGENT_INSTALL_DIR,
    asset_bytes,
    hashed_venv_install,
)
from rayito._agent._runtime import (
    InstallTarget,
    TemplateStep,
    WarmupStep,
    copy_step,
    run_step,
)
from rayito._limits import (
    AGENT_HERMES_API_PORT,
    AGENT_HERMES_COMMIT,
    AGENT_HERMES_PYTHON_BUILD_TAG,
    AGENT_HERMES_PYTHON_SHA256,
    AGENT_HERMES_PYTHON_VERSION,
    AGENT_HERMES_REQUIREMENTS_SHA256,
    AGENT_HERMES_VERSION,
    AGENT_PROTOCOL_VERSION,
    AGENT_STATE_DIR,
    MODEL_CREDENTIAL_PLACEHOLDER,
)
from rayito.exceptions import InvalidArgumentException

HERMES_NAME: Final = "hermes"
HERMES_STATE_DIR: Final = f"{AGENT_STATE_DIR}/hermes"
#: `HERMES_HOME`: configuración, sesiones y estado de Hermes, propios de
#: Rayito y no el `~/.hermes` del usuario.
HERMES_HOME_DIR: Final = f"{HERMES_STATE_DIR}/home"
#: Lo que se instala (root, 0755): el CPython 3.14, el venv y el checkout.
HERMES_INSTALL_DIR: Final = f"{AGENT_INSTALL_DIR}/hermes"
HERMES_PYTHON_DIR: Final = f"{HERMES_INSTALL_DIR}/python"
HERMES_VENV_DIR: Final = f"{HERMES_INSTALL_DIR}/venv"
HERMES_PYTHON: Final = f"{HERMES_VENV_DIR}/bin/python"
HERMES_SOURCE_DIR: Final = f"{HERMES_INSTALL_DIR}/src"
HERMES_REQUIREMENTS_NAME: Final = "requirements-hermes.txt"
HERMES_REPOSITORY_URL: Final = "https://github.com/NousResearch/hermes-agent"
#: Tarball `install_only` de python-build-standalone para linux arm64 glibc.
HERMES_PYTHON_TARBALL_URL: Final = (
    "https://github.com/astral-sh/python-build-standalone/releases/download/"
    f"{AGENT_HERMES_PYTHON_BUILD_TAG}/cpython-{AGENT_HERMES_PYTHON_VERSION}"
    f"%2B{AGENT_HERMES_PYTHON_BUILD_TAG}-aarch64-unknown-linux-gnu-install_only.tar.gz"
)
#: Lo que sobra del checkout en una imagen (medido: 379 MiB -> 86 MiB).
HERMES_PRUNED_PATHS: Final[tuple[str, ...]] = (
    ".git",
    "tests",
    "tests-js",
    "apps",
    "website",
    "ui-tui",
    "web",
    "contributors",
    "evals",
    "docker",
    "nix",
)
#: Toolsets por defecto: terminal y ficheros (medido en el spike: 8
#: herramientas, sin navegador ni web).
HERMES_DEFAULT_TOOLSETS: Final[tuple[str, ...]] = ("terminal", "file")
#: Toolset de Hermes -> herramientas de `AgentPermissions` que lo gobiernan:
#: el toolset se quita cuando todas están denegadas.
HERMES_PERMISSION_TOOLSETS: Final[Mapping[str, tuple[str, ...]]] = {
    "terminal": ("bash",),
    "file": ("read", "edit"),
    "web": ("webfetch", "websearch"),
}
#: Variables de Hermes que dependen de la configuración del SDK. Las fijas
#: (`HERMES_IGNORE_USER_CONFIG`, `TERMINAL_ENV`, `API_SERVER_ENABLED`,
#: `API_SERVER_HOST`) las define sólo el driver del host (`STATIC_ENVS`), y
#: `API_SERVER_KEY` la genera el driver en cada arranque.
HERMES_ENVS: Final[Mapping[str, str]] = {
    "HERMES_HOME": HERMES_HOME_DIR,
    "API_SERVER_PORT": str(AGENT_HERMES_API_PORT),
}
#: Cliente de modelo de Hermes por proveedor, medido en el spike. `google` y
#: `azure` quedan fuera (Hermes no los cablea a través de una pasarela).
HERMES_PROVIDERS: Final[Mapping[str, ProviderClient]] = {
    "bedrock": ProviderClient("bedrock", ""),
    "anthropic": ProviderClient("anthropic", ""),
    "openai": ProviderClient("custom", "/v1"),
    "openai-compatible": ProviderClient("custom", None),
}
#: Proveedores que Hermes lee de variables de entorno en vez de la
#: configuración.
HERMES_ENV_PROVIDERS: Final[tuple[str, ...]] = ("bedrock", "anthropic")
#: Sin pasarela (contenedor exportado), `openai` usa el proveedor nativo de
#: Hermes y su endpoint estándar.
HERMES_DIRECT_OPENAI_CLIENT: Final = "openai-api"
PYTHON_TARBALL_PATH: Final = "/tmp/hermes-python.tar.gz"
HERMES_SMOKE_COMMAND: Final = (
    f"cd {HERMES_SOURCE_DIR} && PYTHONPATH={HERMES_SOURCE_DIR} {HERMES_PYTHON} -P -c "
    "'import aiohttp, boto3, anthropic, hermes_cli.main, gateway.platforms.api_server'"
)
_TOOLSET_PATTERN: Final = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_SKILLS_DIR_PATTERN: Final = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*")


def hermes_requirements() -> bytes:
    """Requisitos con hash del venv de Hermes (dato de paquete; su sha256
    está en `limits.json`)."""
    return asset_bytes(HERMES_REQUIREMENTS_NAME)


def effective_toolsets(spec: AgentSpec, toolsets: Sequence[str]) -> list[str]:
    """Los toolsets pedidos menos los que `AgentPermissions` deniega del
    todo (todas las herramientas que los gobiernan, con
    `DEFAULT_DENIED_TOOLS` por debajo). Hermes decide por toolset, no por
    comando: un patrón en una herramienta que gobierna un toolset es
    `InvalidArgumentException`."""
    effective = spec.permissions.effective_tools()
    for tools in HERMES_PERMISSION_TOOLSETS.values():
        for tool in tools:
            if not isinstance(effective.get(tool, "allow"), str):
                raise InvalidArgumentException(
                    f"Hermes no admite patrones en AgentPermissions.tools[{tool!r}]: "
                    "usa 'allow' o 'deny'"
                )
    default = spec.permissions.default
    kept: list[str] = []
    for toolset in toolsets:
        governing = HERMES_PERMISSION_TOOLSETS.get(toolset)
        denied = governing is not None and all(
            effective.get(tool, default) == "deny" for tool in governing
        )
        if not denied:
            kept.append(toolset)
    return kept


class HermesRuntime(HostBackedRuntime):
    """`AgentRuntime` de Hermes Agent (`runtime=HermesRuntime(...)` o
    `runtime="hermes"`).

    `toolsets` son los toolsets de Hermes que se activan (por defecto
    terminal y ficheros); los que `AgentPermissions` deniega del todo se
    quitan. `skills_dir` es una carpeta de skills de tu `source`
    (`app/<skills_dir>`) que Hermes lee como directorio externo, de sólo
    lectura.

    Coste y activación
    -------------------
    Activa: pasar `HermesRuntime()` o `runtime="hermes"` a `AgentTemplate`,
        `AgentProject` o `sbx.agent.run`.
    Recursos y llamadas AWS: ninguna por sí solo; la imagen que lo instala
        se construye con `AgentTemplate.build` (un build de imagen) y cada
        ejecución llama al modelo de la pasarela que elijas.
    Coste aproximado: el del build de la imagen (ver `AgentTemplate`) más el
        del modelo; Hermes suma unos 0,5 GB (CPython 3.14, venv y fuente),
        así que la imagen es más grande y arranca más despacio que la de
        Strands (se mide en `AWS_API_NOTES.md` al publicar).
    IAM: la política `RayitoTemplateBuilder` para construir; ninguna para
        ejecutar (la pasarela guarda la clave).
    Cómo apagarla: no incluirlo en `runtimes` ni en el proyecto.
    Ejemplo:
        AgentTemplate(runtimes=(HermesRuntime(),))
    """

    name = HERMES_NAME
    state_name = HERMES_NAME

    def __init__(
        self,
        toolsets: Sequence[str] = HERMES_DEFAULT_TOOLSETS,
        skills_dir: str | None = None,
    ) -> None:
        if isinstance(toolsets, str):
            raise InvalidArgumentException("HermesRuntime.toolsets debe ser una secuencia")
        names = tuple(toolsets)
        if not names or len(set(names)) != len(names):
            raise InvalidArgumentException(
                "HermesRuntime.toolsets no puede ser vacío ni repetir un nombre"
            )
        for toolset in names:
            if not isinstance(toolset, str) or not _TOOLSET_PATTERN.fullmatch(toolset):
                raise InvalidArgumentException(
                    "cada toolset de HermesRuntime debe ser un nombre [a-z][a-z0-9_-]*"
                )
        if skills_dir is not None and (
            not isinstance(skills_dir, str)
            or not _SKILLS_DIR_PATTERN.fullmatch(skills_dir)
            or ".." in skills_dir.split("/")
        ):
            raise InvalidArgumentException(
                "HermesRuntime.skills_dir es una ruta relativa dentro de source"
            )
        self.toolsets = names
        self.skills_dir = skills_dir

    def __repr__(self) -> str:
        return f"HermesRuntime(toolsets={self.toolsets!r}, skills_dir={self.skills_dir!r})"

    @classmethod
    def from_options(cls, options: Mapping[str, object]) -> HermesRuntime:
        """El runtime de la tabla `[runtime]` de `rayito-agent.toml`."""
        toolsets = options.get("toolsets", HERMES_DEFAULT_TOOLSETS)
        skills_dir = options.get("skills_dir")
        if not isinstance(toolsets, (list, tuple)) or any(not isinstance(t, str) for t in toolsets):
            raise InvalidArgumentException("runtime.toolsets debe ser una lista de cadenas")
        if skills_dir is not None and not isinstance(skills_dir, str):
            raise InvalidArgumentException("runtime.skills_dir debe ser una cadena")
        return cls(toolsets=tuple(toolsets), skills_dir=skills_dir)

    def to_options(self) -> dict[str, object]:
        """La tabla `[runtime]` canónica."""
        options: dict[str, object] = {"kind": HERMES_NAME}
        if self.toolsets != HERMES_DEFAULT_TOOLSETS:
            options["toolsets"] = list(self.toolsets)
        if self.skills_dir is not None:
            options["skills_dir"] = self.skills_dir
        return options

    def host_python(self) -> str:
        """El host (stdlib) corre con el Python de Hermes: es el único
        intérprete garantizado en la imagen que ya lo trae."""
        return HERMES_PYTHON

    def flag_envs(self) -> Mapping[str, str]:
        return {**PYTHON_FLAG_ENVS, **HERMES_ENVS}

    def wiring_envs(self, spec: AgentSpec, urls: Mapping[str, str] | None) -> Mapping[str, str]:
        """Bedrock y Anthropic por sus variables estándar (el marcador como
        clave y la pasarela como endpoint); `provider: custom` lo lleva la
        configuración, y las variables de OpenAI no se exportan para no
        confundir la resolución de proveedor de Hermes."""
        if spec.model.provider in HERMES_ENV_PROVIDERS:
            return standard_model_envs(spec, urls)
        return {}

    def host_setup(
        self, spec: AgentSpec, urls: Mapping[str, str] | None, workdir: str
    ) -> HostSetup:
        entry = require_supported_provider(spec, HERMES_PROVIDERS, "el runtime 'hermes'")
        client = entry.client
        if urls is None and spec.model.provider == "openai-compatible":
            raise InvalidArgumentException(
                "el proveedor 'openai-compatible' no tiene un endpoint estándar: "
                "sin pasarela necesita una URL explícita"
            )
        if urls is None and spec.model.provider == "openai":
            client = HERMES_DIRECT_OPENAI_CLIENT
        base_url = provider_base_url(spec, entry, urls) if client == "custom" else None
        skills = None if self.skills_dir is None else f"{AGENT_APP_DIR}/{self.skills_dir}"
        config: dict[str, object] = {
            "v": AGENT_PROTOCOL_VERSION,
            "provider": spec.model.provider,
            "client": client,
            "model": spec.model.id,
            "region": spec.model.region,
            "base_url": base_url,
            "credential_placeholder": MODEL_CREDENTIAL_PLACEHOLDER if base_url else None,
            "workdir": workdir,
            "state_dir": HERMES_STATE_DIR,
            "home_dir": HERMES_HOME_DIR,
            "toolsets": effective_toolsets(spec, self.toolsets),
            "skills_dir": skills,
            "instructions": spec.instructions,
            "python": HERMES_PYTHON,
            "source_dir": HERMES_SOURCE_DIR,
            "api_port": AGENT_HERMES_API_PORT,
        }
        return HostSetup(driver="hermes", config=config)

    def install_steps(self, target: InstallTarget) -> Sequence[TemplateStep]:
        """CPython 3.14 independiente (sha256 fijado), venv con
        `--require-hashes` y el checkout del tag verificado contra su
        commit y recortado."""
        hermes_dir = f"{target.install_dir}/{HERMES_NAME}"
        requirements = f"{target.install_dir}/{HERMES_REQUIREMENTS_NAME}"
        interpreter = dataclasses.replace(target, python=f"{hermes_dir}/python/bin/python3")
        steps = list(container_packages_step(target, ("curl", "ca-certificates", "git")))
        steps += [
            run_step(
                f"mkdir -p {hermes_dir}"
                f" && curl -fsSL --retry 3 -o {PYTHON_TARBALL_PATH} {HERMES_PYTHON_TARBALL_URL}"
                f" && echo '{AGENT_HERMES_PYTHON_SHA256}  {PYTHON_TARBALL_PATH}' | sha256sum -c -"
                f" && tar -xzf {PYTHON_TARBALL_PATH} -C {hermes_dir}"
                f" && rm -f {PYTHON_TARBALL_PATH}"
            ),
            copy_step(HERMES_REQUIREMENTS_NAME, requirements),
            run_step(
                hashed_venv_install(
                    interpreter,
                    venv_dir=f"{hermes_dir}/venv",
                    requirements_path=requirements,
                    requirements_sha256=AGENT_HERMES_REQUIREMENTS_SHA256,
                )
            ),
            run_step(self._checkout_command(hermes_dir)),
        ]
        return steps

    @staticmethod
    def _checkout_command(hermes_dir: str) -> str:
        source = f"{hermes_dir}/src"
        pruned = " ".join(HERMES_PRUNED_PATHS)
        return (
            f"git clone --depth 1 --branch v{AGENT_HERMES_VERSION} {HERMES_REPOSITORY_URL} {source}"
            f' && test "$(git -C {source} rev-parse HEAD)" = {AGENT_HERMES_COMMIT}'
            f" && cd {source} && rm -rf {pruned}"
        )

    def context_files(self) -> Mapping[str, bytes]:
        return {HERMES_REQUIREMENTS_NAME: hermes_requirements()}

    def manifest_entry(self) -> Mapping[str, object]:
        """Versión, commit, CPython y hashes de lo instalado."""
        return {
            "version": AGENT_HERMES_VERSION,
            "commit": AGENT_HERMES_COMMIT,
            "python_version": AGENT_HERMES_PYTHON_VERSION,
            "python_sha256": AGENT_HERMES_PYTHON_SHA256,
            "requirements_sha256": AGENT_HERMES_REQUIREMENTS_SHA256,
            "requirements_file_sha256": hashlib.sha256(hermes_requirements()).hexdigest(),
        }

    def smoke_checks(self) -> Sequence[str]:
        """Importa Hermes y su API HTTP como `user`."""
        return (f'su user -c "{HERMES_SMOKE_COMMAND}"',)

    def prefetch_paths(self) -> Sequence[str]:
        """Ninguna: se carga con `warmup_steps()`."""
        return ()

    def warmup_steps(self) -> Sequence[WarmupStep]:
        """Carga Hermes en la caché de páginas."""
        source = shell_quote(HERMES_SOURCE_DIR)
        return (
            WarmupStep(
                cmd=f"cd {source} && PYTHONPATH={source} {shell_quote(HERMES_PYTHON)} -P -c "
                "'import hermes_cli.main' >/dev/null 2>&1 || true"
            ),
        )


__all__ = [
    "HERMES_DEFAULT_TOOLSETS",
    "HERMES_ENVS",
    "HERMES_NAME",
    "HERMES_PERMISSION_TOOLSETS",
    "HERMES_PROVIDERS",
    "HermesRuntime",
    "effective_toolsets",
    "hermes_requirements",
]
