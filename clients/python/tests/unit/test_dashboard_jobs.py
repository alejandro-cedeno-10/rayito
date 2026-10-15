"""La cola de acciones masivas: al cerrar, el sandbox en curso termina y nada
más se toca (ni el resto del trabajo en curso ni los trabajos encolados)."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable

import pytest

from rayito._dashboard._api._jobs import ABORTED_CODE, BulkJobs, JobsBusy
from rayito._dashboard._domain import (
    BulkAction,
    BulkItemResult,
    BulkPlan,
    BulkProgress,
    plan_bulk,
)

WAIT_SECONDS = 5.0
SUSPEND: BulkAction = "suspend"


class GatedFleet:
    """Aplica cada sandbox sólo cuando el test suelta `release`; así el
    trabajo queda a medias mientras se cierra la cola."""

    def __init__(self) -> None:
        self.applied: list[str] = []
        self.first_started = threading.Event()
        self.release = threading.Event()

    def run_bulk(
        self,
        action: BulkAction,
        sandbox_ids: Iterable[str],
        *,
        applied_rate: float | None = None,
        on_progress: Callable[[BulkProgress], None] | None = None,
    ) -> None:
        ids = tuple(sandbox_ids)
        for index, sandbox_id in enumerate(ids, start=1):
            self.first_started.set()
            assert self.release.wait(WAIT_SECONDS)
            self.applied.append(sandbox_id)
            if on_progress is not None:
                on_progress(BulkProgress(index, len(ids), BulkItemResult(sandbox_id, "done")))


def plan(count: int) -> BulkPlan:
    return plan_bulk(SUSPEND, count, None)


def codes(snapshot: dict[str, object]) -> list[str | None]:
    items = snapshot["items"]
    assert isinstance(items, list)
    return [item["code"] for item in items]


def test_close_finishes_the_running_item_and_aborts_the_rest_without_touching_it() -> None:
    fleet = GatedFleet()
    jobs = BulkJobs(fleet)  # type: ignore[arg-type]
    first = jobs.submit(SUSPEND, ["a", "b", "c"], plan=plan(3), applied_rate=None)
    second = jobs.submit(SUSPEND, ["d", "e"], plan=plan(2), applied_rate=None)
    assert fleet.first_started.wait(WAIT_SECONDS)

    closer = threading.Thread(target=jobs.close)
    closer.start()
    fleet.release.set()
    closer.join(WAIT_SECONDS)

    assert not closer.is_alive()
    assert fleet.applied == ["a"]
    finished_first = jobs.get(str(first["jobId"]))
    finished_second = jobs.get(str(second["jobId"]))
    assert finished_first is not None and finished_second is not None
    assert finished_first["status"] == finished_second["status"] == "finished"
    assert codes(finished_first) == [None, ABORTED_CODE, ABORTED_CODE]
    assert codes(finished_second) == [ABORTED_CODE, ABORTED_CODE]


def test_close_without_jobs_returns_at_once() -> None:
    BulkJobs(GatedFleet()).close()  # type: ignore[arg-type]


def test_the_queue_refuses_more_unfinished_jobs_than_the_cap() -> None:
    fleet = GatedFleet()
    jobs = BulkJobs(fleet, active_max=1)  # type: ignore[arg-type]
    jobs.submit(SUSPEND, ["a"], plan=plan(1), applied_rate=None)

    with pytest.raises(JobsBusy):
        jobs.submit(SUSPEND, ["b"], plan=plan(1), applied_rate=None)

    fleet.release.set()
    jobs.close()
