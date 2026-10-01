"""The reconciler's deterministic synthetic ids: same (sandbox, reason,
window) collides on purpose, a different one never does."""

from __future__ import annotations

import pytest

from domain.dedupe import reconciler_window_start_ms, synthetic_event_id


def test_the_same_inputs_always_produce_the_same_id() -> None:
    first = synthetic_event_id(sandbox_id="sbx-1", reason="unknown", window_start_ms=1_000)
    second = synthetic_event_id(sandbox_id="sbx-1", reason="unknown", window_start_ms=1_000)
    assert first == second


def test_a_different_sandbox_or_window_changes_the_id() -> None:
    base = synthetic_event_id(sandbox_id="sbx-1", reason="unknown", window_start_ms=1_000)
    assert synthetic_event_id(sandbox_id="sbx-2", reason="unknown", window_start_ms=1_000) != base
    assert synthetic_event_id(sandbox_id="sbx-1", reason="unknown", window_start_ms=2_000) != base


def test_an_unknown_reason_is_rejected() -> None:
    with pytest.raises(ValueError, match="reason"):
        synthetic_event_id(sandbox_id="sbx-1", reason="request", window_start_ms=1_000)


def test_window_start_floors_to_the_bucket() -> None:
    assert reconciler_window_start_ms(1_234_567, 300_000) == 1_200_000
    assert reconciler_window_start_ms(1_200_000, 300_000) == 1_200_000


def test_window_start_rejects_a_non_positive_window() -> None:
    with pytest.raises(ValueError, match="window_ms"):
        reconciler_window_start_ms(1_000, 0)
