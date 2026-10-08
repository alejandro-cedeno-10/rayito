"""`sbx.agent.run/stream/prepare` síncrono (`ai-agent-core`, design.md §4,
§7, §11): aplica la configuración una sola vez por sha, trocea stdout en
eventos, impone `AgentLimits`, abre el span `rayito.agent.run` sólo con
`tracer_provider=` y nunca llama a AWS ni a `grpc` (todo contra dobles de
`fake_agent.py`)."""

from __future__ import annotations

import json

import pytest

from rayito._agent._domain import AgentLimits, AgentModel, AgentSpec
from rayito._agent._runtime import WarmupStep
from rayito._otel import ALLOWED_SPAN_ATTRIBUTES, instrumentation_for
from rayito.exceptions import AgentException, InvalidArgumentException, TimeoutException
from rayito.sandbox_sync.agent import Agent

from .fake_agent import (
    FAKE_SESSION_ID,
    FakeAgentRuntime,
    FakeCommandHandle,
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


def _sandbox(handle: FakeCommandHandle) -> FakeSandbox:
    return FakeSandbox(
        commands=FakeCommands(handles=[handle]),
        files=FakeFilesystem(),
        gateways={"bedrock": gateway_status()},
    )


def test_run_returns_the_result_of_a_successful_execution() -> None:
    handle = FakeCommandHandle(
        lines=[
            _line(event="step_started", index=1),
            _line(event="text", text="hola"),
            _line(event="step_finished", index=1, usage={"input": 10, "output": 5}),
            _line(event="done", session_id=FAKE_SESSION_ID, exit_code=0, usage={}),
        ]
    )
    sandbox = _sandbox(handle)
    agent = Agent(sandbox)
    result = agent.run("hola agente", spec=_spec(), runtime=FakeAgentRuntime())
    assert result.session_id == FAKE_SESSION_ID
    assert result.text == "hola"
    assert result.steps == 1
    assert result.usage.input == 10
    assert result.exit_code == 0
    assert handle.stdin == b"hola agente"
    assert handle.stdin_closed is True


def test_run_raises_agent_exception_on_agent_failed() -> None:
    handle = FakeCommandHandle(
        lines=[_line(event="agent_failed", reason="model_error", detail_code="APIError")]
    )
    agent = Agent(_sandbox(handle))
    with pytest.raises(AgentException) as excinfo:
        agent.run("hola", spec=_spec(), runtime=FakeAgentRuntime())
    assert excinfo.value.reason == "model_error"
    assert excinfo.value.detail_code == "APIError"


def test_missing_gateway_fails_before_any_rpc() -> None:
    sandbox = FakeSandbox(commands=FakeCommands(), files=FakeFilesystem(), gateways={})
    agent = Agent(sandbox)
    with pytest.raises(InvalidArgumentException):
        agent.run("hola", spec=_spec(), runtime=FakeAgentRuntime())
    assert sandbox.commands.calls == []
    assert sandbox.files.write_files_calls == []


def test_config_is_written_once_per_sha() -> None:
    runtime = FakeAgentRuntime()
    handle_a = FakeCommandHandle(lines=[_line(event="done", exit_code=0, usage={})])
    handle_b = FakeCommandHandle(lines=[_line(event="done", exit_code=0, usage={})])
    sandbox = FakeSandbox(
        commands=FakeCommands(handles=[handle_a, handle_b]),
        files=FakeFilesystem(),
        gateways={"bedrock": gateway_status()},
    )
    agent = Agent(sandbox)
    agent.run("uno", spec=_spec(), runtime=runtime)
    agent.run("dos", spec=_spec(), runtime=runtime)
    assert len(sandbox.files.write_files_calls) == 1


def test_config_is_rewritten_when_the_sha_changes() -> None:
    sandbox = FakeSandbox(
        commands=FakeCommands(
            handles=[
                FakeCommandHandle(lines=[_line(event="done", exit_code=0, usage={})]),
                FakeCommandHandle(lines=[_line(event="done", exit_code=0, usage={})]),
            ]
        ),
        files=FakeFilesystem(),
        gateways={"bedrock": gateway_status()},
    )
    agent = Agent(sandbox)
    agent.run("uno", spec=_spec(), runtime=FakeAgentRuntime(config_sha="sha-a"))
    agent.run("dos", spec=_spec(), runtime=FakeAgentRuntime(config_sha="sha-b"))
    assert len(sandbox.files.write_files_calls) == 2


def test_max_steps_is_enforced_by_the_sdk() -> None:
    handle = FakeCommandHandle(
        lines=[_line(event="step_started", index=1), _line(event="step_started", index=2)]
    )
    agent = Agent(_sandbox(handle))
    with pytest.raises(AgentException) as excinfo:
        agent.run("hola", spec=_spec(), runtime=FakeAgentRuntime(), limits=AgentLimits(max_steps=1))
    assert excinfo.value.reason == "max_steps"


def test_token_budget_is_enforced_after_a_step_finishes() -> None:
    handle = FakeCommandHandle(
        lines=[
            _line(event="step_started", index=1),
            _line(event="step_finished", index=1, usage={"input": 100, "output": 50}),
        ]
    )
    agent = Agent(_sandbox(handle))
    with pytest.raises(AgentException) as excinfo:
        agent.run(
            "hola",
            spec=_spec(),
            runtime=FakeAgentRuntime(),
            limits=AgentLimits(max_total_tokens=10),
        )
    assert excinfo.value.reason == "token_budget"


def test_timeout_during_the_stream_becomes_agent_exception() -> None:
    handle = FakeCommandHandle(
        lines=[_line(event="step_started", index=1)],
        raise_on_iterate=TimeoutException("venció"),
    )
    agent = Agent(_sandbox(handle))
    with pytest.raises(AgentException) as excinfo:
        agent.run("hola", spec=_spec(), runtime=FakeAgentRuntime())
    assert excinfo.value.reason == "timeout"


def test_stream_never_raises_for_an_agent_failure() -> None:
    handle = FakeCommandHandle(lines=[_line(event="agent_failed", reason="busy")])
    agent = Agent(_sandbox(handle))
    with agent.stream("hola", spec=_spec(), runtime=FakeAgentRuntime()) as stream:
        events = list(stream)
    assert events[-1].type == "agent_failed"
    assert events[-1].reason == "busy"


def test_abort_kills_the_handle_of_a_kill_tree_run() -> None:
    handle = FakeCommandHandle(lines=[_line(event="step_started", index=1)])
    sandbox = FakeSandbox(
        commands=FakeCommands(handles=[handle]),
        files=FakeFilesystem(),
        gateways={"bedrock": gateway_status()},
    )
    agent = Agent(sandbox)
    runtime = FakeAgentRuntime()
    stream = agent.stream("hola", spec=_spec(), runtime=runtime)
    stream.abort()
    assert handle.killed is True
    assert len(sandbox.commands.calls) == 1, "rayd para el árbol: no hace falta otra orden"
    assert sandbox.commands.calls[0].kill_tree is True
    with pytest.raises(AgentException) as excinfo:
        stream.result()
    assert excinfo.value.reason == "aborted"


def test_prepare_fires_warmup_steps_in_the_background_and_returns_immediately() -> None:
    handle_a = FakeCommandHandle(lines=[])
    handle_b = FakeCommandHandle(lines=[])
    sandbox = FakeSandbox(
        commands=FakeCommands(handles=[handle_a, handle_b]), files=FakeFilesystem()
    )
    agent = Agent(sandbox)
    runtime = FakeAgentRuntime(
        warmup=(WarmupStep(cmd="echo a", background=True), WarmupStep(cmd="echo b"))
    )
    agent.prepare(runtime=runtime)
    assert [call.cmd for call in sandbox.commands.calls] == ["echo a", "echo b"]
    assert handle_a.disconnected is True
    assert handle_b.disconnected is True


def test_touching_sbx_agent_makes_no_call() -> None:
    sandbox = FakeSandbox(commands=FakeCommands(), files=FakeFilesystem())
    agent = Agent(sandbox)
    assert agent is not None
    assert sandbox.commands.calls == []
    assert sandbox.files.write_files_calls == []


def test_spans_are_noop_without_tracer_provider() -> None:
    handle = FakeCommandHandle(lines=[_line(event="done", exit_code=0, usage={})])
    sandbox = _sandbox(handle)
    assert sandbox._instrumentation is instrumentation_for(None)
    agent = Agent(sandbox)
    result = agent.run("hola", spec=_spec(), runtime=FakeAgentRuntime())
    assert result.exit_code == 0


class _RecordingTracerProvider:
    def __init__(self) -> None:
        self.spans: list[tuple[str, dict[str, object]]] = []

    def get_tracer(self, name: str, version: str | None = None) -> _RecordingTracer:
        return _RecordingTracer(self.spans)


class _RecordingTracer:
    def __init__(self, spans: list[tuple[str, dict[str, object]]]) -> None:
        self._spans = spans

    def start_as_current_span(self, name: str, *, kind, attributes, **_kwargs):  # type: ignore[no-untyped-def]
        import contextlib

        self._spans.append((name, dict(attributes)))

        @contextlib.contextmanager
        def _cm():  # type: ignore[no-untyped-def]
            yield _RecordingSpan()

        return _cm()


class _RecordingSpan:
    def set_attribute(self, key: str, value: object) -> None:
        return None

    def record_exception(self, *args: object, **kwargs: object) -> None:
        return None

    def set_status(self, *args: object, **kwargs: object) -> None:
        return None


def test_span_attributes_are_a_subset_of_the_allowed_list() -> None:
    pytest.importorskip("opentelemetry.trace")
    handle = FakeCommandHandle(
        lines=[
            _line(event="step_started", index=1),
            _line(event="step_finished", index=1, usage={"input": 1, "output": 1}),
            _line(event="done", session_id=FAKE_SESSION_ID, exit_code=0, usage={}),
        ]
    )
    sandbox = _sandbox(handle)
    provider = _RecordingTracerProvider()
    sandbox._instrumentation = instrumentation_for(provider)
    agent = Agent(sandbox)
    result = agent.run("hola", spec=_spec(), runtime=FakeAgentRuntime())
    assert result.exit_code == 0
    assert len(provider.spans) == 1
    name, attributes = provider.spans[0]
    assert name == "rayito.agent.run"
    assert set(attributes) <= ALLOWED_SPAN_ATTRIBUTES
    assert attributes["gen_ai.operation.name"] == "invoke_agent"
    assert attributes["gen_ai.provider.name"] == "aws.bedrock"
    assert "prompt" not in str(attributes).lower()


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
def test_sdk_limit_kills_the_handle_of_a_kill_tree_run(
    limits: AgentLimits, lines: list[bytes], reason: str
) -> None:
    handle = FakeCommandHandle(lines=[*lines, _line(event="step_started", index=3)])
    sandbox = FakeSandbox(
        commands=FakeCommands(handles=[handle]),
        files=FakeFilesystem(),
        gateways={"bedrock": gateway_status()},
    )
    agent = Agent(sandbox)
    stream = agent.stream("hola", spec=_spec(), runtime=FakeAgentRuntime(), limits=limits)
    events = list(stream)
    assert events[-1].type == "agent_failed"
    assert events[-1].reason == reason
    assert handle.killed is True
    assert len(sandbox.commands.calls) == 1, "rayd para el árbol: no hace falta otra orden"
    assert sandbox.commands.calls[0].kill_tree is True


def test_timeout_reported_by_the_end_event_becomes_timeout() -> None:
    """`rayd` mata el proceso al vencer el timeout y el stream acaba con su
    `EndEvent`, sin lanzar al iterar: `wait()` es quien lo dice."""
    handle = FakeCommandHandle(
        lines=[_line(event="step_started", index=1)],
        exit_code=-1,
        raise_on_wait=TimeoutException("venció"),
    )
    agent = Agent(_sandbox(handle))
    with pytest.raises(AgentException) as excinfo:
        agent.run("hola", spec=_spec(), runtime=FakeAgentRuntime())
    assert excinfo.value.reason == "timeout"
