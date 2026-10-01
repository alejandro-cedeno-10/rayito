"""Partes puras de `VolumeStore`, compartidas por la variante síncrona
(`_store.py`) y la asíncrona (`_store_async.py`, que delega en la síncrona
sobre `asyncio.to_thread`, como `_stacks/_service_async.py`): cómo se
construyen los parámetros de `CreateAccessPoint`, cómo se parsea un
`AccessPointDescription` de vuelta a `EfsVolume`, el filtro por la etiqueta
`rayito:volume` y la tabla de errores de EFS (AWS_API_NOTES.md §22).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any, Protocol

from rayito._aws import aws_code
from rayito._volumes._domain import (
    ROOT_DIRECTORY_PERMISSIONS,
    ROOT_DIRECTORY_PREFIX,
    VOLUME_POSIX_GID,
    VOLUME_POSIX_UID,
    VOLUME_TAG_KEY,
    EfsVolume,
)
from rayito.exceptions import RateLimitException, VolumeException, VolumeNotFoundException

#: `ClientToken` acepta hasta 64 caracteres ASCII (modelo `efs`); un hash
#: fijo del nombre lo hace idempotente sin depender del reloj del cliente
#: (dos `create()` concurrentes con el mismo nombre piden la misma
#: operación, no dos access points).
_CLIENT_TOKEN_LENGTH = 64


class EfsApi(Protocol):
    """Lo que Rayito usa de un cliente boto3 `efs` (y sólo esto:
    AWS_API_NOTES.md §22). El adaptador real es el propio cliente."""

    def create_access_point(self, **params: Any) -> dict[str, Any]: ...
    def describe_access_points(self, **params: Any) -> dict[str, Any]: ...
    def delete_access_point(self, **params: Any) -> dict[str, Any]: ...
    def describe_mount_targets(self, **params: Any) -> dict[str, Any]: ...


IAM_ACTIONS: Mapping[str, str] = {
    "create_access_point": "elasticfilesystem:CreateAccessPoint",
    "describe_access_points": "elasticfilesystem:DescribeAccessPoints",
    "delete_access_point": "elasticfilesystem:DeleteAccessPoint",
    "describe_mount_targets": "elasticfilesystem:DescribeMountTargets",
}


def client_token(name: str) -> str:
    """Hash estable del nombre lógico, recortado a `_CLIENT_TOKEN_LENGTH`."""
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:_CLIENT_TOKEN_LENGTH]


def root_directory_path(name: str) -> str:
    return f"{ROOT_DIRECTORY_PREFIX}/{name}"


def create_access_point_params(file_system_id: str, name: str) -> dict[str, Any]:
    """Los únicos parámetros de `CreateAccessPoint` que Rayito manda
    (AWS_API_NOTES.md §22): usuario y grupo 1000:1000 fijos, el directorio
    raíz bajo `ROOT_DIRECTORY_PREFIX` y la etiqueta que identifica el
    volumen."""
    return {
        "ClientToken": client_token(name),
        "FileSystemId": file_system_id,
        "PosixUser": {"Uid": VOLUME_POSIX_UID, "Gid": VOLUME_POSIX_GID},
        "RootDirectory": {
            "Path": root_directory_path(name),
            "CreationInfo": {
                "OwnerUid": VOLUME_POSIX_UID,
                "OwnerGid": VOLUME_POSIX_GID,
                "Permissions": ROOT_DIRECTORY_PERMISSIONS,
            },
        },
        "Tags": [{"Key": VOLUME_TAG_KEY, "Value": name}],
    }


def volume_name_from_tags(tags: list[Mapping[str, Any]] | None) -> str | None:
    for tag in tags or []:
        if tag.get("Key") == VOLUME_TAG_KEY:
            value = tag.get("Value")
            return str(value) if value else None
    return None


def volume_from_description(described: Mapping[str, Any], *, region: str | None) -> EfsVolume:
    return EfsVolume(
        file_system_id=str(described.get("FileSystemId", "")),
        access_point_id=str(described.get("AccessPointId", "")),
        name=volume_name_from_tags(described.get("Tags")),
        region=region,
    )


def translate_error(operation: str, exc: BaseException) -> Exception:
    """El `ClientError`/`BotoCoreError` como excepción propia de Rayito, sin
    el mensaje de AWS (que repite el id del sistema de ficheros o del
    access point); quien la lanza la encadena con `from exc`."""
    code = aws_code(exc)
    error: Exception
    if code in ("AccessPointNotFound", "FileSystemNotFound"):
        error = VolumeNotFoundException("el access point o el sistema de ficheros no existe")
    elif code == "ThrottlingException":
        error = RateLimitException(f"EFS limitó la tasa de {IAM_ACTIONS[operation]}")
    elif code == "AccessDeniedException":
        error = VolumeException(
            f"sin permiso IAM {IAM_ACTIONS[operation]} sobre el sistema de ficheros "
            "(credenciales del llamante)"
        )
    elif code == "AccessPointAlreadyExists":
        error = VolumeException("ya existe un access point con ese nombre")
    elif code == "AccessPointLimitExceeded":
        error = VolumeException("se alcanzó el límite de access points del sistema de ficheros")
    elif code == "IncorrectFileSystemLifeCycleState":
        error = VolumeException("el sistema de ficheros no está en estado 'available'")
    else:
        label = code or type(exc).__name__
        error = VolumeException(f"EFS falló en {IAM_ACTIONS[operation]} ({label})")
    return error
