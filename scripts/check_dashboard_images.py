#!/usr/bin/env python3
"""Comprueba las capturas del panel de gestión que se versionan.

El repositorio es público y las capturas son lo único que no revisa
`check_hygiene.py` (no es texto). Por eso esta puerta exige, para cada imagen de
`docs/site/docs/assets/panel/`:

1. Que sea un PNG válido: otros formatos (JPEG con EXIF, por ejemplo) se
   rechazan.
2. Que sólo lleve los chunks de `ALLOWED_PNG_CHUNKS`: ninguno de texto
   (`tEXt`, `iTXt`, `zTXt`), de EXIF (`eXIf`) ni de fecha (`tIME`), que son los
   que arrastran rutas, nombres de equipo, programas y horas de captura.
3. Que no pese más de `MAX_IMAGE_BYTES`, para no engordar el repositorio ni la
   página de documentación.

Sólo biblioteca estándar y sin red. Sale con 1 y lista cada hallazgo si falla.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
PANEL_IMAGES_DIR: Final = REPO_ROOT / "docs" / "site" / "docs" / "assets" / "panel"
MAX_IMAGE_BYTES: Final = 250 * 1024
"""Tope por imagen: cada captura del recorrido mide hoy menos de la mitad."""
PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
PNG_CHUNK_HEADER_BYTES: Final = 8
PNG_CHUNK_CRC_BYTES: Final = 4
ALLOWED_PNG_CHUNKS: Final = frozenset({b"IHDR", b"PLTE", b"tRNS", b"IDAT", b"IEND"})
"""Los chunks que dibujan la imagen; todo lo demás es metadato."""


def png_chunk_types(data: bytes) -> list[bytes] | None:
    """Los tipos de chunk de un PNG, o `None` si `data` no es un PNG íntegro."""
    if not data.startswith(PNG_SIGNATURE):
        return None
    types: list[bytes] = []
    offset = len(PNG_SIGNATURE)
    while offset + PNG_CHUNK_HEADER_BYTES <= len(data):
        (length,) = struct.unpack(">I", data[offset : offset + 4])
        types.append(data[offset + 4 : offset + PNG_CHUNK_HEADER_BYTES])
        offset += PNG_CHUNK_HEADER_BYTES + length + PNG_CHUNK_CRC_BYTES
    if offset != len(data) or not types or types[-1] != b"IEND":
        return None
    return types


def check_image(path: Path) -> list[str]:
    """Los hallazgos de una imagen: vacío si cumple las tres reglas."""
    data = path.read_bytes()
    problems: list[str] = []
    if len(data) > MAX_IMAGE_BYTES:
        problems.append(f"{path.name}: pesa {len(data)} bytes (máximo {MAX_IMAGE_BYTES})")
    chunks = png_chunk_types(data)
    if chunks is None:
        problems.append(f"{path.name}: no es un PNG íntegro (sólo se admite PNG)")
        return problems
    extra = sorted(
        {chunk.decode("ascii", "replace") for chunk in chunks}
        - {c.decode() for c in ALLOWED_PNG_CHUNKS}
    )
    if extra:
        problems.append(f"{path.name}: lleva metadatos o chunks no permitidos: {', '.join(extra)}")
    return problems


def check_directory(directory: Path = PANEL_IMAGES_DIR) -> list[str]:
    """Los hallazgos de todos los ficheros de imagen del directorio."""
    problems: list[str] = []
    for path in sorted(directory.iterdir()):
        if path.is_file():
            problems.extend(check_image(path))
    return problems


def main() -> int:
    problems = check_directory()
    for problem in problems:
        print(problem)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
