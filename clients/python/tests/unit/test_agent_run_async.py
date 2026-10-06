"""`sbx.agent.run/stream/prepare` asíncrono: mismo contrato que
`test_agent_run.py` sobre `asyncio` (design.md §11, paridad sync/async).
Cancelar la tarea que itera un `AgentStream` dispara `abort()`."""

from __future__ import annotations

import asyncio
import json

import pytest

from rayito._agent._domain import AgentLimits, AgentModel, AgentSpec
from rayito._agent._runtime import WarmupStep
from rayito._agent._stream_base import stop_tree_command
from rayito.exceptions import AgentException, InvalidArgumentException, TimeoutException
from rayito.sandbox_async.agent import AsyncAgent

from .fake_agent import (
    FAKE_PID,
    FAKE_SESSION_ID,
    FakeAgentRuntime,
    FakeAsyncCommandHandle,
    FakeCommands,
    FakeFilesystem,
    FakeSandbox,
    gateway_status,
)


def _line(**payload: object) -> bytes:
    return json.dumps(payload).encode("utf-8")


def _spec(gateway: str = "bedrock") -> AgentSpec:
    return AgentSpec(
        model=AgentModel(provider="bedrock", id="model-x", gateway=gateway, region="us-east-1")
    )


def _sandbox(handle: FakeAsyncCommandHandle) -> FakeSandbox:
    return FakeSandbox(
        commands=FakeCommands(handles=[handle], is_async=True),
        files=FakeFilesystem(is_async=True),
        gateways={"bedrock": gateway_status()},
    )


async def test_run_returns_the_result_of_a_successful_execution() -> None:
    handle = FakeAsyncCommandHandle(
        lines=[
            _line(event="step_started", index=1),
            _line(event="text", text="hola"),
            _line(event="step_finished", index=1, usage={"input": 10, "output": 5}),
            _line(event="done", session_id=FAKE_SESSION_ID, exit_code=0, usage={}),
        ]
    )
    agent = AsyncAgent(_sandbox(handle))
    result = await agent.run("hola agente", spec=_spec(), runtime=FakeAgentRuntime())
    assert result.session_id == FAKE_SESSION_ID
    assert result.text == "hola"
    assert result.usage.input == 10
    assert handle.stdin == b"hola agente"


async def test_run_raises_agent_exception_on_agent_failed() -> None:
    handle = FakeAsyncCommandHandle(lines=[_line(event="agent_failed", reason="model_error")])
    agent = AsyncAgent(_sandbox(handle))
    with pytest.raises(AgentException) as excinfo:
        await agent.run("hola", spec=_spec(), runtime=FakeAgentRuntime())
    assert excinfo.value.reason == "model_error"


async def test_missing_gateway_fails_before_any_rpc() -> None:
    sandbox = FakeSandbox(
        commands=FakeCommands(is_async=True), files=FakeFilesystem(is_async=True), gateways={}
    )
    agent = AsyncAgent(sandbox)
    with pytest.raises(InvalidArgumentException):
        await agent.run("hola", spec=_spec(), runtime=FakeAgentRuntime())
    assert sandbox.commands.calls == []


async def test_max_steps_is_enforced_by_the_sdk() -> None:
    handle = FakeAsyncCommandHandle(
        lines=[_line(event="step_started", index=1), _line(event="step_started", index=2)]
    )
    agent = AsyncAgent(_sandbox(handle))
    with pytest.raises(AgentException) as excinfo:
        await agent.run(
            "hola", spec=_spec(), runtime=FakeAgentRuntime(), limits=AgentLimits(max_steps=1)
        )
    assert excinfo.value.reason == "max_steps"


async def test_token_budget_is_enforced_after_a_step_finishes() -> None:
    handle = FakeAsyncCommandHandle(
        lines=[
            _line(event="step_started", index=1),
            _line(event="step_finished", index=1, usage={"input": 100, "output": 50}),
        ]
    )
    agent = AsyncAgent(_sandbox(handle))
    with pytest.raises(AgentException) as excinfo:
        await agent.run(
            "hola",
            spec=_spec(),
            runtime=FakeAgentRuntime(),
            limits=AgentLimits(max_total_tokens=10),
        )
    assert excinfo.value.reason == "token_budget"


async def test_timeout_during_the_stream_becomes_agent_exception() -> None:
    handle = FakeAsyncCommandHandle(
        lines=[_line(event="step_started", index=1)],
        raise_on_iterate=TimeoutException("venció"),
    )
    agent = AsyncAgent(_sandbox(handle))
    with pytest.raises(AgentException) as excinfo:
        await agent.run("hola", spec=_spec(), runtime=FakeAgentRuntime())
    assert excinfo.value.reason == "timeout"


async def test_stream_never_raises_for_an_agent_failure() -> None:
    handle = FakeAsyncCommandHandle(lines=[_line(event="agent_failed", reason="busy")])
    agent = AsyncAgent(_sandbox(handle))
    stream = await agent.stream("hola", spec=_spec(), runtime=FakeAgentRuntime())
    events = [event async for event in stream]
    await stream.aclose()
    assert events[-1].type == "agent_failed"
    assert events[-1].reason == "busy"


async def test_abort_runs_the_runtime_abort_command_then_kills_the_handle() -> None:
    handle = FakeAsyncCommandHandle(lines=[_line(event="step_started", index=1)])
    sandbox = FakeSandbox(
        commands=FakeCommands(handles=[handle], foreground_results=[None, None], is_async=True),
        files=FakeFilesystem(is_async=True),
        gateways={"bedrock": gateway_status()},
    )
    agent = AsyncAgent(sandbox)
    runtime = FakeAgentRuntime(abort_cmd="curl -X POST http://127.0.0.1:4096/session/x/abort")
    stream = await agent.stream("hola", spec=_spec(), runtime=runtime)
    await stream.abort()
    assert handle.killed is True
    assert [call.cmd for call in sandbox.commands.calls[1:]] == [
        runtime.abort_cmd,
        stop_tree_command(FAKE_PID),
    ]
    with pytest.raises(AgentException) as excinfo:
        await stream.result()
    assert excinfo.value.reason == "aborted"


async def test_cancelling_the_iterating_task_aborts_the_stream() -> None:
    handle = FakeAsyncCommandHandle(lines=[_line(event="step_started", index=1)] * 50)
    agent = AsyncAgent(_sandbox(handle))

    async def consume() -> None:
        async with await agent.stream("hola", spec=_spec(), runtime=FakeAgentRuntime()) as stream:
            async for _event in stream:
                await asyncio.sleep(10)

    task = asyncio.ensure_future(consume())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert handle.killed is True


async def test_prepare_fires_warmup_steps_in_the_background_and_returns_immediately() -> None:
    handle_a = FakeAsyncCommandHandle(lines=[])
    handle_b = FakeAsyncCommandHandle(lines=[])
    sandbox = FakeSandbox(
        commands=FakeCommands(handles=[handle_a, handle_b], is_async=True),
        files=FakeFilesystem(is_async=True),
    )
    agent = AsyncAgent(sandbox)
    runtime = FakeAgentRuntime(
        warmup=(WarmupStep(cmd="echo a", background=True), WarmupStep(cmd="echo b"))
    )
    await agent.prepare(runtime=runtime)
    assert [call.cmd for call in sandbox.commands.calls] == ["echo a", "echo b"]
    assert handle_a.disconnected is True
    assert handle_b.disconnected is True


@pytest.mark.parametrize(
    ("limits", "lines", "reason"),
    [
        (
            AgentLimits(max_steps=1),
            [_line(event="step_started", index=1), _line(event="step_started", index=2)],
            "max_steps",
        ),
        (
            AgentLimits(max_total_tokens=10),
            [
                _line(event="step_started", index=1),
                _line(event="step_finished", index=1, usage={"input": 100, "output": 50}),
            ],
            "token_budget",
        ),
    ],
)
async def test_sdk_limit_stops_the_runtime_and_its_process_tree(
    limits: AgentLimits, lines: list[bytes], reason: str
) -> None:
    handle = FakeAsyncCommandHandle(lines=[*lines, _line(event="step_started", index=3)])
    sandbox = FakeSandbox(
        commands=FakeCommands(handles=[handle], is_async=True),
        files=FakeFilesystem(is_async=True),
        gateways={"bedrock": gateway_status()},
    )
    agent = AsyncAgent(sandbox)
    stream = await agent.stream("hola", spec=_spec(), runtime=FakeAgentRuntime(), limits=limits)
    events = [event async for event in stream]
    assert events[-1].type == "agent_failed"
    assert events[-1].reason == reason
    assert handle.killed is True
    assert [call.cmd for call in sandbox.commands.calls[1:]] == [stop_tree_command(FAKE_PID)]


async def test_timeout_reported_by_the_end_event_becomes_timeout() -> None:
    """`rayd` mata el proceso al vencer el timeout y el stream acaba con su
    `EndEvent`, sin lanzar al iterar: `wait()` es quien lo dice."""
    handle = FakeAsyncCommandHandle(
        lines=[_line(event="step_started", index=1)],
        exit_code=-1,
        raise_on_wait=TimeoutException("venció"),
    )
    agent = AsyncAgent(_sandbox(handle))
    with pytest.raises(AgentException) as excinfo:
        await agent.run("hola", spec=_spec(), runtime=FakeAgentRuntime())
    assert excinfo.value.reason == "timeout"
