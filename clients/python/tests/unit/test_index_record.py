"""Núcleo puro del índice de metadatos (M14): la fila (`record_for`, con su
TTL determinista y sólo las claves permitidas), la unión con
`list-microvms` (`join_index`) y los filtros/huella del listado con índice."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from rayito._index import (
    RECORD_ATTRIBUTES,
    SDK_TAG,
    IndexRecord,
    chunks,
    join_index,
    record_for,
)
from rayito._listing_base import ListFilters, listing_request
from rayito._models import SandboxInfo, SandboxListItem
from rayito.exceptions import InvalidArgumentException

from .fake_dynamodb import fake_index

IMAGE = "arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base"
OTHER_IMAGE = "arn:aws:lambda:us-east-1:123456789012:microvm-image:other"
STARTED = datetime(2026, 9, 30, 12, 0, 0, 250_000, tzinfo=UTC)
STARTED_MS = round(STARTED.timestamp() * 1000)
FORBIDDEN_FRAGMENTS = ("token", "env", "secret", "payload", "jwe", "hash")
GOLDEN_INDEX_FINGERPRINT = "ae65162235b32204"


def info(sandbox_id: str = "microvm-1", *, duration: int = 900) -> SandboxInfo:
    return SandboxInfo(
        sandbox_id=sandbox_id,
        state="PENDING",
        endpoint="example.invalid",
        template=IMAGE,
        template_version="3",
        started_at=STARTED,
        maximum_duration_seconds=duration,
    )


def listed(
    sandbox_id: str,
    state: str = "SUSPENDED",
    *,
    image: str = IMAGE,
    started: datetime = STARTED,
) -> SandboxListItem:
    return SandboxListItem(
        sandbox_id=sandbox_id,
        state=state,
        template=image,
        template_version="3",
        started_at=started,
    )


def row(sandbox_id: str, metadata: dict[str, str], *, expires_at: int | None = None) -> IndexRecord:
    record = record_for(info(sandbox_id), metadata)
    if expires_at is None:
        return record
    return IndexRecord(
        sandbox_id=record.sandbox_id,
        image_arn=record.image_arn,
        image_version=record.image_version,
        started_at_ms=record.started_at_ms,
        metadata=record.metadata,
        sdk=record.sdk,
        expires_at=expires_at,
    )


NOW = STARTED.timestamp() + 60


def test_record_for_computes_a_deterministic_ttl() -> None:
    record = record_for(info(duration=900), {"user": "42"}, 3600)
    assert record.started_at_ms == STARTED_MS
    assert record.expires_at == STARTED_MS // 1000 + 900 + 3600
    assert record == record_for(info(duration=900), {"user": "42"}, 3600)
    assert record.sdk == SDK_TAG
    assert SDK_TAG.startswith("py/")
    assert record_for(info(), None).metadata == {}


def test_record_item_keys_are_the_allow_list_and_never_carry_credentials() -> None:
    item = record_for(info(), {"user": "42"}).to_item()
    assert set(item) == RECORD_ATTRIBUTES
    assert not [key for key in item for bad in FORBIDDEN_FRAGMENTS if bad in key.lower()]
    assert item["pk"] == {"S": "microvm-1"}
    assert item["metadata"] == {"M": {"user": {"S": "42"}}}
    assert item["expires_at"]["N"].isdigit()
    assert IndexRecord.from_item(item) == record_for(info(), {"user": "42"})


def test_record_repr_never_shows_metadata_values() -> None:
    text = repr(record_for(info(), {"user": "value-that-must-not-leak"}))
    assert "value-that-must-not-leak" not in text
    assert "user" in text


def test_malformed_rows_are_treated_as_missing() -> None:
    assert IndexRecord.from_item({"pk": {"S": "x"}}) is None
    item = record_for(info(), {"a": "1"}).to_item()
    item["started_at_ms"] = {"N": "not-a-number"}
    assert IndexRecord.from_item(item) is None


def test_join_keeps_matching_rows_with_the_state_from_list_microvms() -> None:
    records = {"microvm-1": row("microvm-1", {"user": "42", "team": "ml"})}
    kept = join_index([listed("microvm-1", "SUSPENDED")], records, {"user": "42"}, NOW)
    assert [(item.sandbox_id, item.state, item.metadata) for item in kept] == [
        ("microvm-1", "SUSPENDED", {"user": "42", "team": "ml"})
    ]


def test_join_requires_every_wanted_pair() -> None:
    records = {"microvm-1": row("microvm-1", {"user": "42"})}
    assert join_index([listed("microvm-1")], records, {"user": "43"}, NOW) == []
    assert join_index([listed("microvm-1")], records, {"user": "42", "x": "1"}, NOW) == []
    assert len(join_index([listed("microvm-1")], records, {}, NOW)) == 1


def test_join_drops_items_without_a_row() -> None:
    assert join_index([listed("microvm-9")], {}, {"user": "42"}, NOW) == []


def test_join_drops_image_or_started_at_mismatches() -> None:
    records = {
        "a": row("a", {"user": "42"}),
        "b": row("b", {"user": "42"}),
        "c": row("c", {"user": "42"}),
    }
    items = [
        listed("a", image=OTHER_IMAGE),
        listed("b", started=STARTED + timedelta(seconds=2)),
        listed("c", started=STARTED + timedelta(milliseconds=900)),
    ]
    assert [item.sandbox_id for item in join_index(items, records, {"user": "42"}, NOW)] == ["c"]


def test_join_drops_expired_rows() -> None:
    expired = {"microvm-1": row("microvm-1", {"user": "42"}, expires_at=int(NOW) - 1)}
    assert join_index([listed("microvm-1")], expired, {"user": "42"}, NOW) == []


def test_chunks_are_at_most_one_hundred_unique_ids() -> None:
    ids = [f"id-{n}" for n in range(250)] + ["id-0"]
    parts = chunks(ids)
    assert [len(part) for part in parts] == [100, 100, 50]


def test_listing_request_with_index_accepts_suspended_states() -> None:
    index, api, session = fake_index()
    request = listing_request(
        template=None,
        template_version=None,
        states=["SUSPENDED"],
        metadata={"user": "42"},
        started_after=None,
        order=None,
        limit=None,
        next_token=None,
        index=index,
    )
    assert request.index is index
    filters = request.filters(None)
    assert filters.index_table == "rayito-sandboxes"
    assert filters.accepts(listed("x", "SUSPENDED"))
    assert not filters.accepts(listed("x", "RUNNING"))
    assert session.built == []
    assert api.requests == []


def test_listing_request_with_index_defaults_to_every_live_state() -> None:
    index, _, _ = fake_index()
    request = listing_request(
        template=None,
        template_version=None,
        states=None,
        metadata={"user": "42"},
        started_after=None,
        order=None,
        limit=None,
        next_token=None,
        index=index,
    )
    filters = request.filters(None)
    for state in ("RUNNING", "PENDING", "SUSPENDING", "SUSPENDED"):
        assert filters.accepts(listed("x", state))
    assert not filters.accepts(listed("x", "TERMINATED"))


def test_listing_request_with_index_rejects_terminal_states() -> None:
    index, _, _ = fake_index()
    with pytest.raises(InvalidArgumentException, match="TERMINATED"):
        listing_request(
            template=None,
            template_version=None,
            states=["TERMINATED"],
            metadata={"user": "42"},
            started_after=None,
            order=None,
            limit=None,
            next_token=None,
            index=index,
        )


def test_suspended_metadata_without_index_is_still_rejected() -> None:
    with pytest.raises(InvalidArgumentException, match="RUNNING"):
        listing_request(
            template=None,
            template_version=None,
            states=["SUSPENDED"],
            metadata={"user": "42"},
            started_after=None,
            order=None,
            limit=None,
            next_token=None,
        )


def test_index_without_metadata_is_the_plain_listing() -> None:
    index, _, _ = fake_index()
    request = listing_request(
        template=None,
        template_version=None,
        states=None,
        metadata=None,
        started_after=None,
        order=None,
        limit=None,
        next_token=None,
        index=index,
    )
    assert request.index is None
    assert request.filters(None).index_table is None


def test_fingerprint_binds_the_index_table_and_keeps_the_old_one_without_it() -> None:
    base = ListFilters(
        image_arn=None,
        image_version=None,
        states=None,
        started_after_ms=None,
        metadata=(("user", "42"),),
        order=None,
    )
    indexed = ListFilters(
        image_arn=None,
        image_version=None,
        states=None,
        started_after_ms=None,
        metadata=(("user", "42"),),
        order=None,
        index_table="rayito-sandboxes",
    )
    assert base.fingerprint() != indexed.fingerprint()
    assert indexed.fingerprint() == GOLDEN_INDEX_FINGERPRINT


def test_index_argument_must_be_a_dynamodb_index() -> None:
    with pytest.raises(InvalidArgumentException, match="DynamoDbIndex"):
        listing_request(
            template=None,
            template_version=None,
            states=None,
            metadata={"a": "1"},
            started_after=None,
            order=None,
            limit=None,
            next_token=None,
            index="rayito-sandboxes",  # type: ignore[arg-type]
        )
