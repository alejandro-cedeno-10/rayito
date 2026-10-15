"""El registro de auditoría de las acciones: filas append-only en DynamoDB.

- **Quién, qué y cuándo.** Una fila `requested` por sandbox antes de ejecutar
  (sujeto de Cognito, nombre, acción, id del sandbox, instante, id de la
  petición e IP de origen) y una fila `outcome` por petición al terminar (el
  estado HTTP y los totales). CloudTrail sólo vería el rol de la Lambda; esto
  dice quién fue.
- **Cierra en fallo.** Las filas `requested` se escriben en una sola
  transacción *antes* de ejecutar: si no se pueden escribir, la acción no se
  ejecuta (`AuditUnavailable`). La fila `outcome` es de mejor esfuerzo: la
  acción ya ocurrió y lo que falta es sólo el resultado.
- **Sólo se añade.** Cada `PutItem` lleva `attribute_not_exists(pk)` sobre una
  clave que incluye el id de la petición: no puede pisar una fila. El rol de
  la Lambda no tiene `UpdateItem` ni `DeleteItem` sobre la tabla (la
  plantilla los deniega de forma explícita).
- **Sin secretos.** Ni el token, ni la cabecera `Authorization`, ni el
  cuerpo de la petición.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Final

from dashboard_stack.tokens import Caller

logger = logging.getLogger("rayito.dashboard")

PARTITION_KEY: Final = "AUDIT"
KIND_REQUESTED: Final = "requested"
KIND_OUTCOME: Final = "outcome"
NO_RESOURCE: Final = "-"
#: `TransactWriteItems` admite como mucho 100 operaciones
#: (https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_TransactWriteItems.html,
#: consultado 2026-10-13); el tope de ids por acción del panel cabe.
TRANSACTION_MAX_ITEMS: Final = 100
NOT_EXISTS: Final = "attribute_not_exists(pk)"


class AuditUnavailable(Exception):
    """No se pudo escribir el registro de auditoría."""


def utc_now() -> datetime:
    return datetime.now(UTC)


def _text(value: str) -> dict[str, str]:
    return {"S": value}


def _number(value: int) -> dict[str, str]:
    return {"N": str(value)}


class AuditLog:
    def __init__(
        self,
        client: Any,
        table: str,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._client = client
        self._table = table
        self._clock = clock

    def requested(
        self,
        caller: Caller,
        action: str,
        sandbox_ids: Sequence[str],
        *,
        request_id: str,
        source_ip: str,
    ) -> None:
        """Escribe, atómicamente, una fila por sandbox; lanza
        `AuditUnavailable` si no se pudo."""
        if not 0 < len(sandbox_ids) <= TRANSACTION_MAX_ITEMS:
            raise AuditUnavailable("tamaño")
        moment = self._clock().isoformat().replace("+00:00", "Z")
        items = [
            {
                "Put": {
                    "TableName": self._table,
                    "Item": self._item(
                        caller, action, sandbox_id, moment, request_id, source_ip, KIND_REQUESTED
                    )
                    | {"sk": _text(f"{moment}#{request_id}#{KIND_REQUESTED}#{index:03d}")},
                    "ConditionExpression": NOT_EXISTS,
                }
            }
            for index, sandbox_id in enumerate(sandbox_ids)
        ]
        try:
            self._client.transact_write_items(TransactItems=items)
        except Exception:
            raise AuditUnavailable("escritura") from None

    def outcome(
        self,
        caller: Caller,
        action: str,
        *,
        request_id: str,
        source_ip: str,
        http_status: int,
        counts: dict[str, int],
    ) -> None:
        moment = self._clock().isoformat().replace("+00:00", "Z")
        item = self._item(caller, action, NO_RESOURCE, moment, request_id, source_ip, KIND_OUTCOME)
        item["sk"] = _text(f"{moment}#{request_id}#{KIND_OUTCOME}")
        item["http_status"] = _number(http_status)
        for name, value in counts.items():
            item[name] = _number(value)
        try:
            self._client.put_item(TableName=self._table, Item=item, ConditionExpression=NOT_EXISTS)
        except Exception:
            logger.warning("dashboard audit: no se pudo escribir la fila de resultado")

    def _item(
        self,
        caller: Caller,
        action: str,
        resource: str,
        moment: str,
        request_id: str,
        source_ip: str,
        kind: str,
    ) -> dict[str, dict[str, str]]:
        return {
            "pk": _text(PARTITION_KEY),
            "kind": _text(kind),
            "occurred_at": _text(moment),
            "subject": _text(caller.subject),
            "username": _text(caller.username),
            "action": _text(action),
            "resource": _text(resource),
            "request_id": _text(request_id),
            "source_ip": _text(source_ip),
        }
