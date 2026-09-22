"""`AsyncSandbox.run_code` y los contextos de código: misma superficie que la
versión síncrona sobre `grpc.aio`, contra el mismo `rayd` falso."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import grpc
import pytest

from rayito import (
    AsyncSandbox,
    ChartType,
    CodeContext,
    Execution,
    ExecutionError,
    LineChart,
    OutputMessage,
    Result,
)
from rayito._limits import DEFAULT_PORT
from rayito._process_base import STREAM_EOF
from rayito._sandbox_base import ReconnectPoll
from rayito._transport import PROXY_AUTH_KEY, PROXY_FORBIDDEN_MARKER
from rayito.exceptions import (
    AuthenticationException,
    InvalidArgumentException,
    NotFoundException,
    RateLimitException,
    SandboxException,
    SandboxNotFoundException,
    SandboxStateException,
    TimeoutException,
)

from .conftest import (
    ACCESS_TOKEN,
    IMAGE_ARN,
    JWE,
    SANDBOX_ID,
    FakeRpcError,
    RaydEndpoint,
    StubbedControlPlane,
    auth_token_response,
    microvm_response,
)
from .fake_code import DATAFRAME_DATA, MAX_CONTEXTS, OMITTED_NOTE

TIMEOUT_LATENCY_BUDGET_SECONDS = 2.0
INTERRUPT_BUDGET_SECONDS = 2.0
STEP_SECONDS = 0.02


def stub_launch(control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint) -> None:
    control_plane.microvms.add_response("run_microvm", microvm_response(endpoint=fake_rayd.host))
    control_plane.microvms.add_response(
        "create_microvm_auth_token",
        auth_token_response(),
        expected_params={
            "microvmIdentifier": SANDBOX_ID,
            "expirationInMinutes": 60,
            "allowedPorts": [{"port": DEFAULT_PORT}],
        },
    )


def stub_remint(control_plane: StubbedControlPlane, jwe: str) -> None:
    control_plane.microvms.add_response(
        "create_microvm_auth_token",
        auth_token_response(jwe),
        expected_params={
            "microvmIdentifier": SANDBOX_ID,
            "expirationInMinutes": 60,
            "allowedPorts": [{"port": DEFAULT_PORT}],
        },
    )


@pytest.fixture
async def sandbox(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> AsyncIterator[AsyncSandbox]:
    stub_launch(control_plane, fake_rayd)
    created = await AsyncSandbox.create(
        IMAGE_ARN,
        idle=None,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    )
    try:
        yield created
    finally:
        control_plane.microvms.add_response(
            "terminate_microvm", {}, expected_params={"microvmIdentifier": SANDBOX_ID}
        )
        await created.kill()


def proxy_forbidden() -> FakeRpcError:
    return FakeRpcError(
        grpc.StatusCode.PERMISSION_DENIED,
        details="Received http2 header with status: 403",
        debug=f'{{"grpc_status":7,"description":"{PROXY_FORBIDDEN_MARKER}"}}',
    )


ReadStep = Callable[[], Awaitable[Any]]


class AsyncScriptedCall:
    """Un sustituto del call de `grpc.aio`: cada `read()` ejecuta el siguiente
    paso encolado (un mensaje del call real o una excepción) y `cancel()`
    queda registrado."""

    def __init__(self, steps: list[ReadStep]) -> None:
        self._steps = steps
        self.cancelled = False

    async def read(self) -> Any:
        if not self._steps:
            return STREAM_EOF
        return await self._steps.pop(0)()

    def cancel(self) -> None:
        self.cancelled = True


def raising(error: BaseException) -> ReadStep:
    async def step() -> Any:
        raise error

    return step


def first_of(call: Any) -> ReadStep:
    async def step() -> Any:
        return await call.read()

    return step


def execute_failing_first(
    real: Callable[..., Any], failures: list[list[ReadStep]]
) -> Callable[..., Any]:
    def start(request: Any, timeout: float | None = None) -> Any:
        if failures:
            return AsyncScriptedCall(failures.pop(0))
        return real(request, timeout=timeout)

    return start


async def wait_until(predicate: Callable[[], bool], budget: float) -> bool:
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(STEP_SECONDS)
    return predicate()


async def test_async_acceptance_sequence(sandbox: AsyncSandbox, fake_rayd: RaydEndpoint) -> None:
    assignment = await sandbox.run_code("x = 42")
    assert isinstance(assignment, Execution)
    assert assignment.results == []
    assert assignment.error is None
    assert assignment.execution_count == 1
    assert assignment.text is None

    value = await sandbox.run_code("x")
    assert value.text == "42"
    assert value.results[0].is_main_result is True
    assert value.results[0].formats() == ["text"]

    printed = await sandbox.run_code("print(x)")
    assert "42" in "".join(printed.logs.stdout)
    assert printed.text is None

    plot = await sandbox.run_code("plot")
    assert plot.results[0].png is not None
    assert base64.b64decode(plot.results[0].png)[:8] == b"\x89PNG\r\n\x1a\n"
    assert isinstance(plot.results[0].chart, LineChart)
    assert plot.results[0].chart.type is ChartType.LINE
    assert plot.results[0].is_main_result is False

    frame = await sandbox.run_code("df")
    assert frame.results[0].data == DATAFRAME_DATA
    assert frame.results[0].html is not None

    failed = await sandbox.run_code("1/0")
    assert failed.error is not None
    assert failed.error.name == "ZeroDivisionError"
    assert "ZeroDivisionError" in failed.error.traceback
    assert failed.execution_count == 6
    assert fake_rayd.code.execute_requests[0].timeout_ms == 300_000
    assert fake_rayd.code.execute_metadata[0][PROXY_AUTH_KEY] == JWE


async def test_async_timeout_is_data(sandbox: AsyncSandbox, fake_rayd: RaydEndpoint) -> None:
    started = time.perf_counter()
    execution = await sandbox.run_code("sleep", timeout=1)
    assert time.perf_counter() - started < TIMEOUT_LATENCY_BUDGET_SECONDS
    assert execution.error is not None
    assert execution.error.name == "ExecutionTimeout"
    assert "1000" in execution.error.value
    assert execution.execution_count == 1
    assert fake_rayd.code.executions[-1].timeout_ms == 1000
    assert (await sandbox.run_code("1+1", timeout=None)).text == "2"
    assert fake_rayd.code.executions[-1].timeout_ms == 0


async def test_async_callbacks_and_envs(sandbox: AsyncSandbox, fake_rayd: RaydEndpoint) -> None:
    seen: list[object] = []
    await sandbox.run_code("x = 42")
    await sandbox.run_code("print(x)", on_stdout=seen.append)
    await sandbox.run_code("stderr", on_stderr=seen.append)
    await sandbox.run_code("many", on_result=seen.append)
    await sandbox.run_code("raise ValueError('boom')", on_error=seen.append)
    assert isinstance(seen[0], OutputMessage)
    assert (seen[0].line, seen[0].error) == ("42\n", False)
    assert isinstance(seen[1], OutputMessage)
    assert seen[1].error is True
    assert [item.text for item in seen[2:5] if isinstance(item, Result)] == ["one", "two", "three"]
    assert isinstance(seen[5], ExecutionError)
    assert seen[5].name == "ValueError"
    echoed = await sandbox.run_code("envs-echo", envs={"M4_RUN": "yes"})
    assert json.loads("".join(echoed.logs.stdout)) == {"M4_RUN": "yes"}
    assert fake_rayd.code.executions[-1].envs == {"M4_RUN": "yes"}


async def test_async_callback_exception_cancels_the_stream(
    sandbox: AsyncSandbox, fake_rayd: RaydEndpoint
) -> None:
    def boom(message: OutputMessage) -> None:
        raise RuntimeError("callback")

    with pytest.raises(RuntimeError, match="callback"):
        await sandbox.run_code("print-then-sleep", timeout=None, on_stdout=boom)
    assert await wait_until(
        lambda: fake_rayd.code.executions[-1].interrupted, INTERRUPT_BUDGET_SECONDS
    )
    assert (await sandbox.run_code("1+1")).text == "2"


async def test_async_result_edge_cases(sandbox: AsyncSandbox) -> None:
    assert (await sandbox.run_code("bad-json")).results[0].json == "{not json"
    assert (await sandbox.run_code("omitted")).results[0].extra == {"rayito/omitted": OMITTED_NOTE}
    died = await sandbox.run_code("kernel-die")
    assert died.error is not None
    assert died.error.name == "KernelDied"
    assert (await sandbox.run_code("2*2")).text == "4"


async def test_async_context_lifecycle(sandbox: AsyncSandbox, fake_rayd: RaydEndpoint) -> None:
    await sandbox.run_code("x = 42")
    context = await sandbox.create_code_context()
    assert isinstance(context, CodeContext)
    assert context.id != "default"
    assert (context.language, context.cwd) == ("python", "/home/user")
    isolated = await sandbox.run_code("x", context=context)
    assert isolated.error is not None
    assert isolated.error.name == "NameError"
    await sandbox.run_code("y = 7", context=context)
    assert (await sandbox.run_code("y", context=context.id)).text == "7"
    ids = [item.id for item in await sandbox.list_code_contexts()]
    assert ids[0] == "default"
    assert context.id in ids
    other = await sandbox.create_code_context(cwd="/tmp", envs={"M4_ENV": "1"})
    assert other.cwd == "/tmp"
    await sandbox.restart_code_context(context)
    assert fake_rayd.code.restart_requests == [context.id]
    restarted = await sandbox.run_code("y", context=context)
    assert restarted.error is not None
    assert restarted.error.name == "NameError"
    await sandbox.remove_code_context(context)
    assert context.id not in [item.id for item in await sandbox.list_code_contexts()]
    with pytest.raises(NotFoundException):
        await sandbox.run_code("1", context=context)
    with pytest.raises(InvalidArgumentException):
        await sandbox.remove_code_context("default")
    with pytest.raises(NotFoundException):
        await sandbox.remove_code_context("ctx-000000000000")
    await sandbox.remove_code_context(other.id)
    assert (await sandbox.run_code("x")).text == "42"


async def test_async_context_limits_and_arguments(
    sandbox: AsyncSandbox, fake_rayd: RaydEndpoint
) -> None:
    for _ in range(MAX_CONTEXTS - 1):
        await sandbox.create_code_context()
    with pytest.raises(RateLimitException):
        await sandbox.create_code_context()
    with pytest.raises(InvalidArgumentException, match="language"):
        await sandbox.create_code_context(language="ruby")
    with pytest.raises(InvalidArgumentException, match="cwd"):
        await sandbox.create_code_context(cwd="/does/not/exist")
    with pytest.raises(InvalidArgumentException):
        await sandbox.run_code("x", context="")
    with pytest.raises(InvalidArgumentException, match="1 MiB"):
        await sandbox.run_code("a" * (1_048_576 + 1))
    assert len(fake_rayd.code.execute_requests) == 0


async def test_async_execute_uses_the_unary_channel(
    sandbox: AsyncSandbox, fake_rayd: RaydEndpoint
) -> None:
    for _ in range(10):
        assert (await sandbox.run_code("1+1")).text == "2"
    await sandbox.list_code_contexts()
    assert len(fake_rayd.code.peers) == 1
    assert sandbox._stream_channel is None


async def test_async_client_deadline_is_a_timeout_exception(
    sandbox: AsyncSandbox, fake_rayd: RaydEndpoint
) -> None:
    with pytest.raises(TimeoutException):
        await sandbox.run_code("sleep", timeout=None, request_timeout=0.2)
    assert await wait_until(
        lambda: fake_rayd.code.executions[-1].interrupted, INTERRUPT_BUDGET_SECONDS
    )


async def test_async_kernel_gate_without_probing_health(
    sandbox: AsyncSandbox, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.code.kernel_gate = "sidecar relaunching"
    health_calls = len(fake_rayd.servicer.health_calls)
    with pytest.raises(SandboxException, match="kernel no está listo") as excinfo:
        await sandbox.run_code("x")
    assert not isinstance(excinfo.value, SandboxStateException)
    assert len(fake_rayd.servicer.health_calls) == health_calls
    fake_rayd.code.phase = "suspending"
    fake_rayd.code.kernel_gate = None
    with pytest.raises(SandboxStateException, match="suspending"):
        await sandbox.run_code("x")
    fake_rayd.code.phase = None
    assert (await sandbox.run_code("1+1")).text == "2"


async def test_async_proxy_403_on_execute_remints_once(
    sandbox: AsyncSandbox,
    fake_rayd: RaydEndpoint,
    control_plane: StubbedControlPlane,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_remint(control_plane, "jwe-2")
    monkeypatch.setattr(
        sandbox._code,
        "Execute",
        execute_failing_first(sandbox._code.Execute, [[raising(proxy_forbidden())]]),
    )
    assert (await sandbox.run_code("1+1")).text == "2"
    assert len(fake_rayd.code.executions) == 1
    assert fake_rayd.code.execute_metadata[-1][PROXY_AUTH_KEY] == "jwe-2"
    control_plane.microvms.assert_no_pending_responses()


async def test_async_double_proxy_403_surfaces_proxy_rejected(
    sandbox: AsyncSandbox, control_plane: StubbedControlPlane, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_remint(control_plane, "jwe-2")
    monkeypatch.setattr(
        sandbox._code,
        "Execute",
        execute_failing_first(
            sandbox._code.Execute,
            [[raising(proxy_forbidden())], [raising(proxy_forbidden())]],
        ),
    )
    with pytest.raises(AuthenticationException) as excinfo:
        await sandbox.run_code("1+1")
    assert excinfo.value.proxy_rejected is True
    control_plane.microvms.assert_no_pending_responses()


async def test_async_stream_reset_after_started_maps_the_microvm_state(
    sandbox: AsyncSandbox,
    fake_rayd: RaydEndpoint,
    control_plane: StubbedControlPlane,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_execute = sandbox._code.Execute
    reset = FakeRpcError(grpc.StatusCode.UNAVAILABLE, details="Socket closed")
    calls: list[AsyncScriptedCall] = []

    def resetting(request: Any, timeout: float | None = None) -> AsyncScriptedCall:
        real = real_execute(request, timeout=timeout)
        calls.append(AsyncScriptedCall([first_of(real), raising(reset)]))
        return calls[-1]

    monkeypatch.setattr(sandbox._code, "Execute", resetting)
    monkeypatch.setattr(ReconnectPoll, "STATE_CHECK_INTERVAL", 0.1)
    fake_rayd.servicer.unavailable_calls = 10_000
    control_plane.microvms.add_response(
        "get_microvm",
        microvm_response(endpoint=fake_rayd.host, state="TERMINATED", state_reason="Success."),
    )
    with pytest.raises(SandboxNotFoundException, match="TERMINATED"):
        await sandbox.run_code("1+1")
    assert calls[0].cancelled is True


async def test_async_stream_ending_without_end_is_a_protocol_error(
    sandbox: AsyncSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_execute = sandbox._code.Execute
    calls: list[AsyncScriptedCall] = []

    def truncated(request: Any, timeout: float | None = None) -> AsyncScriptedCall:
        calls.append(AsyncScriptedCall([first_of(real_execute(request, timeout=timeout))]))
        return calls[-1]

    monkeypatch.setattr(sandbox._code, "Execute", truncated)
    with pytest.raises(SandboxException, match="ExecutionEnd"):
        await sandbox.run_code("1+1")
    assert calls[0].cancelled is True


async def test_async_create_waits_for_kernel_ready(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.kernel_not_ready_calls = 2
    stub_launch(control_plane, fake_rayd)
    control_plane.microvms.add_response(
        "terminate_microvm", {}, expected_params={"microvmIdentifier": SANDBOX_ID}
    )
    async with await AsyncSandbox.create(
        IMAGE_ARN,
        idle=None,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
        ready_timeout=20,
    ) as sandbox:
        assert (await sandbox.run_code("1+1")).text == "2"
    assert len(fake_rayd.servicer.health_calls) == 3
    assert fake_rayd.servicer.kernel_not_ready_calls == 0


# ------------------------------------------------------------ M7: languages


async def test_async_language_parity(sandbox: AsyncSandbox, fake_rayd: RaydEndpoint) -> None:
    execution = await sandbox.run_code("echo hi", language="bash")
    assert "".join(execution.logs.stdout) == "hi\n"
    assert fake_rayd.code.execute_requests[-1].language == "bash"
    assert fake_rayd.code.executions[-1].context_id == "default-bash"
    listed = [(item.id, item.language) for item in await sandbox.list_code_contexts()]
    assert ("default-bash", "bash") in listed
    context = await sandbox.create_code_context(language="bash")
    assert context.language == "bash"
    assert fake_rayd.code.create_requests[-1].language == "bash"
    with pytest.raises(InvalidArgumentException, match="excluyentes"):
        await sandbox.run_code("echo 1", language="bash", context=context)
    with pytest.raises(InvalidArgumentException, match="language"):
        await sandbox.run_code("echo 1", language="r")
    with pytest.raises(InvalidArgumentException, match="python"):
        await sandbox.run_code("echo $A", language="bash", envs={"A": "1"})
    fake_rayd.code.languages = frozenset({"python"})
    with pytest.raises(InvalidArgumentException) as excinfo:
        await sandbox.run_code("1 + 1", language="javascript")
    assert excinfo.value.grpc_code is grpc.StatusCode.UNIMPLEMENTED
    assert "rayito-base-poly" in str(excinfo.value)
