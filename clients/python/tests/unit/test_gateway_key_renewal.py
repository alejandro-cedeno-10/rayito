"""Renovación opcional de la clave de corta duración de Bedrock
(`renew_gateway_keys`, ADR-034 D7, design D6) contra
`testdata/fleet-recipe/renewal.json`: cuándo se pregunta y cuándo se
escribe, la región de la pasarela, el valor `Bearer`, que nada se loguee y
que sin la opción no se importe el generador. Sin AWS."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError

from rayito._agent import _gateway_keys
from rayito._agent._fleet import AgentFleet
from rayito._agent._gateway_keys import (
    RENEW_NEEDS_BEDROCK,
    GatewayKeyRenewer,
    RenewableGateway,
    bearer_value,
    default_token_provider,
    gateway_region,
    renewable_gateways,
)
from rayito._agent._project_domain import AgentProject
from rayito._limits import GATEWAY_KEY_EXPIRY_SECONDS
from rayito.exceptions import InvalidArgumentException, SecretException
from tests.unit.fake_agent_fleet import FLEET_TOKEN, FleetFlavor
from tests.unit.fake_agent_project import MODEL_ID

VECTORS: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[4] / "testdata" / "fleet-recipe" / "renewal.json").read_text(
        encoding="utf-8"
    )
)
NOW = datetime(2026, 10, 22, 12, tzinfo=UTC)
SECRET_ID = "rayito/agents/support-bot/bedrock"
TOKEN: str = VECTORS["secretValue"]["token"]


def bedrock_project(preset: str = "bedrock") -> AgentProject:
    gateway: dict[str, Any] = {"preset": preset, "secret": "agents/support-bot/bedrock"}
    if preset == "bedrock":
        gateway.update({"region": "us-east-1", "models": [MODEL_ID]})
    model: dict[str, Any] = {"provider": "bedrock", "id": MODEL_ID, "gateway": "bedrock"}
    if preset == "bedrock":
        model["region"] = "us-east-1"
    else:
        model["provider"] = preset
    return AgentProject.from_dict(
        {
            "schema": "rayito.agent-project/1",
            "name": "support-bot",
            "runtime": {"kind": "deepagents"},
            "model": model,
            "gateways": {"bedrock": gateway},
        },
        base_dir=".",
    )


@dataclass
class FakeSecrets:
    """`DescribeSecret` y `PutSecretValue` de doble: la fecha del último
    cambio, las llamadas y un error opcional."""

    changed: datetime | None = None
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    error: str | None = None

    def describe_secret(self, **params: Any) -> dict[str, Any]:
        self.calls.append(("DescribeSecret", params))
        self._fail("DescribeSecret")
        return {} if self.changed is None else {"LastChangedDate": self.changed}

    def put_secret_value(self, **params: Any) -> dict[str, Any]:
        self.calls.append(("PutSecretValue", params))
        self._fail("PutSecretValue")
        return {"VersionId": params["ClientRequestToken"]}

    def _fail(self, operation: str) -> None:
        if self.error is not None:
            raise ClientError({"Error": {"Code": self.error, "Message": SECRET_ID}}, operation)

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


@dataclass
class Clock:
    now: float = 10_000.0

    def __call__(self) -> float:
        return self.now


def renewer(secrets: FakeSecrets, clock: Clock, tokens: list[str]) -> GatewayKeyRenewer:
    def provide(region: str) -> str:
        tokens.append(region)
        return TOKEN

    return GatewayKeyRenewer(
        renewable_gateways(bedrock_project()),
        token_provider=provide,
        secrets=secrets,
        clock=clock,
        now=lambda: NOW + timedelta(seconds=clock.now - 10_000.0),
    )


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda case: case["id"])
def test_the_renewal_decision_follows_the_vectors(case: dict[str, Any]) -> None:
    secrets, clock = FakeSecrets(), Clock()
    tokens: list[str] = []
    renew = renewer(secrets, clock, tokens)
    if case["processRenewedAgoSeconds"] is not None:
        secrets.changed = NOW - timedelta(days=1)
        assert renew.renew()
        clock.now += case["processRenewedAgoSeconds"]
        secrets.calls.clear()
    ago = case["secretChangedAgoSeconds"]
    current = NOW + timedelta(seconds=clock.now - 10_000.0)
    secrets.changed = None if ago is None else current - timedelta(seconds=ago)

    renewed = renew.renew()

    assert renewed is case["renew"]
    assert secrets.names == case["calls"]


@pytest.mark.parametrize("case", VECTORS["regions"], ids=lambda case: case["upstream"])
def test_the_region_comes_from_the_bedrock_upstream(case: dict[str, Any]) -> None:
    assert gateway_region(case["upstream"]) == case["region"]


def test_the_secret_gets_the_bearer_value_with_a_fresh_request_token() -> None:
    secrets, clock = FakeSecrets(changed=NOW - timedelta(hours=1)), Clock()
    tokens: list[str] = []

    renewer(secrets, clock, tokens).renew()

    _, put = secrets.calls[1]
    assert put["SecretId"] == SECRET_ID
    assert put["SecretString"] == VECTORS["secretValue"]["value"] == bearer_value(TOKEN)
    assert len(put["ClientRequestToken"]) == 36
    assert tokens == ["us-east-1"]


def test_three_creations_in_five_minutes_write_once_and_ask_once() -> None:
    secrets, clock = FakeSecrets(changed=NOW - timedelta(hours=1)), Clock()
    tokens: list[str] = []
    renew = renewer(secrets, clock, tokens)

    for _ in range(3):
        renew.renew()
        clock.now += 100

    assert secrets.names == ["DescribeSecret", "PutSecretValue"]


def test_nothing_about_the_key_or_the_secret_is_logged_or_repr(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secrets, clock = FakeSecrets(changed=NOW - timedelta(hours=1)), Clock()
    renew = renewer(secrets, clock, [])

    renew.renew()

    assert TOKEN not in caplog.text and SECRET_ID not in caplog.text
    assert SECRET_ID not in repr(renew)
    gateway = RenewableGateway("bedrock", SECRET_ID, "us-east-1")
    assert SECRET_ID not in repr(gateway)


def test_access_denied_names_the_gateway_and_not_the_secret() -> None:
    secrets = FakeSecrets(changed=NOW - timedelta(hours=1), error="AccessDeniedException")

    with pytest.raises(SecretException) as caught:
        renewer(secrets, Clock(), []).renew()

    assert caught.value.code == "permission_denied"
    assert "'bedrock'" in str(caught.value)
    assert SECRET_ID not in str(caught.value)


def test_a_project_without_a_bedrock_gateway_cannot_renew() -> None:
    with pytest.raises(InvalidArgumentException, match=RENEW_NEEDS_BEDROCK):
        AgentFleet(bedrock_project("openai"), access_token=FLEET_TOKEN, renew_gateway_keys=True)


def test_the_message_is_the_contract_one() -> None:
    contract = json.loads(
        (
            Path(__file__).resolve().parents[4] / "testdata" / "fleet-recipe" / "contract.json"
        ).read_text(encoding="utf-8")
    )
    assert contract["messages"]["renewNeedsBedrock"] == RENEW_NEEDS_BEDROCK


def test_without_the_extra_the_option_names_the_install(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "aws_bedrock_token_generator", None)

    with pytest.raises(InvalidArgumentException, match=r"rayito\[bedrock-keys\]"):
        AgentFleet(bedrock_project(), access_token=FLEET_TOKEN, renew_gateway_keys=True)


def test_without_the_option_the_generator_is_never_imported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(sys.modules, "aws_bedrock_token_generator", raising=False)

    AgentFleet(bedrock_project(), access_token=FLEET_TOKEN)

    assert "aws_bedrock_token_generator" not in sys.modules


def test_the_official_generator_signs_with_the_fleet_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}
    credentials = object()

    def provide_token(**kwargs: Any) -> str:
        seen.update(kwargs)
        seen["credentials"] = kwargs["aws_credentials_provider"].load()
        return TOKEN

    import aws_bedrock_token_generator  # type: ignore[import-untyped]

    monkeypatch.setattr(aws_bedrock_token_generator, "provide_token", provide_token)
    session = type("Session", (), {"get_credentials": lambda self: credentials})()

    token = default_token_provider(session, "us-east-1")("eu-west-1")

    assert token == TOKEN
    assert seen["region"] == "eu-west-1"
    assert seen["expiry"] == timedelta(seconds=GATEWAY_KEY_EXPIRY_SECONDS)
    assert seen["credentials"] is credentials


class CountingRenewer:
    def __init__(self, answers: list[bool]) -> None:
        self.answers = answers
        self.calls = 0

    def renew(self) -> bool:
        self.calls += 1
        return self.answers.pop(0) if self.answers else False


class RefreshSpy(dict[str, Any]):
    def __init__(self, statuses: dict[str, Any]) -> None:
        super().__init__(statuses)
        self.refreshed = 0

    def refresh(self) -> None:
        self.refreshed += 1


def test_the_fleet_renews_before_each_vm_and_refreshes_the_open_ones() -> None:
    flavor = FleetFlavor("sync")
    keys = CountingRenewer([False, True])
    fleet = flavor.fleet(
        None,
        the_project=bedrock_project(),
        renew_gateway_keys=True,
        key_renewer=keys,
    )
    fleet.start()
    opened = flavor.classes.sandboxes[fleet.sandbox_ids[0]]
    opened.gateways = RefreshSpy(opened.gateways)

    fleet.scale(min_vms=2, max_vms=2)

    assert keys.calls == 2
    assert opened.gateways.refreshed == 1


def test_the_module_never_imports_the_generator_at_import_time() -> None:
    assert "provide_token" not in vars(_gateway_keys)
