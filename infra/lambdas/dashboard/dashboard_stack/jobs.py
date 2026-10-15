"""La cola de acciones de la Lambda. En el servidor local un hilo trabajador
ejecuta los trabajos mientras el proceso vive; una Lambda se congela en
cuanto devuelve la respuesta, así que un hilo de fondo no llegaría a
terminar. Aquí cada trabajo se ejecuta **dentro de la petición**, de uno en
uno y al ritmo del TPS de su operación (`Fleet.run_bulk`), y la respuesta
lleva el trabajo ya terminado: la SPA, que sigue el trabajo hasta que
`status` es `finished`, lo muestra sin sondear. El tiempo máximo es el de la
función (`DASHBOARD_STACK_LAMBDA_TIMEOUT_SECONDS`): 100 sandboxes a 2 TPS
son 50 s. La cáscara (`app.py`) rechaza antes de auditar un trabajo cuya
duración prevista (sandboxes / TPS aplicado) no cabe en ese tiempo menos un
margen, porque una función cortada a medias dejaría sandboxes sin tocar y sin
fila de resultado. Quien invalida la caché de lectura tras una acción es esa
misma cáscara, una sola vez, tras escribir la auditoría.

`JobLog` guarda los trabajos de este entorno de ejecución para
`GET /actions`; es por instancia (otra instancia de la función no los ve). La
fuente de verdad de lo ocurrido es el registro de auditoría.
"""

from __future__ import annotations

import itertools
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from rayito._dashboard._api._jobs import BulkJob, abort_unreached, job_json
from rayito._dashboard._domain import BulkAction, BulkPlan, BulkProgress
from rayito._dashboard._fleet import Fleet
from rayito._limits import DASHBOARD_JOBS_RETAINED


def utc_now() -> datetime:
    return datetime.now(UTC)


class JobLog:
    """Los últimos `retained` trabajos terminados de este entorno."""

    def __init__(self, retained: int = DASHBOARD_JOBS_RETAINED) -> None:
        self._retained = retained
        self._jobs: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._sequence = itertools.count(1)
        self._lock = threading.Lock()

    def next_id(self) -> str:
        return f"job-{next(self._sequence)}"

    def put(self, snapshot: dict[str, Any]) -> None:
        with self._lock:
            self._jobs[str(snapshot["jobId"])] = snapshot
            while len(self._jobs) > self._retained:
                self._jobs.popitem(last=False)

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._jobs.get(job_id)

    def snapshots(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(reversed(self._jobs.values()))


class InlineJobs:
    """`JobQueue` síncrona: `submit` ejecuta el trabajo y devuelve su estado final."""

    def __init__(
        self,
        fleet: Fleet,
        log: JobLog,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._fleet = fleet
        self._log = log
        self._clock = clock

    def submit(
        self,
        action: BulkAction,
        sandbox_ids: Iterable[str],
        *,
        plan: BulkPlan,
        applied_rate: float | None,
    ) -> dict[str, Any]:
        job = BulkJob(
            job_id=self._log.next_id(),
            action=action,
            sandbox_ids=tuple(sandbox_ids),
            plan=plan,
            applied_rate=applied_rate,
            created_at=self._clock(),
            status="running",
        )
        self._execute(job)
        job.status = "finished"
        snapshot = job_json(job)
        self._log.put(snapshot)
        return snapshot

    def get(self, job_id: str) -> dict[str, Any] | None:
        return self._log.get(job_id)

    def snapshots(self) -> list[dict[str, Any]]:
        return self._log.snapshots()

    def close(self) -> None:
        """Nada que parar: no hay hilo."""

    def _execute(self, job: BulkJob) -> None:
        def record(progress: BulkProgress) -> None:
            job.items.append(progress.last)

        try:
            self._fleet.run_bulk(
                job.action,
                job.sandbox_ids,
                applied_rate=job.applied_rate,
                on_progress=record,
            )
        except Exception:
            abort_unreached(job)
