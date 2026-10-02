"""Puerto y adaptador de `SizeCatalog` (M15, `sizes-catalog`): confirma, con
una única llamada gratuita a `GetMicrovmImageVersion` por versión de imagen,
que `resources[0].minimumMemoryInMiB` coincide con el tamaño que
`Sandbox.create(size=...)` resolvió localmente contra el catálogo cerrado
de `_sizing.py`. Nunca se llama si `size=` no se usó: ver
`test_m15_sizes_catalog_create.py` (lo prueba contra el plano de control
falso) y el trazado dorado de 0.5.x de `test_m15_zero_cost.py` (ni `size=`
ni nada de esta función aparece ahí).
"""

from __future__ import annotations

import threading
from typing import Protocol

from rayito._models import ImageVersionInfo


class ImageVersionReader(Protocol):
    """Lo único que `ConventionCatalog` necesita de `ControlPlane`."""

    def get_microvm_image_version(self, image_arn: str, image_version: str) -> ImageVersionInfo: ...


class SizeCatalog(Protocol):
    """Puerto que `get_info()` consulta para `SandboxInfo.baseline_memory_mib`."""

    def minimum_memory_mib(
        self, reader: ImageVersionReader, image_arn: str, image_version: str
    ) -> int: ...


class ConventionCatalog:
    """Cachea `minimumMemoryInMiB` por `(image_arn, image_version)` y por
    proceso: una versión de imagen publicada es inmutable
    (`update-microvm-image` siempre crea una versión nueva, nunca muta una
    existente), así que el resultado no puede quedar obsoleto dentro del
    mismo proceso. Un `threading.Lock` evita construir la misma entrada dos
    veces desde hilos distintos; no evita dos llamadas entre *procesos*
    distintos, que es aceptable: la llamada es gratuita y sin cuota propia
    (AWS_API_NOTES.md §24)."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()

    def minimum_memory_mib(
        self, reader: ImageVersionReader, image_arn: str, image_version: str
    ) -> int:
        key = (image_arn, image_version)
        with self._lock:
            cached = self._cache.get(key)
        if cached is not None:
            return cached
        resolved = reader.get_microvm_image_version(image_arn, image_version).minimum_memory_mib
        with self._lock:
            self._cache.setdefault(key, resolved)
            return self._cache[key]


# Instancia compartida por proceso: todas las llamadas de `Sandbox.create`/
# `get_info` que piden `size=` reutilizan la misma caché, igual que
# `shared_secret_cache` hace con los secretos (`_secrets.py`).
DEFAULT_SIZE_CATALOG: ConventionCatalog = ConventionCatalog()
