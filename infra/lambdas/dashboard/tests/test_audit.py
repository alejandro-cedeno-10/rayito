"""El registro de auditoría: quién, qué y cuándo; sólo se añade y cierra en fallo."""

from __future__ import annotations

from datetime import UTC, datetime

import _stack_path  # noqa: F401
import pytest
from dashboard_stack.audit import AuditLog, AuditUnavailable
from dashboard_stack.tokens import Caller
from stack_fakes import FakeDynamo

CALLER = Caller("subject-0001", "ada@example.com", frozenset())
MOMENT = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
IDS = (
    "microvm-00000000-0000-0000-0000-000000000001",
    "microvm-00000000-0000-0000-0000-000000000002",
)


def log(dynamo: FakeDynamo) -> AuditLog:
    return AuditLog(dynamo, "audit-table", clock=lambda: MOMENT)


def test_one_row_per_sandbox_records_who_what_and_when() -> None:
    dynamo = FakeDynamo()
    log(dynamo).requested(CALLER, "kill", IDS, request_id="req-1", source_ip="203.0.113.7")
    rows = [item for key, item in sorted(dynamo.items.items())]
    assert [row["resource"]["S"] for row in rows] == list(IDS)
    for row in rows:
        assert row["subject"]["S"] == "subject-0001"
        assert row["action"]["S"] == "kill"
        assert row["occurred_at"]["S"] == "2026-10-13T12:00:00Z"
        assert row["request_id"]["S"] == "req-1"
        assert row["source_ip"]["S"] == "203.0.113.7"


def test_a_failed_write_raises_so_the_action_does_not_run() -> None:
    with pytest.raises(AuditUnavailable):
        log(FakeDynamo(fail=True)).requested(CALLER, "kill", IDS, request_id="r", source_ip="-")


def test_no_ids_or_too_many_is_refused() -> None:
    with pytest.raises(AuditUnavailable):
        log(FakeDynamo()).requested(CALLER, "kill", (), request_id="r", source_ip="-")


def test_rows_never_overwrite_one_another() -> None:
    dynamo = FakeDynamo()
    audit = log(dynamo)
    audit.requested(CALLER, "kill", IDS, request_id="req-1", source_ip="-")
    with pytest.raises(AuditUnavailable):
        audit.requested(CALLER, "suspend", IDS, request_id="req-1", source_ip="-")
    assert {item["action"]["S"] for item in dynamo.items.values()} == {"kill"}


def test_the_outcome_is_best_effort_and_carries_the_totals() -> None:
    dynamo = FakeDynamo()
    audit = log(dynamo)
    audit.outcome(
        CALLER,
        "kill",
        request_id="r",
        source_ip="-",
        http_status=202,
        counts={"ok": 2, "failed": 0},
    )
    (row,) = dynamo.items.values()
    assert row["http_status"]["N"] == "202" and row["ok"]["N"] == "2"
    log(FakeDynamo(fail=True)).outcome(
        CALLER, "kill", request_id="r", source_ip="-", http_status=202, counts={}
    )


def test_only_conditional_puts_are_used_never_updates_or_deletes() -> None:
    dynamo = FakeDynamo()
    audit = log(dynamo)
    audit.requested(CALLER, "kill", IDS, request_id="r", source_ip="-")
    audit.outcome(CALLER, "kill", request_id="r", source_ip="-", http_status=202, counts={})
    assert set(dynamo.calls) <= {"put_item", "transact_write_items"}
