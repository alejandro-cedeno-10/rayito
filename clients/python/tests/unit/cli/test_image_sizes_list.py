"""`rayito image sizes` (m15-sizes-catalog): por tamaño del catálogo
cerrado, qué imagen de la variante ya publicó `rayito image publish
--sizes` (o si ninguna) — una sola `list-microvm-images` filtrada por el
nombre base, ninguna llamada adicional a AWS."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from rayito.cli._session import Clients
from rayito.cli.app import app
from rayito.cli.image import size_image_names

from .conftest import Stubs, image_summary


def test_size_image_names_follows_the_suffix_convention() -> None:
    names = size_image_names("rayito-base")
    assert names["2gb"] == "rayito-base"  # el baseline nunca lleva sufijo
    assert names["4gb"] == "rayito-base-4gb"
    assert names["512mb"] == "rayito-base-512mb"
    assert set(names) == {"512mb", "1gb", "2gb", "4gb", "8gb"}


def test_size_image_names_respects_the_variant_base_name() -> None:
    names = size_image_names("rayito-base-slim")
    assert names["2gb"] == "rayito-base-slim"
    assert names["4gb"] == "rayito-base-slim-4gb"


def test_sizes_command_marks_published_and_missing_sizes(
    runner: CliRunner, clients: Clients, stubbed_clients: Stubs
) -> None:
    items = [image_summary("rayito-base"), image_summary("rayito-base-4gb", active="1.0")]
    stubbed_clients.microvms.add_response(
        "list_microvm_images", {"items": items}, {"nameFilter": "rayito-base"}
    )

    result = runner.invoke(app, ["--json", "image", "sizes"], obj=clients)

    assert result.exit_code == 0, result.stderr
    rows = {row["size"]: row for row in json.loads(result.stdout)}
    assert set(rows) == {"512mb", "1gb", "2gb", "4gb", "8gb"}
    assert rows["2gb"]["name"] == "rayito-base" and rows["2gb"]["published"] is True
    assert rows["4gb"]["name"] == "rayito-base-4gb" and rows["4gb"]["published"] is True
    assert rows["4gb"]["imageArn"] is not None
    assert rows["512mb"]["published"] is False
    assert rows["512mb"]["imageArn"] is None


def test_sizes_command_uses_the_variant_base_name(
    runner: CliRunner, clients: Clients, stubbed_clients: Stubs
) -> None:
    stubbed_clients.microvms.add_response(
        "list_microvm_images", {"items": []}, {"nameFilter": "rayito-base-slim"}
    )
    result = runner.invoke(app, ["--json", "image", "sizes", "--variant", "slim"], obj=clients)
    assert result.exit_code == 0, result.stderr
    assert all(not row["published"] for row in json.loads(result.stdout))


def test_sizes_command_human_table(
    runner: CliRunner, clients: Clients, stubbed_clients: Stubs
) -> None:
    stubbed_clients.microvms.add_response(
        "list_microvm_images",
        {"items": [image_summary("rayito-base")]},
        {"nameFilter": "rayito-base"},
    )
    result = runner.invoke(app, ["image", "sizes"], obj=clients)
    assert result.exit_code == 0, result.stderr
    assert "2gb" in result.stdout and "rayito-base" in result.stdout
    assert "512mb" in result.stdout
