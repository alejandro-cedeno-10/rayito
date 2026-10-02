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
from typing import Any, Final, Protocol

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
#: fijo de `file_system_id` + nombre lo hace idempotente sin depender del
#: reloj del cliente (dos `create()` concurrentes con el mismo nombre piden
#: la misma operación, no dos access points), y scoped al sistema de
#: ficheros para que el mismo nombre en dos sistemas de ficheros distintos
#: nunca comparta token.
_CLIENT_TOKEN_LENGTH = 64

#: `ClientToken` repetido (mismo `FileSystemId`+nombre): EFS responde 409
#: `AccessPointAlreadyExists`, nunca el access point ya creado
#: (`VolumeStore.create` lo atrapa y hace `get(name)` en su lugar).
ACCESS_POINT_ALREADY_EXISTS: Final = "AccessPointAlreadyExists"

#: `DescribeAccessPoints(FileSystemId=...)` es eventualmente consistente
#: (AWS_API_NOTES.md §16 Q99, medido 2026-10-02 en tres ciclos
#: crear/borrar): un access point recién creado tardó hasta 11 s en
#: aparecer en el listado y uno recién borrado siguió listado como
#: `available` hasta 8 s. `create()` de un nombre que ya existe sólo puede
#: resolver su access point por ese listado, así que reintenta `get` durante
#: este presupuesto (casi 3 veces el peor caso medido) antes de rendirse.
LIST_VISIBILITY_BUDGET_SECONDS: Final = 30.0
#: Pausa entre dos `DescribeAccessPoints` de ese reintento: el listado se
#: puso al día en saltos de 1-10 s (Q99), así que sondear más rápido sólo
#: gastaría llamadas.
LIST_VISIBILITY_POLL_SECONDS: Final = 1.0


class EfsApi(Protocol):
    """Lo que Rayito usa de un cliente boto3 `efs` (y sólo esto:
    AWS_API_NOTES.md §22). El adaptador real es el propio cliente."""

    def create_access_point(self, **params: Any) -> dict[str, Any]: ...
    def describe_access_points(self, **params: Any) -> dict[str, Any]: ...
    def delete_access_point(self, **params: Any) -> dict[str, Any]: ...


#: `describe_mount_targets` is deliberately absent: nothing calls it yet
#: (no SDK resolves `EfsVolumeMount.mount_target_ip`, proto comment), so it
#: would be dead code and dead IAM until the first real mounter lands.
IAM_ACTIONS: Mapping[str, str] = {
    "create_access_point": "elasticfilesystem:CreateAccessPoint",
    "describe_access_points": "elasticfilesystem:DescribeAccessPoints",
    "delete_access_point": "elasticfilesystem:DeleteAccessPoint",
}


def client_token(file_system_id: str, name: str) -> str:
    """Hash estable de `file_system_id`+nombre lógico, recortado a
    `_CLIENT_TOKEN_LENGTH`: scoped al sistema de ficheros (research doc
    §4.5) para que el mismo nombre en dos sistemas de ficheros del mismo
    llamante nunca comparta token."""
    digest_input = f"{file_system_id}:{name}".encode()
    return hashlib.sha256(digest_input).hexdigest()[:_CLIENT_TOKEN_LENGTH]


def root_directory_path(name: str) -> str:
    return f"{ROOT_DIRECTORY_PREFIX}/{name}"


def create_access_point_params(file_system_id: str, name: str) -> dict[str, Any]:
    """Los únicos parámetros de `CreateAccessPoint` que Rayito manda
    (AWS_API_NOTES.md §22): usuario y grupo 1000:1000 fijos, el directorio
    raíz bajo `ROOT_DIRECTORY_PREFIX` y la etiqueta que identifica el
    volumen."""
    return {
        "ClientToken": client_token(file_system_id, name),
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
    elif code == ACCESS_POINT_ALREADY_EXISTS:
        error = VolumeException("ya existe un access point con ese nombre")
    elif code == "AccessPointLimitExceeded":
        error = VolumeException("se alcanzó el límite de access points del sistema de ficheros")
    elif code == "IncorrectFileSystemLifeCycleState":
        error = VolumeException("el sistema de ficheros no está en estado 'available'")
    else:
        label = code or type(exc).__name__
        error = VolumeException(f"EFS falló en {IAM_ACTIONS[operation]} ({label})")
    return error
