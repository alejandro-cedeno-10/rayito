"""Cuándo el uso de tokens de una ejecución es conocido: un `step_finished`
con `usage` lo informa; sin él (una app HTTP, un comando o una función
propios) el uso es cero porque se desconoce, y `usage_reported` lo dice."""

from __future__ import annotations

import json
from typing import Any

import pytest

from rayito._agent._events import AgentResult, Done, TokenUsage
from rayito._agent._protocol_v1 import ProtocolV1State, finish_v1, parse_v1_line

SESSION = "rda_" + "0" * 32


def _feed(state: ProtocolV1State, *events: dict[str, Any]) -> None:
    for event in events:
        parse_v1_line(json.dumps({"v": 1, **event}).encode(), state)


def _run(*steps: dict[str, Any]) -> Done:
    state = ProtocolV1State()
    _feed(state, {"type": "session", "session_id": SESSION}, *steps, {"type": "done"})
    final = finish_v1(state, 0)
    assert isinstance(final, Done)
    return final


def test_a_step_with_usage_is_reported() -> None:
    final = _run(
        {"type": "step_started", "index": 1},
        {"type": "step_finished", "index": 1, "usage": {"input": 4, "output": 1}},
    )
    assert final.usage_reported is True
    assert final.usage == TokenUsage(input=4, output=1)


@pytest.mark.parametrize("usage", [None, "x", 3])
def test_a_step_without_usage_is_unreported(usage: object) -> None:
    final = _run(
        {"type": "step_started", "index": 1},
        {"type": "step_finished", "index": 1, "usage": usage},
    )
    assert final.usage_reported is False
    assert final.usage == TokenUsage()


def test_one_reporting_step_is_enough() -> None:
    final = _run(
        {"type": "step_finished", "index": 1, "usage": None},
        {"type": "step_finished", "index": 2, "usage": {"input": 2}},
    )
    assert final.usage_reported is True
    assert final.usage.input == 2


def test_a_zero_usage_that_was_reported_stays_reported() -> None:
    final = _run({"type": "step_finished", "index": 1, "usage": {"input": 0, "output": 0}})
    assert final.usage_reported is True


def test_the_defaults_keep_the_0_13_shape() -> None:
    assert Done("s", 0, TokenUsage()).usage_reported is True
    assert AgentResult("s", "t", 1, TokenUsage(), 0).usage_reported is True
