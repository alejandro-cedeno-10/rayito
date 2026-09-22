"""`Sandbox.run_code` y los contextos de código contra el `rayd` falso (gRPC
real en loopback) y el plano de control con Stubber."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable, Iterator
from typing import Any

import grpc
import pytest

from rayito import (
    ChartType,
    CodeContext,
    Execution,
    ExecutionError,
    LineChart,
    OutputMessage,
    Result,
    Sandbox,
)
from rayito._limits import DEFAULT_PORT
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
def sandbox(control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint) -> Iterator[Sandbox]:
    stub_launch(control_plane, fake_rayd)
    created = Sandbox.create(
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
        created.kill()


def proxy_forbidden() -> FakeRpcError:
    return FakeRpcError(
        grpc.StatusCode.PERMISSION_DENIED,
        details="Received http2 header with status: 403",
        debug=f'{{"grpc_status":7,"description":"{PROXY_FORBIDDEN_MARKER}"}}',
    )


def failing_stream(error: grpc.RpcError) -> Iterator[Any]:
    yield from ()
    raise error


def start_failing_first(
    real: Callable[..., Any], failures: list[Callable[[], Iterator[Any]]]
) -> Callable[..., Any]:
    def start(request: Any, timeout: float | None = None) -> Any:
        if failures:
            return failures.pop(0)()
        return real(request, timeout=timeout)

    return start


class ScriptedCall:
    """Un sustituto del call de `grpc` que entrega los eventos de un iterador
    y acepta `cancel()`, para simular resets y streams truncados."""

    def __init__(self, events: Iterator[Any]) -> None:
        self._events = events
        self.cancelled = False

    def __iter__(self) -> ScriptedCall:
        return self

    def __next__(self) -> Any:
        return next(self._events)

    def cancel(self) -> None:
        self.cancelled = True


def wait_until(predicate: Callable[[], bool], budget: float) -> bool:
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(STEP_SECONDS)
    return predicate()


def test_acceptance_sequence_against_the_fake(sandbox: Sandbox, fake_rayd: RaydEndpoint) -> None:
    assignment = sandbox.run_code("x = 42")
    assert isinstance(assignment, Execution)
    assert assignment.results == []
    assert assignment.error is None
    assert assignment.execution_count == 1
    assert assignment.text is None

    value = sandbox.run_code("x")
    assert value.text == "42"
    assert value.results[0].is_main_result is True
    assert value.results[0].formats() == ["text"]
    assert value.execution_count == 2

    printed = sandbox.run_code("print(x)")
    assert "42" in "".join(printed.logs.stdout)
    assert printed.text is None
    assert printed.results == []

    plot = sandbox.run_code("plot")
    assert plot.error is None
    assert plot.results[0].png is not None
    assert base64.b64decode(plot.results[0].png)[:8] == b"\x89PNG\r\n\x1a\n"
    assert isinstance(plot.results[0].chart, LineChart)
    assert plot.results[0].chart.type is ChartType.LINE
    assert len(plot.results[0].chart.elements[0].points) == 3
    assert plot.results[0].is_main_result is False
    assert {"png", "chart"} <= set(plot.results[0].formats())

    frame = sandbox.run_code("df")
    assert frame.text is not None and "a" in frame.text
    assert frame.results[0].html is not None
    assert frame.results[0].data == DATAFRAME_DATA
    assert frame.results[0].is_main_result is True

    failed = sandbox.run_code("1/0")
    assert failed.error is not None
    assert failed.error.name == "ZeroDivisionError"
    assert "division by zero" in failed.error.value
    assert "ZeroDivisionError" in failed.error.traceback
    assert failed.execution_count == 6
    assert failed.results == []

    request = fake_rayd.code.execute_requests[0]
    assert request.timeout_ms == 300_000
    assert not request.HasField("context_id")
    assert fake_rayd.code.execute_metadata[0][PROXY_AUTH_KEY] == JWE


def test_timeout_is_data_and_reaches_the_agent(sandbox: Sandbox, fake_rayd: RaydEndpoint) -> None:
    started = time.perf_counter()
    execution = sandbox.run_code("sleep", timeout=1)
    assert time.perf_counter() - started < TIMEOUT_LATENCY_BUDGET_SECONDS
    assert execution.error is not None
    assert execution.error.name == "ExecutionTimeout"
    assert "1000" in execution.error.value
    assert execution.execution_count == 1
    assert fake_rayd.code.executions[-1].timeout_ms == 1000
    assert sandbox.run_code("1+1", timeout=None).text == "2"
    assert fake_rayd.code.executions[-1].timeout_ms == 0
    assert sandbox.run_code("1+1", timeout=0).text == "2"
    assert fake_rayd.code.executions[-1].timeout_ms == 0


def test_callbacks_run_in_order_with_typed_messages(sandbox: Sandbox) -> None:
    seen: list[object] = []
    sandbox.run_code("x = 42")
    sandbox.run_code("print(x)", on_stdout=seen.append)
    sandbox.run_code("stderr", on_stderr=seen.append)
    sandbox.run_code("many", on_result=seen.append)
    sandbox.run_code("raise ValueError('boom')", on_error=seen.append)
    assert isinstance(seen[0], OutputMessage)
    assert seen[0].line == "42\n"
    assert seen[0].error is False
    assert seen[0].timestamp > 0
    assert isinstance(seen[1], OutputMessage)
    assert seen[1].error is True
    results = [item for item in seen[2:5] if isinstance(item, Result)]
    assert [item.text for item in results] == ["one", "two", "three"]
    assert [item.is_main_result for item in results] == [False, True, False]
    assert isinstance(seen[5], ExecutionError)
    assert seen[5].name == "ValueError"
    assert seen[5].value == "boom"


def test_callback_exceptions_propagate_and_cancel_the_stream(
    sandbox: Sandbox, fake_rayd: RaydEndpoint
) -> None:
    def boom(message: OutputMessage) -> None:
        raise RuntimeError("callback")

    with pytest.raises(RuntimeError, match="callback"):
        sandbox.run_code("print-then-sleep", timeout=None, on_stdout=boom)
    assert wait_until(lambda: fake_rayd.code.executions[-1].interrupted, INTERRUPT_BUDGET_SECONDS)
    assert sandbox.run_code("1+1").text == "2"


def test_envs_are_forwarded_per_execution(sandbox: Sandbox, fake_rayd: RaydEndpoint) -> None:
    execution = sandbox.run_code("envs-echo", envs={"M4_RUN": "yes"})
    assert json.loads("".join(execution.logs.stdout)) == {"M4_RUN": "yes"}
    assert fake_rayd.code.executions[-1].envs == {"M4_RUN": "yes"}
    assert json.loads("".join(sandbox.run_code("envs-echo").logs.stdout)) == {}


def test_result_edge_cases_from_the_fake(sandbox: Sandbox) -> None:
    assert sandbox.run_code("bad-json").results[0].json == "{not json"
    omitted = sandbox.run_code("omitted").results[0]
    assert omitted.extra == {"rayito/omitted": OMITTED_NOTE}
    assert omitted.formats() == ["rayito/omitted"]
    died = sandbox.run_code("kernel-die")
    assert died.error is not None
    assert died.error.name == "KernelDied"
    assert died.execution_count == 3
    assert sandbox.run_code("1+1").text == "2"


def test_context_lifecycle(sandbox: Sandbox, fake_rayd: RaydEndpoint) -> None:
    sandbox.run_code("x = 42")
    context = sandbox.create_code_context()
    assert isinstance(context, CodeContext)
    assert context.id != "default"
    assert context.language == "python"
    assert context.cwd == "/home/user"
    isolated = sandbox.run_code("x", context=context)
    assert isolated.error is not None
    assert isolated.error.name == "NameError"
    sandbox.run_code("y = 7", context=context)
    assert sandbox.run_code("y", context=context.id).text == "7"
    assert fake_rayd.code.execute_requests[-1].context_id == context.id
    ids = [item.id for item in sandbox.list_code_contexts()]
    assert ids[0] == "default"
    assert context.id in ids

    other = sandbox.create_code_context(cwd="/tmp", envs={"M4_ENV": "1"})
    assert other.cwd == "/tmp"
    assert dict(fake_rayd.code.create_requests[-1].envs) == {"M4_ENV": "1"}

    sandbox.restart_code_context(context)
    assert fake_rayd.code.restart_requests == [context.id]
    restarted = sandbox.run_code("y", context=context)
    assert restarted.error is not None
    assert restarted.error.name == "NameError"

    sandbox.remove_code_context(context)
    assert context.id not in [item.id for item in sandbox.list_code_contexts()]
    with pytest.raises(NotFoundException):
        sandbox.run_code("1", context=context)
    with pytest.raises(InvalidArgumentException):
        sandbox.remove_code_context("default")
    with pytest.raises(NotFoundException):
        sandbox.remove_code_context("ctx-000000000000")
    with pytest.raises(NotFoundException):
        sandbox.restart_code_context("ctx-000000000000")
    sandbox.remove_code_context(other.id)
    assert sandbox.run_code("x").text == "42"


def test_context_limits_and_arguments(sandbox: Sandbox, fake_rayd: RaydEndpoint) -> None:
    for _ in range(MAX_CONTEXTS - 1):
        sandbox.create_code_context()
    with pytest.raises(RateLimitException):
        sandbox.create_code_context()
    with pytest.raises(InvalidArgumentException, match="language"):
        sandbox.create_code_context(language="ruby")
    with pytest.raises(InvalidArgumentException, match="cwd"):
        sandbox.create_code_context(cwd="relative")
    with pytest.raises(InvalidArgumentException, match="cwd"):
        sandbox.create_code_context(cwd="/does/not/exist")
    with pytest.raises(InvalidArgumentException):
        sandbox.run_code("x", context="")
    with pytest.raises(InvalidArgumentException):
        sandbox.remove_code_context("")
    with pytest.raises(InvalidArgumentException, match="1 MiB"):
        sandbox.run_code("a" * (1_048_576 + 1))
    with pytest.raises(InvalidArgumentException):
        sandbox.run_code("x", timeout=-1)
    assert len(fake_rayd.code.execute_requests) == 0


def test_execute_uses_the_unary_channel_and_never_opens_the_stream_one(
    sandbox: Sandbox, fake_rayd: RaydEndpoint
) -> None:
    for _ in range(10):
        assert sandbox.run_code("1+1").text == "2"
    sandbox.list_code_contexts()
    assert len(fake_rayd.code.peers) == 1
    assert sandbox._stream_channel is None


def test_client_deadline_is_a_timeout_exception(sandbox: Sandbox, fake_rayd: RaydEndpoint) -> None:
    with pytest.raises(TimeoutException):
        sandbox.run_code("sleep", timeout=None, request_timeout=0.2)
    assert wait_until(lambda: fake_rayd.code.executions[-1].interrupted, INTERRUPT_BUDGET_SECONDS)


def test_kernel_gate_is_a_sandbox_exception_without_probing_health(
    sandbox: Sandbox, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.code.kernel_gate = "sidecar relaunching"
    health_calls = len(fake_rayd.servicer.health_calls)
    with pytest.raises(SandboxException, match="kernel no está listo") as excinfo:
        sandbox.run_code("x")
    assert not isinstance(excinfo.value, SandboxStateException)
    assert excinfo.value.grpc_code is grpc.StatusCode.UNAVAILABLE
    with pytest.raises(SandboxException, match="sidecar relaunching"):
        sandbox.create_code_context()
    assert len(fake_rayd.servicer.health_calls) == health_calls
    fake_rayd.code.kernel_gate = None
    assert sandbox.run_code("1+1").text == "2"


def test_phase_gate_is_a_state_error(sandbox: Sandbox, fake_rayd: RaydEndpoint) -> None:
    fake_rayd.code.phase = "suspending"
    with pytest.raises(SandboxStateException, match="suspending"):
        sandbox.run_code("x")
    fake_rayd.code.phase = None


def test_proxy_403_on_execute_remints_once_before_the_first_message(
    sandbox: Sandbox,
    fake_rayd: RaydEndpoint,
    control_plane: StubbedControlPlane,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_remint(control_plane, "jwe-2")
    monkeypatch.setattr(
        sandbox._code,
        "Execute",
        start_failing_first(sandbox._code.Execute, [lambda: failing_stream(proxy_forbidden())]),
    )
    assert sandbox.run_code("1+1").text == "2"
    assert len(fake_rayd.code.executions) == 1
    assert fake_rayd.code.execute_metadata[-1][PROXY_AUTH_KEY] == "jwe-2"
    control_plane.microvms.assert_no_pending_responses()


def test_double_proxy_403_on_execute_surfaces_proxy_rejected(
    sandbox: Sandbox, control_plane: StubbedControlPlane, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_remint(control_plane, "jwe-2")
    monkeypatch.setattr(
        sandbox._code,
        "Execute",
        start_failing_first(
            sandbox._code.Execute,
            [lambda: failing_stream(proxy_forbidden()), lambda: failing_stream(proxy_forbidden())],
        ),
    )
    with pytest.raises(AuthenticationException) as excinfo:
        sandbox.run_code("1+1")
    assert excinfo.value.proxy_rejected is True
    control_plane.microvms.assert_no_pending_responses()


def test_stream_reset_after_started_maps_the_microvm_state(
    sandbox: Sandbox,
    fake_rayd: RaydEndpoint,
    control_plane: StubbedControlPlane,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset = FakeRpcError(grpc.StatusCode.UNAVAILABLE, details="Socket closed")
    real_execute = sandbox._code.Execute

    def resetting_events(request: Any, timeout: float | None) -> Iterator[Any]:
        yield next(real_execute(request, timeout=timeout))
        raise reset

    monkeypatch.setattr(
        sandbox._code,
        "Execute",
        lambda request, timeout=None: ScriptedCall(resetting_events(request, timeout)),
    )
    monkeypatch.setattr(ReconnectPoll, "STATE_CHECK_INTERVAL", 0.1)
    fake_rayd.servicer.unavailable_calls = 10_000
    control_plane.microvms.add_response(
        "get_microvm",
        microvm_response(endpoint=fake_rayd.host, state="TERMINATED", state_reason="Success."),
    )
    with pytest.raises(SandboxNotFoundException, match="TERMINATED"):
        sandbox.run_code("1+1")
    assert fake_rayd.code.reattach_requests == []


def test_stream_ending_without_end_is_a_protocol_error(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_execute = sandbox._code.Execute
    calls: list[ScriptedCall] = []

    def truncated_events(request: Any, timeout: float | None) -> Iterator[Any]:
        yield next(real_execute(request, timeout=timeout))

    def truncated(request: Any, timeout: float | None = None) -> ScriptedCall:
        calls.append(ScriptedCall(truncated_events(request, timeout)))
        return calls[-1]

    monkeypatch.setattr(sandbox._code, "Execute", truncated)
    with pytest.raises(SandboxException, match="ExecutionEnd"):
        sandbox.run_code("1+1")
    assert calls[0].cancelled is True


def test_create_waits_for_kernel_ready(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.kernel_not_ready_calls = 2
    stub_launch(control_plane, fake_rayd)
    control_plane.microvms.add_response(
        "terminate_microvm", {}, expected_params={"microvmIdentifier": SANDBOX_ID}
    )
    with Sandbox.create(
        IMAGE_ARN,
        idle=None,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
        ready_timeout=20,
    ) as sandbox:
        assert sandbox.run_code("1+1").text == "2"
    assert len(fake_rayd.servicer.health_calls) == 3
    assert fake_rayd.servicer.kernel_not_ready_calls == 0


def test_connect_with_a_wrong_token_is_unauthenticated_on_code_rpcs(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    from rayito._payload import generate_access_token

    control_plane.microvms.add_response(
        "get_microvm", microvm_response(endpoint=fake_rayd.host, state="RUNNING")
    )
    control_plane.microvms.add_response("create_microvm_auth_token", auth_token_response())
    other = Sandbox.connect(
        SANDBOX_ID,
        access_token=generate_access_token(),
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    )
    try:
        with pytest.raises(AuthenticationException) as excinfo:
            other.run_code("1+1")
        assert excinfo.value.grpc_code is grpc.StatusCode.UNAUTHENTICATED
        with pytest.raises(AuthenticationException):
            other.list_code_contexts()
    finally:
        other.close()


# ------------------------------------------------------------ M7: languages


def test_bash_cell_routes_to_the_language_default(
    sandbox: Sandbox, fake_rayd: RaydEndpoint
) -> None:
    execution = sandbox.run_code("echo hi", language="bash")
    assert "".join(execution.logs.stdout) == "hi\n"
    assert execution.error is None
    request = fake_rayd.code.execute_requests[-1]
    assert request.language == "bash"
    assert not request.HasField("context_id")
    assert fake_rayd.code.executions[-1].context_id == "default-bash"
    assert fake_rayd.code.lazy_contexts == ["default-bash"]
    listed = [(item.id, item.language) for item in sandbox.list_code_contexts()]
    assert listed[0] == ("default", "python")
    assert ("default-bash", "bash") in listed
    sandbox.run_code("echo again", language="Bash")
    assert fake_rayd.code.lazy_contexts == ["default-bash"]
    assert sandbox.run_code("1 + 1", language="js").text == "2"
    assert fake_rayd.code.executions[-1].context_id == "default-javascript"
    sandbox.run_code("x = 42", language="python")
    assert fake_rayd.code.executions[-1].context_id == "default"
    assert fake_rayd.code.execute_requests[-1].language == "python"


def test_language_normalisation_and_exclusivity(sandbox: Sandbox, fake_rayd: RaydEndpoint) -> None:
    sandbox.run_code("echo 1", language="JS")
    assert fake_rayd.code.execute_requests[-1].language == "javascript"
    requests = len(fake_rayd.code.execute_requests)
    with pytest.raises(InvalidArgumentException, match="language"):
        sandbox.run_code("echo 1", language="r")
    with pytest.raises(InvalidArgumentException, match="excluyentes"):
        sandbox.run_code("echo 1", language="bash", context="default")
    assert len(fake_rayd.code.execute_requests) == requests


def test_python_cells_send_no_language(sandbox: Sandbox, fake_rayd: RaydEndpoint) -> None:
    sandbox.run_code("x")
    sandbox.run_code("x", language=None)
    sandbox.run_code("x", language="")
    assert all(not request.HasField("language") for request in fake_rayd.code.execute_requests)


def test_language_not_shipped_is_unimplemented(sandbox: Sandbox, fake_rayd: RaydEndpoint) -> None:
    fake_rayd.code.languages = frozenset({"python"})
    with pytest.raises(InvalidArgumentException) as excinfo:
        sandbox.run_code("echo hi", language="bash")
    assert excinfo.value.grpc_code is grpc.StatusCode.UNIMPLEMENTED
    assert "rayito-base-poly" in str(excinfo.value)
    assert fake_rayd.code.lazy_contexts == []
    with pytest.raises(InvalidArgumentException) as created:
        sandbox.create_code_context(language="javascript")
    assert created.value.grpc_code is grpc.StatusCode.UNIMPLEMENTED
    assert [item.id for item in sandbox.list_code_contexts()] == ["default"]


def test_envs_per_execution_are_python_only(sandbox: Sandbox, fake_rayd: RaydEndpoint) -> None:
    sandbox.run_code("echo hi", language="bash")
    executions = len(fake_rayd.code.executions)
    with pytest.raises(InvalidArgumentException, match="python"):
        sandbox.run_code("echo $A", language="bash", envs={"A": "1"})
    assert len(fake_rayd.code.executions) == executions
    assert sandbox.run_code("envs-echo", envs={"A": "1"}).logs.stdout == ['{"A": "1"}\n']


def test_create_code_context_with_a_language(sandbox: Sandbox, fake_rayd: RaydEndpoint) -> None:
    context = sandbox.create_code_context(language="bash")
    assert context.language == "bash"
    assert fake_rayd.code.create_requests[-1].language == "bash"
    assert "".join(sandbox.run_code("echo ctx", context=context).logs.stdout) == "ctx\n"
    assert fake_rayd.code.executions[-1].context_id == context.id
    javascript = sandbox.create_code_context(language="js")
    assert javascript.language == "javascript"
