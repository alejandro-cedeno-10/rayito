"""`rayito.cli._publish` sin AWS y con `Stubber`: `--os-capabilities` añade
exactamente `additionalOsCapabilities: ["ALL"]`, la configuración siempre
lleva `baseImageVersion`, el reuse acepta el `1.0` que devuelve
`list-microvm-image-versions` (AWS_API_NOTES.md Q52), los nombres por defecto
siguen la variante, un artefacto que no casa se rechaza antes de cualquier
llamada, el reuse no construye, un build fallido imprime el `stateReason` y
la cola de logs, la subida se salta si la clave existe y `--bucket` se
resuelve del flag o de `RAYITO_BUCKET`."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from botocore.stub import ANY
from typer.testing import CliRunner

from rayito.cli import _artifact, _publish
from rayito.cli._session import Clients
from rayito.cli.app import app

from .conftest import ACCOUNT_ID, BASE_IMAGE_ARN, IMAGE_ARN, REGION, Stubs, version_item

BUILD_ROLE = f"arn:aws:iam::{ACCOUNT_ID}:role/build"
T0 = datetime(2026, 9, 1, tzinfo=UTC)


class FakeClients:
    """Lo mínimo del protocolo `PublishClients` para `desired_configuration`."""

    region = REGION
    account_id = ACCOUNT_ID
    microvms: Any = None
    s3: Any = None
    cloudformation: Any = None
    logs: Any = None


def settings(**overrides: object) -> _publish.PublishSettings:
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


def test_os_capabilities_adds_exactly_one_key() -> None:
    fake = FakeClients()
    default = _publish.desired_configuration(fake, settings(), "s3://b/k.zip")
    caps = _publish.desired_configuration(
        fake, settings(os_capabilities="ALL", image_name="rayito-base-caps"), "s3://b/k.zip"
    )
    assert "additionalOsCapabilities" not in default
    assert caps["additionalOsCapabilities"] == ["ALL"]
    delta = {key for key in caps if caps[key] != default.get(key)}
    assert delta == {"additionalOsCapabilities", "logging"}
    assert caps["logging"] == {"cloudWatch": {"logGroup": "/rayito/rayito-base-caps"}}
    assert caps["hooks"] == default["hooks"] == _publish.IMAGE_HOOKS


def test_configuration_matches_never_reuses_a_default_version_for_the_variant() -> None:
    caps = _publish.desired_configuration(
        FakeClients(), settings(os_capabilities="ALL"), "s3://b/k.zip"
    )
    default_version = {k: v for k, v in caps.items() if k != "additionalOsCapabilities"}
    assert not _publish.configuration_matches(default_version, caps)
    assert _publish.configuration_matches(caps, caps)


def test_configuration_matches_reuses_a_version_echoing_normalised_base() -> None:
    desired = _publish.desired_configuration(
        FakeClients(), settings(base_image_version="1"), "s3://b/k.zip"
    )
    echoed_by_list = {**desired, "baseImageVersion": "1.0"}
    other_base = {**desired, "baseImageVersion": "2"}
    assert _publish.configuration_matches(echoed_by_list, desired)
    assert not _publish.configuration_matches(other_base, desired)
    assert not _publish.configuration_matches(
        {**echoed_by_list, "codeArtifact": {"uri": "s3://b/other.zip"}}, desired
    )


def test_base_image_version_matches_only_numerically_equal_spellings() -> None:
    matches = _publish.base_image_version_matches
    assert matches("1.0", "1")
    assert matches("1", "1.0")
    assert matches("1.0", "1.0")
    assert not matches("1.0", "2")
    assert not matches("10", "1.0")
    assert not matches(None, "1")
    assert not matches("latest", "1")
    assert matches("latest", "latest")


def test_configuration_always_carries_base_image_version() -> None:
    configuration = _publish.desired_configuration(
        FakeClients(), settings(base_image_version="1"), "s3://b/k.zip"
    )
    assert configuration["baseImageVersion"] == "1"
    assert configuration["baseImageArn"] == BASE_IMAGE_ARN
    request = {**configuration, "description": "rayd test"}
    assert request["baseImageVersion"] == "1"


def test_hook_timeouts_stay_what_rayd_derives_its_budgets_from() -> None:
    runtime = _publish.IMAGE_HOOKS["microvmHooks"]
    assert runtime["suspendTimeoutInSeconds"] == 30
    assert runtime["resumeTimeoutInSeconds"] == 30
    assert runtime["runTimeoutInSeconds"] == 30


def test_default_names_follow_the_variant() -> None:
    assert _publish.default_image_name("full") == "rayito-base"
    assert _publish.default_image_name("slim") == "rayito-base-slim"
    assert _publish.default_image_name("poly") == "rayito-base-poly"
    assert settings(image_name="rayito-base-slim").log_group == "/rayito/rayito-base-slim"
    with pytest.raises(ValueError, match="caps"):
        _publish.default_image_name("caps")


MISMATCHES = [
    ("full", "slim"),
    ("full", "poly"),
    ("slim", "full"),
    ("slim", "poly"),
    ("poly", "full"),
    ("poly", "slim"),
]


@pytest.mark.parametrize(("built", "flag"), MISMATCHES)
def test_publish_refuses_a_mismatched_artifact(
    image_dir: Path, tmp_path: Path, built: str, flag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / f"{built}.zip"
    _artifact.write_zip(image_dir, path, built)
    with pytest.raises(SystemExit) as refused:
        _publish.require_matching_variant(settings(artifact=path, variant=flag))
    assert refused.value.code == 1
    assert f"{built} artifact but --variant {flag}" in capsys.readouterr().err


@pytest.mark.parametrize("variant", list(_artifact.VARIANTS))
def test_publish_accepts_a_matching_artifact(image_dir: Path, tmp_path: Path, variant: str) -> None:
    path = tmp_path / f"{variant}.zip"
    _artifact.write_zip(image_dir, path, variant)
    _publish.require_matching_variant(settings(artifact=path, variant=variant))


def test_missing_artifact_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        _publish.require_matching_variant(settings(artifact=tmp_path / "nope.zip"))
    assert "make image-zip" in capsys.readouterr().err


def test_variant_mismatch_stops_before_any_call(
    runner: CliRunner, clients: Clients, image_dir: Path, tmp_path: Path
) -> None:
    path = tmp_path / "full.zip"
    _artifact.write_zip(image_dir, path)
    result = runner.invoke(
        app,
        [
            "image",
            "publish",
            "--artifact",
            str(path),
            "--variant",
            "slim",
            "--base-image-version",
            "1",
            "--bucket",
            "b",
            "--build-role-arn",
            BUILD_ROLE,
        ],
        obj=clients,
    )
    assert result.exit_code == 1
    assert "full artifact but --variant slim" in result.stderr


def test_bucket_resolution(
    runner: CliRunner, clients: Clients, artifact: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RAYITO_BUCKET", raising=False)
    result = runner.invoke(
        app,
        ["image", "publish", "--artifact", str(artifact), "--base-image-version", "1"],
        obj=clients,
    )
    assert result.exit_code == 2
    assert "--bucket" in result.stderr and "RAYITO_BUCKET" in result.stderr


def stub_head_object_missing(stubs: Stubs, bucket: str, key: str) -> None:
    stubs.s3.add_client_error(
        "head_object",
        service_error_code="404",
        service_message="Not Found",
        http_status_code=404,
        expected_params={"Bucket": bucket, "Key": key},
    )


def image_response(state: str = "UPDATED") -> dict[str, Any]:
    return {"imageArn": IMAGE_ARN, "name": "rayito-base", "state": state, "createdAt": T0}


def build_item(version: str, state: str = "SUCCESSFUL") -> dict[str, Any]:
    return {
        "imageArn": IMAGE_ARN,
        "imageVersion": version,
        "buildId": "build-1",
        "buildState": state,
        "architecture": "ARM_64",
        "chipset": "GRAVITON",
        "chipsetGeneration": "3",
        "createdAt": T0,
    }


def stub_latest_build(stubs: Stubs, version: str, state: str = "SUCCESSFUL") -> None:
    stubs.microvms.add_response(
        "list_microvm_image_builds",
        {"items": [build_item(version, state)]},
        {"imageIdentifier": IMAGE_ARN, "imageVersion": version},
    )
    stubs.microvms.add_response(
        "get_microvm_image_build",
        {
            **build_item(version, state),
            "snapshotBuild": {"memorySnapshotSizeInBytes": 1, "diskSnapshotSizeInBytes": 2},
        },
        {"imageIdentifier": IMAGE_ARN, "imageVersion": version, "buildId": "build-1"},
    )


def publish_args(artifact: Path, *extra: str) -> list[str]:
    return [
        "image",
        "publish",
        "--artifact",
        str(artifact),
        "--base-image-version",
        "1",
        "--build-role-arn",
        BUILD_ROLE,
        *extra,
    ]


def test_reuse_makes_no_build_call(
    runner: CliRunner,
    clients: Clients,
    stubbed_clients: Stubs,
    artifact: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAYITO_BUCKET", "bucket")
    key = _publish.artifact_key(artifact.read_bytes())
    stubbed_clients.s3.add_response("head_object", {}, {"Bucket": "bucket", "Key": key})
    stubbed_clients.microvms.add_response(
        "get_microvm_image", image_response(), {"imageIdentifier": IMAGE_ARN}
    )
    stubbed_clients.microvms.add_response(
        "list_microvm_image_versions",
        {"items": [version_item(3, artifact_uri=f"s3://bucket/{key}", base_image_version="1.0")]},
        {"imageIdentifier": IMAGE_ARN},
    )
    stub_latest_build(stubbed_clients, "3.0")
    result = runner.invoke(app, publish_args(artifact), obj=clients)
    assert result.exit_code == 0, result.stderr
    assert "version 3.0 already built from this artifact and config; reusing" in result.stdout
    assert "skipping upload" in result.stdout
    assert result.stdout.rstrip().endswith(f"RAYITO_TEMPLATE={IMAGE_ARN}")


def test_reuse_json_prints_only_the_summary(
    runner: CliRunner, clients: Clients, stubbed_clients: Stubs, artifact: Path
) -> None:
    key = _publish.artifact_key(artifact.read_bytes())
    stubbed_clients.s3.add_response("head_object", {}, {"Bucket": "bucket", "Key": key})
    stubbed_clients.microvms.add_response(
        "get_microvm_image", image_response(), {"imageIdentifier": IMAGE_ARN}
    )
    stubbed_clients.microvms.add_response(
        "list_microvm_image_versions",
        {"items": [version_item(3, artifact_uri=f"s3://bucket/{key}", base_image_version="1.0")]},
        {"imageIdentifier": IMAGE_ARN},
    )
    stub_latest_build(stubbed_clients, "3.0")
    result = runner.invoke(
        app, ["--json", *publish_args(artifact, "--bucket", "bucket")], obj=clients
    )
    assert result.exit_code == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["imageVersion"] == "3.0" and summary["imageArn"] == IMAGE_ARN
    assert summary["snapshotBuild"]["memorySnapshotSizeInBytes"] == 1
    assert "reusing" in result.stderr


def test_failed_build_prints_state_reason_and_log_tail(
    runner: CliRunner,
    clients: Clients,
    stubbed_clients: Stubs,
    artifact: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("rayito.cli._publish.time.sleep", lambda seconds: None)
    key = _publish.artifact_key(artifact.read_bytes())
    stub_head_object_missing(stubbed_clients, "bucket", key)
    stubbed_clients.s3.add_response("put_object", {}, {"Bucket": "bucket", "Key": key, "Body": ANY})
    stubbed_clients.microvms.add_response(
        "get_microvm_image", image_response(), {"imageIdentifier": IMAGE_ARN}
    )
    stubbed_clients.microvms.add_response(
        "list_microvm_image_versions", {"items": []}, {"imageIdentifier": IMAGE_ARN}
    )
    stubbed_clients.microvms.add_response(
        "get_microvm_image", image_response(), {"imageIdentifier": IMAGE_ARN}
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
        "get_microvm_image", image_response("UPDATE_FAILED"), {"imageIdentifier": IMAGE_ARN}
    )
    stubbed_clients.microvms.add_response(
        "get_microvm_image_version",
        {
            **version_item(4, state="FAILED", status="INACTIVE"),
            "stateReason": "Validate hook invocation timed out after PT10M",
        },
        {"imageIdentifier": IMAGE_ARN, "imageVersion": "4.0"},
    )
    stub_latest_build(stubbed_clients, "4.0", "FAILED")
    stubbed_clients.logs.add_response(
        "describe_log_streams",
        {"logStreams": [{"logStreamName": "2026/09/01[4.0]build"}]},
        {
            "logGroupName": "/rayito/rayito-base",
            "orderBy": "LastEventTime",
            "descending": True,
            "limit": 3,
        },
    )
    stubbed_clients.logs.add_response(
        "get_log_events",
        {"events": [{"timestamp": 1, "message": "Step 1/9: FROM …\n"}]},
        {
            "logGroupName": "/rayito/rayito-base",
            "logStreamName": "2026/09/01[4.0]build",
            "limit": 200,
        },
    )
    result = runner.invoke(app, publish_args(artifact, "--bucket", "bucket"), obj=clients)
    assert result.exit_code == 1
    assert "Validate hook invocation timed out after PT10M" in result.stdout
    assert "Step 1/9" in result.stdout
    assert "uploaded s3://bucket/" in result.stdout


def test_upload_skipped_when_key_exists(
    clients: Clients, stubbed_clients: Stubs, artifact: Path
) -> None:
    key = _publish.artifact_key(artifact.read_bytes())
    stubbed_clients.s3.add_response("head_object", {}, {"Bucket": "bucket", "Key": key})
    lines: list[str] = []
    uri = _publish.upload_artifact(clients, settings(artifact=artifact), lines.append)
    assert uri == f"s3://bucket/{key}"
    assert lines == [f"artifact already in S3, skipping upload: {uri}"]


def test_upload_proceeds_when_head_is_forbidden_without_list_bucket(
    clients: Clients, stubbed_clients: Stubs, artifact: Path
) -> None:
    """Sin `s3:ListBucket` S3 responde 403 (no 404) a un HEAD de una clave
    ausente: el artefacto se sube igual y un permiso realmente ausente
    aflora en `put_object`."""
    key = _publish.artifact_key(artifact.read_bytes())
    stubbed_clients.s3.add_client_error(
        "head_object",
        service_error_code="403",
        service_message="Forbidden",
        http_status_code=403,
        expected_params={"Bucket": "bucket", "Key": key},
    )
    stubbed_clients.s3.add_response("put_object", {}, {"Bucket": "bucket", "Key": key, "Body": ANY})
    lines: list[str] = []
    uri = _publish.upload_artifact(clients, settings(artifact=artifact), lines.append)
    assert uri == f"s3://bucket/{key}"
    assert lines == [f"uploaded {uri} ({len(artifact.read_bytes())} bytes)"]


def test_build_role_from_stack_output(clients: Clients, stubbed_clients: Stubs) -> None:
    stubbed_clients.cloudformation.add_response(
        "describe_stacks",
        {
            "Stacks": [
                {
                    "StackName": "stack",
                    "CreationTime": T0,
                    "StackStatus": "CREATE_COMPLETE",
                    "Outputs": [{"OutputKey": "BuildRoleArn", "OutputValue": BUILD_ROLE}],
                }
            ]
        },
        {"StackName": "stack"},
    )
    assert _publish.build_role_arn(clients, settings(build_role_arn=None)) == BUILD_ROLE
