"""Los topes del host del agente (`_runner/agent_host/limits.py`) contra los
vectores compartidos (`testdata/agent/limit-vectors.json`) que también leen
el `LimitTracker` de los dos SDK, más los valores por defecto y el plazo."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rayito._agent._domain import AgentLimits
from rayito._agent._runner.agent_host import limits as host_limits
from rayito._agent._runner.agent_host.limits import Limits, LimitTracker

TESTDATA = Path(__file__).parents[4] / "testdata" / "agent"
CASES = json.loads((TESTDATA / "limit-vectors.json").read_text(encoding="utf-8"))["cases"]


def _projection(event: dict[str, Any], reason: str | None) -> dict[str, Any]:
    """Lo que el host entrega en lugar del evento: el propio evento, o un
    `agent_failed` con el motivo del tope rebasado."""
    if reason is not None:
        return {"type": "agent_failed", "reason": reason}
    shown = {"type": event["type"]}
    if "index" in event and event["type"] == "step_started":
        shown["index"] = event["index"]
    if event["type"] == "agent_failed":
        shown["reason"] = event["reason"]
    return shown


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_host_tracker_matches_the_shared_vectors(case: dict[str, Any]) -> None:
    tracker = LimitTracker(Limits.from_config(case["limits"]))
    produced = [_projection(raw, tracker.observe({"v": 1, **raw})) for raw in case["events"]]
    assert len(produced) == len(case["expected"])
    for shown, expected in zip(produced, case["expected"], strict=True):
        assert {key: shown[key] for key in expected if key != "session_id"} == {
            key: value for key, value in expected.items() if key != "session_id"
        }
    final = case["final"]
    assert tracker.steps == final["steps"]
    assert tracker.total_tokens == final["total_tokens"]
    assert tracker.last_text == final["last_text"]
    assert tracker.tool_calls == final["tool_calls"]


def test_the_defaults_are_the_sdk_defaults() -> None:
    sdk = AgentLimits()
    host = Limits()
    assert host.max_steps == sdk.max_steps
    assert host.timeout_seconds == sdk.timeout_seconds
    assert host.max_output_bytes == sdk.max_output_bytes
    assert host.max_total_tokens == sdk.max_total_tokens
    assert sdk.max_steps == host_limits.DEFAULT_MAX_STEPS


def test_from_config_reads_a_partial_document() -> None:
    limits = Limits.from_config({"max_steps": 3, "timeout_seconds": 1.5, "max_total_tokens": None})
    assert (limits.max_steps, limits.timeout_seconds, limits.max_total_tokens) == (3, 1.5, None)
    assert limits.max_output_bytes == Limits().max_output_bytes


@pytest.mark.parametrize(
    "raw", [None, [], "x", {"max_steps": "mucho"}, {"max_steps": -1}, {"max_steps": True}]
)
def test_from_config_ignores_what_is_not_a_positive_number(raw: object) -> None:
    assert Limits.from_config(raw).max_steps == Limits().max_steps


def test_the_text_falls_back_to_the_deltas() -> None:
    tracker = LimitTracker(Limits())
    for text in ("ho", "la"):
        tracker.observe({"type": "text_delta", "text": text})
    assert tracker.text == "hola"
    tracker.observe({"type": "text", "text": "completo"})
    assert tracker.text == "completo"
