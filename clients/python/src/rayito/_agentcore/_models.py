"""Los valores de `--target agentcore` (ADR-031): las opciones públicas
(`AgentCoreOptions`), el plan que se calcula sin AWS, el estado y el borrado de
un agente, y lo que el servicio observa en la cuenta (el runtime, la imagen y
el build). Sólo datos y validación de tipos: ninguna E/S ni cliente de AWS.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

from rayito.exceptions import InvalidArgumentException

ResourceName = Literal["stack", "image", "runtime"]
StepAction = Literal["planned", "create", "update", "reuse", "wait", "build"]


@dataclass(frozen=True)
class AgentCoreOptions:
    """Las opciones de un despliegue en AgentCore (`deploy_agent(target=
    "agentcore", agentcore=...)`): `bedrock_models` (ids o ARNs de modelos o
    perfiles de inferencia que el rol del runtime puede invocar, además del
    modelo del proyecto), `readable_secrets` (nombres o ARNs de secretos de
    Secrets Manager que el rol puede leer), `idle_session_timeout_seconds` y
    `max_session_lifetime_seconds` (60-28800; `None`, 900 y 28800) y `tags`
    (etiquetas de la pila y del runtime, para organizaciones que las exigen).
    Sólo valida los tipos: los rangos se comprueban al planificar, antes de
    llamar a AWS.

    Coste y activación
    -------------------
    Activa: nada por sí mismo; el valor es puro. Lo que cuesta es
        `deploy_agent(target="agentcore")`.
    Recursos y llamadas AWS: ninguno aquí; ver `deploy_agent()`.
    Coste aproximado: ninguno aquí; ver `deploy_agent()`.
    IAM: ninguna para este valor.
    Cómo apagarla: los valores por defecto no dan al rol del runtime ningún
        permiso de Bedrock extra ni de Secrets Manager.
    Ejemplo:
        AgentCoreOptions(bedrock_models=("amazon.nova-lite-v1:0",), tags={"team": "agents"})
    """

    bedrock_models: tuple[str, ...] = ()
    readable_secrets: tuple[str, ...] = ()
    idle_session_timeout_seconds: int | None = None
    max_session_lifetime_seconds: int | None = None
    tags: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "bedrock_models", _texts(self.bedrock_models, "AgentCoreOptions.bedrock_models")
        )
        object.__setattr__(
            self,
            "readable_secrets",
            _texts(self.readable_secrets, "AgentCoreOptions.readable_secrets"),
        )
        for label in ("idle_session_timeout_seconds", "max_session_lifetime_seconds"):
            value = getattr(self, label)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise InvalidArgumentException(
                    f"AgentCoreOptions.{label} debe ser un entero o None"
                )
        if not isinstance(self.tags, Mapping) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in self.tags.items()
        ):
            raise InvalidArgumentException("AgentCoreOptions.tags debe mapear cadenas a cadenas")
        object.__setattr__(self, "tags", MappingProxyType(dict(self.tags)))


def _texts(values: object, label: str) -> tuple[str, ...]:
    if isinstance(values, str) or not isinstance(values, tuple | list):
        raise InvalidArgumentException(f"{label} debe ser una lista de textos")
    if not all(isinstance(item, str) and item for item in values):
        raise InvalidArgumentException(f"{label} debe ser una lista de textos no vacíos")
    return tuple(values)


@dataclass(frozen=True)
class AgentCoreLifecycle:
    """Los plazos de una sesión de AgentCore, en segundos."""

    idle_session_timeout_seconds: int
    max_session_lifetime_seconds: int


@dataclass(frozen=True)
class AgentCoreStep:
    """Un paso del plan (`planned`) o del despliegue (`create`, `update`,
    `reuse`, `wait`, `build`) sobre un recurso: la pila, la imagen o el
    runtime. `status` es el estado que AWS informó de lo que ya existía."""

    resource: ResourceName
    name: str
    action: StepAction
    status: str | None = None


@dataclass(frozen=True)
class AgentCoreDeployPlan:
    """Lo que `deploy_agent(target="agentcore")` haría, calculado sin llamar a
    AWS: los nombres derivados, el bucket (o `None` si se descubre al
    aplicar) y de dónde sale, los modelos y secretos del rol, el entorno y el
    ciclo de vida del runtime, las etiquetas, los tres pasos, y las claves de
    las líneas de seguridad y de los avisos (`_messages.SECURITY_TEXTS` y
    `WARNING_TEXTS`) y el coste de lista. `secret_env_names` son las
    variables que el host pondrá desde Secrets Manager (ADR-033 D4): sólo
    nombres, nunca valores."""

    name: str
    region: str
    runtime_name: str
    stack_name: str
    repository_name: str
    build_project_name: str
    image_tag: str
    bucket: str | None
    bucket_source: str
    network: str
    bedrock_models: tuple[str, ...]
    readable_secrets: tuple[str, ...]
    environment: Mapping[str, str]
    lifecycle: AgentCoreLifecycle
    tags: Mapping[str, str]
    steps: tuple[AgentCoreStep, ...]
    security: tuple[str, ...]
    warnings: tuple[str, ...]
    cost: tuple[str, ...]
    detection: object | None = None
    target: str = "agentcore"
    secret_env_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentCoreStatus:
    """El estado de un agente desplegado en AgentCore (`GetAgentRuntime`)."""

    name: str
    region: str
    runtime_name: str
    runtime_id: str
    runtime_arn: str
    runtime_version: str
    status: str
    image_uri: str
    log_group: str


@dataclass(frozen=True)
class AgentCoreDeleteResult:
    """Lo que `delete()` borró: el runtime, la pila (con el repositorio, el
    proyecto de CodeBuild y los roles) y el grupo de logs del runtime."""

    name: str
    region: str
    runtime_deleted: bool
    stack_deleted: bool
    log_group_deleted: bool


@dataclass(frozen=True)
class AgentCoreListItem:
    """Un agente de Rayito en AgentCore según `ListAgentRuntimes`."""

    name: str
    runtime_name: str
    runtime_id: str
    runtime_version: str
    status: str


@dataclass(frozen=True)
class RuntimeListing:
    """Un elemento de `ListAgentRuntimes`."""

    runtime_id: str
    arn: str
    name: str
    version: str
    status: str
    description: str


@dataclass(frozen=True)
class RuntimeDetails:
    """Lo que `GetAgentRuntime` dice de la configuración de un runtime y que
    Rayito compara (`contract.constants.runtimeComparedFields`)."""

    container_uri: str
    role_arn: str
    network_mode: str
    server_protocol: str
    environment_variables: Mapping[str, str]
    idle_runtime_session_timeout: int
    max_lifetime: int
    require_mmds_v2: bool


@dataclass(frozen=True)
class RuntimeSpec:
    """La configuración que Rayito quiere en un runtime: lo que se manda a
    `CreateAgentRuntime` y `UpdateAgentRuntime`."""

    name: str
    description: str
    container_uri: str
    role_arn: str
    environment_variables: Mapping[str, str]
    idle_runtime_session_timeout: int
    max_lifetime: int
    tags: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeRef:
    """La respuesta de crear o actualizar un runtime."""

    runtime_id: str
    arn: str
    version: str
    status: str


@dataclass(frozen=True)
class ImageObservation:
    """`DescribeImages` de la etiqueta que se busca: si existe y lo que ocupa
    en ECR."""

    exists: bool
    size_bytes: int = 0


@dataclass(frozen=True)
class BuildObservation:
    """`BatchGetBuilds` de un build: su estado, el grupo de logs y la fase en
    curso."""

    build_id: str
    status: str
    log_group: str
    phase: str = ""


@dataclass(frozen=True)
class AgentCoreApplyRequest:
    """Lo que `AgentCoreDeployer.apply()` necesita además del plan: el árbol
    de ficheros del contexto de build (`render_export`), la región del modelo
    del proyecto (para los ARNs del rol), si se construye la imagen
    (`None`: si falta), el plazo del build y los callbacks del llamante:
    `on_progress` recibe la clave de `_messages.PROGRESS_MESSAGES` y la línea
    ya formateada, y `on_build_logs`, la fase del build que informa CodeBuild."""

    tree: Mapping[str, bytes]
    model_region: str
    build_image: bool | None
    build_timeout_seconds: float
    on_progress: Callable[[str, str], None] | None = None
    on_build_logs: Callable[[str], None] | None = None


@dataclass(frozen=True)
class AgentCoreDeployment:
    """El resultado de aplicar un despliegue: el runtime listo (identificador,
    ARN, versión y estado), la imagen que corre, la pila, el grupo de logs y lo
    que se hizo con cada recurso."""

    name: str
    region: str
    runtime_name: str
    runtime_id: str
    runtime_arn: str
    runtime_version: str
    status: str
    image_uri: str
    stack_name: str
    log_group: str
    steps: tuple[AgentCoreStep, ...]


@dataclass(frozen=True)
class AgentCoreDeletePlan:
    """Lo que `delete` borraría de un agente: el runtime (si existe), la pila
    (si existe) y el grupo de logs del runtime."""

    name: str
    region: str
    stack_name: str
    runtime: RuntimeListing | None
    stack_exists: bool
    log_group: str | None

    @property
    def nothing(self) -> bool:
        return self.runtime is None and not self.stack_exists
