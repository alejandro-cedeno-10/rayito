"""`rayito image publish --sizes`/`--env` (m15-sizes-catalog): `validate_sizes`
y `parse_environment_assignments` son puros y rechazan antes de cualquier
llamada a AWS; `sized_settings` hornea `RAYITO_BASELINE_MEMORY_MIB`; y
`publish_sizes` publica una imagen sufijada por tamaño reutilizando la
versión ya construida, sin tocar el baseline."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import typer

from rayito.cli import _artifact, _publish
from rayito.cli._session import Clients
from rayito.cli.image import parse_environment_assignments, validate_sizes

from .conftest import ACCOUNT_ID, REGION, Stubs

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

    code = _publish.publish_sizes(clients, settings, ("4gb",))

    assert code == 0
    # El reuse no debería haber dejado ninguna respuesta de create/update sin usar.
    stubbed_clients.microvms.assert_no_pending_responses()
    stubbed_clients.s3.assert_no_pending_responses()


def test_publish_sizes_with_no_sizes_makes_no_call(clients: Clients, artifact: Path) -> None:
    code = _publish.publish_sizes(clients, base_settings(artifact=artifact), ())
    assert code == 0
