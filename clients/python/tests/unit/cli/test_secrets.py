"""Canarios: un JWE `CANARY-JWE` acuñado por el plano de control y un
`runHookPayload` `CANARY-PAYLOAD` en `get-microvm` no aparecen en la salida
de `doctor` (humana y `--json`), `sandbox info`, `sandbox list` ni
`image publish` (reuse)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from rayito._aws import sandbox_info_from_response
from rayito.cli import _artifact, _checks, _publish
from rayito.cli._session import Clients
from rayito.cli.app import app
from rayito.sandbox_sync import main as sync_main

from . import test_doctor as doctor_stubs
from .conftest import (
    IMAGE_ARN,
    STARTED_AT,
    FakeControlPlane,
    Stubs,
    list_item,
    sandbox_info,
    version_item,
)

CANARY_JWE = "CANARY-JWE"
CANARY_PAYLOAD = "CANARY-PAYLOAD"
IMAGE_VERSION = "17.0"


def leaky_get_microvm_response() -> dict[str, Any]:
    """Una respuesta de `get-microvm` con un `runHookPayload` colgado: la
    proyección del SDK lo descarta y nada de la CLI debe imprimirlo."""
    return {
        "microvmId": "microvm-a",
        "state": "RUNNING",
        "endpoint": "abc.lambda-microvm.us-east-1.on.aws",
        "imageArn": IMAGE_ARN,
        "imageVersion": IMAGE_VERSION,
        "maximumDurationInSeconds": 3600,
        "startedAt": STARTED_AT,
        "runHookPayload": CANARY_PAYLOAD,
    }


@pytest.fixture
def leaky_plane(fake_plane: FakeControlPlane, monkeypatch: pytest.MonkeyPatch) -> FakeControlPlane:
    fake_plane.jwe = CANARY_JWE
    fake_plane.items = [list_item("microvm-a", version=IMAGE_VERSION)]
    fake_plane.infos["microvm-a"] = sandbox_info("microvm-a", version=IMAGE_VERSION)
    monkeypatch.setattr(
        _checks,
        "probe_health",
        lambda plane, info, transport, timeout: doctor_stubs.health_response(),
    )
    monkeypatch.setattr(
        sync_main, "probe_metadata", lambda plane, info, transport, timeout: {"env": "ci"}
    )
    return fake_plane


def assert_clean(result: Any) -> None:
    for stream in (result.stdout, result.stderr):
        assert CANARY_JWE not in stream
        assert CANARY_PAYLOAD not in stream


def test_doctor_never_leaks_the_token(
    runner: CliRunner, clients: Clients, stubbed_clients: Stubs, leaky_plane: FakeControlPlane
) -> None:
    for args in (["doctor"], ["--json", "doctor"]):
        doctor_stubs.stub_all_ok(stubbed_clients)
        result = runner.invoke(app, [*args, "--bucket", doctor_stubs.BUCKET], obj=clients)
        assert result.exit_code == 0, result.stderr
        assert "token" in result.stdout and "OK" in result.stdout
        assert_clean(result)
    assert leaky_plane.tokens and leaky_plane.jwe == CANARY_JWE


def test_sandbox_commands_never_leak(
    runner: CliRunner, clients: Clients, leaky_plane: FakeControlPlane
) -> None:
    leaky_plane.infos["microvm-a"] = sandbox_info_from_response(leaky_get_microvm_response())
    for args in (["sandbox", "info", "microvm-a"], ["--json", "sandbox", "info", "microvm-a"]):
        result = runner.invoke(app, args, obj=clients)
        assert result.exit_code == 0, result.stderr
        assert "microvm-a" in result.stdout
        assert_clean(result)
    for args in (["sandbox", "list"], ["--json", "sandbox", "list"]):
        result = runner.invoke(app, args, obj=clients)
        assert result.exit_code == 0, result.stderr
        assert_clean(result)


def test_image_publish_reuse_never_leaks(
    runner: CliRunner, clients: Clients, stubbed_clients: Stubs, tmp_path: Path
) -> None:
    image_dir = tmp_path / "image"
    image_dir.mkdir()
    (image_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    artifact = tmp_path / "a.zip"
    _artifact.write_zip(image_dir, artifact)
    key = _publish.artifact_key(artifact.read_bytes())
    stubbed_clients.s3.add_response("head_object", {}, {"Bucket": "b", "Key": key})
    stubbed_clients.microvms.add_response(
        "get_microvm_image",
        {
            "imageArn": IMAGE_ARN,
            "name": "rayito-base",
            "state": "UPDATED",
            "createdAt": doctor_stubs.T0,
        },
        {"imageIdentifier": IMAGE_ARN},
    )
    stubbed_clients.microvms.add_response(
        "list_microvm_image_versions",
        {"items": [version_item(3, artifact_uri=f"s3://b/{key}")]},
        {"imageIdentifier": IMAGE_ARN},
    )
    stubbed_clients.microvms.add_response(
        "list_microvm_image_builds",
        {"items": []},
        {"imageIdentifier": IMAGE_ARN, "imageVersion": "3.0"},
    )
    result = runner.invoke(
        app,
        [
            "image",
            "publish",
            "--artifact",
            str(artifact),
            "--base-image-version",
            "1",
            "--bucket",
            "b",
            "--build-role-arn",
            "arn:aws:iam::123456789012:role/build",
        ],
        obj=clients,
    )
    assert result.exit_code == 0, result.stderr
    assert "reusing" in result.stdout
    assert_clean(result)
