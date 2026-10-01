"""Deterministic ids for the reconciler's synthesized events (§7.4: "the
reconciler synthesizes killed{timeout|unknown} ... and deduplicates").
`rayd` itself mints a random `event_id` (its own `OsRandomSource`); the
reconciler instead derives one from facts it already knows, so running it
twice over the same gap in the timeline writes the same row twice (an
idempotent `PutItem`, not two events) rather than two distinct ones.
"""

from __future__ import annotations

import hashlib

#: Matches `event.py::KILL_REASONS` — kept separate so this module stays
#: free of any dependency on the wire-line parser.
SYNTHETIC_KILL_REASONS = ("timeout", "unknown")


def synthetic_event_id(*, sandbox_id: str, reason: str, window_start_ms: int) -> str:
    """One id per `(sandbox_id, reason, window)` triple. `window_start_ms`
    is the reconciler's own run's floor (its schedule's `rate(5 minutes)`
    truncation), not the event's timestamp — two runs that both notice the
    same gap inside the same window collide on purpose; two different gaps
    (a sandbox that comes back and disappears again later) get distinct
    ids."""
    if reason not in SYNTHETIC_KILL_REASONS:
        raise ValueError(f"reason sintético desconocido: {reason!r}")
    digest = hashlib.sha256(
        f"reconciler|{sandbox_id}|{reason}|{window_start_ms}".encode("utf-8")
    ).hexdigest()
    return f"synthetic-{digest[:32]}"


def reconciler_window_start_ms(now_ms: int, window_ms: int) -> int:
    """Floors `now_ms` to the start of its `window_ms`-wide bucket
    (`rate(5 minutes)` -> `window_ms = 300_000`), the same floor every
    invocation inside that bucket computes."""
    if window_ms <= 0:
        raise ValueError("window_ms debe ser positivo")
    return (now_ms // window_ms) * window_ms
