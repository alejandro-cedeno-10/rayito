"""Las opciones y el plan de `deploy_agent()` (ADR-029, D7 y D8), sin E/S:
`AgentDeployOptions` (dónde y cómo se despliega, no qué agente es), `AgentDeployPlan` y
la estimación de coste que `plan_deploy()` y `rayito agent deploy --dry-run`
muestran sin llamar a AWS.
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final

from rayito._agent._aws_resolve import BUCKET_ENV_VAR
from rayito._agent._fleet_base import (
    AGENT_FLEET_WARM_READY_TIMEOUT_SECONDS,
    fleet_settings_of,
)
from rayito._agent._project_domain import AgentProject
from rayito._agent._scaling_domain import ScalingOptions
from rayito._aws_region import resolve_region
from rayito._dashboard._domain import MIB_PER_GB, estimate_compute_usd
from rayito._images import DEFAULT_BUILD_TIMEOUT_SECONDS
from rayito._limits import (
    SNAPSHOT_STORAGE_PRICE_USD_PER_GB_MONTH,
    SNAPSHOT_WRITE_PRICE_USD_PER_GB,
)
from rayito._payload import validate_access_token
from rayito._sandbox_base import ACCESS_TOKEN_ENV_VAR
from rayito.exceptions import InvalidArgumentException

if TYPE_CHECKING:
    from rayito._agent._detect_domain import DetectOverrides
    from rayito._index import DynamoDbIndex
    from rayito._pool_backends import PoolBackend

SECONDS_PER_HOUR: Final = 3600
#: Días del mes de la lista de precios (`AWS_API_NOTES.md`, sección de coste:
#: el almacenamiento se cotiza por GB-mes de 720 h).
PRICING_DAYS_PER_MONTH: Final = 30
DAYS_PER_WEEK: Final = 7
#: Tamaño del snapshot de una imagen de agente (≈ 3,0 GB: el aviso de coste de
#: `rayito agent template build`, medido en AWS real).
AGENT_IMAGE_SNAPSHOT_GB_ESTIMATE: Final = 3.0
#: Lo que `deploy_agent()` nunca sube como código del agente (patrones de
#: `.dockerignore`): el control de versiones, los ficheros de entorno y
#: secretos, los entornos y paquetes instalados en el portátil (arquitectura
#: equivocada: las dependencias se instalan en la imagen), las cachés y el
#: directorio `.rayito` donde la CLI guarda el access token de la flota
#: (`testdata/agent-deploy/resolve.json`, la misma lista en TypeScript).
AGENT_SOURCE_DEFAULT_EXCLUDES: Final[tuple[str, ...]] = (
    ".git",
    ".env",
    ".env.*",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "*.pyc",
    ".rayito",
)
PARKED_SLOTS_NOTE: Final = (
    "las plazas calientes se aparcan en el pool: cuestan almacenamiento de snapshot, no cómputo"
)
IMAGE_MEMORY_NOTE: Final = (
    "la memoria se fija en la imagen: cambiarla construye una versión nueva (almacenamiento "
    "de snapshot de una semana como mínimo)"
)


@dataclass(frozen=True)
class AgentDeployStep:
    """Un paso de `deploy_agent()` que ya ocurrió: `kind` es de una lista
    cerrada (`base_image`, `secret_created`, `bucket`, `image`, `fleet`,
    `warm`) y `detail` un dato corto para mostrar (la pasarela, el origen y el
    bucket, la versión de la imagen); nunca el valor ni el nombre de un
    secreto."""

    kind: str
    detail: str


@dataclass(frozen=True)
class AgentDeployOptions:
    """Dónde y cómo se despliega (los argumentos de `deploy_agent()` que no
    describen al agente): `bucket` del artefacto de la imagen,
    `base_image_version` (`None` = la más reciente de la base), `secret_names`
    (pasarela → nombre de un secreto que ya existe), `create_secret_from_env`
    (crea el que falte desde la variable estándar del proveedor),
    `ensure_base_image` y `base_publisher` (quién publica la base si falta),
    `access_token`, `keep_on_failure`, `build_image` (`None` = construir si hay
    bucket; `True` = siempre; `False` = reutilizar la imagen existente),
    `scaling` (gana al del proyecto), `session_index` (índice de sesiones de
    los VMs dedicados), `pool_backend` (plazas calientes que sobreviven al
    proceso), `on_build_logs`, `on_step` (recibe cada `AgentDeployStep`),
    `build_timeout_seconds` y `warm_timeout_seconds` (espera a que las plazas calientes queden
    aparcadas).

    Coste y activación
    -------------------
    Activa: nada por sí mismo; el valor es puro. `deploy_agent()` es lo que
        cuesta: `create_secret_from_env=True` crea un secreto,
        `base_publisher` una imagen base, `session_index` filas de DynamoDB.
    Recursos y llamadas AWS: ninguno aquí.
    Coste aproximado: ninguno aquí; ver `deploy_agent()`.
    IAM: ninguna para este valor.
    Cómo apagarla: los valores por defecto no crean ningún secreto, imagen
        base ni índice.
    Ejemplo:
        AgentDeployOptions(bucket="amzn-s3-demo-bucket", secret_names={"bedrock": "mi-clave"})
    """

    bucket: str | None = None
    base_image_version: str | None = None
    secret_names: Mapping[str, str] = field(default_factory=dict)
    create_secret_from_env: bool = False
    ensure_base_image: bool = True
    base_publisher: Callable[[], None] | None = None
    access_token: str | None = None
    keep_on_failure: bool = False
    build_image: bool | None = None
    scaling: ScalingOptions | None = None
    session_index: DynamoDbIndex | None = None
    pool_backend: PoolBackend | None = None
    on_build_logs: Callable[[str], None] | None = None
    on_step: Callable[[AgentDeployStep], None] | None = None
    build_timeout_seconds: float = DEFAULT_BUILD_TIMEOUT_SECONDS
    warm_timeout_seconds: float = AGENT_FLEET_WARM_READY_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        for label in ("bucket", "base_image_version", "access_token"):
            value = getattr(self, label)
            if value is not None and (not isinstance(value, str) or not value):
                raise InvalidArgumentException(
                    f"AgentDeployOptions.{label} debe ser una cadena no vacía o None"
                )
        if self.access_token is not None:
            validate_access_token(self.access_token)
        if not isinstance(self.secret_names, Mapping) or not all(
            isinstance(key, str) and isinstance(value, str) and key and value
            for key, value in self.secret_names.items()
        ):
            raise InvalidArgumentException("AgentDeployOptions.secret_names debe mapear cadenas")
        for label in ("create_secret_from_env", "ensure_base_image", "keep_on_failure"):
            if not isinstance(getattr(self, label), bool):
                raise InvalidArgumentException(f"AgentDeployOptions.{label} debe ser un bool")
        if self.build_image is not None and not isinstance(self.build_image, bool):
            raise InvalidArgumentException("AgentDeployOptions.build_image debe ser un bool o None")
        if self.scaling is not None and not isinstance(self.scaling, ScalingOptions):
            raise InvalidArgumentException("AgentDeployOptions.scaling debe ser un ScalingOptions")
        for label in ("build_timeout_seconds", "warm_timeout_seconds"):
            value = getattr(self, label)
            if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
                raise InvalidArgumentException(f"AgentDeployOptions.{label} debe ser un número > 0")
        object.__setattr__(self, "secret_names", MappingProxyType(dict(self.secret_names)))


@dataclass(frozen=True)
class AgentDeployCostEstimate:
    """Lo que cuesta lo que `deploy_agent()` crea, en dólares, a precios de
    lista (`limits.json`): `running_usd_per_hour` por los VMs `min_vms`
    mientras viven, `parked_usd_per_day` por las plazas aparcadas (sólo
    almacenamiento de snapshot), `image_build_usd` por una versión nueva de la
    imagen y `image_storage_usd_per_week` por conservarla (mínimo una
    semana). No incluye los tokens del modelo."""

    running_usd_per_hour: float
    parked_usd_per_day: float
    image_build_usd: float
    image_storage_usd_per_week: float


@dataclass(frozen=True)
class AgentPlannedSecret:
    """La pasarela y de dónde sale el nombre de su secreto: `override` (el de
    `secret_names`), `project` (el del proyecto). El nombre no se muestra."""

    gateway: str
    source: str


@dataclass(frozen=True)
class AgentDeployPlan:
    """Lo que `deploy_agent()` haría, calculado sin llamar a AWS: el agente,
    el escalado, la región (la del entorno, si la hay), de dónde saldrá el
    bucket, la imagen base, los secretos, el coste y las notas."""

    name: str
    template: str
    base: str
    memory_mib: int
    region: str | None
    scaling: ScalingOptions
    bucket_source: str
    build_image: bool | None
    secrets: tuple[AgentPlannedSecret, ...]
    token_source: str
    cost: AgentDeployCostEstimate
    notes: tuple[str, ...]
    detection: object | None = None


def estimate_cost(memory_mib: int, scaling: ScalingOptions) -> AgentDeployCostEstimate:
    """El coste de lista de `scaling` con VMs de `memory_mib`."""
    gigabytes = memory_mib / MIB_PER_GB
    storage_per_gb_day = SNAPSHOT_STORAGE_PRICE_USD_PER_GB_MONTH / PRICING_DAYS_PER_MONTH
    image_gb = AGENT_IMAGE_SNAPSHOT_GB_ESTIMATE
    return AgentDeployCostEstimate(
        running_usd_per_hour=scaling.min_vms * estimate_compute_usd(memory_mib, SECONDS_PER_HOUR),
        parked_usd_per_day=scaling.warm_vms * gigabytes * storage_per_gb_day,
        image_build_usd=image_gb * SNAPSHOT_WRITE_PRICE_USD_PER_GB,
        image_storage_usd_per_week=image_gb * storage_per_gb_day * DAYS_PER_WEEK,
    )


def bucket_source_of(options: AgentDeployOptions, project_bucket: str | None, environ: Any) -> str:
    """De dónde saldrá el bucket sin preguntar a AWS (el orden de
    `resolve_bucket`)."""
    if options.bucket:
        return "argument"
    if project_bucket:
        return "project_file"
    if environ.get(BUCKET_ENV_VAR):
        return "environment"
    return "discovered"


def token_source_of(options: AgentDeployOptions, environ: Any) -> str:
    if options.access_token:
        return "argument"
    return "environment" if environ.get(ACCESS_TOKEN_ENV_VAR) else "generated"


def with_default_excludes(project: AgentProject) -> AgentProject:
    """El proyecto con `AGENT_SOURCE_DEFAULT_EXCLUDES` delante de sus propias
    exclusiones, sin repetir ninguna."""
    merged = tuple(dict.fromkeys([*AGENT_SOURCE_DEFAULT_EXCLUDES, *project.exclude]))
    return dataclasses.replace(project, exclude=merged)


def project_bucket(project: AgentProject) -> str | None:
    """`[deploy].bucket` del fichero de proyecto (`AgentProject.deploy_bucket`);
    un proyecto sin ese campo no fija bucket."""
    bucket = getattr(project, "deploy_bucket", None)
    return bucket if isinstance(bucket, str) and bucket else None


def planned_secrets(
    project: AgentProject, options: AgentDeployOptions
) -> tuple[AgentPlannedSecret, ...]:
    overridden = set(options.secret_names)
    return tuple(
        AgentPlannedSecret(gateway, "override" if gateway in overridden else "project")
        for gateway in sorted(project.gateways)
    )


def build_plan(
    project: AgentProject,
    options: AgentDeployOptions,
    *,
    region: str | None,
    detection: object | None,
    environ: Mapping[str, str] | None = None,
) -> AgentDeployPlan:
    """El plan de desplegar `project` con `options` (puro)."""
    environment = os.environ if environ is None else environ
    scaling = fleet_settings_of(project, options.scaling).scaling
    notes = [IMAGE_MEMORY_NOTE] if options.build_image is not False else []
    if scaling.warm_vms:
        notes.append(
            "las plazas calientes se aparcan en el pool: cuestan almacenamiento, no cómputo"
        )
    return AgentDeployPlan(
        name=project.name,
        template=project.template or project.name,
        base=project.base,
        memory_mib=project.memory_mib,
        region=region,
        scaling=scaling,
        bucket_source=bucket_source_of(options, project_bucket(project), environment),
        build_image=options.build_image,
        secrets=planned_secrets(project, options),
        token_source=token_source_of(options, environment),
        cost=estimate_cost(project.memory_mib, scaling),
        notes=tuple(notes),
        detection=detection,
    )


def project_for(
    path: str | os.PathLike[str],
    project: AgentProject | None,
    overrides: DetectOverrides | None,
) -> tuple[AgentProject, object | None]:
    """El proyecto a desplegar y, si salió de la detección, su informe. Un
    `project` explícito gana a todo; si no, `AgentProject.detect()` (que lee
    el fichero de proyecto y completa lo que falte sin ejecutar código del
    usuario). Es el único sitio donde el despliegue conoce la detección."""
    if project is not None:
        return project, None
    detect = getattr(AgentProject, "detect", None)
    if detect is not None:
        detected = detect(path, overrides)
        return detected[0], detected[1]
    return AgentProject.load(path), None


def resolved_region(
    region: str | None, project: AgentProject, session: Any, environ: Mapping[str, str]
) -> str | None:
    """La región de la llamada: argumento > `region` del proyecto > la cadena
    de `rayito._aws_region` (sesión, `AWS_REGION`)."""
    return (
        region or getattr(project, "region", None) or resolve_region(None, session, environ=environ)
    )
