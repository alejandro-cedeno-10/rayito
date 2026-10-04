"""`rayito template build | status | logs` (m15-templates): sobre un
`FakeBuildClients`, nunca contra AWS real."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayito._templates import _build
from rayito.cli._session import Clients
from rayito.cli.app import app

from ..fake_templates import FakeBuildClients, make_base_zip

BASE_ARN = "arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base"
BUCKET = "my-artifact-bucket"
BASE_DOCKERFILE = 'FROM scratch\nCOPY rayd /usr/local/bin/rayd\nCMD ["/usr/local/bin/rayd"]\n'
SPEC_SOURCE = (
    "from rayito import Template\n\n"
    'template = Template().from_base_image().pip_install(["pandas"])\n'
)


@pytest.fixture
def fake_clients() -> FakeBuildClients:
    clients = FakeBuildClients()
    clients.images[BASE_ARN] = {"state": "CREATED"}
    clients.versions[(BASE_ARN, "1")] = {
        "state": "SUCCESSFUL",
        "status": "ACTIVE",
        "imageVersion": "1",
        "createdAt": 1,
        "baseImageArn": "arn:aws:lambda:us-east-1:aws:microvm-image:al2023-1",
        "baseImageVersion": "1.0",
        "buildRoleArn": "arn:aws:iam::123456789012:role/rayito-build",
        "hooks": {"port": 9000},
        "codeArtifact": {"uri": f"s3://{BUCKET}/base.zip"},
    }
    clients.objects[(BUCKET, "base.zip")] = make_base_zip(BASE_DOCKERFILE)
    return clients


@pytest.fixture(autouse=True)
def _patch_clients_factory(monkeypatch: pytest.MonkeyPatch, fake_clients: FakeBuildClients) -> None:
    monkeypatch.setattr(_build, "_Clients", lambda **_kwargs: fake_clients)
    monkeypatch.setattr("rayito._templates._build.time.sleep", lambda _seconds: None)


def test_build_writes_the_spec_and_prints_the_template_id(
    tmp_path: Path, runner: CliRunner, clients: Clients
) -> None:
    spec = tmp_path / "spec.py"
    spec.write_text(SPEC_SOURCE)
    result = runner.invoke(
        app,
        ["template", "build", str(spec), "--name", "mi-template", "--bucket", BUCKET],
        obj=clients,
    )
    assert result.exit_code == 0, result.output
    assert "template_id=arn:aws:lambda:" in result.output
    assert ":microvm-image:mi-template" in result.output


def test_build_json_mode_emits_structured_output(
    tmp_path: Path, runner: CliRunner, clients: Clients
) -> None:
    spec = tmp_path / "spec.py"
    spec.write_text(SPEC_SOURCE)
    result = runner.invoke(
        app,
        ["--json", "template", "build", str(spec), "--name", "mi-template", "--bucket", BUCKET],
        obj=clients,
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["alias"] == "mi-template"
    assert "buildId" in payload


def test_build_without_a_template_variable_fails_clearly(
    tmp_path: Path, runner: CliRunner, clients: Clients
) -> None:
    spec = tmp_path / "bad_spec.py"
    spec.write_text("x = 1\n")
    result = runner.invoke(
        app,
        ["template", "build", str(spec), "--name", "mi-template", "--bucket", BUCKET],
        obj=clients,
    )
    assert result.exit_code != 0
    assert "template" in result.output.lower()


def test_status_without_a_version_resolves_the_latest(
    runner: CliRunner, clients: Clients, fake_clients: FakeBuildClients
) -> None:
    arn = "arn:aws:lambda:us-east-1:123456789012:microvm-image:mi-template"
    fake_clients.images[arn] = {"state": "CREATED"}
    fake_clients.versions[(arn, "3")] = {
        "state": "SUCCESSFUL",
        "status": "ACTIVE",
        "imageVersion": "3",
        "createdAt": 3,
    }
    result = runner.invoke(app, ["template", "status", "mi-template"], obj=clients)
    assert result.exit_code == 0, result.output
    assert "state=SUCCESSFUL" in result.output
    assert arn in result.output


def test_logs_prints_the_most_recent_lines(
    runner: CliRunner, clients: Clients, fake_clients: FakeBuildClients
) -> None:
    fake_clients.log_lines = ["#1 [1/1] RUN echo hi", "hi"]
    result = runner.invoke(app, ["template", "logs", "mi-template"], obj=clients)
    assert result.exit_code == 0, result.output
    assert "hi" in result.output
