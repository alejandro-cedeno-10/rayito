"""Copy ``kernel-sidecar/`` into ``image/kernel-sidecar/`` for the image zip
(shim; the logic lives in ``rayito.cli._artifact.copy_main``).

Standard library only, like ``image_zip.py`` and for the same reason: the CI
``build`` job and ``release.yml`` run it on a bare ``python3`` without the
SDK installed, so the module is loaded by file path. The target is replaced
wholesale, the same exclusions as the zip apply, and both pin files
(``requirements.txt``, ``requirements-poly.txt``) must exist.

Usage: ``python scripts/copy_sidecar.py kernel-sidecar image/kernel-sidecar``.
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
copy_tree = _artifact.copy_tree


def main(argv: list[str]) -> int:
    return int(_artifact.copy_main(argv))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
