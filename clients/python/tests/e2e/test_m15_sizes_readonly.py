"""`size=` contra AWS real, sólo lectura (espejo de
`clients/typescript/tests/e2e/m15-sizes.e2e.test.ts`): sin lanzar ningún
MicroVM, resuelve `size` contra el catálogo cerrado, localiza la imagen de ese
tamaño por la convención de nombres (`<imagen>-<tamaño>`) y comprueba, con la
`GetMicrovmImageVersion` gratuita que usa `get_info()`, que su
`minimumMemoryInMiB` es el que el SDK resolvió en cliente.

Necesita `RAYITO_E2E=1`, `RAYITO_TEMPLATE`, `RAYITO_E2E_SIZE_IMAGE` (el nombre
de la imagen sin sufijo de tamaño, nunca su ARN), `RAYITO_E2E_SIZE` (por
ejemplo `4gb`, publicado con `--sizes`) y `RAYITO_E2E_SIZE_IMAGE_VERSION` (una
versión publicada de esa imagen de tamaño); sin ellas se salta. Nunca imprime
un ARN.
"""

from __future__ import annotations

import os
from typing import Final

import pytest

from rayito._aws import LambdaMicrovmsControlPlane
from rayito._sizing import apply_size_suffix, resolve_size

pytestmark = pytest.mark.e2e

IMAGE_VAR: Final = "RAYITO_E2E_SIZE_IMAGE"
SIZE_VAR: Final = "RAYITO_E2E_SIZE"
VERSION_VAR: Final = "RAYITO_E2E_SIZE_IMAGE_VERSION"


def test_the_published_image_of_that_size_reports_the_memory_the_sdk_resolved(
    control_plane: LambdaMicrovmsControlPlane,
) -> None:
    image = os.environ.get(IMAGE_VAR)
    size = os.environ.get(SIZE_VAR)
    version = os.environ.get(VERSION_VAR)
    if not image or not size or not version:
        pytest.skip(f"exporta {IMAGE_VAR}, {SIZE_VAR} y {VERSION_VAR} para el size de sólo lectura")
    resolved = resolve_size(size)
    arn = control_plane.resolve_template_arn(apply_size_suffix(image, resolved))
    info = control_plane.get_microvm_image_version(arn, version)
    print(f"\n[m15-sizes e2e] {size}: {info.minimum_memory_mib} MiB")
    assert info.minimum_memory_mib == resolved.memory_mib
