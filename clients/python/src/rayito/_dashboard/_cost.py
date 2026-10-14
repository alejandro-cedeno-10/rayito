"""`UsageCost`: el coste facturado de Lambda MicroVMs (Cost Explorer, por
usage type, con caché local de `COST_CACHE_TTL_SECONDS`) y la estimación por
sandbox (tiempo activo por los precios documentados). Son dos cosas
distintas y cada resultado lleva su rótulo (`billed`, `estimated`): la
estimación nunca se presenta como factura. Construirlo no hace ninguna
llamada a AWS.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

import boto3

from rayito._aws import LazyClient
from rayito._dashboard._aws import CostExplorerSource
from rayito._dashboard._domain import (
    MEMORY_COUNTED_STATES,
    BilledCost,
    CostEstimate,
    Granularity,
    TtlCache,
    build_cost_estimate,
    estimate_sandbox_cost,
    fold_billed_cost,
)
from rayito._dashboard._inventory import InventoryReader
from rayito._dashboard._plane import LazyPlane
from rayito._dashboard._ports import CostSource, ManagementPlane
from rayito._limits import COST_CACHE_TTL_SECONDS, COST_EXPLORER_REGION
from rayito._size_catalog import DEFAULT_SIZE_CATALOG, SizeCatalog
from rayito.exceptions import InvalidArgumentException

CostKey = tuple[date, date, str, str | None]


def utc_now() -> datetime:
    return datetime.now(UTC)


def _as_date(value: date | str, name: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise InvalidArgumentException(f"{name}: se esperaba una fecha AAAA-MM-DD") from None


class UsageCost:
    """Coste facturado y estimado de Lambda MicroVMs.

    Coste y activación
    -------------------
    Activa: una llamada explícita a `billed()` (la estimación, `estimate()`,
        no usa Cost Explorer); construir `UsageCost` no llama a AWS.
    Recursos y llamadas AWS: sólo lecturas. `billed()` hace
        `ce:GetCostAndUsage` (una petición por página, normalmente una) en
        us-east-1, donde vive Cost Explorer; `estimate()` hace
        `ListMicrovms` y `GetMicrovmImageVersion` (una por versión de
        imagen y proceso).
    Coste aproximado: `billed()` es de pago: $0,01 por petición a Cost
        Explorer (us-east-1, consultado 2026-10-13, https://aws.amazon.com/
        aws-cost-management/aws-cost-explorer/pricing/). La caché está en
        memoria, es de cada instancia (se pierde al terminar el proceso o
        al crear otra `UsageCost`), guarda como mucho `COST_CACHE_MAX_ENTRIES`
        ventanas y se indexa por inicio, fin, granularidad y región: reutiliza
        una sola instancia para ahorrar. Con una instancia de larga vida y
        `COST_CACHE_TTL_SECONDS` (24 h) salen unas 30 peticiones al mes ≈
        $0,30; con una instancia nueva por llamada (un cron, una Lambda, un
        script) cada `billed()` paga $0,01. `DAILY` sobre ventanas largas
        puede necesitar varias páginas de pago. `estimate()` es gratuita.
    IAM: `ce:GetCostAndUsage` para `billed()`; `lambda:ListMicrovms` y
        `lambda:GetMicrovmImageVersion` para `estimate()`.
    Cómo apagarla: no llames a `billed()`; no deja recursos que borrar.
    Opciones: `cache_ttl_seconds` baja o sube la caducidad de la caché de
        `billed()`; bajarla añade peticiones de pago ($0,01 cada una).
    Ejemplo:
        cost = UsageCost().billed()
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
        self._session = session
        self._plane = LazyPlane(region=region, session=session, plane=control_plane)
        self._source = source
        self._catalog = catalog
        self._clock = clock
        self._cache: TtlCache[CostKey, BilledCost] = TtlCache(cache_ttl_seconds, monotonic)

    def billed(
        self,
        *,
        start: date | str | None = None,
        end: date | str | None = None,
        granularity: Granularity = "MONTHLY",
        region: str | None = None,
        refresh: bool = False,
    ) -> BilledCost:
        """El coste facturado de `start` (incluido) a `end` (excluido); por
        defecto, del día 1 del mes en curso (UTC) a mañana. `region` filtra
        por región de AWS (sin filtro, toda la cuenta). Cost Explorer tiene
        unas 24 h de retraso, y `partial` avisa de que aún hay periodos
        estimados por AWS. Una ventana ya leída se sirve de la caché local
        hasta que caduca.

        De pago: cada fallo de caché hace una petición a Cost Explorer
        ($0,01, us-east-1, por página; consultado 2026-10-13), y
        `refresh=True` siempre la hace. Un acierto de caché es gratuito."""
        window_start, window_end = self._window(start, end)
        key: CostKey = (window_start, window_end, granularity, region)
        cached = None if refresh else self._cache.get(key)
        if cached is not None:
            return cached
        result = self._cost_source().get_cost_and_usage(
            start=window_start, end=window_end, granularity=granularity, region=region
        )
        cost = fold_billed_cost(
            result, start=window_start, end=window_end, region=region, fetched_at=self._clock()
        )
        self._cache.put(key, cost)
        return cost

    def estimate(self) -> CostEstimate:
        """La estimación por sandbox: el tiempo transcurrido desde el arranque
        de cada `RUNNING` por el precio de cómputo baseline de su tamaño.
        Siempre `estimated`: no incluye snapshots ni el consumo sobre el
        baseline.

        Gratuita: sólo lee Lambda MicroVMs (`ListMicrovms` y
        `GetMicrovmImageVersion`) y nunca usa Cost Explorer."""
        plane = self._plane.get()
        now = self._clock()
        views = InventoryReader(plane, self._catalog).views(states=MEMORY_COUNTED_STATES)
        return build_cost_estimate(
            estimate_sandbox_cost(
                view.sandbox_id, view.state, view.started_at, view.memory_mib, now
            )
            for view in views
        )

    def _window(self, start: date | str | None, end: date | str | None) -> tuple[date, date]:
        today = self._clock().date()
        window_start = today.replace(day=1) if start is None else _as_date(start, "start")
        window_end = today + timedelta(days=1) if end is None else _as_date(end, "end")
        if window_start >= window_end:
            raise InvalidArgumentException("billed: start tiene que ser anterior a end")
        return window_start, window_end

    def _cost_source(self) -> CostSource:
        if self._source is None:
            client = LazyClient("ce", region=COST_EXPLORER_REGION, session=self._session)
            self._source = CostExplorerSource(client.get)
        return self._source
