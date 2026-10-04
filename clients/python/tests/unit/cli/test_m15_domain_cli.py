"""`rayito domain deploy | status | destroy` (m15-custom-domain): una
fachada fina de `rayito stack ... custom-domain` con los nombres de
parámetro de `CustomDomain` (`--public-domain`, `--certificate-arn`)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from rayito._custom_domain._service import CUSTOM_DOMAIN_WAIT_TIMEOUT_SECONDS
from rayito._stacks._model import StackStatus
from rayito._stacks._service import OptionalStacks
from rayito.cli import domain as domain_cli
from rayito.cli._session import Clients
from rayito.cli.app import app

from ..fake_custom_domain import FakeKeyValueStoreWriter
from ..fake_stacks import FakeStackProvisioner

PUBLIC_DOMAIN = "sbx.example.com"
CERTIFICATE_ARN = "arn:aws:acm:us-east-1:123456789012:certificate/abc"
DISTRIBUTION_DOMAIN_NAME = "d111111abcdef8.cloudfront.net"


@pytest.fixture
def fake_provisioner(monkeypatch: pytest.MonkeyPatch) -> FakeStackProvisioner:
    fake = FakeStackProvisioner()

    def custom_domain_factory(
        *, public_domain: str, stack_name: str | None, region: Any, session: Any
    ) -> Any:
        from rayito._custom_domain._service import CustomDomain

        return CustomDomain(
            public_domain=public_domain,
            stack_name=stack_name,
            provisioner=fake,
            kvs_writer=FakeKeyValueStoreWriter(),
        )

    def stacks_factory(**kwargs: Any) -> OptionalStacks:
        assert set(kwargs) == {"region", "session"}
        return OptionalStacks(provisioner=fake)

    # `deploy` sigue construyendo una `CustomDomain` (valida --public-domain);
    # `status`/`destroy` van directas a `OptionalStacks` (no la necesitan).
    monkeypatch.setattr(domain_cli, "CustomDomain", custom_domain_factory)
    monkeypatch.setattr(domain_cli, "OptionalStacks", stacks_factory)
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


def test_deploy_passes_alternate_domain_names_and_prints_the_dns_target(
    runner: CliRunner, clients: Clients, fake_provisioner: FakeStackProvisioner
) -> None:
    # Una pila con salida `DistributionDomainName`: deploy() la actualiza y
    # la CLI dice a qué apuntar el CNAME.
    fake_provisioner.stacks["rayito-custom-domain"] = StackStatus(
        name="rayito-custom-domain",
        state="CREATE_COMPLETE",
        outputs={"DistributionDomainName": DISTRIBUTION_DOMAIN_NAME},
    )
    result = runner.invoke(
        app,
        [
            "domain",
            "deploy",
            "--public-domain",
            PUBLIC_DOMAIN,
            "--certificate-arn",
            CERTIFICATE_ARN,
            "--alternate-domain-name",
            "8000-a.sbx.example.com",
            "--alternate-domain-name",
            "8000-b.sbx.example.com",
            "--yes",
        ],
        obj=clients,
    )
    assert result.exit_code == 0, result.output
    assert fake_provisioner.parameters["rayito-custom-domain"]["AlternateDomainNames"] == (
        "8000-a.sbx.example.com,8000-b.sbx.example.com"
    )
    assert (
        f"8000-a.sbx.example.com, 8000-b.sbx.example.com a {DISTRIBUTION_DOMAIN_NAME}"
        in result.output
    )


def test_deploy_without_alternate_names_prints_the_wildcard_dns_target(
    runner: CliRunner, clients: Clients, fake_provisioner: FakeStackProvisioner
) -> None:
    fake_provisioner.stacks["rayito-custom-domain"] = StackStatus(
        name="rayito-custom-domain",
        state="CREATE_COMPLETE",
        outputs={"DistributionDomainName": DISTRIBUTION_DOMAIN_NAME},
    )
    result = runner.invoke(
        app,
        [
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
    assert "AlternateDomainNames" not in fake_provisioner.parameters["rayito-custom-domain"]
    assert f"*.{PUBLIC_DOMAIN} a {DISTRIBUTION_DOMAIN_NAME}" in result.output


def test_status_of_an_undeployed_stack(
    runner: CliRunner, clients: Clients, fake_provisioner: FakeStackProvisioner
) -> None:
    result = runner.invoke(app, ["--json", "domain", "status"], obj=clients)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) is None


def test_destroy_without_yes_aborts(
    runner: CliRunner, clients: Clients, fake_provisioner: FakeStackProvisioner
) -> None:
    result = runner.invoke(app, ["domain", "destroy"], input="n\n", obj=clients)
    assert result.exit_code != 0
    assert fake_provisioner.calls == []


def test_destroy_with_yes_deletes_the_stack_and_prints_what_is_retained(
    runner: CliRunner, clients: Clients, fake_provisioner: FakeStackProvisioner
) -> None:
    result = runner.invoke(app, ["domain", "destroy", "--yes"], obj=clients)
    assert result.exit_code == 0, result.output
    assert "Al borrar:" in result.output
    assert [call[0] for call in fake_provisioner.calls] == ["delete", "wait"]
    # Hallazgo del review de PR #74: `destroy` va directo a `OptionalStacks`
    # (sin pasar por `CustomDomain.destroy()`), así que la CLI es quien debe
    # pasar explícitamente el timeout propio de esta función.
    assert fake_provisioner.wait_timeouts == [CUSTOM_DOMAIN_WAIT_TIMEOUT_SECONDS]
