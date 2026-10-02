"""`rayito image publish --sizes`/`--env` (m15-sizes-catalog): `validate_sizes`
y `parse_environment_assignments` son puros y rechazan antes de cualquier
llamada a AWS; `sized_settings` hornea `RAYITO_BASELINE_MEMORY_MIB`;
`publish_sizes` construye o reutiliza una imagen sufijada por tamaño sin
tocar el baseline, sin imprimir nada; y `publish_with_sizes` agrega el
resumen del baseline con el de cada tamaño en un único documento/bloque de
salida (nunca uno por imagen)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import typer
from botocore.stub import ANY
from typer.testing import CliRunner

from rayito.cli import _artifact, _publish
from rayito.cli._session import Clients
from rayito.cli.app import app
from rayito.cli.image import (
    parse_environment_assignments,
    validate_baseline_memory_mib,
    validate_sizes,
)

from .conftest import ACCOUNT_ID, BASE_IMAGE_ARN, IMAGE_ARN, REGION, Stubs, version_item

BUILD_ROLE = f"arn:aws:iam::{ACCOUNT_ID}:role/build"
T0 = datetime(2026, 9, 1, tzinfo=UTC)
SIZED_ARN = f"arn:aws:lambda:{REGION}:{ACCOUNT_ID}:microvm-image:rayito-base-4gb"


def base_settings(**overrides: object) -> _publish.PublishSettings:
    base: dict[str, object] = {
        "artifact": Path("image/rayito-image.zip"),
        "image_name": "rayito-base",
        "variant": "full",
        "bucket": "bucket",
        "stack_name": "stack",
        "build_role_arn": BUILD_ROLE,
        "base_image_version": "1",
        "memory_mib": 2048,
        "force": False,
        "timeout_seconds": 10.0,
    }
    base.update(overrides)
    return _publish.PublishSettings(**base)  # type: ignore[arg-type]


@pytest.fixture
def image_dir(tmp_path: Path) -> Path:
    root = tmp_path / "image"
    root.mkdir()
    (root / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (root / "rayd").write_bytes(b"\x7fELF")
    return root


@pytest.fixture
def artifact(image_dir: Path, tmp_path: Path) -> Path:
    path = tmp_path / "rayito-image.zip"
    _artifact.write_zip(image_dir, path)
    return path


# ------------------------------------------------------------------- validate_sizes


def test_validate_sizes_with_none_is_empty() -> None:
    assert validate_sizes(None) == ()


def test_validate_sizes_parses_a_comma_separated_list() -> None:
    assert validate_sizes("512mb,4gb") == ("512mb", "4gb")


def test_validate_sizes_strips_whitespace_and_drops_empties() -> None:
    assert validate_sizes(" 512mb , 4gb ,") == ("512mb", "4gb")


def test_validate_sizes_rejects_an_unknown_name_before_any_aws_call() -> None:
    with pytest.raises(typer.BadParameter, match="huge"):
        validate_sizes("huge")


def test_validate_sizes_rejects_the_baseline_name() -> None:
    """El baseline (2048 MiB) ya lo publica `publish_command` sin sufijo;
    pedirlo también en `--sizes` sería ambiguo."""
    with pytest.raises(typer.BadParameter, match="baseline"):
        validate_sizes("2gb")


def test_validate_sizes_rejects_duplicates() -> None:
    with pytest.raises(typer.BadParameter, match="repetidos"):
        validate_sizes("4gb,4gb")


def test_validate_baseline_memory_mib_rejects_a_non_default_value_with_sizes() -> None:
    """`rayito image publish --memory-mib 4096 --sizes 512mb` publicaría el
    baseline (sin sufijo) a 4096 MiB, rompiendo la regla de que la imagen
    sin sufijo siempre es el baseline de 2048 MiB (code review de PR #76):
    rechazado antes de cualquier llamada a AWS."""
    with pytest.raises(typer.BadParameter, match="--memory-mib"):
        validate_baseline_memory_mib(4096, ("512mb",))


def test_validate_baseline_memory_mib_allows_the_default_with_sizes() -> None:
    validate_baseline_memory_mib(_publish.DEFAULT_MEMORY_MIB, ("512mb",))  # no lanza


def test_validate_baseline_memory_mib_allows_any_value_without_sizes() -> None:
    """Sin `--sizes` no hay baseline que proteger: `--memory-mib` sigue
    siendo libre, exactamente como antes de sizes-catalog."""
    validate_baseline_memory_mib(4096, ())  # no lanza


# ------------------------------------------------------- parse_environment_assignments


def test_parse_environment_assignments_empty_list() -> None:
    assert parse_environment_assignments([]) == {}


def test_parse_environment_assignments_parses_key_value_pairs() -> None:
    assert parse_environment_assignments(["RAYITO_FOO=bar", "RAYITO_BAZ=qux"]) == {
        "RAYITO_FOO": "bar",
        "RAYITO_BAZ": "qux",
    }


def test_parse_environment_assignments_rejects_a_missing_equals() -> None:
    with pytest.raises(typer.BadParameter, match="K=V"):
        parse_environment_assignments(["RAYITO_FOO"])


def test_parse_environment_assignments_rejects_an_empty_key() -> None:
    """`=valor` (code review de PR #76): `environmentVariables` lo
    aceptaría pero el guest no podría exportarlo como variable de entorno
    de verdad."""
    with pytest.raises(typer.BadParameter, match="inválido"):
        parse_environment_assignments(["=valor"])


@pytest.mark.parametrize("key", ["1FOO", "FOO BAR", "FOO-BAR", "FOO.BAR"])
def test_parse_environment_assignments_rejects_an_invalid_key(key: str) -> None:
    with pytest.raises(typer.BadParameter, match="inválido"):
        parse_environment_assignments([f"{key}=valor"])


def test_parse_environment_assignments_accepts_a_leading_underscore() -> None:
    assert parse_environment_assignments(["_FOO=bar"]) == {"_FOO": "bar"}


# ------------------------------------------------------------------------ sized_settings


def test_sized_settings_appends_the_suffix_and_sets_memory() -> None:
    settings = _publish.sized_settings(base_settings(), "4gb")
    assert settings.image_name == "rayito-base-4gb"
    assert settings.memory_mib == 4096


def test_sized_settings_bakes_in_the_baseline_memory_env_var() -> None:
    settings = _publish.sized_settings(base_settings(), "4gb")
    assert settings.environment_variables == {"RAYITO_BASELINE_MEMORY_MIB": "4096"}


def test_sized_settings_merges_with_existing_environment_variables() -> None:
    settings = _publish.sized_settings(
        base_settings(environment_variables={"RAYITO_ALLOWED_MOUNT_BUCKETS": "my-bucket"}), "512mb"
    )
    assert settings.environment_variables == {
        "RAYITO_ALLOWED_MOUNT_BUCKETS": "my-bucket",
        "RAYITO_BASELINE_MEMORY_MIB": "512",
    }
    assert settings.memory_mib == 512


def test_sized_settings_does_not_mutate_the_base() -> None:
    base = base_settings()
    _publish.sized_settings(base, "4gb")
    assert base.image_name == "rayito-base"
    assert base.memory_mib == 2048
    assert base.environment_variables == {}


def test_desired_configuration_carries_environment_variables_only_when_set() -> None:
    class FakeClients:
        region = REGION
        account_id = ACCOUNT_ID
        microvms: Any = None
        s3: Any = None
        cloudformation: Any = None
        logs: Any = None

    without_env = _publish.desired_configuration(FakeClients(), base_settings(), "s3://b/k.zip")
    assert "environmentVariables" not in without_env

    sized = _publish.sized_settings(base_settings(), "4gb")
    with_env = _publish.desired_configuration(FakeClients(), sized, "s3://b/k.zip")
    assert with_env["environmentVariables"] == {"RAYITO_BASELINE_MEMORY_MIB": "4096"}
    assert with_env["resources"] == [{"minimumMemoryInMiB": 4096}]


# --------------------------------------------------------------------------- publish_sizes


def sized_image_response(state: str = "UPDATED") -> dict[str, Any]:
    return {"imageArn": SIZED_ARN, "name": "rayito-base-4gb", "state": state, "createdAt": T0}


def sized_version_item(number: int, *, artifact_uri: str) -> dict[str, Any]:
    return {
        "imageVersion": f"{number}.0",
        "state": "SUCCESSFUL",
        "status": "ACTIVE",
        "createdAt": datetime(2026, 9, 1, number, tzinfo=UTC),
        "imageArn": SIZED_ARN,
        "baseImageArn": f"arn:aws:lambda:{REGION}:aws:microvm-image:al2023-1",
        "baseImageVersion": "1.0",
        "buildRoleArn": BUILD_ROLE,
        "codeArtifact": {"uri": artifact_uri},
        "resources": [{"minimumMemoryInMiB": 4096}],
        "cpuConfigurations": [{"architecture": "ARM_64"}],
        "hooks": _publish.IMAGE_HOOKS,
        "logging": {"cloudWatch": {"logGroup": "/rayito/rayito-base-4gb"}},
        "environmentVariables": {"RAYITO_BASELINE_MEMORY_MIB": "4096"},
    }


def sized_build_item(version: str) -> dict[str, Any]:
    return {
        "imageArn": SIZED_ARN,
        "imageVersion": version,
        "buildId": "build-1",
        "buildState": "SUCCESSFUL",
        "architecture": "ARM_64",
        "chipset": "GRAVITON",
        "chipsetGeneration": "3",
        "createdAt": T0,
    }


def test_publish_sizes_reuses_an_already_built_version_without_touching_the_baseline(
    clients: Clients, stubbed_clients: Stubs, artifact: Path
) -> None:
    """`publish_sizes` nunca imprime nada: devuelve el resumen de cada
    tamaño para que `publish_with_sizes` lo agregue con el del baseline."""
    settings = base_settings(artifact=artifact)
    key = _publish.artifact_key(artifact.read_bytes())
    stubbed_clients.s3.add_response("head_object", {}, {"Bucket": "bucket", "Key": key})
    stubbed_clients.microvms.add_response(
        "get_microvm_image", sized_image_response(), {"imageIdentifier": SIZED_ARN}
    )
    stubbed_clients.microvms.add_response(
        "list_microvm_image_versions",
        {"items": [sized_version_item(3, artifact_uri=f"s3://bucket/{key}")]},
        {"imageIdentifier": SIZED_ARN},
    )
    stubbed_clients.microvms.add_response(
        "list_microvm_image_builds",
        {"items": [sized_build_item("3.0")]},
        {"imageIdentifier": SIZED_ARN, "imageVersion": "3.0"},
    )
    stubbed_clients.microvms.add_response(
        "get_microvm_image_build",
        {**sized_build_item("3.0"), "snapshotBuild": {}},
        {"imageIdentifier": SIZED_ARN, "imageVersion": "3.0", "buildId": "build-1"},
    )

    summaries = _publish.publish_sizes(clients, settings, ("4gb",))

    assert set(summaries) == {"4gb"}
    assert summaries["4gb"]["imageArn"] == SIZED_ARN
    assert summaries["4gb"]["imageVersion"] == "3.0"
    assert summaries["4gb"]["launchable"] is True
    # El reuse no debería haber dejado ninguna respuesta de create/update sin usar.
    stubbed_clients.microvms.assert_no_pending_responses()
    stubbed_clients.s3.assert_no_pending_responses()


def test_publish_sizes_with_no_sizes_makes_no_call(clients: Clients, artifact: Path) -> None:
    summaries = _publish.publish_sizes(clients, base_settings(artifact=artifact), ())
    assert summaries == {}


# ------------------------------------------------------------- size_env_var


def test_size_env_var_uppercases_the_size_name() -> None:
    assert _publish.size_env_var("4gb") == "RAYITO_TEMPLATE_4GB"
    assert _publish.size_env_var("512mb") == "RAYITO_TEMPLATE_512MB"


# --------------------------------------------------------- aggregate_summary


def test_aggregate_summary_without_sizes_is_exactly_the_baseline() -> None:
    baseline = {"imageArn": IMAGE_ARN, "imageVersion": "3.0"}
    document = _publish.aggregate_summary(baseline, {})
    assert document == baseline
    assert "sizes" not in document


def test_aggregate_summary_carries_the_baseline_at_the_root_and_sizes_nested() -> None:
    baseline = {"imageArn": IMAGE_ARN, "imageVersion": "3.0"}
    sized = {"4gb": {"imageArn": SIZED_ARN, "imageVersion": "1.0"}}
    document = _publish.aggregate_summary(baseline, sized)
    assert document["imageArn"] == IMAGE_ARN
    assert document["sizes"] == sized


# ------------------------------------------------------- publish_with_sizes (CLI)


def publish_args(artifact: Path, *extra: str) -> list[str]:
    return [
        "image",
        "publish",
        "--artifact",
        str(artifact),
        "--base-image-version",
        "1",
        "--bucket",
        "bucket",
        "--build-role-arn",
        BUILD_ROLE,
        *extra,
    ]


def stub_baseline_reuse(stubs: Stubs, key: str) -> None:
    """La misma versión 3.0 de `rayito-base` que `test_publish.py`'s
    `test_reuse_makes_no_build_call` reutiliza, para una ruta de reuse
    determinista sin esperar ningún gate."""
    stubs.s3.add_response("head_object", {}, {"Bucket": "bucket", "Key": key})
    stubs.microvms.add_response(
        "get_microvm_image",
        {"imageArn": IMAGE_ARN, "name": "rayito-base", "state": "UPDATED", "createdAt": T0},
        {"imageIdentifier": IMAGE_ARN},
    )
    stubs.microvms.add_response(
        "list_microvm_image_versions",
        {"items": [version_item(3, artifact_uri=f"s3://bucket/{key}", base_image_version="1.0")]},
        {"imageIdentifier": IMAGE_ARN},
    )
    stubs.microvms.add_response(
        "list_microvm_image_builds",
        {
            "items": [
                {
                    "imageArn": IMAGE_ARN,
                    "imageVersion": "3.0",
                    "buildId": "build-1",
                    "buildState": "SUCCESSFUL",
                    "architecture": "ARM_64",
                    "chipset": "GRAVITON",
                    "chipsetGeneration": "3",
                    "createdAt": T0,
                }
            ]
        },
        {"imageIdentifier": IMAGE_ARN, "imageVersion": "3.0"},
    )
    stubs.microvms.add_response(
        "get_microvm_image_build",
        {
            "imageArn": IMAGE_ARN,
            "imageVersion": "3.0",
            "buildId": "build-1",
            "buildState": "SUCCESSFUL",
            "architecture": "ARM_64",
            "chipset": "GRAVITON",
            "chipsetGeneration": "3",
            "createdAt": T0,
            "snapshotBuild": {},
        },
        {"imageIdentifier": IMAGE_ARN, "imageVersion": "3.0", "buildId": "build-1"},
    )


def stub_sized_reuse(stubs: Stubs, key: str) -> None:
    stubs.s3.add_response("head_object", {}, {"Bucket": "bucket", "Key": key})
    stubs.microvms.add_response(
        "get_microvm_image", sized_image_response(), {"imageIdentifier": SIZED_ARN}
    )
    stubs.microvms.add_response(
        "list_microvm_image_versions",
        {"items": [sized_version_item(3, artifact_uri=f"s3://bucket/{key}")]},
        {"imageIdentifier": SIZED_ARN},
    )
    stubs.microvms.add_response(
        "list_microvm_image_builds",
        {"items": [sized_build_item("3.0")]},
        {"imageIdentifier": SIZED_ARN, "imageVersion": "3.0"},
    )
    stubs.microvms.add_response(
        "get_microvm_image_build",
        {**sized_build_item("3.0"), "snapshotBuild": {}},
        {"imageIdentifier": SIZED_ARN, "imageVersion": "3.0", "buildId": "build-1"},
    )


def test_publish_with_sizes_json_is_a_single_document(
    runner: CliRunner, clients: Clients, stubbed_clients: Stubs, artifact: Path
) -> None:
    """Con `--sizes` y `--json`, `stdout` debe seguir siendo un único
    documento parseable: el baseline en la raíz, el tamaño bajo `sizes`."""
    key = _publish.artifact_key(artifact.read_bytes())
    stub_baseline_reuse(stubbed_clients, key)
    stub_sized_reuse(stubbed_clients, key)

    result = runner.invoke(app, ["--json", *publish_args(artifact, "--sizes", "4gb")], obj=clients)

    assert result.exit_code == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["imageArn"] == IMAGE_ARN
    assert document["imageVersion"] == "3.0"
    assert set(document["sizes"]) == {"4gb"}
    assert document["sizes"]["4gb"]["imageArn"] == SIZED_ARN


def test_publish_with_sizes_plain_prints_the_baseline_line_once(
    runner: CliRunner, clients: Clients, stubbed_clients: Stubs, artifact: Path
) -> None:
    """En texto plano, `RAYITO_TEMPLATE=` debe seguir nombrando sólo el
    baseline — nunca el último tamaño publicado — y cada tamaño adicional
    va en su propia línea `RAYITO_TEMPLATE_<SIZE>=`."""
    key = _publish.artifact_key(artifact.read_bytes())
    stub_baseline_reuse(stubbed_clients, key)
    stub_sized_reuse(stubbed_clients, key)

    result = runner.invoke(app, publish_args(artifact, "--sizes", "4gb"), obj=clients)

    assert result.exit_code == 0, result.stderr
    lines = result.stdout.splitlines()
    template_lines = [line for line in lines if line.startswith("RAYITO_TEMPLATE=")]
    assert template_lines == [f"RAYITO_TEMPLATE={IMAGE_ARN}"]
    assert f"RAYITO_TEMPLATE_4GB={SIZED_ARN}" in lines


def stub_failed_baseline_build(stubbed_clients: Stubs, artifact: Path, key: str) -> None:
    """El baseline sube el artefacto, no encuentra versión que reutilizar,
    construye una nueva y el gate de tres estados la deja `UPDATE_FAILED`
    (`test_publish_with_sizes_skips_sizes_when_the_baseline_fails` y la
    versión en texto plano del mismo caso, code review de PR #76)."""
    stubbed_clients.s3.add_client_error(
        "head_object",
        service_error_code="404",
        service_message="Not Found",
        http_status_code=404,
        expected_params={"Bucket": "bucket", "Key": key},
    )
    stubbed_clients.s3.add_response("put_object", {}, {"Bucket": "bucket", "Key": key, "Body": ANY})
    stubbed_clients.microvms.add_response(
        "get_microvm_image",
        {"imageArn": IMAGE_ARN, "name": "rayito-base", "state": "UPDATED", "createdAt": T0},
        {"imageIdentifier": IMAGE_ARN},
    )
    stubbed_clients.microvms.add_response(
        "list_microvm_image_versions", {"items": []}, {"imageIdentifier": IMAGE_ARN}
    )
    stubbed_clients.microvms.add_response(
        "get_microvm_image",
        {"imageArn": IMAGE_ARN, "name": "rayito-base", "state": "UPDATED", "createdAt": T0},
        {"imageIdentifier": IMAGE_ARN},
    )
    stubbed_clients.microvms.add_response(
        "update_microvm_image",
        {
            "imageArn": IMAGE_ARN,
            "name": "rayito-base",
            "state": "UPDATING",
            "createdAt": T0,
            "updatedAt": T0,
            "baseImageArn": BASE_IMAGE_ARN,
            "buildRoleArn": BUILD_ROLE,
            "codeArtifact": {"uri": f"s3://bucket/{key}"},
            "imageVersion": "4.0",
        },
        {
            "imageIdentifier": IMAGE_ARN,
            "baseImageArn": BASE_IMAGE_ARN,
            "baseImageVersion": "1",
            "buildRoleArn": BUILD_ROLE,
            "codeArtifact": {"uri": f"s3://bucket/{key}"},
            "resources": [{"minimumMemoryInMiB": 2048}],
            "cpuConfigurations": [{"architecture": "ARM_64"}],
            "hooks": _publish.IMAGE_HOOKS,
            "logging": {"cloudWatch": {"logGroup": "/rayito/rayito-base"}},
            "description": ANY,
        },
    )
    stubbed_clients.microvms.add_response(
        "get_microvm_image",
        {"imageArn": IMAGE_ARN, "name": "rayito-base", "state": "UPDATE_FAILED", "createdAt": T0},
        {"imageIdentifier": IMAGE_ARN},
    )
    stubbed_clients.microvms.add_response(
        "get_microvm_image_version",
        {
            **version_item(4, state="FAILED", status="INACTIVE"),
            "stateReason": "Validate hook invocation timed out after PT10M",
        },
        {"imageIdentifier": IMAGE_ARN, "imageVersion": "4.0"},
    )
    stubbed_clients.microvms.add_response(
        "list_microvm_image_builds",
        {
            "items": [
                {
                    "imageArn": IMAGE_ARN,
                    "imageVersion": "4.0",
                    "buildId": "build-1",
                    "buildState": "FAILED",
                    "architecture": "ARM_64",
                    "chipset": "GRAVITON",
                    "chipsetGeneration": "3",
                    "createdAt": T0,
                }
            ]
        },
        {"imageIdentifier": IMAGE_ARN, "imageVersion": "4.0"},
    )
    stubbed_clients.microvms.add_response(
        "get_microvm_image_build",
        {
            "imageArn": IMAGE_ARN,
            "imageVersion": "4.0",
            "buildId": "build-1",
            "buildState": "FAILED",
            "architecture": "ARM_64",
            "chipset": "GRAVITON",
            "chipsetGeneration": "3",
            "createdAt": T0,
        },
        {"imageIdentifier": IMAGE_ARN, "imageVersion": "4.0", "buildId": "build-1"},
    )
    stubbed_clients.logs.add_response(
        "describe_log_streams",
        {"logStreams": []},
        {
            "logGroupName": "/rayito/rayito-base",
            "orderBy": "LastEventTime",
            "descending": True,
            "limit": 3,
        },
    )


def test_publish_with_sizes_skips_sizes_when_the_baseline_fails(
    runner: CliRunner,
    clients: Clients,
    stubbed_clients: Stubs,
    artifact: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Si el baseline no queda lanzable, no tiene sentido gastar builds en
    los tamaños: el documento agregado sólo trae el baseline fallido."""
    monkeypatch.setattr("rayito.cli._publish.time.sleep", lambda seconds: None)
    key = _publish.artifact_key(artifact.read_bytes())
    stub_failed_baseline_build(stubbed_clients, artifact, key)

    result = runner.invoke(app, ["--json", *publish_args(artifact, "--sizes", "4gb")], obj=clients)

    assert result.exit_code == 1
    document = json.loads(result.stdout)
    assert document["launchable"] is False
    assert "sizes" not in document
    # No se intentó construir ni reutilizar ningún tamaño.
    stubbed_clients.microvms.assert_no_pending_responses()


def test_publish_with_sizes_plain_prints_nothing_when_the_baseline_fails(
    runner: CliRunner,
    clients: Clients,
    stubbed_clients: Stubs,
    artifact: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """En texto plano, un build fallido no debe imprimir ningún
    `RAYITO_TEMPLATE`/`RAYITO_TEMPLATE_<SIZE>` — igual que `publish()` sin
    `--sizes` no imprime nada en un build fallido — para que un script que
    evalúe o lea la última línea de `stdout` nunca recoja el ARN de una
    imagen no lanzable (code review de PR #76)."""
    monkeypatch.setattr("rayito.cli._publish.time.sleep", lambda seconds: None)
    key = _publish.artifact_key(artifact.read_bytes())
    stub_failed_baseline_build(stubbed_clients, artifact, key)

    result = runner.invoke(app, publish_args(artifact, "--sizes", "4gb"), obj=clients)

    assert result.exit_code == 1
    assert "RAYITO_TEMPLATE" not in result.stdout
    stubbed_clients.microvms.assert_no_pending_responses()
