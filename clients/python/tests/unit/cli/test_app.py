"""Punto de entrada, extra ausente, opciones globales, `--json` como único
documento, códigos de salida y `run_shim`."""

from __future__ import annotations

import ast
import importlib
import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayito.cli._session import Clients
from rayito.cli.app import app, run_shim

from .conftest import IMAGE_ARN, FakeControlPlane, Stubs, image_summary, list_item

LIBRARY_MODULES = (
    "rayito.cli._artifact",
    "rayito.cli._publish",
    "rayito.cli._prune",
    "rayito.cli._logs",
    "rayito.cli._checks",
    "rayito.cli._compat",
    "rayito.cli._session",
    "rayito.cli._console",
)
TYPER_MODULES = (
    "rayito.cli.app",
    "rayito.cli.image",
    "rayito.cli.sandbox",
    "rayito.cli.doctor",
    "rayito.cli.__main__",
)
NO_AWS_ENV = {
    "AWS_REGION": None,
    "AWS_DEFAULT_REGION": None,
    "AWS_PROFILE": None,
    "AWS_ACCESS_KEY_ID": None,
    "AWS_SECRET_ACCESS_KEY": None,
    "AWS_SESSION_TOKEN": None,
    "AWS_CONFIG_FILE": "/nonexistent/config",
    "AWS_SHARED_CREDENTIALS_FILE": "/nonexistent/credentials",
    "AWS_EC2_METADATA_DISABLED": "true",
}


def block_typer(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in TYPER_MODULES:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "typer", None)


def test_entry_point_without_typer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    block_typer(monkeypatch)
    main_module = importlib.import_module("rayito.cli.__main__")
    assert main_module.main() == 2
    captured = capsys.readouterr()
    assert "rayito[cli]" in captured.err
    assert captured.out == ""


def test_library_modules_import_without_typer(monkeypatch: pytest.MonkeyPatch) -> None:
    block_typer(monkeypatch)
    for name in LIBRARY_MODULES:
        monkeypatch.delitem(sys.modules, name, raising=False)
    for name in LIBRARY_MODULES:
        module = importlib.import_module(name)
        assert module.__name__ == name
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("typer")


def test_cli_init_has_no_imports() -> None:
    source = (Path(__file__).resolve().parents[3] / "src/rayito/cli/__init__.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    assert not [node for node in ast.walk(tree) if isinstance(node, ast.Import | ast.ImportFrom)]


def test_no_region_exits_2_before_any_call(runner: CliRunner) -> None:
    result = runner.invoke(app, ["image", "list"], env=NO_AWS_ENV)
    assert result.exit_code == 2
    assert "sin región" in result.stderr
    assert "--region" in result.stderr and "AWS_REGION" in result.stderr


def test_zip_needs_no_session(runner: CliRunner, tmp_path: Path) -> None:
    image_dir = tmp_path / "image"
    image_dir.mkdir()
    (image_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    result = runner.invoke(
        app, ["--json", "image", "zip", str(image_dir), str(tmp_path / "out.zip")], env=NO_AWS_ENV
    )
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["files"] == 1


def test_client_error_maps_to_exit_1(
    runner: CliRunner, clients: Clients, stubbed_clients: Stubs
) -> None:
    stubbed_clients.microvms.add_client_error(
        "list_microvm_images",
        service_error_code="AccessDeniedException",
        service_message="not authorized",
    )
    result = runner.invoke(app, ["image", "list"], obj=clients)
    assert result.exit_code == 1
    assert "AWS error AccessDeniedException: not authorized" in result.stderr
    assert result.stdout == ""


def test_json_is_a_single_document(
    runner: CliRunner, clients: Clients, stubbed_clients: Stubs, fake_plane: FakeControlPlane
) -> None:
    stubbed_clients.microvms.add_response("list_microvm_images", {"items": [image_summary()]}, {})
    fake_plane.items.append(list_item("microvm-a"))
    for args in (["image", "list"], ["sandbox", "list"], ["sandbox", "kill", "microvm-a"]):
        result = runner.invoke(app, ["--json", *args], obj=clients)
        assert result.exit_code == 0, result.stderr
        document = json.loads(result.stdout)
        assert isinstance(document, list)


def test_root_without_command_shows_help(runner: CliRunner) -> None:
    result = runner.invoke(app, [])
    assert result.exit_code in (0, 2)
    assert "image" in result.output and "sandbox" in result.output and "doctor" in result.output


def test_run_shim_exit_codes(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_shim(["image", "publish"]) == 2
    assert "--artifact" in capsys.readouterr().err
    assert run_shim(["--help"]) == 0
    assert run_shim(["nope"]) == 2
    assert run_shim(["image", "publish", "--artifact", "x.zip"]) == 2
    assert "--base-image-version" in capsys.readouterr().err


def test_injected_clients_are_used(
    runner: CliRunner, clients: Clients, stubbed_clients: Stubs, fake_plane: FakeControlPlane
) -> None:
    stubbed_clients.microvms.add_response(
        "list_microvm_image_versions", {"items": []}, {"imageIdentifier": IMAGE_ARN}
    )
    result = runner.invoke(app, ["--json", "image", "list", "rayito-base"], obj=clients)
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == []
    assert fake_plane.tokens == []
