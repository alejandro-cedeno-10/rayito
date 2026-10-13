"""`AsyncUsageCost`: el mismo contrato que `UsageCost` sobre
`asyncio.to_thread`; delega en un `UsageCost` propio."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import date, datetime

import boto3

from rayito._dashboard._cost import UsageCost, utc_now
from rayito._dashboard._domain import BilledCost, CostEstimate, Granularity
from rayito._dashboard._ports import CostSource, ManagementPlane
from rayito._limits import COST_CACHE_TTL_SECONDS
from rayito._size_catalog import DEFAULT_SIZE_CATALOG, SizeCatalog


class AsyncUsageCost:
    def __init__(
        self,
        *,
        region: str | None = None,
        session: boto3.session.Session | None = None,
        control_plane: ManagementPlane | None = None,
        source: CostSource | None = None,
        catalog: SizeCatalog = DEFAULT_SIZE_CATALOG,
        cache_ttl_seconds: float = COST_CACHE_TTL_SECONDS,
        clock: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._inner = UsageCost(
            region=region,
            session=session,
            control_plane=control_plane,
            source=source,
            catalog=catalog,
            cache_ttl_seconds=cache_ttl_seconds,
            clock=clock,
            monotonic=monotonic,
        )

    async def billed(
        self,
        *,
        start: date | str | None = None,
        end: date | str | None = None,
        granularity: Granularity = "MONTHLY",
        region: str | None = None,
        refresh: bool = False,
    ) -> BilledCost:
        return await asyncio.to_thread(
            self._inner.billed,
            start=start,
            end=end,
            granularity=granularity,
            region=region,
            refresh=refresh,
        )

    async def estimate(self) -> CostEstimate:
        return await asyncio.to_thread(self._inner.estimate)
