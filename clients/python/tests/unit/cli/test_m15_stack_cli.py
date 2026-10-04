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
    result = runner.invoke(app, ["stack", "deploy", "efs-volumes", "--yes"], obj=clients)
    assert result.exit_code != 0
    assert fake_provisioner.calls == []


def test_redeploy_prints_only_the_parameters_that_change(
    runner: CliRunner, clients: Clients, fake_provisioner: FakeStackProvisioner
) -> None:
    first = runner.invoke(
        app,
        ["stack", "deploy", "s3-mounts", "--param", "BucketName=b", "--param", "Prefixes=t/*"],
        input="y\n",
        obj=clients,
    )
    assert first.exit_code == 0, first.output
    assert "Prefixes: '(sin valor)' -> 't/*'" in first.output
    second = runner.invoke(
        app,
        ["stack", "deploy", "s3-mounts", "--param", "ReadOnly=false"],
        input="y\n",
        obj=clients,
    )
    assert second.exit_code == 0, second.output
    assert "ReadOnly: 'true' -> 'false'" in second.output
    assert "Prefixes" not in second.output.split("Parámetros que cambian")[1]
    assert fake_provisioner.stacks["rayito-s3-mounts"].parameters["Prefixes"] == "t/*"


def test_deploy_without_yes_asks_for_confirmation_and_aborts_on_no(
    runner: CliRunner, clients: Clients, fake_provisioner: FakeStackProvisioner
) -> None:
    result = runner.invoke(app, ["stack", "deploy", "metadata-index"], input="n\n", obj=clients)
    assert result.exit_code != 0
    assert "Coste y activación" in result.output
    # Only the read that shows which parameters would change; nothing deployed.
    assert [call[0] for call in fake_provisioner.calls] == ["describe"]


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


def test_domain_stub_subapp_is_hidden_from_the_top_level_help(runner: CliRunner) -> None:
    # m15-events-webhooks and m15-templates implement their own `events` and
    # `template` sub-apps for real; `domain` is the one pending stub left, and
    # stays out of `rayito --help` until m15-custom-domain ships it.
    top = runner.invoke(app, ["--help"])
    assert top.exit_code == 0, top.output
    assert "domain" not in top.output
    result = runner.invoke(app, ["domain", "--help"])
    assert result.exit_code == 0, result.output
    assert "m15-custom-domain" in result.output


def test_template_subapp_is_implemented_for_real(runner: CliRunner) -> None:
    result = runner.invoke(app, ["template", "--help"])
    assert result.exit_code == 0, result.output
    assert "build" in result.output
    assert "status" in result.output
    assert "logs" in result.output
    assert "Pendiente" not in result.output


def test_events_subapp_left_the_pending_stub_in_m15_events_webhooks(runner: CliRunner) -> None:
    # `events` is no longer a stub (m15-events-webhooks); see
    # `test_m15_events_webhooks_cli.py` for its real subcommands.
    result = runner.invoke(app, ["events", "--help"])
    assert result.exit_code == 0, result.output
    assert "m15-events-webhooks" not in result.output
    assert "webhook" in result.output
