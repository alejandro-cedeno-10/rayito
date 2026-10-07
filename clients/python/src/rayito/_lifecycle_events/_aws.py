"""Adaptador `boto3` de `LifecycleEvents`: la tabla de eventos/webhooks
(DynamoDB, vía `_dynamodb`) y la clave del stack (Secrets Manager), detrás
de un único puerto (`EventsGateway`). Toda llamada a AWS pasa por
`_aws_call`, que traduce `ClientError`/`BotoCoreError` a
`WebhookException` con sólo el código de AWS: el mensaje original nombra la
tabla, el secreto y la cuenta (§6), así que ni el texto ni `__cause__` lo
conservan (`sanitize_aws_error(include_message=False)`).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from rayito._aws_region import aws_session, resolve_region
from rayito._aws_sanitize import sanitize_aws_error
from rayito._lifecycle_events import _dynamodb
from rayito.exceptions import WebhookException

if TYPE_CHECKING:
    from rayito._lifecycle_events._domain import EventRecord, WebhookInfo

T = TypeVar("T")


class EventsGateway(Protocol):
    """Lo que `LifecycleEvents` necesita de AWS; `BotoEventsGateway` es el
    adaptador real y los tests usan sesiones falsas por debajo de él."""

    def put_webhook(
        self,
        table_name: str,
        *,
        webhook_id: str,
        url: str,
        secret_name: str,
        types: tuple[str, ...],
    ) -> None: ...

    def list_webhooks(self, table_name: str) -> list[WebhookInfo]: ...

    def delete_webhook(self, table_name: str, webhook_id: str) -> None: ...

    def query_events(
        self,
        table_name: str,
        *,
        sandbox_id: str | None,
        types: tuple[str, ...] | None,
        limit: int,
        order: str,
    ) -> list[EventRecord]: ...

    def read_secret(self, secret_id: str) -> bytes: ...


def _aws_call(operation: str, invoke: Callable[[], T]) -> T:
    """Lanza fuera del `except` y desde el resumen sin mensaje, como
    `rayito._s3.s3_call`: ni `__cause__` ni `__context__` guardan el
    `ClientError` original."""
    try:
        return invoke()
    except (ClientError, BotoCoreError) as exc:
        cause = sanitize_aws_error(exc, include_message=False)
    raise WebhookException(
        f"LifecycleEvents.{operation}: AWS respondió {cause.name}", aws_code=cause.code
    ) from cause


class BotoEventsGateway:
    """`EventsGateway` sobre una sesión `boto3` (la del llamante, o la por
    defecto). Construirlo no llama a AWS; la sesión se resuelve una vez (y
    con ella la caché de credenciales) y cada método crea su recurso o
    cliente sobre ella en la región resuelta."""

    def __init__(self, session: boto3.session.Session | None, region: str | None) -> None:
        self._session = aws_session(session, region)
        self._region = resolve_region(region, session)

    def put_webhook(
        self,
        table_name: str,
        *,
        webhook_id: str,
        url: str,
        secret_name: str,
        types: tuple[str, ...],
    ) -> None:
        _aws_call(
            "register_webhook",
            lambda: _dynamodb.put_webhook(
                self._table(table_name),
                webhook_id=webhook_id,
                url=url,
                secret_name=secret_name,
                types=types,
            ),
        )

    def list_webhooks(self, table_name: str) -> list[WebhookInfo]:
        return _aws_call("list_webhooks", lambda: _dynamodb.list_webhooks(self._table(table_name)))

    def delete_webhook(self, table_name: str, webhook_id: str) -> None:
        _aws_call(
            "delete_webhook",
            lambda: _dynamodb.delete_webhook(self._table(table_name), webhook_id),
        )

    def query_events(
        self,
        table_name: str,
        *,
        sandbox_id: str | None,
        types: tuple[str, ...] | None,
        limit: int,
        order: str,
    ) -> list[EventRecord]:
        return _aws_call(
            "get_events",
            lambda: _dynamodb.query_events(
                self._table(table_name),
                sandbox_id=sandbox_id,
                types=types,
                limit=limit,
                order=order,
            ),
        )

    def read_secret(self, secret_id: str) -> bytes:
        response = _aws_call(
            "stack_key",
            lambda: self._client("secretsmanager").get_secret_value(SecretId=secret_id),
        )
        value = response.get("SecretString")
        if value is not None:
            return bytes(value.encode("utf-8"))
        binary = response.get("SecretBinary")
        if binary is None:
            raise WebhookException(
                "LifecycleEvents.stack_key: el secreto no tiene SecretString ni SecretBinary"
            )
        return bytes(binary)

    def _table(self, table_name: str) -> Any:
        resource = self._session.resource("dynamodb", region_name=self._region)
        return resource.Table(table_name)

    def _client(self, service_name: str) -> Any:
        return self._session.client(service_name, region_name=self._region)
