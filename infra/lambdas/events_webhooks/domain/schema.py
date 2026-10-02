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
- ``STATE#<sandbox_id>`` / ``STATE``: the sandbox's last known event
  (`last_kind`, `last_seen_ms`, `generation`, `image_arn`,
  `image_version`), for the reconciler's "which sandboxes have no `killed`
  yet" scan. Only ever moved forward in time (a conditional write on
  `last_seen_ms`); a `killed` row stays as a tombstone, so a late line can
  never reopen the sandbox, and expires with the events (`expires_at`).
- ``WEBHOOK`` / ``<webhook_id>``: one registered webhook.
- ``DELIVERY#<event_id>`` / ``<webhook_id>``: a delivery's status
  (`delivery_status`: `attempting`, `delivered` or `failed`). DynamoDB
  Streams is at-least-once, so only `delivered` makes the deliverer skip a
  pair; anything else is attempted again. A short TTL (1 day) is enough —
  redelivery of a 7-day-old event was never going to be retried anyway.
"""

from __future__ import annotations

from typing import Final

EVENT_TTL_SECONDS: Final = 7 * 24 * 3600
DELIVERY_DEDUPE_TTL_SECONDS: Final = 24 * 3600

GSI1_NAME: Final = "gsi1"
GSI1_PARTITION_VALUE: Final = "EVENT"

EVENT_PK_PREFIX: Final = "EVENT#"
STATE_PK_PREFIX: Final = "STATE#"
STATE_SK: Final = "STATE"
WEBHOOK_PK: Final = "WEBHOOK"
DELIVERY_PK_PREFIX: Final = "DELIVERY#"

#: `delivery_status` values of a ``DELIVERY#`` row.
DELIVERY_ATTEMPTING: Final = "attempting"
DELIVERY_DELIVERED: Final = "delivered"
DELIVERY_FAILED: Final = "failed"

#: `rayd`'s terminal `EventKind` (`event.py::EVENT_KINDS`).
KILLED_KIND: Final = "killed"


def event_pk(sandbox_id: str) -> str:
    return f"{EVENT_PK_PREFIX}{sandbox_id}"


def event_sk(occurred_at_ms: int, event_id: str) -> str:
    return f"{occurred_at_ms:020d}#{event_id}"


def state_pk(sandbox_id: str) -> str:
    return f"{STATE_PK_PREFIX}{sandbox_id}"


def delivery_pk(event_id: str) -> str:
    return f"{DELIVERY_PK_PREFIX}{event_id}"
