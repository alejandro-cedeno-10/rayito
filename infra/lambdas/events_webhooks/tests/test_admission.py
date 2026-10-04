"""`domain.admission.admit`: a sandbox's events only move forward, and the
kinds its own unprivileged code can make `rayd` emit (`paused`/`resumed`,
through the loopback-reachable hooks) are rate limited per sandbox, so a
suspend/resume loop can neither flood the table and the webhooks nor
replay an older position.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from domain.admission import (
    RATE_BURST_EVENTS,
    RATE_EMISSION_INTERVAL_MS,
    REASON_INVALID_TRANSITION,
    REASON_RATE_LIMITED,
    Admitted,
    Refused,
    SandboxState,
    admit,
)
from domain.event import LifecycleEvent

NOW_MS = 1_790_000_000_000


def _event(kind: str, generation: int = 0, event_id: str | None = None) -> LifecycleEvent:
    return LifecycleEvent(
        event_id=event_id or f"{kind}-{generation}",
        sandbox_id="sbx-1",
        kind=kind,
        generation=generation,
        occurred_at_ms=NOW_MS,
        image_arn="arn:image",
        image_version="1",
        kill_reason="request" if kind == "killed" else None,
    )


def _state_after(*events: LifecycleEvent, now_ms: int = NOW_MS) -> SandboxState | None:
    state: SandboxState | None = None
    for event in events:
        admission = admit(state, event, now_ms)
        assert isinstance(admission, Admitted), (event, admission)
        state = admission.state
    return state


def test_the_first_event_of_a_sandbox_is_admitted_whatever_its_kind() -> None:
    # `created` can be lost (a forwarder outage): the next event still
    # bootstraps the row instead of being refused forever.
    for kind in ("created", "paused", "resumed", "killed"):
        admission = admit(None, _event(kind), NOW_MS)
        assert isinstance(admission, Admitted)
        assert admission.state.revision == 1


def test_a_normal_lifecycle_moves_forward() -> None:
    state = _state_after(
        _event("created"),
        _event("paused", 0),
        _event("resumed", 1),
        _event("paused", 1),
        _event("resumed", 2),
        _event("killed", 2),
    )
    assert state is not None
    assert (state.last_kind, state.generation, state.revision) == ("killed", 2, 6)
    assert not state.is_open


@pytest.mark.parametrize(
    ("history", "late"),
    [
        ((_event("created"),), _event("created", event_id="created-again")),
        ((_event("created"), _event("paused", 0)), _event("paused", 0, "paused-again")),
        ((_event("created"),), _event("resumed", 0)),
        (
            (_event("created"), _event("paused", 0), _event("resumed", 1)),
            _event("paused", 0, "old"),
        ),
        ((_event("created"), _event("killed")), _event("resumed", 1)),
        ((_event("created"), _event("killed")), _event("killed", 0, "killed-again")),
    ],
)
def test_going_back_or_repeating_a_position_is_refused(
    history: tuple[LifecycleEvent, ...], late: LifecycleEvent
) -> None:
    assert admit(_state_after(*history), late, NOW_MS) == Refused(REASON_INVALID_TRANSITION)


def test_a_gap_is_allowed() -> None:
    # A refused or lost `paused` must not wedge the sandbox: the next
    # `resumed` (a higher generation) still moves it forward.
    state = _state_after(_event("created"), _event("resumed", 3))
    assert state is not None and state.generation == 3


def test_the_same_event_again_is_a_duplicate_not_a_refusal() -> None:
    created = _event("created")
    state = _state_after(created)
    assert state is not None
    assert admit(state, created, NOW_MS) == Admitted(state, duplicate=True)


def test_a_suspend_resume_loop_is_rate_limited_per_sandbox() -> None:
    state = _state_after(_event("created"))
    admitted = 0
    refused: list[str] = []
    for generation in range(RATE_BURST_EVENTS):
        for kind, gen in (("paused", generation), ("resumed", generation + 1)):
            admission = admit(state, _event(kind, gen), NOW_MS)
            if isinstance(admission, Admitted):
                state = admission.state
                admitted += 1
            else:
                refused.append(admission.reason)
    assert admitted == RATE_BURST_EVENTS
    assert set(refused) == {REASON_RATE_LIMITED}


def test_the_bucket_refills_on_the_callers_clock() -> None:
    state = _state_after(_event("created"))
    generation = 0
    for _ in range(RATE_BURST_EVENTS // 2):
        state = _state_after_from(state, _event("paused", generation))
        generation += 1
        state = _state_after_from(state, _event("resumed", generation))
    assert admit(state, _event("paused", generation), NOW_MS) == Refused(REASON_RATE_LIMITED)
    later = NOW_MS + RATE_EMISSION_INTERVAL_MS
    assert isinstance(admit(state, _event("paused", generation), later), Admitted)


def test_created_and_killed_never_spend_from_the_bucket() -> None:
    state = _state_after(_event("created"))
    assert state is not None
    exhausted = replace(
        state, rate_tat_ms=NOW_MS + 10 * RATE_BURST_EVENTS * RATE_EMISSION_INTERVAL_MS
    )
    assert isinstance(admit(exhausted, _event("killed"), NOW_MS), Admitted)
    assert admit(exhausted, _event("paused"), NOW_MS) == Refused(REASON_RATE_LIMITED)


def test_a_row_from_before_admission_reads_as_revision_zero_with_a_full_bucket() -> None:
    legacy = SandboxState(
        sandbox_id="sbx-1",
        last_kind="created",
        generation=0,
        last_seen_ms=1,
        image_arn="arn:image",
        image_version="1",
        last_event_id="",
        rate_tat_ms=0,
        revision=0,
    )
    admission = admit(legacy, _event("paused"), NOW_MS)
    assert isinstance(admission, Admitted)
    assert admission.state.revision == 1


def _state_after_from(state: SandboxState | None, event: LifecycleEvent) -> SandboxState:
    admission = admit(state, event, NOW_MS)
    assert isinstance(admission, Admitted), (event, admission)
    return admission.state
