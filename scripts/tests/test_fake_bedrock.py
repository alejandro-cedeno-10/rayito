"""El Bedrock falso de los tests `local` (`dev/local/providers/fake_bedrock.py`):
el guion del modelo y el flujo de eventos, comprobados con el decodificador de
botocore, que es el que lee el flujo en los clientes de Python."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from botocore.eventstream import EventStreamBuffer

MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "dev" / "local" / "providers" / "fake_bedrock.py"
)
SPEC = importlib.util.spec_from_file_location("fake_bedrock", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
fake = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fake
SPEC.loader.exec_module(fake)


def request(
    text: str, *, tools: tuple[str, ...] = (), last: dict[str, Any] | None = None
) -> dict[str, Any]:
    messages: list[dict[str, Any]] = [{"role": "user", "content": [{"text": text}]}]
    if last is not None:
        messages.append(last)
    body: dict[str, Any] = {"messages": messages}
    if tools:
        body["toolConfig"] = {"tools": [{"toolSpec": {"name": name}} for name in tools]}
    return body


def decode(stream: bytes) -> list[tuple[str, dict[str, Any]]]:
    buffer = EventStreamBuffer()
    buffer.add_data(stream)
    return [(event.headers[":event-type"], json.loads(event.payload)) for event in buffer]


def test_a_sum_with_an_add_tool_asks_for_the_tool() -> None:
    turn = fake.turn_for(request("What is 17 plus 25?", tools=("add", "lookup")))
    assert turn.tool_name == "add"
    assert turn.tool_input == {"a": 17, "b": 25}
    assert turn.stop_reason == "tool_use"


def test_a_sum_without_the_tool_gets_the_plain_reply() -> None:
    assert fake.turn_for(request("What is 17 plus 25?")).text == fake.REPLY_TEXT


def test_a_tool_result_is_echoed_in_the_answer() -> None:
    result = {
        "role": "user",
        "content": [{"toolResult": {"content": [{"text": "42"}]}}],
    }
    turn = fake.turn_for(request("What is 17 plus 25?", tools=("add",), last=result))
    assert turn.text == "The answer is 42."
    assert turn.tool_name is None


def test_slow_marker_sets_the_delay() -> None:
    assert fake.turn_for(request("hello slow:2.5")).delay_seconds == 2.5
    assert fake.turn_for(request("hello")).delay_seconds == 0.0


def test_converse_body_carries_text_usage_and_stop_reason() -> None:
    body = fake.converse_response(fake.Turn(text="hi"))
    assert body["output"]["message"]["content"] == [{"text": "hi"}]
    assert body["stopReason"] == "end_turn"
    assert body["usage"]["totalTokens"] == fake.INPUT_TOKENS + fake.OUTPUT_TOKENS


def test_converse_body_for_a_tool_call() -> None:
    turn = fake.Turn(tool_name="add", tool_input={"a": 1, "b": 2})
    block = fake.converse_response(turn)["output"]["message"]["content"][0]["toolUse"]
    assert block["name"] == "add"
    assert block["input"] == {"a": 1, "b": 2}


def test_event_stream_round_trips_through_the_botocore_decoder() -> None:
    kinds = [kind for kind, _ in decode(fake.encode_stream(fake.Turn(text="hi")))]
    assert kinds == [
        "messageStart",
        "contentBlockDelta",
        "contentBlockStop",
        "messageStop",
        "metadata",
    ]


def test_event_stream_for_a_tool_call_streams_its_input_as_json() -> None:
    turn = fake.Turn(tool_name="add", tool_input={"a": 3, "b": 4})
    events = dict(decode(fake.encode_stream(turn)))
    assert events["contentBlockStart"]["start"]["toolUse"]["name"] == "add"
    assert json.loads(events["contentBlockDelta"]["delta"]["toolUse"]["input"]) == {
        "a": 3,
        "b": 4,
    }
    assert events["messageStop"]["stopReason"] == "tool_use"


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("/model/us.anthropic.claude-haiku-4-5-20251001-v1%3A0/converse", "converse"),
        ("/model/x/converse-stream", "converse-stream"),
        ("/v1/chat/completions", None),
        ("/model/x/invoke", None),
    ],
)
def test_path_kind(path: str, kind: str | None) -> None:
    assert fake.model_path_kind(path) == kind
