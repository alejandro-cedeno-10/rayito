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
from domain.admission import Admission, Refused, SandboxState, admit
from domain.event import LifecycleEvent
from ports import OpenSandbox, Webhook

#: First write of a key only (`put_event_if_absent`).
IF_ABSENT: Final = "attribute_not_exists(pk)"
#: A delivery may be (re)claimed unless it is already `delivered`.
IF_NOT_DELIVERED: Final = "attribute_not_exists(pk) OR delivery_status <> :delivered"
#: `admit`'s optimistic concurrency: the `STATE#` row is written only if
#: nobody changed it since it was read. Every admitted write sets
#: `revision >= 1`, so an absent row (read as revision 0) and a row from
#: before admission existed (no `revision` at all) both pass with `:revision
#: = 0`, and a row another writer created in between does not.
IF_REVISION_UNCHANGED: Final = "attribute_not_exists(revision) OR revision = :revision"
#: Reads and conditional writes of one `admit` before giving up: two
#: writers racing on one sandbox (the forwarder and the reconciler) settle
#: in one retry; more means the table is misbehaving.
MAX_ADMISSION_ATTEMPTS: Final = 3


class AdmissionContended(RuntimeError):
    """`admit` lost every optimistic-concurrency race it ran; the forwarder
    treats it as a failed write (the batch is retried)."""


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

    def admit(self, event: LifecycleEvent, now_ms: int) -> Admission:
        for _attempt in range(MAX_ADMISSION_ATTEMPTS):
            current = self._read_state(event.sandbox_id)
            admission = admit(current, event, now_ms)
            if isinstance(admission, Refused) or admission.duplicate:
                return admission
            expected_revision = 0 if current is None else current.revision
            if self._put_if(
                _state_item(admission.state),
                IF_REVISION_UNCHANGED,
                {":revision": expected_revision},
            ):
                return admission
        raise AdmissionContended("STATE# cambió en cada intento")

    def open_sandboxes(self) -> list[OpenSandbox]:
        sandboxes: list[OpenSandbox] = []
        query_kwargs: dict[str, Any] = {
            "IndexName": schema.OPEN_INDEX_NAME,
            "KeyConditionExpression": f"{schema.OPEN_INDEX_ATTRIBUTE} = :open",
            "ExpressionAttributeValues": {":open": schema.OPEN_PARTITION_VALUE},
        }
        while True:
            page = self._table.query(**query_kwargs)
            sandboxes.extend(_open_sandbox(item) for item in page.get("Items", []))
            last_key = page.get("LastEvaluatedKey")
            if not last_key:
                return sandboxes
            query_kwargs["ExclusiveStartKey"] = last_key

    def _read_state(self, sandbox_id: str) -> SandboxState | None:
        response = self._table.get_item(
            Key={"pk": schema.state_pk(sandbox_id), "sk": schema.STATE_SK},
            ConsistentRead=True,
        )
        item = response.get("Item")
        return None if item is None else _sandbox_state(item)

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


def _state_item(state: SandboxState) -> dict[str, Any]:
    """An open sandbox carries `open_pk` (the sparse `open` index the
    reconciler queries); the `killed` tombstone drops it and expires with
    the events it closes, so a late line can never reopen the sandbox."""
    item: dict[str, Any] = {
        "pk": schema.state_pk(state.sandbox_id),
        "sk": schema.STATE_SK,
        "sandbox_id": state.sandbox_id,
        "last_kind": state.last_kind,
        "last_seen_ms": state.last_seen_ms,
        "generation": state.generation,
        "image_arn": state.image_arn,
        "image_version": state.image_version,
        "last_event_id": state.last_event_id,
        "rate_tat_ms": state.rate_tat_ms,
        "revision": state.revision,
    }
    if state.is_open:
        item[schema.OPEN_INDEX_ATTRIBUTE] = schema.OPEN_PARTITION_VALUE
    else:
        item["expires_at"] = _expires_at(schema.EVENT_TTL_SECONDS)
    return item


def _sandbox_state(item: dict[str, Any]) -> SandboxState:
    """Reads a `STATE#` row; a row from before admission existed lacks the
    newer fields and reads as revision 0 with a full bucket."""
    return SandboxState(
        sandbox_id=item["sandbox_id"],
        last_kind=item["last_kind"],
        generation=int(item.get("generation", 0)),
        last_seen_ms=int(item.get("last_seen_ms", 0)),
        image_arn=item.get("image_arn", ""),
        image_version=item.get("image_version", ""),
        last_event_id=item.get("last_event_id", ""),
        rate_tat_ms=int(item.get("rate_tat_ms", 0)),
        revision=int(item.get("revision", 0)),
    )


def _open_sandbox(item: dict[str, Any]) -> OpenSandbox:
    return OpenSandbox(
        sandbox_id=item["sandbox_id"],
        generation=int(item.get("generation", 0)),
        image_arn=item.get("image_arn", ""),
        image_version=item.get("image_version", ""),
    )
