"""`AgentCoreFleet.invoke()` y `stream()` (y los de `AsyncAgentCoreFleet`):
la misma API y semántica de sesión que `AgentFleet`, con el id de sesión de
AgentCore sólo en la cabecera, la respuesta decodificada con los lectores de los
despliegues en MicroVMs y los errores traducidos."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import pytest

from rayito import AgentCoreFleet, AsyncAgentCoreFleet
from rayito._agent._events import Done
from rayito._agent._runner.agent_host import serve
from rayito._agent._runner.agent_host.protocol import build_event
from rayito._agentcore._models import AgentCoreStatus
from rayito._agentcore._ports import RuntimeNotFound
from rayito.exceptions import AgentCoreException, AgentException
from tests.unit import fake_agentcore as fake

HOST_SESSION = "rda_" + "0" * 32
NEW_SESSION_PATTERN = re.compile(r"rayito-[0-9a-f]{32}")
USAGE = {"input": 10, "output": 5, "reasoning": 0, "cache_read": 0, "cache_write": 0}


def status() -> AgentCoreStatus:
    return AgentCoreStatus(
        name=fake.AGENT,
        region=fake.REGION,
        runtime_name=fake.RUNTIME_NAME,
        runtime_id=fake.RUNTIME_ID,
        runtime_arn=f"arn:aws:bedrock-agentcore:{fake.REGION}:{fake.ACCOUNT}:runtime/{fake.RUNTIME_ID}",
        runtime_version="3",
        status="READY",
        image_uri="123456789012.dkr.ecr.us-east-1.amazonaws.com/rayito/agentcore/support-bot:src-1",
        log_group=f"/aws/bedrock-agentcore/runtimes/{fake.RUNTIME_ID}-DEFAULT",
    )


def json_body(text: str = "42", *, failed: dict[str, Any] | None = None) -> bytes:
    document = {
        "v": 1,
        "text": text,
        "session_id": HOST_SESSION,
        "usage": USAGE,
        "usage_reported": True,
        "steps": 1,
        "failed": failed,
    }
    return json.dumps(document).encode()


def sse_body() -> bytes:
    events = [
        build_event("session", session_id=HOST_SESSION),
        build_event("step_started", index=1),
        build_event("text", text="42"),
        build_event("step_finished", index=1, usage=USAGE, finish_reason="stop"),
        build_event("done", session_id=HOST_SESSION),
    ]
    return b"".join(serve.format_sse(event) for event in events)


def fleet_with(invoker: fake.FakeInvoker) -> AgentCoreFleet:
    return AgentCoreFleet(status(), gateways=fake.cloud().gateways, invoker=invoker)


def test_invoke_sends_the_header_session_and_a_body_without_it() -> None:
    invoker = fake.FakeInvoker(json_body())
    result = fleet_with(invoker).invoke("¿Cuánto es 17 + 25?", model="m", reasoning=True)
    ((kind, arn, session, payload, accept),) = invoker.calls
    assert kind == "invoke" and arn == status().runtime_arn
    assert NEW_SESSION_PATTERN.fullmatch(session)
    assert accept == "application/json"
    assert json.loads(payload) == {"prompt": "¿Cuánto es 17 + 25?", "model": "m", "reasoning": True}
    assert (result.text, result.steps, result.usage.total) == ("42", 1, 15)
    assert invoker.closed == 1


def test_the_returned_session_id_is_the_one_used_so_passing_it_back_continues() -> None:
    invoker = fake.FakeInvoker(json_body())
    fleet = fleet_with(invoker)
    first = fleet.invoke("hola")
    second = fleet.invoke("otra vez", session_id=first.session_id)
    assert first.session_id == invoker.calls[0][2]
    assert invoker.calls[1][2] == first.session_id == second.session_id
    assert HOST_SESSION not in {first.session_id, second.session_id}


def test_a_caller_id_maps_to_the_same_session_every_time() -> None:
    invoker = fake.FakeInvoker(json_body())
    fleet = fleet_with(invoker)
    one = fleet.invoke("a", session_id="user-42").session_id
    two = fleet.invoke("b", session_id="user-42").session_id
    assert one == two == "rayito-6d894aa3ee802549d7f340e7c1cf0d1c"


def test_a_failed_run_is_an_agent_exception() -> None:
    body = json_body(failed={"reason": "model_error", "detail_code": "ThrottlingException"})
    with pytest.raises(AgentException) as raised:
        fleet_with(fake.FakeInvoker(body)).invoke("hola")
    assert raised.value.reason == "model_error"


def test_stream_accepts_event_stream_and_decodes_the_hosts_events() -> None:
    invoker = fake.FakeInvoker(sse_body(), chunk_size=17)
    with fleet_with(invoker).stream("hola") as stream:
        events = list(stream)
        used = stream.session_id
    kinds = [type(event).__name__ for event in events]
    assert kinds[0] == "StepStarted" and kinds[-1] == "Done"
    assert invoker.calls[0][0] == "stream" and invoker.calls[0][4] == "text/event-stream"
    done = events[-1]
    assert isinstance(done, Done) and done.session_id == used == invoker.calls[0][2]
    assert invoker.closed >= 1


def test_stream_result_carries_the_used_session() -> None:
    invoker = fake.FakeInvoker(sse_body())
    stream = fleet_with(invoker).stream("hola")
    result = stream.result()
    assert result.text == "42"
    assert result.session_id == invoker.calls[0][2]


def test_a_missing_runtime_is_runtime_not_found_with_the_agents_name() -> None:
    invoker = fake.FakeInvoker(b"", failure=RuntimeNotFound())
    with pytest.raises(AgentCoreException) as raised:
        fleet_with(invoker).invoke("hola")
    assert raised.value.code == "runtime_not_found"
    assert raised.value.fields == {"name": fake.AGENT, "region": fake.REGION}


def test_an_empty_prompt_is_rejected_before_any_call() -> None:
    invoker = fake.FakeInvoker(json_body())
    with pytest.raises(Exception, match="prompt"):
        fleet_with(invoker).invoke("")
    assert invoker.calls == []


def test_stop_session_maps_the_id_like_invoke() -> None:
    invoker = fake.FakeInvoker(json_body())
    assert fleet_with(invoker).stop_session("user-42") is True
    assert invoker.stopped == ["rayito-6d894aa3ee802549d7f340e7c1cf0d1c"]


def test_the_fleet_exposes_the_runtime_it_was_built_from() -> None:
    fleet = fleet_with(fake.FakeInvoker(b""))
    current = status()
    assert (fleet.name, fleet.region, fleet.runtime_name) == (
        current.name,
        current.region,
        current.runtime_name,
    )
    assert (fleet.runtime_id, fleet.runtime_arn, fleet.runtime_version) == (
        current.runtime_id,
        current.runtime_arn,
        current.runtime_version,
    )
    assert (fleet.image_uri, fleet.log_group) == (current.image_uri, current.log_group)


def test_async_invoke_and_stream_behave_like_the_sync_ones() -> None:
    async def scenario() -> None:
        invoker = fake.FakeInvoker(json_body())
        fleet = AsyncAgentCoreFleet(status(), gateways=fake.cloud().gateways, invoker=invoker)
        result = await fleet.invoke("hola", session_id="user-42")
        assert result.text == "42"
        assert result.session_id == "rayito-6d894aa3ee802549d7f340e7c1cf0d1c"
        streaming = fake.FakeInvoker(sse_body(), chunk_size=13)
        fleet = AsyncAgentCoreFleet(status(), gateways=fake.cloud().gateways, invoker=streaming)
        stream = await fleet.stream("hola")
        async with stream as events:
            collected = [event async for event in events]
        assert isinstance(collected[-1], Done)
        assert collected[-1].session_id == stream.session_id == streaming.calls[0][2]
        assert streaming.closed >= 1
        assert await fleet.stop_session("user-42") is True

    asyncio.run(scenario())


def test_async_errors_are_translated_too() -> None:
    async def scenario() -> None:
        invoker = fake.FakeInvoker(b"", failure=RuntimeNotFound())
        fleet = AsyncAgentCoreFleet(status(), gateways=fake.cloud().gateways, invoker=invoker)
        with pytest.raises(AgentCoreException) as raised:
            await fleet.invoke("hola")
        assert raised.value.code == "runtime_not_found"

    asyncio.run(scenario())
