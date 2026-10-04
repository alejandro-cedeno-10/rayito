"""La puerta de `volumes=` en `Sandbox.create()` (`m15-efs-volumes`,
ADR-018, experimental): valida la petición antes de cualquier llamada a AWS
(rutas, tipos, variante de imagen y el conector de `egress=`) y luego lanza
`UnimplementedError`. `rayd` ya monta EFS (`EfsUtilsMounter`), pero sólo en
una imagen con `amazon-efs-utils`, que ninguna imagen publicada de Rayito
trae todavía, y `create()` aún no manda la sección `efs_volumes` de
`ConfigureSandbox`; hasta entonces `volumes=` falla antes de lanzar nada.

El conector se comprueba aquí porque un MicroVM admite **un solo** conector
de egress (`AWS_API_NOTES.md` §16 Q131, EFS-4): un volumen necesita el de
`infra/efs-volumes.yaml`, así que el sandbox no puede usar además
`INTERNET_EGRESS`, y un `egress=` omitido lo hereda de la imagen y nunca
llega al mount target.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Final, NoReturn

from rayito._models import has_internet_connector
from rayito._mount_path import validate_mount_paths
from rayito._role_policy import require_caps_for
from rayito._volumes._domain import EfsVolume
from rayito.exceptions import InvalidArgumentException, UnimplementedError

#: Nombrado en todos los mensajes de esta puerta.
MEASUREMENT_DOC: str = "docs/research/2026-10-efs-persistence.md"
#: La página que explica cómo dar internet a un sandbox con volumen.
VPC_GUIDE: Final = "funciones-opcionales/volumenes-efs-vpc.md"
#: Lo que un sandbox con volumen hace para tener también internet: salir
#: por la VPC, porque el MicroVM sólo admite un conector de egress (Q131).
INTERNET_THROUGH_VPC: Final = (
    "para tener internet además del volumen, sal por tu VPC: una NAT o un transit gateway "
    "en las subredes del conector y un conector cuyo grupo de seguridad permita esa salida "
    f"(el de efs-volumes sólo deja salir NFS); ver {VPC_GUIDE}"
)
UNIMPLEMENTED_REASON: Final = (
    "es experimental: rayd sólo monta en una imagen rayito-base-caps con amazon-efs-utils, que "
    "ninguna imagen publicada trae todavía, y create() aún no manda la sección efs_volumes "
    f"({MEASUREMENT_DOC})"
)

#: Por qué `volume_mounts=` del shim de E2B nunca podrá montar tal cual: el
#: shim siempre lanza con `INTERNET_EGRESS` (la política va en el guest).
SHIM_REASON: Final = (
    "el shim de E2B siempre lanza con INTERNET_EGRESS y un MicroVM sólo admite un conector de "
    "egress, que un volumen necesita para llegar a tu VPC: usa rayito.Sandbox.create(volumes=..., "
    f"egress=[<ConnectorArn>]); además, {UNIMPLEMENTED_REASON}"
)


def require_volume_connector(egress: Sequence[str] | None, *, feature: str = "volumes=") -> None:
    """Sin I/O: `egress` tiene que ser exactamente un conector propio (el
    `ConnectorArn` de `infra/efs-volumes.yaml` o uno de tu VPC que llegue
    al mount target). `InvalidArgumentException` si falta (el sandbox
    heredaría `INTERNET_EGRESS` de la imagen), si incluye `INTERNET_EGRESS`
    o si trae más de uno: `run-microvm` respondería `ValidationException`
    "Only one egress network connector can be provided" (Q131)."""
    connectors = tuple(egress or ())
    if not connectors:
        raise InvalidArgumentException(
            f"{feature} necesita egress=[<ConnectorArn de efs-volumes>]: sin él el sandbox hereda "
            f"INTERNET_EGRESS de la imagen y no llega al mount target; {INTERNET_THROUGH_VPC}"
        )
    if has_internet_connector(connectors):
        raise InvalidArgumentException(
            f"{feature} no se combina con INTERNET_EGRESS: un MicroVM admite un solo conector de "
            f"egress y el volumen necesita el de tu VPC; {INTERNET_THROUGH_VPC}"
        )
    if len(connectors) > 1:
        raise InvalidArgumentException(
            f"{feature} admite un solo conector en egress= (un MicroVM sólo acepta uno); "
            f"{INTERNET_THROUGH_VPC}"
        )


def require_volume_mounts(
    paths: Iterable[str],
    *,
    image_variant: str | None,
    feature: str = "volume_mounts",
    reason: str = SHIM_REASON,
) -> NoReturn:
    """La puerta del `volume_mounts=` del shim de E2B, sin I/O: rutas
    válidas, variante caps y, después, siempre `UnimplementedError` con
    `reason`."""
    validate_mount_paths(paths)
    require_caps_for(feature, image_variant)
    raise UnimplementedError(feature, reason)


def require_volume_support(
    volumes: Mapping[str, EfsVolume],
    *,
    image_variant: str | None,
    egress: Sequence[str] | None = None,
) -> None:
    """Valida `volumes=` por completo y después lanza siempre
    `UnimplementedError`. El orden importa (forma, rutas, caps y conector
    antes que la función pendiente) para que el primer error que vea el
    llamante sea siempre el que puede corregir."""
    if not volumes:
        raise InvalidArgumentException("volumes= no admite un mapa vacío; omite el argumento")
    for value in volumes.values():
        if not isinstance(value, EfsVolume):
            raise InvalidArgumentException(
                f"volumes= espera valores EfsVolume, se recibió {type(value).__name__}"
            )
    validate_mount_paths(volumes.keys())
    require_caps_for("volumes=", image_variant)
    require_volume_connector(egress)
    raise UnimplementedError("volumes=", UNIMPLEMENTED_REASON)
