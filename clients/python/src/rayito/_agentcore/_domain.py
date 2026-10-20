"""Dominio puro de `--target agentcore` (ADR-031): los nombres derivados de un
agente, la validación de lo que se puede comprobar sin AWS, el plan, el id de
sesión de AgentCore, la etiqueta de imagen por contenido, el token de
idempotencia, los ARNs del rol del runtime y cada decisión que se toma al
aplicar sobre lo observado en la cuenta (`testdata/agentcore-deploy/`).

Sólo importa la biblioteca estándar, `rayito._limits` y otros módulos puros del
SDK: nada de red, ficheros del usuario ni AWS.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import uuid
import zipfile
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

from rayito._account_setup._domain import BUCKET_ENV_VAR
from rayito._agent._fleet_base import AGENT_FLEET_WARM_READY_TIMEOUT_SECONDS
from rayito._agent._project_base import template_name
from rayito._agent._project_domain import ServeOptions
from rayito._agent._scaling_domain import ScalingOptions
from rayito._agentcore._env_credentials import uses_env_credentials
from rayito._agentcore._messages import COST_TEXTS
from rayito._agentcore._models import (
    AgentCoreDeployPlan,
    AgentCoreLifecycle,
    AgentCoreOptions,
    AgentCoreStep,
    ImageObservation,
    RuntimeDetails,
    RuntimeListing,
    RuntimeSpec,
)
from rayito._limits import (
    AGENT_MIN_MEMORY_MIB,
    AGENTCORE_BASE_IMAGE_MIRROR,
    AGENTCORE_DESCRIPTION_MARKER,
    AGENTCORE_ENDPOINT_NAME,
    AGENTCORE_ENVIRONMENT_VALUE_MAX_CHARS,
    AGENTCORE_IDLE_SESSION_TIMEOUT_DEFAULT_SECONDS,
    AGENTCORE_IMAGE_RETENTION_COUNT,
    AGENTCORE_IMAGE_TAG_HEX_CHARS,
    AGENTCORE_IMAGE_TAG_PREFIX,
    AGENTCORE_LIFECYCLE_MAX_SECONDS,
    AGENTCORE_LIFECYCLE_MIN_SECONDS,
    AGENTCORE_MAX_ENVIRONMENT_VARIABLES,
    AGENTCORE_MAX_IMAGE_BYTES,
    AGENTCORE_MAX_TAGS,
    AGENTCORE_NETWORK_MODE,
    AGENTCORE_REPOSITORY_PREFIX,
    AGENTCORE_RUNTIME_GB_HOUR_PRICE_USD,
    AGENTCORE_RUNTIME_LOG_GROUP_PREFIX,
    AGENTCORE_RUNTIME_NAME_MAX_LENGTH,
    AGENTCORE_RUNTIME_NAME_PREFIX,
    AGENTCORE_RUNTIME_VCPU_HOUR_PRICE_USD,
    AGENTCORE_SERVER_PROTOCOL,
    AGENTCORE_SESSION_ID_MAX_LENGTH,
    AGENTCORE_SESSION_ID_MIN_LENGTH,
    AGENTCORE_SESSION_ID_PREFIX,
    AGENTCORE_SOURCE_PREFIX,
    AGENTCORE_STACK_NAME_PREFIX,
    AGENTCORE_SUPPORTED_REGIONS,
    BEDROCK_GLOBAL_PROFILE_PREFIX,
    BEDROCK_INFERENCE_PROFILE_PREFIXES,
    CLOUDWATCH_LOGS_INGESTION_GB_PRICE_USD,
    CODEBUILD_ARM_SMALL_MINUTE_PRICE_USD,
    CODEBUILD_FREE_TIER_MINUTES_PER_MONTH,
    ECR_STORAGE_GB_MONTH_PRICE_USD,
)
from rayito._stacks._condition import StackObservation, classify_stack
from rayito._stacks._model import FIXED_TAG_KEYS
from rayito.exceptions import AgentCoreException, InvalidArgumentException

if TYPE_CHECKING:
    from rayito._agent._project_domain import AgentProject

BUCKET_SOURCE_ARGUMENT: Final = "argument"
BUCKET_SOURCE_PROJECT_FILE: Final = "project_file"
BUCKET_SOURCE_ENVIRONMENT: Final = "environment"
BUCKET_SOURCE_DISCOVER: Final = "discover"

#: Un nombre de agente: minúsculas y dígitos con `-` o `_` sueltos entre ellos
#: (`contract.json`, `agentNamePattern`).
AGENT_NAME_PATTERN: Final = re.compile(r"[a-z0-9]+(?:[_-][a-z0-9]+)*")
#: Lo que queda del nombre del runtime (48) tras su prefijo, para que todo
#: nombre derivado quepa (`AWS_API_NOTES.md`, `agentRuntimeName`).
AGENT_NAME_MAX_LENGTH: Final = AGENTCORE_RUNTIME_NAME_MAX_LENGTH - len(
    AGENTCORE_RUNTIME_NAME_PREFIX
)
RUNTIME_DESCRIPTION_TEMPLATE: Final = "Rayito agent {name} ({marker})"
RUNTIME_DESCRIPTION_PATTERN: Final = re.compile(r"Rayito agent (?P<name>\S+) \(.+\)")
RUNTIME_NAME_SEPARATOR: Final = "_"
SOURCE_ZIP_NAME_TEMPLATE: Final = "{image_tag}.zip"

#: Un id de sesión de AgentCore que Rayito deja pasar (`sessionIdRules`).
SESSION_ID_PATTERN: Final = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]*")
EMPTY_SESSION_ID_MESSAGE: Final = "session_id no puede ser vacío"
#: Caracteres hexadecimales del sha256 y del uuid que forman un id o un token
#: (`sessionIdRules`, `clientTokenDigest`).
DERIVED_HEX_CHARS: Final = 32

#: Etiquetas de AWS (`CreateAgentRuntime.tags`, `AWS_API_NOTES.md`): clave de 1
#: a 128 y valor de 0 a 256 caracteres de `[a-zA-Z0-9\s._:/=+@-]`.
TAG_KEY_MAX_CHARS: Final = 128
TAG_VALUE_MAX_CHARS: Final = 256
TAG_TEXT_PATTERN: Final = re.compile(r"[a-zA-Z0-9\s._:/=+@-]*")

REGION_ENV_VARIABLES: Final = ("AWS_REGION", "AWS_DEFAULT_REGION")
AWS_PARTITION: Final = "aws"
SECRET_ARN_TEMPLATE: Final = (
    "arn:{partition}:secretsmanager:{region}:{account}:secret:{name}-??????"
)
FOUNDATION_MODEL_ARN_TEMPLATE: Final = (
    "arn:{partition}:bedrock:{region}::foundation-model/{modelId}"
)
ANY_REGION_MODEL_ARN_TEMPLATE: Final = "arn:{partition}:bedrock:*::foundation-model/{modelId}"
GLOBAL_MODEL_ARN_TEMPLATE: Final = "arn:{partition}:bedrock:::foundation-model/{modelId}"
INFERENCE_PROFILE_ARN_TEMPLATE: Final = (
    "arn:{partition}:bedrock:{region}:{account}:inference-profile/{profileId}"
)
ARN_PREFIX: Final = "arn:"
PROFILE_SEPARATOR: Final = "."

#: Sufijo del grupo de logs del runtime (`<id>-DEFAULT`).
RUNTIME_LOG_GROUP_TEMPLATE: Final = "{prefix}{runtime_id}-{endpoint}"
IMAGE_URI_TEMPLATE: Final = "{repository_uri}:{image_tag}"
BASE_IMAGE_TEMPLATE: Final = "{mirror}@{digest}"
BYTES_PER_MEGABYTE: Final = 1_000_000
ZIP_TIMESTAMP: Final = (1980, 1, 1, 0, 0, 0)
ZIP_FILE_MODE: Final = 0o644
ZIP_UNIX_ATTRIBUTE_SHIFT: Final = 16
SECONDS_PER_MINUTE: Final = 60

#: Salidas de la pila de `infra/agentcore-runtime.yaml` (`stackOutputs`).
STACK_OUTPUT_REPOSITORY_NAME: Final = "RepositoryName"
STACK_OUTPUT_REPOSITORY_URI: Final = "RepositoryUri"
STACK_OUTPUT_REPOSITORY_ARN: Final = "RepositoryArn"
STACK_OUTPUT_BUILD_PROJECT: Final = "BuildProjectName"
STACK_OUTPUT_BUILD_LOG_GROUP: Final = "BuildLogGroupName"
STACK_OUTPUT_RUNTIME_ROLE: Final = "RuntimeRoleArn"
STACK_OUTPUT_KEYS: Final = (
    STACK_OUTPUT_REPOSITORY_NAME,
    STACK_OUTPUT_REPOSITORY_URI,
    STACK_OUTPUT_REPOSITORY_ARN,
    STACK_OUTPUT_BUILD_PROJECT,
    STACK_OUTPUT_BUILD_LOG_GROUP,
    STACK_OUTPUT_RUNTIME_ROLE,
)
#: Parámetros de la pila (`stackParameters`).
STACK_PARAMETER_AGENT_NAME: Final = "AgentName"
STACK_PARAMETER_RUNTIME_NAME: Final = "RuntimeName"
STACK_PARAMETER_BUCKET: Final = "ArtifactBucket"
STACK_PARAMETER_MODEL_ARNS: Final = "BedrockModelArns"
STACK_PARAMETER_SECRET_ARNS: Final = "ReadableSecretArns"
STACK_PARAMETER_RETENTION: Final = "ImageRetentionCount"
STACK_PARAMETER_LOG_DAYS: Final = "BuildLogRetentionDays"
STACK_PARAMETER_BUILD_TIMEOUT: Final = "BuildTimeoutMinutes"

#: Estados de un runtime (`runtimeStates`).
RUNTIME_READY: Final = "READY"
RUNTIME_IN_PROGRESS: Final = frozenset({"CREATING", "UPDATING"})
RUNTIME_BLOCKED: Final = frozenset({"CREATE_FAILED", "DELETING", "DELETE_FAILED"})
RUNTIME_REPAIRABLE: Final = frozenset({"UPDATE_FAILED"})
RUNTIME_FAILED_STATES: Final = frozenset({"CREATE_FAILED", "UPDATE_FAILED"})
#: Estados de un build de CodeBuild (`buildStates`).
BUILD_SUCCEEDED: Final = "SUCCEEDED"
BUILD_IN_PROGRESS: Final = "IN_PROGRESS"

SECURITY_NO_GATEWAY: Final = "noGateway"
SECURITY_MODELS: Final = "bedrockModels"
SECURITY_NO_MODELS: Final = "noBedrockModels"
SECURITY_SECRETS: Final = "readableSecrets"
SECURITY_PUBLIC_NETWORK: Final = "publicNetwork"
SECURITY_ISOLATION: Final = "sessionIsolation"
SECURITY_ENV_CREDENTIALS: Final = "envCredentials"

StackAction = Literal["create", "update", "wait"]
ImageAction = Literal["build", "reuse"]
RuntimeAction = Literal["create", "update", "reuse", "wait"]


# ------------------------------------------------------------ las opciones


class CallOptions(Protocol):
    """Lo que `check_call_options` mira de las opciones de la llamada:
    `AgentDeployOptions` las cumple todas."""

    @property
    def scaling(self) -> ScalingOptions | None: ...

    @property
    def secret_names(self) -> Mapping[str, str]: ...

    @property
    def create_secret_from_env(self) -> bool: ...

    @property
    def access_token(self) -> str | None: ...

    @property
    def keep_on_failure(self) -> bool: ...

    @property
    def base_image_version(self) -> str | None: ...

    @property
    def ensure_base_image(self) -> bool: ...

    @property
    def base_publisher(self) -> Callable[[], None] | None: ...

    @property
    def session_index(self) -> object | None: ...

    @property
    def pool_backend(self) -> object | None: ...

    @property
    def warm_timeout_seconds(self) -> float: ...


def _scaling_differs(field_name: str) -> Callable[[CallOptions], bool]:
    default = getattr(ScalingOptions(), field_name)

    def passed(options: CallOptions) -> bool:
        return options.scaling is not None and getattr(options.scaling, field_name) != default

    return passed


@dataclass(frozen=True)
class CallOption:
    """Una opción del despliegue en MicroVMs que no aplica a AgentCore:
    `name` es su nombre en el SDK, `flag` su bandera de la CLI (si la hay),
    `reason` por qué no aplica (`contract.json`, `optionsTable`) y `passed`
    si la llamada la trae con un valor distinto del de por defecto (`None`
    si sólo existe como bandera de la CLI)."""

    name: str
    flag: str | None
    reason: str
    passed: Callable[[CallOptions], bool] | None


NO_SECRET_GATEWAY_REASON: Final = "no hay pasarela de secretos"
NO_WARM_SLOTS_REASON: Final = "AgentCore no tiene plazas calientes"
IAM_AUTH_REASON: Final = "AgentCore autentica con IAM (SigV4), no con un access token"
#: Las opciones que no aplican, en el orden de `contract.json`
#: (`optionsTable`): ése es el orden en el que se comprueban.
CALL_OPTIONS: Final[tuple[CallOption, ...]] = (
    CallOption(
        "scaling.min_vms",
        "--min-vms",
        "AgentCore escala solo: una microVM por sesión, sin VMs fijos",
        _scaling_differs("min_vms"),
    ),
    CallOption(
        "scaling.max_vms",
        "--max-vms",
        "el tope lo pone la cuota de sesiones activas de la cuenta",
        _scaling_differs("max_vms"),
    ),
    CallOption("scaling.warm_vms", "--warm", NO_WARM_SLOTS_REASON, _scaling_differs("warm_vms")),
    CallOption(
        "scaling.scale_in_after_seconds",
        None,
        "usa idle_session_timeout_seconds / --idle-session-timeout",
        _scaling_differs("scale_in_after_seconds"),
    ),
    CallOption(
        "memory_mib",
        "--memory",
        "cada sesión usa hasta 2 vCPU y 8 GB y se cobra por consumo real",
        None,
    ),
    CallOption("serve.port", "--port", "el contenedor de AgentCore sirve en el 8080", None),
    CallOption(
        "serve.timeout_seconds",
        "--timeout",
        "usa max_session_lifetime_seconds / --max-session-lifetime",
        None,
    ),
    CallOption(
        "serve.idle",
        "--idle",
        "usa idle_session_timeout_seconds / --idle-session-timeout",
        None,
    ),
    CallOption(
        "secret_names",
        "--secret",
        "no hay pasarela de secretos; para dejar leer un secreto usa readable_secrets / "
        "--readable-secret",
        lambda options: bool(options.secret_names),
    ),
    CallOption(
        "create_secret_from_env",
        "--create-secret",
        NO_SECRET_GATEWAY_REASON,
        lambda options: options.create_secret_from_env,
    ),
    CallOption(
        "access_token",
        "--token-file",
        IAM_AUTH_REASON,
        lambda options: options.access_token is not None,
    ),
    CallOption(
        "keep_on_failure",
        "--keep-on-failure",
        "AgentCore siempre conserva lo creado si falla; bórralo con rayito agent delete",
        lambda options: options.keep_on_failure,
    ),
    CallOption(
        "base_image_version",
        None,
        "AgentCore no usa la imagen base de MicroVMs",
        lambda options: options.base_image_version is not None,
    ),
    CallOption(
        "ensure_base_image",
        None,
        "AgentCore no usa la imagen base de MicroVMs",
        lambda options: options.ensure_base_image is not True,
    ),
    CallOption(
        "base_publisher",
        "--base-artifact",
        "AgentCore no usa la imagen base de MicroVMs",
        lambda options: options.base_publisher is not None,
    ),
    CallOption(
        "session_index",
        None,
        "AgentCore enruta las sesiones por su id",
        lambda options: options.session_index is not None,
    ),
    CallOption(
        "pool_backend",
        None,
        NO_WARM_SLOTS_REASON,
        lambda options: options.pool_backend is not None,
    ),
    CallOption(
        "warm_timeout_seconds",
        None,
        NO_WARM_SLOTS_REASON,
        lambda options: options.warm_timeout_seconds != AGENT_FLEET_WARM_READY_TIMEOUT_SECONDS,
    ),
)
#: `--token-file` de `invoke` y de `status` (AgentCore no usa un access token).
TOKEN_FILE_FLAG: Final = "--token-file"


def check_call_options(options: CallOptions, cli_flags: Collection[str] = ()) -> None:
    """`option_not_supported` con la primera opción de MicroVMs que la llamada
    trae y AgentCore no admite, en el orden de `CALL_OPTIONS`. `cli_flags` son
    las banderas de la CLI que se pasaron (`--min-vms`...); un valor igual al
    de por defecto cuenta como no puesto. Antes de cualquier llamada a AWS."""
    for option in CALL_OPTIONS:
        if option.flag is not None and option.flag in cli_flags:
            raise AgentCoreException(
                "option_not_supported", option=option.flag, reason=option.reason
            )
        if option.passed is not None and option.passed(options):
            raise AgentCoreException(
                "option_not_supported", option=option.name, reason=option.reason
            )


def require_supported_region(region: str | None) -> str:
    """La región de AgentCore con builds arm64 de CodeBuild
    (`AGENTCORE_SUPPORTED_REGIONS`); sin región, `InvalidArgumentException`."""
    if not region:
        raise InvalidArgumentException(
            "AgentCore necesita una región de AWS: pasa region= (--region) o exporta AWS_REGION"
        )
    if region not in AGENTCORE_SUPPORTED_REGIONS:
        raise AgentCoreException(
            "unsupported_region", region=region, regions=", ".join(AGENTCORE_SUPPORTED_REGIONS)
        )
    return region


def check_provider(project: AgentProject) -> None:
    """Sólo `bedrock` se cablea: sin pasarela, la credencial de otro proveedor
    iría en claro en la configuración del runtime."""
    provider = project.spec.model.provider
    if provider != "bedrock":
        raise AgentCoreException("provider_unsupported", provider=provider)


def check_network(project: AgentProject) -> None:
    """Una política de salida del proyecto es un error: en modo PUBLIC
    AgentCore no filtra la salida y aplicarla a medias sería un riesgo."""
    network = project.network
    policies = {
        "egress": bool(project.egress),
        "allow_out": network is not None and bool(network.allow_out),
        "deny_out": network is not None and bool(network.deny_out),
    }
    present = [name for name, found in policies.items() if found]
    if present:
        raise AgentCoreException("network_policy_unsupported", what=", ".join(present))


def check_project(project: AgentProject) -> None:
    check_provider(project)
    check_network(project)


def check_raw_protocol(is_raw: bool) -> None:
    if is_raw:
        raise AgentCoreException("raw_protocol")


# ------------------------------------------------------------------ nombres


def agent_name_of(project: AgentProject) -> str:
    return template_name(project)


def validate_agent_name(name: str) -> str:
    """El nombre del agente, o `name_invalid`: hasta `AGENT_NAME_MAX_LENGTH`
    caracteres para que todo nombre derivado quepa."""
    if len(name) > AGENT_NAME_MAX_LENGTH or not AGENT_NAME_PATTERN.fullmatch(name):
        raise AgentCoreException("name_invalid", name=name, max=AGENT_NAME_MAX_LENGTH)
    return name


def runtime_name_for(name: str) -> str:
    return AGENTCORE_RUNTIME_NAME_PREFIX + name.replace("-", RUNTIME_NAME_SEPARATOR)


def stack_name_for(name: str) -> str:
    return AGENTCORE_STACK_NAME_PREFIX + name


def repository_name_for(name: str) -> str:
    return AGENTCORE_REPOSITORY_PREFIX + name


def build_project_name_for(name: str) -> str:
    return AGENTCORE_STACK_NAME_PREFIX + name


def source_key_for(name: str, image_tag: str) -> str:
    zip_name = SOURCE_ZIP_NAME_TEMPLATE.format(image_tag=image_tag)
    return f"{AGENTCORE_SOURCE_PREFIX}{name}/{zip_name}"


def runtime_description_for(name: str) -> str:
    return RUNTIME_DESCRIPTION_TEMPLATE.format(name=name, marker=AGENTCORE_DESCRIPTION_MARKER)


def runtime_log_group_for(runtime_id: str) -> str:
    return RUNTIME_LOG_GROUP_TEMPLATE.format(
        prefix=AGENTCORE_RUNTIME_LOG_GROUP_PREFIX,
        runtime_id=runtime_id,
        endpoint=AGENTCORE_ENDPOINT_NAME,
    )


def image_uri_for(repository_uri: str, image_tag: str) -> str:
    return IMAGE_URI_TEMPLATE.format(repository_uri=repository_uri, image_tag=image_tag)


def base_image_reference(digest: str) -> str:
    """La imagen base de CodeBuild: el espejo de ECR Public con el mismo
    digest que fija el export."""
    return BASE_IMAGE_TEMPLATE.format(mirror=AGENTCORE_BASE_IMAGE_MIRROR, digest=digest)


def is_owned(listed: RuntimeListing) -> bool:
    """Un runtime es de Rayito sólo con su prefijo de nombre y la marca en la
    descripción: uno con el nombre y sin la marca nunca se toca."""
    return listed.name.startswith(AGENTCORE_RUNTIME_NAME_PREFIX) and has_marker(listed.description)


def has_marker(description: str) -> bool:
    return f"({AGENTCORE_DESCRIPTION_MARKER})" in description


def agent_name_of_runtime(listed: RuntimeListing) -> str:
    """El nombre del agente de un runtime de Rayito, de su descripción; si no
    se lee, el del runtime sin su prefijo."""
    match = RUNTIME_DESCRIPTION_PATTERN.fullmatch(listed.description)
    if match is not None:
        return match.group("name")
    return listed.name.removeprefix(AGENTCORE_RUNTIME_NAME_PREFIX)


# ------------------------------------------------- identidad y contenido


def _sha256_hex(data: bytes, chars: int) -> str:
    return hashlib.sha256(data).hexdigest()[:chars]


def image_tag_for(tree: Mapping[str, bytes]) -> str:
    """La etiqueta de imagen por contenido: `src-` y los primeros 16
    caracteres del sha256 del árbol (`constants.imageTagDigest`): por cada
    fichero, en orden de ruta UTF-8, la ruta, un 0, la longitud en decimal,
    un 0 y los bytes."""
    digest = hashlib.sha256()
    for path in sorted(tree, key=lambda item: item.encode("utf-8")):
        data = tree[path]
        digest.update(path.encode("utf-8") + b"\0" + str(len(data)).encode("ascii") + b"\0")
        digest.update(data)
    return AGENTCORE_IMAGE_TAG_PREFIX + digest.hexdigest()[:AGENTCORE_IMAGE_TAG_HEX_CHARS]


def client_token_for(request: Mapping[str, Any]) -> str:
    """El `clientToken` de una petición de crear o actualizar un runtime: el
    sha256 de su JSON canónico sin el propio token (`clientTokenDigest`). Un
    reintento de la misma petición repite el token y AgentCore no crea un
    duplicado."""
    canonical = json.dumps(
        {key: value for key, value in request.items() if key != "clientToken"},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return AGENTCORE_SESSION_ID_PREFIX + _sha256_hex(canonical.encode("utf-8"), DERIVED_HEX_CHARS)


def agentcore_session_id(
    session_id: str | None, new_uuid: Callable[[], uuid.UUID] = uuid.uuid4
) -> str:
    """El `runtimeSessionId` de una llamada (`constants.sessionIdRules`): uno
    nuevo sin `session_id`; el del llamante si ya tiene la forma de AgentCore
    y mide entre 33 y 100 caracteres; si no, `rayito-` y 32 hex del sha256 de
    su UTF-8, así que el mismo id del llamante llega siempre a la misma
    sesión. Un id vacío es `InvalidArgumentException`."""
    if session_id is None:
        return AGENTCORE_SESSION_ID_PREFIX + new_uuid().hex
    if not session_id:
        raise InvalidArgumentException(EMPTY_SESSION_ID_MESSAGE)
    if (
        SESSION_ID_PATTERN.fullmatch(session_id)
        and AGENTCORE_SESSION_ID_MIN_LENGTH <= len(session_id) <= AGENTCORE_SESSION_ID_MAX_LENGTH
    ):
        return session_id
    return AGENTCORE_SESSION_ID_PREFIX + _sha256_hex(session_id.encode("utf-8"), DERIVED_HEX_CHARS)


def source_zip(tree: Mapping[str, bytes]) -> bytes:
    """El zip determinista del contexto de build: entradas ordenadas, fecha
    fija y modo 0644, así que el mismo árbol da siempre los mismos bytes."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(tree):
            entry = zipfile.ZipInfo(path, date_time=ZIP_TIMESTAMP)
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = ZIP_FILE_MODE << ZIP_UNIX_ATTRIBUTE_SHIFT
            archive.writestr(entry, tree[path])
    return buffer.getvalue()


# ------------------------------------------------------- roles y permisos


def _unique(items: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))


def _model_arns(model: str, region: str, account: str, partition: str) -> list[str]:
    if model.startswith(ARN_PREFIX):
        return [model]
    prefix, _, base = model.partition(PROFILE_SEPARATOR)
    if prefix not in BEDROCK_INFERENCE_PROFILE_PREFIXES or not base:
        return [
            FOUNDATION_MODEL_ARN_TEMPLATE.format(partition=partition, region=region, modelId=model)
        ]
    arns = [
        INFERENCE_PROFILE_ARN_TEMPLATE.format(
            partition=partition, region=region, account=account, profileId=model
        ),
        ANY_REGION_MODEL_ARN_TEMPLATE.format(partition=partition, modelId=base),
    ]
    if prefix == BEDROCK_GLOBAL_PROFILE_PREFIX:
        arns.append(GLOBAL_MODEL_ARN_TEMPLATE.format(partition=partition, modelId=base))
    return arns


def bedrock_resource_arns(
    models: Sequence[str], model_region: str, account: str, partition: str = AWS_PARTITION
) -> tuple[str, ...]:
    """Los ARNs que el rol del runtime puede invocar (`BedrockModelArns`): un
    id de perfil de inferencia del sistema da su ARN en la región del modelo
    y el modelo base en cualquier región (un perfil global, además el modelo
    base sin región); otro id es un modelo base de la región; un ARN vale tal
    cual. Sin duplicados y en orden."""
    arns = [arn for model in models for arn in _model_arns(model, model_region, account, partition)]
    return _unique(arns)


def secret_resource_arns(
    secrets: Sequence[str], region: str, account: str, partition: str = AWS_PARTITION
) -> tuple[str, ...]:
    """Los ARNs de los secretos que el rol puede leer (`ReadableSecretArns`):
    el nombre con el sufijo de seis caracteres que Secrets Manager añade, o
    el ARN tal cual."""
    arns = [
        secret
        if secret.startswith(ARN_PREFIX)
        else SECRET_ARN_TEMPLATE.format(
            partition=partition, region=region, account=account, name=secret
        )
        for secret in secrets
    ]
    return _unique(arns)


# --------------------------------------------- validación sin AWS del plan


def resolve_lifecycle(idle_seconds: int | None, max_seconds: int | None) -> AgentCoreLifecycle:
    """El ciclo de vida del runtime: cada valor entre 60 y 28800 (`lifecycle_
    invalid`), por defecto 900 y 28800, con el plazo de inactividad como mucho
    el de vida (`lifecycle_order`). Sólo el de vida y por debajo de 900 baja
    también el de inactividad, para que el valor por defecto no lo contradiga."""
    for option, value in (
        ("idle_session_timeout", idle_seconds),
        ("max_session_lifetime", max_seconds),
    ):
        if value is not None and not (
            AGENTCORE_LIFECYCLE_MIN_SECONDS <= value <= AGENTCORE_LIFECYCLE_MAX_SECONDS
        ):
            raise AgentCoreException(
                "lifecycle_invalid",
                option=option,
                min=AGENTCORE_LIFECYCLE_MIN_SECONDS,
                max=AGENTCORE_LIFECYCLE_MAX_SECONDS,
            )
    longest = AGENTCORE_LIFECYCLE_MAX_SECONDS if max_seconds is None else max_seconds
    if idle_seconds is None:
        idle_seconds = min(AGENTCORE_IDLE_SESSION_TIMEOUT_DEFAULT_SECONDS, longest)
    if idle_seconds > longest:
        raise AgentCoreException("lifecycle_order", idle=idle_seconds, max=longest)
    return AgentCoreLifecycle(idle_seconds, longest)


def validate_tags(tags: Mapping[str, str]) -> dict[str, str]:
    """Las etiquetas del usuario, ordenadas, o `tags_invalid`: contando las
    tres fijas de la pila no pasan de `AGENTCORE_MAX_TAGS`, y cada clave y
    valor lleva sólo los caracteres que AWS admite."""
    merged = {*tags, *FIXED_TAG_KEYS}
    valid = len(merged) <= AGENTCORE_MAX_TAGS and all(
        0 < len(key) <= TAG_KEY_MAX_CHARS
        and len(value) <= TAG_VALUE_MAX_CHARS
        and TAG_TEXT_PATTERN.fullmatch(key)
        and TAG_TEXT_PATTERN.fullmatch(value)
        for key, value in tags.items()
    )
    if not valid:
        raise AgentCoreException("tags_invalid", max=AGENTCORE_MAX_TAGS)
    return dict(sorted(tags.items()))


def runtime_environment(project: AgentProject, region: str) -> dict[str, str]:
    """`environmentVariables` del runtime: el `env` del proyecto y las dos
    variables de región, que ganan (`env_invalid` si no caben). Nunca
    secretos."""
    environment = {**project.env, **dict.fromkeys(REGION_ENV_VARIABLES, region)}
    too_many = len(environment) > AGENTCORE_MAX_ENVIRONMENT_VARIABLES
    too_long = any(
        len(value) > AGENTCORE_ENVIRONMENT_VALUE_MAX_CHARS for value in environment.values()
    )
    if too_many or too_long:
        raise AgentCoreException(
            "env_invalid",
            maxVariables=AGENTCORE_MAX_ENVIRONMENT_VARIABLES,
            maxChars=AGENTCORE_ENVIRONMENT_VALUE_MAX_CHARS,
        )
    return dict(sorted(environment.items()))


def file_warnings(project: AgentProject) -> tuple[str, ...]:
    """Las claves de `WARNING_TEXTS` de los valores del fichero de proyecto
    (compartido entre destinos) que AgentCore no usa: se ignoran con un
    aviso, a diferencia de lo que pasa la llamada. El plazo `[serve].idle` se
    avisa siempre que el fichero lo fije, aunque valga lo mismo que el de por
    defecto: sin `idle_seconds`, `ServeOptions.idle` es el propio objeto
    `DEFAULT_IDLE_POLICY` (como en `_lifecycle_base`), así que se compara por
    identidad."""
    default_scaling = ScalingOptions()
    default_serve = ServeOptions()
    scaling = project.scaling
    checks = (
        (
            "fileScaling",
            (scaling.min_vms, scaling.max_vms, scaling.warm_vms, scaling.scale_in_after_seconds)
            != (
                default_scaling.min_vms,
                default_scaling.max_vms,
                default_scaling.warm_vms,
                default_scaling.scale_in_after_seconds,
            ),
        ),
        ("fileMemory", project.memory_mib != AGENT_MIN_MEMORY_MIB),
        ("filePort", project.serve.port != default_serve.port),
        ("fileTimeout", project.serve.timeout_seconds != default_serve.timeout_seconds),
        ("fileIdle", project.serve.idle is not default_serve.idle),
        ("fileBaseImage", project.base_image_version is not None),
    )
    return tuple(key for key, differs in checks if differs)


def bedrock_models_of(project: AgentProject, agentcore: AgentCoreOptions) -> tuple[str, ...]:
    """Los modelos que el rol puede invocar: el del proyecto y los de
    `AgentCoreOptions.bedrock_models`, sin repetir."""
    return _unique([project.spec.model.id, *agentcore.bedrock_models])


def security_keys(
    project: AgentProject, models: Sequence[str], secrets: Sequence[str]
) -> tuple[str, ...]:
    """Las claves de `SECURITY_TEXTS` del plan, en el orden en que se
    imprimen: la diferencia con la pasarela de MicroVMs va primero."""
    keys = [SECURITY_NO_GATEWAY, SECURITY_MODELS if models else SECURITY_NO_MODELS]
    if secrets:
        keys.append(SECURITY_SECRETS)
    keys.extend((SECURITY_PUBLIC_NETWORK, SECURITY_ISOLATION))
    if uses_env_credentials(project.runtime):
        keys.append(SECURITY_ENV_CREDENTIALS)
    return tuple(keys)


def _usd(price: float) -> str:
    whole, _, fraction = repr(price).partition(".")
    return f"${whole},{fraction.ljust(2, '0')}"


def cost_lines() -> tuple[str, ...]:
    """El coste de lista de `COST_TEXTS` con los precios de `limits.json`."""
    fields = {
        "runtimeVcpuHour": _usd(AGENTCORE_RUNTIME_VCPU_HOUR_PRICE_USD),
        "runtimeGbHour": _usd(AGENTCORE_RUNTIME_GB_HOUR_PRICE_USD),
        "idleMinutes": AGENTCORE_IDLE_SESSION_TIMEOUT_DEFAULT_SECONDS // SECONDS_PER_MINUTE,
        "buildMinute": _usd(CODEBUILD_ARM_SMALL_MINUTE_PRICE_USD),
        "freeMinutes": CODEBUILD_FREE_TIER_MINUTES_PER_MONTH,
        "ecrGbMonth": _usd(ECR_STORAGE_GB_MONTH_PRICE_USD),
        "retained": AGENTCORE_IMAGE_RETENTION_COUNT,
        "logsGb": _usd(CLOUDWATCH_LOGS_INGESTION_GB_PRICE_USD),
    }
    return tuple(COST_TEXTS[key].format(**fields) for key in COST_KEYS)


COST_KEYS: Final = ("runtime", "build", "image", "logs", "model")


def bucket_source_of(
    argument: str | None, project_file: str | None, environ: Mapping[str, str]
) -> tuple[str | None, str]:
    """El bucket del contexto de build y de dónde sale, sin preguntar a AWS:
    el argumento, `[deploy].bucket`, `RAYITO_BUCKET` y, si ninguno, se
    descubre al aplicar (el del bootstrap de CDK o el de `rayito setup`)."""
    from_environment = environ.get(BUCKET_ENV_VAR)
    for bucket, source in (
        (argument, BUCKET_SOURCE_ARGUMENT),
        (project_file, BUCKET_SOURCE_PROJECT_FILE),
        (from_environment, BUCKET_SOURCE_ENVIRONMENT),
    ):
        if bucket:
            return bucket, source
    return None, BUCKET_SOURCE_DISCOVER


def planned_steps(name: str) -> tuple[AgentCoreStep, ...]:
    return (
        AgentCoreStep("stack", stack_name_for(name), "planned"),
        AgentCoreStep("image", repository_name_for(name), "planned"),
        AgentCoreStep("runtime", runtime_name_for(name), "planned"),
    )


def build_plan(
    project: AgentProject,
    options: CallOptions,
    agentcore: AgentCoreOptions,
    *,
    bucket: str | None,
    region: str | None,
    environ: Mapping[str, str],
    tree_for: Callable[[], Mapping[str, bytes]],
    detection: object | None = None,
    cli_flags: Collection[str] = (),
) -> AgentCoreDeployPlan:
    """El plan de `plan_deploy(target="agentcore")`, sin AWS. Valida en este
    orden: la región, las opciones de MicroVMs de la llamada, el proveedor del
    modelo, la política de red, el nombre, el ciclo de vida, las etiquetas y
    el entorno; el primero que falla es el error. `bucket` es el bucket de la
    llamada (`AgentDeployOptions.bucket`)."""
    supported = require_supported_region(region)
    check_call_options(options, cli_flags)
    check_project(project)
    name = validate_agent_name(agent_name_of(project))
    lifecycle = resolve_lifecycle(
        agentcore.idle_session_timeout_seconds, agentcore.max_session_lifetime_seconds
    )
    tags = validate_tags(agentcore.tags)
    environment = runtime_environment(project, supported)
    image_tag = image_tag_for(tree_for())
    chosen_bucket, source = bucket_source_of(bucket, project.bucket, environ)
    models = bedrock_models_of(project, agentcore)
    return AgentCoreDeployPlan(
        name=name,
        region=supported,
        runtime_name=runtime_name_for(name),
        stack_name=stack_name_for(name),
        repository_name=repository_name_for(name),
        build_project_name=build_project_name_for(name),
        image_tag=image_tag,
        bucket=chosen_bucket,
        bucket_source=source,
        network=AGENTCORE_NETWORK_MODE,
        bedrock_models=models,
        readable_secrets=agentcore.readable_secrets,
        environment=MappingProxyType(environment),
        lifecycle=lifecycle,
        tags=MappingProxyType(tags),
        steps=planned_steps(name),
        security=security_keys(project, models, agentcore.readable_secrets),
        warnings=file_warnings(project),
        cost=cost_lines(),
        detection=detection,
    )


# ------------------------------------------------ decisiones al aplicar


@dataclass(frozen=True)
class StackOutputs:
    """Las seis salidas de `infra/agentcore-runtime.yaml`."""

    repository_name: str
    repository_uri: str
    repository_arn: str
    build_project_name: str
    build_log_group: str
    runtime_role_arn: str


def decide_stack(observation: StackObservation, name: str) -> StackAction:
    """Qué hacer con la pila del agente: `create` si falta, `update` si sirve
    (CloudFormation no toca nada si no cambió), `wait` si hay una operación en
    curso, y `stack_blocked` si no se puede usar."""
    condition = classify_stack(observation)
    if condition == "missing":
        return "create"
    if condition == "usable":
        return "update"
    if condition == "in_progress":
        return "wait"
    raise AgentCoreException(
        "stack_blocked", stackName=observation.name, status=observation.status, name=name
    )


def stack_outputs_of(observation: StackObservation) -> StackOutputs:
    """Las salidas de una pila lista; sin alguna, no es la plantilla de Rayito
    (`stack_failed`)."""
    outputs = observation.outputs
    if not all(outputs.get(key) for key in STACK_OUTPUT_KEYS):
        raise AgentCoreException(
            "stack_failed", stackName=observation.name, status=observation.status
        )
    return StackOutputs(
        repository_name=outputs[STACK_OUTPUT_REPOSITORY_NAME],
        repository_uri=outputs[STACK_OUTPUT_REPOSITORY_URI],
        repository_arn=outputs[STACK_OUTPUT_REPOSITORY_ARN],
        build_project_name=outputs[STACK_OUTPUT_BUILD_PROJECT],
        build_log_group=outputs[STACK_OUTPUT_BUILD_LOG_GROUP],
        runtime_role_arn=outputs[STACK_OUTPUT_RUNTIME_ROLE],
    )


def decide_image(
    image: ImageObservation | None, build_image: bool | None, image_tag: str, repository: str
) -> ImageAction:
    """`reuse` si la etiqueta (el contenido) ya está en el repositorio;
    si no, `build`, salvo con `--no-build`, que es `image_missing`. Un
    `build_image=True` se comporta como el valor por defecto: la etiqueta es
    el contenido."""
    if image is not None and image.exists:
        return "reuse"
    if build_image is False:
        raise AgentCoreException("image_missing", imageTag=image_tag, repositoryName=repository)
    return "build"


def check_image_size(size_bytes: int) -> None:
    """Una imagen de más de 2 GB no la admite AgentCore (`image_too_large`)."""
    if size_bytes > AGENTCORE_MAX_IMAGE_BYTES:
        raise AgentCoreException(
            "image_too_large",
            megabytes=size_bytes // BYTES_PER_MEGABYTE,
            maxMegabytes=AGENTCORE_MAX_IMAGE_BYTES // BYTES_PER_MEGABYTE,
        )


def runtime_matches(details: RuntimeDetails, spec: RuntimeSpec) -> bool:
    """Si el runtime ya tiene la configuración que se quiere
    (`runtimeComparedFields`), con MMDSv2 exigido."""
    return (
        details.container_uri == spec.container_uri
        and details.role_arn == spec.role_arn
        and details.network_mode == AGENTCORE_NETWORK_MODE
        and details.server_protocol == AGENTCORE_SERVER_PROTOCOL
        and dict(details.environment_variables) == dict(spec.environment_variables)
        and details.idle_runtime_session_timeout == spec.idle_runtime_session_timeout
        and details.max_lifetime == spec.max_lifetime
        and details.require_mmds_v2
    )


def check_runtime_usable(listed: RuntimeListing | None, runtime_name: str, name: str) -> None:
    """`name_conflict` si el runtime con ese nombre no es de Rayito y
    `runtime_blocked` si está en un estado que no se puede tocar. Se comprueba
    antes de crear la pila y construir la imagen: ahorra el coste de un
    despliegue que acabaría fallando."""
    if listed is None:
        return
    if not is_owned(listed):
        raise AgentCoreException("name_conflict", runtimeName=runtime_name, name=name)
    if listed.status in RUNTIME_BLOCKED:
        raise AgentCoreException(
            "runtime_blocked", runtimeName=runtime_name, status=listed.status, name=name
        )


def decide_runtime(
    listed: RuntimeListing | None, details: RuntimeDetails | None, spec: RuntimeSpec, name: str
) -> RuntimeAction:
    """Qué hacer con el runtime: `create` si no existe; `name_conflict` si el
    nombre es de otro; `wait` si está en curso; `runtime_blocked` si no se puede
    tocar; `update` si quedó en `UPDATE_FAILED` o su configuración difiere;
    `reuse` si ya está al día."""
    check_runtime_usable(listed, spec.name, name)
    if listed is None:
        return "create"
    if listed.status in RUNTIME_IN_PROGRESS:
        return "wait"
    if listed.status in RUNTIME_REPAIRABLE or details is None:
        return "update"
    return "reuse" if runtime_matches(details, spec) else "update"
