"""La cola de acciones masivas del panel: un único hilo trabajador ejecuta
los trabajos de uno en uno con `Fleet.run_bulk`, que ya respeta el TPS de la
operación; al ser uno solo, dos trabajos nunca suman tasa. Cada trabajo
guarda su progreso para que la SPA lo consulte. Sólo guarda ids, resultados
y códigos de AWS."""

from __future__ import annotations

import itertools
import queue
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Literal

from rayito._dashboard._api._contract import iso_utc
from rayito._dashboard._api._serialize import plan_json
from rayito._dashboard._domain import (
    BulkAction,
    BulkItemResult,
    BulkOutcome,
    BulkPlan,
    BulkProgress,
)
from rayito._dashboard._fleet import Fleet
from rayito._limits import DASHBOARD_JOBS_ACTIVE_MAX, DASHBOARD_JOBS_RETAINED

JobStatus = Literal["queued", "running", "finished"]
OUTCOMES: Final[tuple[BulkOutcome, ...]] = ("done", "unchanged", "failed")
#: Código del resultado de los sandboxes que un trabajo abortado no llegó a
#: tocar (un fallo inesperado de la cola, no de AWS).
ABORTED_CODE: Final = "JobAborted"


class JobsClosing(Exception):
    """Lo que `on_progress` lanza al cerrar la cola para cortar el trabajo en
    curso al terminar su sandbox actual; nunca sale de `BulkJobs`."""


class JobsBusy(Exception):
    """Ya hay `DASHBOARD_JOBS_ACTIVE_MAX` trabajos sin terminar: la cola no
    crece sin tope aunque la SPA (o un script) siga enviando acciones."""


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class BulkJob:
    job_id: str
    action: BulkAction
    sandbox_ids: tuple[str, ...]
    plan: BulkPlan
    applied_rate: float | None
    created_at: datetime
    status: JobStatus = "queued"
    items: list[BulkItemResult] = field(default_factory=list)


def job_json(job: BulkJob) -> dict[str, Any]:
    counts = {
        outcome: sum(1 for item in job.items if item.outcome == outcome) for outcome in OUTCOMES
    }
    return {
        "jobId": job.job_id,
        "action": job.action,
        "status": job.status,
        "createdAt": iso_utc(job.created_at),
        "total": len(job.sandbox_ids),
        "completed": len(job.items),
        "counts": counts,
        "plan": plan_json(job.plan),
        "items": [
            {"sandboxId": item.sandbox_id, "outcome": item.outcome, "code": item.code}
            for item in job.items
        ],
    }


class BulkJobs:
    """Registro y cola de trabajos. `on_finished` se llama al acabar cada
    uno (el servidor invalida la caché de lecturas)."""

    def __init__(
        self,
        fleet: Fleet,
        *,
        retained: int = DASHBOARD_JOBS_RETAINED,
        active_max: int = DASHBOARD_JOBS_ACTIVE_MAX,
        clock: Callable[[], datetime] = utc_now,
        on_finished: Callable[[], None] = lambda: None,
    ) -> None:
        self._fleet = fleet
        self._retained = retained
        self._active_max = active_max
        self._clock = clock
        self._on_finished = on_finished
        self._jobs: OrderedDict[str, BulkJob] = OrderedDict()
        self._lock = threading.Lock()
        self._queue: queue.Queue[BulkJob | None] = queue.Queue()
        self._sequence = itertools.count(1)
        self._worker = threading.Thread(target=self._run, name="rayito-dashboard-jobs", daemon=True)
        self._started = False
        self._closing = threading.Event()

    def submit(
        self,
        action: BulkAction,
        sandbox_ids: Iterable[str],
        *,
        plan: BulkPlan,
        applied_rate: float | None,
    ) -> dict[str, Any]:
        ids = tuple(sandbox_ids)
        with self._lock:
            if self._active() >= self._active_max:
                raise JobsBusy
            job = BulkJob(
                job_id=f"job-{next(self._sequence)}",
                action=action,
                sandbox_ids=ids,
                plan=plan,
                applied_rate=applied_rate,
                created_at=self._clock(),
            )
            self._jobs[job.job_id] = job
            self._evict_finished()
            snapshot = job_json(job)
            if not self._started:
                self._started = True
                self._worker.start()
        self._queue.put(job)
        return snapshot

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return None if job is None else job_json(job)

    def snapshots(self) -> list[dict[str, Any]]:
        with self._lock:
            return [job_json(job) for job in reversed(self._jobs.values())]

    def close(self) -> None:
        """Para el hilo trabajador: el sandbox en curso termina, el resto del
        trabajo en curso y todos los encolados quedan `JobAborted` sin
        tocarse, para que cerrar el panel no siga matando ni suspendiendo."""
        self._closing.set()
        if self._started:
            self._queue.put(None)
            self._worker.join()

    def _active(self) -> int:
        return sum(1 for job in self._jobs.values() if job.status != "finished")

    def _evict_finished(self) -> None:
        while len(self._jobs) > self._retained:
            oldest = next((j for j in self._jobs.values() if j.status == "finished"), None)
            if oldest is None:
                return
            del self._jobs[oldest.job_id]

    def _run(self) -> None:
        while (job := self._queue.get()) is not None:
            self._execute(job)

    def _execute(self, job: BulkJob) -> None:
        with self._lock:
            job.status = "running"
        try:
            if self._closing.is_set():
                raise JobsClosing
            self._fleet.run_bulk(
                job.action,
                job.sandbox_ids,
                applied_rate=job.applied_rate,
                on_progress=lambda progress: self._record(job, progress),
            )
        except Exception:
            self._abort(job)
        with self._lock:
            job.status = "finished"
        self._on_finished()

    def _record(self, job: BulkJob, progress: BulkProgress) -> None:
        with self._lock:
            job.items.append(progress.last)
        if self._closing.is_set():
            raise JobsClosing

    def _abort(self, job: BulkJob) -> None:
        with self._lock:
            reached = {item.sandbox_id for item in job.items}
            job.items.extend(
                BulkItemResult(sandbox_id, "failed", ABORTED_CODE)
                for sandbox_id in job.sandbox_ids
                if sandbox_id not in reached
            )
