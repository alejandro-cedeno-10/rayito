"""`adapters.dynamodb.DynamoDbStore`: idempotent event writes, sandbox state
that only moves forward (a `killed` tombstone that nothing reopens), the
delivery status that never loses a delivery, and webhook-by-type
filtering, against
`conftest.FakeTable`, the in-memory fake of the boto3 DynamoDB Table
resource shared with the handler tests.
"""

from __future__ import annotations

from adapters.dynamodb import DynamoDbStore
from conftest import FakeTable
from domain.event import LifecycleEvent
from ports import OpenSandbox


def _event(
    sandbox_id: str = "sbx-1",
    kind: str = "created",
    event_id: str = "evt-1",
    occurred_at_ms: int = 1,
    generation: int = 0,
) -> LifecycleEvent:
    return LifecycleEvent(
        event_id=event_id,
        sandbox_id=sandbox_id,
        kind=kind,
        generation=generation,
        occurred_at_ms=occurred_at_ms,
        image_arn="arn:test",
        image_version="1",
        kill_reason="request" if kind == "killed" else None,
    )


def test_put_event_if_absent_is_idempotent() -> None:
    store = DynamoDbStore(FakeTable())
    assert store.put_event_if_absent(_event()) is True
    assert store.put_event_if_absent(_event()) is False


def test_a_killed_sandbox_is_closed_and_stays_closed() -> None:
    store = DynamoDbStore(FakeTable())
    store.record_sandbox_state(_event(kind="created", occurred_at_ms=1))
    assert [s.sandbox_id for s in store.open_sandboxes()] == ["sbx-1"]
    store.record_sandbox_state(_event(kind="killed", event_id="evt-k", occurred_at_ms=5))
    assert store.open_sandboxes() == []
    # A late or duplicate line after `killed` must not reopen it (which
    # would make the reconciler synthesize a second, spurious `killed`).
    store.record_sandbox_state(_event(kind="resumed", event_id="evt-late", occurred_at_ms=9))
    assert store.open_sandboxes() == []


def test_sandbox_state_never_moves_back_to_an_older_event() -> None:
    store = DynamoDbStore(FakeTable())
    store.record_sandbox_state(_event(kind="resumed", occurred_at_ms=10, generation=2))
    store.record_sandbox_state(_event(kind="paused", event_id="evt-old", occurred_at_ms=4))
    assert store.open_sandboxes() == [
        OpenSandbox(sandbox_id="sbx-1", generation=2, image_arn="arn:test", image_version="1")
    ]


def test_a_delivery_is_claimed_again_until_it_is_delivered() -> None:
    store = DynamoDbStore(FakeTable())
    assert store.claim_delivery("evt-1", "wh-1") is True
    # A crash between claim and finish leaves `attempting`: claimable again.
    assert store.claim_delivery("evt-1", "wh-1") is True
    store.finish_delivery("evt-1", "wh-1", delivered=False)
    assert store.claim_delivery("evt-1", "wh-1") is True
    store.finish_delivery("evt-1", "wh-1", delivered=True)
    assert store.claim_delivery("evt-1", "wh-1") is False
    assert store.claim_delivery("evt-1", "wh-2") is True


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
