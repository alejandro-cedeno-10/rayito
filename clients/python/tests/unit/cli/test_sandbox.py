"""`rayito sandbox list | info | kill`: la lista oculta los estados
terminales salvo `--all-states`, `info` imprime los metadatos y
`--no-metadata` no sondea, `kill` por id devuelve 1 si alguno no existe,
`--all` pide confirmación salvo `--yes` y es excluyente con los ids."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from typer.testing import CliRunner

from rayito.cli._session import Clients
from rayito.cli.app import app
from rayito.sandbox_sync import main as sync_main
from rayito.v1 import health_pb2

from .conftest import IMAGE_ARN, FakeControlPlane, list_item, sandbox_info

OTHER_ARN = "arn:aws:lambda:us-east-1:123456789012:microvm-image:other"


@pytest.fixture
def three_sandboxes(fake_plane: FakeControlPlane) -> FakeControlPlane:
    fake_plane.items = [
        list_item("microvm-a", "RUNNING"),
        list_item("microvm-b", "SUSPENDED", template=OTHER_ARN),
        list_item("microvm-c", "TERMINATED"),
    ]
    return fake_plane


def test_list_hides_terminal_states(
    runner: CliRunner, clients: Clients, three_sandboxes: FakeControlPlane
) -> None:
    result = runner.invoke(app, ["--json", "sandbox", "list"], obj=clients)
    assert result.exit_code == 0, result.stderr
    rows = json.loads(result.stdout)
    assert [row["sandbox_id"] for row in rows] == ["microvm-a", "microvm-b"]
    assert rows[0]["template"] == "rayito-base" and rows[0]["template_version"] == "1.0"
    assert rows[0]["started_at"] == "2026-09-15T14:39:02Z"
    assert rows[0]["age"]
    everything = runner.invoke(app, ["--json", "sandbox", "list", "--all-states"], obj=clients)
    assert len(json.loads(everything.stdout)) == 3
    filtered = runner.invoke(app, ["--json", "sandbox", "list", "--template", "other"], obj=clients)
    assert [row["sandbox_id"] for row in json.loads(filtered.stdout)] == ["microvm-b"]
    human = runner.invoke(app, ["sandbox", "list"], obj=clients)
    assert human.exit_code == 0 and "microvm-a" in human.stdout and "microvm-c" not in human.stdout
    assert three_sandboxes.tokens == []


def test_info_prints_metadata_and_no_metadata_skips_the_probe(
    runner: CliRunner,
    clients: Clients,
    fake_plane: FakeControlPlane,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_plane.infos["microvm-a"] = sandbox_info("microvm-a")
    probes: list[str] = []

    def fake_probe(
        plane: Any, info: Any, transport: Any, timeout: float
    ) -> health_pb2.HealthResponse:
        probes.append(info.sandbox_id)
        return health_pb2.HealthResponse(agent_ready=True, metadata={"env": "ci"})

    monkeypatch.setattr(sync_main, "probe_health", fake_probe)
    result = runner.invoke(app, ["--json", "sandbox", "info", "microvm-a"], obj=clients)
    assert result.exit_code == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["metadata"] == {"env": "ci"} and document["state"] == "RUNNING"
    assert document["template"] == IMAGE_ARN and document["template_name"] == "rayito-base"
    assert document["endpoint_url"].startswith("https://")
    assert probes == ["microvm-a"]
    human = runner.invoke(app, ["sandbox", "info", "microvm-a", "--no-metadata"], obj=clients)
    assert human.exit_code == 0, human.stderr
    assert "metadata: None" in human.stdout and "sandbox_id: microvm-a" in human.stdout
    assert probes == ["microvm-a"]


def test_info_unknown_id_exits_1(runner: CliRunner, clients: Clients) -> None:
    result = runner.invoke(app, ["sandbox", "info", "microvm-zz", "--no-metadata"], obj=clients)
    assert result.exit_code == 1
    assert "microvm-zz" in result.stderr


def test_kill_reports_unknown_ids(
    runner: CliRunner, clients: Clients, fake_plane: FakeControlPlane
) -> None:
    fake_plane.missing.add("microvm-b")
    result = runner.invoke(app, ["sandbox", "kill", "microvm-a", "microvm-b"], obj=clients)
    assert result.exit_code == 1
    assert "microvm-a terminated" in result.stdout and "microvm-b not found" in result.stdout
    assert fake_plane.terminated == ["microvm-a"]
    ok = runner.invoke(app, ["--json", "sandbox", "kill", "microvm-a"], obj=clients)
    assert ok.exit_code == 0
    assert json.loads(ok.stdout) == [{"sandbox_id": "microvm-a", "outcome": "terminated"}]


def test_kill_all_requires_confirmation(
    runner: CliRunner, clients: Clients, three_sandboxes: FakeControlPlane
) -> None:
    declined = runner.invoke(app, ["sandbox", "kill", "--all"], obj=clients, input="n\n")
    assert declined.exit_code == 1
    assert three_sandboxes.terminated == []
    accepted = runner.invoke(app, ["sandbox", "kill", "--all"], obj=clients, input="y\n")
    assert accepted.exit_code == 0, accepted.stderr
    assert three_sandboxes.terminated == ["microvm-a", "microvm-b"]
    three_sandboxes.terminated.clear()
    yes = runner.invoke(
        app, ["--json", "sandbox", "kill", "--all", "--yes", "--template", "other"], obj=clients
    )
    assert yes.exit_code == 0, yes.stderr
    assert json.loads(yes.stdout) == [{"sandbox_id": "microvm-b", "outcome": "terminated"}]
    assert three_sandboxes.terminated == ["microvm-b"]


def test_kill_all_with_nothing_live_exits_0(runner: CliRunner, clients: Clients) -> None:
    result = runner.invoke(app, ["--json", "sandbox", "kill", "--all", "--yes"], obj=clients)
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == []


def test_kill_all_and_ids_are_exclusive(runner: CliRunner, clients: Clients) -> None:
    result = runner.invoke(app, ["sandbox", "kill", "--all", "microvm-a"], obj=clients)
    assert result.exit_code == 2
    nothing = runner.invoke(app, ["sandbox", "kill"], obj=clients)
    assert nothing.exit_code == 2


def test_age_formats() -> None:
    from rayito.cli._console import age

    now = datetime(2026, 9, 15, 16, 2, 7, tzinfo=UTC)
    assert age(datetime(2026, 9, 15, 14, 39, 2, tzinfo=UTC), now) == "1h23m"
    assert age(datetime(2026, 9, 15, 15, 50, 0, tzinfo=UTC), now) == "12m07s"
    assert age(datetime(2026, 9, 15, 16, 2, 0, tzinfo=UTC), now) == "7s"
    assert age(now, datetime(2026, 9, 15, 16, 0, 0, tzinfo=UTC)) == "0s"
