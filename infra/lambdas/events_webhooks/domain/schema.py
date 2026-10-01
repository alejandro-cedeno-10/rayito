"""The events/webhooks table's key design (single table, on-demand,
`infra/events-webhooks.yaml`). Pure string formatting only — every actual
`boto3` call lives in `adapters/dynamodb.py`. The SDK's own reader
(`clients/python/src/rayito/_lifecycle_events/_dynamodb.py`,
`src/lifecycle-events/dynamodb.ts`) mirrors these exact formats in its own
package (a Lambda zip and the SDK are separate deployable artifacts, so
there is no import boundary to share this across) — a change here is a
change there too, pinned by `AWS_API_NOTES.md` §25.

Item shapes, by `pk` prefix:
- ``EVENT#<sandbox_id>`` / ``<occurred_at_ms padded to 20 digits>#<event_id>``:
  one lifecycle event. ``gsi1pk="EVENT"`` / ``gsi1sk`` = the same sort key,
  so ``get_events(sandbox_id=None)`` scans one sparse index instead of
  every sandbox's partition. TTL (``expires_at``) is 7 days out.
- ``STATE#<sandbox_id>`` / ``STATE``: the sandbox's last known event, for
  the reconciler's "which sandboxes have no `killed` yet" query. No TTL —
  overwritten on every event, deleted when a `killed` event lands (see
  `adapters/dynamodb.py`).
- ``WEBHOOK`` / ``<webhook_id>``: one registered webhook.
- ``DELIVERY#<event_id>`` / ``<webhook_id>``: a delivery attempt's dedupe
  marker (DynamoDB Streams is at-least-once: the deliverer may see the same
  `INSERT` record twice). A short TTL (1 day) is enough — redelivery of a
  7-day-old event was never going to be retried again anyway.
"""

from __future__ import annotations

from typing import Final

EVENT_TTL_SECONDS: Final = 7 * 24 * 3600
DELIVERY_DEDUPE_TTL_SECONDS: Final = 24 * 3600

GSI1_NAME: Final = "gsi1"
GSI1_PARTITION_VALUE: Final = "EVENT"


def event_pk(sandbox_id: str) -> str:
    return f"EVENT#{sandbox_id}"


def event_sk(occurred_at_ms: int, event_id: str) -> str:
    return f"{occurred_at_ms:020d}#{event_id}"


def state_pk(sandbox_id: str) -> str:
    return f"STATE#{sandbox_id}"


STATE_SK: Final = "STATE"
WEBHOOK_PK: Final = "WEBHOOK"


def delivery_pk(event_id: str) -> str:
    return f"DELIVERY#{event_id}"
