"""`LimitTracker` contra los vectores compartidos
(`testdata/agent/limit-vectors.json`): los lee también el SDK de TypeScript
y los tests del host del agente, así que los tres imponen `max_steps` y
`max_total_tokens` igual."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from rayito import (
    AgentEvent,
    AgentFailed,
    AgentLimits,
    Done,
    StepFinished,
    StepStarted,
    Text,
    TokenUsage,
    ToolCall,
)
from rayito._agent._protocol_v1 import ProtocolV1State, finish_v1, parse_v1_line
from rayito._agent._stream_base import LimitTracker

TESTDATA = Path(__file__).parents[4] / "testdata" / "agent"
CASES = json.loads((TESTDATA / "limit-vectors.json").read_text(encoding="utf-8"))["cases"]


def _event(raw: dict[str, Any]) -> AgentEvent:
    kind = raw["type"]
    if kind == "step_started":
        return StepStarted(index=raw["index"])
    if kind == "step_finished":
        return StepFinished(
            index=0, usage=TokenUsage(**raw["usage"]), finish_reason=raw.get("finish_reason")
        )
    if kind == "text":
        return Text(text=raw["text"])
    if kind == "tool_call":
        return ToolCall(call_id=raw["call_id"], name=raw["name"], status=raw["status"])
    if kind == "done":
        return Done(session_id=raw["session_id"], exit_code=raw["exit_code"], usage=TokenUsage())
    return AgentFailed(reason=raw["reason"])


def _projection(event: AgentEvent) -> dict[str, Any]:
    shown: dict[str, Any] = {"type": event.type}
    if isinstance(event, StepStarted):
        shown["index"] = event.index
    if isinstance(event, AgentFailed):
        shown["reason"] = event.reason
        shown["session_id"] = event.session_id
    return shown


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_limit_tracker_matches_the_shared_vectors(case: dict[str, Any]) -> None:
    tracker = LimitTracker(AgentLimits(**case["limits"]), session_id=case["session_id"])
    produced = [_projection(tracker.track(_event(raw))) for raw in case["events"]]

    assert len(produced) == len(case["expected"])
    for shown, expected in zip(produced, case["expected"], strict=True):
        assert {key: shown[key] for key in expected} == expected
    final = case["final"]
    assert tracker.steps == final["steps"]
    assert tracker.usage.total == final["total_tokens"]
    assert tracker.last_text == final["last_text"]
    assert len(tracker.tool_calls) == final["tool_calls"]
    assert tracker.session_id == final["session_id"]


def test_the_v1_decoder_alone_produces_the_golden_events() -> None:
    """El decodificador extraído de deepagents, sin adaptador, da los mismos
    eventos que los vectores de v1: lo que Strands, Hermes y `CustomRuntime`
    reutilizan."""
    expected = json.loads((TESTDATA / "rayito-protocol-v1-expected.json").read_text("utf-8"))
    state = ProtocolV1State()
    events: list[dict[str, Any]] = []
    for line in (TESTDATA / "rayito-protocol-v1.jsonl").read_bytes().splitlines():
        events.extend(dataclasses.asdict(event) for event in parse_v1_line(line, state))
    assert events == expected["events"]
    assert state.ignored_lines == expected["ignored_lines"]
    assert dataclasses.asdict(finish_v1(state, 0)) == expected["done"]
