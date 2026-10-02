"""``gen_stack_assets.py``'s Lambda artifact packaging: regression for a
committed `.zip` that embedded `__pycache__/*.pyc` (non-deterministic — the
interpreter version and the absolute path a file was compiled from both end
up inside the bytecode) and the component's own `tests/` directory.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import gen_stack_assets


def _write(path: Path, content: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_build_artifact_excludes_pycache_and_tests(tmp_path: Path, monkeypatch) -> None:
    infra = tmp_path / "infra"
    lambda_dir = infra / "lambdas" / "widget"
    _write(lambda_dir / "handlers" / "handler.py", "def handler(event, ctx): ...\n")
    _write(lambda_dir / "handlers" / "__pycache__" / "handler.cpython-314.pyc", "junk")
    _write(lambda_dir / "__pycache__" / "handler.cpython-314.pyc", "junk")
    _write(lambda_dir / "tests" / "test_handler.py", "def test_x(): ...\n")
    _write(lambda_dir / "tests" / "__pycache__" / "test_handler.cpython-314.pyc", "junk")
    _write(lambda_dir / ".mypy_cache" / "3.14" / "cache.db", "junk")
    _write(lambda_dir / "notes.txt", "local scratch")

    monkeypatch.setattr(gen_stack_assets, "INFRA_DIR", infra)
    archive_bytes = gen_stack_assets.build_artifact("widget")
    assert archive_bytes is not None

    names = zipfile.ZipFile(gen_stack_assets.io.BytesIO(archive_bytes)).namelist()
    assert names == sorted(names), "deterministic (sorted) entry order"
    assert "handlers/handler.py" in names
    assert not any("__pycache__" in name for name in names)
    assert not any(name.startswith("tests/") for name in names)
    assert not any(name.endswith(".pyc") for name in names)
    assert not any(name.startswith(".") for name in names)
    assert "notes.txt" not in names


def test_build_artifact_is_none_without_a_lambda_dir(tmp_path: Path, monkeypatch) -> None:
    infra = tmp_path / "infra"
    infra.mkdir()
    monkeypatch.setattr(gen_stack_assets, "INFRA_DIR", infra)
    assert gen_stack_assets.build_artifact("absent") is None


def test_build_artifact_is_none_when_only_excluded_files_exist(
    tmp_path: Path, monkeypatch
) -> None:
    infra = tmp_path / "infra"
    lambda_dir = infra / "lambdas" / "widget"
    _write(lambda_dir / "tests" / "test_handler.py", "def test_x(): ...\n")
    _write(lambda_dir / "__pycache__" / "stale.cpython-314.pyc", "junk")
    monkeypatch.setattr(gen_stack_assets, "INFRA_DIR", infra)
    assert gen_stack_assets.build_artifact("widget") is None


def test_the_committed_events_webhooks_zip_has_no_pycache_or_tests() -> None:
    # The actual regression, against the real committed artifact, not just
    # the logic that builds it: `scripts/gen_stack_assets.py --check` catches
    # drift, but only if something actually runs it (wired into CI by this
    # same change) and only for *this* artifact, not a future one that
    # forgets to regenerate.
    archive = (
        gen_stack_assets.REPO_ROOT
        / "clients"
        / "python"
        / "src"
        / "rayito"
        / "_stacks"
        / "_artifacts"
        / "events-webhooks.zip"
    )
    names = zipfile.ZipFile(archive).namelist()
    assert not any("__pycache__" in name for name in names)
    assert not any(name.startswith("tests/") for name in names)
    assert not any(name.endswith((".pyc", ".pyo")) for name in names)
    assert not any(part.startswith(".") for name in names for part in name.split("/"))


def test_a_bundled_service_model_is_injected_from_its_single_source(
    tmp_path: Path, monkeypatch
) -> None:
    infra = tmp_path / "infra"
    _write(infra / "lambdas" / "widget" / "handlers" / "handler.py", "def handler(e, c): ...\n")
    model = tmp_path / "docs" / "service-2.json"
    _write(model, '{"metadata": {"apiVersion": "2025-01-01"}}')
    monkeypatch.setattr(gen_stack_assets, "INFRA_DIR", infra)
    monkeypatch.setattr(gen_stack_assets, "BUNDLED_SERVICE_MODELS", {"widget": (("svc", model),)})

    archive_bytes = gen_stack_assets.build_artifact("widget")
    assert archive_bytes is not None
    archive = zipfile.ZipFile(gen_stack_assets.io.BytesIO(archive_bytes))
    assert archive.read("models/svc/2025-01-01/service-2.json") == model.read_bytes()


def test_the_committed_events_webhooks_zip_bundles_the_lambda_microvms_model() -> None:
    archive = zipfile.ZipFile(
        gen_stack_assets.REPO_ROOT
        / "clients"
        / "python"
        / "src"
        / "rayito"
        / "_stacks"
        / "_artifacts"
        / "events-webhooks.zip"
    )
    source = gen_stack_assets.REPO_ROOT / "docs" / "aws-api" / "service-2.json"
    assert archive.read("models/lambda-microvms/2025-09-09/service-2.json") == source.read_bytes()
    assert not (
        gen_stack_assets.REPO_ROOT / "infra" / "lambdas" / "events_webhooks" / "models"
    ).exists(), "the model is injected at generation time, never vendored"
