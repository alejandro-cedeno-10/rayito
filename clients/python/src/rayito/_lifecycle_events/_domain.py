"""Dominio puro de `m15-events-webhooks`: el modelo de evento tal y como lo
lee el SDK (espejo de `rayd_core::lifecycle_events::event` y de
`infra/lambdas/events_webhooks/domain/event.py`) y `WebhookInfo`, lo que
`list_webhooks()` devuelve. Nada aquí importa `boto3`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

#: Prefijo del `type` compatible con E2B que `register_webhook(types=...)` y
#: `get_events()` usan (`sandbox.lifecycle.created|paused|resumed|killed`).
EVENT_TYPE_PREFIX: Final = "sandbox.lifecycle."

EVENT_KINDS: Final = ("created", "paused", "resumed", "killed")
KILL_REASONS: Final = ("request", "timeout", "unknown")

DEFAULT_STACK_NAME: Final = "rayito-events-webhooks"

#: `deploy(reconciler_interval_minutes=...)`'s default, mirrored by
#: `infra/events-webhooks.yaml`'s own `ReconcilerIntervalMinutes` parameter
#: default — kept as one constant so the sync API, the async API, the CLI
#: and the TypeScript mirror (`DEFAULT_RECONCILER_INTERVAL_MINUTES` in
#: `domain.ts`) can never drift from each other.
DEFAULT_RECONCILER_INTERVAL_MINUTES: Final = 5

#: `infra/events-webhooks.yaml`'s `ReconcilerIntervalMinutes` `MinValue`:
#: EventBridge Scheduler only accepts `rate(1 minute)` (singular) for 1, and
#: the template always renders `rate(N minutes)`, so 1 is excluded rather
#: than special-cased.
MIN_RECONCILER_INTERVAL_MINUTES: Final = 2

#: `get_events(limit=...)`'s default, also `_MAX_GET_EVENTS_LIMIT` in
#: `_service.py`/`_service_async.py` (a `DynamoDB` `Query`'s own practical
#: page size for this table, AWS_API_NOTES.md §25).
DEFAULT_GET_EVENTS_LIMIT: Final = 100


def event_type(kind: str) -> str:
    return f"{EVENT_TYPE_PREFIX}{kind}"


@dataclass(frozen=True)
class EventRecord:
    """Una fila de `get_events()`. `sandbox_execution_id` es
    ``f"{sandbox_id}#{generation}"`` (documentado para el payload del
    webhook); `sandbox_template_id` es el ARN de la imagen."""

    event_id: str
    sandbox_id: str
    kind: str
    kill_reason: str | None
    generation: int
    occurred_at_ms: int
    image_arn: str
    image_version: str

    @property
    def type(self) -> str:
        return event_type(self.kind)

    @property
    def sandbox_execution_id(self) -> str:
        return f"{self.sandbox_id}#{self.generation}"


@dataclass(frozen=True)
class WebhookInfo:
    """Lo que `register_webhook`/`list_webhooks` exponen; nunca el secreto
    (ni su nombre en Secrets Manager, ni mucho menos su valor)."""

    webhook_id: str
    url: str
    types: tuple[str, ...]
