"""`rayito domain deploy | status | destroy` (m15-custom-domain): una
fachada fina de `rayito stack ... custom-domain` con los nombres de
parámetro de `CustomDomain` (`--public-domain`, `--certificate-arn`)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from rayito._custom_domain._service import CUSTOM_DOMAIN_WAIT_TIMEOUT_SECONDS
from rayito._stacks._model import StackEvent, StackStatus
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
    assert fake_provisioner.sent_parameters["AlternateDomainNames"] == (
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
    assert "AlternateDomainNames" not in fake_provisioner.sent_parameters
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


def _deploy_arguments(*extra: str) -> list[str]:
    return [
        "domain",
        "deploy",
        "--public-domain",
        PUBLIC_DOMAIN,
        "--certificate-arn",
        CERTIFICATE_ARN,
        "--yes",
        *extra,
    ]


def test_deploy_sends_the_repeatable_tags_next_to_the_fixed_ones(
    runner: CliRunner, clients: Clients, fake_provisioner: FakeStackProvisioner
) -> None:
    result = runner.invoke(app, _deploy_arguments("--tag", "team=a", "--tag", "env=b"), obj=clients)
    assert result.exit_code == 0, result.output
    assert fake_provisioner.sent_tags["team"] == "a"
    assert fake_provisioner.sent_tags["env"] == "b"
    assert {"rayito:component", "rayito:managed-by", "rayito:sdk-version"} <= set(
        fake_provisioner.sent_tags
    )


def test_deploy_rejects_a_malformed_tag_before_any_call(
    runner: CliRunner, clients: Clients, fake_provisioner: FakeStackProvisioner
) -> None:
    result = runner.invoke(app, _deploy_arguments("--tag", "no-equals"), obj=clients)
    assert result.exit_code == 2
    assert fake_provisioner.calls == []


def test_a_rollback_prints_the_first_failed_resource_on_stderr_and_still_fails(
    runner: CliRunner, clients: Clients, fake_provisioner: FakeStackProvisioner
) -> None:
    fake_provisioner.fail_wait_state = "ROLLBACK_COMPLETE"
    fake_provisioner.events["rayito-custom-domain"] = [
        StackEvent("Distribution", "CREATE_FAILED", "synthetic reason"),
        StackEvent("rayito-custom-domain", "CREATE_IN_PROGRESS", "User Initiated"),
    ]
    result = runner.invoke(app, _deploy_arguments(), obj=clients)
    assert result.exit_code == 1
    assert "  motivo: Distribution (CREATE_FAILED): synthetic reason" in result.stderr
    assert "terminó en ROLLBACK_COMPLETE" in result.stderr
    assert "motivo" not in result.stdout
    assert ("failure_reason", "rayito-custom-domain") in fake_provisioner.calls


def test_a_rollback_with_json_keeps_stdout_empty(
    runner: CliRunner, clients: Clients, fake_provisioner: FakeStackProvisioner
) -> None:
    fake_provisioner.fail_wait_state = "ROLLBACK_COMPLETE"
    fake_provisioner.events["rayito-custom-domain"] = [
        StackEvent("Distribution", "CREATE_FAILED", "synthetic reason"),
    ]
    result = runner.invoke(app, ["--json", *_deploy_arguments()], obj=clients)
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "  motivo: Distribution" in result.stderr


def test_a_rejected_create_call_prints_no_reason_and_never_reads_it(
    runner: CliRunner, clients: Clients, fake_provisioner: FakeStackProvisioner
) -> None:
    fake_provisioner.fail_call = True
    fake_provisioner.events["rayito-custom-domain"] = [
        StackEvent("Distribution", "CREATE_FAILED", "reason of an older operation"),
    ]
    result = runner.invoke(app, _deploy_arguments(), obj=clients)
    assert result.exit_code == 1
    assert "motivo" not in result.stderr
    assert all(call[0] != "failure_reason" for call in fake_provisioner.calls)


def test_a_rejected_update_call_on_a_rolled_back_stack_prints_no_old_reason(
    runner: CliRunner, clients: Clients, fake_provisioner: FakeStackProvisioner
) -> None:
    fake_provisioner.stacks["rayito-custom-domain"] = StackStatus(
        name="rayito-custom-domain", state="UPDATE_ROLLBACK_COMPLETE"
    )
    fake_provisioner.events["rayito-custom-domain"] = [
        StackEvent("Distribution", "UPDATE_FAILED", "reason of an older operation"),
    ]
    fake_provisioner.fail_call = True
    result = runner.invoke(app, _deploy_arguments(), obj=clients)
    assert result.exit_code == 1
    assert "motivo" not in result.stderr
    assert all(call[0] != "failure_reason" for call in fake_provisioner.calls)


@pytest.mark.parametrize("state", ["UPDATE_ROLLBACK_FAILED", "DELETE_FAILED"])
def test_a_wait_ending_in_a_non_explainable_failure_prints_no_reason(
    state: str,
    runner: CliRunner,
    clients: Clients,
    fake_provisioner: FakeStackProvisioner,
) -> None:
    fake_provisioner.fail_wait_state = state
    fake_provisioner.events["rayito-custom-domain"] = [
        StackEvent("Distribution", "UPDATE_FAILED", "reason that may be older"),
    ]
    result = runner.invoke(app, _deploy_arguments(), obj=clients)
    assert result.exit_code == 1
    assert f"terminó en {state}" in result.stderr
    assert "motivo" not in result.stderr
    assert all(call[0] != "failure_reason" for call in fake_provisioner.calls)
