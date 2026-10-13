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
        return await asyncio.to_thread(self._inner.sandboxes, states=states, image=image)

    async def images(self, *, name_filter: str | None = None) -> list[ImageSummary]:
        return await asyncio.to_thread(self._inner.images, name_filter=name_filter)

    async def image_versions(self, image: str) -> list[ImageVersionSummary]:
        return await asyncio.to_thread(self._inner.image_versions, image)

    async def events(
        self,
        *,
        sandbox_id: str | None = None,
        types: Sequence[str] | None = None,
        limit: int = DEFAULT_GET_EVENTS_LIMIT,
        order: str = "desc",
    ) -> list[EventRecord]:
        return await asyncio.to_thread(
            self._inner.events, sandbox_id=sandbox_id, types=types, limit=limit, order=order
        )

    def plan_bulk(
        self, action: BulkAction, count: int, *, applied_rate: float | None = None
    ) -> BulkPlan:
        return self._inner.plan_bulk(action, count, applied_rate=applied_rate)

    async def run_bulk(
        self,
        action: BulkAction,
        sandbox_ids: Iterable[str],
        *,
        applied_rate: float | None = None,
        on_progress: BulkProgressCallback | None = None,
    ) -> BulkResult:
        return await asyncio.to_thread(
            self._inner.run_bulk,
            action,
            tuple(sandbox_ids),
            applied_rate=applied_rate,
            on_progress=on_progress,
        )
