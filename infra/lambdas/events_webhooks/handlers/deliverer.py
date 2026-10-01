"""DynamoDB Streams handler on the events table (`NEW_IMAGE` of an `EVENT#`
row): delivers the event to every webhook subscribed to its type, signed
E2B-style, retried up to `MAX_ATTEMPTS` with backoff. Idempotent against
DynamoDB Streams' own at-least-once delivery via
`EventStore.mark_delivery_attempted`.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import boto3

from adapters.dynamodb import DynamoDbStore
from adapters.http_client import HttpsOnlySender, SsrfBlocked
from adapters.secrets import SecretsManagerReader
from domain.event import LifecycleEvent
from domain.schema import event_pk  # noqa: F401 - documents the key shape streamed records use
from domain.signature import sign_delivery

_DYNAMODB_TABLE_ENV = "EVENTS_TABLE_NAME"
_STACK_KEY_SECRET_ENV = "STACK_KEY_SECRET_ID"

#: §7.4: "retries at most 3 times with backoff".
MAX_ATTEMPTS = 3
#: Doubles each attempt: 0.5s, 1s, 2s — well under a Lambda's own timeout.
BACKOFF_BASE_SECONDS = 0.5

_store: DynamoDbStore | None = None
_secrets: SecretsManagerReader | None = None
_sender = HttpsOnlySender()


def _store_singleton() -> DynamoDbStore:
    global _store
    if _store is None:
        table = boto3.resource("dynamodb").Table(os.environ[_DYNAMODB_TABLE_ENV])
        _store = DynamoDbStore(table)
    return _store


def _secrets_singleton() -> SecretsManagerReader:
    global _secrets
    if _secrets is None:
        _secrets = SecretsManagerReader(
            boto3.client("secretsmanager"),
            stack_key_secret_id=os.environ[_STACK_KEY_SECRET_ENV],
        )
    return _secrets


def _event_from_stream_image(image: dict[str, Any]) -> LifecycleEvent:
    kill_reason = image.get("kill_reason", {}).get("S") if "kill_reason" in image else None
    return LifecycleEvent(
        event_id=image["event_id"]["S"],
        sandbox_id=image["sandbox_id"]["S"],
        kind=image["kind"]["S"],
        generation=int(image["generation"]["N"]),
        occurred_at_ms=int(image["occurred_at_ms"]["N"]),
        image_arn=image["image_arn"]["S"],
        image_version=image["image_version"]["S"],
        kill_reason=kill_reason,
    )


def _deliver_once(webhook_id: str, url: str, secret: bytes, payload: bytes) -> int:
    signed = sign_delivery(webhook_id=webhook_id, secret=secret, payload=payload)
    return _sender.post(url, signed.headers, signed.body)


def handler(event: dict[str, Any], _context: object) -> dict[str, int]:
    store = _store_singleton()
    secrets = _secrets_singleton()
    delivered = 0
    skipped = 0
    for record in event.get("Records", []):
        if record.get("eventName") != "INSERT":
            continue
        image = record.get("dynamodb", {}).get("NewImage", {})
        if not image.get("pk", {}).get("S", "").startswith("EVENT#"):
            continue  # a STATE/WEBHOOK/DELIVERY row, not a lifecycle event
        lifecycle_event = _event_from_stream_image(image)
        payload = json.dumps(
            {
                "event_id": lifecycle_event.event_id,
                "sandbox_id": lifecycle_event.sandbox_id,
                "type": lifecycle_event.e2b_type,
                "kill_reason": lifecycle_event.kill_reason,
                "generation": lifecycle_event.generation,
                "occurred_at_ms": lifecycle_event.occurred_at_ms,
                "sandbox_template_id": lifecycle_event.image_arn,
                "sandbox_execution_id": f"{lifecycle_event.sandbox_id}#{lifecycle_event.generation}",
            }
        ).encode("utf-8")
        for webhook in store.webhooks_for_type(lifecycle_event.e2b_type):
            if not store.mark_delivery_attempted(lifecycle_event.event_id, webhook.webhook_id):
                skipped += 1
                continue
            secret = secrets.webhook_secret(webhook.secret_name)
            if _deliver_with_retries(webhook.webhook_id, webhook.url, secret, payload):
                delivered += 1
    return {"delivered": delivered, "skipped": skipped}


def _deliver_with_retries(webhook_id: str, url: str, secret: bytes, payload: bytes) -> bool:
    for attempt in range(MAX_ATTEMPTS):
        try:
            status = _deliver_once(webhook_id, url, secret, payload)
            if 200 <= status < 300:
                return True
        except SsrfBlocked:
            return False  # never retryable: the URL itself is the problem
        except OSError:
            pass  # network failure: fall through to backoff/retry
        if attempt < MAX_ATTEMPTS - 1:
            time.sleep(BACKOFF_BASE_SECONDS * (2**attempt))
    return False
