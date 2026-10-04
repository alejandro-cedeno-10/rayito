"""Dominio puro de `volumes=` (`m15-efs-volumes`, ADR-018, experimental):
el valor inmutable `EfsVolume`, el estado que reporta un montaje
(`VolumeStatus`/`MountState`) y las constantes de la convención de acceso
que `VolumeStore` aplica siempre (research doc
`docs/research/2026-10-efs-persistence.md` §4.1/§4.5). Sin `boto3`: las
llamadas a EFS viven en `_store.py`/`_store_async.py`.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Final, Literal

from rayito.exceptions import InvalidArgumentException

#: `fs-[0-9a-f]{8,40}` (research doc R3; AWS_API_NOTES.md §22).
FILE_SYSTEM_ID_PATTERN: Final = re.compile(r"^fs-[0-9a-f]{8,40}$")
#: `fsap-[0-9a-f]{8,40}` (research doc R3; AWS_API_NOTES.md §22).
ACCESS_POINT_ID_PATTERN: Final = re.compile(r"^fsap-[0-9a-f]{8,40}$")
#: Un nombre lógico de volumen: letras, números y guiones (research doc §2,
#: "Volume.create(name)"), como mucho lo que cabe en un componente de
#: `RootDirectory.Path` (100 caracteres, modelo `efs`).
VOLUME_NAME_PATTERN: Final = re.compile(r"^[A-Za-z0-9-]{1,100}$")

#: Cada volumen se expone bajo este prefijo del sistema de ficheros del
#: operador (research doc §4.5): `RootDirectory.Path = f"{ROOT_DIRECTORY_PREFIX}/{name}"`.
ROOT_DIRECTORY_PREFIX: Final = "/rayito-volumes"
#: El access point fuerza este usuario POSIX para todo acceso al volumen,
#: tanto en `PosixUser` como en `CreationInfo` (research doc §4.1 regla 5,
#: "root squash": `ClientRootAccess` nunca se concede).
VOLUME_POSIX_UID: Final = 1000
VOLUME_POSIX_GID: Final = 1000
#: Permisos octales de `CreationInfo` para un directorio raíz nuevo
#: (research doc §4.5: "CreationInfo 1000:1000 0750").
ROOT_DIRECTORY_PERMISSIONS: Final = "0750"
#: Etiqueta que identifica el access point como un volumen de Rayito
#: (research doc §4.5); `VolumeStore.list()`/`get()` filtran por ella.
VOLUME_TAG_KEY: Final = "rayito:volume"


def validate_volume_name(name: str) -> str:
    if not isinstance(name, str) or not VOLUME_NAME_PATTERN.match(name):
        raise InvalidArgumentException(
            "el nombre de un volumen EFS es letras, números y guiones, de 1 a 100 caracteres"
        )
    return name


def validate_file_system_id(value: str) -> str:
    """Nunca repite `value` en el mensaje (§6 "los errores nunca repiten
    … identificadores de sistema de ficheros"): sólo la forma esperada."""
    if not isinstance(value, str) or not FILE_SYSTEM_ID_PATTERN.match(value):
        raise InvalidArgumentException("file_system_id inválido: se esperaba 'fs-' y 8-40 hex")
    return value


def validate_mount_target_ip(value: str) -> str:
    """Una IPv4 en notación decimal con puntos (la que `rayd` pasa como
    `mounttargetip=`, `rayd_core::volume::MountTargetIp`); nunca repite
    `value` en el mensaje."""
    try:
        ipaddress.IPv4Address(value)
    except (ipaddress.AddressValueError, TypeError, ValueError):
        raise InvalidArgumentException(
            "mount_target_ip inválido: se esperaba una IPv4 (a.b.c.d)"
        ) from None
    return value


def validate_access_point_id(value: str) -> str:
    """Nunca repite `value` en el mensaje (§6, igual que
    `validate_file_system_id`)."""
    if not isinstance(value, str) or not ACCESS_POINT_ID_PATTERN.match(value):
        raise InvalidArgumentException("access_point_id inválido: se esperaba 'fsap-' y 8-40 hex")
    return value


#: Mirrors `rayd_core::volume::MountState`; sólo `"mounted"` deja usar el
#: volumen (`"degraded"`/`"remounting"` aparecen tras un `resume()`).
MountState = Literal[
    "requested", "mounting", "mounted", "degraded", "remounting", "unmounted", "failed"
]


@dataclass(frozen=True)
class EfsVolume:
    """Un volumen EFS: el access point que un sandbox puede montar bajo
    `volumes={path: EfsVolume(...)}`. Validado en construcción, sin ninguna
    llamada a AWS; `name`/`region` son metadata del cliente (`VolumeStore`
    los rellena al crear o leer uno), nunca parte de la identidad que
    manda al guest."""

    file_system_id: str
    access_point_id: str
    name: str | None = None
    region: str | None = None
    read_only: bool = False
    mount_target_ip: str | None = None

    def __post_init__(self) -> None:
        validate_file_system_id(self.file_system_id)
        validate_access_point_id(self.access_point_id)
        if self.name is not None:
            validate_volume_name(self.name)
        if self.mount_target_ip is not None:
            validate_mount_target_ip(self.mount_target_ip)


@dataclass(frozen=True)
class VolumeStatus:
    """Lo que `sbx.volumes[path]` reporta (una `ConfigureStatus` por
    lectura). `last_error_class` es una cadena cerrada de
    `efs_volumes.proto` (`credentials_expired`, `flush_timeout`, `stale`,
    `iam_denied`...), nunca un mensaje de AWS."""

    state: MountState
    last_error_class: str | None = None
