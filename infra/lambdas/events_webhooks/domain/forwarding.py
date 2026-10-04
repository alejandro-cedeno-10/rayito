"""Pure decision logic for the forwarder (T22): given one already-decoded
CloudWatch Logs record and the stack key, decide whether to accept an event
line. Kept separate from `handlers/forwarder.py` so every branch — a forged
MAC, a sandbox id that does not match its own log stream, a malformed or
stale line — is unit tested without a CloudWatch Logs payload or a real
table.

Order matters: the MAC is checked **before** the payload is parsed. The key
is derived from the sandbox id the log stream names, so an unauthenticated
line is rejected on a constant-time comparison and its bytes never reach a
JSON parser. The stream name alone is not identity: any holder of the
execution role (or the build role) can create a stream ending in
`]<any id>`; only the MAC authenticates.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from domain.event import LifecycleEvent, MalformedEventLine, parse_event_line
from domain.mac import derive_sandbox_key, verify_mac
from domain.schema import KILLED_KIND

#: `SectionResult.error_class`-style closed strings this module can report;
#: never an exception message or the raw line.
REASON_MALFORMED: Final = "malformed_line"
REASON_MAC_INVALID: Final = "mac_invalid"
REASON_SANDBOX_MISMATCH: Final = "sandbox_mismatch"
REASON_STALE: Final = "stale_event"

#: A Lambda MicroVM image's log stream is `YYYY/MM/DD[<imageVersion>]<microvmId>`
#: (measured, AWS_API_NOTES.md §25, Q106): the microVM id is everything
#: after the last closing bracket.
LOG_STREAM_ID_SEPARATOR: Final = "]"

#: How old an event may be when the forwarder sees it. The measured path
#: line -> CloudWatch -> forwarder takes seconds (Q106: 226 ms to ingest a
#: `paused` line, 0.3-13.9 s to the table); Lambda's asynchronous retries
#: give up after 6 h (`MaximumEventAgeInSeconds` default). A day covers
#: both with room to spare and stays far below the 7-day dedupe TTL
#: (`schema.EVENT_TTL_SECONDS`), so a line replayed once its dedupe row has
#: expired is refused as stale instead of being accepted again.
MAX_EVENT_AGE_MS: Final = 24 * 60 * 60 * 1000
#: How far in the future an event may claim to be. AWS corrects the guest's
#: wall clock on resume (AWS_API_NOTES.md §15, measured -1 s after 313 s
#: suspended); a far-future timestamp would otherwise freeze the sandbox's
#: `STATE#` row (it only moves forward in time).
MAX_CLOCK_SKEW_MS: Final = 5 * 60 * 1000

#: `rayd` mints `event_id` as 16 random bytes, hex-encoded
#: (`features/lifecycle_events.rs::EVENT_ID_BYTES`). Anything else — in
#: particular the reconciler's `synthetic-…` ids — never comes from `rayd`.
RAYD_EVENT_ID_PATTERN: Final = re.compile(r"[0-9a-f]{32}")
#: The only `kill_reason` `rayd` itself emits (`on_terminate`); `timeout`
#: and `unknown` are the reconciler's alone (`event.py::KILL_REASONS`).
RAYD_KILL_REASON: Final = "request"
#: The `image_arn` the SDK configures is the microVM image's ARN
#: (`resolve_template_arn`): `arn:<partition>:lambda:<region>:<account or
#: aws>:microvm-image:<name>`, optionally with a version suffix.
IMAGE_ARN_PATTERN: Final = re.compile(
    r"arn:aws[a-z-]*:lambda:[a-z0-9-]+:(\d{12}|aws):microvm-image:[A-Za-z0-9_.:/-]+"
)


@dataclass(frozen=True)
class Accepted:
    event: LifecycleEvent


@dataclass(frozen=True)
class Rejected:
    reason: str


Decision = Accepted | Rejected


def stream_sandbox_id(log_stream: str) -> str | None:
    """The microVM id a log stream names (everything after its last `]`),
    or `None` when there is no separator or nothing after it."""
    _head, separator, sandbox_id = log_stream.rpartition(LOG_STREAM_ID_SEPARATOR)
    if not separator or not sandbox_id:
        return None
    return sandbox_id


def decide(*, log_stream: str, message: str, stack_key: bytes, now_ms: int) -> Decision:
    """`log_stream` names the sandbox (`]<sandbox_id>`, AWS_API_NOTES.md
    §25, Q106); its `k_sbx` must verify the line's MAC, and only then is
    the payload parsed and checked: its `sandbox_id` must be the stream's,
    its fields must be what `rayd` emits, and its `occurred_at_ms` must lie
    within `MAX_EVENT_AGE_MS` before and `MAX_CLOCK_SKEW_MS` after `now_ms`
    (the forwarder's own clock, never the CloudWatch timestamp). Never
    raises for any input."""
    sandbox_id = stream_sandbox_id(log_stream)
    if sandbox_id is None:
        return Rejected(REASON_SANDBOX_MISMATCH)
    try:
        payload, mac = parse_event_line(message)
    except MalformedEventLine:
        return Rejected(REASON_MALFORMED)
    if not verify_mac(derive_sandbox_key(stack_key, sandbox_id), payload, mac):
        return Rejected(REASON_MAC_INVALID)
    try:
        event = LifecycleEvent.from_json_bytes(payload)
    except MalformedEventLine:
        return Rejected(REASON_MALFORMED)
    if event.sandbox_id != sandbox_id:
        return Rejected(REASON_SANDBOX_MISMATCH)
    if not _is_what_rayd_emits(event):
        return Rejected(REASON_MALFORMED)
    if not _is_fresh(event.occurred_at_ms, now_ms):
        return Rejected(REASON_STALE)
    return Accepted(event)


def _is_what_rayd_emits(event: LifecycleEvent) -> bool:
    """The `rayd` contract on top of the wire shape: a random hex
    `event_id`, `kill_reason` present exactly on `killed` and always
    `request`, and a microVM image ARN."""
    if not RAYD_EVENT_ID_PATTERN.fullmatch(event.event_id):
        return False
    expected_reason = RAYD_KILL_REASON if event.kind == KILLED_KIND else None
    if event.kill_reason != expected_reason:
        return False
    return IMAGE_ARN_PATTERN.fullmatch(event.image_arn) is not None


def _is_fresh(occurred_at_ms: int, now_ms: int) -> bool:
    return now_ms - MAX_EVENT_AGE_MS <= occurred_at_ms <= now_ms + MAX_CLOCK_SKEW_MS
