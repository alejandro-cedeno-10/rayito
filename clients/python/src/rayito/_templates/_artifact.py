"""`ArtifactAssembler` (dominio puro salvo la E/S local ya resuelta por
`_context.py`): junta el Dockerfile compuesto, los ficheros de contexto y
`/etc/rayito/template.json` en un zip determinista — mismas entradas, mismos
bytes, siempre (fechas fijas, orden estable), para que la clave S3 por
sha256 (`_build.py`, igual que `cli/_publish.py` `artifact_key`) detecte de
verdad cuándo el contenido no cambió.
"""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Sequence
from typing import Final

from rayito._templates._dockerfile import (
    TEMPLATE_JSON_CONTEXT_PATH,
    compose_dockerfile,
    context_entry_path,
)
from rayito._templates._instructions import TEMPLATE_SPEC_VERSION, StartSpec, TemplateSpec

#: Zip determinista: toda entrada usa esta fecha fija (igual que
#: `scripts/gen_stack_assets.py` `build_artifact`), así que dos builds del
#: mismo contenido producen bytes idénticos.
ZIP_FIXED_DATE_TIME: Final = (1980, 1, 1, 0, 0, 0)
DOCKERFILE_NAME: Final = "Dockerfile"


def start_spec_to_json(start: StartSpec) -> bytes:
    """El `/etc/rayito/template.json` que lee `rayd` (`rayito.template/1`):
    mismo esquema que `rayd_core::template::StartSpec` (`envs` como mapa,
    no como pares, para ese lado)."""
    payload = {
        "version": TEMPLATE_SPEC_VERSION,
        "start_cmd": start.start_cmd,
        "ready_cmd": start.ready_cmd,
        "user": start.user,
        "workdir": start.workdir,
        "envs": dict(start.envs),
        "ready_poll": (
            {
                "interval_seconds": start.ready_poll.interval_seconds,
                "timeout_seconds": start.ready_poll.timeout_seconds,
            }
            if start.ready_poll is not None
            else None
        ),
    }
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def read_zip_entries(zip_bytes: bytes) -> dict[str, bytes]:
    """Cada entrada de un zip ya existente (el `codeArtifact` de la imagen
    base), como `{ruta: contenido}`; ignora los directorios (entradas que
    terminan en `/`, sin contenido propio)."""
    entries: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        for name in archive.namelist():
            if not name.endswith("/"):
                entries[name] = archive.read(name)
    return entries


def assemble_artifact(
    base_zip: bytes, spec: TemplateSpec, context_files: Sequence[tuple[str, bytes]]
) -> bytes:
    """El zip completo que sube `Template.build()`: todas las entradas del
    zip de la imagen base (un `RUN`/`COPY` de su propio Dockerfile puede
    necesitar cualquiera de ellas), con su `Dockerfile` sustituido por la
    composición (`compose_dockerfile`), más cada fichero de `context_files`
    (ya ordenado y filtrado, `_context.collect_context_files`) bajo
    `CONTEXT_ENTRY_PREFIX` — nunca en el espacio de nombres de la imagen
    base, así que un fichero del usuario no puede sustituir el Dockerfile
    compuesto ni el binario de `rayd` — y `template.json` si `spec.start`
    está puesto. Determinista: mismas
    entradas, mismos bytes, siempre el mismo zip."""
    entries = read_zip_entries(base_zip)
    base_dockerfile = entries.get(DOCKERFILE_NAME, b"").decode("utf-8")
    entries[DOCKERFILE_NAME] = compose_dockerfile(base_dockerfile, spec).encode("utf-8")
    entries.update((context_entry_path(relpath), content) for relpath, content in context_files)
    if spec.start is not None:
        entries[TEMPLATE_JSON_CONTEXT_PATH] = start_spec_to_json(spec.start)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(entries):
            info = zipfile.ZipInfo(name, date_time=ZIP_FIXED_DATE_TIME)
            info.external_attr = 0o644 << 16
            archive.writestr(info, entries[name])
    return buffer.getvalue()
