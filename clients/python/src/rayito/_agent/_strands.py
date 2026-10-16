"""Adaptador de Strands Agents (`strands-agents` y `strands-harness`, Python
y TypeScript) de los puertos `AgentRuntime`, `InstallableRuntime` y
`ServableRuntime` (ADR-028, D2, D7 y D9). Puro: escribe la configuración que
lee el driver del host (`strands` para Python y el harness, `strands-ts`
para el puente de Node), compone la receta de instalación y deja que
`_host_runtime.HostBackedRuntime` arme el contrato de tareas.

Qué hace cada variante:

- **Strands (Python)**: sin `entrypoint` el host construye `Agent(model=...,
  system_prompt=instrucciones, tools=[])`; con `entrypoint="modulo:atributo"`
  importa tu `Agent` (o la función `fn(ctx) -> Agent`, que recibe el modelo ya
  cableado a la pasarela en `ctx.model`) desde `AGENT_APP_DIR`.
- **Harness**: `create_harness(...)` con las herramientas integradas que
  permiten los `AgentPermissions`; `programmatic_tool_caller` y `web_search`
  van siempre apagadas, y la sesión y la memoria viven bajo
  `AGENT_STATE_DIR/strands`.
- **TypeScript**: tu `Agent` de `@strands-agents/sdk` (módulo `entrypoint`,
  que exporta `agent` o `createAgent(ctx)`) corre en Node a través de
  `strands_bridge.mjs`; las dependencias son las de tu `package-lock.json`.

El modelo llega siempre por la pasarela de secretos con un marcador como
clave, nunca con credenciales de AWS que el agente pudiera leer (`AWS_*`).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Final, Literal

from rayito._agent._domain import AgentSpec
from rayito._agent._host_runtime import (
    PYTHON_FLAG_ENVS,
    HostBackedRuntime,
    HostSetup,
    ProviderClient,
    container_packages_step,
    permission_rules,
    provider_base_url,
    require_supported_provider,
    standard_model_envs,
    tool_allowed,
    user_requirements_steps,
)
from rayito._agent._opencode import shell_quote
from rayito._agent._recipes import (
    AGENT_APP_DIR,
    AGENT_INSTALL_DIR,
    AGENT_RAYITO_DIR,
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
    AGENT_NODE_SHA256,
    AGENT_NODE_VERSION,
    AGENT_PROTOCOL_VERSION,
    AGENT_STATE_DIR,
    AGENT_STRANDS_HARNESS_REQUIREMENTS_SHA256,
    AGENT_STRANDS_HARNESS_VERSION,
    AGENT_STRANDS_REQUIREMENTS_SHA256,
    AGENT_STRANDS_TS_HARNESS_VERSION,
    AGENT_STRANDS_TS_VERSION,
    AGENT_STRANDS_VERSION,
    MODEL_CREDENTIAL_PLACEHOLDER,
)
from rayito.exceptions import InvalidArgumentException

StrandsLanguage = Literal["python", "typescript"]
STRANDS_LANGUAGES: Final[tuple[str, ...]] = ("python", "typescript")
#: Nombres con los que el registro y el telemetría conocen al adaptador.
STRANDS_NAME: Final = "strands"
STRANDS_HARNESS_NAME: Final = "strands-harness"
#: Estado del runtime dentro de `AGENT_STATE_DIR` (sesiones y memoria).
STRANDS_STATE_NAME: Final = "strands"
STRANDS_STATE_DIR: Final = f"{AGENT_STATE_DIR}/{STRANDS_STATE_NAME}"
STRANDS_SESSIONS_DIR: Final = f"{STRANDS_STATE_DIR}/sessions"
STRANDS_MEMORY_DIR: Final = f"{STRANDS_STATE_DIR}/memory"
#: Venvs de la plantilla (`/opt/agents/strands` y `/opt/agents/strands-harness`).
STRANDS_VENV_NAME: Final = "strands"
STRANDS_HARNESS_VENV_NAME: Final = "strands-harness"
STRANDS_REQUIREMENTS_NAME: Final = "requirements-strands.txt"
STRANDS_HARNESS_REQUIREMENTS_NAME: Final = "requirements-strands-harness.txt"
#: El puente de Node (dato de paquete) y dónde lo instala la plantilla.
STRANDS_BRIDGE_NAME: Final = "strands_bridge.mjs"
STRANDS_BRIDGE_PATH: Final = f"{AGENT_RAYITO_DIR}/{STRANDS_BRIDGE_NAME}"
#: Node: tarball oficial linux arm64 (SHASUMS256 de nodejs.org) en `node/`.
NODE_DIR_NAME: Final = "node"
NODE_TARBALL_URL: Final = (
    f"https://nodejs.org/dist/v{AGENT_NODE_VERSION}/node-v{AGENT_NODE_VERSION}-linux-arm64.tar.gz"
)
#: El `package.json` y el lockfile del usuario (`app/` del contexto de build)
#: que `npm ci` instala junto a Node, en `<install_dir>/` para que Node los
#: resuelva desde `AGENT_APP_DIR` y desde el puente.
NODE_PACKAGE_FILES: Final[tuple[str, ...]] = ("package.json", "package-lock.json")
#: Lo que el build importa como prueba de humo de cada venv.
STRANDS_SMOKE_MODULES: Final = "strands, boto3, anthropic, openai, google.genai"
STRANDS_HARNESS_SMOKE_MODULES: Final = f"{STRANDS_SMOKE_MODULES}, strands_harness"
TS_SMOKE_MODULE: Final = "@strands-agents/sdk"
#: Telemetría de Strands apagada: sin exportador no sale nada del sandbox
#: (`strands.telemetry` sólo exporta si el código del usuario lo configura).
STRANDS_TELEMETRY_ENVS: Final[Mapping[str, str]] = {
    "OTEL_SDK_DISABLED": "true",
    "OTEL_TRACES_EXPORTER": "none",
    "OTEL_METRICS_EXPORTER": "none",
    "OTEL_LOGS_EXPORTER": "none",
}
#: Herramientas integradas del harness (`BUILTIN_TOOL_NAMES`, comprobadas en
#: `testdata/agent/strands-spike.json`) -> nombre de herramienta de
#: `AgentPermissions` que las gobierna.
HARNESS_PERMISSION_TOOLS: Final[Mapping[str, tuple[str, ...]]] = {
    "bash": ("shell",),
    "read": ("read",),
    "edit": ("write", "edit"),
    "webfetch": ("web_fetch",),
    "task": ("subagent",),
}
#: Siempre apagadas: la primera ejecuta código que escribe el modelo y la
#: segunda manda las consultas a un tercero.
HARNESS_ALWAYS_OFF: Final[tuple[str, ...]] = ("programmatic_tool_caller", "web_search")
#: Proveedores por pasarela de Strands (Python y harness), medidos en
#: `testdata/agent/strands-spike.json`. `azure` queda fuera: la pasarela de
#: Azure inyecta `api-key` y estos clientes mandan `Authorization`.
STRANDS_PROVIDERS: Final[Mapping[str, ProviderClient]] = {
    "bedrock": ProviderClient("BedrockModel", ""),
    "anthropic": ProviderClient("AnthropicModel", ""),
    "openai": ProviderClient("OpenAIModel", "/v1"),
    "openai-compatible": ProviderClient("OpenAIModel", None),
    "google": ProviderClient("GeminiModel", ""),
}
#: Los de `@strands-agents/sdk` (TypeScript), medidos igual.
STRANDS_TS_PROVIDERS: Final[Mapping[str, ProviderClient]] = {
    "bedrock": ProviderClient("BedrockModel", ""),
    "anthropic": ProviderClient("AnthropicModel", ""),
    "openai": ProviderClient("OpenAIModel", "/v1"),
    "openai-compatible": ProviderClient("OpenAIModel", None),
    "google": ProviderClient("GoogleModel", ""),
}
_PYTHON_ENTRYPOINT_PATTERN: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*")
_TS_ENTRYPOINT_PATTERN: Final = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:ts|mts|js|mjs)")
_REQUIREMENTS_PATTERN: Final = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*")


def strands_requirements() -> bytes:
    """Requisitos con hash del venv de Strands (dato de paquete; su sha256
    está en `limits.json`)."""
    return asset_bytes(STRANDS_REQUIREMENTS_NAME)


def strands_harness_requirements() -> bytes:
    """Requisitos con hash del venv del harness de Strands."""
    return asset_bytes(STRANDS_HARNESS_REQUIREMENTS_NAME)


def strands_bridge() -> bytes:
    """El puente de Node que importa tu agente y habla el protocolo v1."""
    return asset_bytes(STRANDS_BRIDGE_NAME)


def harness_permission_aliases() -> dict[str, str]:
    """Nombre de herramienta integrada del harness -> herramienta de
    `AgentPermissions` que la gobierna (la inversa de
    `HARNESS_PERMISSION_TOOLS`), para que el gancho de permisos del driver
    aplique las reglas (y los patrones de `bash`) a `shell`, `write`..."""
    return {
        name: permission for permission, names in HARNESS_PERMISSION_TOOLS.items() for name in names
    }


def harness_builtin_tools(spec: AgentSpec) -> dict[str, bool]:
    """Las herramientas integradas del harness que `AgentPermissions`
    permite: parte de cero (`"*": False`), enciende las que gobierna una
    herramienta no denegada y apaga siempre `HARNESS_ALWAYS_OFF`."""
    tools: dict[str, bool] = {"*": False}
    for permission, names in HARNESS_PERMISSION_TOOLS.items():
        allowed = tool_allowed(spec.permissions, permission)
        for name in names:
            tools[name] = allowed
    for name in HARNESS_ALWAYS_OFF:
        tools[name] = False
    return tools


class StrandsRuntime(HostBackedRuntime):
    """`AgentRuntime` de Strands Agents (`runtime=StrandsRuntime(...)`;
    `"strands"` y `"strands-harness"` en el registro).

    `entrypoint` es `modulo:atributo` en Python (un `Agent` o una función
    `fn(ctx) -> Agent`) y la ruta de un módulo bajo `AGENT_APP_DIR` en
    TypeScript (obligatoria, y exporta `agent` o `createAgent(ctx)`).
    `harness=True` usa `create_harness` del paquete `strands-harness`
    (sólo con `language="python"`; en TypeScript el harness se importa en tu
    código). `requirements` es un fichero de requisitos de tu `source`
    (`app/<requirements>`) que se instala en el venv después de los pines de
    Rayito (sólo Python). Con `language="typescript"` el `source` debe llevar
    `package.json` y `package-lock.json`: el build ejecuta `npm ci
    --ignore-scripts` con ellos y falla si faltan.

    Coste y activación
    -------------------
    Activa: pasar `StrandsRuntime(...)` o `runtime="strands"` /
        `"strands-harness"` a `AgentTemplate`, `AgentProject` o
        `sbx.agent.run`.
    Recursos y llamadas AWS: ninguna por sí solo; la imagen que lo instala
        se construye con `AgentTemplate.build` (un build de imagen) y cada
        ejecución llama al modelo de la pasarela que elijas.
    Coste aproximado: el del build de la imagen (ver `AgentTemplate`) más el
        del modelo; el venv de Strands añade unos 0,3 GB al snapshot (medido
        en `AWS_API_NOTES.md` cuando se publique).
    IAM: la política `RayitoTemplateBuilder` para construir; ninguna para
        ejecutar (la pasarela guarda la clave).
    Cómo apagarla: no incluirlo en `runtimes` ni en el proyecto.
    Ejemplo:
        AgentTemplate(runtimes=(StrandsRuntime(entrypoint="agent:agent"),))
    """

    state_name = STRANDS_STATE_NAME

    def __init__(
        self,
        entrypoint: str | None = None,
        harness: bool = False,
        language: StrandsLanguage = "python",
        requirements: str | None = None,
    ) -> None:
        if language not in STRANDS_LANGUAGES:
            raise InvalidArgumentException(
                f"StrandsRuntime.language debe ser uno de {list(STRANDS_LANGUAGES)}"
            )
        if not isinstance(harness, bool):
            raise InvalidArgumentException("StrandsRuntime.harness debe ser un bool")
        if language == "typescript" and harness:
            raise InvalidArgumentException(
                "con language='typescript' el harness se importa en tu código "
                "(@strands-agents/harness); harness=True es sólo de Python"
            )
        self._check_entrypoint(entrypoint, language)
        if requirements is not None and (
            language == "typescript"
            or not _REQUIREMENTS_PATTERN.fullmatch(requirements)
            or ".." in requirements.split("/")
        ):
            raise InvalidArgumentException(
                "StrandsRuntime.requirements es una ruta relativa dentro de source "
                "y sólo existe con language='python'"
            )
        self.entrypoint = entrypoint
        self.harness = harness
        self.language: StrandsLanguage = language
        self.requirements = requirements
        self.name = STRANDS_HARNESS_NAME if harness else STRANDS_NAME

    @staticmethod
    def _check_entrypoint(entrypoint: str | None, language: str) -> None:
        if language == "typescript":
            if entrypoint is None or not _TS_ENTRYPOINT_PATTERN.fullmatch(entrypoint):
                raise InvalidArgumentException(
                    "StrandsRuntime(language='typescript') necesita un entrypoint con la "
                    "ruta de un módulo .ts/.js dentro de source, por ejemplo 'agent.ts'"
                )
            if ".." in entrypoint.split("/"):
                raise InvalidArgumentException("StrandsRuntime.entrypoint no puede salir de source")
            return
        if entrypoint is not None and not _PYTHON_ENTRYPOINT_PATTERN.fullmatch(entrypoint):
            raise InvalidArgumentException(
                "StrandsRuntime.entrypoint debe tener la forma 'paquete.modulo:atributo'"
            )

    def __repr__(self) -> str:
        return (
            f"StrandsRuntime(entrypoint={self.entrypoint!r}, harness={self.harness!r}, "
            f"language={self.language!r}, requirements={self.requirements!r})"
        )

    @classmethod
    def from_options(cls, kind: str, options: Mapping[str, object]) -> StrandsRuntime:
        """El runtime de la tabla `[runtime]` de `rayito-agent.toml`
        (`kind` es `"strands"` o `"strands-harness"`)."""
        entrypoint = options.get("entrypoint")
        language = options.get("language", "python")
        requirements = options.get("requirements")
        for label, value in (("entrypoint", entrypoint), ("requirements", requirements)):
            if value is not None and not isinstance(value, str):
                raise InvalidArgumentException(f"runtime.{label} debe ser una cadena")
        if not isinstance(language, str):
            raise InvalidArgumentException("runtime.language debe ser una cadena")
        return cls(
            entrypoint=entrypoint if isinstance(entrypoint, str) else None,
            harness=kind == STRANDS_HARNESS_NAME,
            language=language,  # type: ignore[arg-type]
            requirements=requirements if isinstance(requirements, str) else None,
        )

    def to_options(self) -> dict[str, object]:
        """La tabla `[runtime]` canónica: `kind` y sus opciones."""
        options: dict[str, object] = {"kind": self.name}
        if self.entrypoint is not None:
            options["entrypoint"] = self.entrypoint
        if self.language != "python":
            options["language"] = self.language
        if self.requirements is not None:
            options["requirements"] = self.requirements
        return options

    @property
    def _typescript(self) -> bool:
        return self.language == "typescript"

    @property
    def _venv_name(self) -> str:
        return STRANDS_HARNESS_VENV_NAME if self.harness else STRANDS_VENV_NAME

    def venv_python(self, install_dir: str = AGENT_INSTALL_DIR) -> str:
        return f"{install_dir}/{self._venv_name}/bin/python"

    def host_python(self) -> str:
        return "python3" if self._typescript else self.venv_python()

    def flag_envs(self) -> Mapping[str, str]:
        return {**PYTHON_FLAG_ENVS, **STRANDS_TELEMETRY_ENVS}

    def wiring_envs(self, spec: AgentSpec, urls: Mapping[str, str] | None) -> Mapping[str, str]:
        """Las variables estándar del proveedor apuntadas a la pasarela (ver
        `standard_model_envs`)."""
        return standard_model_envs(spec, urls)

    def host_setup(
        self, spec: AgentSpec, urls: Mapping[str, str] | None, workdir: str
    ) -> HostSetup:
        providers = STRANDS_TS_PROVIDERS if self._typescript else STRANDS_PROVIDERS
        entry = require_supported_provider(spec, providers, f"el runtime {self.name!r}")
        base_url = provider_base_url(spec, entry, urls)
        config: dict[str, object] = {
            "v": AGENT_PROTOCOL_VERSION,
            "provider": spec.model.provider,
            "client": entry.client,
            "model": spec.model.id,
            "region": spec.model.region,
            "base_url": base_url,
            "credential_placeholder": MODEL_CREDENTIAL_PLACEHOLDER if base_url else None,
            "prompt_caching": spec.model.prompt_caching,
            "instructions": spec.instructions,
            "workdir": workdir,
            "app_dir": AGENT_APP_DIR,
            "state_dir": STRANDS_STATE_DIR,
            "sessions_dir": STRANDS_SESSIONS_DIR,
            "entrypoint": self.entrypoint,
            "permissions": permission_rules(spec.permissions),
        }
        if self._typescript:
            config["bridge"] = STRANDS_BRIDGE_PATH
            return HostSetup(driver="strands-ts", config=config)
        config["harness"] = self.harness
        config["memory_dir"] = STRANDS_MEMORY_DIR
        config["builtin_tools"] = harness_builtin_tools(spec) if self.harness else None
        config["permission_aliases"] = harness_permission_aliases() if self.harness else {}
        return HostSetup(driver="strands", config=config)

    def install_steps(self, target: InstallTarget) -> Sequence[TemplateStep]:
        """Python: los requisitos con hash, el venv (`--require-hashes`) y,
        si hay, los del usuario. TypeScript: Node (sha256 fijado) y
        `npm ci --ignore-scripts` de tu lockfile, más el puente."""
        if self._typescript:
            return self._typescript_steps(target)
        name = STRANDS_HARNESS_REQUIREMENTS_NAME if self.harness else STRANDS_REQUIREMENTS_NAME
        sha256 = (
            AGENT_STRANDS_HARNESS_REQUIREMENTS_SHA256
            if self.harness
            else AGENT_STRANDS_REQUIREMENTS_SHA256
        )
        requirements = f"{target.install_dir}/{name}"
        venv_dir = f"{target.install_dir}/{self._venv_name}"
        steps = [
            copy_step(name, requirements),
            run_step(
                hashed_venv_install(
                    target,
                    venv_dir=venv_dir,
                    requirements_path=requirements,
                    requirements_sha256=sha256,
                )
            ),
        ]
        if self.requirements is not None:
            steps += user_requirements_steps(
                target, venv_dir=venv_dir, requirements=self.requirements, slug=self._venv_name
            )
        return steps

    def _typescript_steps(self, target: InstallTarget) -> list[TemplateStep]:
        node_dir = f"{target.install_dir}/{NODE_DIR_NAME}"
        node_bin = f"{node_dir}/bin"
        steps = list(container_packages_step(target, ("curl", "ca-certificates")))
        steps.append(
            run_step(
                f"curl -fsSL --retry 3 -o /tmp/node.tar.gz {NODE_TARBALL_URL}"
                f" && echo '{AGENT_NODE_SHA256}  /tmp/node.tar.gz' | sha256sum -c -"
                f" && mkdir -p {node_dir}"
                f" && tar -xzf /tmp/node.tar.gz -C {node_dir} --strip-components=1"
                f" && rm -f /tmp/node.tar.gz"
                f" && ln -sf {node_bin}/node /usr/local/bin/node"
            )
        )
        steps += [
            copy_step(f"app/{name}", f"{target.install_dir}/{name}") for name in NODE_PACKAGE_FILES
        ]
        steps.append(
            run_step(
                f"cd {target.install_dir} && PATH={node_bin}:$PATH"
                " npm ci --ignore-scripts --omit=dev --no-audit --no-fund"
            )
        )
        steps.append(copy_step(STRANDS_BRIDGE_NAME, STRANDS_BRIDGE_PATH))
        return steps

    def context_files(self) -> Mapping[str, bytes]:
        if self._typescript:
            return {STRANDS_BRIDGE_NAME: strands_bridge()}
        if self.harness:
            return {STRANDS_HARNESS_REQUIREMENTS_NAME: strands_harness_requirements()}
        return {STRANDS_REQUIREMENTS_NAME: strands_requirements()}

    def manifest_entry(self) -> Mapping[str, object]:
        """Versiones y hashes de lo instalado, para el manifiesto."""
        if self._typescript:
            return {
                "language": "typescript",
                "node_version": AGENT_NODE_VERSION,
                "node_sha256": AGENT_NODE_SHA256,
                "tested_sdk_version": AGENT_STRANDS_TS_VERSION,
                "tested_harness_version": AGENT_STRANDS_TS_HARNESS_VERSION,
                "bridge_sha256": hashlib.sha256(strands_bridge()).hexdigest(),
            }
        if self.harness:
            return {
                "language": "python",
                "version": AGENT_STRANDS_VERSION,
                "harness_version": AGENT_STRANDS_HARNESS_VERSION,
                "requirements_sha256": AGENT_STRANDS_HARNESS_REQUIREMENTS_SHA256,
            }
        return {
            "language": "python",
            "version": AGENT_STRANDS_VERSION,
            "requirements_sha256": AGENT_STRANDS_REQUIREMENTS_SHA256,
        }

    def smoke_checks(self) -> Sequence[str]:
        """Importa el SDK (y el harness) como `user`."""
        if self._typescript:
            return (
                "su user -c 'node --version'",
                f'su user -c "cd {AGENT_INSTALL_DIR} && node --input-type=module -e '
                f'\'import \\"{TS_SMOKE_MODULE}\\"\'"',
                f"su user -c 'test -r {STRANDS_BRIDGE_PATH}'",
            )
        modules = STRANDS_HARNESS_SMOKE_MODULES if self.harness else STRANDS_SMOKE_MODULES
        return (f"su user -c \"{self.venv_python()} -c 'import {modules}'\"",)

    def prefetch_paths(self) -> Sequence[str]:
        """El binario de Node (TypeScript); el venv de Python se carga con
        `warmup_steps()`."""
        return (f"{AGENT_INSTALL_DIR}/{NODE_DIR_NAME}/bin/node",) if self._typescript else ()

    def warmup_steps(self) -> Sequence[WarmupStep]:
        """Carga Strands (o Node con el SDK) en la caché de páginas."""
        if self._typescript:
            command = (
                f"cd {AGENT_INSTALL_DIR} && node --input-type=module "
                f"-e 'import \"{TS_SMOKE_MODULE}\"' >/dev/null 2>&1 || true"
            )
            return (WarmupStep(cmd=command),)
        return (
            WarmupStep(
                cmd=f"{shell_quote(self.venv_python())} -c 'import strands, boto3' "
                ">/dev/null 2>&1 || true"
            ),
        )


__all__ = [
    "HARNESS_ALWAYS_OFF",
    "HARNESS_PERMISSION_TOOLS",
    "STRANDS_BRIDGE_PATH",
    "STRANDS_HARNESS_NAME",
    "STRANDS_NAME",
    "STRANDS_PROVIDERS",
    "STRANDS_TS_PROVIDERS",
    "StrandsRuntime",
    "harness_builtin_tools",
    "strands_bridge",
    "strands_harness_requirements",
    "strands_requirements",
]
