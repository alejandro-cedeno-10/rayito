"""El zip de la pila `dashboard` es autosuficiente: cada módulo `rayito.*` que
algún `.py` del zip importa está también en el zip, la SPA va dentro y nada
pesado (gRPC, el código del proto, la CLI) se cuela. El runtime de Lambda sólo
trae `boto3`; la pila no instala dependencias."""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import gen_stack_assets

HEAVY_PREFIXES = ("rayito.v1", "rayito.cli", "rayito.sandbox_sync", "rayito.sandbox_async", "grpc")


def entries() -> dict[str, bytes]:
    data = gen_stack_assets.build_artifact("dashboard")
    assert data is not None
    archive = zipfile.ZipFile(io.BytesIO(data))
    return {name: archive.read(name) for name in archive.namelist()}


def module_present(name: str, files: dict[str, bytes]) -> bool:
    path = name.replace(".", "/")
    return f"{path}.py" in files or f"{path}/__init__.py" in files


def test_every_sdk_import_of_the_bundle_is_in_the_bundle() -> None:
    files = entries()
    missing: set[str] = set()
    for name, source in files.items():
        if name.endswith(".py"):
            for module in gen_stack_assets.imported_sdk_modules(source):
                if gen_stack_assets.sdk_module_path(module) and not module_present(module, files):
                    missing.add(f"{name} -> {module}")
    assert not missing


def test_the_bundle_carries_the_handler_the_spa_and_the_service_model() -> None:
    files = entries()
    assert "handler.py" in files
    assert "rayito/_dashboard/_static/index.html" in files
    assert "rayito/_dashboard/_static/js/auth.js" in files
    assert "models/lambda-microvms/2025-09-09/service-2.json" in files


def test_nothing_heavy_or_unrelated_is_bundled() -> None:
    files = entries()
    offenders = [name for name in files if name.replace("/", ".").startswith(HEAVY_PREFIXES)]
    assert not offenders
    assert not any(name.startswith("tests/") or "__pycache__" in name for name in files)


def test_package_inits_are_docstring_stubs_so_importing_a_module_pulls_in_nothing_else() -> None:
    for name, source in entries().items():
        if name.startswith("rayito/") and name.endswith("__init__.py"):
            assert source == gen_stack_assets.PACKAGE_STUB, name


def test_the_bundle_is_reproducible() -> None:
    assert gen_stack_assets.build_artifact("dashboard") == gen_stack_assets.build_artifact(
        "dashboard"
    )
