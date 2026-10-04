"""DynamoDB Streams handler on the events table (`NEW_IMAGE` of an `EVENT#`
row): delivers the event to every webhook subscribed to its type, signed
E2B-style, with the retry policy of `domain/delivery.py`.

Never loses a delivery: each `(event_id, webhook_id)` pair is claimed
(`attempting`) before the first attempt and finished as `delivered` or
`failed` after the last one; only `delivered` makes a later invocation skip
it (DynamoDB Streams is at-least-once). One webhook never blocks the others:
a URL the sender cannot even parse is a permanent failure of that webhook
(`invalid_url`, not retried), and anything unexpected while delivering to
one webhook is logged under `internal_error` and the next webhook is tried,
so a bad endpoint never fails the stream batch for everyone. Every attempt and backoff is fitted
into the invocation's remaining time (`context.get_remaining_time_in_millis`);
when it runs out, the pair in flight is finished as `failed` and the handler
reports that record as its first `batchItemFailures` entry, so the stream
retries from there (`FunctionResponseTypes: [ReportBatchItemFailures]` in
`infra/events-webhooks.yaml`) and every pair already `delivered` is skipped.
"""

from __future__ import annotations

import http.client
import json
import os
import time
from collections import Counter
from typing import Any, Final

import boto3
from adapters.dynamodb import DynamoDbStore
from adapters.http_client import HttpsOnlySender, SsrfBlocked
from adapters.secrets import SecretsManagerReader
from botocore.exceptions import BotoCoreError, ClientError
from domain import schema
from domain.delivery import MAX_ATTEMPTS, backoff_seconds, is_delivered, is_retryable
from domain.event import LifecycleEvent
from domain.signature import sign_delivery
from ports import EventStore, HttpSender, SecretReader, Webhook, WebhookStore

EVENTS_TABLE_ENV: Final = "EVENTS_TABLE_NAME"
#: Every environment variable this handler reads; `DelivererFunction` in
#: `infra/events-webhooks.yaml` must declare each (pinned by
#: `tests/test_template_env.py`).
REQUIRED_ENV: Final = (EVENTS_TABLE_ENV,)

#: Upper bound of one HTTP attempt (connect, TLS, request, status line):
#: generous for a receiver that is itself a cold-starting Lambda Function URL.
ATTEMPT_TIMEOUT_SECONDS: Final = 10.0
#: Left untouched at the end of the invocation (60 s, the template's
#: `Timeout`) for the last status write and the batch response.
SAFETY_MARGIN_SECONDS: Final = 2.0
#: An attempt given less time than this would only time out: stop instead.
MIN_ATTEMPT_SECONDS: Final = 1.0
_MILLIS_PER_SECOND: Final = 1000
#: Closed `delivery_failure_reason` values added for one bad webhook that
#: must not block the others (the rest are the literal reasons below).
FAILURE_INVALID_URL: Final = "invalid_url"
FAILURE_INTERNAL_ERROR: Final = "internal_error"

_store: DynamoDbStore | None = None
_secrets: SecretsManagerReader | None = None
_sender: HttpSender = HttpsOnlySender()


class _TimeBudgetExhausted(Exception):
    """No room left in this invocation for another attempt or backoff."""


class _TimeBudget:
    def __init__(self, context: Any) -> None:
        self._context = context

    def _available_seconds(self) -> float:
        remaining = self._context.get_remaining_time_in_millis() / _MILLIS_PER_SECOND
        return remaining - SAFETY_MARGIN_SECONDS

    def attempt_timeout(self) -> float:
        available = self._available_seconds()
        if available < MIN_ATTEMPT_SECONDS:
            raise _TimeBudgetExhausted
        return min(ATTEMPT_TIMEOUT_SECONDS, available)

    def sleep(self, seconds: float) -> None:
        if self._available_seconds() - seconds < MIN_ATTEMPT_SECONDS:
            raise _TimeBudgetExhausted
        time.sleep(seconds)


def _store_singleton() -> DynamoDbStore:
    global _store
    if _store is None:
        table = boto3.resource("dynamodb").Table(os.environ[EVENTS_TABLE_ENV])
        _store = DynamoDbStore(table)
    return _store


def _secrets_singleton() -> SecretsManagerReader:
    global _secrets
    if _secrets is None:
        _secrets = SecretsManagerReader(boto3.client("secretsmanager"))
    return _secrets


def handler(event: dict[str, Any], context: Any) -> dict[str, list[dict[str, str]]]:
    store = _store_singleton()
    secrets = _secrets_singleton()
    budget = _TimeBudget(context)
    counts: Counter[str] = Counter()
    for record in event.get("Records", []):
        try:
            _deliver_record(record, store, store, secrets, budget, counts)
        except _TimeBudgetExhausted:
            _log({"delivery_batch": "time_budget_exhausted", **counts})
            sequence_number = record["dynamodb"]["SequenceNumber"]
            return {"batchItemFailures": [{"itemIdentifier": sequence_number}]}
    _log({"delivery_batch": "done", **counts})
    return {"batchItemFailures": []}


def _deliver_record(
    record: dict[str, Any],
    events: EventStore,
    webhooks: WebhookStore,
    secrets: SecretReader,
    budget: _TimeBudget,
    counts: Counter[str],
) -> None:
    if record.get("eventName") != "INSERT":
        return
    image = record.get("dynamodb", {}).get("NewImage", {})
    if not image.get("pk", {}).get("S", "").startswith(schema.EVENT_PK_PREFIX):
        return  # a STATE/WEBHOOK/DELIVERY row, not a lifecycle event
    lifecycle_event = _event_from_stream_image(image)
    payload = _webhook_payload(lifecycle_event)
    for webhook in webhooks.webhooks_for_type(lifecycle_event.e2b_type):
        if not events.claim_delivery(lifecycle_event, webhook.webhook_id):
            counts["skipped"] += 1
            continue
        delivered = False
        try:
            delivered = _deliver_isolated(webhook, payload, secrets, budget)
        finally:
            events.finish_delivery(lifecycle_event, webhook.webhook_id, delivered=delivered)
        counts["delivered" if delivered else "failed"] += 1


def _deliver_isolated(
    webhook: Webhook, payload: bytes, secrets: SecretReader, budget: _TimeBudget
) -> bool:
    """`_deliver` for one webhook, where nothing but the invocation's own
    time budget escapes: running out of time must stop the batch (the
    record is retried from there), anything else only fails this webhook."""
    try:
        return _deliver(webhook, payload, secrets, budget)
    except _TimeBudgetExhausted:
        raise
    except Exception:
        _log_failure(webhook.webhook_id, FAILURE_INTERNAL_ERROR)
        return False


def _deliver(webhook: Webhook, payload: bytes, secrets: SecretReader, budget: _TimeBudget) -> bool:
    try:
        secret = secrets.read(webhook.secret_name)
    except (ClientError, BotoCoreError):
        # This webhook's secret is gone or unreadable; the others in the
        # record still get tried.
        _log_failure(webhook.webhook_id, "secret_unavailable")
        return False
    for attempt in range(MAX_ATTEMPTS):
        signed = sign_delivery(webhook_id=webhook.webhook_id, secret=secret, payload=payload)
        try:
            status = _sender.post(
                webhook.url, signed.headers, signed.body, timeout=budget.attempt_timeout()
            )
        except SsrfBlocked:
            _log_failure(webhook.webhook_id, "ssrf_blocked")
            return False  # never retryable: the URL itself is the problem
        except ValueError:
            # `UnicodeError` included: a port out of range, an unparsable
            # host or a label IDNA cannot encode. Never retryable either.
            _log_failure(webhook.webhook_id, FAILURE_INVALID_URL)
            return False
        except (OSError, http.client.HTTPException):
            status = None  # transport failure: retryable
        if status is not None and is_delivered(status):
            return True
        if status is not None and not is_retryable(status):
            _log_failure(webhook.webhook_id, f"rejected_{status}")
            return False
        if attempt < MAX_ATTEMPTS - 1:
            budget.sleep(backoff_seconds(attempt))
    _log_failure(webhook.webhook_id, "attempts_exhausted")
    return False


def _event_from_stream_image(image: dict[str, Any]) -> LifecycleEvent:
    kill_reason = image["kill_reason"]["S"] if "kill_reason" in image else None
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


def _webhook_payload(event: LifecycleEvent) -> bytes:
    """The E2B-compatible body (`docs/site` events guide)."""
    return json.dumps(
        {
            "event_id": event.event_id,
            "sandbox_id": event.sandbox_id,
            "type": event.e2b_type,
            "kill_reason": event.kill_reason,
            "generation": event.generation,
            "occurred_at_ms": event.occurred_at_ms,
            "sandbox_template_id": event.image_arn,
            "sandbox_execution_id": f"{event.sandbox_id}#{event.generation}",
        }
    ).encode("utf-8")


def _log_failure(webhook_id: str, reason: str) -> None:
    """One structured line per undelivered webhook (never its URL or
    secret): CloudWatch Logs Insights is where these are read back."""
    _log({"webhook_id": webhook_id, "delivery_failure_reason": reason})


def _log(fields: dict[str, Any]) -> None:
    print(json.dumps(fields, sort_keys=True))
