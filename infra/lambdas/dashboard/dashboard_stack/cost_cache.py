"""La caché del coste facturado en la tabla DynamoDB de la pila. Cost Explorer
cobra por petición ($0,01) y una Lambda pierde su memoria al enfriarse, así
que la caché en memoria de `UsageCost` no basta: aquí cada ventana consultada
se guarda `COST_CACHE_TTL_SECONDS` (24 h, el retraso de los datos de Cost
Explorer) con el atributo TTL de la tabla, que DynamoDB borra solo (la Lambda
no tiene `DeleteItem`).

Es un `CostSource` que envuelve al de Cost Explorer. `refresh=true` de la
API no se salta esta caché: sólo la de memoria. Por sí sola la caché **no**
limita el gasto, porque la clave lleva `start`, `end` y `region`, que vienen
de la petición: el límite lo ponen `cost_guard.py` (ventanas y regiones
acotadas, y un presupuesto diario de consultas de pago que se gasta sólo en
un fallo de esta caché).

Un fallo de la caché nunca impide servir el coste: se consulta Cost Explorer
y, si tampoco se puede guardar, se sigue sin caché.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any, Final

from rayito._dashboard._domain import (
    CostGroup,
    CostPeriod,
    CostQueryResult,
    Granularity,
)
from rayito._dashboard._ports import CostSource
from rayito._limits import COST_CACHE_TTL_SECONDS

logger = logging.getLogger("rayito.dashboard")

KEY_PREFIX: Final = "COST"
NO_REGION: Final = "-"


def cache_key(start: date, end: date, granularity: str, region: str | None) -> str:
    return f"{KEY_PREFIX}#{start.isoformat()}#{end.isoformat()}#{granularity}#{region or NO_REGION}"


def encode(result: CostQueryResult, fetched_at: datetime) -> str:
    return json.dumps(
        {
            "requests": result.requests,
            "fetched_at": fetched_at.isoformat(),
            "periods": [
                {
                    "start": period.start.isoformat(),
                    "end": period.end.isoformat(),
                    "estimated": period.estimated,
                    "groups": [
                        [group.usage_type, group.cost_usd, group.quantity, group.unit]
                        for group in period.groups
                    ],
                }
                for period in result.periods
            ],
        }
    )


def decode(payload: str) -> CostQueryResult:
    document = json.loads(payload)
    return CostQueryResult(
        periods=tuple(
            CostPeriod(
                start=date.fromisoformat(period["start"]),
                end=date.fromisoformat(period["end"]),
                estimated=bool(period["estimated"]),
                groups=tuple(CostGroup(*group) for group in period["groups"]),
            )
            for period in document["periods"]
        ),
        requests=int(document["requests"]),
        fetched_at=datetime.fromisoformat(document["fetched_at"]),
    )


class DynamoCostCache:
    def __init__(
        self,
        inner: CostSource,
        client: Any,
        table: str,
        *,
        ttl_seconds: int = COST_CACHE_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._inner = inner
        self._client = client
        self._table = table
        self._ttl = ttl_seconds
        self._clock = clock

    def get_cost_and_usage(
        self, *, start: date, end: date, granularity: Granularity, region: str | None
    ) -> CostQueryResult:
        key = cache_key(start, end, granularity, region)
        cached = self._read(key)
        if cached is not None:
            return cached
        result = self._inner.get_cost_and_usage(
            start=start, end=end, granularity=granularity, region=region
        )
        fetched_at = datetime.fromtimestamp(self._clock(), UTC)
        self._write(key, encode(result, fetched_at))
        return CostQueryResult(result.periods, result.requests, fetched_at)

    def _read(self, key: str) -> CostQueryResult | None:
        try:
            item = self._client.get_item(TableName=self._table, Key={"pk": {"S": key}}).get("Item")
            if item is None or int(item["expires_at"]["N"]) <= self._clock():
                return None
            return decode(item["payload"]["S"])
        except Exception:
            logger.warning("dashboard cost cache: lectura descartada")
            return None

    def _write(self, key: str, payload: str) -> None:
        item = {
            "pk": {"S": key},
            "payload": {"S": payload},
            "expires_at": {"N": str(int(self._clock()) + self._ttl)},
        }
        try:
            self._client.put_item(TableName=self._table, Item=item)
        except Exception:
            logger.warning("dashboard cost cache: no se pudo guardar")
