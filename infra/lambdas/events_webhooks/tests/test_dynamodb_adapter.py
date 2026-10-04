"""`adapters.dynamodb.DynamoDbStore`: idempotent event writes, sandbox state
that only moves forward (a `killed` tombstone that nothing reopens, written
under an optimistic-concurrency check, and a sparse `open` index instead of
a table scan), the
delivery status that never loses a delivery, and webhook-by-type
filtering, against
`conftest.FakeTable`, the in-memory fake of the boto3 DynamoDB Table
resource shared with the handler tests.
"""

from __future__ import annotations

from typing import Any

import pytest
from adapters.dynamodb import AdmissionContended, DynamoDbStore
from conftest import FakeTable
from domain.admission import REASON_INVALID_TRANSITION, Admitted, Refused
from domain.event import LifecycleEvent
from ports import OpenSandbox

NOW_MS = 1_790_000_000_000


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
    table = FakeTable()
    store = DynamoDbStore(table)
    store.admit(_event(kind="created", occurred_at_ms=1), NOW_MS)
    assert [s.sandbox_id for s in store.open_sandboxes()] == ["sbx-1"]
    assert table.items[("STATE#sbx-1", "STATE")]["open_pk"] == "OPEN"
    store.admit(_event(kind="killed", event_id="evt-k", occurred_at_ms=5), NOW_MS)
    assert store.open_sandboxes() == []
    tombstone = table.items[("STATE#sbx-1", "STATE")]
    assert "open_pk" not in tombstone
    assert "expires_at" in tombstone
    # A late or duplicate line after `killed` must not reopen it (which
    # would make the reconciler synthesize a second, spurious `killed`).
    late = store.admit(_event(kind="resumed", event_id="evt-late", generation=1), NOW_MS)
    assert late == Refused(REASON_INVALID_TRANSITION)
    assert store.open_sandboxes() == []


def test_sandbox_state_never_moves_back_to_an_older_position() -> None:
    store = DynamoDbStore(FakeTable())
    store.admit(_event(kind="resumed", occurred_at_ms=10, generation=2), NOW_MS)
    old = store.admit(_event(kind="paused", event_id="evt-old", generation=1), NOW_MS)
    assert old == Refused(REASON_INVALID_TRANSITION)
    assert store.open_sandboxes() == [
        OpenSandbox(sandbox_id="sbx-1", generation=2, image_arn="arn:test", image_version="1")
    ]


def test_open_sandboxes_queries_the_sparse_index_never_scans() -> None:
    # The fake has no `scan` at all: the reconciler's cost follows the
    # number of open sandboxes, never every EVENT/DELIVERY row.
    table = FakeTable()
    store = DynamoDbStore(table)
    store.admit(_event(sandbox_id="sbx-open"), NOW_MS)
    store.admit(_event(sandbox_id="sbx-closed"), NOW_MS)
    store.admit(_event(sandbox_id="sbx-closed", kind="killed", event_id="evt-k"), NOW_MS)
    assert store.put_event_if_absent(_event(sandbox_id="sbx-open")) is True
    assert not hasattr(table, "scan")
    assert [s.sandbox_id for s in store.open_sandboxes()] == ["sbx-open"]


def test_a_row_written_before_admission_existed_is_moved_forward() -> None:
    table = FakeTable()
    table.put_item(
        Item={
            "pk": "STATE#sbx-1",
            "sk": "STATE",
            "sandbox_id": "sbx-1",
            "last_kind": "created",
            "last_seen_ms": 1,
            "generation": 0,
            "image_arn": "arn:test",
            "image_version": "1",
        }
    )
    store = DynamoDbStore(table)
    assert isinstance(store.admit(_event(kind="paused", event_id="evt-p"), NOW_MS), Admitted)
    row = table.items[("STATE#sbx-1", "STATE")]
    assert (row["last_kind"], row["revision"], row["open_pk"]) == ("paused", 1, "OPEN")


def test_admit_retries_when_another_writer_changed_the_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = FakeTable()
    store = DynamoDbStore(table)
    store.admit(_event(kind="created"), NOW_MS)
    key = {"pk": "STATE#sbx-1", "sk": "STATE"}
    stale = table.get_item(Key=key, ConsistentRead=True)
    store.admit(_event(kind="killed", event_id="evt-k"), NOW_MS)
    real_get = table.get_item
    stale_reads = iter([stale])

    def get_item(**kwargs: Any) -> dict[str, Any]:
        return next(stale_reads, None) or real_get(**kwargs)

    monkeypatch.setattr(table, "get_item", get_item)
    # The first read still saw `created` (the reconciler closed the sandbox
    # right after it): the conditional write fails, the re-read sees the
    # tombstone and refuses.
    paused = store.admit(_event(kind="paused", event_id="evt-p"), NOW_MS)
    assert paused == Refused(REASON_INVALID_TRANSITION)
    assert table.items[("STATE#sbx-1", "STATE")]["last_kind"] == "killed"


def test_admit_gives_up_after_bounded_contention(monkeypatch: pytest.MonkeyPatch) -> None:
    table = FakeTable()
    store = DynamoDbStore(table)
    store.admit(_event(kind="created"), NOW_MS)

    def always_conflicts(**_kwargs: Any) -> None:
        raise table.meta.client.exceptions.ConditionalCheckFailedException()

    monkeypatch.setattr(table, "put_item", always_conflicts)
    with pytest.raises(AdmissionContended):
        store.admit(_event(kind="paused", event_id="evt-p"), NOW_MS)


def test_a_delivery_is_claimed_again_until_it_is_delivered() -> None:
    store = DynamoDbStore(FakeTable())
    event = _event()
    assert store.claim_delivery(event, "wh-1") is True
    # A crash between claim and finish leaves `attempting`: claimable again.
    assert store.claim_delivery(event, "wh-1") is True
    store.finish_delivery(event, "wh-1", delivered=False)
    assert store.claim_delivery(event, "wh-1") is True
    store.finish_delivery(event, "wh-1", delivered=True)
    assert store.claim_delivery(event, "wh-1") is False
    assert store.claim_delivery(event, "wh-2") is True


def test_one_sandboxs_delivery_never_marks_another_sandboxs_as_done() -> None:
    # A sandbox chooses its own event ids: if it reuses another sandbox's
    # (or one the reconciler will synthesize for it), the dedupe row it
    # gets marked `delivered` must be its own, never the victim's.
    table = FakeTable()
    store = DynamoDbStore(table)
    attacker = _event(sandbox_id="sbx-attacker", event_id="synthetic-0123")
    victim = _event(sandbox_id="sbx-victim", event_id="synthetic-0123", kind="killed")
    assert store.claim_delivery(attacker, "wh-1") is True
    store.finish_delivery(attacker, "wh-1", delivered=True)
    assert store.claim_delivery(victim, "wh-1") is True
    assert ("DELIVERY#sbx-victim#synthetic-0123", "wh-1") in table.items


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
