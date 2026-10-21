"""Entrada estructurada en los SDK (ADR-033 D3): el cuerpo de
`POST /invocations` con `prompt` o `input` (`testdata/agent-deploy/
invocation-bodies.json`, compartido con TypeScript), la lectura de `output`,
los mensajes por `detail_code`, el aviso de un host anterior y `invoke()` /
`stream()` con `input` en despliegues, flotas y AgentCore. Ninguna prueba
llama a AWS."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rayito import AgentCoreFleet
from rayito._agent._deployment_base import (
    HOST_TOO_OLD_MESSAGE,
    EventDecoder,
    ResultBuilder,
    http_error,
    invocation_body,
    result_from_body,
)
from rayito._agent._events import (
    AGENT_DETAIL_MESSAGES,
    AgentResult,
    Done,
    TokenUsage,
    failure_message,
)
from rayito._agent._sse import SseMessage
from rayito._agent._stream_base import (
    INPUT_NOT_OBJECT_MESSAGE,
    INPUT_TOO_LARGE_MESSAGE,
    PROMPT_OR_INPUT_MESSAGE,
)
from rayito._agentcore._fleet_base import event_with_session_id
from rayito._limits import AGENT_SERVICE_MAX_OUTPUT_BYTES, AGENT_SERVICE_MAX_REQUEST_BYTES
from rayito.exceptions import AgentException, InvalidArgumentException
from tests.unit import fake_agentcore as fake
from tests.unit.fake_agent_fleet import FleetFlavor
from tests.unit.fake_agent_project import Scripted, json_body, sse
from tests.unit.test_agent_deployment import Harness
from tests.unit.test_agentcore_invoke import status

ROOT = Path(__file__).resolve().parents[4] / "testdata"
VECTORS = json.loads((ROOT / "agent-deploy" / "invocation-bodies.json").read_text("utf-8"))
CONTRACT = json.loads((ROOT / "agnostic-deploy" / "contract.json").read_text("utf-8"))
TICKET = {"ticket_id": "T-1"}
OUTPUT = {"priority": "high", "summary": "printer offline"}
SESSION = "rda_0123456789abcdef0123456789abcdef"


def _input_of(args: dict[str, Any]) -> Any:
    generator = args.get("inputGenerator")
    if generator is None:
        return args.get("input")
    return {generator["key"]: generator["char"] * generator["length"]}


def _message(error: dict[str, Any]) -> str:
    return str(VECTORS["messages"][error["messageKey"]].format(**error.get("fields", {})))


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda case: case["name"])
def test_invocation_bodies_match_the_shared_vectors(case: dict[str, Any]) -> None:
    args = case["args"]

    def build() -> bytes:
        return invocation_body(
            args.get("prompt"),
            session_id=args.get("sessionId"),
            model=args.get("model"),
            reasoning=args.get("reasoning", False),
            input=_input_of(args),
        )

    if "error" in case:
        with pytest.raises(InvalidArgumentException) as raised:
            build()
        assert str(raised.value) == _message(case["error"])
    else:
        assert build().decode("utf-8") == case["body"]


def test_the_sdk_messages_are_the_vector_messages() -> None:
    assert VECTORS["messages"]["prompt_or_input"] == PROMPT_OR_INPUT_MESSAGE
    assert VECTORS["messages"]["input_not_object"] == INPUT_NOT_OBJECT_MESSAGE
    assert VECTORS["messages"]["input_too_large"] == INPUT_TOO_LARGE_MESSAGE
    assert VECTORS["messages"]["host_too_old"] == HOST_TOO_OLD_MESSAGE
    assert dict(AGENT_DETAIL_MESSAGES) == VECTORS["detailCodes"]
    assert VECTORS["maxRequestBytesLimit"] == "agentServiceMaxRequestBytes"


def test_the_output_limit_is_the_contract_value() -> None:
    (limit,) = [item for item in CONTRACT["limits"] if item["feature"] == "B"]
    assert limit["key"] == "agentServiceMaxOutputBytes"
    assert limit["value"] == AGENT_SERVICE_MAX_OUTPUT_BYTES


@pytest.mark.parametrize("case", VECTORS["resultCases"], ids=lambda case: case["name"])
def test_results_match_the_shared_vectors(case: dict[str, Any]) -> None:
    result = result_from_body(case["body"].encode("utf-8"))
    assert result.text == case["expected"]["text"]
    assert result.output == case["expected"]["output"]


def test_an_input_that_cannot_be_encoded_is_not_an_object() -> None:
    values: list[Any] = [{"when": object()}, {1: "x"}, {"nan": float("nan")}]
    for value in values:
        with pytest.raises(InvalidArgumentException) as raised:
            invocation_body(None, session_id=None, model=None, reasoning=False, input=value)
        assert str(raised.value) == INPUT_NOT_OBJECT_MESSAGE


def test_an_input_that_fits_exactly_is_accepted() -> None:
    padding = AGENT_SERVICE_MAX_REQUEST_BYTES - len(json.dumps({"k": ""}, separators=(",", ":")))
    body = invocation_body(
        None, session_id=None, model=None, reasoning=False, input={"k": "x" * padding}
    )
    assert body.startswith(b'{"input":{"k":"x')


@pytest.mark.parametrize("detail", sorted(VECTORS["detailCodes"]))
def test_a_structured_detail_code_has_its_own_message(detail: str) -> None:
    message = failure_message("protocol_error", detail)
    assert message == f"{VECTORS['detailCodes'][detail]} ({detail})"
    assert failure_message("runtime_error", detail).startswith("el runtime del agente")


def test_a_bad_request_after_input_is_a_host_that_is_too_old() -> None:
    assert str(http_error(400, sent_input=True)) == HOST_TOO_OLD_MESSAGE
    assert str(http_error(400)) != HOST_TOO_OLD_MESSAGE
    assert isinstance(http_error(400, sent_input=True), InvalidArgumentException)


def test_done_keeps_its_dataclass_shape_and_carries_output() -> None:
    import dataclasses

    plain = Done(session_id=SESSION, exit_code=0, usage=TokenUsage())
    rich = Done(session_id=SESSION, exit_code=0, usage=TokenUsage(), output=OUTPUT)
    assert plain.output is None
    assert rich.output == OUTPUT
    assert dataclasses.asdict(rich) == dataclasses.asdict(plain)
    assert "printer" not in repr(rich)
    assert event_with_session_id(rich, "otra").output == OUTPUT  # type: ignore[union-attr]


def test_the_sse_done_event_and_the_result_builder_carry_output() -> None:
    decoder = EventDecoder()
    payload = {"v": 1, "type": "done", "session_id": SESSION, "output": OUTPUT}
    (done,) = decoder.feed(SseMessage(event="done", data=json.dumps(payload)))
    assert isinstance(done, Done)
    assert done.output == OUTPUT
    builder = ResultBuilder()
    builder.add(done)
    assert builder.result().output == OUTPUT


@pytest.fixture(params=["sync", "async"])
def flavor_name(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def _structured_body() -> bytes:
    return json_body(
        {
            "v": 1,
            "text": "",
            "session_id": SESSION,
            "usage": {},
            "usage_reported": False,
            "steps": 0,
            "failed": None,
            "output": OUTPUT,
        }
    )


def test_deployment_invoke_sends_input_and_reads_output(flavor_name: str) -> None:
    harness = Harness(flavor_name, Scripted(body=_structured_body()))
    result = harness.call("invoke", input=TICKET, session_id=SESSION)
    assert isinstance(result, AgentResult)
    assert result.output == OUTPUT
    assert result.text == ""
    (sent,) = harness.transport.requests
    assert json.loads(sent.body or b"") == {"input": TICKET, "session_id": SESSION}


def test_deployment_stream_ends_with_the_output(flavor_name: str) -> None:
    body = sse(
        ("session", {"session_id": SESSION}),
        ("done", {"session_id": SESSION, **{"output": OUTPUT}}),
    )
    harness = Harness(flavor_name, Scripted(chunks=[body]))
    events, _ = harness.events(input=TICKET)
    assert isinstance(events[-1], Done)
    assert events[-1].output == OUTPUT
    assert b'"input":{"ticket_id":"T-1"}' in (harness.transport.requests[0].body or b"")


@pytest.mark.parametrize(
    "call",
    [
        {},
        {"prompt": "hola", "input": TICKET},
        {"input": ["T-1"]},
    ],
)
def test_deployment_validates_before_any_request(flavor_name: str, call: dict[str, Any]) -> None:
    harness = Harness(flavor_name)
    with pytest.raises(InvalidArgumentException):
        harness.call("invoke", **call)
    assert harness.transport.requests == []


def test_a_host_that_rejects_input_asks_for_a_new_deploy(flavor_name: str) -> None:
    harness = Harness(flavor_name, Scripted(status=400, body=b"{}"))
    with pytest.raises(InvalidArgumentException) as raised:
        harness.call("invoke", input=TICKET)
    assert str(raised.value) == HOST_TOO_OLD_MESSAGE


def test_a_structured_failure_is_an_agent_exception_with_its_detail(flavor_name: str) -> None:
    body = json_body(
        {
            "v": 1,
            "text": "",
            "session_id": SESSION,
            "usage": {},
            "steps": 0,
            "failed": {"reason": "protocol_error", "detail_code": "input_required"},
        }
    )
    harness = Harness(flavor_name, Scripted(body=body))
    with pytest.raises(AgentException) as raised:
        harness.call("invoke", "hola")
    assert raised.value.reason == "protocol_error"
    assert raised.value.detail_code == "input_required"
    assert VECTORS["detailCodes"]["input_required"] in str(raised.value)


@pytest.fixture(params=["sync", "async"])
def fleet_flavor(request: pytest.FixtureRequest) -> FleetFlavor:
    return FleetFlavor(request.param)


def test_a_fleet_checks_the_input_before_placing_a_session(fleet_flavor: FleetFlavor) -> None:
    fleet = fleet_flavor.fleet()
    with pytest.raises(InvalidArgumentException) as raised:
        fleet_flavor.call(fleet.invoke(prompt="hola", input=TICKET))
    assert str(raised.value) == PROMPT_OR_INPUT_MESSAGE
    with pytest.raises(InvalidArgumentException):
        fleet_flavor.call(fleet.stream(input=[1]))
    assert fleet_flavor.classes.created == []


def test_a_fleet_sends_the_input_to_its_vm(fleet_flavor: FleetFlavor) -> None:
    fleet = fleet_flavor.started()
    host = fleet_flavor.backend.host(fleet.sandbox_ids[0])
    host.invoke_status = 400
    with pytest.raises(InvalidArgumentException) as raised:
        fleet_flavor.call(fleet.invoke(input=TICKET))
    assert str(raised.value) == HOST_TOO_OLD_MESSAGE
    assert host.invocations[-1] == {"input": TICKET}


def test_agentcore_invoke_sends_input_without_a_session_in_the_body() -> None:
    invoker = fake.FakeInvoker(_structured_body())
    fleet = AgentCoreFleet(status(), gateways=fake.cloud().gateways, invoker=invoker)
    result = fleet.invoke(input=TICKET, model="m")
    ((_, _, _, payload, accept),) = invoker.calls
    assert json.loads(payload) == {"input": TICKET, "model": "m"}
    assert accept == "application/json"
    assert result.output == OUTPUT


def test_agentcore_validates_before_calling_aws() -> None:
    invoker = fake.FakeInvoker(_structured_body())
    fleet = AgentCoreFleet(status(), gateways=fake.cloud().gateways, invoker=invoker)
    with pytest.raises(InvalidArgumentException):
        fleet.invoke()
    assert invoker.calls == []
