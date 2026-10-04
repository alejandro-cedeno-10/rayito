"""`boto3` DynamoDB adapter over the single table `infra/events-webhooks.yaml`
declares (key design: `domain/schema.py`). Implements `ports.EventStore` and
`ports.WebhookStore`.

Every conditional write uses one of the named condition expressions below;
`tests/conftest.py`'s fake table evaluates exactly these, so the fake and
the real table can never disagree on which write wins.
"""

from __future__ import annotations

import time
from typing import Any, Final

from domain import schema
from domain.event import LifecycleEvent
from ports import OpenSandbox, Webhook

#: First write of a key only (`put_event_if_absent`).
IF_ABSENT: Final = "attribute_not_exists(pk)"
#: A delivery may be (re)claimed unless it is already `delivered`.
IF_NOT_DELIVERED: Final = "attribute_not_exists(pk) OR delivery_status <> :delivered"
#: A non-`killed` event moves `STATE#` forward only: never past a `killed`
#: tombstone, never back to an older event.
IF_OPEN_AND_NOT_NEWER: Final = (
    "attribute_not_exists(pk) OR (last_kind <> :killed AND last_seen_ms <= :seen)"
)
#: A `killed` event closes `STATE#` unless it is already closed.
IF_OPEN: Final = "attribute_not_exists(pk) OR last_kind <> :killed"
#: `open_sandboxes`' scan filter.
OPEN_STATE_FILTER: Final = "begins_with(pk, :prefix) AND last_kind <> :killed"


class DynamoDbStore:
    def __init__(self, table: Any) -> None:
        # `table`: a `boto3.resource("dynamodb").Table(name)` — a resource,
        # not the low-level client, so item (de)serialization is boto3's
        # job, not this module's.
        self._table = table

    def put_event_if_absent(self, event: LifecycleEvent) -> bool:
        item = {
            "pk": schema.event_pk(event.sandbox_id),
            "sk": schema.event_sk(event.occurred_at_ms, event.event_id),
            "gsi1pk": schema.GSI1_PARTITION_VALUE,
            "gsi1sk": schema.event_sk(event.occurred_at_ms, event.event_id),
            "event_id": event.event_id,
            "sandbox_id": event.sandbox_id,
            "kind": event.kind,
            "generation": event.generation,
            "occurred_at_ms": event.occurred_at_ms,
            "image_arn": event.image_arn,
            "image_version": event.image_version,
            "expires_at": _expires_at(schema.EVENT_TTL_SECONDS),
        }
        if event.kill_reason is not None:
            item["kill_reason"] = event.kill_reason
        return self._put_if(item, IF_ABSENT, {})

    def record_sandbox_state(self, event: LifecycleEvent) -> None:
        item: dict[str, Any] = {
            "pk": schema.state_pk(event.sandbox_id),
            "sk": schema.STATE_SK,
            "sandbox_id": event.sandbox_id,
            "last_kind": event.kind,
            "last_seen_ms": event.occurred_at_ms,
            "generation": event.generation,
            "image_arn": event.image_arn,
            "image_version": event.image_version,
        }
        values: dict[str, Any] = {":killed": schema.KILLED_KIND}
        if event.kind == schema.KILLED_KIND:
            # The tombstone: keeps a late line from reopening the sandbox,
            # and expires with the events it closes.
            item["expires_at"] = _expires_at(schema.EVENT_TTL_SECONDS)
            self._put_if(item, IF_OPEN, values)
            return
        values[":seen"] = event.occurred_at_ms
        self._put_if(item, IF_OPEN_AND_NOT_NEWER, values)

    def open_sandboxes(self) -> list[OpenSandbox]:
        sandboxes: list[OpenSandbox] = []
        scan_kwargs: dict[str, Any] = {
            "FilterExpression": OPEN_STATE_FILTER,
            "ExpressionAttributeValues": {
                ":prefix": schema.STATE_PK_PREFIX,
                ":killed": schema.KILLED_KIND,
            },
        }
        while True:
            page = self._table.scan(**scan_kwargs)
            sandboxes.extend(_open_sandbox(item) for item in page.get("Items", []))
            last_key = page.get("LastEvaluatedKey")
            if not last_key:
                return sandboxes
            scan_kwargs["ExclusiveStartKey"] = last_key

    def claim_delivery(self, event: LifecycleEvent, webhook_id: str) -> bool:
        return self._put_if(
            _delivery_item(event, webhook_id, schema.DELIVERY_ATTEMPTING),
            IF_NOT_DELIVERED,
            {":delivered": schema.DELIVERY_DELIVERED},
        )

    def finish_delivery(self, event: LifecycleEvent, webhook_id: str, *, delivered: bool) -> None:
        status = schema.DELIVERY_DELIVERED if delivered else schema.DELIVERY_FAILED
        self._table.put_item(Item=_delivery_item(event, webhook_id, status))

    def webhooks_for_type(self, event_type: str) -> list[Webhook]:
        items: list[dict[str, Any]] = []
        query_kwargs: dict[str, Any] = {
            "KeyConditionExpression": "pk = :pk",
            "ExpressionAttributeValues": {":pk": schema.WEBHOOK_PK},
        }
        while True:
            page = self._table.query(**query_kwargs)
            items.extend(page.get("Items", []))
            last_key = page.get("LastEvaluatedKey")
            if not last_key:
                break
            query_kwargs["ExclusiveStartKey"] = last_key
        return [
            Webhook(
                webhook_id=item["sk"],
                url=item["url"],
                secret_name=item["secret_name"],
                types=tuple(item["types"]),
            )
            for item in items
            if event_type in item["types"]
        ]

    def _put_if(self, item: dict[str, Any], condition: str, values: dict[str, Any]) -> bool:
        """`True` if the conditional write happened, `False` if its
        condition did not hold — never an exception for that case."""
        kwargs: dict[str, Any] = {"Item": item, "ConditionExpression": condition}
        if values:
            kwargs["ExpressionAttributeValues"] = values
        try:
            self._table.put_item(**kwargs)
            return True
        except self._table.meta.client.exceptions.ConditionalCheckFailedException:
            return False


def _expires_at(ttl_seconds: int) -> int:
    return int(time.time()) + ttl_seconds


def _delivery_item(event: LifecycleEvent, webhook_id: str, status: str) -> dict[str, Any]:
    return {
        "pk": schema.delivery_pk(event.sandbox_id, event.event_id),
        "sk": webhook_id,
        "delivery_status": status,
        "expires_at": _expires_at(schema.DELIVERY_DEDUPE_TTL_SECONDS),
    }


def _open_sandbox(item: dict[str, Any]) -> OpenSandbox:
    return OpenSandbox(
        sandbox_id=item["sandbox_id"],
        generation=int(item.get("generation", 0)),
        image_arn=item.get("image_arn", ""),
        image_version=item.get("image_version", ""),
    )
