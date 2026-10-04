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


#: The wire's integers are `rayd`'s `u64` (`event.rs`): anything outside
#: `[0, 2**64)` cannot come from `rayd` and is refused before it reaches
#: DynamoDB (whose Number type would otherwise accept it).
MAX_WIRE_INT: Final = 2**64 - 1

#: Upper bound of every string field of an event line (`event_id`,
#: `sandbox_id`, `kind`, `image_arn`, `image_version`, `kill_reason`). A
#: microVM image ARN is well under this; the cap keeps a MAC-valid line from
#: growing the DynamoDB item (400 KB limit) or the webhook body without bound.
MAX_FIELD_CHARS: Final = 512


class MalformedEventLine(ValueError):
    """Raised by `parse_event_line` and `LifecycleEvent.from_json_bytes` for
    anything that is not `rayito.event.v1 <b64url> <b64url>` carrying a JSON
    object of the exact wire shape — the forwarder drops the line and counts
    it, it never raises past the handler boundary."""


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
        """Strict parse of the wire JSON: an object whose string fields are
        strings of at most `MAX_FIELD_CHARS` and whose integer fields are
        JSON integers in `[0, MAX_WIRE_INT]` (never a float, a bool or a
        numeric string). Every other shape — a list, a scalar, a missing
        field, an absurdly deep nesting, an out-of-range number — raises
        `MalformedEventLine`, never `TypeError`/`OverflowError`/
        `RecursionError`."""
        data = _load_object(payload)
        kill_reason = (
            None if data.get("kill_reason") is None else _string_field(data, "kill_reason")
        )
        try:
            return cls(
                event_id=_string_field(data, "event_id"),
                sandbox_id=_string_field(data, "sandbox_id"),
                kind=_string_field(data, "kind"),
                generation=_integer_field(data, "generation"),
                occurred_at_ms=_integer_field(data, "occurred_at_ms"),
                image_arn=_string_field(data, "image_arn"),
                image_version=_string_field(data, "image_version"),
                kill_reason=kill_reason,
            )
        except ValueError as error:
            raise MalformedEventLine("valor fuera del contrato") from error


def _load_object(payload: bytes) -> dict[str, object]:
    try:
        data = json.loads(payload)
    except (ValueError, RecursionError) as error:
        raise MalformedEventLine("JSON inválido") from error
    if not isinstance(data, dict):
        raise MalformedEventLine("el evento no es un objeto JSON")
    return data


def _string_field(data: dict[str, object], name: str) -> str:
    value = data.get(name)
    if not isinstance(value, str) or len(value) > MAX_FIELD_CHARS:
        raise MalformedEventLine(f"campo de texto inválido: {name}")
    return value


def _integer_field(data: dict[str, object], name: str) -> int:
    value = data.get(name)
    if type(value) is not int or not 0 <= value <= MAX_WIRE_INT:
        raise MalformedEventLine(f"campo entero inválido: {name}")
    return value


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
