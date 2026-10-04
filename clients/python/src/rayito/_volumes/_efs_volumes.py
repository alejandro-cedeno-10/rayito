"""`EfsVolumes` (`m15-efs-volumes`, experimental): la puesta en marcha rápida
y segura de `infra/efs-volumes.yaml` en una VPC **que ya existe** (el caso
común: muchas cuentas no pueden crear VPCs). Fachada fina sobre
`OptionalStacks` (`deploy`/`status`/`destroy`), más la comprobación previa
de sólo lectura (`check`, `_network.py` + `_vpc.py`) y el borrado explícito
del sistema de ficheros que la pila siempre conserva. Construirla no hace
ninguna llamada a AWS.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from typing import Any, Final

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from rayito._aws import LazyClient, aws_code
from rayito._stacks._model import StackComponent, StackStatus
from rayito._stacks._registry import component_by_name
from rayito._stacks._service import OptionalStacks
from rayito._volumes._base import EfsFileSystemApi, translate_error
from rayito._volumes._domain import validate_file_system_id
from rayito._volumes._network import (
    EfsNetworkReport,
    NetworkInspector,
    inspect_network,
    validate_access_point_arns,
)
from rayito._volumes._store import VolumeStore
from rayito._volumes._vpc import Ec2NetworkInspector
from rayito.exceptions import (
    InvalidArgumentException,
    VolumeException,
    VolumeNotFoundException,
)

_COMPONENT: StackComponent = component_by_name("efs-volumes")  # type: ignore[assignment]

DEFAULT_STACK_NAME: Final = _COMPONENT.default_stack_name
#: Las salidas de la pila que esta fachada lee (`infra/efs-volumes.yaml`).
FILE_SYSTEM_ID_OUTPUT: Final = "FileSystemId"
#: Tras borrar la pila, CloudFormation ya esperó a que cada
#: `AWS::EFS::MountTarget` se borrara; `DescribeMountTargets` puede tardar
#: algo más en dejar de listarlos (como `DescribeAccessPoints`, eventualmente
#: consistente: AWS_API_NOTES.md §16 Q125, hasta 11 s), así que se sondea
#: con margen antes de `DeleteFileSystem`.
MOUNT_TARGET_DRAIN_BUDGET_SECONDS: Final = 120.0
MOUNT_TARGET_DRAIN_POLL_SECONDS: Final = 5.0
#: `DescribeFileSystems`/`DescribeMountTargets`/`DeleteFileSystem` sobre un
#: sistema de ficheros que ya no existe (modelo `efs`).
FILE_SYSTEM_NOT_FOUND: Final = "FileSystemNotFound"
#: La etiqueta literal que `infra/efs-volumes.yaml` pone a su sistema de
#: ficheros (`FileSystemTags`): `delete_file_system` sólo borra uno que la
#: lleve.
FILE_SYSTEM_TAG_KEY: Final = "rayito"
FILE_SYSTEM_TAG_VALUE: Final = "efs-volumes"


def _flag(value: bool) -> str:
    """`AllowWrite` de la plantilla (`AllowedValues: ["true", "false"]`)."""
    return "true" if value else "false"


class EfsVolumes:
    """Volúmenes EFS en tu propia VPC: comprueba, despliega, consulta y
    borra el componente `efs-volumes`. Construirlo no llama a AWS.

    Coste y activación
    -------------------
    Activa: una llamada explícita a `deploy(vpc_id=, subnet_ids=)` (o
        `rayito stack deploy efs-volumes --param VpcId=... --param
        SubnetIds=...`). `check()` es de sólo lectura y no crea nada.
        Experimental: el CRUD de volúmenes es real, pero
        `Sandbox.create(volumes=...)` sigue en `UnimplementedError` (ninguna
        imagen publicada trae `amazon-efs-utils` todavía). Un sandbox con
        volumen usa este conector como único `egress=` y no puede usar
        además `INTERNET_EGRESS`.
    Recursos y llamadas AWS: `check()` = `ec2:DescribeVpcs`,
        `DescribeVpcAttribute`, `DescribeSubnets`, `DescribeRouteTables`.
        `deploy()` crea, sólo dentro de la VPC dada y etiquetado: un sistema
        de ficheros EFS cifrado (clave KMS gestionada por AWS), un mount
        target por subred, dos grupos de seguridad nuevos (el de los mount
        targets sólo admite TCP 2049 desde el del conector), el
        `AWS::Lambda::NetworkConnector` de salida a la VPC para MicroVMs, su
        rol de operador y la política `RayitoEfsVolumeClient`. Nunca modifica
        la VPC, sus subredes, tablas de rutas, NACLs ni grupos existentes.
    Coste aproximado: $0 con el sistema de ficheros vacío; $0,30/GB-mes
        (Standard) y $0,016/GB-mes tras 30 días (IA); Elastic Throughput
        $0,03/GB leído y $0,06/GB escrito; sin cargo listado por mount
        targets, access points ni ENIs del conector (us-east-1, 2026-09-11).
    IAM: el llamante necesita `cloudformation:*Stack*` sobre la pila, crear
        los recursos de arriba (`CAPABILITY_IAM`), los `ec2:Describe*` de
        `check()` y, para borrar el sistema de ficheros,
        `elasticfilesystem:DescribeFileSystems`/`DescribeMountTargets`/
        `DescribeAccessPoints`/`DeleteAccessPoint`/`DeleteFileSystem`; el
        execution role del MicroVM, la política `CallerPolicyArn` de la
        salida (`ClientMount`/`ClientWrite` sólo sobre este sistema de
        ficheros y sus access points; nunca `ClientWrite` sobre
        `read_only_access_point_arns`).
    Cómo apagarla: `destroy()` borra la pila (conector, grupos, mount
        targets, rol y política) y conserva el sistema de ficheros con sus
        datos; `destroy(delete_file_system=True)` borra además sus access
        points y el propio sistema de ficheros: todo lo que `deploy()` creó.
        Tras `rayito stack destroy efs-volumes` (que siempre lo conserva),
        `delete_file_system("fs-…")` borra el que quedó.
    Ejemplo:
        efs = EfsVolumes(region="us-east-1")
        network = {"vpc_id": "vpc-0123456789abcdef0", "subnet_ids": ["subnet-0123456789abcdef0"]}
        report = efs.check(**network)
        efs.deploy(**network)
        store = efs.volume_store()
        efs.destroy(delete_file_system=True)
    """

    def __init__(
        self,
        *,
        stack_name: str = DEFAULT_STACK_NAME,
        region: str | None = None,
        session: boto3.session.Session | None = None,
        stacks: OptionalStacks | None = None,
        network: NetworkInspector | None = None,
        efs: Callable[[], EfsFileSystemApi] | None = None,
    ) -> None:
        self._stack_name = stack_name
        self._region = region
        self._session = session
        self._stacks = stacks or OptionalStacks(region=region, session=session)
        self._network: NetworkInspector = network or Ec2NetworkInspector.lazy(
            region=region, session=session
        )
        self._efs: Callable[[], EfsFileSystemApi] = (
            efs or LazyClient("efs", region=region, session=session).get
        )
        # Reloj y espera del sondeo de mount targets: sólo los tests los cambian.
        self._sleep: Callable[[float], None] = time.sleep
        self._clock: Callable[[], float] = time.monotonic

    @property
    def stack_name(self) -> str:
        return self._stack_name

    # -- comprobación previa (sólo lectura) ------------------------------

    def check(self, *, vpc_id: str, subnet_ids: Sequence[str] | str) -> EfsNetworkReport:
        """Valida, sin crear nada, que la VPC y las subredes sirven: que
        existen, que las subredes son de la VPC y están en AZs distintas,
        que les quedan IPs libres y si la VPC resuelve DNS; avisa de una
        sola AZ y de cómo saldría a internet un sandbox. El informe incluye
        lo que `deploy()` crearía y su coste. Sólo llama a
        `ec2:DescribeVpcs`/`DescribeVpcAttribute`/`DescribeSubnets`/
        `DescribeRouteTables`."""
        return inspect_network(self._network, vpc_id, subnet_ids, cost=_COMPONENT.cost)

    # -- OptionalStack ----------------------------------------------------

    def deploy(
        self,
        *,
        vpc_id: str,
        subnet_ids: Sequence[str] | str,
        allow_write: bool = True,
        access_point_arns: Sequence[str] = (),
        read_only_access_point_arns: Sequence[str] = (),
        connector_name: str | None = None,
        tags: dict[str, str] | None = None,
        wait: bool = True,
    ) -> StackStatus:
        """Corre `check()` y, sólo si ningún hallazgo es `FAIL`, despliega
        (o actualiza) la pila en la VPC dada. `access_point_arns` acota la
        política `RayitoEfsVolumeClient` a esos access points; vacío, a
        cualquier access point de la cuenta y región sobre este sistema de
        ficheros. `allow_write=False` sólo concede `ClientMount`.
        `read_only_access_point_arns` deja esos access points en sólo
        lectura aunque `allow_write` sea `True` (la política les deniega
        `ClientWrite`): es lo que hace de sólo lectura un volumen, porque la
        opción `ro` del montaje no impide escribir por el túnel de
        `efs-proxy` (`AWS_API_NOTES.md` §16 Q133). Ver el bloque "Coste y
        activación" de la clase."""
        arns = validate_access_point_arns(access_point_arns)
        read_only_arns = validate_access_point_arns(
            read_only_access_point_arns, field="read_only_access_point_arns"
        )
        report = self.check(vpc_id=vpc_id, subnet_ids=subnet_ids)
        if not report.ok:
            reasons = "; ".join(finding.message for finding in report.failures)
            raise InvalidArgumentException(
                f"la VPC no sirve para efs-volumes (no se creó nada): {reasons}"
            )
        parameters = {
            "VpcId": report.vpc_id,
            "SubnetIds": ",".join(report.subnet_ids),
            "AllowWrite": _flag(allow_write),
            "AccessPointArns": ",".join(arns),
            "ReadOnlyAccessPointArns": ",".join(read_only_arns),
        }
        if connector_name is not None:
            parameters["ConnectorName"] = connector_name
        return self._stacks.deploy(
            _COMPONENT,
            stack_name=self._stack_name,
            parameters=parameters,
            tags=tags or {},
            wait=wait,
        )

    def status(self) -> StackStatus | None:
        return self._stacks.status(_COMPONENT, stack_name=self._stack_name)

    def volume_store(self) -> VolumeStore:
        """Un `VolumeStore` sobre el sistema de ficheros de la pila ya
        desplegada (lee `FileSystemId` de sus salidas). `VolumeException`
        si la pila no existe."""
        return VolumeStore(
            file_system_id=self._deployed_file_system_id(),
            region=self._region,
            session=self._session,
        )

    def destroy(self, *, delete_file_system: bool = False, wait: bool = True) -> None:
        """Borra la pila. El sistema de ficheros se conserva siempre
        (`DeletionPolicy: Retain`) salvo con `delete_file_system=True`, que
        después borra sus access points y el propio sistema de ficheros (y
        con él todos los datos): eso exige `wait=True`, porque
        `DeleteFileSystem` falla mientras quede un mount target."""
        if delete_file_system and not wait:
            raise InvalidArgumentException(
                "destroy(delete_file_system=True) necesita wait=True: el sistema de ficheros "
                "sólo se puede borrar cuando la pila ya borró sus mount targets"
            )
        file_system_id = self._file_system_id_or_none() if delete_file_system else None
        self._stacks.destroy(_COMPONENT, stack_name=self._stack_name, wait=wait)
        if file_system_id is not None:
            self.delete_file_system(file_system_id)

    def delete_file_system(self, file_system_id: str) -> None:
        """Borra un sistema de ficheros que esta pila creó y conservó (p. ej.
        tras `rayito stack destroy efs-volumes`, que siempre lo deja): espera
        a que no le quede ningún mount target, borra sus access points y
        después el sistema de ficheros, con todos sus datos. Se niega
        (`VolumeException`) si no lleva la etiqueta que `infra/efs-volumes.yaml`
        le pone (`rayito=efs-volumes`): nunca borra uno ajeno. Uno que ya no
        existe es un no-op."""
        validate_file_system_id(file_system_id)
        try:
            self._require_stack_file_system(file_system_id)
            self._wait_without_mount_targets(file_system_id)
            self._delete_access_points(file_system_id)
            self._call("delete_file_system", FileSystemId=file_system_id)
        except _FileSystemGone:
            return

    # -- internos ------------------------------------------------------------

    def _file_system_id_or_none(self) -> str | None:
        status = self.status()
        if status is None or not status.exists:
            return None
        value = status.outputs.get(FILE_SYSTEM_ID_OUTPUT)
        return None if value is None else validate_file_system_id(value)

    def _deployed_file_system_id(self) -> str:
        file_system_id = self._file_system_id_or_none()
        if file_system_id is None:
            raise VolumeException(
                f"la pila {self._stack_name!r} no existe o no tiene FileSystemId: "
                "despliégala con EfsVolumes.deploy(...)"
            )
        return file_system_id

    def _call(self, operation: str, **params: Any) -> dict[str, Any]:
        method = getattr(self._efs(), operation)
        try:
            return dict(method(**params) or {})
        except (ClientError, BotoCoreError) as exc:
            if aws_code(exc) == FILE_SYSTEM_NOT_FOUND:
                raise _FileSystemGone from exc
            raise translate_error(operation, exc) from exc

    def _require_stack_file_system(self, file_system_id: str) -> None:
        described = self._call("describe_file_systems", FileSystemId=file_system_id)
        file_systems = described.get("FileSystems", [])
        if not file_systems:
            raise _FileSystemGone
        tags = {tag.get("Key"): tag.get("Value") for tag in file_systems[0].get("Tags", [])}
        if tags.get(FILE_SYSTEM_TAG_KEY) != FILE_SYSTEM_TAG_VALUE:
            raise VolumeException(
                "ese sistema de ficheros no lleva la etiqueta de efs-volumes "
                f"({FILE_SYSTEM_TAG_KEY}={FILE_SYSTEM_TAG_VALUE}): no se borra"
            )

    def _wait_without_mount_targets(self, file_system_id: str) -> None:
        deadline = self._clock() + MOUNT_TARGET_DRAIN_BUDGET_SECONDS
        while self._call("describe_mount_targets", FileSystemId=file_system_id).get("MountTargets"):
            if self._clock() >= deadline:
                raise VolumeException(
                    "el sistema de ficheros sigue con mount targets tras borrar la pila; "
                    "repite destroy(delete_file_system=True) en unos minutos"
                )
            self._sleep(MOUNT_TARGET_DRAIN_POLL_SECONDS)

    def _delete_access_points(self, file_system_id: str) -> None:
        next_token: str | None = None
        while True:
            params: dict[str, Any] = {"FileSystemId": file_system_id}
            if next_token:
                params["NextToken"] = next_token
            response = self._call("describe_access_points", **params)
            for access_point in response.get("AccessPoints", []):
                try:
                    self._call("delete_access_point", AccessPointId=access_point["AccessPointId"])
                except VolumeNotFoundException:
                    continue  # el listado aún mostraba uno ya borrado (Q125)
            next_token = response.get("NextToken")
            if not next_token:
                return


class _FileSystemGone(Exception):
    """El sistema de ficheros ya no existe: borrarlo es entonces un no-op."""
