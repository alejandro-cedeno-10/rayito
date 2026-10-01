"""Catálogo cerrado de tamaños CPU/RAM de `size=`/`SizeRequest` (M15,
`sizes-catalog`, §4 opción A de `docs/research/2026-10-e2b-out-of-scope.md`).

Dominio puro: ninguna función de aquí hace E/S de red ni de disco. Resuelve
el tamaño pedido por el llamante al primer valor publicado que lo cubra
(nunca por debajo de lo pedido) dentro del catálogo cerrado de
`limits.json`/`_limits.py` (`SUPPORTED_MEMORY_MIB`): `create-microvm-image`
sólo acepta 512/1024/2048/4096/8192 MiB, cualquier otro valor falla con
`ValidationException` síncrona sin crear nada (medido, AWS_API_NOTES.md
Q87), así que el SDK aplica el mismo catálogo antes de llamar a AWS. También
calcula el sufijo de imagen (`<variant>-<size>`, por ejemplo
`rayito-base-4gb`) que `rayito image publish --sizes` publica
(`cli/_publish.py`) y que `apply_size_suffix` antepone al nombre de
plantilla de `Sandbox.create(size=...)`.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import Final

from rayito._limits import SUPPORTED_MEMORY_MIB
from rayito.exceptions import InvalidArgumentException, RayitoCompatWarning

# Nombres cortos de `size="…"`, en el mismo orden que `SUPPORTED_MEMORY_MIB`
# (Q87): el único acoplamiento entre ambas tuplas es este orden, verificado
# por `test_m15_sizes_catalog_domain.py`.
SIZE_NAMES: Final[tuple[str, ...]] = ("512mb", "1gb", "2gb", "4gb", "8gb")
NAME_TO_MEMORY_MIB: Final[dict[str, int]] = dict(zip(SIZE_NAMES, SUPPORTED_MEMORY_MIB, strict=True))
MEMORY_MIB_TO_NAME: Final[dict[int, str]] = {mib: name for name, mib in NAME_TO_MEMORY_MIB.items()}

# DEFAULT_MEMORY_MIB de `cli/_publish.py`: la imagen que `rayito image
# publish` construye sin `--sizes` no lleva sufijo, así que el tamaño que
# coincide con ella tampoco necesita uno.
BASELINE_MEMORY_MIB: Final[int] = 2048
MIN_SUPPORTED_MEMORY_MIB: Final[int] = min(SUPPORTED_MEMORY_MIB)
MAX_SUPPORTED_MEMORY_MIB: Final[int] = max(SUPPORTED_MEMORY_MIB)

# RES-2 (Q88, docs/research/2026-10-e2b-out-of-scope.md, confirma el punto
# suelto de Q68 en AWS_API_NOTES.md §4): el guest ve memoria/512 vCPU en
# los cinco tamaños del catálogo (512->1, 1024->2, 2048->4, 4096->8,
# 8192->16), medido con `nproc`. Una proporción constante, no una
# extrapolación: cubre los cinco valores de `SUPPORTED_MEMORY_MIB`.
MIB_PER_VCPU_Q88: Final[int] = 512


@dataclass(frozen=True)
class SizeRequest:
    """`size=SizeRequest(memory_mib=3000)`: una petición explícita en MiB,
    para cuando ningún nombre corto (`"512mb".."8gb"`) encaja exactamente.
    Siempre se redondea hacia arriba, nunca hacia abajo: un sandbox nunca
    recibe menos memoria de la que se pidió."""

    memory_mib: int


@dataclass(frozen=True)
class ResolvedSize:
    """Un tamaño ya resuelto contra el catálogo cerrado. `name` es el
    sufijo de imagen (`"4gb"`; nunca se usa para el baseline, ver
    `is_baseline`), `memory_mib` el valor publicado (siempre uno de
    `SUPPORTED_MEMORY_MIB`) y `requested_mib` lo que el llamante pidió en
    origen (igual a `memory_mib` cuando el nombre corto encaja exacto)."""

    name: str
    memory_mib: int
    requested_mib: int

    @property
    def rounded_up(self) -> bool:
        return self.memory_mib != self.requested_mib

    @property
    def is_baseline(self) -> bool:
        return self.memory_mib == BASELINE_MEMORY_MIB

    @property
    def baseline_cpu(self) -> int:
        """vCPU del guest para `memory_mib` (RES-2/Q88, ver `MIB_PER_VCPU_Q88`)."""
        return baseline_cpu_for(self.memory_mib)


def baseline_cpu_for(memory_mib: int) -> int:
    """vCPU del guest para `memory_mib`, medidos exactamente para los cinco
    tamaños del catálogo (RES-2/Q88: ver `MIB_PER_VCPU_Q88`); al menos 1.
    Se usa tanto para el tamaño resuelto localmente como para el
    `minimumMemoryInMiB` que `ConventionCatalog` confirma por AWS, que
    pueden diferir si una imagen se publicó a mano, fuera de `rayito image
    publish --sizes`."""
    return max(1, memory_mib // MIB_PER_VCPU_Q88)


def _requested_memory_mib(size: str | SizeRequest) -> int:
    if isinstance(size, SizeRequest):
        if size.memory_mib <= 0:
            raise InvalidArgumentException(
                f"size=SizeRequest(memory_mib={size.memory_mib!r}): tiene que ser positivo"
            )
        return size.memory_mib
    try:
        return NAME_TO_MEMORY_MIB[size]
    except KeyError:
        admitted = ", ".join(SIZE_NAMES)
        raise InvalidArgumentException(
            f"size={size!r}: admitidos {admitted} o SizeRequest(memory_mib=...)"
        ) from None


def resolve_size(size: str | SizeRequest) -> ResolvedSize:
    """Redondea `size` hacia arriba al primer valor de
    `SUPPORTED_MEMORY_MIB` que lo cubra. `InvalidArgumentException` antes de
    cualquier llamada a AWS tanto si el nombre/los MiB no son válidos como
    si ni siquiera `MAX_SUPPORTED_MEMORY_MIB` alcanza (por ejemplo 16384,
    Q87)."""
    requested = _requested_memory_mib(size)
    for published in SUPPORTED_MEMORY_MIB:
        if published >= requested:
            return ResolvedSize(
                name=MEMORY_MIB_TO_NAME[published],
                memory_mib=published,
                requested_mib=requested,
            )
    raise InvalidArgumentException(
        f"size={size!r}: pide {requested} MiB, por encima del máximo publicado "
        f"({MAX_SUPPORTED_MEMORY_MIB} MiB, Q87)"
    )


def apply_size_suffix(image_name: str, resolved: ResolvedSize) -> str:
    """`"rayito-base"` + tamaño `4gb` -> `"rayito-base-4gb"`. El baseline
    (2048 MiB) no añade sufijo: `rayito image publish` sin `--sizes` ya lo
    publica así. Un ARN no admite sufijo (la imagen la nombra su ARN, no la
    convención `rayito-<variant>[-<size>]`): usa el nombre, no el ARN, con
    `size=`."""
    if image_name.startswith("arn:"):
        raise InvalidArgumentException(
            "size= no se puede combinar con un template dado por ARN: publica una "
            "imagen con sufijo de tamaño y pásala por su nombre, no por ARN"
        )
    if resolved.is_baseline:
        return image_name
    return f"{image_name}-{resolved.name}"


def warn_if_rounded(resolved: ResolvedSize, *, stacklevel: int) -> None:
    """Un `RayitoCompatWarning` (no deduplicado entre llamadas: a
    diferencia de `warn_shared_access_token`, el mensaje depende de lo
    pedido) cuando `size=` no cae justo en el catálogo."""
    if not resolved.rounded_up:
        return
    warnings.warn(
        f"size pidió {resolved.requested_mib} MiB; se redondea hacia arriba a "
        f"{resolved.memory_mib} MiB ({resolved.name})",
        RayitoCompatWarning,
        stacklevel=stacklevel,
    )
