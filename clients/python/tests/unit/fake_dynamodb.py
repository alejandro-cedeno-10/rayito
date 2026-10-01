"""DynamoDB falso para los tests del índice de metadatos (M14).

- `FakeDynamoDb`: un cliente con la forma de botocore (`put_item(**params)`,
  `batch_get_item(**params)`, `ClientError` con `Code`) que guarda ítems en
  memoria, anota cada petición y puede dejar claves sin procesar o fallar.
- `DynamoSpySession`: una sesión boto3 falsa que devuelve ese cliente y
  apunta cada `client(servicio)` construido.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from botocore.exceptions import ClientError

from rayito import DynamoDbIndex

TABLE = "rayito-sandboxes"
REGION = "us-east-1"


def client_error(code: str, operation: str) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": f"{TABLE} secret-ish detail"},
            "ResponseMetadata": {"HTTPStatusCode": 400},
        },
        operation,
    )


@dataclass(eq=False)
class FakeDynamoDb:
    """Semántica mínima de `PutItem`/`BatchGetItem` sobre una tabla."""

    items: dict[str, dict[str, Any]] = field(default_factory=dict)
    requests: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    put_error: str | None = None
    batch_error: str | None = None
    unprocessed_rounds: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def calls(self, operation: str) -> list[dict[str, Any]]:
        return [params for name, params in self.requests if name == operation]

    def put_item(self, **params: Any) -> dict[str, Any]:
        with self.lock:
            self.requests.append(("put_item", params))
            assert set(params) == {"TableName", "Item", "ConditionExpression"}, params
            assert params["TableName"] == TABLE
            if self.put_error is not None:
                raise client_error(self.put_error, "PutItem")
            key = params["Item"]["pk"]["S"]
            if params["ConditionExpression"] == "attribute_not_exists(pk)" and key in self.items:
                raise client_error("ConditionalCheckFailedException", "PutItem")
            self.items[key] = params["Item"]
            return {}

    def batch_get_item(self, **params: Any) -> dict[str, Any]:
        with self.lock:
            self.requests.append(("batch_get_item", params))
            assert set(params) == {"RequestItems"}, params
            if self.batch_error is not None:
                raise client_error(self.batch_error, "BatchGetItem")
            request = params["RequestItems"][TABLE]
            assert set(request) == {"Keys", "ConsistentRead"}, request
            assert request["ConsistentRead"] is False
            keys = request["Keys"]
            assert 1 <= len(keys) <= 100
            if self.unprocessed_rounds > 0:
                self.unprocessed_rounds -= 1
                served, pending = keys[: len(keys) // 2], keys[len(keys) // 2 :]
            else:
                served, pending = keys, []
            found = [self.items[k["pk"]["S"]] for k in served if k["pk"]["S"] in self.items]
            response: dict[str, Any] = {"Responses": {TABLE: found}}
            if pending:
                response["UnprocessedKeys"] = {TABLE: {"Keys": pending, "ConsistentRead": False}}
            else:
                response["UnprocessedKeys"] = {}
            return response


@dataclass
class DynamoSpySession:
    """Sesión boto3 falsa: `client("dynamodb")` devuelve `api` y queda apuntado."""

    api: FakeDynamoDb = field(default_factory=FakeDynamoDb)
    region_name: str | None = REGION
    built: list[str] = field(default_factory=list)

    def client(self, service: str, **kwargs: Any) -> Any:
        self.built.append(service)
        assert service == "dynamodb", service
        return self.api


def fake_index(
    api: FakeDynamoDb | None = None, *, now: float | None = None, **kwargs: Any
) -> tuple[DynamoDbIndex, FakeDynamoDb, DynamoSpySession]:
    """Un `DynamoDbIndex` real sobre la tabla falsa, sin esperas reales; `now`
    fija el reloj con el que se descartan filas caducadas."""
    session = DynamoSpySession(api=api or FakeDynamoDb())
    index = DynamoDbIndex(TABLE, session=session, **kwargs)
    index._sleep = lambda _seconds: None
    if now is not None:
        index._clock = lambda: now
    return index, session.api, session
