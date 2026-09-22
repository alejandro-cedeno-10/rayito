"""Zip an image directory with its Dockerfile at the archive root
(shim of ``rayito image zip``; the logic lives in ``rayito.cli._artifact``).

Standard library only, on purpose: the CI ``build`` job and ``release.yml``
zip the image on a bare ``python3`` in a Rust job with no ``uv``, and
installing the SDK (grpcio, protobuf, boto3) only to zip a directory would
enlarge the trusted base of the signed release artifact. So this file loads
``clients/python/src/rayito/cli/_artifact.py`` **by file path** (no package
import, no site-packages) and re-exports ``marker_variant``, ``VARIANTS``,
``write_zip`` and ``is_excluded`` for the callers that import them
(``copy_sidecar.py``, the CI one-liner). ``tests/unit/cli/test_artifact.py``
keeps that module stdlib-only and ``scripts/tests/test_shims.py`` runs this
shim with ``python -I``.

Usage: ``python scripts/image_zip.py image image/rayito-image.zip [--variant full|slim|poly]``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

ARTIFACT_MODULE = (
    Path(__file__).resolve().parent.parent
    / "clients"
    / "python"
    / "src"
    / "rayito"
    / "cli"
    / "_artifact.py"
)


def load_artifact_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "rayito_cli_artifact", ARTIFACT_MODULE
    )
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {ARTIFACT_MODULE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_artifact = load_artifact_module()

VARIANTS = _artifact.VARIANTS
WARMUP_MARKER_ENTRY = _artifact.WARMUP_MARKER_ENTRY
KERNELS_MARKER_ENTRY = _artifact.KERNELS_MARKER_ENTRY
FIXED_DATE_TIME = _artifact.FIXED_DATE_TIME
is_excluded = _artifact.is_excluded
write_zip = _artifact.write_zip
marker_variant = _artifact.marker_variant


def main(argv: list[str]) -> int:
    return int(_artifact.zip_main(argv))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
