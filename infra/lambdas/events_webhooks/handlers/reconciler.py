"""EventBridge Scheduler handler, `rate(5 minutes)` (`RECONCILER_INTERVAL`
in `infra/events-webhooks.yaml`): synthesizes a `killed` event for any
sandbox the events table still considers open but `ListMicrovms` no longer
reports — the only path to a `killed` event when the sandbox itself never
got to call `/terminate` (a hard platform kill, a spot-style interruption).
Decision 8: `lambda-microvms` is bundled under ``models/`` in this zip and
`AWS_DATA_PATH` is set to it before the client is built, since the Lambda
runtime's own `boto3` does not know this service.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import boto3

from adapters.dynamodb import DynamoDbStore
from adapters.microvms import ListMicrovmsLister
from domain.dedupe import reconciler_window_start_ms, synthetic_event_id
from domain.event import LifecycleEvent

_DYNAMODB_TABLE_ENV = "EVENTS_TABLE_NAME"
_IMAGE_ARN_ENV = "RECONCILER_IMAGE_ARN"
_IMAGE_VERSION_ENV = "RECONCILER_IMAGE_VERSION"

#: Matches the EventBridge Scheduler rule's own `rate(5 minutes)`
#: (`infra/events-webhooks.yaml`); kept as a constant here only for
#: `reconciler_window_start_ms`'s dedupe bucket, never to compute the
#: schedule itself.
RECONCILE_WINDOW_MS = 5 * 60 * 1000

_LAMBDA_MICROVMS_MODEL_DIR = str(Path(__file__).resolve().parent.parent / "models")

_store: DynamoDbStore | None = None
_lister: ListMicrovmsLister | None = None


def _store_singleton() -> DynamoDbStore:
    global _store
    if _store is None:
        table = boto3.resource("dynamodb").Table(os.environ[_DYNAMODB_TABLE_ENV])
        _store = DynamoDbStore(table)
    return _store


def _lister_singleton() -> ListMicrovmsLister:
    global _lister
    if _lister is None:
        # Set once, before the first `lambda-microvms` client is built in
        # this execution environment — never overwritten afterwards, so a
        # concurrent `boto3` client for another service in the same
        # environment is unaffected.
        os.environ.setdefault("AWS_DATA_PATH", _LAMBDA_MICROVMS_MODEL_DIR)
        _lister = ListMicrovmsLister(boto3.client("lambda-microvms"))
    return _lister


def handler(_event: dict[str, Any], _context: object) -> dict[str, int]:
    store = _store_singleton()
    lister = _lister_singleton()
    live = lister.running_sandbox_ids()
    now_ms = int(time.time() * 1000)
    window_start_ms = reconciler_window_start_ms(now_ms, RECONCILE_WINDOW_MS)
    synthesized = 0
    for sandbox_id in store.open_sandbox_ids():
        if sandbox_id in live:
            continue
        event = LifecycleEvent(
            event_id=synthetic_event_id(
                sandbox_id=sandbox_id, reason="unknown", window_start_ms=window_start_ms
            ),
            sandbox_id=sandbox_id,
            kind="killed",
            kill_reason="unknown",
            generation=0,
            occurred_at_ms=now_ms,
            image_arn=os.environ.get(_IMAGE_ARN_ENV, ""),
            image_version=os.environ.get(_IMAGE_VERSION_ENV, ""),
        )
        if store.put_event_if_absent(event):
            store.mark_sandbox_state(sandbox_id, event)
            synthesized += 1
    return {"synthesized": synthesized}
