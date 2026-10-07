"""Dominio puro del agente de IA (`ai-agent-core`, ADR-025): qué es un
`AgentSpec` válido. Nada de AWS, grpc, reloj ni I/O: el puerto
`AgentRuntime` y sus adaptadores (OpenCode, deepagents) traducen estos
valores a ficheros de configuración y a un comando; este módulo sólo valida
la forma de lo que el llamante escribió, antes de cualquier RPC.

Las credenciales del modelo nunca pasan por aquí: `AgentModel.gateway` y
`McpRemote.gateway` nombran una entrada de `sbx.gateways` (ADR-023), y la
pasarela inyecta la cabecera real fuera del alcance del código del sandbox.
Por eso ningún tipo tiene un campo de clave ni de cabeceras.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final, Literal, TypeAlias, get_args

from rayito._limits import (
    COMMAND_OUTPUT_MAX_BYTES,
    DEFAULT_AGENT_MAX_OUTPUT_BYTES,
    DEFAULT_AGENT_MAX_STEPS,
    DEFAULT_AGENT_MAX_TOTAL_TOKENS,
    DEFAULT_AGENT_TIMEOUT_SECONDS,
)
from rayito._secret_gateway._domain import is_safe_request_path, validate_route_name
from rayito.exceptions import InvalidArgumentException

ModelProvider: TypeAlias = Literal[
    "bedrock", "anthropic", "openai-compatible", "openai", "google", "azure"
]
PermissionAction: TypeAlias = Literal["allow", "deny"]
ToolPermission: TypeAlias = "PermissionAction | Mapping[str, PermissionAction]"

#: Los proveedores que un runtime sabe hablar a través de una pasarela
#: (`SecretGateway`): Bedrock `Converse`, la Messages API de Anthropic,
#: cualquier API con `chat/completions` de OpenAI (OpenRouter, Groq,
#: Mistral, DeepSeek, LiteLLM), la Responses API de OpenAI (también xAI),
#: Gemini y Azure OpenAI v1 (`testdata/agent/provider-catalogue.json`).
MODEL_PROVIDERS: Final[tuple[str, ...]] = get_args(ModelProvider)
PERMISSION_ACTIONS: Final[tuple[str, ...]] = get_args(PermissionAction)
#: El runtime que usa `sbx.agent.run()` si no se pasa `runtime=`.
DEFAULT_AGENT_RUNTIME: Final = "opencode"
#: OpenCode (`--auto`) y deepagents corren sin nadie que conteste: una
#: acción `"ask"` colgaría la ejecución hasta el timeout.
ASK_ACTION: Final = "ask"
#: Herramientas denegadas por defecto, por debajo de las entradas del
#: llamante: `question` espera una respuesta humana que nunca llega, y
#: `webfetch`/`websearch` sólo gastan turnos del modelo con el egress
#: cerrado (deny-all, la postura recomendada).
DEFAULT_DENIED_TOOLS: Final[tuple[str, ...]] = ("question", "webfetch", "websearch")
#: Claves de la configuración de OpenCode que escribe el adaptador:
#: `raw_config` no puede tocarlas, porque llevarían el modelo fuera de la
#: pasarela (`provider`, `enabled_providers`), reactivarían descargas
#: (`autoupdate`, `share`) o anularían permisos y límites.
RESERVED_CONFIG_KEYS: Final[tuple[str, ...]] = (
    "provider",
    "autoupdate",
    "share",
    "enabled_providers",
    "model",
    "small_model",
    "mcp",
    "agent",
    "permission",
    "instructions",
)
#: El agente principal que el adaptador de OpenCode configura; un subagente
#: con este nombre lo sustituiría.
RESERVED_AGENT_NAMES: Final[tuple[str, ...]] = ("build",)
#: Nombres de `agents`, `mcp` y herramientas: se escriben como claves JSON
#: de la configuración del runtime y aparecen en eventos, así que sólo
#: admiten un alfabeto corto y nunca son secretos.
MAX_NAME_LEN: Final = 64
#: El identificador más largo que aceptamos para un modelo: un ARN de
#: perfil de inferencia de Bedrock mide menos de 200 caracteres.
MAX_MODEL_ID_LEN: Final = 256
#: Un patrón de herramienta (`bash` → `"git *"`) se compara en el runtime
#: como glob; sólo se limita su tamaño y que no lleve controles.
MAX_TOOL_PATTERN_LEN: Final = 256

_NAME_PATTERN: Final = re.compile(rf"[a-z0-9][a-z0-9_-]{{0,{MAX_NAME_LEN - 1}}}")
_TOOL_NAME_PATTERN: Final = re.compile(rf"[a-z0-9*][a-z0-9_*-]{{0,{MAX_NAME_LEN - 1}}}")
_MODEL_ID_PATTERN: Final = re.compile(rf"[A-Za-z0-9][A-Za-z0-9._:/@+-]{{0,{MAX_MODEL_ID_LEN - 1}}}")
_REGION_PATTERN: Final = re.compile(r"[a-z]{2}(-[a-z]+)+-[0-9]+")
_ENV_NAME_PATTERN: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_FIRST_PRINTABLE_ASCII: Final = 0x20
_ASCII_DELETE: Final = 0x7F


def _frozen_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise InvalidArgumentException(f"{label} debe ser un dict")
    return MappingProxyType(dict(value))


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidArgumentException(f"{label} debe ser una cadena no vacía")
    return value


def _has_control(value: str) -> bool:
    return any(ord(char) < _FIRST_PRINTABLE_ASCII or ord(char) == _ASCII_DELETE for char in value)


def validate_name(name: object, label: str) -> str:
    """1-64 caracteres `[a-z0-9_-]`, empezando por letra o dígito."""
    if not isinstance(name, str) or not _NAME_PATTERN.fullmatch(name):
        raise InvalidArgumentException(
            f"nombre inválido en {label}: 1-{MAX_NAME_LEN} caracteres [a-z0-9_-]"
        )
    return name


def validate_model_id(model_id: object, label: str) -> str:
    """Un id de modelo del proveedor (`us.anthropic.claude-haiku-4-5-...:0`,
    `claude-sonnet-4-5`, `gpt-4o-mini`): sin espacios ni controles."""
    if not isinstance(model_id, str) or not _MODEL_ID_PATTERN.fullmatch(model_id):
        raise InvalidArgumentException(
            f"{label} debe ser un id de modelo de 1-{MAX_MODEL_ID_LEN} caracteres "
            "[A-Za-z0-9._:/@+-]"
        )
    return model_id


def validate_base_path(base_path: object, label: str) -> str:
    """`""` o una ruta absoluta sin `/` final (`"/v1"`, `"/openai/v1"`) que
    la pasarela pueda reenviar tal cual (`is_safe_request_path`)."""
    if not isinstance(base_path, str):
        raise InvalidArgumentException(f"{label} debe ser una cadena")
    if base_path == "":
        return base_path
    if base_path.endswith("/") or not is_safe_request_path(base_path):
        raise InvalidArgumentException(
            f"{label} debe ser '' o una ruta absoluta sin '/' final ('/v1')"
        )
    return base_path


def validate_region(region: object, label: str) -> str:
    if not isinstance(region, str) or not _REGION_PATTERN.fullmatch(region):
        raise InvalidArgumentException(f"{label} debe ser una región de AWS ('us-east-1')")
    return region


@dataclass(frozen=True)
class AgentModel:
    """El modelo que usa el agente y la pasarela por la que llega a él.

    `gateway` nombra una entrada de `sbx.gateways` (un `SecretGateway` ya
    aplicado, por ejemplo con `bedrock_gateway(...)`); si el sandbox no la
    tiene, `sbx.agent.run()` falla con `InvalidArgumentException` antes de
    cualquier RPC. `region` es obligatoria con `provider="bedrock"`.
    `base_path` sólo se usa con `"openai-compatible"` (`"/v1"`) y es el
    del preset: `"/api/v1"` con `openrouter_gateway`, `"/openai/v1"` con
    `groq_gateway`, `"/v1"` con `mistral_gateway` y `litellm_gateway`, `""`
    con `deepseek_gateway`. `"openai"` va con `openai_gateway` o
    `xai_gateway`, `"google"` con `gemini_gateway` y `"azure"` con
    `azure_openai_gateway`; esos tres fijan su ruta en el adaptador.
    `prompt_caching=True` deja que el runtime marque puntos de caché
    (Bedrock `cachePoint`, Anthropic `cache_control`): con dos o más pasos
    abarata la entrada repetida."""

    provider: ModelProvider
    id: str
    gateway: str
    region: str | None = None
    base_path: str = ""
    prompt_caching: bool = True

    def __post_init__(self) -> None:
        if self.provider not in MODEL_PROVIDERS:
            raise InvalidArgumentException(
                f"AgentModel.provider debe ser uno de {list(MODEL_PROVIDERS)}"
            )
        validate_model_id(self.id, "AgentModel.id")
        validate_route_name(self.gateway)
        if self.provider == "bedrock" and self.region is None:
            raise InvalidArgumentException("AgentModel.region es obligatoria con 'bedrock'")
        if self.region is not None:
            validate_region(self.region, "AgentModel.region")
        validate_base_path(self.base_path, "AgentModel.base_path")
        if self.base_path and self.provider != "openai-compatible":
            raise InvalidArgumentException(
                "AgentModel.base_path sólo se admite con 'openai-compatible'"
            )
        if not isinstance(self.prompt_caching, bool):
            raise InvalidArgumentException("AgentModel.prompt_caching debe ser un bool")


def _validate_action(action: object, label: str) -> PermissionAction:
    if action == ASK_ACTION:
        raise InvalidArgumentException(
            f"{label}: 'ask' no se admite, la ejecución no tiene a nadie que conteste; "
            "usa 'allow' o 'deny'"
        )
    if action == "allow":
        return "allow"
    if action == "deny":
        return "deny"
    raise InvalidArgumentException(f"{label} debe ser 'allow' o 'deny'")


def _validate_tool_permission(tool: str, rule: object) -> ToolPermission:
    label = f"AgentPermissions.tools[{tool!r}]"
    if not isinstance(rule, Mapping):
        return _validate_action(rule, label)
    if not rule:
        raise InvalidArgumentException(f"{label} no puede ser un dict vacío")
    patterns: dict[str, PermissionAction] = {}
    for pattern, action in rule.items():
        if (
            not isinstance(pattern, str)
            or not pattern
            or len(pattern) > MAX_TOOL_PATTERN_LEN
            or _has_control(pattern)
        ):
            raise InvalidArgumentException(
                f"{label}: cada patrón debe ser una cadena de 1-{MAX_TOOL_PATTERN_LEN} "
                "caracteres sin controles"
            )
        patterns[pattern] = _validate_action(action, label)
    return MappingProxyType(patterns)


@dataclass(frozen=True)
class AgentPermissions:
    """Qué herramientas puede usar el agente. `default` aplica a toda
    herramienta sin entrada propia; `tools` asigna `"allow"`/`"deny"` a una
    herramienta (`"edit": "deny"`) o a patrones de su argumento
    (`"bash": {"git *": "allow", "*": "deny"}`). `"ask"` no existe: la
    ejecución es desatendida.

    No es una frontera de seguridad (SECURITY.md T29): la frontera es la
    MicroVM con el egress cerrado. `effective_tools()` añade
    `DEFAULT_DENIED_TOOLS` por debajo de las entradas del llamante."""

    default: PermissionAction = "allow"
    tools: Mapping[str, ToolPermission] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_action(self.default, "AgentPermissions.default")
        tools = _frozen_mapping(self.tools, "AgentPermissions.tools")
        validated: dict[str, ToolPermission] = {}
        for tool, rule in tools.items():
            if not isinstance(tool, str) or not _TOOL_NAME_PATTERN.fullmatch(tool):
                raise InvalidArgumentException(
                    f"nombre de herramienta inválido en AgentPermissions.tools: 1-{MAX_NAME_LEN} "
                    "caracteres [a-z0-9_*-]"
                )
            validated[tool] = _validate_tool_permission(tool, rule)
        object.__setattr__(self, "tools", MappingProxyType(validated))

    def effective_tools(self) -> Mapping[str, ToolPermission]:
        """`DEFAULT_DENIED_TOOLS` en `"deny"` y encima las entradas de
        `tools` (una entrada del llamante gana)."""
        merged: dict[str, ToolPermission] = dict.fromkeys(DEFAULT_DENIED_TOOLS, "deny")
        merged.update(self.tools)
        return MappingProxyType(merged)


@dataclass(frozen=True)
class SubAgent:
    """Un subagente que el agente principal puede invocar (OpenCode
    `mode: subagent`, deepagents `subagents=`). `model` es un id del mismo
    proveedor y pasarela que `AgentSpec.model`; `None` hereda el suyo."""

    description: str
    instructions: str
    model: str | None = None
    permissions: AgentPermissions | None = None

    def __post_init__(self) -> None:
        _require_text(self.description, "SubAgent.description")
        _require_text(self.instructions, "SubAgent.instructions")
        if self.model is not None:
            validate_model_id(self.model, "SubAgent.model")
        if self.permissions is not None and not isinstance(self.permissions, AgentPermissions):
            raise InvalidArgumentException("SubAgent.permissions debe ser un AgentPermissions")


@dataclass(frozen=True)
class McpLocal:
    """Un servidor MCP que el runtime arranca dentro del sandbox como
    proceso hijo (`command` en forma de lista, sin shell). `envs` se escribe
    en la configuración del runtime, legible por uid 1000: nunca pongas ahí
    una credencial, usa una pasarela."""

    command: Sequence[str]
    envs: Mapping[str, str] = field(default_factory=dict, repr=False)
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.command, str)
            or not isinstance(self.command, Sequence)
            or not self.command
        ):
            raise InvalidArgumentException("McpLocal.command debe ser una lista no vacía")
        for part in self.command:
            if not isinstance(part, str) or not part or "\x00" in part:
                raise InvalidArgumentException(
                    "McpLocal.command sólo admite cadenas no vacías sin NUL"
                )
        object.__setattr__(self, "command", tuple(self.command))
        envs = _frozen_mapping(self.envs, "McpLocal.envs")
        for name, value in envs.items():
            if not isinstance(name, str) or not _ENV_NAME_PATTERN.fullmatch(name):
                raise InvalidArgumentException("nombre de variable inválido en McpLocal.envs")
            if not isinstance(value, str) or "\x00" in value:
                raise InvalidArgumentException("McpLocal.envs sólo admite cadenas sin NUL")
        object.__setattr__(self, "envs", envs)
        if self.timeout_seconds is not None and (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int | float)
            or self.timeout_seconds <= 0
        ):
            raise InvalidArgumentException("McpLocal.timeout_seconds debe ser > 0")


@dataclass(frozen=True)
class McpRemote:
    """Un servidor MCP remoto alcanzado a través de la pasarela `gateway`
    (la URL sale de `sbx.gateways[gateway].url` + `path`). No hay
    `headers` a propósito: la credencial sólo la pone la pasarela."""

    gateway: str
    path: str = "/"

    def __post_init__(self) -> None:
        validate_route_name(self.gateway)
        if not isinstance(self.path, str) or not is_safe_request_path(self.path):
            raise InvalidArgumentException(
                "McpRemote.path debe ser una ruta absoluta que la pasarela pueda reenviar"
            )


McpServer: TypeAlias = McpLocal | McpRemote


@dataclass(frozen=True)
class AgentSpec:
    """La configuración estática de un agente: modelo, instrucciones,
    servidores MCP, permisos y subagentes. Construirlo no llama a nada.

    `small_model` (títulos, resúmenes) es por defecto `model.id`, así que el
    runtime nunca elige por su cuenta un modelo que la cuenta quizá no tenga.
    `instructions` se escribe en un `AGENTS.md` propio bajo
    `AGENT_STATE_DIR`, nunca sobre el del directorio de trabajo (que OpenCode
    también lee). `raw_config` se fusiona en profundidad al final y no puede
    tocar `RESERVED_CONFIG_KEYS`. `runtime_version` sólo se valida como
    texto: hoy no se compara con el manifiesto de la plantilla y nunca
    produce `runtime_version_mismatch`."""

    model: AgentModel
    small_model: str | None = None
    instructions: str | None = None
    mcp: Mapping[str, McpServer] = field(default_factory=dict)
    permissions: AgentPermissions = field(default_factory=AgentPermissions)
    agents: Mapping[str, SubAgent] = field(default_factory=dict)
    raw_config: Mapping[str, object] | None = None
    runtime_version: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.model, AgentModel):
            raise InvalidArgumentException("AgentSpec.model debe ser un AgentModel")
        if self.small_model is not None:
            validate_model_id(self.small_model, "AgentSpec.small_model")
        if self.instructions is not None:
            _require_text(self.instructions, "AgentSpec.instructions")
        if not isinstance(self.permissions, AgentPermissions):
            raise InvalidArgumentException("AgentSpec.permissions debe ser un AgentPermissions")
        object.__setattr__(self, "mcp", _validate_mcp(self.mcp))
        object.__setattr__(self, "agents", _validate_agents(self.agents))
        if self.raw_config is not None:
            object.__setattr__(self, "raw_config", validate_raw_config(self.raw_config))
        if self.runtime_version is not None:
            _require_text(self.runtime_version, "AgentSpec.runtime_version")

    @property
    def effective_small_model(self) -> str:
        return self.small_model if self.small_model is not None else self.model.id

    def gateway_names(self) -> frozenset[str]:
        """Las pasarelas que el agente necesita: la del modelo y la de cada
        `McpRemote`."""
        remote = {server.gateway for server in self.mcp.values() if isinstance(server, McpRemote)}
        return frozenset({self.model.gateway, *remote})

    def require_gateways(self, available: Collection[str]) -> None:
        """Falla con `InvalidArgumentException` si alguna pasarela de
        `gateway_names()` no está en `available` (los nombres de
        `sbx.gateways`). Un nombre de pasarela nunca es secreto."""
        missing = sorted(self.gateway_names() - set(available))
        if missing:
            raise InvalidArgumentException(
                f"el sandbox no tiene la pasarela {missing[0]!r}: créalo con "
                f"gateways={{{missing[0]!r}: SecretGateway(...)}}"
            )


def _validate_mcp(mcp: object) -> Mapping[str, McpServer]:
    servers = _frozen_mapping(mcp, "AgentSpec.mcp")
    validated: dict[str, McpServer] = {}
    for name, server in servers.items():
        validate_name(name, "AgentSpec.mcp")
        if not isinstance(server, McpLocal | McpRemote):
            raise InvalidArgumentException(
                "cada valor de AgentSpec.mcp debe ser un McpLocal o un McpRemote"
            )
        validated[name] = server
    return MappingProxyType(validated)


def _validate_agents(agents: object) -> Mapping[str, SubAgent]:
    entries = _frozen_mapping(agents, "AgentSpec.agents")
    validated: dict[str, SubAgent] = {}
    for name, agent in entries.items():
        validate_name(name, "AgentSpec.agents")
        if name in RESERVED_AGENT_NAMES:
            raise InvalidArgumentException(
                f"AgentSpec.agents no puede usar el nombre reservado {name!r}"
            )
        if not isinstance(agent, SubAgent):
            raise InvalidArgumentException("cada valor de AgentSpec.agents debe ser un SubAgent")
        validated[name] = agent
    return MappingProxyType(validated)


def validate_raw_config(raw_config: object) -> Mapping[str, object]:
    """Un dict cuyas claves de primer nivel no están en
    `RESERVED_CONFIG_KEYS`; el error nombra la clave."""
    config = _frozen_mapping(raw_config, "AgentSpec.raw_config")
    for key in config:
        if not isinstance(key, str):
            raise InvalidArgumentException("las claves de AgentSpec.raw_config deben ser cadenas")
        if key in RESERVED_CONFIG_KEYS:
            raise InvalidArgumentException(
                f"AgentSpec.raw_config no puede fijar {key!r}: la escribe Rayito"
            )
    return config


def _positive_int(value: object, label: str, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InvalidArgumentException(f"{label} debe ser un entero >= 1")
    if maximum is not None and value > maximum:
        raise InvalidArgumentException(f"{label} no puede pasar de {maximum}")
    return value


@dataclass(frozen=True)
class AgentLimits:
    """Topes duros de una ejecución, impuestos por el SDK (no por el
    runtime): `max_steps` aborta con `max_steps` al empezar el paso
    `max_steps + 1`; `max_total_tokens` se comprueba tras cada paso, así que
    puede pasarse en un paso (`None` lo desactiva); `timeout_seconds` y
    `max_output_bytes` son los del comando que corre el runtime. Los valores
    por defecto acotan el coste de una ejecución (design.md de
    `ai-agent-core`, D4)."""

    max_steps: int = DEFAULT_AGENT_MAX_STEPS
    timeout_seconds: float = DEFAULT_AGENT_TIMEOUT_SECONDS
    max_output_bytes: int = DEFAULT_AGENT_MAX_OUTPUT_BYTES
    max_total_tokens: int | None = DEFAULT_AGENT_MAX_TOTAL_TOKENS

    def __post_init__(self) -> None:
        _positive_int(self.max_steps, "AgentLimits.max_steps")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int | float)
            or self.timeout_seconds <= 0
        ):
            raise InvalidArgumentException("AgentLimits.timeout_seconds debe ser > 0")
        _positive_int(
            self.max_output_bytes, "AgentLimits.max_output_bytes", COMMAND_OUTPUT_MAX_BYTES
        )
        if self.max_total_tokens is not None:
            _positive_int(self.max_total_tokens, "AgentLimits.max_total_tokens")
