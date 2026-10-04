"""Tests `local` (`make local-e2e`, docs/site/docs/guias/probar-en-local.md):
el SDK contra el `rayd` del contenedor `guest` y contra Floci, sin AWS.

Solo corren con `-m local` y con `RAYITO_LOCAL_GUEST` (el runner de
`dev/local/compose.yaml` la define); sin ella se saltan. Las llamadas a AWS
van a Floci por `AWS_ENDPOINT_URL`, que boto3 lee del entorno, con las
credenciales ficticias del compose. La sesión siembra en Floci la imagen
`rayito-local` (un zip con el Dockerfile de producto en S3: Floci solo
guarda el registro, el guest es quien ejecuta) y el bucket de artefactos.
"""

from __future__ import annotations

import contextlib
import io
import os
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import boto3
import pytest

from rayito import Sandbox
from rayito._aws import LambdaMicrovmsControlPlane
from rayito._transport import TransportSettings
from rayito.exceptions import SandboxNotFoundException

from .guest import LOCAL_GUEST_VAR, GuestAddress, LocalGuestControlPlane

ARTIFACT_BUCKET_VAR: Final = "RAYITO_LOCAL_ARTIFACT_BUCKET"
DEFAULT_ARTIFACT_BUCKET: Final = "rayito-local-artifacts"
LOCAL_IMAGE_NAME: Final = "rayito-local"
#: La imagen gestionada que Floci lista (`list-managed-microvm-images`) y la
#: versión que `make image-publish` pasa siempre (`BASE_IMAGE_VERSION`).
MANAGED_BASE_IMAGE: Final = "al2023-1"
BASE_IMAGE_VERSION: Final = "1"
#: Floci no asume el rol: basta un ARN con forma de rol.
LOCAL_BUILD_ROLE_ARN: Final = "arn:aws:iam::000000000000:role/rayito-local-build"
LOCAL_ARTIFACT_KEY: Final = "rayito-local/image.zip"
TEST_SANDBOX_TIMEOUT_SECONDS: Final = 900
PRODUCT_DOCKERFILE: Final = Path(__file__).resolve().parents[4] / "image" / "Dockerfile"


@dataclass(frozen=True)
class LocalSettings:
    address: GuestAddress
    region: str
    artifact_bucket: str

    @property
    def transport(self) -> TransportSettings:
        return self.address.transport()


def local_enabled() -> bool:
    return bool(os.environ.get(LOCAL_GUEST_VAR))


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if local_enabled():
        return
    skip = pytest.mark.skip(reason=f"entorno local apagado: make local-up define {LOCAL_GUEST_VAR}")
    for item in items:
        if "local" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def local_settings() -> LocalSettings:
    raw = os.environ.get(LOCAL_GUEST_VAR)
    if not raw:
        pytest.fail(f"los tests local requieren {LOCAL_GUEST_VAR} (make local-up)")
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
    return LocalSettings(
        address=GuestAddress.parse(raw),
        region=region,
        artifact_bucket=os.environ.get(ARTIFACT_BUCKET_VAR, DEFAULT_ARTIFACT_BUCKET),
    )


@pytest.fixture(scope="session")
def aws_session(local_settings: LocalSettings) -> boto3.session.Session:
    return boto3.session.Session(region_name=local_settings.region)


@pytest.fixture(scope="session")
def artifact_bucket(local_settings: LocalSettings, aws_session: boto3.session.Session) -> str:
    s3 = aws_session.client("s3")
    with contextlib.suppress(s3.exceptions.BucketAlreadyOwnedByYou):
        s3.create_bucket(Bucket=local_settings.artifact_bucket)
    return local_settings.artifact_bucket


def image_artifact() -> bytes:
    """El zip de la imagen con el Dockerfile de producto: `Template.build`
    lo lee para componer el suyo. Floci no construye nada, así que el
    binario y el sidecar no hacen falta (los ejecuta el guest)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("Dockerfile", PRODUCT_DOCKERFILE.read_text(encoding="utf-8"))
    return buffer.getvalue()


def seed_local_image(session: boto3.session.Session, bucket: str) -> str:
    """El registro de la imagen en Floci, con los mismos parámetros que
    `make image-publish` (`AWS_API_NOTES.md` §4); idempotente."""
    client: Any = session.client("lambda-microvms")
    session.client("s3").put_object(Bucket=bucket, Key=LOCAL_ARTIFACT_KEY, Body=image_artifact())
    try:
        return str(client.get_microvm_image(imageIdentifier=LOCAL_IMAGE_NAME)["imageArn"])
    except client.exceptions.ResourceNotFoundException:
        pass
    region = session.region_name
    created = client.create_microvm_image(
        name=LOCAL_IMAGE_NAME,
        codeArtifact={"uri": f"s3://{bucket}/{LOCAL_ARTIFACT_KEY}"},
        buildRoleArn=LOCAL_BUILD_ROLE_ARN,
        baseImageArn=f"arn:aws:lambda:{region}:aws:microvm-image:{MANAGED_BASE_IMAGE}",
        baseImageVersion=BASE_IMAGE_VERSION,
    )
    return str(created["imageArn"])


@pytest.fixture(scope="session")
def control_plane(
    local_settings: LocalSettings, aws_session: boto3.session.Session
) -> Iterator[LocalGuestControlPlane]:
    inner = LambdaMicrovmsControlPlane.from_session(aws_session, region=local_settings.region)
    plane = LocalGuestControlPlane(inner, local_settings.address)
    yield plane
    live = plane.live_sandbox_id
    if live is not None:
        with contextlib.suppress(SandboxNotFoundException):
            plane.terminate_microvm(live)


@pytest.fixture(scope="session")
def template_arn(aws_session: boto3.session.Session, artifact_bucket: str) -> str:
    return seed_local_image(aws_session, artifact_bucket)


def create_local_sandbox(
    local_settings: LocalSettings,
    control_plane: LocalGuestControlPlane,
    template_arn: str,
    **options: Any,
) -> Sandbox:
    """`create()` como lo llama un test e2e, con el plano y el transporte
    locales; `options` son los kwargs propios de cada test."""
    options.setdefault("timeout", TEST_SANDBOX_TIMEOUT_SECONDS)
    return Sandbox.create(
        template_arn,
        control_plane=control_plane,
        transport=local_settings.transport,
        **options,
    )


@pytest.fixture
def sandbox(
    local_settings: LocalSettings, control_plane: LocalGuestControlPlane, template_arn: str
) -> Iterator[Sandbox]:
    created = create_local_sandbox(local_settings, control_plane, template_arn)
    try:
        yield created
    finally:
        with contextlib.suppress(SandboxNotFoundException):
            created.kill()
