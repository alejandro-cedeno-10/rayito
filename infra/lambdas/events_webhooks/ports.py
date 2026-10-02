"""Ports the three handlers depend on; `adapters/` implements each against
real AWS, `tests/` implements each as an in-memory fake. No handler ever
imports `boto3` for anything but building those adapters.
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


@dataclass(frozen=True)
class OpenSandbox:
    """A `STATE#` row whose last event is not `killed`: what the reconciler
    needs to synthesize a faithful `killed` for it (the same generation and
    image the sandbox's own last event carried)."""

    sandbox_id: str
    generation: int
    image_arn: str
    image_version: str


class EventStore(Protocol):
    def put_event_if_absent(self, event: LifecycleEvent) -> bool:
        """`True` if this call wrote the row (first time this `event_id` is
        seen), `False` if it already existed — idempotent, never an
        exception for the duplicate case."""
        ...

    def record_sandbox_state(self, event: LifecycleEvent) -> None:
        """Moves the `STATE#<sandbox_id>` row to `event`, but only forward:
        an event older than the one already recorded, or anything after a
        `killed` (which stays as a tombstone until its TTL), changes
        nothing."""
        ...

    def open_sandboxes(self) -> list[OpenSandbox]:
        """Sandboxes whose last recorded event is not `killed`."""
        ...

    def claim_delivery(self, event_id: str, webhook_id: str) -> bool:
        """Marks the `(event_id, webhook_id)` delivery as being attempted.
        `False` only when it is already `delivered` (DynamoDB Streams is
        at-least-once); a pair left `attempting` or `failed` by an earlier
        invocation is claimed again, so a crash or a timeout never loses a
        delivery."""
        ...

    def finish_delivery(self, event_id: str, webhook_id: str, *, delivered: bool) -> None:
        """Records the outcome of a claimed delivery."""
        ...


class WebhookStore(Protocol):
    def webhooks_for_type(self, event_type: str) -> list[Webhook]: ...


class SecretReader(Protocol):
    def read(self, secret_id: str) -> bytes:
        """The secret's value (`SecretString` as UTF-8, or `SecretBinary`)."""
        ...


class HttpSender(Protocol):
    def post(self, url: str, headers: dict[str, str], body: bytes, *, timeout: float) -> int:
        """Returns the HTTP status code, or raises on a transport failure
        (DNS, TLS, connect, SSRF block); `timeout` bounds every socket
        operation of this one request. The caller decides whether a status
        or an error is retryable."""
        ...


class MicrovmLister(Protocol):
    def running_sandbox_ids(self) -> set[str]: ...
