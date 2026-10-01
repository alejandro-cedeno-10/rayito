"""`LifecycleEvents` (sync): construirlo no llama a AWS; `deploy`/`status`/
`destroy` delegan en `OptionalStacks`; `register_webhook`/`list_webhooks`/
`delete_webhook`/`get_events` son directas sobre DynamoDB (fakes); `_build_section`
deriva `k_sbx` correctamente a partir del secreto del stack.
"""

from __future__ import annotations

import pytest

from rayito import InvalidArgumentException, LifecycleEvents, WebhookException
from rayito._lifecycle_events._keys import derive_sandbox_key
from rayito._stacks._service import OptionalStacks

from .fake_lifecycle_events_aws import FakeAwsSession, FakeSecretsClient, FakeTable
from .fake_stacks import FakeStackProvisioner

STACK_KEY = b"the-stack-wide-hmac-secret"
SECRET_ID = "arn:aws:secretsmanager:us-east-1:123456789012:secret:stack-key-abc123"
TABLE_NAME = "rayito-events-webhooks-table"


def _events() -> LifecycleEvents:
    table = FakeTable()
    secrets = FakeSecretsClient(secrets={SECRET_ID: STACK_KEY})
    session = FakeAwsSession(table=table, secrets_client=secrets)
    provisioner = FakeStackProvisioner()
    ev = LifecycleEvents(
        region="us-east-1", session=session, stacks=OptionalStacks(provisioner=provisioner)
    )
    # Blanco: nos saltamos `deploy()` real para los tests de plano de datos,
    # que sólo necesitan saber dónde está la tabla/el secreto.
    ev._table_name = TABLE_NAME
    ev._stack_key_secret_id = SECRET_ID
    return ev


def test_constructing_lifecycle_events_calls_no_aws(monkeypatch: pytest.MonkeyPatch) -> None:
    import boto3

    calls: list[str] = []
    monkeypatch.setattr(
        boto3.session.Session,
        "client",
        lambda self, *a, **k: calls.append(a[0]) or object(),  # type: ignore[func-returns-value]
    )
    LifecycleEvents()
    assert calls == []


def test_register_then_list_then_delete_webhook() -> None:
    ev = _events()
    webhook = ev.register_webhook(
        "https://hooks.example.com/rayito",
        secret_name="mi-webhook",
        types=["sandbox.lifecycle.killed"],
    )
    assert webhook.url == "https://hooks.example.com/rayito"
    listed = ev.list_webhooks()
    assert [w.webhook_id for w in listed] == [webhook.webhook_id]
    ev.delete_webhook(webhook.webhook_id)
    assert ev.list_webhooks() == []


def test_register_webhook_requires_https() -> None:
    ev = _events()
    with pytest.raises(InvalidArgumentException):
        ev.register_webhook(
            "http://hooks.example.com", secret_name="x", types=["sandbox.lifecycle.killed"]
        )


def test_register_webhook_rejects_unknown_type() -> None:
    ev = _events()
    with pytest.raises(InvalidArgumentException):
        ev.register_webhook(
            "https://hooks.example.com", secret_name="x", types=["sandbox.lifecycle.exploded"]
        )


def test_get_events_filters_by_type_and_respects_limit() -> None:
    ev = _events()
    table = ev._table()
    for index, kind in enumerate(["created", "paused", "killed"]):
        table.put_item(
            Item={
                "pk": "EVENT#sbx-1",
                "sk": f"{index:020d}#evt-{index}",
                "gsi1pk": "EVENT",
                "gsi1sk": f"{index:020d}#evt-{index}",
                "event_id": f"evt-{index}",
                "sandbox_id": "sbx-1",
                "kind": kind,
                "generation": 0,
                "occurred_at_ms": index,
                "image_arn": "arn:test",
                "image_version": "1",
            }
        )
    events = ev.get_events(sandbox_id="sbx-1", types=["sandbox.lifecycle.killed"])
    assert [e.kind for e in events] == ["killed"]
    assert len(ev.get_events(sandbox_id="sbx-1", limit=2)) == 2


def test_get_events_rejects_limit_above_the_maximum() -> None:
    ev = _events()
    with pytest.raises(InvalidArgumentException):
        ev.get_events(limit=101)


def test_get_events_rejects_an_invalid_order() -> None:
    ev = _events()
    with pytest.raises(InvalidArgumentException):
        ev.get_events(order="sideways")


def test_build_section_derives_the_sandbox_key_from_the_stack_secret() -> None:
    ev = _events()
    section = ev._build_section(sandbox_id="sbx-1", image_arn="arn:test", image_version="1")
    assert section.sandbox_key == derive_sandbox_key(STACK_KEY, "sbx-1")
    assert section.sandbox_id == "sbx-1"
    assert section.section == "lifecycle_events"
    assert section.required_flag == "lifecycle_events"


def test_data_plane_methods_require_deploy_first() -> None:
    table = FakeTable()
    secrets = FakeSecretsClient()
    session = FakeAwsSession(table=table, secrets_client=secrets)
    ev = LifecycleEvents(
        region="us-east-1",
        session=session,
        stacks=OptionalStacks(provisioner=FakeStackProvisioner()),
    )
    with pytest.raises(WebhookException, match="deploy"):
        ev.list_webhooks()
