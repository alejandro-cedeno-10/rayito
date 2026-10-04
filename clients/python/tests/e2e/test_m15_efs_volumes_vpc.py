"""`EfsVolumes` en una VPC existente contra AWS real (`RAYITO_E2E=1`,
`m15-efs-volumes`): comprobación previa, despliegue, CRUD de un volumen y
`destroy(delete_file_system=True)`, probando además que la VPC del llamante
no cambia (tablas de rutas, NACLs y grupos de seguridad idénticos antes y
después).

Necesita, además de las variables habituales de `conftest.py`:
- `RAYITO_E2E_VPC_ID`: una VPC que ya existe y que puedes usar (nunca se
  crea, modifica ni borra).
- `RAYITO_E2E_SUBNET_IDS`: de 1 a 3 subredes de esa VPC, separadas por
  comas, cada una en otra AZ.

Nunca imprime ni guarda un id de la VPC, de las subredes ni de lo creado:
sólo cuentas y códigos. Coste: un sistema de ficheros vacío y sus mount
targets durante unos minutos (~$0) más el conector (sin cargo listado).
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError

from rayito import EfsVolumes

from .conftest import E2ESettings

pytestmark = pytest.mark.e2e

VPC_ID_VAR = "RAYITO_E2E_VPC_ID"
SUBNET_IDS_VAR = "RAYITO_E2E_SUBNET_IDS"
NFS_PORT = 2049
E2E_TAG_KEY = "rayito:e2e"
#: `DeleteFileSystem` es asíncrono: el sistema de ficheros sigue en
#: `DescribeFileSystems` con `LifeCycleState=deleting` unos segundos antes de
#: dar `FileSystemNotFound` (medido 2026-10-04, AWS_API_NOTES.md §16 Q126).
FILE_SYSTEM_GONE_BUDGET_SECONDS = 120.0
FILE_SYSTEM_GONE_POLL_SECONDS = 2.0
DELETING_STATES = {"deleting", "deleted"}


def report(label: str, value: object) -> None:
    print(f"[efs-volumes vpc e2e] {label}: {value}")


def _network() -> tuple[str, list[str]] | None:
    vpc_id = os.environ.get(VPC_ID_VAR, "")
    subnets = [s.strip() for s in os.environ.get(SUBNET_IDS_VAR, "").split(",") if s.strip()]
    return (vpc_id, subnets) if vpc_id and subnets else None


requires_network = pytest.mark.skipif(
    _network() is None, reason=f"necesita {VPC_ID_VAR} y {SUBNET_IDS_VAR}"
)


def _canonical(items: Any) -> tuple[str, ...]:
    """Una lista de la API EC2 sin depender de su orden: EC2 no garantiza el
    orden de `Routes`, `Associations`, `Entries` ni `IpPermissions` entre dos
    llamadas (medido: dos `DescribeRouteTables` seguidas devolvieron las
    mismas asociaciones en otro orden), así que se compara como conjunto."""
    return tuple(sorted(json.dumps(item, sort_keys=True, default=str) for item in items or []))


def network_snapshot(ec2: Any, vpc_id: str, *, groups_in: set[str] | None = None) -> dict[str, Any]:
    """Lo que la pila no debe tocar nunca: las tablas de rutas, las NACLs
    y los grupos de seguridad de la VPC. Con `groups_in`, sólo esos grupos
    (los que ya existían antes del despliegue): un grupo creado entretanto
    por la pila, o por otro despliegue en la misma VPC, no cuenta como
    cambio."""
    vpc_filter = [{"Name": "vpc-id", "Values": [vpc_id]}]
    tables = ec2.describe_route_tables(Filters=vpc_filter)["RouteTables"]
    acls = ec2.describe_network_acls(Filters=vpc_filter)["NetworkAcls"]
    groups = ec2.describe_security_groups(Filters=vpc_filter)["SecurityGroups"]
    return {
        "routes": sorted(
            (t["RouteTableId"], _canonical(t.get("Routes")), _canonical(t.get("Associations")))
            for t in tables
        ),
        "acls": sorted(
            (a["NetworkAclId"], _canonical(a.get("Entries")), _canonical(a.get("Associations")))
            for a in acls
        ),
        "groups": sorted(
            (
                g["GroupId"],
                _canonical(g.get("IpPermissions")),
                _canonical(g.get("IpPermissionsEgress")),
            )
            for g in groups
            if groups_in is None or g["GroupId"] in groups_in
        ),
    }


@pytest.fixture
def efs_volumes(e2e_settings: E2ESettings) -> Iterator[EfsVolumes]:
    """Una pila con nombre (y conector) propio de esta ejecución, borrada
    con su sistema de ficheros al final pase lo que pase."""
    name = f"rayito-efs-e2e-{uuid.uuid4().hex[:8]}"
    volumes = EfsVolumes(stack_name=name, region=e2e_settings.region)
    yield volumes
    volumes.destroy(delete_file_system=True)
    assert volumes.status() is None
    report("cleanup", "pila y sistema de ficheros borrados")


@requires_network
def test_check_is_read_only_and_accepts_the_network(e2e_settings: E2ESettings) -> None:
    network = _network()
    assert network is not None
    vpc_id, subnets = network
    volumes = EfsVolumes(region=e2e_settings.region)
    result = volumes.check(vpc_id=vpc_id, subnet_ids=subnets)
    report("check", f"{result.status} {[(f.code, f.level) for f in result.findings]}")
    report("availability zones", len(result.availability_zones))
    assert result.ok


@requires_network
def test_deploy_crud_and_destroy_leave_the_vpc_untouched(
    efs_volumes: EfsVolumes, e2e_settings: E2ESettings
) -> None:
    network = _network()
    assert network is not None
    vpc_id, subnets = network
    session = boto3.session.Session(region_name=e2e_settings.region)
    ec2 = session.client("ec2")
    efs = session.client("efs")
    before = network_snapshot(ec2, vpc_id)

    run_tag = uuid.uuid4().hex[:8]
    status = efs_volumes.deploy(
        vpc_id=vpc_id,
        subnet_ids=subnets,
        connector_name=efs_volumes.stack_name,
        tags={E2E_TAG_KEY: run_tag},
    )
    outputs = status.outputs
    own_groups = {outputs["MountTargetSecurityGroupId"], outputs["ClientSecurityGroupId"]}
    report("deploy", f"{status.state}, connector {outputs.get('ConnectorState')}")
    assert not own_groups & {g[0] for g in before["groups"]}, "la pila reutilizó un grupo existente"

    # Mount targets: NFS only, only from the client group.
    (mount_group,) = ec2.describe_security_groups(GroupIds=[outputs["MountTargetSecurityGroupId"]])[
        "SecurityGroups"
    ]
    rules = mount_group["IpPermissions"]
    assert len(rules) == 1
    assert (rules[0]["IpProtocol"], rules[0]["FromPort"], rules[0]["ToPort"]) == (
        "tcp",
        NFS_PORT,
        NFS_PORT,
    )
    assert [p["GroupId"] for p in rules[0]["UserIdGroupPairs"]] == [
        outputs["ClientSecurityGroupId"]
    ]
    assert not rules[0].get("IpRanges")

    # The file system: encrypted, tagged, one mount target per subnet, TLS policy.
    file_system_id = outputs["FileSystemId"]
    (described,) = efs.describe_file_systems(FileSystemId=file_system_id)["FileSystems"]
    tags = {t["Key"]: t["Value"] for t in described.get("Tags", [])}
    assert described["Encrypted"] is True
    assert tags.get("rayito") == "efs-volumes"
    report("file system tags propagated from the stack", E2E_TAG_KEY in tags)
    mount_targets = efs.describe_mount_targets(FileSystemId=file_system_id)["MountTargets"]
    assert len(mount_targets) == len(subnets)
    policy = efs.describe_file_system_policy(FileSystemId=file_system_id)["Policy"]
    assert "aws:SecureTransport" in policy

    # The quick path: a VolumeStore straight from the stack.
    store = efs_volumes.volume_store()
    volume = store.create(f"e2e-{run_tag}")
    assert volume.access_point_id.startswith("fsap-")
    assert store.destroy(volume.name or "") is True

    after = network_snapshot(ec2, vpc_id, groups_in={g[0] for g in before["groups"]})
    assert after == before, "la VPC del llamante cambió"
    report("vpc untouched", "rutas, NACLs y grupos existentes idénticos")

    efs_volumes.destroy(delete_file_system=True)
    report("file system gone after (s)", seconds_until_file_system_gone(efs, file_system_id))


def seconds_until_file_system_gone(efs: Any, file_system_id: str) -> float:
    """Segundos hasta `FileSystemNotFound` tras `DeleteFileSystem`; mientras
    tanto sólo se admite `deleting`/`deleted` (nunca `available`)."""
    started = time.monotonic()
    while True:
        try:
            (described,) = efs.describe_file_systems(FileSystemId=file_system_id)["FileSystems"]
        except ClientError as exc:
            assert exc.response["Error"]["Code"] == "FileSystemNotFound"
            return round(time.monotonic() - started, 1)
        assert described["LifeCycleState"] in DELETING_STATES
        assert time.monotonic() - started < FILE_SYSTEM_GONE_BUDGET_SECONDS
        time.sleep(FILE_SYSTEM_GONE_POLL_SECONDS)
