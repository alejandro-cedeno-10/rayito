"""`LifecycleEvents` (M15, m15-events-webhooks): fachada sobre
`OptionalStacks` (`deploy`/`status`/`destroy`) más las operaciones propias
de la función (`register_webhook`/`list_webhooks`/`delete_webhook`/
`get_events`), todas directas sobre DynamoDB — ninguna pasa por un Lambda.
Construirlo no hace ninguna llamada a AWS.
"""

from __future__ import annotations

import re
import secrets as _secrets_module
from typing import TYPE_CHECKING, Final

import boto3

from rayito._lifecycle_events._domain import (
    DEFAULT_GET_EVENTS_LIMIT,
    DEFAULT_RECONCILER_INTERVAL_MINUTES,
    DEFAULT_STACK_NAME,
    EventRecord,
    WebhookInfo,
)
from rayito._lifecycle_events._aws import BotoEventsGateway, EventsGateway
from rayito._lifecycle_events._keys import derive_sandbox_key
from rayito._lifecycle_events._section import LifecycleEventsSection
from rayito._secrets import DEFAULT_SECRET_PREFIX, resolve_secret_id
from rayito._stacks._model import StackComponent, StackStatus
from rayito._stacks._registry import component_by_name
from rayito._stacks._service import OptionalStacks
from rayito.exceptions import InvalidArgumentException, WebhookException

if TYPE_CHECKING:
    from collections.abc import Sequence

#: `register_webhook(secret_name=...)` resolves under this prefix, matching
#: the deliverer's own IAM statement (`infra/events-webhooks.yaml`'s
#: `ReadWebhookSecrets` resource pattern) — the webhook's signing secret
#: must already exist (create it with `SecretStore(prefix="rayito/webhooks/")`);
#: this facade only records which one to use, it never creates a secret.
WEBHOOK_SECRET_PREFIX: Final = f"{DEFAULT_SECRET_PREFIX}webhooks/"

_COMPONENT: StackComponent = component_by_name("events-webhooks")  # type: ignore[assignment]

_EVENT_TYPE_PATTERN: Final = re.compile(r"sandbox\.lifecycle\.(created|paused|resumed|killed)")
#: Coincides with `DEFAULT_GET_EVENTS_LIMIT` today, but is a different
#: knob (the hard ceiling `limit=` may never exceed, not the default when
#: it is omitted) — a `DynamoDB` `Query`'s own practical page size for this
#: table (AWS_API_NOTES.md §25), kept separate on purpose.
_MAX_GET_EVENTS_LIMIT: Final = 100
_MIN_GET_EVENTS_LIMIT: Final = 1


def _validate_types(types: Sequence[str] | None) -> tuple[str, ...] | None:
    if types is None:
        return None
    resolved = tuple(types)
    for event_type in resolved:
        if not _EVENT_TYPE_PATTERN.fullmatch(event_type):
            raise InvalidArgumentException(
                f"tipo de evento desconocido: {event_type!r} "
                "(sandbox.lifecycle.{created,paused,resumed,killed})"
            )
    return resolved


class LifecycleEvents:
    def __init__(
        self,
        *,
        stack_name: str = DEFAULT_STACK_NAME,
        region: str | None = None,
        session: boto3.session.Session | None = None,
        stacks: OptionalStacks | None = None,
        gateway: EventsGateway | None = None,
    ) -> None:
        self._stack_name = stack_name
        self._stacks = stacks or OptionalStacks(region=region, session=session)
        self._gateway: EventsGateway = gateway or BotoEventsGateway(session, region)
        self._table_name: str | None = None
        self._stack_key_secret_id: str | None = None
        self._stack_key_cache: bytes | None = None

    # -- OptionalStack facade --------------------------------------------

    def deploy(
        self,
        *,
        artifact_bucket: str,
        log_group_name: str,
        reconciler_interval_minutes: int = DEFAULT_RECONCILER_INTERVAL_MINUTES,
        tags: dict[str, str] | None = None,
        wait: bool = True,
    ) -> StackStatus:
        """Despliega `infra/events-webhooks.yaml`.

        Coste y activación
        -------------------
        Activa: una llamada explícita a este método (o `rayito events deploy`).
        Recursos y llamadas AWS: un secreto de Secrets Manager (la clave
            HMAC del stack), una tabla DynamoDB on-demand con streams, tres
            funciones Lambda, una suscripción de CloudWatch Logs, una cola
            SQS de fallos del deliverer y una regla de EventBridge Scheduler
            (`rate(reconciler_interval_minutes)`, mínimo 2).
        Coste aproximado: $0,40/mes el secreto; DynamoDB, Lambda y SQS son
            on-demand/por uso ($0 en reposo); el Scheduler invoca el
            reconciliador cada `reconciler_interval_minutes` minutos
            (~$0,0000002 por invocación, us-east-1, 2026-09-30).
        IAM: la política `EventsOperatorPolicy` que la pila emite (adjúntala
            a quien llame a `events=`/`register_webhook`/`list_webhooks`/
            `delete_webhook`/`get_events`): `PutItem`/`Query`/`DeleteItem`
            sobre la tabla y su índice, `DescribeStacks` sobre la pila y
            `GetSecretValue` sobre la clave del stack.
        Cómo apagarla: `destroy()` (fuerza el borrado del secreto: cualquier
            webhook registrado deja de poder verificarse).
        Ejemplo:
            ev = LifecycleEvents()
            ev.deploy(artifact_bucket="mi-bucket", log_group_name="/rayito/rayito-base")
        """
        status = self._stacks.deploy(
            _COMPONENT,
            stack_name=self._stack_name,
            artifact_bucket=artifact_bucket,
            parameters={
                "ArtifactBucket": artifact_bucket,
                "LogGroupName": log_group_name,
                "ReconcilerIntervalMinutes": str(reconciler_interval_minutes),
            },
            tags=tags or {},
            wait=wait,
        )
        self._table_name = status.outputs.get("EventsTableName")
        self._stack_key_secret_id = status.outputs.get("StackKeySecretArn")
        return status

    def status(self) -> StackStatus | None:
        return self._stacks.status(_COMPONENT, stack_name=self._stack_name)

    def destroy(self, *, wait: bool = True) -> None:
        """Borra la pila entera: el secreto del stack (force-delete), la
        tabla con todos sus eventos y webhooks, las tres Lambdas, la
        suscripción, la cola de fallos y el scheduler. No toca los secretos
        de cada webhook (`rayito/webhooks/...`, de `SecretStore`) ni el log
        group de la imagen, que esta pila nunca creó."""
        self._stacks.destroy(_COMPONENT, stack_name=self._stack_name, wait=wait)

    # -- Webhooks ----------------------------------------------------------

    def register_webhook(self, url: str, *, secret_name: str, types: Sequence[str]) -> WebhookInfo:
        if not url.startswith("https://"):
            raise InvalidArgumentException("register_webhook: url debe ser https://")
        validated_types = _validate_types(types)
        if not validated_types:
            raise InvalidArgumentException("register_webhook: types no puede estar vacío")
        webhook_id = _secrets_module.token_hex(8)
        resolved_secret_id = resolve_secret_id(secret_name, WEBHOOK_SECRET_PREFIX)
        self._gateway.put_webhook(
            self._resolve_table_name(),
            webhook_id=webhook_id,
            url=url,
            secret_name=resolved_secret_id,
            types=validated_types,
        )
        return WebhookInfo(webhook_id=webhook_id, url=url, types=validated_types)

    def list_webhooks(self) -> list[WebhookInfo]:
        return self._gateway.list_webhooks(self._resolve_table_name())

    def delete_webhook(self, webhook_id: str) -> None:
        self._gateway.delete_webhook(self._resolve_table_name(), webhook_id)

    # -- Events --------------------------------------------------------------

    def get_events(
        self,
        *,
        sandbox_id: str | None = None,
        types: Sequence[str] | None = None,
        limit: int = DEFAULT_GET_EVENTS_LIMIT,
        order: str = "desc",
    ) -> list[EventRecord]:
        if not _MIN_GET_EVENTS_LIMIT <= limit <= _MAX_GET_EVENTS_LIMIT:
            raise InvalidArgumentException(
                f"get_events: limit entre {_MIN_GET_EVENTS_LIMIT} y {_MAX_GET_EVENTS_LIMIT}"
            )
        if order not in ("asc", "desc"):
            raise InvalidArgumentException("get_events: order debe ser 'asc' o 'desc'")
        validated_types = _validate_types(types)
        return self._gateway.query_events(
            self._resolve_table_name(),
            sandbox_id=sandbox_id,
            types=validated_types,
            limit=limit,
            order=order,
        )

    # -- Internals used by `_feature_options.plan_features` (events=) ------

    def _build_section(
        self, *, sandbox_id: str, image_arn: str, image_version: str
    ) -> LifecycleEventsSection:
        """Builds the per-sandbox `ConfigureSection` once `sandbox_id` is
        known (after `run-microvm`). Not yet called by `create()` — see
        ADR-020's "Known integration gap" note; this method is unit tested
        in isolation so that wiring is a pure plumbing change when it
        lands."""
        stack_key = self._stack_key()
        sandbox_key = derive_sandbox_key(stack_key, sandbox_id)
        return LifecycleEventsSection(
            sandbox_key=sandbox_key,
            sandbox_id=sandbox_id,
            image_arn=image_arn,
            image_version=image_version,
        )

    def _stack_key(self) -> bytes:
        if self._stack_key_cache is None:
            self._stack_key_cache = self._gateway.read_secret(
                self._resolve_stack_key_secret_id()
            )
        return self._stack_key_cache

    def _resolve_stack_key_secret_id(self) -> str:
        if self._stack_key_secret_id is not None:
            return self._stack_key_secret_id
        status = self.status()
        if status is None or "StackKeySecretArn" not in status.outputs:
            raise WebhookException(
                f"la pila {self._stack_name!r} no está desplegada (llama a deploy() primero)"
            )
        self._stack_key_secret_id = status.outputs["StackKeySecretArn"]
        return self._stack_key_secret_id

    def _resolve_table_name(self) -> str:
        if self._table_name is not None:
            return self._table_name
        status = self.status()
        if status is None or "EventsTableName" not in status.outputs:
            raise WebhookException(
                f"la pila {self._stack_name!r} no está desplegada (llama a deploy() primero)"
            )
        self._table_name = status.outputs["EventsTableName"]
        return self._table_name
