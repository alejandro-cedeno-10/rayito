"""`rayito stack list | deploy | status | destroy` (M15 foundations):
`list` no hace ninguna llamada a AWS, `deploy` imprime el coste y pide
confirmación salvo `--yes`, un componente sin plantilla se rechaza antes de
tocar el provisioner falso."""

from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from rayito._stacks._service import OptionalStacks
from rayito.cli import stack as stack_cli
from rayito.cli._session import Clients
from rayito.cli.app import app

from ..fake_stacks import FakeStackProvisioner


@pytest.fixture
def fake_provisioner(monkeypatch: pytest.MonkeyPatch) -> FakeStackProvisioner:
    fake = FakeStackProvisioner()

    def factory(**kwargs: Any) -> OptionalStacks:
        assert set(kwargs) == {"region", "session"}
        return OptionalStacks(provisioner=fake)

    monkeypatch.setattr(stack_cli, "OptionalStacks", factory)
    return fake


def test_list_makes_no_provisioner_call(
    runner: CliRunner, clients: Clients, fake_provisioner: FakeStackProvisioner
) -> None:
    result = runner.invoke(app, ["--json", "stack", "list"], obj=clients)
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    names = {row["name"] for row in rows}
    assert "metadata-index" in names
    assert "s3-mounts" in names
    assert fake_provisioner.calls == []


def test_deploying_an_unsupported_component_fails_before_any_call(
    runner: CliRunner, clients: Clients, fake_provisioner: FakeStackProvisioner
) -> None:
    result = runner.invoke(app, ["stack", "deploy", "s3-mounts", "--yes"], obj=clients)
    assert result.exit_code != 0
    assert fake_provisioner.calls == []


def test_deploy_without_yes_asks_for_confirmation_and_aborts_on_no(
    runner: CliRunner, clients: Clients, fake_provisioner: FakeStackProvisioner
) -> None:
    result = runner.invoke(app, ["stack", "deploy", "metadata-index"], input="n\n", obj=clients)
    assert result.exit_code != 0
    assert "Coste y activación" in result.output
    assert fake_provisioner.calls == []


def test_deploy_with_yes_creates_the_stack(
    runner: CliRunner, clients: Clients, fake_provisioner: FakeStackProvisioner
) -> None:
    result = runner.invoke(
        app, ["--json", "stack", "deploy", "metadata-index", "--yes"], obj=clients
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["state"] == "CREATE_COMPLETE"
    assert [call[0] for call in fake_provisioner.calls] == [
        "describe",
        "create",
        "wait",
        "describe",
    ]


def test_destroy_with_yes_deletes_the_stack(
    runner: CliRunner, clients: Clients, fake_provisioner: FakeStackProvisioner
) -> None:
    result = runner.invoke(
        app, ["--json", "stack", "destroy", "metadata-index", "--yes"], obj=clients
    )
    assert result.exit_code == 0, result.output
    assert [call[0] for call in fake_provisioner.calls] == ["delete", "wait"]


def test_status_of_an_undeployed_component_is_none(
    runner: CliRunner, clients: Clients, fake_provisioner: FakeStackProvisioner
) -> None:
    result = runner.invoke(app, ["--json", "stack", "status", "secrets-access"], obj=clients)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) is None


def test_events_template_domain_stub_subapps_print_pending_help(runner: CliRunner) -> None:
    for name, slug in (
        ("events", "m15-events-webhooks"),
        ("template", "m15-templates"),
        ("domain", "m15-custom-domain"),
    ):
        result = runner.invoke(app, [name, "--help"])
        assert result.exit_code == 0, result.output
        assert slug in result.output
