"""Which authenticated events may move a sandbox's `STATE#` row, pure: the
forwarder and the reconciler both ask `admit` before an event is stored
and delivered, and `adapters/dynamodb.py` writes the state it returns with
an optimistic-concurrency condition on `revision`.

Why this exists on top of the MAC: `paused` and `resumed` are emitted by
`rayd` itself whenever its `/suspend` and `/resume` hooks run, and those
hooks are reachable from the sandbox's own unprivileged code (`SECURITY.md`
T2). A MAC-valid `paused`/`resumed` therefore proves that `rayd` ran the
hook, not that the platform suspended the VM. Two rules bound what such
code can do with that:

- **Order.** Each sandbox's events must move strictly forward: `created`
  only first, `paused` only after `created`/`resumed` of the same
  generation, `resumed` only with a higher generation than the last event,
  and nothing after `killed` (the tombstone). A gap is allowed (a line can
  be lost or refused), going back or repeating a position is not.
- **Rate.** `paused`/`resumed` spend from a per-sandbox token bucket kept on
  the `STATE#` row (GCRA: one field, the bucket's theoretical arrival time,
  measured on the forwarder's own clock, never the event's). `created` and
  `killed` happen once per sandbox and never spend.

An event refused here is counted under a closed reason and never stored
nor delivered.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Final

from domain.event import LifecycleEvent
from domain.schema import KILLED_KIND

#: Closed rejection reasons (counted by the forwarder's summary line).
REASON_INVALID_TRANSITION: Final = "invalid_transition"
REASON_RATE_LIMITED: Final = "rate_limited"

CREATED_KIND: Final = "created"
PAUSED_KIND: Final = "paused"
RESUMED_KIND: Final = "resumed"

#: Position of a kind inside one generation: `created` (generation 0) and
#: `resumed` (generation n) open it, `paused` closes it. `rayd` bumps the
#: generation on every accepted `/resume` and never on `/suspend`
#: (`crates/rayd/src/features/lifecycle_events.rs`).
PHASE_RANK: Final = {CREATED_KIND: 0, RESUMED_KIND: 0, PAUSED_KIND: 1}

#: Kinds that spend from the bucket: the only ones guest code can make
#: `rayd` emit at will.
RATE_LIMITED_KINDS: Final = (PAUSED_KIND, RESUMED_KIND)
#: Events a sandbox may emit back to back before the sustained rate
#: applies: ten pause/resume pairs, more than a client pausing and resuming
#: in a tight loop of its own needs.
RATE_BURST_EVENTS: Final = 20
#: Sustained rate once the burst is spent: one event every 30 s, a
#: pause/resume pair per minute, above anything an idle-timeout-driven
#: pause produces (those are minutes apart).
RATE_EMISSION_INTERVAL_MS: Final = 30 * 1000
#: GCRA's tolerance: how far ahead of now the bucket's theoretical arrival
#: time may run before an event is refused.
RATE_TOLERANCE_MS: Final = (RATE_BURST_EVENTS - 1) * RATE_EMISSION_INTERVAL_MS


@dataclass(frozen=True)
class SandboxState:
    """The `STATE#<sandbox_id>` row. `revision` is 0 only for a row written
    before admission existed (or for none at all); every admitted write
    increments it, and the adapter writes only if it is unchanged."""

    sandbox_id: str
    last_kind: str
    generation: int
    last_seen_ms: int
    image_arn: str
    image_version: str
    last_event_id: str
    rate_tat_ms: int
    revision: int

    @property
    def is_open(self) -> bool:
        return self.last_kind != KILLED_KIND


@dataclass(frozen=True)
class Admitted:
    """`state` is what the row must become. `duplicate` is the same event
    as the one last admitted (a redelivery or a retry after a failed event
    write): nothing to write, but the event may still be stored."""

    state: SandboxState
    duplicate: bool = False


@dataclass(frozen=True)
class Refused:
    reason: str


Admission = Admitted | Refused


def admit(current: SandboxState | None, event: LifecycleEvent, now_ms: int) -> Admission:
    """Never raises. `now_ms` is the caller's own clock (the forwarder's or
    the reconciler's), the bucket's only time reference."""
    if current is not None and current.last_event_id == event.event_id:
        return Admitted(current, duplicate=True)
    if current is not None and not _moves_forward(current, event):
        return Refused(REASON_INVALID_TRANSITION)
    rate_tat_ms = 0 if current is None else current.rate_tat_ms
    if event.kind in RATE_LIMITED_KINDS:
        spent = _spend(rate_tat_ms, now_ms)
        if spent is None:
            return Refused(REASON_RATE_LIMITED)
        rate_tat_ms = spent
    return Admitted(_next_state(current, event, rate_tat_ms))


def _moves_forward(current: SandboxState, event: LifecycleEvent) -> bool:
    if not current.is_open or event.kind == CREATED_KIND:
        return False
    if event.kind == KILLED_KIND:
        return True
    current_rank = PHASE_RANK.get(current.last_kind)
    if current_rank is None:
        return False
    return (event.generation, PHASE_RANK[event.kind]) > (current.generation, current_rank)


def _spend(rate_tat_ms: int, now_ms: int) -> int | None:
    """GCRA: the new theoretical arrival time, or `None` when the bucket
    is empty."""
    arrival = max(rate_tat_ms, now_ms)
    if arrival - now_ms > RATE_TOLERANCE_MS:
        return None
    return arrival + RATE_EMISSION_INTERVAL_MS


def _next_state(
    current: SandboxState | None, event: LifecycleEvent, rate_tat_ms: int
) -> SandboxState:
    fields = SandboxState(
        sandbox_id=event.sandbox_id,
        last_kind=event.kind,
        generation=event.generation,
        last_seen_ms=event.occurred_at_ms,
        image_arn=event.image_arn,
        image_version=event.image_version,
        last_event_id=event.event_id,
        rate_tat_ms=rate_tat_ms,
        revision=1,
    )
    if current is None:
        return fields
    return replace(fields, revision=current.revision + 1)
