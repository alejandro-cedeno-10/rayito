"""`adapters.dynamodb.DynamoDbStore`: idempotent event writes, sandbox state
tracking (deleted once killed) and webhook-by-type filtering, against a
minimal in-memory fake of the boto3 DynamoDB Table resource.
"""

from __future__ import annotations

from typing import Any

from adapters.dynamodb import DynamoDbStore
from domain.event import LifecycleEvent


class _ConditionalCheckFailedException(Exception):
    pass


class _Exceptions:
    ConditionalCheckFailedException = _ConditionalCheckFailedException


class _Client:
    exceptions = _Exceptions


class FakeTable:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.meta = type("Meta", (), {"client": _Client})()

    def put_item(self, *, Item: dict[str, Any], ConditionExpression: str | None = None) -> None:
        key = (Item["pk"], Item["sk"])
        if ConditionExpression and key in self.items:
            raise _ConditionalCheckFailedException()
        self.items[key] = dict(Item)

    def delete_item(self, *, Key: dict[str, Any]) -> None:
        self.items.pop((Key["pk"], Key["sk"]), None)

    def scan(self, **kwargs: Any) -> dict[str, Any]:
        prefix = kwargs["ExpressionAttributeValues"][":prefix"]
        items = [item for (pk, _sk), item in self.items.items() if pk.startswith(prefix)]
        return {"Items": items}

    def query(self, **kwargs: Any) -> dict[str, Any]:
        wanted_pk = kwargs["ExpressionAttributeValues"][":pk"]
        items = [item for (pk, _sk), item in self.items.items() if pk == wanted_pk]
        return {"Items": items}


def _event(
    sandbox_id: str = "sbx-1", kind: str = "created", event_id: str = "evt-1"
) -> LifecycleEvent:
    return LifecycleEvent(
        event_id=event_id,
        sandbox_id=sandbox_id,
        kind=kind,
        generation=0,
        occurred_at_ms=1,
        image_arn="arn:test",
        image_version="1",
    )


def test_put_event_if_absent_is_idempotent() -> None:
    store = DynamoDbStore(FakeTable())
    assert store.put_event_if_absent(_event()) is True
    assert store.put_event_if_absent(_event()) is False


def test_mark_sandbox_state_deletes_on_killed() -> None:
    store = DynamoDbStore(FakeTable())
    store.mark_sandbox_state("sbx-1", _event(kind="created"))
    assert store.open_sandbox_ids() == ["sbx-1"]
    store.mark_sandbox_state("sbx-1", _event(kind="killed"))
    assert store.open_sandbox_ids() == []


def test_mark_delivery_attempted_is_idempotent_per_webhook() -> None:
    store = DynamoDbStore(FakeTable())
    assert store.mark_delivery_attempted("evt-1", "wh-1") is True
    assert store.mark_delivery_attempted("evt-1", "wh-1") is False
    assert store.mark_delivery_attempted("evt-1", "wh-2") is True


def test_webhooks_for_type_filters_by_subscribed_type() -> None:
    table = FakeTable()
    store = DynamoDbStore(table)
    table.put_item(
        Item={
            "pk": "WEBHOOK",
            "sk": "wh-1",
            "url": "https://a",
            "secret_name": "s",
            "types": ["sandbox.lifecycle.killed"],
        }
    )
    table.put_item(
        Item={
            "pk": "WEBHOOK",
            "sk": "wh-2",
            "url": "https://b",
            "secret_name": "s",
            "types": ["sandbox.lifecycle.paused"],
        }
    )
    matched = store.webhooks_for_type("sandbox.lifecycle.killed")
    assert [w.webhook_id for w in matched] == ["wh-1"]
