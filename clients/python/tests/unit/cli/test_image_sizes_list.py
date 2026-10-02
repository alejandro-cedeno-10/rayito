"""`rayito image sizes` (m15-sizes-catalog): por tamaño del catálogo
cerrado, qué imagen de la variante ya publicó `rayito image publish
--sizes` (o si ninguna) y si comparte artefacto con el baseline
(`sameArtifact`) — siempre una `list-microvm-images` filtrada por el
nombre base, y sólo si hay algún tamaño adicional publicado una
`GetMicrovmImageVersion` (sin cuota propia) por imagen publicada; con sólo
el baseline publicado, ninguna llamada adicional."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from rayito.cli._session import Clients
from rayito.cli.app import app
from rayito.cli.image import size_image_names

from .conftest import ACCOUNT_ID, REGION, Stubs, image_summary, version_item

SIZED_ARN = f"arn:aws:lambda:{REGION}:{ACCOUNT_ID}:microvm-image:rayito-base-4gb"


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


def test_sizes_command_with_only_the_baseline_published_makes_no_extra_call(
    runner: CliRunner, clients: Clients, stubbed_clients: Stubs
) -> None:
    """Sin ningún tamaño adicional publicado, `sameArtifact` no tiene nada
    que comparar: ni siquiera el baseline pide su `GetMicrovmImageVersion`
    (code review de PR #76 #2) — `stubbed_clients` falla el test si algo
    pide una respuesta que nadie añadió."""
    stubbed_clients.microvms.add_response(
        "list_microvm_images",
        {"items": [image_summary("rayito-base")]},
        {"nameFilter": "rayito-base"},
    )
    result = runner.invoke(app, ["--json", "image", "sizes"], obj=clients)
    assert result.exit_code == 0, result.stderr
    rows = {row["size"]: row for row in json.loads(result.stdout)}
    assert rows["2gb"]["published"] is True
    assert all(row["sameArtifact"] is None for row in rows.values())


def test_sizes_command_marks_published_and_missing_sizes(
    runner: CliRunner, clients: Clients, stubbed_clients: Stubs
) -> None:
    items = [image_summary("rayito-base"), image_summary("rayito-base-4gb", active="1.0")]
    stubbed_clients.microvms.add_response(
        "list_microvm_images", {"items": items}, {"nameFilter": "rayito-base"}
    )
    stubbed_clients.microvms.add_response(
        "get_microvm_image_version",
        version_item(3, artifact_uri="s3://b/rayito/images/rayd-aaaaaaaaaaaa.zip"),
        {"imageIdentifier": items[0]["imageArn"], "imageVersion": "3.0"},
    )
    stubbed_clients.microvms.add_response(
        "get_microvm_image_version",
        version_item(
            1,
            artifact_uri="s3://b/rayito/images/rayd-aaaaaaaaaaaa.zip",
            image_name="rayito-base-4gb",
        ),
        {"imageIdentifier": SIZED_ARN, "imageVersion": "1.0"},
    )

    result = runner.invoke(app, ["--json", "image", "sizes"], obj=clients)

    assert result.exit_code == 0, result.stderr
    rows = {row["size"]: row for row in json.loads(result.stdout)}
    assert set(rows) == {"512mb", "1gb", "2gb", "4gb", "8gb"}
    assert rows["2gb"]["name"] == "rayito-base" and rows["2gb"]["published"] is True
    assert rows["2gb"]["sameArtifact"] is None  # el baseline nunca se compara consigo mismo
    assert rows["4gb"]["name"] == "rayito-base-4gb" and rows["4gb"]["published"] is True
    assert rows["4gb"]["imageArn"] is not None
    assert rows["4gb"]["sameArtifact"] is True  # mismo sha256 que el baseline
    assert rows["512mb"]["published"] is False
    assert rows["512mb"]["imageArn"] is None
    assert rows["512mb"]["sameArtifact"] is None  # no publicado: nada que comparar


def test_sizes_command_flags_a_size_published_from_a_different_artifact(
    runner: CliRunner, clients: Clients, stubbed_clients: Stubs
) -> None:
    items = [image_summary("rayito-base"), image_summary("rayito-base-4gb", active="1.0")]
    stubbed_clients.microvms.add_response(
        "list_microvm_images", {"items": items}, {"nameFilter": "rayito-base"}
    )
    stubbed_clients.microvms.add_response(
        "get_microvm_image_version",
        version_item(3, artifact_uri="s3://b/rayito/images/rayd-aaaaaaaaaaaa.zip"),
        {"imageIdentifier": items[0]["imageArn"], "imageVersion": "3.0"},
    )
    stubbed_clients.microvms.add_response(
        "get_microvm_image_version",
        version_item(
            1,
            artifact_uri="s3://b/rayito/images/rayd-bbbbbbbbbbbb.zip",
            image_name="rayito-base-4gb",
        ),
        {"imageIdentifier": SIZED_ARN, "imageVersion": "1.0"},
    )

    result = runner.invoke(app, ["--json", "image", "sizes"], obj=clients)

    assert result.exit_code == 0, result.stderr
    rows = {row["size"]: row for row in json.loads(result.stdout)}
    assert rows["4gb"]["sameArtifact"] is False


def test_sizes_command_uses_the_variant_base_name(
    runner: CliRunner, clients: Clients, stubbed_clients: Stubs
) -> None:
    stubbed_clients.microvms.add_response(
        "list_microvm_images", {"items": []}, {"nameFilter": "rayito-base-slim"}
    )
    result = runner.invoke(app, ["--json", "image", "sizes", "--variant", "slim"], obj=clients)
    assert result.exit_code == 0, result.stderr
    assert all(not row["published"] for row in json.loads(result.stdout))
    assert all(row["sameArtifact"] is None for row in json.loads(result.stdout))


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
