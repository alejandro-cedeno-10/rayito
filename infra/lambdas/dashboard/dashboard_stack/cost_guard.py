"""Los topes del coste facturado en la pila: lo que hace que «pedir coste» no
sea un grifo abierto para quien tenga sesión.

La caché de DynamoDB (`cost_cache.py`) sólo evita repetir una consulta con la
misma clave, y la clave incluye `start`, `end` y `region`, que vienen de la
petición. Cada clave nueva es una consulta de pago a Cost Explorer ($0,01), así
que, sin más, cualquier usuario autenticado (también uno sin acceso a las
acciones) podría generar claves nuevas sin parar. Dos topes lo impiden:

1. `CostWindowGuard`: sólo se aceptan ventanas alineadas a meses dentro de la
   historia de Cost Explorer (`DASHBOARD_STACK_COST_LOOKBACK_MONTHS`) y
   regiones conocidas. El espacio de claves es finito y pequeño.
2. `DynamoMissBudget` + `BudgetedCostSource`: como mucho
   `DASHBOARD_STACK_COST_MISSES_PER_DAY` consultas de pago por día UTC en
   total, para todos los usuarios, con un contador condicional en la tabla de
   caché. Se gasta sólo en un fallo de caché y, si no se puede comprobar, se
   rechaza (cierra en fallo): lo que está en juego es dinero.

El peor día cuesta `DASHBOARD_STACK_COST_MISSES_PER_DAY` por
`COST_EXPLORER_REQUEST_PRICE_USD` (más una petición por página extra). El
tope es de la pila entera, no por usuario.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any, Final

from botocore.exceptions import BotoCoreError, ClientError

from rayito._dashboard._api._router import ApiError
from rayito._dashboard._cost import utc_now
from rayito._dashboard._domain import CostQueryResult, Granularity
from rayito._dashboard._ports import CostSource
from rayito._limits import (
    COST_CACHE_TTL_SECONDS,
    DASHBOARD_STACK_COST_LOOKBACK_MONTHS,
    DASHBOARD_STACK_COST_MISSES_PER_DAY,
    SUPPORTED_REGIONS,
)
from rayito.exceptions import InvalidArgumentException

logger = logging.getLogger("rayito.dashboard")

BUDGET_KEY_PREFIX: Final = "BUDGET"
CONDITION_FAILED: Final = "ConditionalCheckFailedException"
MONTHS_PER_YEAR: Final = 12
BUDGET_EXHAUSTED: Final = (
    "se agotó el presupuesto diario de consultas de coste facturado: "
    "inténtalo mañana (UTC) o consulta Cost Explorer directamente"
)
BUDGET_UNAVAILABLE: Final = "no se pudo comprobar el presupuesto diario de coste facturado"
BAD_WINDOW: Final = (
    "start y end tienen que ser inicios de mes (end también puede ser mañana) "
    "dentro de la historia de Cost Explorer que admite el panel"
)
BAD_REGION: Final = "region no es una región admitida"


def _month_index(day: date) -> int:
    return day.year * MONTHS_PER_YEAR + day.month - 1


class CostWindowGuard:
    """Un `CostSource` que sólo deja pasar ventanas y regiones acotadas."""

    def __init__(
        self,
        inner: CostSource,
        *,
        lookback_months: int = DASHBOARD_STACK_COST_LOOKBACK_MONTHS,
        regions: frozenset[str] = frozenset(SUPPORTED_REGIONS),
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._inner = inner
        self._lookback = lookback_months
        self._regions = regions
        self._clock = clock

    def get_cost_and_usage(
        self, *, start: date, end: date, granularity: Granularity, region: str | None
    ) -> CostQueryResult:
        self._check_window(start, end)
        if region is not None and region not in self._regions:
            raise InvalidArgumentException(BAD_REGION)
        return self._inner.get_cost_and_usage(
            start=start, end=end, granularity=granularity, region=region
        )

    def _check_window(self, start: date, end: date) -> None:
        today = self._clock().date()
        tomorrow = today + timedelta(days=1)
        months_ago = _month_index(today) - _month_index(start)
        valid = (
            start.day == 1
            and 0 <= months_ago < self._lookback
            and start < end <= tomorrow
            and (end.day == 1 or end == tomorrow)
        )
        if not valid:
            raise InvalidArgumentException(BAD_WINDOW)


class DynamoMissBudget:
    """El contador diario de consultas de pago, en la tabla de caché (el TTL de
    la tabla borra la fila sola; la Lambda no tiene `DeleteItem`)."""

    def __init__(
        self,
        client: Any,
        table: str,
        *,
        misses_per_day: int = DASHBOARD_STACK_COST_MISSES_PER_DAY,
        clock: Callable[[], datetime] = utc_now,
        expiry_clock: Callable[[], float] = time.time,
    ) -> None:
        self._client = client
        self._table = table
        self._limit = misses_per_day
        self._clock = clock
        self._expiry_clock = expiry_clock

    def spend(self) -> None:
        """Gasta una consulta del día o lanza `ApiError` si no queda."""
        key = f"{BUDGET_KEY_PREFIX}#{self._clock().date().isoformat()}"
        expires_at = int(self._expiry_clock()) + COST_CACHE_TTL_SECONDS
        try:
            self._client.update_item(
                TableName=self._table,
                Key={"pk": {"S": key}},
                UpdateExpression="ADD used :one SET expires_at = :expires",
                ConditionExpression="attribute_not_exists(used) OR used < :limit",
                ExpressionAttributeValues={
                    ":one": {"N": "1"},
                    ":limit": {"N": str(self._limit)},
                    ":expires": {"N": str(expires_at)},
                },
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == CONDITION_FAILED:
                raise ApiError("aws_throttled", BUDGET_EXHAUSTED) from None
            logger.warning("dashboard cost budget: ClientError")
            raise ApiError("aws_error", BUDGET_UNAVAILABLE) from None
        except BotoCoreError:
            logger.warning("dashboard cost budget: BotoCoreError")
            raise ApiError("aws_error", BUDGET_UNAVAILABLE) from None


class BudgetedCostSource:
    """Un `CostSource` que gasta una consulta del presupuesto antes de cada
    llamada de pago. Va *dentro* de la caché: un acierto no gasta nada."""

    def __init__(self, inner: CostSource, budget: DynamoMissBudget) -> None:
        self._inner = inner
        self._budget = budget

    def get_cost_and_usage(
        self, *, start: date, end: date, granularity: Granularity, region: str | None
    ) -> CostQueryResult:
        self._budget.spend()
        return self._inner.get_cost_and_usage(
            start=start, end=end, granularity=granularity, region=region
        )
