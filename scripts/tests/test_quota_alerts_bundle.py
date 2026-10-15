"""El zip de `quota-alerts` es autosuficiente: cada módulo `rayito.*` que un
`.py` del zip importa está en el zip, lleva el modelo de `lambda-microvms` y
no arrastra gRPC, la CLI ni la SPA."""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import gen_stack_assets

COMPONENT = "quota-alerts"
HEAVY_PREFIXES = (
    "rayito.v1",
    "rayito.cli",
    "rayito.sandbox_sync",
    "rayito.sandbox_async",
    "rayito._dashboard._static",
    "grpc",
)


def entries() -> dict[str, bytes]:
    data = gen_stack_assets.build_artifact(COMPONENT)
    assert data is not None
    archive = zipfile.ZipFile(io.BytesIO(data))
    return {name: archive.read(name) for name in archive.namelist()}


def module_present(name: str, files: dict[str, bytes]) -> bool:
    path = name.replace(".", "/")
    return f"{path}.py" in files or f"{path}/__init__.py" in files


def test_every_sdk_import_of_the_bundle_is_in_the_bundle() -> None:
    files = entries()
    missing = {
        f"{name} -> {module}"
        for name, source in files.items()
        if name.endswith(".py")
        for module in gen_stack_assets.imported_sdk_modules(source)
        if gen_stack_assets.sdk_module_path(module) and not module_present(module, files)
    }
    assert not missing


def test_the_bundle_carries_the_handler_the_check_and_the_service_model() -> None:
    files = entries()
    assert "handler.py" in files
    assert "quota_alert_check/runner.py" in files
    assert "rayito/_dashboard/_quotas.py" in files
    assert "models/lambda-microvms/2025-09-09/service-2.json" in files


def test_nothing_heavy_or_unrelated_is_bundled() -> None:
    files = entries()
    assert not [n for n in files if n.replace("/", ".").startswith(HEAVY_PREFIXES)]
    assert not any(name.startswith("tests/") or "__pycache__" in name for name in files)
