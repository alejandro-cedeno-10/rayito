"""`rayito image list`: tabla y JSON de imágenes, versiones de una imagen de
la más nueva a la más antigua con las seis claves, `--name-filter` como
`nameFilter` y `image zip --sidecar`."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from typer.testing import CliRunner

from rayito.cli._artifact import KERNELS_MARKER_ENTRY, WARMUP_MARKER_ENTRY
from rayito.cli._session import Clients
from rayito.cli.app import app

from .conftest import IMAGE_ARN, Stubs, image_summary, version_item


def test_images_table_and_json(runner: CliRunner, clients: Clients, stubbed_clients: Stubs) -> None:
    items = [image_summary(), image_summary("rayito-base-caps", active="6.0", failed="5.0")]
    stubbed_clients.microvms.add_response("list_microvm_images", {"items": items}, {})
    stubbed_clients.microvms.add_response("list_microvm_images", {"items": items}, {})
    human = runner.invoke(app, ["image", "list"], obj=clients)
    assert human.exit_code == 0, human.stderr
    assert (
        "rayito-base-caps" in human.stdout and "6.0" in human.stdout and "UPDATED" in human.stdout
    )
    as_json = runner.invoke(app, ["--json", "image", "list"], obj=clients)
    assert as_json.exit_code == 0, as_json.stderr
    document = json.loads(as_json.stdout)
    assert [row["name"] for row in document] == ["rayito-base", "rayito-base-caps"]
    assert document[1]["latestFailedImageVersion"] == "5.0"
    assert set(document[0]) >= {
        "name",
        "state",
        "latestActiveImageVersion",
        "latestFailedImageVersion",
        "createdAt",
    }


def test_name_filter_is_forwarded(
    runner: CliRunner, clients: Clients, stubbed_clients: Stubs
) -> None:
    stubbed_clients.microvms.add_response(
        "list_microvm_images", {"items": []}, {"nameFilter": "rayito"}
    )
    result = runner.invoke(app, ["--json", "image", "list", "--name-filter", "rayito"], obj=clients)
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == []
    refused = runner.invoke(
        app, ["image", "list", "rayito-base", "--name-filter", "x"], obj=clients
    )
    assert refused.exit_code == 2


def test_versions_of_one_image_newest_first(
    runner: CliRunner, clients: Clients, stubbed_clients: Stubs
) -> None:
    stubbed_clients.microvms.add_response(
        "list_microvm_image_versions",
        {"items": [version_item(2, state="FAILED", status="INACTIVE"), version_item(3)]},
        {"imageIdentifier": IMAGE_ARN},
    )
    result = runner.invoke(app, ["--json", "image", "list", "rayito-base"], obj=clients)
    assert result.exit_code == 0, result.stderr
    document = json.loads(result.stdout)
    assert [row["imageVersion"] for row in document] == ["3.0", "2.0"]
    assert [row["status"] for row in document] == ["ACTIVE", "INACTIVE"]
    assert set(document[0]) == {
        "imageVersion",
        "state",
        "status",
        "baseImageVersion",
        "artifact",
        "createdAt",
    }
    assert document[0]["artifact"] == "rayd-000000000000.zip"
    stubbed_clients.microvms.add_response(
        "list_microvm_image_versions", {"items": [version_item(3)]}, {"imageIdentifier": IMAGE_ARN}
    )
    human = runner.invoke(app, ["image", "list", IMAGE_ARN], obj=clients)
    assert human.exit_code == 0, human.stderr
    assert "3.0" in human.stdout and "SUCCESSFUL" in human.stdout


def image_tree(tmp_path: Path) -> tuple[Path, Path]:
    image_dir = tmp_path / "image"
    image_dir.mkdir()
    (image_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    sidecar = tmp_path / "kernel-sidecar"
    (sidecar / "tests").mkdir(parents=True)
    (sidecar / ".venv").mkdir()
    (sidecar / "__pycache__").mkdir()
    (sidecar / "requirements.txt").write_text("a\n", encoding="utf-8")
    (sidecar / "requirements-poly.txt").write_text("b\n", encoding="utf-8")
    (sidecar / "main.py").write_text("pass\n", encoding="utf-8")
    (sidecar / "tests" / "test_a.py").write_text("", encoding="utf-8")
    (sidecar / ".venv" / "x").write_text("", encoding="utf-8")
    (sidecar / "__pycache__" / "x.pyc").write_bytes(b"")
    (sidecar / "uv.lock").write_text("", encoding="utf-8")
    return image_dir, sidecar


def test_zip_with_sidecar_copy_is_deterministic(runner: CliRunner, tmp_path: Path) -> None:
    image_dir, sidecar = image_tree(tmp_path)
    destination = tmp_path / "out.zip"
    args = [
        "--json",
        "image",
        "zip",
        str(image_dir),
        str(destination),
        "--variant",
        "slim",
        "--sidecar",
        str(sidecar),
    ]
    first = runner.invoke(app, args)
    assert first.exit_code == 0, first.stderr
    summary = json.loads(first.stdout)
    assert summary["variant"] == "slim" and summary["sidecarFiles"] == 3
    copied = sorted(p.name for p in (image_dir / "kernel-sidecar").rglob("*"))
    assert copied == ["main.py", "requirements-poly.txt", "requirements.txt"]
    with zipfile.ZipFile(destination) as archive:
        assert archive.read(WARMUP_MARKER_ENTRY) == b"slim\n"
        assert KERNELS_MARKER_ENTRY not in archive.namelist()
    digest = summary["sha256"]
    second = runner.invoke(app, args)
    assert json.loads(second.stdout)["sha256"] == digest
    human = runner.invoke(app, args[1:])
    assert human.exit_code == 0 and f"sha256={digest}" in human.stdout
    refused = runner.invoke(
        app, ["image", "zip", str(image_dir), str(destination), "--variant", "caps"]
    )
    assert refused.exit_code == 2
