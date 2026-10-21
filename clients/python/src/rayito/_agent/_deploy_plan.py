"""Las opciones y el plan de `deploy_agent()` (ADR-029, D7 y D8):
`AgentDeployOptions` (dónde y cómo se despliega, no qué agente es), `AgentDeployPlan` y
la estimación de coste que `plan_deploy()` y `rayito agent deploy --dry-run`
muestran sin llamar a AWS. Todo es puro salvo `project_for()` (lee los ficheros
del proyecto) y `resolved_region()` (lee la configuración local de boto3): ninguno
llama a AWS.
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final

from rayito._agent._aws_resolve import BUCKET_ENV_VAR
from rayito._agent._deploy_domain import (
    BASE_IMAGE_VERSION_LATEST,
    validate_bucket_hint,
    validate_image_version,
    validate_secret_name,
)
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
    AGENT_SOURCE_DEFAULT_EXCLUDES,
    SNAPSHOT_STORAGE_PRICE_USD_PER_GB_MONTH,
    SNAPSHOT_WRITE_PRICE_USD_PER_GB,
)
from rayito._payload import validate_access_token
from rayito._sandbox_base import ACCESS_TOKEN_ENV_VAR
from rayito.exceptions import InvalidArgumentException

if TYPE_CHECKING:
    from rayito._agent._detect_report import DetectionReport, DetectOverrides
    from rayito._index import DynamoDbIndex
    from rayito._pool_backends import PoolBackend

SECONDS_PER_HOUR: Final = 3600
#: El prefijo que lleva un patrón de `.dockerignore` a cualquier profundidad.
NESTED_EXCLUDE_PREFIX: Final = "**/"
#: Lo que `.dockerignore` lee como comodín, negación o comentario: se escapa
#: con una barra invertida para excluir un fichero por su nombre literal.
GLOB_SPECIALS: Final = frozenset("*?[]\\!#")
DOCKERIGNORE_ESCAPE: Final = "\\"
#: Días del mes de la lista de precios (`AWS_API_NOTES.md`, sección de coste:
#: el almacenamiento se cotiza por GB-mes de 720 h).
PRICING_DAYS_PER_MONTH: Final = 30
DAYS_PER_WEEK: Final = 7
#: Tamaño del snapshot de una imagen de agente (≈ 3,0 GB: el aviso de coste de
#: `rayito agent template build`, medido en AWS real).
AGENT_IMAGE_SNAPSHOT_GB_ESTIMATE: Final = 3.0
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
    secreto. Con `target="agentcore"`, `kind` es la clave de la línea de
    progreso (`stackDeploy`, `buildStart`, `runtimeReady`...) y `detail` la
    línea ya escrita."""

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
    aparcadas), `create_secrets_from` (un `.env` cuyos valores van a un secreto JSON
    `rayito/agents/<nombre>/env` que el agente recibe como `secret_env_from`) y `check_env`
    (avisa de las variables de `.env.example` que no reciben valor).

    Coste y activación
    -------------------
    Activa: nada por sí mismo; el valor es puro. `deploy_agent()` es lo que
        cuesta: `create_secret_from_env=True` crea un secreto,
        `create_secrets_from` crea o actualiza el secreto de un `.env`
        (`CreateSecret` o `PutSecretValue`, $0,40 al mes por secreto),
        `base_publisher` una imagen base, `session_index` filas de DynamoDB.
    Recursos y llamadas AWS: ninguno aquí.
    Coste aproximado: ninguno aquí; ver `deploy_agent()`.
    IAM: ninguna para este valor.
    Cómo apagarla: los valores por defecto no crean ningún secreto, imagen
        base ni índice; `check_env` sólo lee ficheros locales.
    Ejemplo:
        AgentDeployOptions(bucket="amzn-s3-demo-bucket", secret_names={"bedrock": "mi-clave"})
    """

    bucket: str | None = None
    base_image_version: str | None = None
    secret_names: Mapping[str, str] = field(default_factory=dict)
    create_secret_from_env: bool = False
    ensure_base_image: bool = True
    base_publisher: Callable[[], None] | None = None
    access_token: str | None = field(default=None, repr=False)
    keep_on_failure: bool = False
    build_image: bool | None = None
    scaling: ScalingOptions | None = None
    session_index: DynamoDbIndex | None = None
    pool_backend: PoolBackend | None = None
    on_build_logs: Callable[[str], None] | None = None
    on_step: Callable[[AgentDeployStep], None] | None = None
    build_timeout_seconds: float = DEFAULT_BUILD_TIMEOUT_SECONDS
    warm_timeout_seconds: float = AGENT_FLEET_WARM_READY_TIMEOUT_SECONDS
    create_secrets_from: str | None = None
    check_env: bool = False

    def __post_init__(self) -> None:
        if self.bucket is not None:
            validate_bucket_hint(self.bucket, "AgentDeployOptions.bucket")
        if self.base_image_version is not None:
            validate_image_version(self.base_image_version, "AgentDeployOptions.base_image_version")
        if self.access_token is not None:
            if not isinstance(self.access_token, str) or not self.access_token:
                raise InvalidArgumentException(
                    "AgentDeployOptions.access_token debe ser una cadena no vacía o None"
                )
            validate_access_token(self.access_token)
        if not isinstance(self.secret_names, Mapping) or not all(
            isinstance(key, str) and key for key in self.secret_names
        ):
            raise InvalidArgumentException("AgentDeployOptions.secret_names debe mapear cadenas")
        for value in self.secret_names.values():
            validate_secret_name(value, "AgentDeployOptions.secret_names")
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
        self._validate_secret_env_options()

    def _validate_secret_env_options(self) -> None:
        path = self.create_secrets_from
        if path is not None and (not isinstance(path, str) or not path):
            raise InvalidArgumentException(
                "AgentDeployOptions.create_secrets_from debe ser una ruta o None"
            )
        if not isinstance(self.check_env, bool):
            raise InvalidArgumentException("AgentDeployOptions.check_env debe ser un bool")


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


def source_excludes(exclude: Sequence[str]) -> tuple[str, ...]:
    """`AGENT_SOURCE_DEFAULT_EXCLUDES` en la raíz y a cualquier profundidad
    (`**/.env`, `**/.git`) delante de `exclude`, sin repetir ninguna:
    `.dockerignore` ancla un patrón simple en la raíz, y la imagen se
    construye con todo lo que haya en el contexto, así que un `sub/.env` no
    debe colarse ni en MicroVMs ni en AgentCore."""
    nested = [f"{NESTED_EXCLUDE_PREFIX}{pattern}" for pattern in AGENT_SOURCE_DEFAULT_EXCLUDES]
    return tuple(dict.fromkeys([*AGENT_SOURCE_DEFAULT_EXCLUDES, *nested, *exclude]))


def with_default_excludes(project: AgentProject) -> AgentProject:
    """El proyecto con `source_excludes` sobre sus propias exclusiones."""
    return dataclasses.replace(project, exclude=source_excludes(project.exclude))


def excluded_file_pattern(
    source: str | os.PathLike[str], path: str | os.PathLike[str]
) -> str | None:
    """El patrón de `.dockerignore` que excluye exactamente `path` del código
    de `source` (anclado en la raíz y con los comodines escapados); `None` si
    `path` queda fuera de `source`. Las dos rutas se resuelven (enlaces
    simbólicos incluidos) antes de compararlas."""
    root = Path(source).resolve()
    target = Path(path).resolve()
    if not target.is_relative_to(root) or target == root:
        return None
    relative = target.relative_to(root).as_posix()
    return "".join(
        f"{DOCKERIGNORE_ESCAPE}{char}" if char in GLOB_SPECIALS else char for char in relative
    )


def with_excluded_file(project: AgentProject, path: str | os.PathLike[str] | None) -> AgentProject:
    """El proyecto que además excluye `path` de su código si está dentro (el
    fichero del access token que escribe la CLI: nunca debe acabar en la
    imagen ni cambiar su hash). Va el último, así que ninguna negación de
    `exclude` lo vuelve a meter; fuera del código, o sin código, no cambia
    nada."""
    if path is None or project.source is None:
        return project
    pattern = excluded_file_pattern(project.source, path)
    if pattern is None:
        return project
    kept = [entry for entry in project.exclude if entry != pattern]
    return dataclasses.replace(project, exclude=(*kept, pattern))


def project_bucket(project: AgentProject) -> str | None:
    """`[deploy].bucket` del fichero de proyecto (`AgentProject.bucket`); un
    proyecto sin ese campo no fija bucket."""
    return project.bucket


def base_version_of(options: AgentDeployOptions, project: AgentProject) -> str | None:
    """La versión de la imagen base: el argumento, si no `[deploy]
    base_image_version` del proyecto y, si ninguno la fija o pide `"latest"`,
    `None` (la más reciente)."""
    version = options.base_image_version or project.base_image_version
    return None if version in (None, BASE_IMAGE_VERSION_LATEST) else version


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
    region: str | None = None,
) -> tuple[AgentProject, DetectionReport | None]:
    """El proyecto a desplegar y, si salió de la detección, su informe. Un
    `project` explícito gana a todo; si no, `AgentProject.detect()` (que lee
    el fichero de proyecto y completa lo que falte sin ejecutar código del
    usuario). Es el único sitio donde el despliegue conoce la detección."""
    if project is not None:
        return project, None
    return AgentProject.detect(path, overrides, region=region)


def resolved_region(
    region: str | None, project: AgentProject, session: Any, environ: Mapping[str, str]
) -> str | None:
    """La región de la llamada: argumento > `region` del proyecto > la cadena
    de `rayito._aws_region` (sesión, `AWS_REGION`)."""
    return (
        region or getattr(project, "region", None) or resolve_region(None, session, environ=environ)
    )
