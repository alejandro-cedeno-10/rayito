"""La puerta de `volumes=` en `Sandbox.create()` (`m15-efs-volumes`,
ADR-018, experimental): valida la forma de la petición antes de cualquier
llamada a AWS (rutas, tipos, variante de imagen) y luego lanza
`UnimplementedError`, porque ningún build 0.6 de `rayd` tiene todavía un
`VolumeMounter` real (`UnavailableEfsMounter`, pendiente de la campaña de
medición EFS-1..EFS-20, `docs/research/2026-10-efs-persistence.md`). La
firma toma sólo lo que ya pasa por `_feature_options.plan_features`
(`volumes`, `image_variant`): exigir `execution_role_arn=` y un conector de
`egress=` es responsabilidad del adaptador real, una vez la campaña decida
uno — hoy comprobarlos aquí no cambiaría el resultado (siempre
`UnimplementedError`) y extendería una firma compartida que las otras
funciones 0.6 también usan sin cambios.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import NoReturn

from rayito._mount_path import validate_mount_paths
from rayito._role_policy import require_caps_for
from rayito._volumes._domain import EfsVolume
from rayito.exceptions import InvalidArgumentException, UnimplementedError

#: Nombrado en todos los mensajes de esta puerta (research doc: EFS-1..EFS-20).
MEASUREMENT_DOC: str = "docs/research/2026-10-efs-persistence.md"


def require_volume_mounts(
    paths: Iterable[str], *, image_variant: str | None, feature: str = "volumes="
) -> NoReturn:
    """Lo que comparten `volumes=` y el `volume_mounts=` del shim de E2B, sin
    I/O: rutas válidas, variante caps y, después, siempre
    `UnimplementedError` (ningún `VolumeMounter` real todavía). Cuando el
    montaje exista, esta función dejará de lanzar al final y el shim pasará
    a resolver los nombres de `volume_mounts=` contra su `VolumeStore`."""
    validate_mount_paths(paths)
    require_caps_for(feature, image_variant)
    raise UnimplementedError(
        feature,
        "es experimental: necesita una imagen rayito-base-caps con amazon-efs-utils y "
        "execution_role_arn= más un conector egress= a infra/efs-volumes.yaml, pendiente de "
        f"la campaña de medición EFS-1..EFS-20 ({MEASUREMENT_DOC})",
    )


def require_volume_support(volumes: Mapping[str, EfsVolume], *, image_variant: str | None) -> None:
    """Valida `volumes=` por completo y después lanza siempre
    `UnimplementedError`: hoy no hay ningún adaptador de montaje real. El
    orden importa (rutas y forma antes que caps) para que el primer error
    que vea el llamante sea siempre el más específico."""
    if not volumes:
        raise InvalidArgumentException("volumes= no admite un mapa vacío; omite el argumento")
    for value in volumes.values():
        if not isinstance(value, EfsVolume):
            raise InvalidArgumentException(
                f"volumes= espera valores EfsVolume, se recibió {type(value).__name__}"
            )
    require_volume_mounts(volumes.keys(), image_variant=image_variant)
