"""`EfsVolumes`/`AsyncEfsVolumes` (`m15-efs-volumes`): la comprobación previa
antes de desplegar, los parámetros exactos que llegan a la plantilla,
`volume_store()` y `destroy(delete_file_system=...)`, todo con fakes (sin
AWS)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from rayito import AsyncEfsVolumes, AsyncVolumeStore, EfsVolumes, VolumeStore
from rayito._stacks._model import StackComponent, StackStatus
from rayito._stacks._service import OptionalStacks
from rayito._volumes._vpc import Ec2NetworkInspector
from rayito.exceptions import InvalidArgumentException, VolumeException

from .fake_ec2 import SUBNET_A, SUBNET_B, VPC_ID, FakeEc2Api, subnet
from .fake_efs import client_error
from .fake_stacks import FakeStackProvisioner

STACK = "rayito-efs-volumes"
FILE_SYSTEM_ID = "fs-0123456789abcdef0"
ACCESS_POINT_ARN = (
    "arn:aws:elasticfilesystem:us-east-1:123456789012:access-point/fsap-0123456789abcdef0"
)


@dataclass
class RecordingProvisioner(FakeStackProvisioner):
    """`FakeStackProvisioner` que además guarda los parámetros y devuelve
    las salidas de `infra/efs-volumes.yaml` que la fachada lee."""

    parameters: dict[str, str] = field(default_factory=dict)

    def create(
        self,
        component: StackComponent,
        *,
        stack_name: str,
        template_body: str,
        parameters: dict[str, str],
        tags: dict[str, str],
    ) -> None:
        super().create(
            component,
            stack_name=stack_name,
            template_body=template_body,
            parameters=parameters,
            tags=tags,
        )
        self.parameters = dict(parameters)
        self.stacks[stack_name] = StackStatus(
            name=stack_name, state="CREATE_COMPLETE", outputs={"FileSystemId": FILE_SYSTEM_ID}
        )


@dataclass(eq=False)
class FakeFileSystemApi:
    """`efs` para el borrado del sistema de ficheros conservado: mount
    targets que tardan `drain_polls` sondeos en desaparecer."""

    access_points: list[str] = field(default_factory=lambda: ["fsap-1", "fsap-2"])
    drain_polls: int = 1
    exists: bool = True
    tags: list[dict[str, str]] = field(
        default_factory=lambda: [{"Key": "rayito", "Value": "efs-volumes"}]
    )
    calls: list[str] = field(default_factory=list)

    def describe_file_systems(self, **params: Any) -> dict[str, Any]:
        self.calls.append("describe_file_systems")
        if not self.exists:
            raise client_error("FileSystemNotFound", "no", "DescribeFileSystems")
        return {"FileSystems": [{"FileSystemId": params["FileSystemId"], "Tags": self.tags}]}

    def describe_mount_targets(self, **params: Any) -> dict[str, Any]:
        self.calls.append("describe_mount_targets")
        remaining = self.drain_polls
        self.drain_polls = max(0, self.drain_polls - 1)
        return {"MountTargets": [{"MountTargetId": "fsmt-1"}] if remaining else []}

    def describe_access_points(self, **params: Any) -> dict[str, Any]:
        self.calls.append("describe_access_points")
        return {"AccessPoints": [{"AccessPointId": ap} for ap in self.access_points]}

    def delete_access_point(self, **params: Any) -> dict[str, Any]:
        self.calls.append(f"delete_access_point:{params['AccessPointId']}")
        if params["AccessPointId"] == "fsap-stale":
            raise client_error("AccessPointNotFound", "no", "DeleteAccessPoint")
        return {}

    def delete_file_system(self, **params: Any) -> dict[str, Any]:
        assert params == {"FileSystemId": FILE_SYSTEM_ID}
        self.calls.append("delete_file_system")
        return {}

    def create_access_point(self, **params: Any) -> dict[str, Any]:  # pragma: no cover
        raise AssertionError("destroy never creates")


def facade(
    *,
    ec2: FakeEc2Api | None = None,
    efs: FakeFileSystemApi | None = None,
    provisioner: RecordingProvisioner | None = None,
) -> tuple[EfsVolumes, RecordingProvisioner, FakeFileSystemApi, FakeEc2Api]:
    provisioner = provisioner or RecordingProvisioner()
    ec2 = ec2 or FakeEc2Api()
    efs = efs or FakeFileSystemApi()
    volumes = EfsVolumes(
        region="us-east-1",
        stacks=OptionalStacks(provisioner=provisioner),
        network=Ec2NetworkInspector(lambda: ec2),
        efs=lambda: efs,
    )
    volumes._sleep = lambda _seconds: None
    return volumes, provisioner, efs, ec2


def test_constructing_it_calls_nothing() -> None:
    volumes, provisioner, efs, ec2 = facade()
    assert volumes.stack_name == STACK
    assert provisioner.calls == [] and efs.calls == [] and ec2.calls == []


def test_deploy_checks_first_and_sends_exactly_the_template_parameters() -> None:
    volumes, provisioner, _, ec2 = facade()
    status = volumes.deploy(vpc_id=VPC_ID, subnet_ids=[SUBNET_A, SUBNET_B])
    assert status.outputs["FileSystemId"] == FILE_SYSTEM_ID
    assert ec2.calls[0] == "describe_vpcs"
    assert provisioner.parameters == {
        "VpcId": VPC_ID,
        "SubnetIds": f"{SUBNET_A},{SUBNET_B}",
        "AllowWrite": "true",
        "AccessPointArns": "",
        "ConnectorName": "rayito-efs",
    }


def test_deploy_read_only_and_scoped_to_access_points() -> None:
    volumes, provisioner, _, _ = facade()
    volumes.deploy(
        vpc_id=VPC_ID,
        subnet_ids=[SUBNET_A],
        allow_write=False,
        access_point_arns=[ACCESS_POINT_ARN],
        connector_name="team-efs",
    )
    assert provisioner.parameters["AllowWrite"] == "false"
    assert provisioner.parameters["AccessPointArns"] == ACCESS_POINT_ARN
    assert provisioner.parameters["ConnectorName"] == "team-efs"


def test_deploy_refuses_a_vpc_that_fails_the_check_and_creates_nothing() -> None:
    ec2 = FakeEc2Api(subnets=[subnet(SUBNET_A, "us-east-1a"), subnet(SUBNET_B, "us-east-1a")])
    volumes, provisioner, _, _ = facade(ec2=ec2)
    with pytest.raises(InvalidArgumentException, match="no se creó nada"):
        volumes.deploy(vpc_id=VPC_ID, subnet_ids=[SUBNET_A, SUBNET_B])
    assert not [call for call in provisioner.calls if call[0] in {"create", "update"}]


def test_deploy_rejects_a_bad_access_point_arn_before_any_call() -> None:
    volumes, provisioner, _, ec2 = facade()
    with pytest.raises(InvalidArgumentException):
        volumes.deploy(vpc_id=VPC_ID, subnet_ids=[SUBNET_A], access_point_arns=["fsap-1"])
    assert ec2.calls == [] and provisioner.calls == []


def test_volume_store_reads_the_file_system_from_the_stack() -> None:
    volumes, _, _, _ = facade()
    with pytest.raises(VolumeException):
        volumes.volume_store()
    volumes.deploy(vpc_id=VPC_ID, subnet_ids=[SUBNET_A])
    store = volumes.volume_store()
    assert isinstance(store, VolumeStore)
    assert store.file_system_id == FILE_SYSTEM_ID


def test_destroy_keeps_the_file_system_by_default() -> None:
    volumes, provisioner, efs, _ = facade()
    volumes.deploy(vpc_id=VPC_ID, subnet_ids=[SUBNET_A])
    volumes.destroy()
    assert ("delete", STACK) in provisioner.calls
    assert efs.calls == []


def test_destroy_with_delete_file_system_removes_everything_deploy_created() -> None:
    efs = FakeFileSystemApi(access_points=["fsap-1", "fsap-stale"], drain_polls=2)
    volumes, provisioner, _, _ = facade(efs=efs)
    volumes.deploy(vpc_id=VPC_ID, subnet_ids=[SUBNET_A])
    volumes.destroy(delete_file_system=True)
    assert ("delete", STACK) in provisioner.calls
    assert efs.calls == [
        "describe_file_systems",
        "describe_mount_targets",
        "describe_mount_targets",
        "describe_mount_targets",
        "describe_access_points",
        "delete_access_point:fsap-1",
        "delete_access_point:fsap-stale",
        "delete_file_system",
    ]


def test_destroy_with_delete_file_system_needs_wait() -> None:
    volumes, provisioner, _, _ = facade()
    with pytest.raises(InvalidArgumentException):
        volumes.destroy(delete_file_system=True, wait=False)
    assert provisioner.calls == []


def test_destroy_of_an_already_gone_file_system_is_a_no_op() -> None:
    efs = FakeFileSystemApi(exists=False)
    volumes, _, _, _ = facade(efs=efs)
    volumes.deploy(vpc_id=VPC_ID, subnet_ids=[SUBNET_A])
    volumes.destroy(delete_file_system=True)
    assert efs.calls == ["describe_file_systems"]


def test_destroy_stops_if_mount_targets_never_drain() -> None:
    efs = FakeFileSystemApi(drain_polls=10_000)
    volumes, _, _, _ = facade(efs=efs)
    clock = iter(range(0, 10_000, 60))
    volumes._clock = lambda: float(next(clock))
    volumes.deploy(vpc_id=VPC_ID, subnet_ids=[SUBNET_A])
    with pytest.raises(VolumeException, match="mount targets"):
        volumes.destroy(delete_file_system=True)
    assert "delete_file_system" not in efs.calls


def test_delete_file_system_refuses_one_without_the_stack_tag() -> None:
    efs = FakeFileSystemApi(tags=[{"Key": "team", "Value": "data"}])
    volumes, _, _, _ = facade(efs=efs)
    with pytest.raises(VolumeException, match="etiqueta"):
        volumes.delete_file_system(FILE_SYSTEM_ID)
    assert efs.calls == ["describe_file_systems"]


def test_delete_file_system_after_a_cli_destroy() -> None:
    """`rayito stack destroy efs-volumes` keeps the file system; the facade
    removes it by id later, with the same drain/access-point/delete steps."""
    efs = FakeFileSystemApi(drain_polls=0, access_points=[])
    volumes, provisioner, _, _ = facade(efs=efs)
    volumes.delete_file_system(FILE_SYSTEM_ID)
    assert provisioner.calls == []
    assert efs.calls == [
        "describe_file_systems",
        "describe_mount_targets",
        "describe_access_points",
        "delete_file_system",
    ]
    with pytest.raises(InvalidArgumentException):
        volumes.delete_file_system("not-a-file-system")


@pytest.mark.asyncio
async def test_async_facade_delegates() -> None:
    provisioner = RecordingProvisioner()
    ec2 = FakeEc2Api()
    efs = FakeFileSystemApi(drain_polls=0)
    volumes = AsyncEfsVolumes(
        region="us-east-1",
        stacks=OptionalStacks(provisioner=provisioner),
        network=Ec2NetworkInspector(lambda: ec2),
        efs=lambda: efs,
    )
    report = await volumes.check(vpc_id=VPC_ID, subnet_ids=[SUBNET_A])
    assert report.ok
    await volumes.deploy(vpc_id=VPC_ID, subnet_ids=[SUBNET_A])
    store = await volumes.volume_store()
    assert isinstance(store, AsyncVolumeStore)
    assert store.file_system_id == FILE_SYSTEM_ID
    assert (await volumes.status()) is not None
    await volumes.destroy(delete_file_system=True)
    assert efs.calls[-1] == "delete_file_system"
    efs.calls.clear()
    await volumes.delete_file_system(FILE_SYSTEM_ID)
    assert efs.calls[-1] == "delete_file_system"
