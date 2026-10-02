"""Pure decision logic for the forwarder (T22): given one already-decoded
CloudWatch Logs record and the stack key, decide whether to accept an event
line. Kept separate from `handlers/forwarder.py` so every branch — a forged
MAC, a sandbox id that does not match its own log stream, a malformed line —
is unit tested without a CloudWatch Logs payload or a real table.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from domain.event import LifecycleEvent, MalformedEventLine, parse_event_line
from domain.mac import derive_sandbox_key, verify_mac

#: `SectionResult.error_class`-style closed strings this module can report;
#: never an exception message or the raw line.
REASON_MALFORMED = "malformed_line"
REASON_MAC_INVALID = "mac_invalid"
REASON_SANDBOX_MISMATCH = "sandbox_mismatch"

#: A Lambda MicroVM image's log stream is `YYYY/MM/DD[<imageVersion>]<microvmId>`
#: (measured, AWS_API_NOTES.md §25, Q106): the microVM id is everything
#: after the closing bracket.
LOG_STREAM_ID_SEPARATOR: Final = "]"


@dataclass(frozen=True)
class Accepted:
    event: LifecycleEvent


@dataclass(frozen=True)
class Rejected:
    reason: str


Decision = Accepted | Rejected


def decide(*, log_stream: str, message: str, stack_key: bytes) -> Decision:
    """`log_stream` must end in `]<sandbox_id>` (the image's CloudWatch
    Logs stream is named after the microVM, `AWS_API_NOTES.md` §25, Q106)
    — this is what stops a sandbox from forging another sandbox's
    events even though it technically could compute *a* valid MAC for
    itself: the id in the signed payload and the id the log stream proves
    it came from must agree."""
    try:
        payload, mac = parse_event_line(message)
    except MalformedEventLine:
        return Rejected(REASON_MALFORMED)
    try:
        event = LifecycleEvent.from_json_bytes(payload)
    except (KeyError, ValueError):
        return Rejected(REASON_MALFORMED)
    # An empty `sandbox_id` is rejected unconditionally: `endswith` would
    # otherwise accept it against any stream that ends in the separator.
    # The MAC below is what actually authenticates the event; this is
    # defence in depth.
    if not event.sandbox_id or not log_stream.endswith(LOG_STREAM_ID_SEPARATOR + event.sandbox_id):
        return Rejected(REASON_SANDBOX_MISMATCH)
    key = derive_sandbox_key(stack_key, event.sandbox_id)
    if not verify_mac(key, payload, mac):
        return Rejected(REASON_MAC_INVALID)
    return Accepted(event)
