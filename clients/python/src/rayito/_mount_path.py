"""Regla de rutas de montaje compartida por `mounts=` (s3-mounts) y
`volumes=` (efs-volumes) (M15 foundations): valida las claves antes de
lanzar nada, puramente sobre la cadena — el SDK no ve el sistema de
ficheros del guest, así que nunca comprueba que la ruta exista.

Reglas: absoluta, canónica (sin `.`, `..` ni `//`, sin `/` final), bajo
`/mnt/` o `/home/user/`, sin solapar entre sí y como mucho `MAX_MOUNTS` por
sandbox.
"""

from __future__ import annotations

import posixpath
from collections.abc import Iterable
from typing import Final

from rayito.exceptions import InvalidArgumentException

# Un sandbox tiene como mucho 4 puntos de montaje entre `mounts=` y
# `volumes=` juntos: suficiente para los casos reales (datos + salida, o
# un volumen compartido más un bucket) sin convertir el plano de arranque
# en una lista arbitraria.
MAX_MOUNTS: Final = 4
ALLOWED_ROOTS: Final = ("/mnt/", "/home/user/")


def validate_mount_paths(paths: Iterable[str]) -> tuple[str, ...]:
    """Valida cada ruta y que, juntas, no se solapen ni superen
    `MAX_MOUNTS`. Devuelve las rutas tal cual, en el orden recibido."""
    ordered = tuple(paths)
    if len(ordered) > MAX_MOUNTS:
        raise InvalidArgumentException(
            f"como mucho {MAX_MOUNTS} montajes por sandbox (mounts= + volumes=), "
            f"se pidieron {len(ordered)}"
        )
    validated: list[str] = []
    for path in ordered:
        _validate_one(path)
        for other in validated:
            if _overlaps(path, other):
                raise InvalidArgumentException(
                    f"las rutas de montaje no pueden solaparse: {path!r} y {other!r}"
                )
        validated.append(path)
    return ordered


def _validate_one(path: str) -> None:
    if not path.startswith("/"):
        raise InvalidArgumentException(f"la ruta de montaje debe ser absoluta: {path!r}")
    if posixpath.normpath(path) != path:
        raise InvalidArgumentException(
            f"la ruta de montaje debe ser canónica (sin '.', '..', '//' ni '/' final): {path!r}"
        )
    if not any(path.startswith(root) and path != root.rstrip("/") for root in ALLOWED_ROOTS):
        raise InvalidArgumentException(
            f"la ruta de montaje debe estar bajo {ALLOWED_ROOTS}: {path!r}"
        )


def _overlaps(first: str, second: str) -> bool:
    first_dir = f"{first}/"
    second_dir = f"{second}/"
    return first == second or first_dir.startswith(second_dir) or second_dir.startswith(first_dir)
