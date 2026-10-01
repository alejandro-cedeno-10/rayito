"""Ports the three handlers depend on; `adapters/` implements each against
real AWS, `tests/` implements each as an in-memory fake. No handler ever
imports `boto3` directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from domain.event import LifecycleEvent


@dataclass(frozen=True)
class Webhook:
    webhook_id: str
    url: str
    secret_name: str
    types: tuple[str, ...]


class EventStore(Protocol):
    def put_event_if_absent(self, event: LifecycleEvent) -> bool:
        """`True` if this call wrote the row (first time this `event_id` is
        seen), `False` if it already existed — idempotent, never an
        exception for the duplicate case."""
        ...

    def mark_sandbox_state(self, sandbox_id: str, event: LifecycleEvent) -> None:
        """Upserts the `STATE#<sandbox_id>` row the reconciler reads to
        find sandboxes it has not seen a `killed` event for yet."""
        ...

    def open_sandbox_ids(self) -> list[str]:
        """Sandboxes whose last known state is not `killed`."""
        ...

    def undelivered_webhook_types(self, sandbox_id: str, event_id: str) -> tuple[str, ...]:
        ...


class WebhookStore(Protocol):
    def webhooks_for_type(self, event_type: str) -> list[Webhook]: ...


class SecretReader(Protocol):
    def stack_key(self) -> bytes: ...

    def webhook_secret(self, secret_name: str) -> bytes: ...


class HttpSender(Protocol):
    def post(self, url: str, headers: dict[str, str], body: bytes) -> int:
        """Returns the HTTP status code, or raises on a transport failure
        (DNS, TLS, connect, SSRF block) — the caller decides whether that is
        retryable."""
        ...


class MicrovmLister(Protocol):
    def running_sandbox_ids(self) -> set[str]: ...
