"""`rayito domain deploy | status | destroy` (m15-custom-domain): una
fachada fina de `rayito stack ... custom-domain` con los nombres de
parámetro de `CustomDomain` (`--public-domain`, `--certificate-arn`)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from rayito.cli import domain as domain_cli
from rayito.cli._session import Clients
from rayito.cli.app import app

from ..fake_custom_domain import FakeKeyValueStoreWriter
from ..fake_stacks import FakeStackProvisioner

PUBLIC_DOMAIN = "sbx.example.com"
CERTIFICATE_ARN = "arn:aws:acm:us-east-1:123456789012:certificate/abc"


@pytest.fixture
def fake_provisioner(monkeypatch: pytest.MonkeyPatch) -> FakeStackProvisioner:
    fake = FakeStackProvisioner()

    def factory(*, public_domain: str, stack_name: str | None, region: Any, session: Any) -> Any:
        from rayito._custom_domain._service import CustomDomain

        return CustomDomain(
            public_domain=public_domain,
            stack_name=stack_name,
            provisioner=fake,
            kvs_writer=FakeKeyValueStoreWriter(),
        )

    monkeypatch.setattr(domain_cli, "CustomDomain", factory)
    return fake


def test_deploy_without_yes_asks_for_confirmation_and_aborts_on_no(
    runner: CliRunner, clients: Clients, fake_provisioner: FakeStackProvisioner
) -> None:
    result = runner.invoke(
        app,
        [
            "domain",
            "deploy",
            "--public-domain",
            PUBLIC_DOMAIN,
            "--certificate-arn",
            CERTIFICATE_ARN,
        ],
        input="n\n",
        obj=clients,
    )
    assert result.exit_code != 0
    assert "Coste y activación" in result.output
    assert fake_provisioner.calls == []


def test_deploy_with_yes_creates_the_stack(
    runner: CliRunner, clients: Clients, fake_provisioner: FakeStackProvisioner
) -> None:
    result = runner.invoke(
        app,
        [
            "--json",
            "domain",
            "deploy",
            "--public-domain",
            PUBLIC_DOMAIN,
            "--certificate-arn",
            CERTIFICATE_ARN,
            "--yes",
        ],
        obj=clients,
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["state"] == "CREATE_COMPLETE"
    calls = [call[0] for call in fake_provisioner.calls]
    assert calls == ["describe", "create", "wait", "describe"]


def test_status_of_an_undeployed_stack(
    runner: CliRunner, clients: Clients, fake_provisioner: FakeStackProvisioner
) -> None:
    result = runner.invoke(
        app, ["--json", "domain", "status", "--public-domain", PUBLIC_DOMAIN], obj=clients
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) is None


def test_destroy_without_yes_aborts(
    runner: CliRunner, clients: Clients, fake_provisioner: FakeStackProvisioner
) -> None:
    result = runner.invoke(
        app, ["domain", "destroy", "--public-domain", PUBLIC_DOMAIN], input="n\n", obj=clients
    )
    assert result.exit_code != 0
    assert fake_provisioner.calls == []
