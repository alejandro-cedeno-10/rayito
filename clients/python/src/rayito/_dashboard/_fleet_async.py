"""`AsyncFleet`: el mismo contrato que `Fleet` sobre `asyncio.to_thread`;
delega en un `Fleet` propio."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterable, Sequence

import boto3

from rayito._dashboard._domain import (
    BulkAction,
    BulkPlan,
    BulkResult,
    ImageSummary,
    ImageVersionSummary,
    SandboxView,
)
from rayito._dashboard._fleet import BulkProgressCallback, Fleet
from rayito._dashboard._ports import EventsHistory, ImageCatalog, ManagementPlane
from rayito._lifecycle_events._domain import DEFAULT_GET_EVENTS_LIMIT, EventRecord
from rayito._size_catalog import DEFAULT_SIZE_CATALOG, SizeCatalog


class AsyncFleet:
    """El mismo contrato y el mismo coste que `Fleet`.

    Coste y activación
    -------------------
    Activa: llamar a sus métodos; construir `AsyncFleet` no llama a AWS. Las
        acciones masivas exigen además `allow_actions=True`.
    Recursos y llamadas AWS: lecturas (`ListMicrovms`, `ListMicrovmImages`,
        `ListMicrovmImageVersions`, `GetMicrovmImageVersion`) y, con
        `allow_actions=True`, `TerminateMicrovm`, `SuspendMicrovm` y
        `ResumeMicrovm`; `events()` lee la tabla DynamoDB de la pila
        `events-webhooks` si se pasó `events=`.
    Coste aproximado: $0 las lecturas de sandboxes, imágenes y versiones;
        `events()` es de pago, aunque mínimo (lecturas bajo demanda de
        DynamoDB, sin sondeo). Las acciones no facturan por la llamada, pero
        cambian la factura (AWS_API_NOTES.md §12, us-east-1, consultado
        2026-10-13): `kill` deja de facturar cómputo; `suspend` escribe un
        snapshot ($0,0038/GB) y paga almacenamiento ($0,08/GB-mes);
        `resume` lee el snapshot ($0,00155/GB) y vuelve a facturar cómputo al
        precio de su tamaño.
    IAM: las de lectura de `Fleet`; con acciones, `lambda:TerminateMicrovm`,
        `lambda:SuspendMicrovm` y `lambda:ResumeMicrovm`.
    Cómo apagarla: no llames a sus métodos; las acciones están apagadas
        salvo `allow_actions=True`.
    Ejemplo:
        fleet = AsyncFleet(region="us-east-1")
        running = await fleet.sandboxes(states={"RUNNING"})
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
        self._inner = Fleet(
            region=region,
            session=session,
            control_plane=control_plane,
            images=images,
            events=events,
            allow_actions=allow_actions,
            catalog=catalog,
            monotonic=monotonic,
            sleep=sleep,
        )

    @property
    def allow_actions(self) -> bool:
        return self._inner.allow_actions

    async def sandboxes(
        self, *, states: Iterable[str] | None = None, image: str | None = None
    ) -> list[SandboxView]:
        """Gratuita. Ver `Fleet.sandboxes`."""
        return await asyncio.to_thread(self._inner.sandboxes, states=states, image=image)

    async def images(self, *, name_filter: str | None = None) -> list[ImageSummary]:
        """Gratuita. Ver `Fleet.images`."""
        return await asyncio.to_thread(self._inner.images, name_filter=name_filter)

    async def image_versions(self, image: str) -> list[ImageVersionSummary]:
        """Gratuita. Ver `Fleet.image_versions`."""
        return await asyncio.to_thread(self._inner.image_versions, image)

    async def events(
        self,
        *,
        sandbox_id: str | None = None,
        types: Sequence[str] | None = None,
        limit: int = DEFAULT_GET_EVENTS_LIMIT,
        order: str = "desc",
    ) -> list[EventRecord]:
        """De pago, aunque mínimo: lecturas de DynamoDB. Ver `Fleet.events`."""
        return await asyncio.to_thread(
            self._inner.events, sandbox_id=sandbox_id, types=types, limit=limit, order=order
        )

    def plan_bulk(
        self, action: BulkAction, count: int, *, applied_rate: float | None = None
    ) -> BulkPlan:
        """Gratuita: cálculo local. Ver `Fleet.plan_bulk`."""
        return self._inner.plan_bulk(action, count, applied_rate=applied_rate)

    async def run_bulk(
        self,
        action: BulkAction,
        sandbox_ids: Iterable[str],
        *,
        applied_rate: float | None = None,
        on_progress: BulkProgressCallback | None = None,
    ) -> BulkResult:
        """Exige `allow_actions=True`; el efecto factura según la acción
        (`resume` vuelve a facturar cómputo). Ver `Fleet.run_bulk`."""
        return await asyncio.to_thread(
            self._inner.run_bulk,
            action,
            tuple(sandbox_ids),
            applied_rate=applied_rate,
            on_progress=on_progress,
        )
