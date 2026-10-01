"""Adaptador `boto3` sobre la tabla de `infra/events-webhooks.yaml`
(`LifecycleEvents.register_webhook/list_webhooks/delete_webhook/get_events`).
El diseño de claves (``WEBHOOK``/``<id>``, ``EVENT#<sandbox_id>``/
``<occurred_at_ms>#<event_id>``, el GSI ``gsi1`` para listar todos los
sandboxes) es el mismo que usan los Lambdas
(`infra/lambdas/events_webhooks/domain/schema.py`) — dos paquetes
desplegables distintos (un zip de Lambda y este SDK), así que no hay un
`import` que compartir entre ellos; un cambio aquí es un cambio allí,
fijado por `AWS_API_NOTES.md` §25.
"""

from __future__ import annotations

from typing import Any

from rayito._lifecycle_events._domain import EventRecord, WebhookInfo

GSI1_NAME = "gsi1"
GSI1_PARTITION_VALUE = "EVENT"
WEBHOOK_PK = "WEBHOOK"


def _event_sk(occurred_at_ms: int, event_id: str) -> str:
    return f"{occurred_at_ms:020d}#{event_id}"


def put_webhook(
    table: Any, *, webhook_id: str, url: str, secret_name: str, types: tuple[str, ...]
) -> None:
    table.put_item(
        Item={
            "pk": WEBHOOK_PK,
            "sk": webhook_id,
            "url": url,
            "secret_name": secret_name,
            "types": list(types),
        }
    )


def list_webhooks(table: Any) -> list[WebhookInfo]:
    items: list[dict[str, Any]] = []
    query_kwargs: dict[str, Any] = {
        "KeyConditionExpression": "pk = :pk",
        "ExpressionAttributeValues": {":pk": WEBHOOK_PK},
    }
    while True:
        page = table.query(**query_kwargs)
        items.extend(page.get("Items", []))
        last_key = page.get("LastEvaluatedKey")
        if not last_key:
            break
        query_kwargs["ExclusiveStartKey"] = last_key
    return [
        WebhookInfo(webhook_id=item["sk"], url=item["url"], types=tuple(item["types"]))
        for item in items
    ]


def delete_webhook(table: Any, webhook_id: str) -> None:
    table.delete_item(Key={"pk": WEBHOOK_PK, "sk": webhook_id})


def query_events(
    table: Any,
    *,
    sandbox_id: str | None,
    types: tuple[str, ...] | None,
    limit: int,
    order: str,
) -> list[EventRecord]:
    """`sandbox_id=None` queries the sparse `gsi1` index (every sandbox);
    otherwise it queries that sandbox's own partition directly — cheaper,
    and the common case. `types` filters client-side (the table does not
    index on event kind, and the expected row count per sandbox is small)."""
    scan_forward = order == "asc"
    if sandbox_id is not None:
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": "pk = :pk",
            "ExpressionAttributeValues": {":pk": f"EVENT#{sandbox_id}"},
            "ScanIndexForward": scan_forward,
            "Limit": limit,
        }
        page = table.query(**kwargs)
    else:
        kwargs = {
            "IndexName": GSI1_NAME,
            "KeyConditionExpression": "gsi1pk = :pk",
            "ExpressionAttributeValues": {":pk": GSI1_PARTITION_VALUE},
            "ScanIndexForward": scan_forward,
            "Limit": limit,
        }
        page = table.query(**kwargs)
    records = [_record_from_item(item) for item in page.get("Items", [])]
    if types is not None:
        records = [record for record in records if record.type in types]
    return records[:limit]


def _record_from_item(item: dict[str, Any]) -> EventRecord:
    return EventRecord(
        event_id=item["event_id"],
        sandbox_id=item["sandbox_id"],
        kind=item["kind"],
        kill_reason=item.get("kill_reason"),
        generation=int(item["generation"]),
        occurred_at_ms=int(item["occurred_at_ms"]),
        image_arn=item["image_arn"],
        image_version=item["image_version"],
    )
