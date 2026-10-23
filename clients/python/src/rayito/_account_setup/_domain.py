"""Dominio puro de `rayito setup` (ADR-030): el plan de cuatro pasos (bucket,
pila de IAM, imagen base e imagen base con capacidades), sus reglas, los errores cerrados, las
constantes y los textos del contrato compartido con la CLI de TypeScript
(`testdata/account-setup/contract.json`).

Sólo importa la biblioteca estándar, `rayito._limits` y las constantes de
`rayito._images`: nada de red, ficheros ni AWS. Cada decisión es una
función pura sobre lo observado en la cuenta.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Literal

from rayito._images import (
    ACTIVE_VERSION_STATUS,
    BUILD_ROLE_OUTPUT_KEY,
    BUILDING_VERSION_STATES,
    DEFAULT_BASE_IMAGE_NAME,
    DEFAULT_IAM_STACK_NAME,
    LAUNCHABLE_IMAGE_STATES,
    SUCCESSFUL_VERSION_STATE,
)
from rayito._limits import SUPPORTED_REGIONS
from rayito._stacks._condition import DELETING_STACK_STATE as DELETING_STACK_STATE
from rayito._stacks._condition import IN_PROGRESS_STACK_SUFFIX as IN_PROGRESS_STACK_SUFFIX
from rayito._stacks._condition import USABLE_STACK_STATES as USABLE_STACK_STATES
from rayito._stacks._condition import StackObservation as StackObservation
from rayito._stacks._condition import classify_stack

ResourceKind = Literal["bucket", "iamStack", "baseImage", "capsImage"]
SetupAction = Literal["create", "update", "reuse", "wait", "publish", "skip"]
BucketSource = Literal[
    "argument", "environment", "iamStack", "cdkBootstrap", "artifactStack", "rayitoDefault"
]
BucketState = Literal["found", "missing", "forbidden"]
ImageSource = Literal["release", "artifact"]
ImageCondition = Literal["missing", "launchable", "building", "unlaunchable"]
ErrorCode = Literal[
    "unsupported_region",
    "bucket_unavailable",
    "bucket_region",
    "bucket_taken",
    "artifact_stack_conflict",
    "artifact_missing",
    "artifact_variant",
    "needs_confirmation",
    "stack_blocked",
    "stack_incomplete",
    "release_download",
    "checksum_missing",
    "checksum_mismatch",
    "image_build_failed",
]

#: Los cuatro pasos del plan, siempre en este orden.
RESOURCE_ORDER: Final[tuple[ResourceKind, ...]] = ("bucket", "iamStack", "baseImage", "capsImage")
#: Acciones que admite cada recurso (diseño D2).
ALLOWED_ACTIONS: Final[Mapping[ResourceKind, frozenset[SetupAction]]] = MappingProxyType(
    {
        "bucket": frozenset({"create", "reuse", "wait"}),
        "iamStack": frozenset({"create", "update", "reuse", "wait"}),
        "baseImage": frozenset({"publish", "reuse", "wait"}),
        "capsImage": frozenset({"publish", "reuse", "wait", "skip"}),
    }
)
#: Acciones que escriben en la cuenta y por tanto piden confirmación.
WRITING_ACTIONS: Final = frozenset({"create", "update", "publish"})
#: Acciones de una cuenta ya preparada: nada que hacer.
READY_ACTIONS: Final = frozenset({"reuse", "skip"})

IAM_STACK_NAME: Final = DEFAULT_IAM_STACK_NAME
IAM_COMPONENT_NAME: Final = "iam"
IAM_CAPABILITIES: Final = ("CAPABILITY_NAMED_IAM",)
ARTIFACT_BUCKET_PARAMETER: Final = "ArtifactBucket"
#: Parámetro de `infra/iam.yaml` con el sha256 de la plantilla que la
#: desplegó (`template_fingerprint`). Una pila de 0.17.0 o anterior no lo
#: tiene y una desplegada a mano lo deja vacío: en los dos casos la plantilla
#: del SDK es otra y `rayito setup` la actualiza.
TEMPLATE_FINGERPRINT_PARAMETER: Final = "TemplateFingerprint"
#: Plantilla de la pila de IAM; el mensaje `stack_incomplete` la nombra.
IAM_TEMPLATE_PATH: Final = "infra/iam.yaml"
#: La pila que crea el bucket de artefactos cuando no hay ninguno que
#: reutilizar (contrato; diseño D4 y D10), junto a `rayito-m0-iam`.
ARTIFACT_STACK_NAME: Final = "rayito-m0-artifacts"
ARTIFACT_COMPONENT_NAME: Final = "artifact-bucket"
ARTIFACT_TEMPLATE_PATH: Final = "infra/artifact-bucket.yaml"
#: Parámetro de `infra/artifact-bucket.yaml` con el nombre del bucket y salida
#: que lo devuelve (diseño D10).
BUCKET_NAME_PARAMETER: Final = "BucketName"
BUCKET_NAME_OUTPUT_KEY: Final = "BucketName"
#: Nombre del bucket sin `--bucket` ni `RAYITO_BUCKET` (diseño D4): determinista
#: para que una nueva ejecución lo encuentre; como mucho 44 caracteres.
DEFAULT_ARTIFACT_BUCKET_TEMPLATE: Final = "rayito-artifacts-{account}-{region}"
#: Días que tardan en caducar los zips bajo `rayito/images/`; igual al
#: `Default` de `ArtifactExpirationDays` en la plantilla (diseño D10).
ARTIFACT_EXPIRATION_DAYS: Final = 7
#: Orígenes de un nombre que el usuario pidió expresamente: si no existe se
#: crea con ese nombre y es el único candidato.
EXPLICIT_BUCKET_SOURCES: Final[frozenset[BucketSource]] = frozenset({"argument", "environment"})
BASE_IMAGE_NAME: Final = DEFAULT_BASE_IMAGE_NAME
BASE_IMAGE_VARIANT: Final = "full"
#: La base que el agente en MicroVM usa por defecto (igual a
#: `DEFAULT_AGENT_TEMPLATE_BASE`, que un test compara): la misma imagen
#: `full` publicada con `additionalOsCapabilities` (`AWS_API_NOTES.md` §4,
#: el camino de `rayito image publish --os-capabilities ALL`).
CAPS_IMAGE_NAME: Final = "rayito-base-caps"
#: El único valor de `additionalOsCapabilities` (§4; igual a
#: `ALL_OS_CAPABILITIES` de `rayito image publish`).
CAPS_IMAGE_OS_CAPABILITIES: Final = "ALL"
BUCKET_ENV_VAR: Final = "RAYITO_BUCKET"
#: Calificador por defecto del bootstrap de CDK
#: (docs.aws.amazon.com/cdk/v2/guide/bootstrapping-env.html, consultado
#: 2026-10-16): el bucket de activos es `cdk-<calificador>-assets-<cuenta>-<región>`.
CDK_BOOTSTRAP_QUALIFIER: Final = "hnb659fds"
CDK_ASSETS_BUCKET_TEMPLATE: Final = "cdk-{qualifier}-assets-{account}-{region}"
RELEASE_TAG_PREFIX: Final = "rayd-v"
RELEASE_DOWNLOAD_URL_TEMPLATE: Final = (
    "https://github.com/alejandro-cedeno-10/rayito/releases/download/{tag}/{asset}"
)
IMAGE_ASSET_NAME: Final = "rayito-image.zip"
CHECKSUMS_ASSET_NAME: Final = "SHA256SUMS"
#: Plazo de descarga de cada asset de la release (`tasks.md`, contrato).
RELEASE_DOWNLOAD_TIMEOUT_SECONDS: Final = 600.0
#: Línea de `sha256sum`: 64 hex en minúsculas, un espacio, y espacio (texto)
#: o `*` (binario) antes del nombre.
CHECKSUM_LINE_PATTERN: Final = re.compile(r"(?P<sha256>[0-9a-f]{64}) [ *](?P<name>\S.*)")

#: Cómo se nombra cada origen del bucket al usuario.
BUCKET_SOURCE_LABELS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "argument": "--bucket",
        "environment": "RAYITO_BUCKET",
        "iamStack": "el de la pila de IAM",
        "cdkBootstrap": "bootstrap de CDK",
        "artifactStack": "el de la pila de artefactos",
        "rayitoDefault": "nombre por defecto de Rayito",
    }
)

#: Cifras ya publicadas, no nuevas (el bucket: los días de caducidad de
#: `infra/artifact-bucket.yaml`, diseño D10); la imagen, `cost.md` fila "Mantener
#: publicada una versión de imagen"; la pila de IAM, `configurar-aws.md`
#: paso 3; el zip en el bucket, `configurar-aws.md` paso 2.
COST_TEXTS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "bucket": (
            "bucket de artefactos: los zips de la imagen caducan a los {days} días (regla de "
            "ciclo de vida)"
        ),
        "iamStack": "pila de IAM: gratis",
        "image": "imagen: ≈ $0,04 por semana por versión publicada (mínimo una semana)",
        "artifact": "zip de la imagen en el bucket: céntimos",
        "capsImage": (
            "imagen con capacidades: ≈ $0,04 por semana por versión publicada ($0,08 por "
            "GB-mes, mínimo una semana); sólo con --caps-image"
        ),
    }
)

ERROR_MESSAGES: Final[Mapping[ErrorCode, str]] = MappingProxyType(
    {
        "unsupported_region": "la región {region} no tiene Lambda MicroVMs: usa una de {regions}",
        "bucket_unavailable": (
            "el bucket {bucket} ({source}) existe pero no es accesible con estas credenciales: "
            "puede ser de otra cuenta; pasa otro nombre con --bucket"
        ),
        "bucket_region": (
            "el bucket {bucket} está en {bucketRegion}, no en {region}: el build fallaría con "
            "S3_CROSS_REGION_ACCESS_DENIED"
        ),
        "bucket_taken": (
            "el nombre {bucket} ya lo usa un bucket que no creó la pila {stack}: si es tuyo, "
            "pásalo con --bucket {bucket}; si no, elige otro nombre con --bucket"
        ),
        "artifact_stack_conflict": (
            "la pila {stack} ya gestiona el bucket {existing}: pasa --bucket {existing} o, para "
            "crear {bucket}, borra antes esa pila (el bucket {existing} se conserva)"
        ),
        "artifact_missing": "{artifact} no existe",
        "artifact_variant": (
            "{artifact} no es un zip de la variante full sin EFS: publica las otras variantes con "
            "`rayito image publish`"
        ),
        "needs_confirmation": (
            "preparar la cuenta crea recursos con coste: confírmalo con --yes (o desde una "
            "terminal)"
        ),
        "stack_blocked": (
            "la pila {name} está en {status}: bórrala (aws cloudformation delete-stack "
            "--stack-name {name}), espera a que desaparezca y vuelve a ejecutar `rayito setup`"
        ),
        "stack_incomplete": (
            "la pila {name} no tiene la salida {output}: no es la plantilla {template} de Rayito"
        ),
        "release_download": "no se pudo descargar {asset} de la release {release}: {reason}",
        "checksum_missing": "SHA256SUMS de la release {release} no lista {asset}: no se publica",
        "checksum_mismatch": (
            "el sha256 de {asset} no coincide con SHA256SUMS de la release {release}: no se publica"
        ),
        "image_build_failed": (
            "la imagen {name} no quedó lanzable (versión {imageVersion}, {state}): vuelve a "
            "ejecutar `rayito setup`"
        ),
    }
)
#: Código de salida de cada error (diseño D7): 2 uso o entorno, 1 fallo.
ERROR_EXIT_CODES: Final[Mapping[ErrorCode, int]] = MappingProxyType(
    {
        "unsupported_region": 2,
        "bucket_unavailable": 2,
        "bucket_region": 2,
        "bucket_taken": 2,
        "artifact_stack_conflict": 2,
        "artifact_missing": 2,
        "artifact_variant": 2,
        "needs_confirmation": 2,
        "stack_blocked": 1,
        "stack_incomplete": 1,
        "release_download": 1,
        "checksum_missing": 1,
        "checksum_mismatch": 1,
        "image_build_failed": 1,
    }
)
#: Líneas de progreso de `apply`.
PROGRESS_MESSAGES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "bucketCreate": "creando el bucket {bucket} con la pila {name}",
        "bucketWait": "esperando a la pila {name} del bucket ({status})",
        "bucketReady": "bucket {bucket} listo (pila {name})",
        "stackCreate": "creando la pila de IAM {name}",
        "stackUpdate": "actualizando la pila de IAM {name}",
        "stackWait": "esperando a la pila de IAM {name} ({status})",
        "stackReady": "pila de IAM {name} lista",
        "download": "descargando {asset} de {release}",
        "verified": "sha256 de {asset} verificado contra SHA256SUMS: {sha256}",
        "imageWait": "esperando el build en curso de {name} (versión {imageVersion})",
        "imageReady": "imagen {name} {imageVersion} lanzable",
    }
)
#: Motivo de un `release_download` fallido; nunca incluye la URL.
DOWNLOAD_REASONS: Final[Mapping[str, str]] = MappingProxyType(
    {"http": "HTTP {status}", "timeout": "tiempo agotado", "network": "error de red"}
)
ACCOUNT_SETUP_HINT: Final = (
    "si la cuenta o la región aún no están preparadas, ejecuta `rayito setup`"
)
#: La pista cuando falta `rayito-base-caps`: `rayito setup` sólo la publica
#: con `--caps-image` (otra versión de imagen que cuesta, ADR-034 D9).
CAPS_IMAGE_SETUP_HINT: Final = (
    "rayito setup no publica rayito-base-caps por defecto porque cuesta: ejecuta "
    "`rayito setup --caps-image`"
)
HINT_TEMPLATE: Final = "{message} ({hint})"


class AccountSetupException(Exception):
    """Error cerrado de `rayito setup`: su `code` fija el mensaje (con
    `ERROR_MESSAGES`) y el código de salida. No es un `SandboxException`:
    nada de esto es atribuible a un sandbox. El mensaje nunca lleva una URL,
    un token ni el texto crudo de AWS."""

    def __init__(self, code: ErrorCode, **fields: str) -> None:
        super().__init__(ERROR_MESSAGES[code].format(**fields))
        self.code: ErrorCode = code

    @property
    def exit_code(self) -> int:
        return ERROR_EXIT_CODES[self.code]


@dataclass(frozen=True)
class SetupRequest:
    """Lo que pide `rayito setup`. `caps_image` (por defecto `False`) añade el
    paso `capsImage`: publicar `rayito-base-caps`, la base del agente en
    MicroVM, desde el mismo zip que `rayito-base`. Sin él (sin
    `--caps-image`) el paso es `skip` y no hace ninguna llamada: publicar
    otra imagen cuesta, así que es opcional.

    Coste y activación
    -------------------
    Activa: `caps_image=True` / `rayito setup --caps-image` (con `--dry-run`
        sólo lee). Apagada por defecto: sin ella no se lee ni se publica
        `rayito-base-caps`.
    Recursos y llamadas AWS: con `caps_image`, `GetMicrovmImage` y
        `ListMicrovmImageVersions` de `rayito-base-caps` en el plan y, si
        falta, `CreateMicrovmImage`/`UpdateMicrovmImage` con
        `additionalOsCapabilities=["ALL"]` desde el zip ya subido (§4).
    Coste aproximado: otra versión de imagen publicada, ≈ $0,04 por semana
        ($0,08 por GB-mes de snapshot, mínimo una semana; us-east-1,
        consultado 2026-10-22, https://aws.amazon.com/lambda/pricing/).
    IAM: las mismas de publicar `rayito-base` (`infra/iam.yaml`).
    Cómo apagarla: no pases `caps_image` (por defecto `False`) ni `--caps-image`.
    Ejemplo:
        SetupRequest(region="us-east-1", bucket=None, environment_bucket=None,
                     artifact=None, sdk_version="0.17.0", caps_image=True)
    """

    region: str
    bucket: str | None
    environment_bucket: str | None
    artifact: Path | None
    sdk_version: str
    caps_image: bool = False


@dataclass(frozen=True)
class BucketCandidate:
    name: str
    source: BucketSource


@dataclass(frozen=True)
class BucketProbe:
    """Resultado de un `HeadBucket` (`found` 200, `missing` 404, `forbidden`
    403: existe pero estas credenciales no lo ven; `AWS_API_NOTES.md`,
    `HeadBucket`); `region` es `None` salvo que `found` traiga `BucketRegion`."""

    state: BucketState
    region: str | None


@dataclass(frozen=True)
class ImageVersionItem:
    image_version: str
    state: str
    status: str
    created_at: datetime


@dataclass(frozen=True)
class ImageObservation:
    """`GetMicrovmImage` y `ListMicrovmImageVersions` de `rayito-base`;
    `state` es `None` si la imagen no existe."""

    name: str
    image_arn: str
    state: str | None
    versions: tuple[ImageVersionItem, ...]


@dataclass(frozen=True)
class SetupStep:
    resource: ResourceKind
    name: str
    action: SetupAction
    source: BucketSource | ImageSource | None = None
    status: str | None = None
    image_version: str | None = None
    release: str | None = None
    previous_bucket: str | None = None

    def __post_init__(self) -> None:
        if self.action not in ALLOWED_ACTIONS[self.resource]:
            raise ValueError(f"acción {self.action!r} no admitida para {self.resource!r}")

    def as_json(self) -> dict[str, object]:
        return {
            "resource": self.resource,
            "name": self.name,
            "action": self.action,
            "source": self.source,
            "status": self.status,
            "imageVersion": self.image_version,
            "release": self.release,
            "previousBucket": self.previous_bucket,
        }


@dataclass(frozen=True)
class SetupPlan:
    region: str
    account: str
    steps: tuple[SetupStep, ...]
    cost: tuple[str, ...]
    artifact: Path | None
    image_arn: str
    stack_status: str | None
    caps_image_arn: str | None = None

    def __post_init__(self) -> None:
        if tuple(step.resource for step in self.steps) != RESOURCE_ORDER:
            raise ValueError(f"el plan lleva exactamente {RESOURCE_ORDER} en ese orden")

    @property
    def ready(self) -> bool:
        return all(step.action in READY_ACTIONS for step in self.steps)

    @property
    def needs_confirmation(self) -> bool:
        return any(step.action in WRITING_ACTIONS for step in self.steps)

    @property
    def bucket(self) -> str:
        return self.steps[0].name

    @property
    def stack_step(self) -> SetupStep:
        return self.steps[1]

    @property
    def image_step(self) -> SetupStep:
        return self.steps[2]

    @property
    def caps_step(self) -> SetupStep:
        return self.steps[3]


@dataclass(frozen=True)
class SetupResult:
    """Lo que dejó `apply`. `caps_image_arn`/`caps_image_version` son los de
    `rayito-base-caps`, o `None` sin `--caps-image`."""

    bucket: str
    image_arn: str
    image_version: str
    artifact_uploaded: bool | None
    caps_image_arn: str | None = None
    caps_image_version: str | None = None

    def as_json(self) -> dict[str, object]:
        return {
            "bucket": self.bucket,
            "imageArn": self.image_arn,
            "imageVersion": self.image_version,
            "artifactUploaded": self.artifact_uploaded,
            "capsImageArn": self.caps_image_arn,
            "capsImageVersion": self.caps_image_version,
        }


@dataclass(frozen=True)
class DownloadedRelease:
    checksums_text: str
    artifact: Path
    artifact_sha256: str


@dataclass(frozen=True)
class PublishedImage:
    image_arn: str
    image_version: str
    artifact_uploaded: bool


@dataclass(frozen=True)
class ImageGateResult:
    launchable: bool
    version_state: str


def require_supported_region(region: str) -> None:
    if region not in SUPPORTED_REGIONS:
        raise AccountSetupException(
            "unsupported_region", region=region, regions=", ".join(SUPPORTED_REGIONS)
        )


def release_tag(sdk_version: str) -> str:
    return f"{RELEASE_TAG_PREFIX}{sdk_version}"


def release_asset_url(tag: str, asset: str) -> str:
    return RELEASE_DOWNLOAD_URL_TEMPLATE.format(tag=tag, asset=asset)


def cdk_bucket_name(account: str, region: str) -> str:
    return CDK_ASSETS_BUCKET_TEMPLATE.format(
        qualifier=CDK_BOOTSTRAP_QUALIFIER, account=account, region=region
    )


def with_setup_hint(message: str) -> str:
    return HINT_TEMPLATE.format(message=message, hint=ACCOUNT_SETUP_HINT)


def with_base_image_hint(message: str, base: str) -> str:
    """`message` con la pista de la imagen base que falta: para
    `rayito-base-caps`, `rayito setup --caps-image`; para cualquier otra, la de
    `rayito setup`."""
    hint = CAPS_IMAGE_SETUP_HINT if base == CAPS_IMAGE_NAME else ACCOUNT_SETUP_HINT
    return HINT_TEMPLATE.format(message=message, hint=hint)


def default_artifact_bucket_name(account: str, region: str) -> str:
    return DEFAULT_ARTIFACT_BUCKET_TEMPLATE.format(account=account, region=region)


def bucket_candidates(
    request: SetupRequest, stack: StackObservation, account: str
) -> tuple[BucketCandidate, ...]:
    """Los buckets reutilizables, en orden de prueba: el nombre explícito
    (`--bucket`, luego `RAYITO_BUCKET`) como único candidato o, sin él, el
    `ArtifactBucket` de la pila de IAM si existe y el del bootstrap de CDK."""
    if request.bucket:
        return (BucketCandidate(request.bucket, "argument"),)
    if request.environment_bucket:
        return (BucketCandidate(request.environment_bucket, "environment"),)
    candidates: list[BucketCandidate] = []
    from_stack = stack.parameters.get(ARTIFACT_BUCKET_PARAMETER)
    if stack.status is not None and from_stack:
        candidates.append(BucketCandidate(from_stack, "iamStack"))
    candidates.append(BucketCandidate(cdk_bucket_name(account, request.region), "cdkBootstrap"))
    return tuple(candidates)


def decide_bucket(candidate: BucketCandidate, probe: BucketProbe, region: str) -> SetupStep | None:
    """`reuse` si el candidato está en la región; error si está en otra o si
    no se puede ver y Rayito lo necesita. `None`: pasa al siguiente candidato
    o, si no quedan, a la fase de creación (diseño D2)."""
    if probe.state == "found":
        if probe.region is not None and probe.region != region:
            raise AccountSetupException(
                "bucket_region", bucket=candidate.name, bucketRegion=probe.region, region=region
            )
        return SetupStep("bucket", candidate.name, "reuse", source=candidate.source)
    if probe.state == "forbidden" and candidate.source != "cdkBootstrap":
        raise AccountSetupException(
            "bucket_unavailable",
            bucket=candidate.name,
            source=BUCKET_SOURCE_LABELS[candidate.source],
        )
    return None


def creation_target(request: SetupRequest, account: str) -> BucketCandidate:
    """El bucket que se crea: el nombre explícito o el nombre por defecto."""
    explicit = request.bucket or request.environment_bucket
    if explicit:
        return BucketCandidate(explicit, "argument" if request.bucket else "environment")
    return BucketCandidate(default_artifact_bucket_name(account, request.region), "rayitoDefault")


def needs_default_probe(target: BucketCandidate, artifact_stack: StackObservation) -> bool:
    """El nombre por defecto se sonda sólo si ninguna pila lo gestiona: un
    bucket con ese nombre que no creó la pila nunca se adopta."""
    return target.source == "rayitoDefault" and artifact_stack.status is None


def plan_bucket_creation(
    target: BucketCandidate,
    artifact_stack: StackObservation,
    probe: BucketProbe | None,
    region: str,
) -> SetupStep:
    """El paso del bucket cuando nada se reutilizó, según la pila
    `rayito-m0-artifacts`: `reuse` o `wait` el bucket que ya gestiona,
    `create` el del objetivo, o el error que corresponda."""
    condition = classify_stack(artifact_stack)
    if condition == "blocked":
        raise AccountSetupException(
            "stack_blocked", name=artifact_stack.name, status=str(artifact_stack.status)
        )
    if condition == "missing":
        return _create_unless_taken(target, probe)
    if condition == "usable":
        managed = stack_output(artifact_stack, BUCKET_NAME_OUTPUT_KEY, ARTIFACT_TEMPLATE_PATH)
        action: SetupAction = "reuse"
    else:
        managed = artifact_stack.parameters.get(BUCKET_NAME_PARAMETER, target.name)
        action = "wait"
    _require_same_bucket(target, managed)
    return SetupStep(
        "bucket", managed, action, source="artifactStack", status=artifact_stack.status
    )


def _create_unless_taken(target: BucketCandidate, probe: BucketProbe | None) -> SetupStep:
    if target.source in EXPLICIT_BUCKET_SOURCES:
        return SetupStep("bucket", target.name, "create", source=target.source)
    if probe is None:
        raise ValueError("el nombre por defecto se sonda antes de crearlo")
    if probe.state != "missing":
        raise AccountSetupException("bucket_taken", bucket=target.name, stack=ARTIFACT_STACK_NAME)
    return SetupStep("bucket", target.name, "create", source=target.source)


def _require_same_bucket(target: BucketCandidate, managed: str) -> None:
    if target.source in EXPLICIT_BUCKET_SOURCES and target.name != managed:
        raise AccountSetupException(
            "artifact_stack_conflict",
            stack=ARTIFACT_STACK_NAME,
            existing=managed,
            bucket=target.name,
        )


def stack_output(observation: StackObservation, key: str, template: str) -> str:
    """La salida `key` de una pila de Rayito; sin ella la pila no es la
    plantilla `template`."""
    value = observation.outputs.get(key)
    if not value:
        raise AccountSetupException(
            "stack_incomplete", name=observation.name, output=key, template=template
        )
    return value


def plan_stack(stack: StackObservation, bucket: str, template_fingerprint: str) -> SetupStep:
    """`create` sin pila, `wait` si está en curso y, con la pila lista,
    `reuse` sólo si lleva el bucket del plan y la plantilla de este SDK
    (`template_fingerprint`, el sha256 de su `infra/iam.yaml`); si no,
    `update`. `previous_bucket` sólo se rellena si cambia el bucket: con el
    mismo bucket, el `update` sólo pone al día la plantilla."""
    condition = classify_stack(stack)
    if condition == "missing":
        return SetupStep("iamStack", IAM_STACK_NAME, "create")
    if condition == "blocked":
        raise AccountSetupException("stack_blocked", name=stack.name, status=str(stack.status))
    if condition == "in_progress":
        return SetupStep("iamStack", IAM_STACK_NAME, "wait", status=stack.status)
    stack_output(stack, BUILD_ROLE_OUTPUT_KEY, IAM_TEMPLATE_PATH)
    previous = stack.parameters.get(ARTIFACT_BUCKET_PARAMETER)
    current_template = stack.parameters.get(TEMPLATE_FINGERPRINT_PARAMETER) == template_fingerprint
    if previous == bucket and current_template:
        return SetupStep("iamStack", IAM_STACK_NAME, "reuse", status=stack.status)
    return SetupStep(
        "iamStack",
        IAM_STACK_NAME,
        "update",
        status=stack.status,
        previous_bucket=None if previous == bucket else previous,
    )


def newest(versions: Sequence[ImageVersionItem]) -> ImageVersionItem:
    return max(versions, key=lambda item: item.created_at)


def building_versions(image: ImageObservation) -> list[ImageVersionItem]:
    return [item for item in image.versions if item.state in BUILDING_VERSION_STATES]


def launchable_versions(image: ImageObservation) -> list[ImageVersionItem]:
    return [
        item
        for item in image.versions
        if item.state == SUCCESSFUL_VERSION_STATE and item.status == ACTIVE_VERSION_STATUS
    ]


def classify_image(image: ImageObservation) -> ImageCondition:
    if image.state is None:
        return "missing"
    if building_versions(image):
        return "building"
    if image.state in LAUNCHABLE_IMAGE_STATES and launchable_versions(image):
        return "launchable"
    return "unlaunchable"


def plan_image(
    image: ImageObservation,
    request: SetupRequest,
    resource: ResourceKind = "baseImage",
    name: str = BASE_IMAGE_NAME,
) -> SetupStep:
    """`wait` si hay un build en curso, `reuse` si hay una versión lanzable
    y, si no, `publish` desde `--artifact` o desde la release del SDK. Vale
    para `rayito-base` y para `rayito-base-caps` (`resource`/`name`)."""
    condition = classify_image(image)
    if condition == "building":
        version = newest(building_versions(image)).image_version
        return SetupStep(resource, name, "wait", image_version=version)
    if condition == "launchable":
        version = newest(launchable_versions(image)).image_version
        return SetupStep(resource, name, "reuse", image_version=version)
    if request.artifact is not None:
        return SetupStep(resource, name, "publish", source="artifact")
    return SetupStep(
        resource,
        name,
        "publish",
        source="release",
        release=release_tag(request.sdk_version),
    )


def plan_caps_image(caps: ImageObservation | None, request: SetupRequest) -> SetupStep:
    """El paso `capsImage`: `skip` sin `caps_image` (y entonces no se observó
    nada); si no, la misma decisión que la imagen base."""
    if not request.caps_image or caps is None:
        return SetupStep("capsImage", CAPS_IMAGE_NAME, "skip")
    return plan_image(caps, request, "capsImage", CAPS_IMAGE_NAME)


def cost_lines(steps: Sequence[SetupStep]) -> tuple[str, ...]:
    """Sólo el coste de lo que cuesta: el bucket al crearse (primero), la pila
    de IAM al crear o actualizar; la imagen y el zip al publicar `rayito-base`;
    la imagen con capacidades al publicar `rayito-base-caps` (y el zip, si
    sólo se publica ésa)."""
    lines: list[str] = []
    by_resource = {step.resource: step for step in steps}
    if by_resource["bucket"].action == "create":
        lines.append(COST_TEXTS["bucket"].format(days=ARTIFACT_EXPIRATION_DAYS))
    if by_resource["iamStack"].action in {"create", "update"}:
        lines.append(COST_TEXTS["iamStack"])
    base_publishes = by_resource["baseImage"].action == "publish"
    if base_publishes:
        lines.extend((COST_TEXTS["image"], COST_TEXTS["artifact"]))
    if by_resource["capsImage"].action == "publish":
        lines.append(COST_TEXTS["capsImage"])
        if not base_publishes:
            lines.append(COST_TEXTS["artifact"])
    return tuple(lines)


def plan_setup(
    request: SetupRequest,
    account: str,
    stack: StackObservation,
    bucket_step: SetupStep,
    image: ImageObservation,
    caps: ImageObservation | None = None,
    *,
    iam_template_fingerprint: str,
) -> SetupPlan:
    """El plan de los cuatro pasos. `caps` es la observación de
    `rayito-base-caps`, o `None` si `caps_image` está apagado;
    `iam_template_fingerprint`, el sha256 del `infra/iam.yaml` de este SDK."""
    require_supported_region(request.region)
    steps = (
        bucket_step,
        plan_stack(stack, bucket_step.name, iam_template_fingerprint),
        plan_image(image, request),
        plan_caps_image(caps, request),
    )
    return SetupPlan(
        region=request.region,
        account=account,
        steps=steps,
        cost=cost_lines(steps),
        artifact=request.artifact,
        image_arn=image.image_arn,
        stack_status=stack.status,
        caps_image_arn=None if caps is None or not request.caps_image else caps.image_arn,
    )


def applied_steps(plan: SetupPlan, result: SetupResult | None) -> tuple[SetupStep, ...]:
    """Los pasos del plan tras `apply`: un `publish` de imagen lleva la
    versión que acaba de publicar (`result`), que el plan aún no conocía.
    Sin `result` (dry-run o un fallo), los pasos del plan tal cual."""
    if result is None:
        return plan.steps
    published: Mapping[ResourceKind, str | None] = {
        "baseImage": result.image_version,
        "capsImage": result.caps_image_version,
    }
    return tuple(
        replace(step, image_version=published[step.resource])
        if step.action == "publish" and published.get(step.resource) is not None
        else step
        for step in plan.steps
    )


def parse_checksums(text: str) -> dict[str, str]:
    """`SHA256SUMS` en formato `sha256sum`: nombre -> sha256. Las líneas mal
    formadas se ignoran."""
    checksums: dict[str, str] = {}
    for line in text.splitlines():
        match = CHECKSUM_LINE_PATTERN.fullmatch(line)
        if match is not None:
            checksums[match.group("name")] = match.group("sha256")
    return checksums


def checksum_for(text: str, asset: str) -> str | None:
    return parse_checksums(text).get(asset)


def verify_checksum(release: DownloadedRelease, tag: str) -> None:
    """El sha256 del zip, calculado al descargarlo, debe ser el de su
    entrada en `SHA256SUMS`. Prueba que no está truncado ni corrupto, no su
    autenticidad (ver el diseño D5)."""
    expected = checksum_for(release.checksums_text, IMAGE_ASSET_NAME)
    if expected is None:
        raise AccountSetupException("checksum_missing", release=tag, asset=IMAGE_ASSET_NAME)
    if expected != release.artifact_sha256:
        raise AccountSetupException("checksum_mismatch", release=tag, asset=IMAGE_ASSET_NAME)


def progress_message(key: str, **fields: Any) -> str:
    return PROGRESS_MESSAGES[key].format(**fields)
