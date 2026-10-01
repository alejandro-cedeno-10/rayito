"""`boto3` DynamoDB adapter over the single table `infra/events-webhooks.yaml`
declares (key design: `domain/schema.py`). Implements `ports.EventStore` and
`ports.WebhookStore`.
"""

from __future__ import annotations

import time
from typing import Any

from domain import schema
from domain.event import LifecycleEvent
from ports import Webhook


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
            "expires_at": int(time.time()) + schema.EVENT_TTL_SECONDS,
        }
        if event.kill_reason is not None:
            item["kill_reason"] = event.kill_reason
        return self._put_if_absent(item)

    def mark_sandbox_state(self, sandbox_id: str, event: LifecycleEvent) -> None:
        if event.kind == "killed":
            # No open sandbox to reconcile against once it is confirmed
            # killed; deleting the row keeps `open_sandbox_ids()` a Scan
            # over a set bounded by *currently live* sandboxes, not every
            # sandbox that ever ran.
            self._table.delete_item(Key={"pk": schema.state_pk(sandbox_id), "sk": schema.STATE_SK})
            return
        self._table.put_item(
            Item={
                "pk": schema.state_pk(sandbox_id),
                "sk": schema.STATE_SK,
                "sandbox_id": sandbox_id,
                "last_kind": event.kind,
                "last_seen_ms": event.occurred_at_ms,
            }
        )

    def open_sandbox_ids(self) -> list[str]:
        ids: list[str] = []
        scan_kwargs: dict[str, Any] = {
            "FilterExpression": "begins_with(pk, :prefix)",
            "ExpressionAttributeValues": {":prefix": "STATE#"},
        }
        while True:
            page = self._table.scan(**scan_kwargs)
            ids.extend(item["sandbox_id"] for item in page.get("Items", []))
            last_key = page.get("LastEvaluatedKey")
            if not last_key:
                return ids
            scan_kwargs["ExclusiveStartKey"] = last_key

    def mark_delivery_attempted(self, event_id: str, webhook_id: str) -> bool:
        return self._put_if_absent(
            {
                "pk": schema.delivery_pk(event_id),
                "sk": webhook_id,
                "expires_at": int(time.time()) + schema.DELIVERY_DEDUPE_TTL_SECONDS,
            }
        )

    def webhooks_for_type(self, event_type: str) -> list[Webhook]:
        page = self._table.query(
            KeyConditionExpression="pk = :pk",
            ExpressionAttributeValues={":pk": schema.WEBHOOK_PK},
        )
        return [
            Webhook(
                webhook_id=item["sk"],
                url=item["url"],
                secret_name=item["secret_name"],
                types=tuple(item["types"]),
            )
            for item in page.get("Items", [])
            if event_type in item["types"]
        ]

    def _put_if_absent(self, item: dict[str, Any]) -> bool:
        try:
            self._table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(pk) AND attribute_not_exists(sk)",
            )
            return True
        except self._table.meta.client.exceptions.ConditionalCheckFailedException:
            return False
