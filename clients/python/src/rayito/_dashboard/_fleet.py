"""`Fleet`: lo que el panel necesita para ver y operar la flota, sobre los
puertos de `_ports.py` y reutilizando el SDK (`ControlPlane`,
`LifecycleEvents`): inventario de sandboxes, imágenes y versiones, historial
de eventos y acciones masivas con una cola que respeta los TPS. Es de sólo
lectura salvo que se construya con `allow_actions=True`. Construirlo no hace
ninguna llamada a AWS.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Sequence

import boto3

from rayito._aws import LazyClient, TokenBucket
from rayito._dashboard._aws import LambdaMicrovmsImageCatalog
from rayito._dashboard._domain import (
    BULK_OPERATIONS,
    BulkAction,
    BulkItemResult,
    BulkPlan,
    BulkProgress,
    BulkResult,
    ImageSummary,
    ImageVersionSummary,
    SandboxView,
    plan_bulk,
)
from rayito._dashboard._inventory import InventoryReader
from rayito._dashboard._plane import LazyPlane
from rayito._dashboard._ports import EventsHistory, ImageCatalog, ManagementPlane
from rayito._lifecycle_events._domain import DEFAULT_GET_EVENTS_LIMIT, EventRecord
from rayito._limits import MICROVM_ID_MAX_LENGTH, MICROVM_ID_MIN_LENGTH
from rayito._size_catalog import DEFAULT_SIZE_CATALOG, SizeCatalog
from rayito.exceptions import AuthenticationException, InvalidArgumentException, SandboxException

BulkProgressCallback = Callable[[BulkProgress], None]


class Fleet:
    """Inventario, imágenes, eventos y acciones sobre los MicroVMs de la cuenta.

    Coste y activación
    -------------------
    Activa: llamar a sus métodos; construir `Fleet` no llama a AWS. Las
        acciones masivas exigen además `allow_actions=True`.
    Recursos y llamadas AWS: lecturas (`ListMicrovms`, `ListMicrovmImages`,
        `ListMicrovmImageVersions`, `GetMicrovmImageVersion`) y, con
        `allow_actions=True`, `TerminateMicrovm`, `SuspendMicrovm` y
        `ResumeMicrovm` sobre los sandboxes que se indiquen; `events()` lee
        la tabla de la pila `events-webhooks` si se pasó `events=`.
    Coste aproximado: $0 las lecturas; las acciones no facturan por la
        llamada (terminar y suspender dejan de facturar cómputo; suspender
        escribe un snapshot, $0,0038/GB, us-east-1, AWS_API_NOTES.md §12,
        consultado 2026-10-13).
    IAM: `lambda:ListMicrovms`, `lambda:ListMicrovmImages`,
        `lambda:ListMicrovmImageVersions`, `lambda:GetMicrovmImageVersion`;
        con acciones, `lambda:TerminateMicrovm`, `lambda:SuspendMicrovm` y
        `lambda:ResumeMicrovm`.
    Cómo apagarla: no llames a sus métodos; las acciones están apagadas
        salvo `allow_actions=True` y no dejan recursos que borrar.
    Ejemplo:
        fleet = Fleet(region="us-east-1")
        running = fleet.sandboxes(states={"RUNNING"})
    """

    def __init__(
        self,
        *,
        region: str | None = None,
        session: boto3.session.Session | None = None,
        control_plane: ManagementPlane | None = None,
        images: ImageCatalog | None = None,
        events: EventsHistory | None = None,
        allow_actions: bool = False,
        catalog: SizeCatalog = DEFAULT_SIZE_CATALOG,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._session = session
        self._plane = LazyPlane(region=region, session=session, plane=control_plane)
        self._images = images
        self._events = events
        self._allow_actions = allow_actions
        self._catalog = catalog
        self._monotonic = monotonic
        self._sleep = sleep

    @property
    def allow_actions(self) -> bool:
        return self._allow_actions

    def sandboxes(
        self, *, states: Iterable[str] | None = None, image: str | None = None
    ) -> list[SandboxView]:
        """Los sandboxes con la memoria de su imagen. Sin `states`, los que no
        están terminados; `image` filtra por nombre o ARN de imagen."""
        plane = self._plane.get()
        image_arn = None if image is None else plane.resolve_template_arn(image)
        return InventoryReader(plane, self._catalog).views(states=states, image_arn=image_arn)

    def images(self, *, name_filter: str | None = None) -> list[ImageSummary]:
        return self._image_catalog().list_images(name_filter=name_filter)

    def image_versions(self, image: str) -> list[ImageVersionSummary]:
        """Las versiones de `image` (nombre o ARN), la más nueva primero."""
        return self._image_catalog().list_versions(self._plane.get().resolve_template_arn(image))

    def events(
        self,
        *,
        sandbox_id: str | None = None,
        types: Sequence[str] | None = None,
        limit: int = DEFAULT_GET_EVENTS_LIMIT,
        order: str = "desc",
    ) -> list[EventRecord]:
        if self._events is None:
            raise InvalidArgumentException(
                "Fleet.events: no hay historial de eventos; pasa events=LifecycleEvents(...) "
                "con la pila events-webhooks desplegada"
            )
        return self._events.get_events(sandbox_id=sandbox_id, types=types, limit=limit, order=order)

    def plan_bulk(
        self, action: BulkAction, count: int, *, applied_rate: float | None = None
    ) -> BulkPlan:
        """Cuánto tardaría una acción masiva; `applied_rate` es la cuota de
        TPS aplicada (`QuotaReport.applied_rate`)."""
        _validate_action(action)
        return plan_bulk(action, count, applied_rate)

    def run_bulk(
        self,
        action: BulkAction,
        sandbox_ids: Iterable[str],
        *,
        applied_rate: float | None = None,
        on_progress: BulkProgressCallback | None = None,
    ) -> BulkResult:
        """Aplica `action` a cada sandbox, de uno en uno y al ritmo de la
        cuota de TPS de su operación (suspender, 2 TPS). Un fallo de un
        sandbox no detiene a los demás: queda `failed` con el código de AWS.
        Un id repetido se aplica una sola vez. `on_progress` se llama tras
        cada sandbox, desde el hilo que ejecuta la cola."""
        if not self._allow_actions:
            raise InvalidArgumentException(
                "Fleet.run_bulk: las acciones están desactivadas; crea Fleet(allow_actions=True)"
            )
        _validate_action(action)
        ids = _validated_ids(sandbox_ids)
        plan = plan_bulk(action, len(ids), applied_rate)
        bucket = TokenBucket(plan.tps, clock=self._monotonic, sleep=self._sleep)
        results: list[BulkItemResult] = []
        for sandbox_id in ids:
            bucket.acquire()
            result = self._apply(action, sandbox_id)
            results.append(result)
            if on_progress is not None:
                on_progress(BulkProgress(len(results), len(ids), result))
        return BulkResult(plan, tuple(results))

    def _apply(self, action: BulkAction, sandbox_id: str) -> BulkItemResult:
        plane = self._plane.get()
        operations = {
            "kill": plane.terminate_microvm,
            "suspend": plane.suspend_microvm,
            "resume": plane.resume_microvm,
        }
        try:
            changed = operations[action](sandbox_id)
        except (SandboxException, AuthenticationException) as exc:
            code = getattr(exc, "aws_code", None) or type(exc).__name__
            return BulkItemResult(sandbox_id, "failed", code)
        return BulkItemResult(sandbox_id, "done" if changed else "unchanged")

    def _image_catalog(self) -> ImageCatalog:
        if self._images is None:
            region = self._plane.get().region
            client = LazyClient("lambda-microvms", region=region, session=self._session)
            self._images = LambdaMicrovmsImageCatalog(client.get)
        return self._images


def _validate_action(action: str) -> None:
    if action not in BULK_OPERATIONS:
        admitted = ", ".join(BULK_OPERATIONS)
        raise InvalidArgumentException(f"action: {action!r} no es válida (admitidas: {admitted})")


def _validated_ids(sandbox_ids: Iterable[str]) -> tuple[str, ...]:
    ids = tuple(dict.fromkeys(sandbox_ids))
    if not ids:
        raise InvalidArgumentException("sandbox_ids no puede estar vacío")
    for sandbox_id in ids:
        if not MICROVM_ID_MIN_LENGTH <= len(sandbox_id) <= MICROVM_ID_MAX_LENGTH:
            raise InvalidArgumentException(
                f"sandbox_ids: cada id mide entre {MICROVM_ID_MIN_LENGTH} y "
                f"{MICROVM_ID_MAX_LENGTH} caracteres"
            )
    return ids
