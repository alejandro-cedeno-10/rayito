"""Eventos del agente de IA (`ai-agent-core`): discriminadores, tabla
cerrada de fallos, `TokenUsage` y el recorte de la salida de herramientas,
contra los vectores compartidos con TypeScript."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rayito import (
    AgentException,
    AgentFailed,
    Done,
    InvalidArgumentException,
    Reasoning,
    SandboxException,
    StepFinished,
    StepStarted,
    Text,
    TextDelta,
    TokenUsage,
    ToolCall,
)
from rayito._agent import (
    AGENT_EVENT_TYPES,
    AGENT_FAILURE_MESSAGES,
    AGENT_FAILURE_REASONS,
    failure_message,
    truncate_tool_output,
)
from rayito._limits import MAX_TOOL_OUTPUT_PREVIEW_BYTES

VECTORS: dict[str, Any] = json.loads(
    (Path(__file__).parents[4] / "testdata" / "agent" / "domain-vectors.json").read_text(
        encoding="utf-8"
    )
)


def test_event_discriminators_follow_the_protocol_order() -> None:
    usage = TokenUsage()
    events = (
        TextDelta("a"),
        Text("a"),
        Reasoning("r"),
        ToolCall(call_id="c1", name="bash", status="completed"),
        StepStarted(1),
        StepFinished(1, usage),
        AgentFailed("aborted"),
        Done("s1", 0, usage),
    )
    assert tuple(event.type for event in events) == AGENT_EVENT_TYPES
    assert list(AGENT_EVENT_TYPES) == VECTORS["event_types"]


def test_type_is_not_a_constructor_argument() -> None:
    with pytest.raises(TypeError):
        Text("a", type="done")  # type: ignore[call-arg]


def test_failure_table_is_closed_and_shared() -> None:
    assert dict(AGENT_FAILURE_MESSAGES) == VECTORS["failures"]["messages"]
    assert set(AGENT_FAILURE_REASONS) == set(AGENT_FAILURE_MESSAGES)


@pytest.mark.parametrize("case", VECTORS["failures"]["detail_codes"])
def test_detail_code_never_carries_free_text(case: dict[str, Any]) -> None:
    assert failure_message(case["reason"], case["detail_code"]) == case["message"]
    event = AgentFailed(case["reason"], detail_code=case["detail_code"])
    assert event.message == case["message"]


def test_unknown_reason_is_rejected() -> None:
    with pytest.raises(InvalidArgumentException):
        AgentFailed("exploded")  # type: ignore[arg-type]
    with pytest.raises(InvalidArgumentException):
        failure_message("exploded")


def test_agent_failed_becomes_an_agent_exception() -> None:
    usage = TokenUsage(input=10, output=2)
    error = AgentFailed(
        "token_budget", exit_code=137, detail_code="x", session_id="ses_1"
    ).to_exception(usage)
    assert isinstance(error, AgentException)
    assert isinstance(error, SandboxException)
    assert (error.reason, error.session_id, error.exit_code, error.detail_code) == (
        "token_budget",
        "ses_1",
        137,
        "x",
    )
    assert error.usage == usage
    assert str(error) == "el agente superó su presupuesto de tokens (x)"


@pytest.mark.parametrize("case", VECTORS["usage"])
def test_token_usage_total(case: dict[str, Any]) -> None:
    assert TokenUsage(**case["tokens"]).total == case["total"]


def test_token_usage_adds_and_rejects_negatives() -> None:
    total = TokenUsage(input=1, cache_read=2) + TokenUsage(output=3, cache_write=4, reasoning=5)
    assert total == TokenUsage(input=1, output=3, reasoning=5, cache_read=2, cache_write=4)
    with pytest.raises(InvalidArgumentException):
        TokenUsage(input=-1)


def test_tool_output_preview_is_cut_on_a_character_boundary() -> None:
    assert VECTORS["tool_output"]["preview_bytes"] == MAX_TOOL_OUTPUT_PREVIEW_BYTES
    assert truncate_tool_output("ok") == ("ok", False)
    text = "a" * (MAX_TOOL_OUTPUT_PREVIEW_BYTES - 1) + "ñ" + "tail"
    preview, truncated = truncate_tool_output(text)
    assert truncated
    assert preview == "a" * (MAX_TOOL_OUTPUT_PREVIEW_BYTES - 1)
    assert len(preview.encode("utf-8")) <= MAX_TOOL_OUTPUT_PREVIEW_BYTES


def test_tool_call_status_is_closed() -> None:
    with pytest.raises(InvalidArgumentException):
        ToolCall(call_id="c", name="bash", status="running")  # type: ignore[arg-type]
