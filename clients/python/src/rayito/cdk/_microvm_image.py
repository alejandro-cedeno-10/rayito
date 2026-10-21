"""`RayitoMicrovmImage` y `MicrovmImageArtifact` (ADR-032 D9): una imagen de
Lambda MicroVMs con `rayd`, publicada por CloudFormation
(`AWS::Lambda::MicrovmImage`) con la misma configuración que `rayito image
publish`. Los MicroVMs los sigue arrancando el SDK (`Sandbox.create(template=
"<nombre>")`): no hay recurso de CloudFormation para un MicroVM en marcha.

La síntesis no llama a AWS: el artefacto es un fichero explícito (con su
sha256) o la release descargada antes de construir el stack
(`download_release`), la versión de la imagen base es una prop obligatoria y
el rol de build lo crea el constructo. Los ids de cada hijo son los de
`testdata/agentcore-cdk/contract.json` (`constructIds.RayitoMicrovmImage`).
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3_assets as s3_assets
from constructs import Construct

from rayito._account_setup._domain import (
    CHECKSUMS_ASSET_NAME,
    IMAGE_ASSET_NAME,
    AccountSetupException,
    DownloadedRelease,
    release_tag,
    verify_checksum,
)
from rayito._account_setup._ports import ReleaseArtifactSource
from rayito._account_setup._release import GithubReleaseSource
from rayito._agentcore._domain import validate_tags
from rayito._images import DEFAULT_MEMORY_MIB
from rayito._images_publish_domain import (
    ALL_OS_CAPABILITIES,
    ALLOWED_MOUNT_BUCKETS_ENV_VAR,
    BASELINE_MEMORY_ENV_VAR,
    DEFAULT_IMAGE_NAME,
    ENV_KEY_PATTERN,
    MOUNT_BUCKETS_SEPARATOR,
    ImageBuildSettings,
    desired_configuration,
    log_group_for,
    require_efs_combination,
    size_names_problem,
    sized_settings,
)
from rayito._version import __version__
from rayito.cdk._environment import (
    IMAGE_LOG_RETENTION,
    MICROVM_IMAGE_COMPONENT,
    apply_tags,
    stack_environment,
)
from rayito.cdk._errors import RayitoCdkException
from rayito.cdk._image_properties import camel_case, cfn_image_properties
from rayito.cli._artifact import VARIANTS, artifact_sha256, marker_has_efs, marker_variant
from rayito.exceptions import AgentCoreException

CONSTRUCT_NAME: Final = "RayitoMicrovmImage"
#: `Name` de `AWS::Lambda::MicrovmImage` (AWS_API_NOTES.md, «Constructos CDK»).
IMAGE_NAME_PATTERN: Final = re.compile(r"[a-zA-Z0-9-_]+")
IMAGE_NAME_MAX_LENGTH: Final = 64
#: Los nombres de `rayito setup` y `rayito image publish` (`rayito-base*`).
RESERVED_IMAGE_NAME_PREFIX: Final = DEFAULT_IMAGE_NAME
IMAGE_DESCRIPTION_TEMPLATE: Final = "Rayito image {name} (rayito.microvm-image/1+cdk)"
LATEST_BASE_IMAGE_VERSION: Final = "latest"
DEFAULT_VARIANT: Final = "full"
#: `EnvironmentVariables` del esquema: hasta 50, clave de 1 a 256 y valor de
#: hasta 4096 caracteres.
MAX_ENVIRONMENT_VARIABLES: Final = 50
ENVIRONMENT_KEY_MAX_CHARS: Final = 256
ENVIRONMENT_VALUE_MAX_CHARS: Final = 4096
#: Un nombre de bucket de S3 (sin puntos dobles ni mayúsculas).
BUCKET_NAME_PATTERN: Final = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
#: Dónde guarda `download_release` la release (bajo el directorio de trabajo).
RELEASES_DIRECTORY: Final = Path(".rayito") / "releases"
LAMBDA_SERVICE: Final = "lambda.amazonaws.com"
SOURCE_ACCOUNT_CONDITION: Final = "aws:SourceAccount"
BUILD_POLICY_NAME: Final = "build"
LOG_ACTIONS: Final = ("logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents")
S3_OBJECT_ARN_TEMPLATE: Final = "arn:{partition}:s3:::{bucket}/{key}"
ARTIFACT_URI_TEMPLATE: Final = "s3://{bucket}/{key}"
ARTIFACT_FIELD_LABEL: Final = "artifact"
HAS_MARKER: Final = "lleva"
LACKS_MARKER: Final = "no lleva"


def _ignore_progress(_message: str) -> None:
    return None


#: Cómo se descarga la release; los tests la sustituyen por una sin red.
release_source_factory: Callable[[], ReleaseArtifactSource] = GithubReleaseSource


class MicrovmImageArtifact:
    """El zip de la imagen (`rayito-image.zip`, el de `make image-zip` o el de
    una release de Rayito) que `RayitoMicrovmImage` publica, comprobado en
    local antes de sintetizar.

    Coste y activación
    -------------------
    Activa: nada por sí mismo; `download_release` descarga por HTTPS dos
        ficheros públicos de GitHub (sin AWS) sólo si no están ya en su
        directorio.
    Recursos y llamadas AWS: ninguno.
    Coste aproximado: ninguno.
    IAM: ninguna.
    Cómo apagarla: usar `from_file` con un zip propio.
    Ejemplo:
        artifact = MicrovmImageArtifact.download_release()
    """

    def __init__(self, path: Path, sha256: str) -> None:
        self._path = path
        self._sha256 = sha256

    @property
    def path(self) -> Path:
        return self._path

    @property
    def sha256(self) -> str:
        return self._sha256

    @staticmethod
    def from_file(
        path: str | os.PathLike[str], *, sha256: str | None = None
    ) -> MicrovmImageArtifact:
        """Un zip local: `artifact_not_found` si no existe y
        `artifact_checksum_mismatch` si `sha256` no es el suyo."""
        artifact = Path(path)
        if not artifact.is_file():
            raise RayitoCdkException("artifact_not_found", path=artifact)
        digest = artifact_sha256(artifact)
        if sha256 is not None and sha256.lower() != digest:
            raise RayitoCdkException("artifact_checksum_mismatch", path=artifact)
        return MicrovmImageArtifact(artifact, digest)

    @staticmethod
    def download_release(
        *, version: str | None = None, directory: str | os.PathLike[str] | None = None
    ) -> MicrovmImageArtifact:
        """`rayito-image.zip` de la release `rayd-v<version>` (por defecto la
        del SDK, como `rayito setup`), verificado contra su `SHA256SUMS`. Se
        guarda con `SHA256SUMS` en `directory` (por defecto
        `.rayito/releases/<release>/`) y la siguiente síntesis lo reutiliza sin
        red, verificándolo otra vez. Se llama antes de construir el stack."""
        tag = release_tag(version or __version__)
        target = Path(directory) if directory is not None else RELEASES_DIRECTORY / tag
        artifact = target / IMAGE_ASSET_NAME
        checksums = target / CHECKSUMS_ASSET_NAME
        if artifact.is_file() and checksums.is_file():
            release = DownloadedRelease(
                checksums_text=checksums.read_text(encoding="utf-8"),
                artifact=artifact,
                artifact_sha256=artifact_sha256(artifact),
            )
            _verify(release, tag)
            return MicrovmImageArtifact(artifact, release.artifact_sha256)
        target.mkdir(parents=True, exist_ok=True)
        try:
            release = release_source_factory().fetch(tag, target, _ignore_progress)
        except AccountSetupException as exc:
            raise RayitoCdkException(
                "release_download_failed",
                asset=IMAGE_ASSET_NAME,
                release=tag,
                reason=str(exc).rpartition(": ")[2],
            ) from None
        try:
            _verify(release, tag)
        except RayitoCdkException:
            release.artifact.unlink(missing_ok=True)
            raise
        checksums.write_text(release.checksums_text, encoding="utf-8")
        return MicrovmImageArtifact(release.artifact, release.artifact_sha256)


def _verify(release: DownloadedRelease, tag: str) -> None:
    try:
        verify_checksum(release, tag)
    except AccountSetupException:
        raise RayitoCdkException("artifact_checksum_mismatch", path=release.artifact) from None


def _option_invalid(option: str, reason: str) -> RayitoCdkException:
    return RayitoCdkException("option_invalid", option=option, reason=reason)


def _check_names(name: str, sizes: Sequence[str]) -> None:
    if name.startswith(RESERVED_IMAGE_NAME_PREFIX):
        raise RayitoCdkException(
            "image_name_reserved", name=name, prefix=RESERVED_IMAGE_NAME_PREFIX
        )
    for candidate in (name, *(f"{name}-{size}" for size in sizes)):
        if len(candidate) > IMAGE_NAME_MAX_LENGTH or not IMAGE_NAME_PATTERN.fullmatch(candidate):
            raise RayitoCdkException(
                "image_name_invalid", name=candidate, max=IMAGE_NAME_MAX_LENGTH
            )


def _check_options(
    *,
    base_image_version: str,
    variant: str,
    with_efs: bool,
    os_capabilities: bool,
    memory_mib: int,
    sizes: Sequence[str],
) -> None:
    if not base_image_version or base_image_version == LATEST_BASE_IMAGE_VERSION:
        raise _option_invalid(
            "base_image_version",
            "la versión mayor de la imagen base gestionada (p. ej. 1); latest leería AWS al "
            "sintetizar",
        )
    if variant not in VARIANTS:
        raise _option_invalid("variant", f"admitidas: {', '.join(VARIANTS)}")
    if with_efs:
        try:
            require_efs_combination(variant, ALL_OS_CAPABILITIES if os_capabilities else None)
        except ValueError as exc:
            raise _option_invalid("with_efs", str(exc)) from None
    problem = size_names_problem(sizes)
    if problem is not None:
        raise _option_invalid("sizes", problem)
    if isinstance(memory_mib, bool) or not isinstance(memory_mib, int) or memory_mib <= 0:
        raise _option_invalid("memory_mib", "un número entero de MiB")
    if sizes and memory_mib != DEFAULT_MEMORY_MIB:
        raise _option_invalid(
            "memory_mib",
            f"con sizes la imagen sin sufijo es el baseline de {DEFAULT_MEMORY_MIB} MiB",
        )


def image_environment(
    environment: Mapping[str, str], allowed_mount_buckets: Sequence[str]
) -> dict[str, str]:
    """`EnvironmentVariables` de la imagen base: las del usuario y, con
    `allowed_mount_buckets`, `RAYITO_ALLOWED_MOUNT_BUCKETS` (separados por
    comas). Una clave que Rayito escribe no la puede traer el usuario."""
    for key, value in environment.items():
        if key in (ALLOWED_MOUNT_BUCKETS_ENV_VAR, BASELINE_MEMORY_ENV_VAR):
            raise _option_invalid("environment", f"{key} lo pone el constructo")
        if (
            not ENV_KEY_PATTERN.fullmatch(key)
            or len(key) > ENVIRONMENT_KEY_MAX_CHARS
            or len(value) > ENVIRONMENT_VALUE_MAX_CHARS
        ):
            raise _option_invalid(
                "environment",
                f"{key}: clave de letras, dígitos o _ (hasta {ENVIRONMENT_KEY_MAX_CHARS}) y valor "
                f"de hasta {ENVIRONMENT_VALUE_MAX_CHARS} caracteres",
            )
    for bucket in allowed_mount_buckets:
        if not BUCKET_NAME_PATTERN.fullmatch(bucket):
            raise _option_invalid("allowed_mount_buckets", f"{bucket} no es un nombre de bucket")
    merged = dict(environment)
    if allowed_mount_buckets:
        merged[ALLOWED_MOUNT_BUCKETS_ENV_VAR] = MOUNT_BUCKETS_SEPARATOR.join(
            dict.fromkeys(allowed_mount_buckets)
        )
    if len(merged) + 1 > MAX_ENVIRONMENT_VARIABLES:
        raise _option_invalid(
            "environment", f"como mucho {MAX_ENVIRONMENT_VARIABLES - 1} variables"
        )
    return merged


def _check_markers(artifact: MicrovmImageArtifact, variant: str, with_efs: bool) -> None:
    found = marker_variant(artifact.path)
    if found != variant:
        raise RayitoCdkException("artifact_variant_mismatch", found=found, variant=variant)
    has_efs = marker_has_efs(artifact.path)
    if has_efs != with_efs:
        raise RayitoCdkException(
            "artifact_efs_mismatch",
            state=HAS_MARKER if has_efs else LACKS_MARKER,
            withEfs=str(with_efs).lower(),
        )


def _user_tags(tags: Mapping[str, str] | None) -> dict[str, str]:
    try:
        return validate_tags(dict(tags or {}))
    except AgentCoreException as exc:
        raise _option_invalid("tags", str(exc)) from None


class RayitoMicrovmImage(Construct):
    """Una imagen de Lambda MicroVMs con `rayd`, publicada con `cdk deploy`
    (ADR-032 D9): la misma configuración que `rayito image publish` (hooks,
    memoria, arquitectura, capacidades, logs y variables), para la imagen
    `name` y una más por cada tamaño de `sizes` (`<name>-<size>`).

    Sintetiza el asset `Artifact` con el zip (uno para todos los tamaños), el
    rol `BuildRole` (lo asume `lambda.amazonaws.com`; sólo lee ese objeto y
    escribe en los grupos de logs de sus imágenes) y, por imagen, un grupo
    de logs `/rayito/<nombre>` de 14 días y un `AWS::Lambda::MicrovmImage`.
    Valida sin AWS: región concreta, nombre (los `rayito-base*` son de
    `rayito setup`), `base_image_version` (obligatoria; `latest` no), la
    variante y el marcador de EFS del zip, y los tamaños.

    Los sandboxes se crean con el SDK de siempre:
    `Sandbox.create(template="<name>")`. `rayito image publish` rechaza una
    imagen de CDK; `rayito image prune` puede borrar sus versiones antiguas.

    Coste y activación
    -------------------
    Activa: sólo `cdk deploy` de un stack que contiene el constructo;
        sintetizar no llama a AWS ni cuesta nada.
    Recursos y llamadas AWS: el zip en el bucket del bootstrap (queda hasta
        `cdk gc`), un rol de IAM, un grupo de logs y una imagen por tamaño;
        cada cambio de configuración o de artefacto construye una versión
        nueva de cada imagen.
    Coste aproximado: el build de cada versión y su almacenamiento de
        snapshot (≈ $0,04 por semana y versión), más los logs del build.
        Los MicroVMs se cobran al usarlos, como siempre.
    IAM: la ejecución de CloudFormation necesita `lambda:CreateMicrovmImage`,
        `lambda:PassNetworkConnector` sobre el conector gestionado
        `INTERNET_EGRESS` e `iam:PassRole` del rol de build (el rol de
        ejecución por defecto de CDK los tiene).
    Cómo apagarla: quitar el constructo del stack o `cdk destroy` (borra las
        imágenes); `rayito image prune` borra versiones antiguas.
    Ejemplo:
        image = RayitoMicrovmImage(
            stack, "Image", name="team-sandbox",
            artifact=MicrovmImageArtifact.download_release(), base_image_version="1",
        )
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        name: str,
        artifact: MicrovmImageArtifact,
        base_image_version: str,
        variant: str = DEFAULT_VARIANT,
        with_efs: bool = False,
        os_capabilities: bool = False,
        memory_mib: int | None = None,
        sizes: Sequence[str] = (),
        allowed_mount_buckets: Sequence[str] = (),
        environment: Mapping[str, str] | None = None,
        tags: Mapping[str, str] | None = None,
    ) -> None:
        env = stack_environment(scope, CONSTRUCT_NAME)
        size_names = tuple(sizes)
        memory = DEFAULT_MEMORY_MIB if memory_mib is None else memory_mib
        _check_names(name, size_names)
        _check_options(
            base_image_version=base_image_version,
            variant=variant,
            with_efs=with_efs,
            os_capabilities=os_capabilities,
            memory_mib=memory,
            sizes=size_names,
        )
        variables = image_environment(dict(environment or {}), tuple(allowed_mount_buckets))
        user_tags = _user_tags(tags)
        _check_markers(artifact, variant, with_efs)
        super().__init__(scope, id)
        base = ImageBuildSettings(
            image_name=name,
            base_image_version=base_image_version,
            memory_mib=memory,
            os_capabilities=ALL_OS_CAPABILITIES if os_capabilities else None,
            environment_variables=variables,
        )
        settings = {"": base, **{size: sized_settings(base, size) for size in size_names}}
        asset = s3_assets.Asset(self, "Artifact", path=str(artifact.path))
        log_groups = {
            size: logs.LogGroup(
                self,
                f"LogGroup{size.upper()}",
                log_group_name=log_group_for(item.image_name),
                retention=IMAGE_LOG_RETENTION,
                removal_policy=cdk.RemovalPolicy.DESTROY,
            )
            for size, item in settings.items()
        }
        role = iam.Role(
            self,
            "BuildRole",
            assumed_by=iam.ServicePrincipal(
                LAMBDA_SERVICE,
                conditions={"StringEquals": {SOURCE_ACCOUNT_CONDITION: env.account}},
            ).with_session_tags(),
            inline_policies={
                BUILD_POLICY_NAME: iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            actions=["s3:GetObject"],
                            resources=[
                                S3_OBJECT_ARN_TEMPLATE.format(
                                    partition=cdk.Aws.PARTITION,
                                    bucket=asset.s3_bucket_name,
                                    key=asset.s3_object_key,
                                )
                            ],
                        ),
                        iam.PolicyStatement(
                            actions=list(LOG_ACTIONS),
                            resources=[group.log_group_arn for group in log_groups.values()],
                        ),
                    ]
                )
            },
        )
        artifact_uri = ARTIFACT_URI_TEMPLATE.format(
            bucket=asset.s3_bucket_name, key=asset.s3_object_key
        )
        images = {
            size: self._image(
                f"Image{size.upper()}",
                desired_configuration(
                    region=env.region,
                    settings=item,
                    artifact_uri=artifact_uri,
                    build_role_arn=role.role_arn,
                ),
                item.image_name,
                log_groups[size],
            )
            for size, item in settings.items()
        }
        base_image = images.pop("")
        self.image_name: str = name
        self.image_arn: str = base_image.attr_image_arn
        self.latest_active_image_version: str = base_image.attr_latest_active_image_version
        self.build_role_arn: str = role.role_arn
        self.log_group_name: str = log_group_for(name)
        self.size_image_arns: dict[str, str] = {
            size: image.attr_image_arn for size, image in images.items()
        }
        apply_tags(self, MICROVM_IMAGE_COMPONENT, user_tags)

    def _image(
        self,
        construct_id: str,
        configuration: Mapping[str, Any],
        image_name: str,
        log_group: logs.LogGroup,
    ) -> lambda_.CfnMicrovmImage:
        properties = cfn_image_properties(
            configuration,
            name=image_name,
            description=IMAGE_DESCRIPTION_TEMPLATE.format(name=image_name),
        )
        image = lambda_.CfnMicrovmImage(
            self,
            construct_id,
            name=properties["Name"],
            description=properties["Description"],
            base_image_arn=properties["BaseImageArn"],
            base_image_version=properties["BaseImageVersion"],
            build_role_arn=properties["BuildRoleArn"],
            additional_os_capabilities=properties["AdditionalOsCapabilities"],
            egress_network_connectors=properties["EgressNetworkConnectors"],
            code_artifact=cdk.Token.as_any(camel_case(properties["CodeArtifact"])),
            cpu_configurations=cdk.Token.as_any(camel_case(properties["CpuConfigurations"])),
            environment_variables=cdk.Token.as_any(camel_case(properties["EnvironmentVariables"])),
            hooks=cdk.Token.as_any(camel_case(properties["Hooks"])),
            logging=cdk.Token.as_any(camel_case(properties["Logging"])),
            resources=cdk.Token.as_any(camel_case(properties["Resources"])),
        )
        image.node.add_dependency(log_group)
        return image


__all__ = [
    "MicrovmImageArtifact",
    "RayitoMicrovmImage",
    "image_environment",
    "release_source_factory",
]
