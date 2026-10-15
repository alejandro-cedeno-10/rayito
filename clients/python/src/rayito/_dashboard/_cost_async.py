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
    """El mismo contrato y el mismo coste que `UsageCost`.

    Coste y activación
    -------------------
    Activa: una llamada explícita a `billed()`; construir `AsyncUsageCost`
        no llama a AWS, y `estimate()` no usa Cost Explorer.
    Recursos y llamadas AWS: sólo lecturas. `billed()` hace
        `ce:GetCostAndUsage` (una petición por página) en us-east-1;
        `estimate()` hace `ListMicrovms` y `GetMicrovmImageVersion`.
    Coste aproximado: `billed()` es de pago, $0,01 por petición a Cost
        Explorer (us-east-1, consultado 2026-10-13, https://aws.amazon.com/
        aws-cost-management/aws-cost-explorer/pricing/). La caché está en
        memoria, es de cada instancia y se pierde con el proceso, guarda como
        mucho `COST_CACHE_MAX_ENTRIES` ventanas y se indexa por inicio, fin,
        granularidad y región: reutiliza una sola instancia (unas 30
        peticiones al mes ≈ $0,30); con una instancia nueva por llamada cada
        `billed()` paga $0,01. `estimate()` es gratuita.
    IAM: `ce:GetCostAndUsage` para `billed()`; `lambda:ListMicrovms` y
        `lambda:GetMicrovmImageVersion` para `estimate()`.
    Cómo apagarla: no llames a `billed()`; no deja recursos que borrar.
    Ejemplo:
        cost = await AsyncUsageCost().billed()
        print(cost.total_usd, cost.partial)
    """

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
        """De pago: una petición a Cost Explorer ($0,01, us-east-1, por
        página) en cada fallo de caché; `refresh=True` siempre paga. Ver
        `UsageCost.billed`."""
        return await asyncio.to_thread(
            self._inner.billed,
            start=start,
            end=end,
            granularity=granularity,
            region=region,
            refresh=refresh,
        )

    async def estimate(self) -> CostEstimate:
        """Gratuita: no usa Cost Explorer. Ver `UsageCost.estimate`."""
        return await asyncio.to_thread(self._inner.estimate)
