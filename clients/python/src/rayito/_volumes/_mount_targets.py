"""La IP del mount target de cada volumen de `volumes=` (`m15-efs-volumes`,
research doc §4.5 y R3): `rayd` monta con `mounttargetip=` para no depender
de que el guest resuelva el nombre DNS de EFS, así que `create()` la pide a
EFS con las credenciales del llamante cuando el `EfsVolume` no la trae.

Una sola `DescribeMountTargets(FileSystemId=...)` por sistema de ficheros y
por `create()` (`MountTargetResolver` es de un solo uso): dos volúmenes del
mismo sistema de ficheros reutilizan la respuesta. EFS admite un mount target
por AZ y el SDK no conoce la AZ del MicroVM (el conector puede salir por
cualquiera de sus subredes), así que elige el primero `available` en orden de
`AvailabilityZoneId`: determinista y alcanzable desde todas las subredes de la
VPC (el grupo de los mount targets admite el del conector). Llegar a un mount
target de otra AZ funciona; el tráfico entre AZs lo factura EC2 aparte.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any, Final, Protocol

from botocore.exceptions import BotoCoreError, ClientError

from rayito._volumes._base import translate_error
from rayito._volumes._domain import EfsVolume
from rayito.exceptions import VolumeException

#: El único `LifeCycleState` de un mount target que acepta conexiones
#: (AWS_API_NOTES.md §22: `creating`, `available`, `updating`, `deleting`,
#: `deleted`, `error`).
MOUNT_TARGET_AVAILABLE: Final = "available"
#: La operación (clave de `IAM_ACTIONS`) que hace esta resolución.
DESCRIBE_MOUNT_TARGETS: Final = "describe_mount_targets"


class MountTargetsApi(Protocol):
    """Lo único que la resolución usa de un cliente boto3 `efs`."""

    def describe_mount_targets(self, **params: Any) -> dict[str, Any]: ...


def choose_mount_target_ip(response: Mapping[str, Any]) -> str | None:
    """Pura: la `IpAddress` del primer mount target `available` de una
    respuesta de `DescribeMountTargets`, ordenando por `AvailabilityZoneId`
    (y `MountTargetId` para desempatar); `None` si no hay ninguno."""
    candidates = sorted(
        (
            str(target.get("AvailabilityZoneId", "")),
            str(target.get("MountTargetId", "")),
            str(target["IpAddress"]),
        )
        for target in response.get("MountTargets", [])
        if target.get("LifeCycleState") == MOUNT_TARGET_AVAILABLE and target.get("IpAddress")
    )
    return candidates[0][2] if candidates else None


class MountTargetResolver:
    """Resuelve y recuerda la IP de mount target de cada sistema de ficheros
    durante un solo `create()` (una llamada por sistema de ficheros)."""

    def __init__(self, client: MountTargetsApi) -> None:
        self._client = client
        self._cache: dict[str, str] = {}

    def ip_for(self, file_system_id: str) -> str:
        """`VolumeException` si EFS no tiene ningún mount target `available`
        para ese sistema de ficheros (sin pila desplegada, o aún creándose);
        un error de EFS sale traducido por `translate_error`, sin su mensaje."""
        cached = self._cache.get(file_system_id)
        if cached is not None:
            return cached
        try:
            response = self._client.describe_mount_targets(FileSystemId=file_system_id)
        except (ClientError, BotoCoreError) as exc:
            raise translate_error(DESCRIBE_MOUNT_TARGETS, exc) from exc
        ip = choose_mount_target_ip(response)
        if ip is None:
            raise VolumeException(
                "volumes=: el sistema de ficheros no tiene ningún mount target 'available' "
                "(despliega la pila efs-volumes o espera a que termine)"
            )
        self._cache[file_system_id] = ip
        return ip


def needs_mount_targets(volumes: Mapping[str, EfsVolume]) -> bool:
    """`True` si algún volumen llega sin `mount_target_ip`: sólo entonces se
    construye el cliente `efs`."""
    return any(volume.mount_target_ip is None for volume in volumes.values())


def resolve_mount_targets(
    volumes: Mapping[str, EfsVolume], resolver: MountTargetResolver
) -> dict[str, EfsVolume]:
    """Cada volumen con su `mount_target_ip`: el que ya la trae, tal cual;
    el resto, una copia con la que `resolver` devuelve."""
    return {
        path: volume
        if volume.mount_target_ip is not None
        else dataclasses.replace(volume, mount_target_ip=resolver.ip_for(volume.file_system_id))
        for path, volume in volumes.items()
    }
