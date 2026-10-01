"""The event model and the stdout line format, mirrored from
`rayd_core::lifecycle_events` (`crates/rayd-core/src/lifecycle_events/`) —
this is the Lambda side's only copy of that wire shape, kept in sync by the
shared vectors at `testdata/lifecycle-events/mac-vectors.json`.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from typing import Final

#: The literal token `rayd` emits (`emit.rs::LIFECYCLE_EVENT_TOKEN`).
LINE_TOKEN: Final = "rayito.event.v1"

#: `rayd`'s closed `EventKind` (`event.rs`), as the wire JSON spells it.
EVENT_KINDS: Final = ("created", "paused", "resumed", "killed")

#: The E2B-compatible webhook `type` string for each kind
#: (`ev.register_webhook(types=["sandbox.lifecycle.killed", ...])`).
E2B_EVENT_TYPE_PREFIX: Final = "sandbox.lifecycle."

#: Reasons `KillReason` carries (`event.rs`); `request` comes from `rayd`
#: itself, `timeout`/`unknown` are only ever synthesized by the reconciler.
KILL_REASONS: Final = ("request", "timeout", "unknown")


class MalformedEventLine(ValueError):
    """Raised by `parse_event_line` for anything that is not
    `rayito.event.v1 <b64url> <b64url>` with both parts valid base64url —
    the forwarder drops the line and counts it, it never raises past the
    handler boundary."""


@dataclass(frozen=True)
class LifecycleEvent:
    event_id: str
    sandbox_id: str
    kind: str
    generation: int
    occurred_at_ms: int
    image_arn: str
    image_version: str
    kill_reason: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in EVENT_KINDS:
            raise ValueError(f"kind desconocido: {self.kind!r}")
        if self.kill_reason is not None and self.kill_reason not in KILL_REASONS:
            raise ValueError(f"kill_reason desconocido: {self.kill_reason!r}")

    @property
    def e2b_type(self) -> str:
        return f"{E2B_EVENT_TYPE_PREFIX}{self.kind}"

    @classmethod
    def from_json_bytes(cls, payload: bytes) -> LifecycleEvent:
        data = json.loads(payload)
        return cls(
            event_id=data["event_id"],
            sandbox_id=data["sandbox_id"],
            kind=data["kind"],
            generation=int(data["generation"]),
            occurred_at_ms=int(data["occurred_at_ms"]),
            image_arn=data["image_arn"],
            image_version=data["image_version"],
            kill_reason=data.get("kill_reason"),
        )


def parse_event_line(line: str) -> tuple[bytes, bytes]:
    """Splits `rayito.event.v1 <b64url(event)> <b64url(mac)>` into the two
    raw byte strings the MAC check runs over. Raises `MalformedEventLine`
    for anything else — a forged prefix, wrong arity, or invalid base64 —
    so the caller can count it without the exact shape of the attack
    mattering."""
    parts = line.strip().split(" ")
    if len(parts) != 3 or parts[0] != LINE_TOKEN:
        raise MalformedEventLine(f"forma inesperada ({len(parts)} partes)")
    try:
        payload = base64.urlsafe_b64decode(_pad(parts[1]))
        mac = base64.urlsafe_b64decode(_pad(parts[2]))
    except (binascii.Error, ValueError) as error:
        raise MalformedEventLine("base64url inválido") from error
    return payload, mac


def _pad(segment: str) -> str:
    return segment + "=" * (-len(segment) % 4)
