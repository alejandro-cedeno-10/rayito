"""Entrada estructurada en el host del agente (ADR-033 D3): el cuerpo de
`POST /invocations` (`hostCases` de `testdata/agent-deploy/
invocation-bodies.json`), `output` en `done` y en la respuesta JSON, el driver
de deepagents/langgraph con un grafo de mensajes (los mismos argumentos de
`stream` de siempre) y con un grafo de esquema propio, y el paso de `input` y
`output` por el puente de Node. LangChain y LangGraph son dobles en
`sys.modules`."""

from __future__ import annotations

import dataclasses
import datetime
import io
import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from rayito._agent._runner.agent_host import limits as host_limits
from rayito._agent._runner.agent_host import serve, task
from rayito._agent._runner.agent_host.drivers import command, node_callable
from rayito._agent._runner.agent_host.drivers import deepagents as driver
from rayito._agent._runner.agent_host.protocol import (
    AgentFailure,
    Emitter,
    Request,
    RunState,
    build_event,
    parse_request,
    require_input_support,
    run_to_terminal,
)
from rayito._limits import AGENT_SERVICE_MAX_OUTPUT_BYTES
from tests.unit.agent_host_support import FakeDriver, request, running_service
from tests.unit.test_agent_deepagents_runner import (
    MODEL_META,
    FakeGraph,
    FakeMessage,
    _config,
    stubs,
)

ROOT = Path(__file__).resolve().parents[4] / "testdata"
VECTORS = json.loads((ROOT / "agent-deploy" / "invocation-bodies.json").read_text("utf-8"))
INPUT_VALIDATION = json.loads((ROOT / "agent-deploy" / "input-validation.json").read_text("utf-8"))
TICKET = {"ticket_id": "T-1"}
INPUT_SCHEMA = {
    "type": "object",
    "properties": {"ticket_id": {"type": "string"}, "tags": {"type": "array"}},
    "required": ["ticket_id"],
    "additionalProperties": False,
}
OUTPUT_SCHEMA = {"type": "object", "properties": {"priority": {}, "summary": {}}}
FINAL_STATE = {"ticket_id": "T-1", "priority": "high", "summary": "printer offline"}

__all__ = ["stubs"]


def test_a_bad_request_says_the_host_accepts_input() -> None:
    with running_service(FakeDriver()) as (_, port):
        status, _, data = request(
            port, "POST", "/invocations", raw_body=b'{"input":{"a":1},"extra":1}'
        )
    assert status == 400
    assert data.decode("utf-8") == VECTORS["hostBadRequestBody"]


@pytest.mark.parametrize("case", VECTORS["hostCases"], ids=lambda case: case["name"])
def test_the_host_accepts_exactly_the_shared_bodies(case: dict[str, Any]) -> None:
    body = case["body"].encode("utf-8")
    if case["expected"] == "accepted":
        request, _ = serve.parse_invocation(body, None)
        data = json.loads(body)
        assert request.prompt == data.get("prompt", "")
        assert request.input == data.get("input")
    else:
        with pytest.raises(AgentFailure) as raised:
            serve.parse_invocation(body, None)
        assert raised.value.detail_code == case["expected"]


def test_the_request_hides_its_input_and_the_state_its_output() -> None:
    request = parse_request({"v": 1, "input": {"secreto": "valor-privado"}})
    state = RunState(session_id="s", output={"secreto": "valor-privado"})
    assert "valor-privado" not in repr(request)
    assert "valor-privado" not in repr(state)


class Collector:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, kind: str, **fields: object) -> None:
        self.events.append(build_event(kind, **fields))


def test_done_carries_the_output_only_when_there_is_one() -> None:
    plain, rich = Collector(), Collector()
    run_to_terminal(plain, lambda state: None, lambda exc: ("runtime_error", "x"))

    def produce(state: RunState) -> None:
        state.output = {"priority": "high"}

    run_to_terminal(rich, produce, lambda exc: ("runtime_error", "x"))
    assert "output" not in plain.events[-1]
    assert rich.events[-1]["output"] == {"priority": "high"}


class _Invocation:
    def __init__(self) -> None:
        self.session_id = "s"
        self.tracker = host_limits.LimitTracker(host_limits.Limits())


def test_the_json_body_has_output_only_after_a_successful_structured_run() -> None:
    invocation: Any = _Invocation()
    done = build_event("done", session_id="s", output={"priority": "high"})
    failed = build_event("agent_failed", reason="protocol_error", detail_code="invalid_input")
    assert serve.result_body(invocation, done)["output"] == {"priority": "high"}
    assert "output" not in serve.result_body(invocation, build_event("done", session_id="s"))
    assert "output" not in serve.result_body(invocation, failed)


def test_a_driver_without_input_support_rejects_input() -> None:
    request = Request(prompt="", input=TICKET)
    with pytest.raises(AgentFailure) as raised:
        require_input_support(object(), request)
    assert (raised.value.reason, raised.value.detail_code) == ("protocol_error", "invalid_input")
    require_input_support(object(), Request(prompt="hola"))
    require_input_support(driver.Driver({}), request)


class _PromptOnlyDriver:
    def __init__(self) -> None:
        self.runs = 0

    def warm(self) -> None:
        return None

    def run(self, request: Request, emit: Any, state: RunState) -> None:
        self.runs += 1

    def cancel(self) -> None:
        return None


def test_task_mode_refuses_an_input_for_a_driver_without_input() -> None:
    stream = io.StringIO()
    driver_instance = _PromptOnlyDriver()
    task.run_task(lambda: driver_instance, {"v": 1, "input": TICKET}, Emitter(stream))
    assert driver_instance.runs == 0
    (event,) = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert (event["type"], event["reason"], event["detail_code"]) == (
        "agent_failed",
        "protocol_error",
        "invalid_input",
    )


def test_the_output_limit_is_the_shared_limit() -> None:
    assert host_limits.MAX_SERVICE_OUTPUT_BYTES == AGENT_SERVICE_MAX_OUTPUT_BYTES


class SchemaGraph(FakeGraph):
    """Un grafo con `get_input_jsonschema` / `get_output_jsonschema` que, en
    modo `values`, emite el estado final."""

    def __init__(
        self,
        chunks: list[tuple[Any, ...]],
        *,
        input_schema: Any = INPUT_SCHEMA,
        output_schema: Any = OUTPUT_SCHEMA,
        final: Any = None,
        error: Exception | None = None,
    ) -> None:
        super().__init__(chunks, error)
        self.input_schema = input_schema
        self.output_schema = output_schema
        self.final = FINAL_STATE if final is None else final

    def get_input_jsonschema(self) -> Any:
        if isinstance(self.input_schema, Exception):
            raise self.input_schema
        return self.input_schema

    def get_output_jsonschema(self) -> Any:
        if isinstance(self.output_schema, Exception):
            raise self.output_schema
        return self.output_schema

    def stream(self, inputs: Any, **kwargs: Any) -> Iterator[tuple[Any, ...]]:
        yield from super().stream(inputs, **kwargs)
        if "values" in kwargs.get("stream_mode", []):
            yield (), "values", self.final


@pytest.fixture
def messages_module(stubs: dict[str, Any]) -> Any:
    import sys

    module = sys.modules["langchain_core.messages"]
    module.BaseMessage = FakeMessage  # type: ignore[attr-defined]
    module.convert_to_messages = lambda items: [  # type: ignore[attr-defined]
        FakeMessage(content=item["content"], kind=item["role"]) for item in items
    ]
    return module


def _execute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    graph: Any,
    request: Request,
) -> tuple[list[dict[str, Any]], RunState]:
    monkeypatch.setattr(driver, "load_entrypoint", lambda *args: graph)
    config = _config(tmp_path, entrypoint="agente:graph")
    events: list[dict[str, Any]] = []
    state = RunState()
    driver.execute(config, request, events.append, state)
    return events, state


def test_a_message_graph_with_a_prompt_streams_exactly_as_before(
    messages_module: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plain = FakeGraph([("messages", (FakeMessage(content="hola"), MODEL_META))])
    with_schema = SchemaGraph(
        [("messages", (FakeMessage(content="hola"), MODEL_META))],
        input_schema={"properties": {"messages": {"type": "array"}}},
    )
    failing = SchemaGraph([], input_schema=RuntimeError("sin esquema"))
    for graph in (plain, with_schema, failing):
        _, state = _execute(tmp_path, monkeypatch, graph, Request(prompt="hola"))
        ((payload, kwargs),) = graph.inputs
        assert kwargs == {
            "config": {"recursion_limit": driver.RECURSION_LIMIT},
            "stream_mode": ["messages", "updates"],
            "subgraphs": True,
        }
        assert [message.content for message in payload["messages"]] == ["hola"]
        assert state.output is None


def test_a_prompt_to_a_graph_without_required_input_runs_as_before(
    messages_module: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    optional = {"type": "object", "properties": {"result": {"type": "string"}}}
    for schema in (optional, {**optional, "required": []}):
        graph = SchemaGraph(
            [("messages", (FakeMessage(content="hola"), MODEL_META))], input_schema=schema
        )
        _, state = _execute(tmp_path, monkeypatch, graph, Request(prompt="hola"))
        ((payload, kwargs),) = graph.inputs
        assert kwargs == {
            "config": {"recursion_limit": driver.RECURSION_LIMIT},
            "stream_mode": ["messages", "updates"],
            "subgraphs": True,
        }
        assert [message.content for message in payload["messages"]] == ["hola"]
        assert state.output is None


def test_an_input_for_a_message_graph_is_passed_as_is_after_the_history(
    messages_module: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = FakeGraph([])
    _, state = _execute(tmp_path, monkeypatch, first, Request(prompt="hola"))
    graph = FakeGraph([])
    data = {"messages": [{"role": "user", "content": "sigue"}], "locale": "es"}
    request = Request(prompt="", session_id=state.session_id, input=data)
    _execute(tmp_path, monkeypatch, graph, request)
    ((payload, _),) = graph.inputs
    assert payload["locale"] == "es"
    assert [message.content for message in payload["messages"]] == ["hola", "sigue"]


def test_a_structured_graph_runs_with_its_input_and_returns_the_filtered_state(
    messages_module: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = SchemaGraph([])
    events, state = _execute(tmp_path, monkeypatch, graph, Request(prompt="", input=TICKET))
    ((payload, kwargs),) = graph.inputs
    assert payload == TICKET
    assert kwargs["config"] == {
        "recursion_limit": driver.RECURSION_LIMIT,
        "configurable": {"thread_id": state.session_id},
    }
    assert kwargs["stream_mode"] == ["messages", "updates", "values"]
    assert state.output == {"priority": "high", "summary": "printer offline"}
    assert events[0]["type"] == "session"


def test_a_structured_session_continues_with_the_same_thread(
    messages_module: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, first = _execute(tmp_path, monkeypatch, SchemaGraph([]), Request(prompt="", input=TICKET))
    graph = SchemaGraph([])
    request = Request(prompt="", session_id=first.session_id, input=TICKET)
    _, second = _execute(tmp_path, monkeypatch, graph, request)
    assert second.session_id == first.session_id
    ((_, kwargs),) = graph.inputs
    assert kwargs["config"]["configurable"] == {"thread_id": first.session_id}


@pytest.mark.parametrize(
    ("request_fields", "detail"),
    [
        ({"prompt": "hola"}, "input_required"),
        ({"prompt": "", "input": {}}, "invalid_input"),
        ({"prompt": "", "input": {"ticket_id": 7}}, "invalid_input"),
        ({"prompt": "", "input": {"ticket_id": "T-1", "extra": 1}}, "invalid_input"),
        ({"prompt": "", "input": {"ticket_id": "T-1", "tags": "no-lista"}}, "invalid_input"),
    ],
)
def test_a_structured_graph_rejects_what_its_schema_does_not_allow(
    messages_module: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request_fields: Mapping[str, Any],
    detail: str,
) -> None:
    graph = SchemaGraph([])
    with pytest.raises(AgentFailure) as raised:
        _execute(tmp_path, monkeypatch, graph, Request(**request_fields))
    assert (raised.value.reason, raised.value.detail_code) == ("protocol_error", detail)
    assert graph.inputs == []


@pytest.mark.parametrize("case", INPUT_VALIDATION["cases"], ids=lambda case: str(case["name"]))
def test_the_input_validation_vectors(case: dict[str, Any]) -> None:
    schema = INPUT_VALIDATION["schemas"][case["schema"]]
    if case["valid"]:
        driver.validate_input(schema, case["input"])
        return
    with pytest.raises(AgentFailure) as raised:
        driver.validate_input(schema, case["input"])
    assert raised.value.detail_code == "invalid_input"


def test_optional_fields_accept_any_of_their_branches() -> None:
    schema = {
        "properties": {"note": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
        "required": [],
    }
    driver.validate_input(schema, {"note": None})
    driver.validate_input(schema, {"note": "texto", "otra": 1})
    with pytest.raises(AgentFailure):
        driver.validate_input(schema, {"note": 3})


def test_a_pydantic_validation_error_is_invalid_input(
    messages_module: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    validation_error = type("ValidationError", (ValueError,), {"__module__": "pydantic_core._x"})
    graph = SchemaGraph([], error=validation_error("ticket_id=secreto"))
    with pytest.raises(AgentFailure) as raised:
        _execute(tmp_path, monkeypatch, graph, Request(prompt="", input=TICKET))
    assert raised.value.detail_code == "invalid_input"
    assert "secreto" not in str(raised.value)


def test_another_error_of_the_graph_is_not_reclassified(
    messages_module: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = SchemaGraph([], error=KeyError("priority"))
    with pytest.raises(KeyError):
        _execute(tmp_path, monkeypatch, graph, Request(prompt="", input=TICKET))


def test_messages_in_the_output_are_serialized_and_unknown_schemas_are_not_filtered(
    messages_module: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    final = {"messages": [FakeMessage(content="listo")], "priority": "high"}
    graph = SchemaGraph([], output_schema=RuntimeError("sin esquema"), final=final)
    _, state = _execute(tmp_path, monkeypatch, graph, Request(prompt="", input=TICKET))
    assert state.output == {"messages": [{"kind": "ai", "content": "listo"}], "priority": "high"}


def test_pydantic_models_and_dataclasses_in_the_output_are_serialized(
    messages_module: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pydantic = pytest.importorskip("pydantic")

    class Summary(pydantic.BaseModel):  # type: ignore[misc,name-defined]
        text: str
        when: datetime.date

    @dataclasses.dataclass
    class Priority:
        level: str
        summary: Any

    final = {
        "priority": Priority("high", Summary(text="listo", when=datetime.date(2026, 1, 2))),
        "summary": Summary(text="otro", when=datetime.date(2026, 1, 3)),
    }
    graph = SchemaGraph([], final=final)
    _, state = _execute(tmp_path, monkeypatch, graph, Request(prompt="", input=TICKET))
    assert state.output == {
        "priority": {"level": "high", "summary": {"text": "listo", "when": "2026-01-02"}},
        "summary": {"text": "otro", "when": "2026-01-03"},
    }


@pytest.mark.parametrize(
    "final",
    [
        {"priority": object()},
        {"priority": "x" * (AGENT_SERVICE_MAX_OUTPUT_BYTES + 1)},
    ],
    ids=["not-json", "too-large"],
)
def test_an_output_that_cannot_be_returned_is_output_not_serializable(
    messages_module: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, final: Any
) -> None:
    graph = SchemaGraph([], final=final)
    with pytest.raises(AgentFailure) as raised:
        _execute(tmp_path, monkeypatch, graph, Request(prompt="", input=TICKET))
    assert raised.value.detail_code == "output_not_serializable"


def test_the_service_answers_a_structured_run_with_output(
    messages_module: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(driver, "load_entrypoint", lambda *args: SchemaGraph([]))
    service = serve.Service(
        driver.Driver(_config(tmp_path, entrypoint="agente:graph")), serve.ServiceConfig()
    )
    service.mark_ready()
    request, _ = serve.parse_invocation(b'{"input":{"ticket_id":"T-1"}}', None)
    invocation = serve.Invocation(service, request)
    invocation.start()
    events = list(invocation.events())
    invocation.finish()
    body = serve.result_body(invocation, events[-1])
    assert body["output"] == {"priority": "high", "summary": "printer offline"}
    assert body["failed"] is None and body["text"] == ""


def test_the_node_bridge_driver_sends_input_and_relays_output() -> None:
    line = command._request_line(Request(prompt="", input=TICKET), "rda_" + "0" * 32)
    assert json.loads(line)["input"] == TICKET
    assert "input" not in json.loads(command._request_line(Request(prompt="hola"), "s"))
    relay, state = command.Relay(), RunState()
    relay.relay(b'{"v":1,"type":"done","output":{"priority":"high"}}', lambda e: None, state)
    assert relay.done and state.output == {"priority": "high"}


@pytest.mark.parametrize(("adapter", "accepts"), [("langgraph", True), ("strands", False)])
def test_only_the_langgraph_bridge_accepts_input(
    tmp_path: Path, adapter: str, accepts: bool
) -> None:
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"bridge": "bridge.mjs", "adapter": adapter}), encoding="utf-8")
    created = node_callable.create({"config_path": str(config)})
    assert created.accepts_input is accepts
    assert command.Driver({"command": ["x"], "protocol": "text"}).accepts_input is False
