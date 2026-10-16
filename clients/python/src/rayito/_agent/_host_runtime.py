"""Base de los runtimes que corren por el host del agente (ADR-028, D1 y D3):
Strands, Hermes y `CustomRuntime`. Cada uno aporta un driver del host, el
intérprete que lo ejecuta, su configuración y sus variables de entorno; esta
base arma con eso el contrato de tareas de `AgentRuntime` (`build_config`,
`command`, `parse_line`, `finish`) y el puerto `ServableRuntime`
(`host_config`), de modo que `sbx.agent.run` y `AgentProject.deploy` ejecutan
exactamente lo mismo.

Las variables que dependen de la pasarela (la URL de Bedrock, el marcador de
la clave) se escriben en un fichero `wiring.env` que el script de una
ejecución carga antes de lanzar el host, porque `command()` no conoce las URLs
de las pasarelas; en modo servicio las mismas variables van en
`HostConfig.envs`. Ningún secreto pasa por aquí: la clave es siempre el
marcador `MODEL_CREDENTIAL_PLACEHOLDER` y la real la pone la pasarela.

Puro: sin E/S de red ni reloj.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from rayito._agent._domain import AgentPermissions, AgentSpec, validate_model_id
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
from rayito._agent._recipes import AGENT_HOST_DIR
from rayito._agent._runtime import (
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
    AGENT_HOST_SCHEMA,
    AGENT_PROTOCOL_VERSION,
    AGENT_STATE_DIR,
    DEFAULT_AGENT_WORKDIR,
    MODEL_CREDENTIAL_PLACEHOLDER,
)
from rayito.exceptions import InvalidArgumentException

#: Modo `task` del host (`python agent_host task <host.json>`).
HOST_TASK_MODE: Final = "task"
CONFIG_FILE_NAME: Final = "config.json"
HOST_FILE_NAME: Final = "host.json"
WIRING_ENV_FILE_NAME: Final = "wiring.env"
#: Modo de los ficheros de configuración: sólo los lee el usuario del sandbox.
CONFIG_FILE_MODE: Final = 0o600
#: Variables de todo proceso Python del host: sin `.pyc` en el directorio del
#: usuario y con la salida sin búfer (los eventos llegan en cuanto ocurren).
PYTHON_FLAG_ENVS: Final[Mapping[str, str]] = {
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
}


@dataclass(frozen=True)
class HostSetup:
    """Lo que un runtime decide para el host: el driver, la configuración
    que escribe en `config.json` (o `None` si el driver recibe sus opciones
    en línea) y las opciones del driver. Con `config`, las opciones llevan
    además `config_path`."""

    driver: str
    config: Mapping[str, object] | None = None
    options: Mapping[str, object] = field(default_factory=dict)


def failure_line(kind: str) -> str:
    """El `printf` del script que escribe una línea `rayito.<kind>` del
    protocolo (`busy`, `runtime_missing`)."""
    return f'printf \'%s\\n\' \'{{"v":{AGENT_PROTOCOL_VERSION},"type":"rayito.{kind}"}}\''


def env_file_bytes(envs: Mapping[str, str]) -> bytes:
    """Un fichero `KEY='value'` por variable, ordenado, para
    `set -a; . fichero`. Los valores van entre comillas simples de POSIX."""
    return "".join(f"{key}={shell_quote(envs[key])}\n" for key in sorted(envs)).encode("utf-8")


def permission_rules(permissions: AgentPermissions) -> dict[str, object]:
    """Los permisos de `AgentSpec` como los lee un driver: `default` y una
    entrada por herramienta (con `DEFAULT_DENIED_TOOLS` por debajo), un
    patrón por comando en las que los admiten."""
    tools: dict[str, object] = {}
    for tool, rule in permissions.effective_tools().items():
        tools[tool] = rule if isinstance(rule, str) else dict(rule)
    return {"default": permissions.default, "tools": tools}


def tool_allowed(permissions: AgentPermissions, tool: str) -> bool:
    """`False` sólo si `tool` está denegada del todo; una herramienta con
    patrones sigue disponible y el driver aplica cada patrón al llamarla."""
    rule = permissions.effective_tools().get(tool, permissions.default)
    return rule != "deny"


@dataclass(frozen=True)
class ProviderClient:
    """Cómo un framework habla con un proveedor del modelo a través de la
    pasarela: la clase de cliente y la ruta que cuelga de la URL de la
    pasarela (`None`: la `base_path` de `AgentModel`, sólo
    `openai-compatible`). `testdata/agent/provider-catalogue.json`
    (`runtimes`) es la tabla que comprueban los tests de los dos SDK."""

    client: str
    base_path: str | None = ""


def require_supported_provider(
    spec: AgentSpec, supported: Mapping[str, ProviderClient], label: str
) -> ProviderClient:
    """El cliente del proveedor del modelo de `spec`, o
    `InvalidArgumentException` (antes de cualquier RPC) si `label` no lo
    admite."""
    entry = supported.get(spec.model.provider)
    if entry is None:
        raise InvalidArgumentException(
            f"{label} no admite el proveedor {spec.model.provider!r}; "
            f"usa uno de {sorted(supported)}"
        )
    return entry


def provider_base_url(
    spec: AgentSpec, entry: ProviderClient, urls: Mapping[str, str] | None
) -> str | None:
    """La URL base del modelo: la de su pasarela más la ruta del cliente;
    `None` sin pasarela (contenedor exportado). Falla con
    `InvalidArgumentException` si el sandbox no tiene la pasarela."""
    if urls is None:
        return None
    spec.require_gateways(urls.keys())
    path = spec.model.base_path if entry.base_path is None else entry.base_path
    return urls[spec.model.gateway].rstrip("/") + path


#: Variables estándar que leen los SDK de cada proveedor: (variable de la
#: clave, variable de la URL base, ruta que cuelga de la pasarela, `None` =
#: la `base_path` de `AgentModel`). Verificadas contra los SDK en
#: `testdata/agent/strands-spike.json`.
STANDARD_PROVIDER_ENVS: Final[Mapping[str, tuple[str, str, str | None]]] = {
    "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", ""),
    "openai": ("OPENAI_API_KEY", "OPENAI_BASE_URL", "/v1"),
    "openai-compatible": ("OPENAI_API_KEY", "OPENAI_BASE_URL", None),
    "google": ("GEMINI_API_KEY", "GOOGLE_GEMINI_BASE_URL", ""),
}
#: Bedrock no tiene variable de URL base propia: el SDK de AWS lee el
#: endpoint de `bedrock-runtime` y la clave de portador (sin SigV4 ni
#: credenciales de AWS) de estas dos.
BEDROCK_KEY_ENV: Final = "AWS_BEARER_TOKEN_BEDROCK"
BEDROCK_ENDPOINT_ENV: Final = "AWS_ENDPOINT_URL_BEDROCK_RUNTIME"
BEDROCK_REGION_ENV: Final = "AWS_REGION"


def model_base_url(spec: AgentSpec, urls: Mapping[str, str]) -> str:
    """La URL que el SDK del proveedor espera como base: la de su variable
    estándar (la pasarela más la ruta del proveedor) o, si no tiene, la raíz
    de la pasarela. Falla con `InvalidArgumentException` si el sandbox no
    tiene la pasarela del modelo."""
    spec.require_gateways(urls.keys())
    model = spec.model
    gateway = urls[model.gateway].rstrip("/")
    standard = STANDARD_PROVIDER_ENVS.get(model.provider)
    if standard is None:
        return gateway
    path = standard[2]
    return gateway + (model.base_path if path is None else path)


def standard_model_envs(spec: AgentSpec, urls: Mapping[str, str] | None) -> dict[str, str]:
    """Las variables estándar del proveedor del modelo apuntadas a la
    pasarela con el marcador como clave, de modo que también un cliente que
    construyas tú (sin el `ctx.model` ya cableado) pase por ella y no por
    credenciales que el agente pudiera leer. `urls=None` es el contenedor
    exportado: sólo la región, la credencial la pone la plataforma."""
    model = spec.model
    envs: dict[str, str] = {}
    if model.region is not None:
        envs[BEDROCK_REGION_ENV] = model.region
    if urls is None:
        return envs
    base_url = model_base_url(spec, urls)
    if model.provider == "bedrock":
        envs[BEDROCK_KEY_ENV] = MODEL_CREDENTIAL_PLACEHOLDER
        envs[BEDROCK_ENDPOINT_ENV] = base_url
        return envs
    standard = STANDARD_PROVIDER_ENVS.get(model.provider)
    if standard is not None:
        envs[standard[0]] = MODEL_CREDENTIAL_PLACEHOLDER
        envs[standard[1]] = base_url
    return envs


def container_packages_step(
    target: InstallTarget, packages: Sequence[str]
) -> tuple[TemplateStep, ...]:
    """El `RUN` de `apt-get` con las herramientas de build que una imagen
    Debian de un contenedor exportado no trae; vacío en un MicroVM, cuya
    base ya las tiene."""
    if target.platform != "container":
        return ()
    names = " ".join(packages)
    return (
        run_step(
            "apt-get update && apt-get install -y --no-install-recommends "
            f"{names} && rm -rf /var/lib/apt/lists/*"
        ),
    )


def user_requirements_steps(
    target: InstallTarget, *, venv_dir: str, requirements: str, slug: str
) -> tuple[TemplateStep, ...]:
    """Instala en `venv_dir` el fichero de requisitos del usuario
    (`app/<requirements>` del contexto de build) después de los pines de
    Rayito. `--require-hashes` se añade solo si el fichero lleva algún
    `--hash=` (con hashes incompletos pip falla, que es lo que se quiere), y
    el paso termina con `pip check`."""
    destination = f"{target.install_dir}/requirements-user-{slug}.txt"
    venv_python = f"{venv_dir}/bin/python"
    command = (
        f"hashes=''; if grep -q -- '--hash=' {destination}; then hashes='--require-hashes'; fi"
        f" && {venv_python} -m pip install --no-cache-dir --only-binary=:all: $hashes"
        f" -r {destination} && {venv_python} -m pip check"
    )
    return (copy_step(f"app/{requirements}", destination), run_step(command))


class HostBackedRuntime:
    """`AgentRuntime` y `ServableRuntime` de un runtime que corre por el host.

    Una subclase implementa `host_python()` (el intérprete que ejecuta el
    host), `host_setup()` (qué driver, qué configuración), `flag_envs()` y
    `wiring_envs()`, y los puertos de instalación si es `InstallableRuntime`.
    `name` identifica el adaptador en la telemetría y `state_name` es su
    carpeta bajo `AGENT_STATE_DIR`."""

    name: str
    state_name: str

    def host_python(self) -> str:
        """El intérprete con el que se ejecuta el host (stdlib únicamente)."""
        raise NotImplementedError

    def host_setup(
        self, spec: AgentSpec, urls: Mapping[str, str] | None, workdir: str
    ) -> HostSetup:
        raise NotImplementedError

    def flag_envs(self) -> Mapping[str, str]:
        """Variables que no dependen de la pasarela."""
        return PYTHON_FLAG_ENVS

    def wiring_envs(self, spec: AgentSpec, urls: Mapping[str, str] | None) -> Mapping[str, str]:
        """Variables del proveedor del modelo; `urls=None` es el contenedor
        exportado, donde no hay marcador ni URL de pasarela."""
        return {}

    @property
    def state_dir(self) -> str:
        return f"{AGENT_STATE_DIR}/{self.state_name}"

    @property
    def config_path(self) -> str:
        return f"{self.state_dir}/{CONFIG_FILE_NAME}"

    @property
    def host_path(self) -> str:
        return f"{self.state_dir}/{HOST_FILE_NAME}"

    @property
    def wiring_env_path(self) -> str:
        return f"{self.state_dir}/{WIRING_ENV_FILE_NAME}"

    def _options(self, setup: HostSetup) -> dict[str, object]:
        options = dict(setup.options)
        if setup.config is not None:
            options["config_path"] = self.config_path
        return options

    def _config_files(self, setup: HostSetup) -> list[RuntimeFile]:
        if setup.config is None:
            return []
        return [RuntimeFile(self.config_path, canonical_json(setup.config), CONFIG_FILE_MODE)]

    def host_config(self, spec: AgentSpec, *, wiring: ModelWiring, workdir: str) -> HostConfig:
        """La configuración del host para el modo servicio."""
        urls = wiring_urls(wiring)
        setup = self.host_setup(spec, urls, workdir)
        files = self._config_files(setup)
        return HostConfig(
            driver=setup.driver,
            python=self.host_python(),
            files=RuntimeFiles(files=tuple(files), config_sha256=files_sha256(files)),
            envs={**self.flag_envs(), **self.wiring_envs(spec, urls)},
            options=self._options(setup),
        )

    def build_config(
        self, spec: AgentSpec, *, gateway_urls: Mapping[str, str], workdir: str
    ) -> RuntimeFiles:
        """Los ficheros de una ejecución `sbx.agent.run`: la configuración
        del driver, el `host.json` del modo `task` y las variables del
        proveedor."""
        setup = self.host_setup(spec, gateway_urls, workdir)
        host_document = {
            "schema": AGENT_HOST_SCHEMA,
            "v": AGENT_PROTOCOL_VERSION,
            "driver": setup.driver,
            "options": self._options(setup),
        }
        files = [
            *self._config_files(setup),
            RuntimeFile(self.host_path, canonical_json(host_document), CONFIG_FILE_MODE),
            RuntimeFile(
                self.wiring_env_path,
                env_file_bytes(self.wiring_envs(spec, gateway_urls)),
                CONFIG_FILE_MODE,
            ),
        ]
        return RuntimeFiles(files=tuple(files), config_sha256=files_sha256(files))

    def command(self, request: RunRequest) -> RunCommand:
        """El script (cerrojo, comprobación del host, variables del
        proveedor y `exec` del host en modo `task`) y la petición JSON por
        stdin. El prompt nunca va en argv."""
        if request.session_id is not None and not SESSION_ID_PATTERN.fullmatch(request.session_id):
            raise InvalidArgumentException(
                f"session_id de {self.name} debe tener la forma rda_<hex>"
            )
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
            script=self._run_script(self.host_python()),
            envs={"HOME": DEFAULT_AGENT_WORKDIR, **self.flag_envs()},
            stdin=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        )

    def _run_script(self, python: str) -> str:
        lines = [
            "set -u",
            f"mkdir -p {shell_quote(self.state_dir)}",
            f"exec 9>{shell_quote(AGENT_RUN_LOCK_PATH)}",
            f"if ! flock -n 9; then {failure_line('busy')}; exit 0; fi",
            f"if ! command -v {shell_quote(python)} >/dev/null 2>&1 || "
            f"[ ! -r {shell_quote(AGENT_HOST_DIR + '/__main__.py')} ]; then "
            f"{failure_line('runtime_missing')}; exit 0; fi",
            f"set -a; . {shell_quote(self.wiring_env_path)}; set +a",
            f"exec {shell_quote(python)} {shell_quote(AGENT_HOST_DIR)} {HOST_TASK_MODE} "
            f"{shell_quote(self.host_path)}",
        ]
        return "\n".join(lines) + "\n"

    def new_state(self) -> RuntimeState:
        return ProtocolV1State()

    def parse_line(self, line: bytes, state: RuntimeState) -> Sequence[AgentEvent]:
        """Una línea del protocolo v1 a cero o un evento."""
        assert isinstance(state, ProtocolV1State)
        return parse_v1_line(line, state)

    def finish(self, state: RuntimeState, exit_code: int) -> Done | AgentFailed:
        """`Done` sólo con salida 0, el `done` del host y una sesión."""
        assert isinstance(state, ProtocolV1State)
        return finish_v1(state, exit_code)

    def template_steps(self) -> Sequence[TemplateStep]:
        """Vacío: la imagen se compone con `install_steps()`."""
        return ()

    def warmup_steps(self) -> Sequence[WarmupStep]:
        """Ninguno por defecto; los runtimes con un import caro lo
        sobrescriben."""
        return ()


__all__ = [
    "CONFIG_FILE_MODE",
    "HOST_TASK_MODE",
    "PYTHON_FLAG_ENVS",
    "STANDARD_PROVIDER_ENVS",
    "HostBackedRuntime",
    "HostSetup",
    "ProviderClient",
    "container_packages_step",
    "env_file_bytes",
    "failure_line",
    "model_base_url",
    "permission_rules",
    "provider_base_url",
    "require_supported_provider",
    "standard_model_envs",
    "tool_allowed",
    "user_requirements_steps",
]
