"""Adaptador de deepagents del puerto `AgentRuntime` (`ai-agent-deepagents`,
ADR-025, design.md §3). Puro: escribe la configuración JSON que lee el
runner (`_runner/deepagents_runner.py`, que la plantilla instala en
`DEEPAGENTS_RUNNER_PATH`), construye el script de una ejecución y traduce
el protocolo JSONL v1 de Rayito a eventos.

A diferencia de OpenCode, el runner ya habla en eventos de Rayito
(`text_delta`, `text`, `reasoning`, `tool_call`, `step_started`,
`step_finished`, `agent_failed`, `done`) más `session`; el adaptador sólo
los valida, recorta y acumula. El script escribe además `rayito.busy` y
`rayito.runtime_missing`, como el de OpenCode.

Los permisos de `AgentSpec` se traducen a los nombres de herramienta de
deepagents (`DEEPAGENTS_TOOL_NAMES`). Sólo `bash` admite patrones (sobre el
comando de `execute`); `mcp` y `raw_config` no existen en
este runtime y fallan con `InvalidArgumentException` antes de cualquier
RPC.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Final

from rayito._agent._domain import (
    DEFAULT_DENIED_TOOLS,
    AgentPermissions,
    AgentSpec,
    validate_model_id,
)
from rayito._agent._events import AgentEvent, AgentFailed, Done
from rayito._agent._opencode import (
    AGENT_RUN_LOCK_PATH,
    canonical_json,
    files_sha256,
    shell_quote,
)
from rayito._agent._protocol_v1 import (
    SESSION_ID_PATTERN,
    ProtocolV1State,
    finish_v1,
    parse_v1_line,
)
from rayito._agent._recipes import (
    AGENT_APP_DIR,
    AGENT_INSTALL_DIR,
    AGENT_RAYITO_DIR,
    asset_bytes,
    hashed_venv_install,
    runner_bytes,
)
from rayito._agent._runtime import (
    DirectWiring,
    HostConfig,
    InstallTarget,
    ModelWiring,
    RunCommand,
    RunRequest,
    RuntimeFile,
    RuntimeFiles,
    RuntimeState,
    TemplateStep,
    WarmupStep,
    copy_step,
    run_step,
    wiring_urls,
)
from rayito._limits import (
    AGENT_DEEPAGENTS_REQUIREMENTS_SHA256,
    AGENT_PROTOCOL_VERSION,
    AGENT_STATE_DIR,
    DEFAULT_AGENT_WORKDIR,
    MODEL_CREDENTIAL_PLACEHOLDER,
)
from rayito.exceptions import InvalidArgumentException

#: Estado de deepagents dentro de `AGENT_STATE_DIR`: configuración y sesiones.
DEEPAGENTS_STATE_DIR: Final = f"{AGENT_STATE_DIR}/deepagents"
#: La configuración estática que lee el runner (su único argumento).
DEEPAGENTS_CONFIG_PATH: Final = f"{DEEPAGENTS_STATE_DIR}/config.json"
#: Historial de cada sesión, `<id>.json` con `messages_to_dict`.
DEEPAGENTS_SESSIONS_DIR: Final = f"{DEEPAGENTS_STATE_DIR}/sessions"
#: El venv de deepagents de la plantilla y su Python
#: (`dev/local/agent/Dockerfile`, docs/research/2026-10-agent-spike.md).
DEEPAGENTS_VENV_DIR: Final = f"{AGENT_INSTALL_DIR}/deepagents"
DEEPAGENTS_PYTHON: Final = f"{DEEPAGENTS_VENV_DIR}/bin/python"
DEEPAGENTS_REQUIREMENTS_NAME: Final = "requirements-deepagents.txt"
#: El runner (dato de paquete de `_agent/_runner/`) en el contexto de build.
DEEPAGENTS_RUNNER_NAME: Final = "deepagents_runner.py"
#: Donde la plantilla instala el runner, de root y 0755 (design.md §3).
DEEPAGENTS_RUNNER_PATH: Final = f"{AGENT_RAYITO_DIR}/{DEEPAGENTS_RUNNER_NAME}"
#: Lo que el build importa como prueba de humo del venv: deepagents y las
#: clases de modelo de cada proveedor (`langchain_openai` para OpenAI,
#: xAI, Azure y las APIs compatibles; `langchain_google_genai` para Gemini).
DEEPAGENTS_SMOKE_MODULES: Final = (
    "deepagents, langchain_aws, langchain_anthropic, langchain_openai, langchain_google_genai"
)
#: Nombre de herramienta de `AgentPermissions` (el vocabulario de OpenCode)
#: -> herramientas de deepagents 0.7 (`create_deep_agent`).
DEEPAGENTS_TOOL_NAMES: Final[Mapping[str, tuple[str, ...]]] = {
    "read": ("read_file",),
    "edit": ("write_file", "edit_file", "delete"),
    "list": ("ls",),
    "glob": ("glob",),
    "grep": ("grep",),
    "bash": ("execute",),
    "task": ("task",),
    "todowrite": ("write_todos",),
}
#: La única herramienta cuyos patrones tienen sentido: se comparan con el
#: comando de `execute` (`fnmatch`).
PATTERN_TOOL: Final = "bash"
#: Variables de cada ejecución: sin `.pyc` en el directorio del usuario y
#: con el stdout sin búfer (los eventos llegan en cuanto ocurren).
DEEPAGENTS_FLAG_ENVS: Final[Mapping[str, str]] = {
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
}
#: `pkg.mod:build`: un módulo importable desde el directorio de trabajo y
#: una función que recibe un `RunnerContext` y devuelve un grafo compilado.
#: Lo que la clase de LangChain espera en `base_url` con los proveedores
#: nativos: `ChatOpenAI` y `AzureChatOpenAI` añaden `/responses` y
#: `ChatGoogleGenerativeAI` (`google-genai`) añade `/v1beta/models/...`
#: por su cuenta (`testdata/agent/provider-catalogue.json`, comprobado
#: contra una pasarela falsa en docs/research/2026-10-provider-runtimes-spike.md).
#: Con `"openai-compatible"` se usa `AgentModel.base_path`.
DEEPAGENTS_NATIVE_BASE_PATHS: Final[Mapping[str, str]] = {
    "openai": "/v1",
    "google": "",
    "azure": "/openai/v1",
}
_ENTRYPOINT_PATTERN: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*")


def _tool_rules(permissions: AgentPermissions, label: str) -> dict[str, object]:
    tools: dict[str, object] = {}
    for tool, rule in permissions.effective_tools().items():
        if tool in DEFAULT_DENIED_TOOLS and tool not in permissions.tools:
            continue
        names = DEEPAGENTS_TOOL_NAMES.get(tool)
        if names is None:
            raise InvalidArgumentException(
                f"{label}: deepagents no tiene la herramienta {tool!r}; usa una de "
                f"{sorted(DEEPAGENTS_TOOL_NAMES)}"
            )
        if not isinstance(rule, str) and tool != PATTERN_TOOL:
            raise InvalidArgumentException(
                f"{label}: con deepagents sólo {PATTERN_TOOL!r} admite patrones"
            )
        for name in names:
            tools[name] = rule if isinstance(rule, str) else dict(rule)
    return {"default": permissions.default, "tools": tools}


def build_deepagents_config(
    spec: AgentSpec,
    *,
    gateway_urls: Mapping[str, str] | None,
    workdir: str,
    entrypoint: str | None,
) -> dict[str, object]:
    """La configuración que lee el runner, como diccionario.
    `gateway_urls=None` es la configuración sin pasarela de un contenedor
    exportado: `base_url` y `credential_placeholder` van a `null` y el
    proveedor usa su endpoint y credencial estándar."""
    base_url = _base_url(spec, gateway_urls)
    if spec.mcp:
        raise InvalidArgumentException("deepagents no admite AgentSpec.mcp; usa runtime='opencode'")
    if spec.raw_config:
        raise InvalidArgumentException(
            "AgentSpec.raw_config es configuración de OpenCode; deepagents no la admite"
        )
    model = spec.model
    subagents: list[dict[str, object]] = []
    for name, sub in spec.agents.items():
        subagents.append(
            {
                "name": name,
                "description": sub.description,
                "system_prompt": sub.instructions,
                "model": sub.model,
                "permissions": None
                if sub.permissions is None
                else _tool_rules(sub.permissions, f"AgentSpec.agents[{name!r}].permissions"),
            }
        )
    return {
        "v": AGENT_PROTOCOL_VERSION,
        "provider": model.provider,
        "model": model.id,
        "region": model.region,
        "base_url": base_url,
        "credential_placeholder": MODEL_CREDENTIAL_PLACEHOLDER if base_url is not None else None,
        "prompt_caching": model.prompt_caching,
        "instructions": spec.instructions,
        "workdir": workdir,
        "app_dir": AGENT_APP_DIR,
        "sessions_dir": DEEPAGENTS_SESSIONS_DIR,
        "entrypoint": entrypoint,
        "permissions": _tool_rules(spec.permissions, "AgentSpec.permissions"),
        "subagents": subagents,
    }


def _base_url(spec: AgentSpec, gateway_urls: Mapping[str, str] | None) -> str | None:
    model = spec.model
    if gateway_urls is None:
        if model.provider not in DEEPAGENTS_NATIVE_BASE_PATHS and model.provider not in (
            "bedrock",
            "anthropic",
        ):
            raise InvalidArgumentException(
                f"el proveedor {model.provider!r} no tiene un endpoint estándar: "
                "sin pasarela necesita una URL explícita"
            )
        return None
    spec.require_gateways(gateway_urls.keys())
    base_path = DEEPAGENTS_NATIVE_BASE_PATHS.get(model.provider, model.base_path)
    return gateway_urls[model.gateway].rstrip("/") + base_path


def _protocol_line(kind: str) -> str:
    return f'printf \'%s\\n\' \'{{"v":{AGENT_PROTOCOL_VERSION},"type":"rayito.{kind}"}}\''


def _run_script(config_path: str) -> str:
    lines = [
        "set -u",
        f"mkdir -p {shell_quote(DEEPAGENTS_SESSIONS_DIR)}",
        f"exec 9>{shell_quote(AGENT_RUN_LOCK_PATH)}",
        f"if ! flock -n 9; then {_protocol_line('busy')}; exit 0; fi",
        f"if [ ! -x {shell_quote(DEEPAGENTS_PYTHON)} ] || "
        f"[ ! -r {shell_quote(DEEPAGENTS_RUNNER_PATH)} ]; then "
        f"{_protocol_line('runtime_missing')}; exit 0; fi",
        f"exec {shell_quote(DEEPAGENTS_PYTHON)} {shell_quote(DEEPAGENTS_RUNNER_PATH)} "
        f"{shell_quote(config_path)}",
    ]
    return "\n".join(lines) + "\n"


class DeepAgents:
    """`AgentRuntime` de deepagents (`runtime=DeepAgents(...)` o
    `runtime="deepagents"`).

    Sin `entrypoint`, el runner construye `create_deep_agent(model,
    system_prompt=instructions, subagents, backend=LocalShellBackend(
    root_dir=workdir), middleware=ctx.middleware)`. Con `entrypoint=
    "pkg.mod:build"`, importa ese módulo desde el directorio de trabajo y
    llama a `build(ctx)` con un `RunnerContext(model, instructions,
    subagents, backend, middleware, workdir)`; debe devolver un grafo
    compilado. Si no pasa `ctx.middleware` a su grafo, se pierden los
    permisos de `AgentSpec`.

    `require_entrypoint=True` es el runtime registrado como `"langgraph"`: el
    mismo adaptador, pero sin `entrypoint` no hay agente que construir y
    `build_config()` / `host_config()` fallan con `InvalidArgumentException`.

    El modelo siempre llega por la pasarela (`ChatBedrockConverse`,
    `ChatAnthropic` o `ChatOpenAI` con la URL de la pasarela y un marcador
    como clave). deepagents marca puntos de caché de prompts para Claude
    (Bedrock y Anthropic); `AgentModel(prompt_caching=False)` lo apaga."""

    name: str = "deepagents"

    def __init__(self, entrypoint: str | None = None, *, require_entrypoint: bool = False) -> None:
        if entrypoint is not None and (
            not isinstance(entrypoint, str) or not _ENTRYPOINT_PATTERN.fullmatch(entrypoint)
        ):
            raise InvalidArgumentException(
                "DeepAgents.entrypoint debe tener la forma 'paquete.modulo:funcion'"
            )
        self.entrypoint = entrypoint
        self.require_entrypoint = require_entrypoint

    def __repr__(self) -> str:
        return f"DeepAgents(entrypoint={self.entrypoint!r})"

    def build_config(
        self, spec: AgentSpec, *, gateway_urls: Mapping[str, str], workdir: str
    ) -> RuntimeFiles:
        return self._config_files(spec, gateway_urls, workdir)

    def _config_files(
        self, spec: AgentSpec, gateway_urls: Mapping[str, str] | None, workdir: str
    ) -> RuntimeFiles:
        if self.require_entrypoint and self.entrypoint is None:
            raise InvalidArgumentException(
                "el runtime 'langgraph' necesita un entrypoint 'paquete.modulo:funcion'"
            )
        config = build_deepagents_config(
            spec, gateway_urls=gateway_urls, workdir=workdir, entrypoint=self.entrypoint
        )
        files = [RuntimeFile(DEEPAGENTS_CONFIG_PATH, canonical_json(config), 0o600)]
        return RuntimeFiles(files=tuple(files), config_sha256=files_sha256(files))

    def command(self, request: RunRequest) -> RunCommand:
        """El script (cerrojo, comprobación del runner, `exec` del Python
        del venv) y la petición JSON por stdin."""
        if request.session_id is not None and not SESSION_ID_PATTERN.fullmatch(request.session_id):
            raise InvalidArgumentException("session_id de deepagents debe tener la forma rda_<hex>")
        if request.model is not None:
            validate_model_id(request.model, "model")
        payload = {
            "v": AGENT_PROTOCOL_VERSION,
            "prompt": request.prompt,
            "session_id": request.session_id,
            "model": request.model,
            "reasoning": request.reasoning,
        }
        return RunCommand(
            script=_run_script(DEEPAGENTS_CONFIG_PATH),
            envs=_run_envs(request.spec),
            stdin=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        )

    def new_state(self) -> RuntimeState:
        return ProtocolV1State()

    def parse_line(self, line: bytes, state: RuntimeState) -> Sequence[AgentEvent]:
        """Una línea del protocolo v1 a cero o un evento. Lo que no es JSON,
        otra versión o un tipo desconocido se ignora y se cuenta."""
        assert isinstance(state, ProtocolV1State)
        return parse_v1_line(line, state)

    def finish(self, state: RuntimeState, exit_code: int) -> Done | AgentFailed:
        """`Done` sólo con salida 0, el `done` del runner y una sesión."""
        assert isinstance(state, ProtocolV1State)
        return finish_v1(state, exit_code)

    def template_steps(self) -> Sequence[TemplateStep]:
        """Vacío: la plantilla de agente (`ai-agent-fast-start`) declara sus
        propios pasos."""
        return ()

    def warmup_steps(self) -> Sequence[WarmupStep]:
        """Carga deepagents y langchain en la caché de páginas."""
        return (
            WarmupStep(
                cmd=f"{shell_quote(DEEPAGENTS_PYTHON)} -c 'import deepagents, langchain_aws' "
                ">/dev/null 2>&1 || true"
            ),
        )

    def install_steps(self, target: InstallTarget) -> Sequence[TemplateStep]:
        """Copia los requisitos con hash, crea el venv con `--require-hashes`
        y copia el runner (0755)."""
        requirements = f"{target.install_dir}/{DEEPAGENTS_REQUIREMENTS_NAME}"
        return (
            copy_step(DEEPAGENTS_REQUIREMENTS_NAME, requirements),
            run_step(
                hashed_venv_install(
                    target,
                    venv_dir=f"{target.install_dir}/deepagents",
                    requirements_path=requirements,
                    requirements_sha256=AGENT_DEEPAGENTS_REQUIREMENTS_SHA256,
                )
            ),
            copy_step(
                DEEPAGENTS_RUNNER_NAME,
                f"{target.install_dir}/rayito/{DEEPAGENTS_RUNNER_NAME}",
                executable=True,
            ),
        )

    def context_files(self) -> Mapping[str, bytes]:
        """Los requisitos con hash y el runner (datos de paquete)."""
        return {
            DEEPAGENTS_REQUIREMENTS_NAME: deepagents_requirements(),
            DEEPAGENTS_RUNNER_NAME: deepagents_runner(),
        }

    def manifest_entry(self) -> Mapping[str, object]:
        """El sha256 de los requisitos y el del runner, para el manifiesto."""
        return {
            "requirements_sha256": AGENT_DEEPAGENTS_REQUIREMENTS_SHA256,
            "runner_sha256": hashlib.sha256(deepagents_runner()).hexdigest(),
        }

    def smoke_checks(self) -> Sequence[str]:
        """Importa deepagents y los modelos de cada proveedor, y comprueba
        que el runner es legible, todo como `user`."""
        return (
            f"su user -c \"{DEEPAGENTS_PYTHON} -c 'import {DEEPAGENTS_SMOKE_MODULES}'\"",
            f"su user -c 'test -r {DEEPAGENTS_RUNNER_PATH}'",
        )

    def prefetch_paths(self) -> Sequence[str]:
        """Ninguna: el venv se carga con `warmup_steps()`."""
        return ()

    def host_config(self, spec: AgentSpec, *, wiring: ModelWiring, workdir: str) -> HostConfig:
        """La configuración del host para el driver `deepagents`: el mismo
        fichero de configuración y las mismas variables que `command()`."""
        return HostConfig(
            driver="deepagents",
            python=DEEPAGENTS_PYTHON,
            files=self._config_files(spec, wiring_urls(wiring), workdir),
            envs=_run_envs(spec, direct=isinstance(wiring, DirectWiring)),
            options={"config_path": DEEPAGENTS_CONFIG_PATH},
        )


def deepagents_requirements() -> bytes:
    """El fichero de requisitos con hash del venv de deepagents (dato de
    paquete; su sha256 está en `limits.json`)."""
    return asset_bytes(DEEPAGENTS_REQUIREMENTS_NAME)


def deepagents_runner() -> bytes:
    """El runner de deepagents que la plantilla instala (su sha256 va en el
    manifiesto como `runner_sha256`)."""
    return runner_bytes(DEEPAGENTS_RUNNER_NAME)


def _run_envs(spec: AgentSpec, *, direct: bool = False) -> dict[str, str]:
    envs = {"HOME": DEFAULT_AGENT_WORKDIR, **DEEPAGENTS_FLAG_ENVS}
    if spec.model.provider == "bedrock" and not direct:
        envs["AWS_BEARER_TOKEN_BEDROCK"] = MODEL_CREDENTIAL_PLACEHOLDER
    if spec.model.region is not None:
        envs["AWS_REGION"] = spec.model.region
    return envs


__all__ = [
    "DEEPAGENTS_CONFIG_PATH",
    "DEEPAGENTS_RUNNER_PATH",
    "DEEPAGENTS_SESSIONS_DIR",
    "DEEPAGENTS_TOOL_NAMES",
    "DeepAgents",
    "build_deepagents_config",
    "deepagents_requirements",
    "deepagents_runner",
]
