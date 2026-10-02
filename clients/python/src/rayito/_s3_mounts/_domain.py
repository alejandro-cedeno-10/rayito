"""Dominio puro de `mounts=` (`m15-s3-mounts`, ADR-017): el `S3Mount` que
describe un montaje deseado y el `MountStatus` que `ConfigureStatus`
devuelve. Nada aquí importa `grpc` ni `boto3`; la traducción a
`ConfigureRequest` vive en `_section.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from rayito.exceptions import InvalidArgumentException

MountPhase = Literal["pending", "mounted", "failed"]

#: Cadenas cerradas; espejo exacto de `rayd_core::s3_mount::MountErrorClass`
#: (`error.rs`) y de `S3MountState.error_class` en el proto. `MountException`
#: nunca lleva un `code` fuera de este conjunto (salvo `UNKNOWN_ERROR_CLASS`,
#: para una clase que el agente mande y este SDK todavía no conozca).
MOUNT_ERROR_CLASSES: Final[tuple[str, ...]] = (
    "network",
    "iam_denied",
    "not_found",
    "not_allowed",
    "invalid_path",
    "helper_missing",
    "timeout",
)

#: La clase que `create()` informa cuando un montaje sigue `"pending"` al
#: agotar su espera (`_section.MOUNT_SETTLE_TIMEOUT_S`); la misma cadena que
#: el agente usa para su propio límite.
TIMEOUT_ERROR_CLASS: Final = "timeout"

#: Nunca confundir con una clase real del agente (nunca aparece en
#: `MOUNT_ERROR_CLASSES`): `check_section_result` la usa en vez de adivinar
#: una de las clases reales cuando el agente manda una que este SDK no
#: reconoce todavía — ocultar eso detrás de, por ejemplo, `"network"`
#: enmascararía una deriva de protocolo en vez de hacerla visible.
UNKNOWN_ERROR_CLASS: Final = "unknown"


@dataclass(frozen=True)
class S3Mount:
    """Un bucket (o un prefijo suyo) montado en el guest por `rayd` vía
    `mount-s3`/FUSE. Sólo tiene efecto sobre `rayito-base-caps` (o una
    variante derivada por tamaño): `_role_policy.require_caps_for` lo
    comprueba antes de lanzar cuando la imagen se conoce.

    `read_only=True` (el valor por defecto) es la postura de mínimo
    privilegio: `allow_overwrite`/`allow_delete` sólo tienen efecto, y sólo
    se aceptan, con `read_only=False`.
    """

    bucket: str
    prefix: str = ""
    read_only: bool = True
    allow_overwrite: bool = False
    allow_delete: bool = False

    def __post_init__(self) -> None:
        if not self.bucket:
            raise InvalidArgumentException("S3Mount.bucket no puede estar vacío")
        if self.prefix.startswith("/"):
            raise InvalidArgumentException(
                f"S3Mount.prefix es relativo al bucket, sin '/' inicial: {self.prefix!r}"
            )
        if self.read_only and (self.allow_overwrite or self.allow_delete):
            raise InvalidArgumentException(
                "S3Mount: allow_overwrite/allow_delete exigen read_only=False"
            )


@dataclass(frozen=True)
class MountStatus:
    """Lo que `sbx.mounts[path]` expone, leído de `ConfigureStatus` (o del
    resultado de la última `Configure`): `last_error_class` es `None` salvo
    cuando `state == "failed"`.
    """

    state: MountPhase
    last_error_class: str | None = None
