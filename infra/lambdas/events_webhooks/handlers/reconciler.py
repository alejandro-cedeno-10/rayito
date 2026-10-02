"""EventBridge Scheduler handler, `rate(<ReconcilerIntervalMinutes> minutes)`
(`infra/events-webhooks.yaml`): synthesizes a `killed{unknown}` event for any
sandbox the events table still considers open but `ListMicrovms` no longer
reports — the only path to a `killed` event when the sandbox itself never
got to call `/terminate` (a hard platform kill). The synthesized event
carries the generation and image of the sandbox's own last event (its
`STATE#` row). `timeout` is never synthesized: nothing in the table or in
`ListMicrovms` tells a deadline kill from any other one.

Decision 8: the Lambda runtime's `boto3` does not know `lambda-microvms`.
Its model is bundled in the zip under `models/` (`scripts/gen_stack_assets.py`)
and the client is built from a dedicated botocore session whose `data_path`
points there — set on that session before its loader exists, so it never
depends on import order or on which other client was built first. The
template also sets `AWS_DATA_PATH` to the same directory.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Final

import boto3
from adapters.dynamodb import DynamoDbStore
from adapters.microvms import ListMicrovmsLister, client_from_bundled_model
from domain.dedupe import reconciler_window_start_ms, synthetic_event_id
from domain.event import LifecycleEvent
from ports import EventStore, MicrovmLister, OpenSandbox

EVENTS_TABLE_ENV: Final = "EVENTS_TABLE_NAME"
INTERVAL_MINUTES_ENV: Final = "RECONCILER_INTERVAL_MINUTES"
#: Read by botocore itself; listed so the template test pins it.
AWS_DATA_PATH_ENV: Final = "AWS_DATA_PATH"
#: Every environment variable this handler (or botocore for it) reads;
#: `ReconcilerFunction` in `infra/events-webhooks.yaml` must declare each
#: (pinned by `tests/test_template_env.py`).
REQUIRED_ENV: Final = (EVENTS_TABLE_ENV, INTERVAL_MINUTES_ENV, AWS_DATA_PATH_ENV)

#: Only kill reason the reconciler can honestly claim (see module doc).
SYNTHESIZED_KILL_REASON: Final = "unknown"
_MILLIS_PER_MINUTE: Final = 60 * 1000

#: The zip's own `models/` directory (`/var/task/models` in Lambda).
BUNDLED_MODELS_DIR: Final = Path(__file__).resolve().parent.parent / "models"

_store: DynamoDbStore | None = None
_lister: ListMicrovmsLister | None = None


def _store_singleton() -> DynamoDbStore:
    global _store
    if _store is None:
        table = boto3.resource("dynamodb").Table(os.environ[EVENTS_TABLE_ENV])
        _store = DynamoDbStore(table)
    return _store


def _lister_singleton() -> ListMicrovmsLister:
    global _lister
    if _lister is None:
        _lister = ListMicrovmsLister(client_from_bundled_model(BUNDLED_MODELS_DIR))
    return _lister


def reconcile_window_ms() -> int:
    """The dedupe window: exactly the schedule's own interval, read from
    the same template parameter (never a second literal)."""
    return int(os.environ[INTERVAL_MINUTES_ENV]) * _MILLIS_PER_MINUTE


def handler(_event: dict[str, Any], _context: object) -> dict[str, int]:
    return {"synthesized": reconcile(_store_singleton(), _lister_singleton())}


def reconcile(store: EventStore, lister: MicrovmLister) -> int:
    live = lister.running_sandbox_ids()
    now_ms = int(time.time() * 1000)
    window_start_ms = reconciler_window_start_ms(now_ms, reconcile_window_ms())
    synthesized = 0
    for sandbox in store.open_sandboxes():
        if sandbox.sandbox_id in live:
            continue
        event = _synthesized_killed(sandbox, now_ms=now_ms, window_start_ms=window_start_ms)
        if store.put_event_if_absent(event):
            store.record_sandbox_state(event)
            synthesized += 1
    return synthesized


def _synthesized_killed(
    sandbox: OpenSandbox, *, now_ms: int, window_start_ms: int
) -> LifecycleEvent:
    return LifecycleEvent(
        event_id=synthetic_event_id(
            sandbox_id=sandbox.sandbox_id,
            reason=SYNTHESIZED_KILL_REASON,
            window_start_ms=window_start_ms,
        ),
        sandbox_id=sandbox.sandbox_id,
        kind="killed",
        kill_reason=SYNTHESIZED_KILL_REASON,
        generation=sandbox.generation,
        occurred_at_ms=now_ms,
        image_arn=sandbox.image_arn,
        image_version=sandbox.image_version,
    )
