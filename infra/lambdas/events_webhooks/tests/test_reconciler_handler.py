"""`handlers.reconciler.reconcile` with fake ports, in exactly the
environment `ReconcilerFunction` declares in `infra/events-webhooks.yaml`:
a `killed{unknown}` is synthesized once for a sandbox `ListMicrovms` no
longer reports, carrying that sandbox's own generation and image, and the
dedupe window is the template's own interval.
"""

from __future__ import annotations

from typing import Any

import pytest
from adapters.dynamodb import DynamoDbStore
from adapters.microvms import ListMicrovmsLister
from conftest import FakeTable, template_environment
from domain.event import LifecycleEvent
from handlers import reconciler

_INTERVAL_MINUTES = 7


class _FakeMicrovmsClient:
    def __init__(self, items: list[dict[str, str]]) -> None:
        self._items = items

    def list_microvms(self, **_kwargs: Any) -> dict[str, Any]:
        return {"items": self._items}


@pytest.fixture(autouse=True)
def _template_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in template_environment("ReconcilerFunction").items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("RECONCILER_INTERVAL_MINUTES", str(_INTERVAL_MINUTES))


def _store_with_open_sandbox(
    sandbox_id: str, *, generation: int = 3
) -> tuple[FakeTable, DynamoDbStore]:
    table = FakeTable()
    store = DynamoDbStore(table)
    store.admit(
        LifecycleEvent(
            event_id="evt-r",
            sandbox_id=sandbox_id,
            kind="resumed",
            generation=generation,
            occurred_at_ms=1,
            image_arn="arn:image",
            image_version="42",
        ),
        1,
    )
    return table, store


def _lister(*listed: tuple[str, str]) -> ListMicrovmsLister:
    return ListMicrovmsLister(
        _FakeMicrovmsClient([{"microvmId": vm, "state": state} for vm, state in listed])
    )


def test_the_window_is_the_template_interval() -> None:
    assert reconciler.reconcile_window_ms() == _INTERVAL_MINUTES * 60 * 1000


def test_synthesizes_killed_with_the_sandboxs_own_generation_and_image() -> None:
    table, store = _store_with_open_sandbox("sbx-gone", generation=3)

    assert reconciler.reconcile(store, _lister()) == 1

    (killed,) = [item for (pk, _sk), item in table.items.items() if pk == "EVENT#sbx-gone"]
    assert killed["kind"] == "killed"
    assert killed["kill_reason"] == "unknown"
    assert killed["generation"] == 3
    assert (killed["image_arn"], killed["image_version"]) == ("arn:image", "42")
    assert table.items[("STATE#sbx-gone", "STATE")]["last_kind"] == "killed"


def test_a_second_run_synthesizes_nothing_more() -> None:
    _table, store = _store_with_open_sandbox("sbx-gone")
    assert reconciler.reconcile(store, _lister()) == 1
    assert reconciler.reconcile(store, _lister()) == 0


def test_leaves_a_still_listed_sandbox_alone() -> None:
    _table, store = _store_with_open_sandbox("sbx-alive")
    assert reconciler.reconcile(store, _lister(("sbx-alive", "SUSPENDED"))) == 0
    assert [s.sandbox_id for s in store.open_sandboxes()] == ["sbx-alive"]


def test_the_handler_logs_what_the_run_did(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _table, store = _store_with_open_sandbox("sbx-gone")
    monkeypatch.setattr(reconciler, "_store_singleton", lambda: store)
    monkeypatch.setattr(reconciler, "_lister_singleton", _lister)

    assert reconciler.handler({}, None) == {"synthesized": 1}
    assert reconciler.handler({}, None) == {"synthesized": 0}
    assert capsys.readouterr().out.splitlines() == ['{"synthesized": 1}', '{"synthesized": 0}']


def test_a_stored_event_whose_tombstone_failed_is_closed_by_the_next_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Both runs inside one window: the same synthesized id, one stored event.
    monkeypatch.setattr(reconciler.time, "time", lambda: 1_790_000_000.0)
    table, store = _store_with_open_sandbox("sbx-gone")
    real_admit = store.admit
    calls: list[int] = []

    def admit_failing_once(event: LifecycleEvent, now_ms: int) -> Any:
        calls.append(now_ms)
        if len(calls) == 1:
            raise RuntimeError("DynamoDB unavailable")
        return real_admit(event, now_ms)

    store.admit = admit_failing_once  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        reconciler.reconcile(store, _lister())
    assert [s.sandbox_id for s in store.open_sandboxes()] == ["sbx-gone"]
    reconciler.reconcile(store, _lister())
    assert store.open_sandboxes() == []
    killed = [key for key in table.items if key[0] == "EVENT#sbx-gone"]
    assert len(killed) == 1
