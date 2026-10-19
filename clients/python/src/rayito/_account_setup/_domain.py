"""Dominio puro de `rayito setup` (ADR-030): el plan de tres pasos (bucket,
pila de IAM, imagen base), sus reglas, los errores cerrados, las
constantes y los textos del contrato compartido con la CLI de TypeScript
(`testdata/account-setup/contract.json`).

Sólo importa la biblioteca estándar, `rayito._limits` y las constantes de
`rayito._images`: nada de red, ficheros ni AWS. Cada decisión es una
función pura sobre lo observado en la cuenta.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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

ResourceKind = Literal["bucket", "iamStack", "baseImage"]
SetupAction = Literal["create", "update", "reuse", "wait", "publish"]
BucketSource = Literal["argument", "environment", "iamStack", "cdkBootstrap"]
ImageSource = Literal["release", "artifact"]
StackCondition = Literal["missing", "usable", "in_progress", "blocked"]
ImageCondition = Literal["missing", "launchable", "building", "unlaunchable"]
ErrorCode = Literal[
    "unsupported_region",
    "no_bucket",
    "bucket_unavailable",
    "bucket_region",
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

#: Los tres pasos del plan, siempre en este orden.
RESOURCE_ORDER: Final[tuple[ResourceKind, ...]] = ("bucket", "iamStack", "baseImage")
#: Acciones que admite cada recurso (diseño D2).
ALLOWED_ACTIONS: Final[Mapping[ResourceKind, frozenset[SetupAction]]] = MappingProxyType(
    {
        "bucket": frozenset({"reuse"}),
        "iamStack": frozenset({"create", "update", "reuse", "wait"}),
        "baseImage": frozenset({"publish", "reuse", "wait"}),
    }
)
#: Acciones que escriben en la cuenta y por tanto piden confirmación.
WRITING_ACTIONS: Final = frozenset({"create", "update", "publish"})

IAM_STACK_NAME: Final = DEFAULT_IAM_STACK_NAME
IAM_COMPONENT_NAME: Final = "iam"
IAM_CAPABILITIES: Final = ("CAPABILITY_NAMED_IAM",)
ARTIFACT_BUCKET_PARAMETER: Final = "ArtifactBucket"
BASE_IMAGE_NAME: Final = DEFAULT_BASE_IMAGE_NAME
BASE_IMAGE_VARIANT: Final = "full"
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
#: Estados de la pila de IAM con los que sirve tal cual; `UPDATE_ROLLBACK_COMPLETE`
#: volvió a la última plantilla que funcionaba.
USABLE_STACK_STATES: Final = frozenset(
    {"CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE"}
)
IN_PROGRESS_STACK_SUFFIX: Final = "_IN_PROGRESS"
#: Una pila que se está borrando nunca llega a "deployed": esperarla no terminaría.
DELETING_STACK_STATE: Final = "DELETE_IN_PROGRESS"
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
    }
)

#: Cifras ya publicadas, no nuevas: la imagen, `cost.md` fila "Mantener
#: publicada una versión de imagen"; la pila de IAM, `configurar-aws.md`
#: paso 3; el zip en el bucket, `configurar-aws.md` paso 2.
COST_TEXTS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "iamStack": "pila de IAM: gratis",
        "image": "imagen: ≈ $0,04 por semana por versión publicada (mínimo una semana)",
        "artifact": "zip de la imagen en el bucket: céntimos",
    }
)

ERROR_MESSAGES: Final[Mapping[ErrorCode, str]] = MappingProxyType(
    {
        "unsupported_region": "la región {region} no tiene Lambda MicroVMs: usa una de {regions}",
        "no_bucket": (
            "no hay bucket para el artefacto de la imagen: pasa --bucket, exporta RAYITO_BUCKET "
            "o ejecuta `cdk bootstrap` en esta cuenta y región; Rayito no crea buckets"
        ),
        "bucket_unavailable": (
            "el bucket {bucket} ({source}) no existe o no es accesible con estas credenciales"
        ),
        "bucket_region": (
            "el bucket {bucket} está en {bucketRegion}, no en {region}: "
            "el build fallaría con S3_CROSS_REGION_ACCESS_DENIED"
        ),
        "artifact_missing": "{artifact} no existe",
        "artifact_variant": (
            "{artifact} no es un zip de la variante full sin EFS: "
            "publica las otras variantes con `rayito image publish`"
        ),
        "needs_confirmation": (
            "preparar la cuenta crea recursos con coste: confírmalo con --yes "
            "(o desde una terminal)"
        ),
        "stack_blocked": (
            "la pila {name} está en {status}: bórrala (aws cloudformation delete-stack "
            "--stack-name {name}), espera a que desaparezca y vuelve a ejecutar `rayito setup`"
        ),
        "stack_incomplete": (
            "la pila {name} no tiene la salida BuildRoleArn: no es la plantilla infra/iam.yaml "
            "de Rayito"
        ),
        "release_download": ("no se pudo descargar {asset} de la release {release}: {reason}"),
        "checksum_missing": "SHA256SUMS de la release {release} no lista {asset}: no se publica",
        "checksum_mismatch": (
            "el sha256 de {asset} no coincide con SHA256SUMS de la release {release}: no se publica"
        ),
        "image_build_failed": (
            "la imagen {name} no quedó lanzable (versión {imageVersion}, {state}): "
            "vuelve a ejecutar `rayito setup`"
        ),
    }
)
#: Código de salida de cada error (diseño D7): 2 uso o entorno, 1 fallo.
ERROR_EXIT_CODES: Final[Mapping[ErrorCode, int]] = MappingProxyType(
    {
        "unsupported_region": 2,
        "no_bucket": 2,
        "bucket_unavailable": 2,
        "bucket_region": 2,
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
    region: str
    bucket: str | None
    environment_bucket: str | None
    artifact: Path | None
    sdk_version: str


@dataclass(frozen=True)
class StackObservation:
    """Lo que `DescribeStacks` dice de `rayito-m0-iam`; `status` es `None`
    si la pila no existe."""

    name: str
    status: str | None
    outputs: Mapping[str, str]
    parameters: Mapping[str, str]


@dataclass(frozen=True)
class BucketCandidate:
    name: str
    source: BucketSource


@dataclass(frozen=True)
class BucketProbe:
    """Resultado de un `HeadBucket`; `region` es `None` si no existe o si la
    respuesta no trajo `BucketRegion`."""

    exists: bool
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

    def __post_init__(self) -> None:
        if tuple(step.resource for step in self.steps) != RESOURCE_ORDER:
            raise ValueError(f"el plan lleva exactamente {RESOURCE_ORDER} en ese orden")

    @property
    def ready(self) -> bool:
        return all(step.action == "reuse" for step in self.steps)

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


@dataclass(frozen=True)
class SetupResult:
    bucket: str
    image_arn: str
    image_version: str
    artifact_uploaded: bool | None

    def as_json(self) -> dict[str, object]:
        return {
            "bucket": self.bucket,
            "imageArn": self.image_arn,
            "imageVersion": self.image_version,
            "artifactUploaded": self.artifact_uploaded,
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


def choose_bucket_candidate(
    request: SetupRequest, stack: StackObservation, account: str
) -> BucketCandidate:
    """El primer bucket presente: `--bucket`, `RAYITO_BUCKET`, el
    `ArtifactBucket` de la pila de IAM y el del bootstrap de CDK."""
    if request.bucket:
        return BucketCandidate(request.bucket, "argument")
    if request.environment_bucket:
        return BucketCandidate(request.environment_bucket, "environment")
    from_stack = stack.parameters.get(ARTIFACT_BUCKET_PARAMETER)
    if stack.status is not None and from_stack:
        return BucketCandidate(from_stack, "iamStack")
    return BucketCandidate(cdk_bucket_name(account, request.region), "cdkBootstrap")


def decide_bucket(candidate: BucketCandidate, probe: BucketProbe, region: str) -> SetupStep:
    if not probe.exists:
        if candidate.source == "cdkBootstrap":
            raise AccountSetupException("no_bucket")
        raise AccountSetupException(
            "bucket_unavailable",
            bucket=candidate.name,
            source=BUCKET_SOURCE_LABELS[candidate.source],
        )
    if probe.region is not None and probe.region != region:
        raise AccountSetupException(
            "bucket_region", bucket=candidate.name, bucketRegion=probe.region, region=region
        )
    return SetupStep("bucket", candidate.name, "reuse", source=candidate.source)


def classify_stack(stack: StackObservation) -> StackCondition:
    if stack.status is None:
        return "missing"
    if stack.status in USABLE_STACK_STATES:
        return "usable"
    if stack.status == DELETING_STACK_STATE:
        return "blocked"
    if stack.status.endswith(IN_PROGRESS_STACK_SUFFIX):
        return "in_progress"
    return "blocked"


def plan_stack(stack: StackObservation, bucket: str) -> SetupStep:
    condition = classify_stack(stack)
    if condition == "missing":
        return SetupStep("iamStack", IAM_STACK_NAME, "create")
    if condition == "blocked":
        raise AccountSetupException("stack_blocked", name=stack.name, status=str(stack.status))
    if condition == "in_progress":
        return SetupStep("iamStack", IAM_STACK_NAME, "wait", status=stack.status)
    if BUILD_ROLE_OUTPUT_KEY not in stack.outputs:
        raise AccountSetupException("stack_incomplete", name=stack.name)
    previous = stack.parameters.get(ARTIFACT_BUCKET_PARAMETER)
    if previous == bucket:
        return SetupStep("iamStack", IAM_STACK_NAME, "reuse", status=stack.status)
    return SetupStep(
        "iamStack", IAM_STACK_NAME, "update", status=stack.status, previous_bucket=previous
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


def plan_image(image: ImageObservation, request: SetupRequest) -> SetupStep:
    condition = classify_image(image)
    if condition == "building":
        version = newest(building_versions(image)).image_version
        return SetupStep("baseImage", BASE_IMAGE_NAME, "wait", image_version=version)
    if condition == "launchable":
        version = newest(launchable_versions(image)).image_version
        return SetupStep("baseImage", BASE_IMAGE_NAME, "reuse", image_version=version)
    if request.artifact is not None:
        return SetupStep("baseImage", BASE_IMAGE_NAME, "publish", source="artifact")
    return SetupStep(
        "baseImage",
        BASE_IMAGE_NAME,
        "publish",
        source="release",
        release=release_tag(request.sdk_version),
    )


def cost_lines(steps: Sequence[SetupStep]) -> tuple[str, ...]:
    """Sólo el coste de lo que cuesta: la pila de IAM al crear o actualizar;
    la imagen y el zip al publicar."""
    lines: list[str] = []
    by_resource = {step.resource: step for step in steps}
    if by_resource["iamStack"].action in {"create", "update"}:
        lines.append(COST_TEXTS["iamStack"])
    if by_resource["baseImage"].action == "publish":
        lines.extend((COST_TEXTS["image"], COST_TEXTS["artifact"]))
    return tuple(lines)


def plan_setup(
    request: SetupRequest,
    account: str,
    stack: StackObservation,
    candidate: BucketCandidate,
    probe: BucketProbe,
    image: ImageObservation,
) -> SetupPlan:
    require_supported_region(request.region)
    bucket_step = decide_bucket(candidate, probe, request.region)
    steps = (
        bucket_step,
        plan_stack(stack, bucket_step.name),
        plan_image(image, request),
    )
    return SetupPlan(
        region=request.region,
        account=account,
        steps=steps,
        cost=cost_lines(steps),
        artifact=request.artifact,
        image_arn=image.image_arn,
        stack_status=stack.status,
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
