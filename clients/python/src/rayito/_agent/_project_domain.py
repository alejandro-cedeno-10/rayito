"""`AgentProject`: la descripción declarativa y validada de un agente
(ADR-028, `ai-agent-project`). Es un valor puro: construirlo, cargarlo
(`load`, `from_dict`) y escribirlo (`to_dict`) no llaman a AWS, no crean
clientes y no leen más que, vía el lector que se inyecta, el fichero de
instrucciones. Las operaciones que cuestan (`build`, `run`, `deploy`) viven
en los servicios que lo usan y no en este módulo.

El proyecto contiene el `AgentSpec` existente (modelo, instrucciones, MCP,
permisos) en vez de copiar sus campos, y las pasarelas del modelo por
nombre. Ningún campo lleva un secreto: una pasarela nombra el secreto de
Secrets Manager, nunca su valor.

El documento es el de `rayito-agent.toml` / `rayito-agent.json` (esquema
`rayito.agent-project/1`, `testdata/agent-project/schema.json`): una tabla
anidada. `from_dict` rechaza toda clave desconocida con su ruta punteada y
un `code` de la lista cerrada `AGENT_PROJECT_ERROR_CODES`.
"""

from __future__ import annotations

import asyncio
import json
import os
import posixpath
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, Self, TypeVar, cast

from rayito._agent._deploy_domain import (
    validate_bucket_hint,
    validate_image_version,
)
from rayito._agent._detection_rules import DETECTION_RULES
from rayito._agent._domain import (
    MODEL_ENV_KEYS,
    AgentLimits,
    AgentModel,
    AgentPermissions,
    AgentSpec,
    McpLocal,
    McpRemote,
    McpServer,
    SubAgent,
    validate_name,
)
from rayito._agent._gateways import (
    anthropic_gateway,
    azure_openai_gateway,
    bedrock_gateway,
    deepseek_gateway,
    gemini_gateway,
    groq_gateway,
    litellm_gateway,
    mistral_gateway,
    openai_compatible_gateway,
    openai_gateway,
    openrouter_gateway,
    xai_gateway,
)
from rayito._agent._runtime import AgentRuntime
from rayito._agent._runtimes import (
    DEFAULT_AGENT_RUNTIMES,
    MODEL_BUILDING_RUNTIMES,
    RUNTIME_KINDS,
    RUNTIME_OPTION_KEYS,
    require_runtime_model_id,
    resolve_runtime,
    runtime_from_options,
    runtime_to_options,
)
from rayito._agent._scaling_domain import ScalingOptions, max_sessions_for_memory
from rayito._agent._secret_env_domain import (
    SECRET_ENV_KEY_PATTERN,
    SECRET_ID_PATTERN,
    SecretEnvRef,
    SecretEnvSource,
    SecretEnvValue,
    has_reserved_prefix,
    is_reserved_name,
    secret_ids,
)
from rayito._agent._template import DEFAULT_AGENT_TEMPLATE_BASE
from rayito._images import DEFAULT_BUILD_TIMEOUT_SECONDS
from rayito._lifecycle_base import DEFAULT_IDLE_POLICY
from rayito._limits import (
    AGENT_MIN_MEMORY_MIB,
    AGENT_NODE_BUILD_VERSIONS,
    AGENT_PROJECT_SCHEMA,
    AGENT_PYTHON_BUILD_VERSIONS,
    AGENT_SECRET_ENV_MAX_SECRETS,
    AGENT_SERVICE_MAX_CONCURRENCY,
    AGENT_SERVICE_PORT,
    AGENT_SERVICE_QUEUE_MAX,
    AGENT_SERVICE_QUEUE_MAX_DEFAULT,
    AGENT_SERVICE_QUEUE_TIMEOUT_SECONDS_DEFAULT,
    AGENT_SERVICE_QUEUE_TIMEOUT_SECONDS_MAX,
    DEFAULT_AGENT_DEPLOY_TIMEOUT_SECONDS,
    DEFAULT_PORT,
    EGRESS_HOSTNAME_MAX_CHARS,
    EGRESS_MAX_ENTRIES_PER_LIST,
    HOOKS_PORT,
    MAX_DURATION_SECONDS,
    PORT_MAX,
    PORT_MIN,
)
from rayito._models import IdlePolicy, NetworkPolicy
from rayito._secret_gateway._domain import SecretGateway
from rayito.exceptions import InvalidArgumentException

if TYPE_CHECKING:
    from rayito._agent._deployment import AgentDeployment
    from rayito._agent._deployment_async import AsyncAgentDeployment
    from rayito._agent._detect_report import DetectionReport, DetectOverrides
    from rayito._agent._events import AgentEvent, AgentResult
    from rayito._agent._export import AgentExport
    from rayito._secrets import SecretRef
    from rayito._templates._models import BuildInfo

#: Los códigos de error de lectura de un proyecto: lista cerrada que los dos
#: SDK comparten (`testdata/agent-project/invalid/cases.json`).
AGENT_PROJECT_ERROR_CODES: Final[tuple[str, ...]] = (
    "unknown_key",
    "missing_key",
    "invalid_type",
    "invalid_value",
    "path_escape",
    "conflict",
    "secret_value",
    "unsupported_schema",
    "file_not_found",
    "ambiguous",
    "not_detected",
    "unsupported",
)
#: Claves que, de aparecer sin estar declaradas, intentan meter un secreto
#: en el fichero: el error es `secret_value` y no `unknown_key`.
SECRET_LIKE_KEYS: Final[tuple[str, ...]] = (
    "access_key",
    "api_key",
    "apikey",
    "key",
    "password",
    "secret_access_key",
    "secret_value",
    "token",
)
#: Prefijos de credenciales conocidas: un nombre de secreto que empieza así
#: es casi seguro el valor pegado en lugar del nombre.
SECRET_VALUE_PREFIXES: Final[tuple[str, ...]] = (
    "AIza",
    "AKIA",
    "ASIA",
    "ABSK",
    "Bearer",
    "ghp_",
    "gho_",
    "sk-",
    "xoxb-",
)
#: Pasarelas de `[gateways.<nombre>] preset = ...`, además de `"custom"`.
CUSTOM_GATEWAY_PRESET: Final = "custom"
_RATE_KEY: Final = "rate_per_minute"
_MODELS_KEY: Final = "models"
_ENV_NAME_PATTERN: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_T = TypeVar("_T")


#: `[runtime] graph`: el nombre de un grafo de `langgraph.json`, sólo con
#: `kind = "langgraph"` (`testdata/agent-project/schema.json`). Se resuelve
#: al `entrypoint` de ese grafo antes de construir el runtime.
GRAPH_KEY: Final = "graph"
GRAPH_RUNTIME_KIND: Final = "langgraph"
GRAPH_NAME_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
ENTRYPOINT_KEY: Final = "entrypoint"
LANGGRAPH_FILE: Final[str] = DETECTION_RULES["entryFiles"]["langgraph"]["file"]


class AgentProjectException(InvalidArgumentException):
    """Un documento de proyecto inválido. `path` es la ruta punteada de la
    clave culpable (`runtime.entrpoint`) y `code` uno de
    `AGENT_PROJECT_ERROR_CODES`. Es una `InvalidArgumentException`: quien ya
    capturaba esa clase no necesita cambiar."""

    def __init__(self, message: str, *, path: str, code: str) -> None:
        super().__init__(message)
        self.path = path
        self.code = code


def _error(path: str, code: str, detail: str) -> AgentProjectException:
    return AgentProjectException(f"{path}: {detail}", path=path, code=code)


@dataclass(frozen=True)
class ServeOptions:
    """Cómo se sirve el agente (`AgentProject.deploy()`): el `port` del
    servicio (no 8080 ni 9000, que son de `rayd`), la vida del sandbox
    (`timeout_seconds`, como mucho el máximo de la plataforma), la política
    de `idle` (suspensión y reanudación automáticas; `None` la quita), la
    concurrencia por VM (`max_concurrency`, acotada por las conexiones que
    admite el MicroVM de `AgentProject.memory_mib`), las etiquetas `metadata`
    del sandbox y la cola del host: `queue_max` llamadas esperan hasta
    `queue_timeout_seconds` cuando el VM está lleno (con `queue_max=0`, el
    valor por defecto, la llamada que no cabe recibe 429 enseguida)."""

    port: int = AGENT_SERVICE_PORT
    timeout_seconds: float = DEFAULT_AGENT_DEPLOY_TIMEOUT_SECONDS
    idle: IdlePolicy | None = DEFAULT_IDLE_POLICY
    max_concurrency: int = AGENT_SERVICE_MAX_CONCURRENCY
    metadata: Mapping[str, str] = field(default_factory=dict)
    queue_max: int = AGENT_SERVICE_QUEUE_MAX_DEFAULT
    queue_timeout_seconds: float = AGENT_SERVICE_QUEUE_TIMEOUT_SECONDS_DEFAULT

    def __post_init__(self) -> None:
        if isinstance(self.port, bool) or not isinstance(self.port, int):
            raise InvalidArgumentException("ServeOptions.port debe ser un entero")
        if not PORT_MIN <= self.port <= PORT_MAX or self.port in (DEFAULT_PORT, HOOKS_PORT):
            raise InvalidArgumentException(
                f"ServeOptions.port debe estar en {PORT_MIN}..{PORT_MAX} y no ser "
                f"{DEFAULT_PORT} ni {HOOKS_PORT}, que usa rayd"
            )
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int | float)
            or not 0 < self.timeout_seconds <= MAX_DURATION_SECONDS
        ):
            raise InvalidArgumentException(
                f"ServeOptions.timeout_seconds debe estar en (0, {MAX_DURATION_SECONDS}]"
            )
        if self.idle is not None and not isinstance(self.idle, IdlePolicy):
            raise InvalidArgumentException("ServeOptions.idle debe ser una IdlePolicy o None")
        if (
            isinstance(self.max_concurrency, bool)
            or not isinstance(self.max_concurrency, int)
            or self.max_concurrency < 1
        ):
            raise InvalidArgumentException("ServeOptions.max_concurrency debe ser un entero >= 1")
        if not isinstance(self.metadata, Mapping) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in self.metadata.items()
        ):
            raise InvalidArgumentException("ServeOptions.metadata debe mapear cadenas a cadenas")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
        if (
            isinstance(self.queue_max, bool)
            or not isinstance(self.queue_max, int)
            or not 0 <= self.queue_max <= AGENT_SERVICE_QUEUE_MAX
        ):
            raise InvalidArgumentException(
                f"ServeOptions.queue_max debe ser un entero en 0..{AGENT_SERVICE_QUEUE_MAX}"
            )
        if (
            isinstance(self.queue_timeout_seconds, bool)
            or not isinstance(self.queue_timeout_seconds, int | float)
            or not 0 < self.queue_timeout_seconds <= AGENT_SERVICE_QUEUE_TIMEOUT_SECONDS_MAX
        ):
            raise InvalidArgumentException(
                "ServeOptions.queue_timeout_seconds debe estar en "
                f"(0, {AGENT_SERVICE_QUEUE_TIMEOUT_SECONDS_MAX}]"
            )


#: Los lenguajes de un proyecto de agente.
AGENT_LANGUAGES: Final[tuple[str, ...]] = ("python", "typescript")


@dataclass(frozen=True)
class RuntimeVersions:
    """Las versiones de intérprete del agente: `python` (la menor, `"3.12"`) y
    `node` (la mayor, `"22"`) entre las compilaciones fijadas de
    `limits.json`. `None` es la de por defecto: la del intérprete de la
    imagen base (sin descarga) y la que exige Strands para TypeScript."""

    python: str | None = None
    node: str | None = None

    def __post_init__(self) -> None:
        _supported_version(self.python, AGENT_PYTHON_BUILD_VERSIONS, "RuntimeVersions.python")
        _supported_version(self.node, AGENT_NODE_BUILD_VERSIONS, "RuntimeVersions.node")

    @property
    def is_default(self) -> bool:
        return self.python is None and self.node is None


def _supported_version(value: object, builds: Mapping[str, str], label: str) -> None:
    if value is not None and value not in builds:
        raise InvalidArgumentException(f"{label} debe ser una de {sorted(builds)}")


def _concurrency_problem(serve: ServeOptions, memory_mib: int) -> str | None:
    """Por qué `serve.max_concurrency` no cabe en las conexiones de un VM de
    `memory_mib`, o `None` si cabe."""
    cap = max_sessions_for_memory(memory_mib)
    if serve.max_concurrency <= cap:
        return None
    return (
        f"admite como mucho {cap} con memory_mib={memory_mib} (conexiones del MicroVM menos "
        "las reservadas); sube la memoria o baja la concurrencia"
    )


def _secret_like_env_name(name: str) -> bool:
    lowered = name.lower()
    return any(lowered == word or lowered.endswith(f"_{word}") for word in SECRET_LIKE_KEYS)


def _validated_env(env: object) -> Mapping[str, str]:
    """`AgentProject.env`: nombres de variable válidos, valores de texto y
    nada que parezca un secreto (un secreto va por una pasarela)."""
    if not isinstance(env, Mapping):
        raise InvalidArgumentException("AgentProject.env debe mapear cadenas a cadenas")
    for name, value in env.items():
        if not isinstance(name, str) or not _ENV_NAME_PATTERN.fullmatch(name):
            raise InvalidArgumentException(
                "AgentProject.env: cada nombre debe ser un identificador de variable de entorno"
            )
        if not isinstance(value, str):
            raise InvalidArgumentException("AgentProject.env debe mapear cadenas a cadenas")
        if _secret_like_env_name(name) or value.startswith(SECRET_VALUE_PREFIXES):
            raise InvalidArgumentException(
                "AgentProject.env no puede llevar secretos: usa una pasarela para las credenciales"
            )
    return env


def _validated_network(network: object) -> NetworkPolicy | None:
    if network is None:
        return None
    if not isinstance(network, NetworkPolicy):
        raise InvalidArgumentException("AgentProject.network debe ser un NetworkPolicy o None")
    for label, entries in (("allow_out", network.allow_out), ("deny_out", network.deny_out)):
        if len(entries) > EGRESS_MAX_ENTRIES_PER_LIST or not all(
            isinstance(entry, str) and 0 < len(entry) <= EGRESS_HOSTNAME_MAX_CHARS
            for entry in entries
        ):
            raise InvalidArgumentException(
                f"AgentProject.network.{label} admite como mucho {EGRESS_MAX_ENTRIES_PER_LIST} "
                "cadenas no vacías"
            )
    if network.egress_proxy is not None:
        raise InvalidArgumentException(
            "AgentProject.network no admite egress_proxy: sus credenciales no van en el proyecto"
        )
    return network


SECRET_ENV_KEY: Final = "secret_env"
SECRET_ENV_FROM_KEY: Final = "secret_env_from"


def secret_env_path(name: str) -> str:
    """La ruta de error de una variable de `secret_env` (la tabla, si el
    nombre está vacío)."""
    return f"{SECRET_ENV_KEY}.{name}" if name else SECRET_ENV_KEY


def secret_env_from_path(index: int) -> str:
    return f"{SECRET_ENV_FROM_KEY}[{index}]"


def check_secret_env_name(name: object, path: str) -> str:
    """Un nombre de variable válido y no reservado, o `invalid_value`."""
    if not isinstance(name, str) or not _ENV_NAME_PATTERN.fullmatch(name):
        raise _error(path, "invalid_value", "debe ser un nombre de variable de entorno")
    if is_reserved_name(name):
        raise _error(path, "invalid_value", "es un nombre reservado del runtime")
    return name


def check_secret_id(value: object, path: str) -> str:
    """El nombre completo o el ARN de un secreto: `secret_value` si parece el
    valor pegado en su lugar, `invalid_value` si no es un nombre."""
    if not isinstance(value, str):
        raise _error(path, "invalid_type", "debe ser el nombre o el ARN de un secreto")
    if value.startswith(SECRET_VALUE_PREFIXES):
        raise _error(
            path, "secret_value", "parece un valor y no el nombre de un secreto; escribe el nombre"
        )
    if not SECRET_ID_PATTERN.fullmatch(value):
        raise _error(path, "invalid_value", "debe ser el nombre o el ARN de un secreto")
    return value


def check_secret_env_ref(value: object, path: str) -> SecretEnvValue:
    """Un valor de `secret_env`: el nombre de un secreto o un `SecretEnvRef`."""
    if isinstance(value, str):
        return check_secret_id(value, path)
    if not isinstance(value, SecretEnvRef):
        raise _error(path, "invalid_type", "debe ser el nombre de un secreto o un SecretEnvRef")
    check_secret_id(value.secret, path)
    for text in (value.key, value.version_stage):
        if text is not None and not SECRET_ENV_KEY_PATTERN.fullmatch(text):
            raise _error(path, "invalid_value", "debe ser ASCII imprimible sin espacios")
    return value


def check_secret_env_source(source: object, path: str) -> SecretEnvSource:
    """Una entrada de `secret_env_from`: secreto válido, prefijo vacío o de
    nombre de variable y no reservado, y claves que con el prefijo son
    nombres válidos y no reservados."""
    if not isinstance(source, SecretEnvSource):
        raise _error(path, "invalid_type", "debe ser un SecretEnvSource")
    check_secret_id(source.secret, f"{path}.secret")
    prefix_path = f"{path}.prefix"
    if source.prefix and not _ENV_NAME_PATTERN.fullmatch(source.prefix):
        raise _error(prefix_path, "invalid_value", "debe ser un prefijo de nombre de variable")
    if has_reserved_prefix(source.prefix):
        raise _error(prefix_path, "invalid_value", "es un prefijo reservado del runtime")
    for key in source.keys or ():
        check_secret_env_name(f"{source.prefix}{key}", f"{path}.keys")
    return source


def _validated_secret_env(
    secret_env: object, env: Mapping[str, str]
) -> Mapping[str, SecretEnvValue]:
    if not isinstance(secret_env, Mapping):
        raise _error(SECRET_ENV_KEY, "invalid_type", "debe mapear nombres a secretos")
    for name, value in secret_env.items():
        path = secret_env_path(name if isinstance(name, str) else "")
        check_secret_env_name(name, path)
        check_secret_env_ref(value, path)
        if name in env:
            raise _error(path, "conflict", "la variable está a la vez en [env] y en [secret_env]")
    return secret_env


def _validated_secret_env_from(sources: object) -> tuple[SecretEnvSource, ...]:
    if isinstance(sources, str) or not isinstance(sources, Sequence):
        raise _error(SECRET_ENV_FROM_KEY, "invalid_type", "debe ser una lista de SecretEnvSource")
    return tuple(
        check_secret_env_source(source, secret_env_from_path(index))
        for index, source in enumerate(sources)
    )


@dataclass(frozen=True)
class AgentProject:
    """Un agente descrito una vez: runtime, `AgentSpec`, pasarelas del
    modelo, código (`source`), red e imagen. Construirlo valida todo antes de
    llamar a nadie: `name`, que cada pasarela que el `spec` necesita esté en
    `gateways`, `serve` y la memoria.

    `runtime` es un nombre del registro (`"opencode"`, `"deepagents"`,
    `"langgraph"`...) o un objeto de runtime. `source` es el directorio con
    el código del agente (se copia a `AGENT_APP_DIR` con `exclude`);
    `requirements`, un fichero de requisitos suyo. `base_dir` es el
    directorio contra el que `to_dict()` escribe las rutas relativas.
    `template` es el nombre de una imagen ya construida (por defecto,
    `name`).

    Para servir el agente en varios VMs: `scaling` (`ScalingOptions`),
    `env` (variables no secretas del agente), `network` (política
    `allow_out`/`deny_out` del sandbox, que necesita `rayito-base-caps`),
    `region`, `bucket` y `base_image_version` (los valores por defecto de
    `deploy_agent()`), `language` y `framework` (lo que la detección o el
    fichero declaran del proyecto) y `versions` (`RuntimeVersions`).
    `graph` es el nombre del grafo de `langgraph.json` que se sirve (sólo con
    el runtime `langgraph`); `to_dict()` lo escribe en lugar del
    `entrypoint`.

    `gateway_presets` (pasarela -> preset, p. ej. `{"model": "bedrock"}`) es
    lo que el fichero declara en `[gateways.<nombre>] preset`: lo rellenan
    `load()`, `from_dict()` y `detect()`, sirve para elegir la variable
    estándar de `--create-secret` y `to_dict()` no lo escribe (cada pasarela
    ya va en su forma `custom`), así que el documento canónico no cambia.

    `secret_env` (nombre de variable -> nombre o ARN de un secreto, o un
    `SecretEnvRef`) y `secret_env_from` (`SecretEnvSource`) ponen valores de
    Secrets Manager en el entorno del agente al arrancar (ADR-033 D4): el
    proyecto sólo lleva nombres, nunca valores, y el agente puede leer su
    entorno (ver `SecretEnvRef` para el coste).

    Coste y activación
    -------------------
    Activa: nada; `AgentProject(...)`, `AgentProject.load()`, `from_dict()` y
        `to_dict()` son puros. Lo que cuesta son `build()`, `run()` y
        `deploy()`.
    Recursos y llamadas AWS: ninguno aquí; ni se crea un cliente de AWS.
    Coste aproximado: ninguno; el de las operaciones está en la
        documentación de `AgentTemplate` (`build()`), de `Sandbox.create()`
        (`run()`) y de `deploy()`.
    IAM: ninguna para este valor.
    Cómo apagarla: no llames a `build()`, `run()` ni `deploy()`.
    Ejemplo:
        project = AgentProject.load("./support-bot")
        project.to_dict()
    """

    name: str
    runtime: str | AgentRuntime
    spec: AgentSpec
    gateways: Mapping[str, SecretGateway] = field(default_factory=dict)
    source: str | None = None
    exclude: Sequence[str] = ()
    requirements: str | None = None
    limits: AgentLimits = field(default_factory=AgentLimits)
    serve: ServeOptions = field(default_factory=ServeOptions)
    allow_internet_access: bool = False
    egress: Sequence[str] | None = None
    memory_mib: int = AGENT_MIN_MEMORY_MIB
    base: str = DEFAULT_AGENT_TEMPLATE_BASE
    template: str | None = None
    base_dir: str = "."
    scaling: ScalingOptions = field(default_factory=ScalingOptions)
    env: Mapping[str, str] = field(default_factory=dict)
    network: NetworkPolicy | None = None
    region: str | None = None
    bucket: str | None = None
    base_image_version: str | None = None
    language: str | None = None
    framework: str | None = None
    versions: RuntimeVersions = field(default_factory=RuntimeVersions)
    graph: str | None = None
    secret_env: Mapping[str, SecretEnvValue] = field(default_factory=dict)
    secret_env_from: Sequence[SecretEnvSource] = ()
    gateway_presets: Mapping[str, str] = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        validate_name(self.name, "AgentProject.name")
        resolve_runtime(self.runtime)
        if not isinstance(self.spec, AgentSpec):
            raise InvalidArgumentException("AgentProject.spec debe ser un AgentSpec")
        require_runtime_model_id(self.spec, self.runtime)
        gateways = _validated_gateways(self.gateways)
        self.spec.require_gateways(gateways)
        object.__setattr__(self, "gateways", MappingProxyType(dict(gateways)))
        _optional_text(self.source, "AgentProject.source")
        _optional_text(self.requirements, "AgentProject.requirements")
        _optional_text(self.template, "AgentProject.template")
        object.__setattr__(self, "exclude", _text_tuple(self.exclude, "AgentProject.exclude"))
        if self.egress is not None:
            object.__setattr__(self, "egress", _text_tuple(self.egress, "AgentProject.egress"))
        if not isinstance(self.limits, AgentLimits):
            raise InvalidArgumentException("AgentProject.limits debe ser un AgentLimits")
        if not isinstance(self.serve, ServeOptions):
            raise InvalidArgumentException("AgentProject.serve debe ser un ServeOptions")
        if not isinstance(self.allow_internet_access, bool):
            raise InvalidArgumentException("AgentProject.allow_internet_access debe ser un bool")
        if (
            isinstance(self.memory_mib, bool)
            or not isinstance(self.memory_mib, int)
            or self.memory_mib < AGENT_MIN_MEMORY_MIB
        ):
            raise InvalidArgumentException(
                f"AgentProject.memory_mib debe ser un entero >= {AGENT_MIN_MEMORY_MIB}"
            )
        if not self.base or not self.base_dir:
            raise InvalidArgumentException("AgentProject.base y base_dir no pueden ser vacíos")
        self._validate_fleet_options()
        _validate_graph(self.graph, self.runtime)
        self._validate_secret_env()
        object.__setattr__(self, "gateway_presets", _validated_presets(self.gateway_presets))

    def _validate_fleet_options(self) -> None:
        if not isinstance(self.scaling, ScalingOptions):
            raise InvalidArgumentException("AgentProject.scaling debe ser un ScalingOptions")
        if not isinstance(self.versions, RuntimeVersions):
            raise InvalidArgumentException("AgentProject.versions debe ser un RuntimeVersions")
        problem = _concurrency_problem(self.serve, self.memory_mib)
        if problem is not None:
            raise InvalidArgumentException(f"ServeOptions.max_concurrency {problem}")
        object.__setattr__(self, "env", MappingProxyType(dict(_validated_env(self.env))))
        object.__setattr__(self, "network", _validated_network(self.network))
        for label, value in (("region", self.region), ("framework", self.framework)):
            _optional_text(value, f"AgentProject.{label}")
        if self.bucket is not None:
            validate_bucket_hint(self.bucket, "AgentProject.bucket")
        if self.base_image_version is not None:
            validate_image_version(self.base_image_version, "AgentProject.base_image_version")
        if self.language is not None and self.language not in AGENT_LANGUAGES:
            raise InvalidArgumentException(
                f"AgentProject.language debe ser uno de {list(AGENT_LANGUAGES)}"
            )

    def _validate_secret_env(self) -> None:
        secret_env = _validated_secret_env(self.secret_env, self.env)
        secret_env_from = _validated_secret_env_from(self.secret_env_from)
        if len(secret_ids(secret_env, secret_env_from)) > AGENT_SECRET_ENV_MAX_SECRETS:
            raise _error(
                SECRET_ENV_KEY,
                "invalid_value",
                f"admite como mucho {AGENT_SECRET_ENV_MAX_SECRETS} secretos",
            )
        object.__setattr__(self, "secret_env", MappingProxyType(dict(secret_env)))
        object.__setattr__(self, "secret_env_from", secret_env_from)

    @classmethod
    def load(cls, path: str | os.PathLike[str] = ".") -> Self:
        """Lee `rayito-agent.toml` o `rayito-agent.json` de `path` (un
        fichero, o un directorio donde se busca el primero de
        `AGENT_PROJECT_FILE_NAMES`)."""
        from rayito._agent._project_file import read_project_document

        document = read_project_document(path)
        return cls.from_dict(
            document.data, base_dir=document.base_dir, read_text=document.read_text
        )

    @classmethod
    def detect(
        cls,
        path: str | os.PathLike[str] = ".",
        overrides: DetectOverrides | None = None,
        *,
        region: str | None = None,
    ) -> tuple[Self, DetectionReport | None]:
        """El proyecto de un agente que ya tienes y su `DetectionReport`
        (`None` si un fichero de proyecto completo no necesitó detectar nada),
        sin ejecutar su código ni llamar a AWS. El fichero de proyecto, si
        existe, gana clave por clave; lo que calla lo completa la detección
        (lenguaje, framework, punto de entrada, instalación, proveedor y
        modelo) y `overrides` fuerza lo que tú indiques. Si el caso no es
        claro lanza `AgentProjectException` con `code` `ambiguous`,
        `not_detected` o `unsupported` y la clave o bandera que lo resuelve.
        """
        from rayito._agent._project_file import detect_project

        project, report = detect_project(path, overrides, region=region)
        return cast(Self, project), report

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, object],
        *,
        base_dir: str,
        read_text: Callable[[str], str] | None = None,
    ) -> Self:
        """El proyecto de un documento ya leído. Puro: las rutas relativas
        se resuelven contra `base_dir` sin tocar el disco, y
        `instructions_file` pasa por `read_text` (el lector que inyecta
        `load`; sin él, esa clave es un error)."""
        return cls(**_project_kwargs(data, base_dir, read_text), base_dir=base_dir)

    def to_dict(self) -> dict[str, object]:
        """La forma canónica del documento, la que `from_dict` vuelve a leer
        y que comparten los dos SDK. Omite lo que vale su valor por defecto;
        el texto de las instrucciones va en línea."""
        document: dict[str, object] = {"schema": AGENT_PROJECT_SCHEMA, "name": self.name}
        document["runtime"] = _runtime_to_dict(self)
        source = _source_to_dict(self)
        if source:
            document["source"] = source
        document["model"] = _model_to_dict(self.spec.model)
        if self.gateways:
            document["gateways"] = {
                name: _gateway_to_dict(gateway) for name, gateway in sorted(self.gateways.items())
            }
        for key, table in (
            ("agent", _agent_to_dict(self.spec)),
            ("limits", _limits_to_dict(self.limits)),
            ("serve", _serve_to_dict(self.serve)),
            ("scaling", _scaling_to_dict(self.scaling)),
            ("env", dict(sorted(self.env.items()))),
            ("secret_env", _secret_env_to_dict(self)),
            ("secret_env_from", _secret_env_from_to_dict(self)),
            ("network", _network_to_dict(self)),
            ("deploy", _deploy_to_dict(self)),
            ("image", _image_to_dict(self)),
        ):
            if table:
                document[key] = table
        return document

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
        """Construye la imagen del proyecto con `AgentTemplate` (su runtime,
        su código y su memoria). Cuesta un build de imagen: ver el bloque
        "Coste y activación" de `AgentTemplate`."""
        from rayito._agent._project_service import ProjectService

        return ProjectService(self).build(
            bucket=bucket,
            force=force,
            timeout=timeout,
            on_build_logs=on_build_logs,
            region=region,
            session=session,
        )

    def run(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        model: str | None = None,
        reasoning: bool = False,
        on_event: Callable[[AgentEvent], None] | None = None,
        region: str | None = None,
        session: Any = None,
    ) -> AgentResult:
        """Una ejecución del agente en un sandbox efímero: lo crea desde la
        imagen del proyecto con sus pasarelas y su red, llama a
        `sbx.agent.run()` y lo termina siempre, también ante un error. Para
        que el sandbox sobreviva, usa `deploy()`."""
        from rayito._agent._project_service import ProjectService

        return ProjectService(self).run(
            prompt,
            session_id=session_id,
            model=model,
            reasoning=reasoning,
            on_event=on_event,
            region=region,
            session=session,
        )

    def deploy(
        self,
        *,
        region: str | None = None,
        session: Any = None,
        access_token: str | None = None,
        keep_on_failure: bool = False,
    ) -> AgentDeployment:
        """Sirve el agente desde un MicroVM y devuelve su `AgentDeployment`:
        crea un sandbox desde la imagen del proyecto (con `serve.timeout_seconds`,
        `serve.idle` y las etiquetas de despliegue), escribe la configuración
        del host, lo arranca en modo servicio y espera su marcador de listo.
        Si cualquier paso falla, termina el sandbox antes de lanzar (salvo
        con `keep_on_failure=True`).

        `access_token` es el del sandbox (como el de `Sandbox.create()`; sin
        él, `RAYITO_ACCESS_TOKEN` o uno nuevo). Con uno nuevo, el valor está en
        `AgentDeployment.access_token`: guárdalo para reconectarte desde otro
        proceso (`AgentDeployment.connect(sandbox_id, access_token=...)`,
        `rayito agent invoke`); sin él sólo `Sandbox.kill(sandbox_id)` llega al
        sandbox.

        Un arranque fallido lanza `SandboxException` con el código del fallo
        y el final de la salida de error de la app, sin valores de entorno ni
        credenciales. Con `keep_on_failure=True` el sandbox no se termina
        (para entrar a depurarlo: el error lleva su id en una nota) y hace
        falta `access_token` (o `RAYITO_ACCESS_TOKEN`) para poder
        conectarse; termínalo tú con `Sandbox.kill(sandbox_id)`, porque sigue
        facturando. Por defecto es `False`.

        Coste y activación
        -------------------
        Activa: llamar a `deploy()` (o `rayito agent deploy`); nada más lo
            crea.
        Recursos y llamadas AWS: un sandbox (`run-microvm`) que vive hasta
            `serve.timeout_seconds` (como mucho 8 horas) o hasta
            `AgentDeployment.stop()`, y el token del proxy
            (`CreateMicrovmAuthToken`, gratuito); el modelo lo llama la
            pasarela del sandbox.
        Coste aproximado: el del MicroVM por segundo mientras vive (2 GB:
            ≈ $0,14 por hora en us-east-1, consultado 2026-10-06 en
            https://aws.amazon.com/lambda/pricing/; `serve.idle` lo suspende
            y la suspensión no factura cómputo) más el del modelo del agente.
        IAM: los de `Sandbox.create()` (`RayitoSandboxRunner`) y, por cada
            pasarela, `secretsmanager:GetSecretValue`.
        Cómo apagarla: `AgentDeployment.stop()` o `rayito agent stop`; no
            llames a `deploy()` y nada se crea.
        Ejemplo:
            deployment = AgentProject.load("./support-bot").deploy()
            deployment.invoke("Resume el README.").text
            deployment.stop()
        """
        from rayito._agent._project_service import ProjectService

        return ProjectService(self).deploy(
            region=region,
            session=session,
            access_token=access_token,
            keep_on_failure=keep_on_failure,
        )

    def export(
        self,
        *,
        target: str = "docker",
        out_dir: str | os.PathLike[str],
        overwrite: bool = False,
    ) -> AgentExport:
        """Escribe en `out_dir` un contexto de build de contenedor
        `linux/arm64` (`target="docker"` o `"agentcore"`) sin ninguna
        llamada de red ni de AWS. Falla si `out_dir` no está vacío, salvo con
        `overwrite=True`. Fuera de Rayito no hay pasarela de secretos: la
        credencial del modelo la pone la plataforma."""
        from rayito._agent._export import export_project

        return export_project(self, target=target, out_dir=out_dir, overwrite=overwrite)


@dataclass(frozen=True)
class AsyncAgentProject(AgentProject):
    """`AgentProject` con las operaciones `async`: el mismo constructor y la
    misma superficie (`build`, `run`, `deploy`, `export`) como corrutinas."""

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
        from rayito._agent._project_service_async import AsyncProjectService

        return await AsyncProjectService(self).build(
            bucket=bucket,
            force=force,
            timeout=timeout,
            on_build_logs=on_build_logs,
            region=region,
            session=session,
        )

    async def run(  # type: ignore[override]
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        model: str | None = None,
        reasoning: bool = False,
        on_event: Callable[[AgentEvent], None] | None = None,
        region: str | None = None,
        session: Any = None,
    ) -> AgentResult:
        from rayito._agent._project_service_async import AsyncProjectService

        return await AsyncProjectService(self).run(
            prompt,
            session_id=session_id,
            model=model,
            reasoning=reasoning,
            on_event=on_event,
            region=region,
            session=session,
        )

    async def deploy(  # type: ignore[override]
        self,
        *,
        region: str | None = None,
        session: Any = None,
        access_token: str | None = None,
        keep_on_failure: bool = False,
    ) -> AsyncAgentDeployment:
        """Como `AgentProject.deploy()`, con `AsyncAgentDeployment`. Mismo
        bloque de coste: crea un sandbox que vive hasta `stop()` o hasta
        `serve.timeout_seconds`.

        Coste y activación
        -------------------
        Activa: `await deploy()` (o `rayito agent deploy`); nada más lo crea.
        Recursos y llamadas AWS: un sandbox (`run-microvm`) que vive hasta
            `serve.timeout_seconds` (como mucho 8 horas) o hasta
            `await AsyncAgentDeployment.stop()`, y el token del proxy
            (`CreateMicrovmAuthToken`, gratuito); el modelo lo llama la
            pasarela del sandbox.
        Coste aproximado: el del MicroVM por segundo mientras vive (2 GB:
            ≈ $0,14 por hora en us-east-1, consultado 2026-10-06 en
            https://aws.amazon.com/lambda/pricing/) más el del modelo.
        IAM: los de `AsyncSandbox.create()` y, por cada pasarela,
            `secretsmanager:GetSecretValue`.
        Cómo apagarla: `await deployment.stop()`; no llames a `deploy()` y
            nada se crea.
        Ejemplo:
            deployment = await AsyncAgentProject.load("./support-bot").deploy()
            (await deployment.invoke("Resume el README.")).text
            await deployment.stop()
        """
        from rayito._agent._project_service_async import AsyncProjectService

        return await AsyncProjectService(self).deploy(
            region=region,
            session=session,
            access_token=access_token,
            keep_on_failure=keep_on_failure,
        )

    async def export(  # type: ignore[override]
        self,
        *,
        target: str = "docker",
        out_dir: str | os.PathLike[str],
        overwrite: bool = False,
    ) -> AgentExport:
        """Como `AgentProject.export()`; la escritura de ficheros corre en un
        hilo para no bloquear el bucle de eventos."""
        return await asyncio.to_thread(
            super().export, target=target, out_dir=out_dir, overwrite=overwrite
        )


# ------------------------------------------------------------------ validation


def _validate_graph(graph: object, runtime: str | AgentRuntime) -> None:
    """`AgentProject.graph`: un nombre de grafo válido y sólo con el runtime
    `langgraph`."""
    if graph is None:
        return
    if not isinstance(graph, str) or not GRAPH_NAME_PATTERN.fullmatch(graph):
        raise InvalidArgumentException(
            f"AgentProject.graph debe casar con {GRAPH_NAME_PATTERN.pattern}"
        )
    if runtime_to_options(runtime).get("kind") != GRAPH_RUNTIME_KIND:
        raise InvalidArgumentException(
            f"AgentProject.graph sólo vale con el runtime {GRAPH_RUNTIME_KIND!r}"
        )


def _graph_entrypoint(
    graph: str, base_dir: str, read_text: Callable[[str], str] | None, path: str
) -> str:
    """El `entrypoint` del grafo `graph` del `langgraph.json` de `base_dir`,
    leído con el lector que inyecta `load` (sin él, `invalid_value`, como
    `instructions_file`). Sólo se lee ese fichero; nunca se importa el código
    del proyecto."""
    from rayito._agent._detect_entry import graph_entrypoint

    if read_text is None:
        raise _error(path, "invalid_value", "no hay lector de ficheros disponible")
    return graph_entrypoint(_langgraph_document(base_dir, read_text, path), graph, DETECTION_RULES)


def _langgraph_document(base_dir: str, read_text: Callable[[str], str], path: str) -> object:
    """El JSON de `langgraph.json`, o `None` si no existe o no es JSON."""
    try:
        return json.loads(read_text(_inside(base_dir, LANGGRAPH_FILE, path)))
    except (AgentProjectException, ValueError):
        return None


def _optional_text(value: object, label: str) -> None:
    if value is not None and (not isinstance(value, str) or not value):
        raise InvalidArgumentException(f"{label} debe ser una cadena no vacía")


def _text_tuple(values: object, label: str) -> tuple[str, ...]:
    if isinstance(values, str) or not isinstance(values, Sequence):
        raise InvalidArgumentException(f"{label} debe ser una lista de cadenas")
    if not all(isinstance(item, str) and item for item in values):
        raise InvalidArgumentException(f"{label} sólo admite cadenas no vacías")
    return tuple(values)


def _validated_gateways(gateways: object) -> Mapping[str, SecretGateway]:
    if not isinstance(gateways, Mapping):
        raise InvalidArgumentException("AgentProject.gateways debe ser un dict nombre -> pasarela")
    for name, gateway in gateways.items():
        validate_name(name, "AgentProject.gateways")
        if not isinstance(gateway, SecretGateway):
            raise InvalidArgumentException(
                "cada valor de AgentProject.gateways debe ser un SecretGateway"
            )
    return gateways


# --------------------------------------------------------------------- reading


class _Table:
    """Lectura estricta de una tabla: cada clave que se pide queda marcada
    y `finish()` falla con la primera que sobra."""

    def __init__(self, data: object, path: str) -> None:
        if not isinstance(data, Mapping):
            raise _error(path or "<raíz>", "invalid_type", "debe ser una tabla")
        self._data = data
        self.path = path
        self._used: set[str] = set()

    def at(self, key: str) -> str:
        return f"{self.path}.{key}" if self.path else key

    def has(self, key: str) -> bool:
        return key in self._data

    def raw(self, key: str) -> object:
        self._used.add(key)
        return self._data.get(key)

    def text(self, key: str, *, required: bool = False) -> str | None:
        value = self._value(key, required)
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise _error(self.at(key), "invalid_type", "debe ser una cadena no vacía")
        return value

    def boolean(self, key: str) -> bool | None:
        value = self._value(key, False)
        if value is not None and not isinstance(value, bool):
            raise _error(self.at(key), "invalid_type", "debe ser true o false")
        return value

    def integer(self, key: str) -> int | None:
        value = self._value(key, False)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise _error(self.at(key), "invalid_type", "debe ser un entero")
        return value

    def number(self, key: str) -> float | None:
        value = self._value(key, False)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int | float)):
            raise _error(self.at(key), "invalid_type", "debe ser un número")
        return value

    def texts(self, key: str) -> tuple[str, ...] | None:
        value = self._value(key, False)
        if value is None:
            return None
        if (
            isinstance(value, str)
            or not isinstance(value, Sequence)
            or not all(isinstance(item, str) and item for item in value)
        ):
            raise _error(self.at(key), "invalid_type", "debe ser una lista de cadenas")
        return tuple(value)

    def table(self, key: str) -> _Table | None:
        value = self._value(key, False)
        return None if value is None else _Table(value, self.at(key))

    def entries(self, key: str) -> list[tuple[str, _Table]]:
        table = self.table(key)
        if table is None:
            return []
        return [(name, _Table(table.raw(name), table.at(name))) for name in sorted(table.keys())]

    def keys(self) -> list[str]:
        return list(self._data)

    def _value(self, key: str, required: bool) -> object:
        self._used.add(key)
        value = self._data.get(key)
        if value is None and required:
            raise _error(self.at(key), "missing_key", "es obligatoria")
        return value

    def finish(self) -> None:
        for key in sorted(set(self._data) - self._used):
            code = "secret_value" if key.lower() in SECRET_LIKE_KEYS else "unknown_key"
            detail = (
                "un fichero de proyecto nunca lleva un secreto; usa el nombre de un secreto"
                if code == "secret_value"
                else "clave desconocida"
            )
            raise _error(self.at(key), code, detail)


def _guard(path: str, build: Callable[[], _T]) -> _T:
    try:
        return build()
    except AgentProjectException:
        raise
    except InvalidArgumentException as exc:
        raise _error(path, "invalid_value", str(exc)) from exc


def _inside(base_dir: str, relative: str, path: str) -> str:
    """`relative` resuelto contra `base_dir` sin tocar el disco; una ruta
    absoluta o que sale de `base_dir` es `path_escape`."""
    if posixpath.isabs(relative) or os.path.isabs(relative):
        raise _error(path, "path_escape", "debe ser relativa al directorio del fichero")
    root = os.path.abspath(base_dir)
    resolved = os.path.abspath(os.path.join(root, relative))
    if os.path.commonpath([root, resolved]) != root:
        raise _error(path, "path_escape", "sale del directorio del fichero")
    return resolved


def _project_kwargs(
    data: Mapping[str, object], base_dir: str, read_text: Callable[[str], str] | None
) -> dict[str, Any]:
    root = _Table(data, "")
    schema = root.text("schema", required=True)
    if schema != AGENT_PROJECT_SCHEMA:
        raise _error("schema", "unsupported_schema", f"debe ser {AGENT_PROJECT_SCHEMA!r}")
    name = root.text("name", required=True)
    runtime, runtime_extras = _read_runtime(root.table("runtime"), base_dir, read_text)
    kwargs: dict[str, Any] = {"name": name, "runtime": runtime, **runtime_extras}
    kwargs.update(_read_source(root.table("source"), base_dir))
    gateways = _read_gateways(root)
    model_table = root.table("model")
    if model_table is None:
        raise _error("model", "missing_key", "es obligatoria")
    model = _read_model(model_table)
    kwargs["gateways"] = gateways
    kwargs["spec"] = _read_agent(root.table("agent"), model, read_text, base_dir)
    kwargs.update(_read_limits(root.table("limits")))
    kwargs.update(_read_serve(root.table("serve")))
    kwargs.update(_read_scaling(root.table("scaling")))
    kwargs.update(_read_env(root.table("env")))
    kwargs.update(_read_secret_env(root))
    kwargs.update(_read_network(root.table("network")))
    kwargs.update(_read_deploy(root.table("deploy")))
    kwargs.update(_read_image(root.table("image")))
    root.finish()
    _check_gateways(kwargs["spec"], gateways)
    _check_concurrency(kwargs)
    presets = _gateway_presets(data)
    _check_model(kwargs, presets)
    kwargs["gateway_presets"] = presets
    return kwargs


def _gateway_presets(data: Mapping[str, object]) -> dict[str, str]:
    """`[gateways.<nombre>] preset` de cada pasarela del documento (ya
    validado): `AgentProject` sólo guarda la pasarela construida."""
    gateways = data.get("gateways")
    if not isinstance(gateways, Mapping):
        return {}
    return {
        str(name): str(entry["preset"])
        for name, entry in gateways.items()
        if isinstance(entry, Mapping) and isinstance(entry.get("preset"), str)
    }


def _validated_presets(presets: Mapping[str, str]) -> Mapping[str, str]:
    """`AgentProject.gateway_presets` de sólo lectura: un mapeo de cadenas."""
    if not isinstance(presets, Mapping) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in presets.items()
    ):
        raise InvalidArgumentException(
            "AgentProject.gateway_presets debe ser un mapeo pasarela -> preset (cadenas)"
        )
    return MappingProxyType(dict(presets))


def _check_model(kwargs: Mapping[str, Any], presets: Mapping[str, str]) -> None:
    """El id del modelo donde Rayito lo usa y los nombres de `[model]` que
    no repiten una variable de `[env]` (`conflict`)."""
    model: AgentModel = kwargs["spec"].model
    runtime = runtime_to_options(kwargs["runtime"])
    require_model_id(
        model.id,
        runtime=str(runtime["kind"]),
        entrypoint=runtime.get("entrypoint") is not None,
        gateway_preset=presets.get(model.gateway),
    )
    env = kwargs.get("env", {})
    for key, name in model.env_names().items():
        if name in env:
            raise _error(f"model.{key}", "conflict", "esa variable ya está en [env]")


def _check_concurrency(kwargs: Mapping[str, Any]) -> None:
    serve = kwargs.get("serve", ServeOptions())
    problem = _concurrency_problem(serve, kwargs.get("memory_mib", AGENT_MIN_MEMORY_MIB))
    if problem is not None:
        raise _error("serve.max_concurrency", "invalid_value", problem)


def _check_gateways(spec: AgentSpec, gateways: Mapping[str, SecretGateway]) -> None:
    """Cada pasarela que el agente necesita debe estar en `[gateways]`; el
    error nombra la clave que la pide."""
    if spec.model.gateway not in gateways:
        raise _error(
            "model.gateway", "invalid_value", "la pasarela no está declarada en [gateways]"
        )
    for name, server in sorted(spec.mcp.items()):
        if isinstance(server, McpRemote) and server.gateway not in gateways:
            raise _error(
                f"agent.mcp.{name}.gateway",
                "invalid_value",
                "la pasarela no está declarada en [gateways]",
            )


def _read_runtime(
    table: _Table | None, base_dir: str, read_text: Callable[[str], str] | None
) -> tuple[AgentRuntime, dict[str, object]]:
    if table is None:
        raise _error("runtime", "missing_key", "es obligatoria")
    kind = table.text("kind", required=True)
    if kind not in RUNTIME_KINDS:
        raise _error("runtime.kind", "invalid_value", f"debe ser uno de {list(RUNTIME_KINDS)}")
    extras = _read_runtime_extras(table)
    options = {key: table.raw(key) for key in RUNTIME_OPTION_KEYS[kind]}
    table.finish()
    present = {key: value for key, value in options.items() if value is not None}
    graph = extras.get(GRAPH_KEY)
    if isinstance(graph, str):
        present[ENTRYPOINT_KEY] = _graph_entrypoint(graph, base_dir, read_text, table.at(GRAPH_KEY))
    runtime = _guard("runtime", lambda: runtime_from_options(str(kind), present))
    return runtime, extras


def _read_runtime_extras(table: _Table) -> dict[str, object]:
    """Las claves de `[runtime]` que son del proyecto y no de un runtime:
    `language`, `framework` y las versiones de intérprete."""
    extras: dict[str, object] = {}
    for key in ("language", "framework"):
        value = table.text(key)
        if value is not None:
            extras[key] = value
    if extras.get("language", AGENT_LANGUAGES[0]) not in AGENT_LANGUAGES:
        raise _error(
            table.at("language"), "invalid_value", f"debe ser uno de {list(AGENT_LANGUAGES)}"
        )
    python = _read_version(table, "python_version", "python")
    node = _read_version(table, "node_version", "node")
    if python is not None or node is not None:
        extras["versions"] = RuntimeVersions(python=python, node=node)
    graph = _read_graph(table)
    if graph is not None:
        extras[GRAPH_KEY] = graph
    return extras


def _read_graph(table: _Table) -> str | None:
    """`[runtime] graph`: un nombre válido, sólo con `kind = "langgraph"` y
    nunca junto a `entrypoint` (`conflict`)."""
    graph = table.text(GRAPH_KEY)
    if graph is None:
        return None
    path = table.at(GRAPH_KEY)
    if table.raw("kind") != GRAPH_RUNTIME_KIND:
        raise _error(path, "invalid_value", f"sólo vale con kind = {GRAPH_RUNTIME_KIND!r}")
    if not GRAPH_NAME_PATTERN.fullmatch(graph):
        raise _error(path, "invalid_value", f"debe casar con {GRAPH_NAME_PATTERN.pattern}")
    if table.has(ENTRYPOINT_KEY):
        raise _error(path, "conflict", DETECTION_RULES["messages"]["graph_conflict"])
    return graph


def _read_version(table: _Table, key: str, field_name: str) -> str | None:
    value = table.text(key)
    if value is not None:
        _guard(table.at(key), lambda: RuntimeVersions(**{field_name: value}))
    return value


def _read_source(table: _Table | None, base_dir: str) -> dict[str, object]:
    if table is None:
        return {}
    path = table.text("path") or "."
    requirements = table.text("requirements")
    exclude = table.texts("exclude") or ()
    table.finish()
    kwargs: dict[str, object] = {
        "source": _inside(base_dir, path, "source.path"),
        "exclude": exclude,
    }
    if requirements is not None:
        kwargs["requirements"] = _inside(base_dir, requirements, "source.requirements")
    return kwargs


def _read_model(table: _Table) -> AgentModel:
    provider = table.text("provider", required=True)
    model_id = table.text("id")
    gateway = table.text("gateway", required=True)
    region = table.text("region")
    base_path = table.text("base_path")
    caching = table.boolean("prompt_caching")
    extra: dict[str, object] = _read_model_env_names(table)
    table.finish()
    if caching is not None:
        extra["prompt_caching"] = caching
    return _guard(
        "model",
        lambda: AgentModel(
            provider=provider,  # type: ignore[arg-type]
            id=model_id,
            gateway=str(gateway),
            region=region,
            base_path=base_path or "",
            **extra,  # type: ignore[arg-type]
        ),
    )


def _read_model_env_names(table: _Table) -> dict[str, object]:
    """Las claves `MODEL_ENV_KEYS` de `[model]` que el fichero fija: nombres
    de variable, nunca valores."""
    names: dict[str, object] = {}
    for key in MODEL_ENV_KEYS:
        name = table.text(key)
        if name is None:
            continue
        if not _ENV_NAME_PATTERN.fullmatch(name):
            raise _error(table.at(key), "invalid_value", "debe ser un nombre de variable")
        names[key] = name
    return names


def _read_agent(
    table: _Table | None,
    model: AgentModel,
    read_text: Callable[[str], str] | None,
    base_dir: str,
) -> AgentSpec:
    if table is None:
        return AgentSpec(model=model)
    instructions = _read_instructions(table, read_text, base_dir)
    small_model = table.text("small_model")
    runtime_version = table.text("runtime_version")
    permissions = _read_permissions(table.table("permissions"), table.at("permissions"))
    mcp = {name: _read_mcp(entry) for name, entry in table.entries("mcp")}
    agents = {name: _read_subagent(entry) for name, entry in table.entries("agents")}
    raw_config = table.raw("raw_config")
    if raw_config is not None and not isinstance(raw_config, Mapping):
        raise _error(table.at("raw_config"), "invalid_type", "debe ser una tabla")
    table.finish()
    return _guard(
        table.path,
        lambda: AgentSpec(
            model=model,
            small_model=small_model,
            instructions=instructions,
            mcp=mcp,
            permissions=permissions,
            agents=agents,
            raw_config=raw_config,
            runtime_version=runtime_version,
        ),
    )


def _read_instructions(
    table: _Table, read_text: Callable[[str], str] | None, base_dir: str
) -> str | None:
    inline = table.text("instructions")
    file = table.text("instructions_file")
    if inline is not None and file is not None:
        raise _error(
            table.at("instructions_file"), "conflict", "es excluyente con agent.instructions"
        )
    if file is None:
        return inline
    resolved = _inside(base_dir, file, table.at("instructions_file"))
    if read_text is None:
        raise _error(
            table.at("instructions_file"), "invalid_value", "no hay lector de ficheros disponible"
        )
    return read_text(resolved)


def _read_permissions(table: _Table | None, path: str) -> AgentPermissions:
    if table is None:
        return AgentPermissions()
    default = table.text("default")
    tools = table.raw("tools")
    if tools is not None and not isinstance(tools, Mapping):
        raise _error(table.at("tools"), "invalid_type", "debe ser una tabla")
    table.finish()
    kwargs: dict[str, object] = {}
    if default is not None:
        kwargs["default"] = default
    if tools is not None:
        kwargs["tools"] = tools
    return _guard(path, lambda: AgentPermissions(**kwargs))  # type: ignore[arg-type]


def _read_mcp(entry: _Table) -> McpServer:
    kind = entry.text("type", required=True)
    if kind == "local":
        command = entry.texts("command")
        envs = entry.raw("envs")
        timeout = entry.number("timeout_seconds")
        entry.finish()
        if envs is not None and not isinstance(envs, Mapping):
            raise _error(entry.at("envs"), "invalid_type", "debe ser una tabla")
        return _guard(
            entry.path,
            lambda: McpLocal(command=command or (), envs=envs or {}, timeout_seconds=timeout),
        )
    if kind == "remote":
        gateway = entry.text("gateway", required=True)
        remote_path = entry.text("path")
        entry.finish()
        return _guard(entry.path, lambda: McpRemote(gateway=str(gateway), path=remote_path or "/"))
    raise _error(entry.at("type"), "invalid_value", "debe ser 'local' o 'remote'")


def _read_subagent(entry: _Table) -> SubAgent:
    description = entry.text("description", required=True)
    instructions = entry.text("instructions", required=True)
    model = entry.text("model")
    permissions = entry.table("permissions")
    parsed = None if permissions is None else _read_permissions(permissions, permissions.path)
    entry.finish()
    return _guard(
        entry.path,
        lambda: SubAgent(
            description=str(description),
            instructions=str(instructions),
            model=model,
            permissions=parsed,
        ),
    )


def _read_limits(table: _Table | None) -> dict[str, object]:
    if table is None:
        return {}
    kwargs: dict[str, object] = {}
    for key, value in (
        ("max_steps", table.integer("max_steps")),
        ("timeout_seconds", table.number("timeout_seconds")),
        ("max_output_bytes", table.integer("max_output_bytes")),
    ):
        if value is not None:
            kwargs[key] = value
    max_total_tokens = table.integer("max_total_tokens")
    if max_total_tokens is not None:
        kwargs["max_total_tokens"] = max_total_tokens or None
    table.finish()
    return {"limits": _guard("limits", lambda: AgentLimits(**kwargs))}  # type: ignore[arg-type]


def _read_serve(table: _Table | None) -> dict[str, object]:
    if table is None:
        return {}
    kwargs: dict[str, object] = {}
    for key in ("port", "max_concurrency", "queue_max"):
        value = table.integer(key)
        if value is not None:
            kwargs[key] = value
    for key in ("timeout_seconds", "queue_timeout_seconds"):
        number = table.number(key)
        if number is not None:
            kwargs[key] = number
    idle_seconds = table.integer("idle_seconds")
    if idle_seconds is not None:
        kwargs["idle"] = (
            None
            if idle_seconds == 0
            else _guard("serve.idle_seconds", lambda: IdlePolicy(max_idle_seconds=idle_seconds))
        )
    metadata = table.raw("metadata")
    if metadata is not None:
        kwargs["metadata"] = metadata
    table.finish()
    return {"serve": _guard("serve", lambda: ServeOptions(**kwargs))}  # type: ignore[arg-type]


def _read_network(table: _Table | None) -> dict[str, object]:
    if table is None:
        return {}
    internet = table.boolean("allow_internet_access")
    egress = table.texts("egress")
    allow_out = table.texts("allow_out")
    deny_out = table.texts("deny_out")
    table.finish()
    kwargs: dict[str, object] = {}
    if internet is not None:
        kwargs["allow_internet_access"] = internet
    if egress is not None:
        kwargs["egress"] = egress
    if allow_out is not None or deny_out is not None:
        kwargs["network"] = NetworkPolicy(allow_out=allow_out or (), deny_out=deny_out or ())
    return kwargs


def _read_scaling(table: _Table | None) -> dict[str, object]:
    if table is None:
        return {}
    kwargs: dict[str, object] = {}
    for key in ("min_vms", "max_vms", "warm_vms"):
        value = table.integer(key)
        if value is not None:
            kwargs[key] = value
    sessions = table.text("sessions")
    if sessions is not None:
        kwargs["sessions"] = sessions
    seconds = table.number("scale_in_after_seconds")
    if seconds is not None:
        kwargs["scale_in_after_seconds"] = seconds
    table.finish()
    return {"scaling": _guard("scaling", lambda: ScalingOptions(**kwargs))}  # type: ignore[arg-type]


def _read_env(table: _Table | None) -> dict[str, object]:
    if table is None:
        return {}
    env: dict[str, str] = {}
    for name in sorted(table.keys()):
        path = table.at(name)
        value = table.raw(name)
        if not isinstance(value, str):
            raise _error(path, "invalid_type", "debe ser una cadena")
        if _secret_like_env_name(name) or value.startswith(SECRET_VALUE_PREFIXES):
            raise _error(
                path,
                "secret_value",
                "un fichero de proyecto nunca lleva un secreto; usa una pasarela",
            )
        env[name] = value
    table.finish()
    return {"env": _guard("env", lambda: _validated_env(env))}


def _read_secret_env_value(table: _Table, name: str) -> SecretEnvValue:
    path = table.at(name)
    check_secret_env_name(name, path)
    value = table.raw(name)
    if not isinstance(value, Mapping):
        return check_secret_id(value, path)
    reference = _Table(value, path)
    secret = check_secret_id(reference.text("secret", required=True), reference.at("secret"))
    key = reference.text("key")
    stage = reference.text("version_stage")
    reference.finish()
    return check_secret_env_ref(SecretEnvRef(secret, key=key, version_stage=stage), path)


def _read_secret_env_source(entry: _Table) -> SecretEnvSource:
    secret = check_secret_id(entry.text("secret", required=True), entry.at("secret"))
    prefix = entry.raw("prefix")
    if prefix is not None and not isinstance(prefix, str):
        raise _error(entry.at("prefix"), "invalid_type", "debe ser una cadena")
    keys = entry.texts("keys")
    entry.finish()
    source = SecretEnvSource(secret, prefix=prefix or "", keys=keys)
    return check_secret_env_source(source, entry.path)


def _read_secret_env(root: _Table) -> dict[str, object]:
    """`[secret_env]` y `[[secret_env_from]]` (ADR-033 D4), sólo si están."""
    kwargs: dict[str, object] = {}
    table = root.table(SECRET_ENV_KEY)
    if table is not None:
        kwargs["secret_env"] = {
            name: _read_secret_env_value(table, name) for name in sorted(table.keys())
        }
        table.finish()
    sources = root.raw(SECRET_ENV_FROM_KEY)
    if sources is not None:
        if isinstance(sources, str) or not isinstance(sources, Sequence):
            raise _error(SECRET_ENV_FROM_KEY, "invalid_type", "debe ser una lista de tablas")
        kwargs["secret_env_from"] = tuple(
            _read_secret_env_source(_Table(item, secret_env_from_path(index)))
            for index, item in enumerate(sources)
        )
    return kwargs


_DEPLOY_CHECKS: Final[Mapping[str, Callable[[object, str], str]]] = {
    "bucket": validate_bucket_hint,
    "base_image_version": validate_image_version,
}


def _checked(path: str, check: Callable[[object, str], str], value: str) -> str:
    return _guard(path, lambda: check(value, path))


def _read_deploy(table: _Table | None) -> dict[str, object]:
    if table is None:
        return {}
    kwargs: dict[str, object] = {}
    for key in ("region", "bucket", "base_image_version"):
        value = table.text(key)
        if value is None:
            continue
        check = _DEPLOY_CHECKS.get(key)
        kwargs[key] = value if check is None else _checked(table.at(key), check, value)
    table.finish()
    return kwargs


def _read_image(table: _Table | None) -> dict[str, object]:
    if table is None:
        return {}
    kwargs: dict[str, object] = {}
    for key in ("base", "template"):
        value = table.text(key)
        if value is not None:
            kwargs[key] = value
    memory = table.integer("memory_mib")
    if memory is not None:
        kwargs["memory_mib"] = memory
    table.finish()
    return kwargs


# -------------------------------------------------------------------- gateways


def _secret_name(value: object, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise _error(path, "invalid_type", "debe ser el nombre de un secreto")
    if any(char.isspace() for char in value) or value.startswith(SECRET_VALUE_PREFIXES):
        raise _error(
            path, "secret_value", "parece un valor y no el nombre de un secreto; escribe el nombre"
        )
    return value


@dataclass(frozen=True)
class _Preset:
    keys: tuple[str, ...]
    required: tuple[str, ...]
    build: Callable[..., SecretGateway]


#: `preset` -> claves que admite (además de `secret` y `rate_per_minute`),
#: cuáles son obligatorias y la función de `_gateways.py` que lo construye.
_GATEWAY_PRESETS: Final[Mapping[str, _Preset]] = {
    "bedrock": _Preset(("region", "models"), ("region", "models"), bedrock_gateway),
    "anthropic": _Preset((), (), anthropic_gateway),
    "openai": _Preset((), (), openai_gateway),
    "openai-compatible": _Preset(
        ("upstream", "base_path"), ("upstream",), openai_compatible_gateway
    ),
    "gemini": _Preset(("models",), ("models",), gemini_gateway),
    "azure": _Preset(("resource",), ("resource",), azure_openai_gateway),
    "openrouter": _Preset((), (), openrouter_gateway),
    "groq": _Preset((), (), groq_gateway),
    "mistral": _Preset((), (), mistral_gateway),
    "deepseek": _Preset((), (), deepseek_gateway),
    "xai": _Preset((), (), xai_gateway),
    "litellm": _Preset(("upstream", "base_path"), ("upstream",), litellm_gateway),
}


#: Las pasarelas que filtran por modelo (la lista `models` de su ruta): con
#: ellas el id del modelo es obligatorio (ADR-033 D2).
MODEL_ID_REQUIRED_PRESETS: Final[tuple[str, ...]] = tuple(
    name for name, preset in _GATEWAY_PRESETS.items() if _MODELS_KEY in preset.required
)
_MODEL_ID_PATH: Final = "model.id"
_MISSING_KEY: Final = "missing_key"
_NOT_DETECTED: Final = "not_detected"


def model_id_required(*, runtime: str, entrypoint: bool, gateway_preset: str | None) -> bool:
    """Si Rayito necesita el id del modelo: lo pide una pasarela que filtra
    por modelo o un runtime que construye el modelo
    (`testdata/agent-model-optional/requirement.json`)."""
    if gateway_preset in MODEL_ID_REQUIRED_PRESETS or runtime in MODEL_BUILDING_RUNTIMES:
        return True
    return runtime in DEFAULT_AGENT_RUNTIMES and not entrypoint


def require_model_id(
    model_id: str | None,
    *,
    runtime: str,
    entrypoint: bool,
    gateway_preset: str | None,
    detected: bool = False,
) -> None:
    """`AgentProjectException` en `model.id` si falta y hace falta:
    `not_detected` si el proyecto sale de la detección y `missing_key` si
    sale de un fichero."""
    if model_id is not None or not model_id_required(
        runtime=runtime, entrypoint=entrypoint, gateway_preset=gateway_preset
    ):
        return
    if detected:
        raise _error(
            _MODEL_ID_PATH,
            _NOT_DETECTED,
            "no se encontró el id del modelo en el código: escríbelo como literal, pásalo con "
            "--model-id o define [model] id en rayito-agent.toml (rayito agent detect --write)",
        )
    raise _error(
        _MODEL_ID_PATH,
        _MISSING_KEY,
        "es obligatoria con esta pasarela o con un runtime que construye el modelo",
    )


def _read_gateways(root: _Table) -> dict[str, SecretGateway]:
    return {name: _read_gateway(name, entry) for name, entry in root.entries("gateways")}


def _read_gateway(name: str, entry: _Table) -> SecretGateway:
    preset = entry.text("preset", required=True)
    rate = entry.integer(_RATE_KEY)
    if preset == CUSTOM_GATEWAY_PRESET:
        return _read_custom_gateway(entry, rate)
    spec = _GATEWAY_PRESETS.get(str(preset))
    if spec is None:
        allowed = [*_GATEWAY_PRESETS, CUSTOM_GATEWAY_PRESET]
        raise _error(entry.at("preset"), "invalid_value", f"debe ser uno de {allowed}")
    secret = _secret_name(entry.text("secret", required=True), entry.at("secret"))
    options: dict[str, object] = {}
    for key in spec.keys:
        value = entry.texts(key) if key == "models" else entry.text(key)
        if value is None and key in spec.required:
            raise _error(entry.at(key), "missing_key", "es obligatoria con este preset")
        if value is not None:
            options[key] = value
    entry.finish()
    if rate is not None:
        options[_RATE_KEY] = rate
    return _guard(entry.path, lambda: spec.build(secret, **options))


def _read_custom_gateway(entry: _Table, rate: int | None) -> SecretGateway:
    upstream = entry.text("upstream", required=True)
    headers = entry.table("headers")
    if headers is None:
        raise _error(entry.at("headers"), "missing_key", "es obligatoria con preset 'custom'")
    secrets = {
        header: _secret_name(headers.raw(header), headers.at(header))
        for header in sorted(headers.keys())
    }
    allow = _read_allow(entry.raw("allow"), entry.at("allow"))
    entry.finish()
    kwargs: dict[str, object] = {} if rate is None else {_RATE_KEY: rate}
    return _guard(
        entry.path,
        lambda: SecretGateway(
            upstream=str(upstream),
            headers=secrets,
            allow=allow,
            **kwargs,  # type: ignore[arg-type]
        ),
    )


def _read_allow(value: object, path: str) -> list[tuple[str, str]]:
    if value is None:
        raise _error(path, "missing_key", "es obligatoria con preset 'custom'")
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise _error(path, "invalid_type", "debe ser una lista de [método, ruta]")
    rules: list[tuple[str, str]] = []
    for rule in value:
        if (
            isinstance(rule, str)
            or not isinstance(rule, Sequence)
            or len(rule) != 2
            or not all(isinstance(part, str) for part in rule)
        ):
            raise _error(path, "invalid_type", "cada regla debe ser [método, ruta]")
        rules.append((rule[0], rule[1]))
    return rules


# ---------------------------------------------------------------------- writing


def _relative(path: str, base_dir: str) -> str:
    return os.path.relpath(path, base_dir).replace(os.sep, "/")


def _source_to_dict(project: AgentProject) -> dict[str, object]:
    table: dict[str, object] = {}
    if project.source is not None:
        table["path"] = _relative(project.source, project.base_dir)
    if project.requirements is not None:
        table["requirements"] = _relative(project.requirements, project.base_dir)
    if project.exclude:
        table["exclude"] = list(project.exclude)
    return table


def _model_to_dict(model: AgentModel) -> dict[str, object]:
    """`[model]` canónico: `id` y los nombres de variable sólo si están
    puestos, así que un proyecto con id se escribe igual que antes."""
    table: dict[str, object] = {"provider": model.provider}
    if model.id is not None:
        table["id"] = model.id
    table["gateway"] = model.gateway
    if model.region is not None:
        table["region"] = model.region
    if model.base_path:
        table["base_path"] = model.base_path
    if not model.prompt_caching:
        table["prompt_caching"] = False
    table.update(model.env_names())
    return table


def _secret_env_to_dict(project: AgentProject) -> dict[str, object]:
    table: dict[str, object] = {}
    for name, value in sorted(project.secret_env.items()):
        if isinstance(value, str):
            table[name] = value
            continue
        reference: dict[str, object] = {"secret": value.secret}
        if value.key is not None:
            reference["key"] = value.key
        if value.version_stage is not None:
            reference["version_stage"] = value.version_stage
        table[name] = reference
    return table


def _secret_env_from_to_dict(project: AgentProject) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for source in project.secret_env_from:
        entry: dict[str, object] = {"secret": source.secret}
        if source.prefix:
            entry["prefix"] = source.prefix
        if source.keys is not None:
            entry["keys"] = list(source.keys)
        entries.append(entry)
    return entries


def _secret_ref_name(value: str | SecretRef) -> str:
    if isinstance(value, str):
        return value
    if value.version_id is not None or value.version_stage is not None:
        raise InvalidArgumentException(
            "una pasarela con un SecretRef de versión fija no se puede escribir en el fichero"
        )
    return value.name


def _gateway_to_dict(gateway: SecretGateway) -> dict[str, object]:
    table: dict[str, object] = {
        "preset": CUSTOM_GATEWAY_PRESET,
        "upstream": gateway.upstream,
        "headers": {name: _secret_ref_name(ref) for name, ref in sorted(gateway.headers.items())},
        "allow": [[method, route] for method, route in gateway.allow],
    }
    if gateway.rate_per_minute:
        table[_RATE_KEY] = gateway.rate_per_minute
    return table


def _permissions_to_dict(permissions: AgentPermissions) -> dict[str, object]:
    table: dict[str, object] = {}
    if permissions.default != AgentPermissions().default:
        table["default"] = permissions.default
    if permissions.tools:
        table["tools"] = {
            tool: rule if isinstance(rule, str) else dict(rule)
            for tool, rule in sorted(permissions.tools.items())
        }
    return table


def _mcp_to_dict(server: McpServer) -> dict[str, object]:
    if isinstance(server, McpRemote):
        remote: dict[str, object] = {"type": "remote", "gateway": server.gateway}
        if server.path != "/":
            remote["path"] = server.path
        return remote
    local: dict[str, object] = {"type": "local", "command": list(server.command)}
    if server.envs:
        local["envs"] = dict(server.envs)
    if server.timeout_seconds is not None:
        local["timeout_seconds"] = server.timeout_seconds
    return local


def _subagent_to_dict(agent: SubAgent) -> dict[str, object]:
    table: dict[str, object] = {
        "description": agent.description,
        "instructions": agent.instructions,
    }
    if agent.model is not None:
        table["model"] = agent.model
    if agent.permissions is not None:
        table["permissions"] = _permissions_to_dict(agent.permissions)
    return table


def _agent_to_dict(spec: AgentSpec) -> dict[str, object]:
    table: dict[str, object] = {}
    if spec.instructions is not None:
        table["instructions"] = spec.instructions
    if spec.small_model is not None:
        table["small_model"] = spec.small_model
    if spec.runtime_version is not None:
        table["runtime_version"] = spec.runtime_version
    permissions = _permissions_to_dict(spec.permissions)
    if permissions:
        table["permissions"] = permissions
    if spec.mcp:
        table["mcp"] = {name: _mcp_to_dict(server) for name, server in sorted(spec.mcp.items())}
    if spec.agents:
        table["agents"] = {
            name: _subagent_to_dict(agent) for name, agent in sorted(spec.agents.items())
        }
    if spec.raw_config:
        table["raw_config"] = dict(spec.raw_config)
    return table


def _limits_to_dict(limits: AgentLimits) -> dict[str, object]:
    defaults = AgentLimits()
    table: dict[str, object] = {}
    for key in ("max_steps", "timeout_seconds", "max_output_bytes"):
        if getattr(limits, key) != getattr(defaults, key):
            table[key] = getattr(limits, key)
    if limits.max_total_tokens != defaults.max_total_tokens:
        table["max_total_tokens"] = limits.max_total_tokens or 0
    return table


def _runtime_to_dict(project: AgentProject) -> dict[str, object]:
    table = runtime_to_options(project.runtime)
    if project.graph is not None:
        table.pop(ENTRYPOINT_KEY, None)
        table[GRAPH_KEY] = project.graph
    for key, value in (
        ("language", project.language),
        ("framework", project.framework),
        ("python_version", project.versions.python),
        ("node_version", project.versions.node),
    ):
        if value is not None and key not in table:
            table[key] = value
    return table


def _scaling_to_dict(scaling: ScalingOptions) -> dict[str, object]:
    defaults = ScalingOptions()
    return {
        key: getattr(scaling, key)
        for key in ("min_vms", "max_vms", "warm_vms", "sessions", "scale_in_after_seconds")
        if getattr(scaling, key) != getattr(defaults, key)
    }


def _deploy_to_dict(project: AgentProject) -> dict[str, object]:
    return {
        key: value
        for key, value in (
            ("region", project.region),
            ("bucket", project.bucket),
            ("base_image_version", project.base_image_version),
        )
        if value is not None
    }


def _serve_to_dict(serve: ServeOptions) -> dict[str, object]:
    defaults = ServeOptions()
    table: dict[str, object] = {}
    for key in (
        "port",
        "timeout_seconds",
        "max_concurrency",
        "queue_max",
        "queue_timeout_seconds",
    ):
        if getattr(serve, key) != getattr(defaults, key):
            table[key] = getattr(serve, key)
    if serve.idle is not DEFAULT_IDLE_POLICY:
        table["idle_seconds"] = _idle_seconds(serve.idle)
    if serve.metadata:
        table["metadata"] = dict(sorted(serve.metadata.items()))
    return table


def _idle_seconds(idle: IdlePolicy | None) -> int:
    if idle is None:
        return 0
    if idle.suspended_duration_seconds is not None or not idle.auto_resume:
        raise InvalidArgumentException(
            "ServeOptions.idle con suspended_duration_seconds o sin auto_resume no se puede "
            "escribir en el fichero"
        )
    return idle.max_idle_seconds


def _network_to_dict(project: AgentProject) -> dict[str, object]:
    table: dict[str, object] = {}
    if project.allow_internet_access:
        table["allow_internet_access"] = True
    if project.egress is not None:
        table["egress"] = list(project.egress)
    if project.network is not None:
        if project.network.allow_out:
            table["allow_out"] = list(project.network.allow_out)
        if project.network.deny_out:
            table["deny_out"] = list(project.network.deny_out)
    return table


def _image_to_dict(project: AgentProject) -> dict[str, object]:
    table: dict[str, object] = {}
    if project.base != DEFAULT_AGENT_TEMPLATE_BASE:
        table["base"] = project.base
    if project.template is not None:
        table["template"] = project.template
    if project.memory_mib != AGENT_MIN_MEMORY_MIB:
        table["memory_mib"] = project.memory_mib
    return table


__all__ = [
    "AGENT_LANGUAGES",
    "AGENT_PROJECT_ERROR_CODES",
    "AgentProject",
    "AgentProjectException",
    "AsyncAgentProject",
    "RuntimeVersions",
    "ServeOptions",
]
