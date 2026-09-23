"""Fixtures de la CLI: clientes boto3 con `Stubber`, un plano de control en
memoria y un `Clients` inyectable con `CliRunner.invoke(app, args, obj=...)`."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import boto3
import pytest
from botocore.config import Config
from botocore.stub import Stubber
from typer.testing import CliRunner

from rayito._aws import PortSpec
from rayito._limits import TERMINAL_STATES
from rayito._models import MicrovmListPage, SandboxInfo, SandboxListItem
from rayito.cli._publish import IMAGE_HOOKS
from rayito.cli._session import SERVICE_NAMES, Clients
from rayito.exceptions import SandboxNotFoundException

REGION = "us-east-1"
ACCOUNT_ID = "123456789012"
IMAGE_NAME = "rayito-base"
IMAGE_ARN = f"arn:aws:lambda:{REGION}:{ACCOUNT_ID}:microvm-image:{IMAGE_NAME}"
BASE_IMAGE_ARN = f"arn:aws:lambda:{REGION}:aws:microvm-image:al2023-1"
STARTED_AT = datetime(2026, 9, 15, 14, 39, 2, tzinfo=UTC)
JWE = "eyJhbGciOiJkaXIiLCJlbmMiOiJBMjU2R0NNIn0..fake.jwe"
USER_ARN = f"arn:aws:iam::{ACCOUNT_ID}:user/maintainer"


def no_retry_client(service: str) -> Any:
    session = boto3.session.Session(
        region_name=REGION, aws_access_key_id="testing", aws_secret_access_key="testing"
    )
    return session.client(
        service, config=Config(retries={"total_max_attempts": 1, "mode": "standard"})
    )


@dataclass
class Stubs:
    """Un `Stubber` activo por servicio; cada test añade sus respuestas."""

    clients: dict[str, Any]
    stubbers: dict[str, Stubber]

    def __getattr__(self, name: str) -> Stubber:
        try:
            return self.stubbers[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


@pytest.fixture
def stubbed_clients() -> Iterator[Stubs]:
    clients = {name: no_retry_client(service) for name, service in SERVICE_NAMES.items()}
    stubbers = {name: Stubber(client) for name, client in clients.items()}
    for stubber in stubbers.values():
        stubber.activate()
    try:
        yield Stubs(clients=clients, stubbers=stubbers)
        for stubber in stubbers.values():
            stubber.assert_no_pending_responses()
    finally:
        for stubber in stubbers.values():
            stubber.deactivate()


@dataclass
class FakeControlPlane:
    """`ControlPlane` en memoria para los comandos `sandbox` y el `doctor`."""

    items: list[SandboxListItem] = field(default_factory=list)
    infos: dict[str, SandboxInfo] = field(default_factory=dict)
    missing: set[str] = field(default_factory=set)
    terminated: list[str] = field(default_factory=list)
    tokens: list[tuple[str, tuple[PortSpec, ...]]] = field(default_factory=list)
    jwe: str = JWE
    region_name: str = REGION

    @property
    def region(self) -> str:
        return self.region_name

    def resolve_template_arn(self, template: str) -> str:
        if template.startswith("arn:"):
            return template
        return f"arn:aws:lambda:{self.region_name}:{ACCOUNT_ID}:microvm-image:{template}"

    def run_microvm(self, request: Any) -> SandboxInfo:
        raise NotImplementedError("la CLI nunca lanza MicroVMs fuera de --launch")

    def get_microvm(self, sandbox_id: str) -> SandboxInfo:
        if sandbox_id in self.missing or sandbox_id not in self.infos:
            raise SandboxNotFoundException(f"no existe {sandbox_id}")
        return self.infos[sandbox_id]

    def list_microvms(
        self,
        *,
        image_arn: str | None = None,
        image_version: str | None = None,
        states: Iterable[str] | None = None,
    ) -> Iterator[SandboxListItem]:
        wanted = frozenset(states) if states is not None else None
        for item in self.items:
            if image_arn is not None and item.template != image_arn:
                continue
            if image_version is not None and item.template_version != image_version:
                continue
            if wanted is None and item.state in TERMINAL_STATES:
                continue
            if wanted is not None and item.state not in wanted:
                continue
            yield item

    def list_microvms_page(
        self,
        *,
        image_arn: str | None,
        image_version: str | None,
        max_results: int,
        next_token: str | None,
    ) -> MicrovmListPage:
        """Una sola página con todos los `items` de la imagen y versión pedidas
        (como AWS, sin filtrar por estado): la CLI nunca lista más de 50."""
        matching = tuple(
            item
            for item in self.items
            if (image_arn is None or item.template == image_arn)
            and (image_version is None or item.template_version == image_version)
        )
        return MicrovmListPage(items=matching[:max_results], next_token=None)

    def terminate_microvm(self, sandbox_id: str) -> bool:
        if sandbox_id in self.missing:
            return False
        self.terminated.append(sandbox_id)
        return True

    def suspend_microvm(self, sandbox_id: str) -> bool:
        return True

    def resume_microvm(self, sandbox_id: str) -> bool:
        return True

    def create_auth_token(self, sandbox_id: str, ports: Sequence[PortSpec]) -> str:
        self.tokens.append((sandbox_id, tuple(ports)))
        return self.jwe


def list_item(
    sandbox_id: str,
    state: str = "RUNNING",
    *,
    template: str = IMAGE_ARN,
    version: str = "1.0",
    started_at: datetime = STARTED_AT,
) -> SandboxListItem:
    return SandboxListItem(
        sandbox_id=sandbox_id,
        state=state,
        template=template,
        template_version=version,
        started_at=started_at,
    )


def sandbox_info(
    sandbox_id: str,
    state: str = "RUNNING",
    *,
    endpoint: str = "abc.lambda-microvm.us-east-1.on.aws",
    template: str = IMAGE_ARN,
    version: str = "1.0",
    started_at: datetime = STARTED_AT,
    metadata: dict[str, str] | None = None,
) -> SandboxInfo:
    return SandboxInfo(
        sandbox_id=sandbox_id,
        state=state,
        endpoint=endpoint,
        template=template,
        template_version=version,
        started_at=started_at,
        maximum_duration_seconds=3600,
        metadata=metadata,
    )


@pytest.fixture
def fake_plane() -> FakeControlPlane:
    return FakeControlPlane()


@pytest.fixture
def clients(stubbed_clients: Stubs, fake_plane: FakeControlPlane) -> Clients:
    session = boto3.session.Session(
        region_name=REGION, aws_access_key_id="testing", aws_secret_access_key="testing"
    )
    return Clients(
        session=session,
        region=REGION,
        clients=dict(stubbed_clients.clients),
        control_plane_override=fake_plane,
        account_id_override=ACCOUNT_ID,
    )


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def image_summary(
    name: str = IMAGE_NAME,
    state: str = "UPDATED",
    *,
    active: str | None = "3.0",
    failed: str | None = None,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "imageArn": f"arn:aws:lambda:{REGION}:{ACCOUNT_ID}:microvm-image:{name}",
        "name": name,
        "state": state,
        "createdAt": STARTED_AT,
    }
    if active is not None:
        summary["latestActiveImageVersion"] = active
    if failed is not None:
        summary["latestFailedImageVersion"] = failed
    return summary


def version_item(
    number: int,
    state: str = "SUCCESSFUL",
    status: str = "ACTIVE",
    *,
    artifact_uri: str = "s3://b/rayito/images/rayd-000000000000.zip",
    base_image_version: str = "1.0",
    image_name: str = IMAGE_NAME,
) -> dict[str, Any]:
    """Un item de `list-microvm-image-versions` con la configuración completa
    que `desired_configuration` compara para el reuse."""
    return {
        "imageVersion": f"{number}.0",
        "state": state,
        "status": status,
        "createdAt": datetime(2026, 9, 1, number, tzinfo=UTC),
        "imageArn": IMAGE_ARN,
        "baseImageArn": BASE_IMAGE_ARN,
        "baseImageVersion": base_image_version,
        "buildRoleArn": f"arn:aws:iam::{ACCOUNT_ID}:role/build",
        "codeArtifact": {"uri": artifact_uri},
        "resources": [{"minimumMemoryInMiB": 2048}],
        "cpuConfigurations": [{"architecture": "ARM_64"}],
        "hooks": IMAGE_HOOKS,
        "logging": {"cloudWatch": {"logGroup": f"/rayito/{image_name}"}},
    }
