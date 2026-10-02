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

from rayito._lifecycle_events._domain import EVENT_TYPE_PREFIX, EventRecord, WebhookInfo

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


#: DynamoDB's own cap on how many `Query` pages `query_events` will turn a
#: single `get_events(types=..., limit=...)` call into: `FilterExpression`
#: (below) is applied *after* `Limit` on each page, so a type-filtered
#: query that matches rarely could otherwise paginate the entire table one
#: `Limit`-sized page at a time. 25 pages of up to 100 raw rows each is
#: already far more than `get_events` is meant for (a live tail of recent
#: events, not a bulk export); beyond that, `query_events` stops and
#: returns whatever it already found rather than scan without bound.
_MAX_QUERY_PAGES = 25


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
    and the common case. `types` becomes a `FilterExpression` on `kind`
    (the table does not index on event kind) rather than a client-side
    filter after the fact: DynamoDB applies `Limit` to the raw rows
    *before* `FilterExpression` runs, so filtering only after fetching
    `limit` rows could return fewer than `limit` matches even when more
    exist — this paginates (`ExclusiveStartKey`) until `limit` matches are
    collected, the table (this partition or the sparse index) is
    exhausted, or `_MAX_QUERY_PAGES` is reached."""
    scan_forward = order == "asc"
    kwargs: dict[str, Any] = {"ScanIndexForward": scan_forward, "Limit": limit}
    if sandbox_id is not None:
        kwargs["KeyConditionExpression"] = "pk = :pk"
        kwargs["ExpressionAttributeValues"] = {":pk": f"EVENT#{sandbox_id}"}
    else:
        kwargs["IndexName"] = GSI1_NAME
        kwargs["KeyConditionExpression"] = "gsi1pk = :pk"
        kwargs["ExpressionAttributeValues"] = {":pk": GSI1_PARTITION_VALUE}
    if types is not None:
        kinds = {event_type.removeprefix(EVENT_TYPE_PREFIX) for event_type in types}
        names = {f":kind{index}": kind for index, kind in enumerate(sorted(kinds))}
        kwargs["FilterExpression"] = f"kind IN ({', '.join(names)})"
        kwargs["ExpressionAttributeValues"].update(names)

    records: list[EventRecord] = []
    for _page_number in range(_MAX_QUERY_PAGES):
        page = table.query(**kwargs)
        records.extend(_record_from_item(item) for item in page.get("Items", []))
        last_key = page.get("LastEvaluatedKey")
        if len(records) >= limit or not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key
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
