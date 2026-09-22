"""M4 "ejecución de código": `CodeService` completo a través del proxy de AWS
con el kernel sidecar real (ipykernel como `user`), `Health.kernel_ready`
tras la rotación del kernel en `/run`, matplotlib/pandas con sus mime types
enriquecidos, timeout del servidor, contextos aislados, muerte del kernel y
la unicidad del PRNG entre dos MicroVMs. Un MicroVM de ≈ 4 min y otro de
≈ 1 min.

Los 19 bloques siguen `openspec/changes/m4-code-execution/design.md`
"Acceptance test list", en el mismo orden; cada bloque es una función para
que un fallo diga qué contrato se rompió."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import statistics
import time
from collections.abc import Callable

import pytest

from rayito import (
    AsyncSandbox,
    ChartType,
    CodeContext,
    ExecutionError,
    LineChart,
    OutputMessage,
    Sandbox,
)
from rayito._aws import LambdaMicrovmsControlPlane
from rayito._limits import TERMINAL_STATES
from rayito.exceptions import InvalidArgumentException, NotFoundException, SandboxNotFoundException

from .conftest import BootTimings, E2ESettings, create_test_sandbox
from .test_m1_hello import wait_for_terminal_state

KERNEL_READY_BUDGET_SECONDS = 15.0
FIRST_CELL_BUDGET_SECONDS = 2.0
MATPLOTLIB_BUDGET_SECONDS = 5.0
PANDAS_BUDGET_SECONDS = 3.0
CONTEXT_BUDGET_SECONDS = 8.0
TIMEOUT_LATENCY_BUDGET_SECONDS = 8.0
KERNEL_DEATH_RECOVERY_BUDGET_SECONDS = 15.0
SEQUENTIAL_CELLS = 20
SEQUENTIAL_BUDGET_SECONDS = 15.0
SILENT_CELL_SECONDS = 12
SILENT_CELL_TIMEOUT_SECONDS = 60
HEALTH_PROBE_TIMEOUT_SECONDS = 5.0
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
RANDOM_CELL = "import random; [random.random() for _ in range(3)]"
NUMPY_RANDOM_CELL = "import numpy as np; np.random.default_rng().random(3).tolist()"


def report(label: str, seconds: float) -> None:
    print(f"\n[m4] {label}: {seconds:.2f} s", flush=True)


def timed(label: str, action: Callable[[], object]) -> float:
    started = time.perf_counter()
    action()
    elapsed = time.perf_counter() - started
    report(label, elapsed)
    return elapsed


def context_ids(sandbox: Sandbox) -> list[str]:
    return [context.id for context in sandbox.list_code_contexts()]


def check_readiness(sandbox: Sandbox, boot_timings: BootTimings) -> None:
    assert sandbox.is_running() is True
    health = sandbox._probe_health(HEALTH_PROBE_TIMEOUT_SECONDS)
    assert health is not None
    assert health.kernel_ready is True
    assert health.kernel_state_lost is False
    kernel_ready_s = boot_timings[sandbox.sandbox_id]
    report("run-microvm -> kernel_ready (kernel_ready_s)", kernel_ready_s)
    assert kernel_ready_s <= KERNEL_READY_BUDGET_SECONDS


def check_first_cell(sandbox: Sandbox) -> None:
    started = time.perf_counter()
    execution = sandbox.run_code("x = 42")
    elapsed = time.perf_counter() - started
    report("primera celda (x = 42)", elapsed)
    assert elapsed <= FIRST_CELL_BUDGET_SECONDS
    assert execution.results == []
    assert execution.error is None
    assert execution.execution_count == 1
    assert execution.text is None


def check_execute_result(sandbox: Sandbox) -> None:
    execution = sandbox.run_code("x")
    assert execution.text == "42"
    assert execution.results[0].is_main_result is True
    assert execution.results[0].formats() == ["text"]
    assert execution.execution_count == 2


def check_streams(sandbox: Sandbox) -> None:
    printed = sandbox.run_code("print(x)")
    assert "42" in "".join(printed.logs.stdout)
    assert printed.text is None
    assert printed.results == []
    errored = sandbox.run_code("import sys; print('e', file=sys.stderr)")
    assert "e" in "".join(errored.logs.stderr)
    assert errored.logs.stdout == []


def check_callbacks(sandbox: Sandbox) -> None:
    seen: list[OutputMessage] = []
    sandbox.run_code("print('a'); print('b')", on_stdout=seen.append)
    joined = "".join(message.line for message in seen)
    assert "a" in joined
    assert "b" in joined
    for message in seen:
        assert isinstance(message, OutputMessage)
        assert message.error is False
        assert message.timestamp > 0


def check_matplotlib(sandbox: Sandbox) -> None:
    started = time.perf_counter()
    execution = sandbox.run_code("import matplotlib.pyplot as plt; plt.plot([1, 2, 3]); plt.show()")
    elapsed = time.perf_counter() - started
    report("celda matplotlib (plot + show)", elapsed)
    assert elapsed <= MATPLOTLIB_BUDGET_SECONDS
    assert execution.error is None
    result = execution.results[0]
    assert result.png is not None
    assert base64.b64decode(result.png)[: len(PNG_SIGNATURE)] == PNG_SIGNATURE
    assert result.chart is not None
    assert result.chart.type is ChartType.LINE
    assert isinstance(result.chart, LineChart)
    assert len(result.chart.elements[0].points) == 3
    assert result.is_main_result is False
    assert "png" in result.formats()
    assert "chart" in result.formats()


def check_pandas(sandbox: Sandbox) -> None:
    started = time.perf_counter()
    execution = sandbox.run_code(
        "import pandas as pd; df = pd.DataFrame({'a': [1, 2], 'b': [3.5, 4.5]}); df"
    )
    elapsed = time.perf_counter() - started
    report("celda pandas (DataFrame)", elapsed)
    assert elapsed <= PANDAS_BUDGET_SECONDS
    assert execution.text is not None
    assert "a" in execution.text
    assert "b" in execution.text
    result = execution.results[0]
    assert result.html is not None
    assert result.data == {"a": [1, 2], "b": [3.5, 4.5]}
    assert result.is_main_result is True
    assert sandbox.run_code("df.describe()").results[0].data is not None


def check_errors(sandbox: Sandbox) -> None:
    execution = sandbox.run_code("1/0")
    assert execution.error is not None
    assert execution.error.name == "ZeroDivisionError"
    assert "division by zero" in execution.error.value
    assert "ZeroDivisionError" in execution.error.traceback
    assert "\x1b[" not in execution.error.traceback
    assert execution.execution_count is not None
    assert execution.results == []
    errors: list[ExecutionError] = []
    sandbox.run_code("raise ValueError('boom')", on_error=errors.append)
    assert errors[0].name == "ValueError"


def check_timeout_interrupts_and_keeps_state(sandbox: Sandbox) -> None:
    started = time.perf_counter()
    execution = sandbox.run_code("import time; time.sleep(10)", timeout=2)
    elapsed = time.perf_counter() - started
    report("timeout=2 sobre time.sleep(10)", elapsed)
    assert elapsed <= TIMEOUT_LATENCY_BUDGET_SECONDS
    assert execution.error is not None
    assert execution.error.name == "ExecutionTimeout"
    assert "2000" in execution.error.value
    assert execution.execution_count is not None
    assert sandbox.run_code("x").text == "42"
    assert sandbox.run_code("1+1").text == "2"


def check_silent_cell_through_the_proxy(sandbox: Sandbox) -> None:
    started = time.perf_counter()
    execution = sandbox.run_code(
        f"import time; time.sleep({SILENT_CELL_SECONDS}); 'done'",
        timeout=SILENT_CELL_TIMEOUT_SECONDS,
    )
    report(f"celda silenciosa de {SILENT_CELL_SECONDS} s", time.perf_counter() - started)
    assert execution.error is None
    assert execution.text == "'done'"


def check_contexts(sandbox: Sandbox) -> tuple[CodeContext, CodeContext]:
    started = time.perf_counter()
    context = sandbox.create_code_context()
    elapsed = time.perf_counter() - started
    report("create_code_context", elapsed)
    assert elapsed <= CONTEXT_BUDGET_SECONDS
    assert context.id != "default"
    assert context.language == "python"
    assert context.cwd == "/home/user"
    isolated = sandbox.run_code("x", context=context)
    assert isolated.error is not None
    assert isolated.error.name == "NameError"
    sandbox.run_code("y = 7", context=context)
    assert sandbox.run_code("y", context=context.id).text == "7"
    ids = context_ids(sandbox)
    assert ids[0] == "default"
    assert context.id in ids
    other = sandbox.create_code_context(cwd="/tmp", envs={"M4_ENV": "1"})
    assert sandbox.run_code("import os; os.getcwd()", context=other).text == "'/tmp'"
    assert sandbox.run_code("import os; os.environ['M4_ENV']", context=other).text == "'1'"
    return context, other


def check_per_execution_envs(sandbox: Sandbox) -> None:
    cell = "import os; os.environ.get('M4_RUN', 'unset')"
    assert sandbox.run_code(cell, envs={"M4_RUN": "yes"}).text == "'yes'"
    assert sandbox.run_code(cell).text == "'unset'"


def check_restart_and_remove(sandbox: Sandbox, context: CodeContext, other: CodeContext) -> None:
    elapsed = timed("restart_code_context", lambda: sandbox.restart_code_context(context))
    assert elapsed <= CONTEXT_BUDGET_SECONDS
    restarted = sandbox.run_code("y", context=context)
    assert restarted.error is not None
    assert restarted.error.name == "NameError"
    sandbox.remove_code_context(context)
    assert context.id not in context_ids(sandbox)
    with pytest.raises(NotFoundException):
        sandbox.run_code("1", context=context)
    with pytest.raises(InvalidArgumentException):
        sandbox.remove_code_context("default")
    with pytest.raises(NotFoundException):
        sandbox.remove_code_context("ctx-000000000000")
    sandbox.remove_code_context(other)


def check_sequential_budget_and_identity(sandbox: Sandbox) -> None:
    durations: list[float] = []

    def run_all() -> None:
        for _ in range(SEQUENTIAL_CELLS):
            started = time.perf_counter()
            assert sandbox.run_code("1+1").text == "2"
            durations.append(time.perf_counter() - started)

    total = timed(f"{SEQUENTIAL_CELLS} celdas secuenciales", run_all)
    percentile_95 = statistics.quantiles(durations, n=20)[-1]
    report(f"p95 de {SEQUENTIAL_CELLS} celdas", percentile_95)
    assert total <= SEQUENTIAL_BUDGET_SECONDS
    assert sandbox.commands.run("id -u").stdout.strip() == "1000"
    owners = sandbox.commands.run("ps -o user= -C python3 | sort -u").stdout.split()
    assert owners == ["user"]


def check_kernel_death_converges(sandbox: Sandbox) -> None:
    died = sandbox.run_code("import os; os._exit(3)")
    assert died.error is not None
    assert died.error.name == "KernelDied"
    started = time.perf_counter()
    assert sandbox.run_code("1+1").text == "2"
    elapsed = time.perf_counter() - started
    report("primera celda tras KernelDied", elapsed)
    assert elapsed <= KERNEL_DEATH_RECOVERY_BUDGET_SECONDS
    lost = sandbox.run_code("x")
    assert lost.error is not None
    assert lost.error.name == "NameError"


def check_metrics(sandbox: Sandbox) -> None:
    assert sandbox.get_metrics().mem_used_bytes > 0


async def async_parity(sandbox_id: str, access_token: str, region: str) -> None:
    sandbox = await AsyncSandbox.connect(sandbox_id, access_token=access_token, region=region)
    try:
        await sandbox.run_code("x = 42")
        assert (await sandbox.run_code("x")).text == "42"
        context = await sandbox.create_code_context()
        assert (await sandbox.run_code("2*2", context=context)).text == "4"
        await sandbox.remove_code_context(context)
        failed = await sandbox.run_code("1/0")
        assert failed.error is not None
        assert failed.error.name == "ZeroDivisionError"
    finally:
        await sandbox.close()


def check_async_parity(sandbox: Sandbox) -> None:
    asyncio.run(async_parity(sandbox.sandbox_id, sandbox.access_token, sandbox.region))


def check_two_sandboxes_have_distinct_rng(
    sandbox: Sandbox,
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    template_arn: str,
    boot_timings: BootTimings,
) -> None:
    other = create_test_sandbox(e2e_settings, control_plane, template_arn, boot_timings)
    try:
        report("run-microvm -> kernel_ready del segundo sandbox", boot_timings[other.sandbox_id])
        assert boot_timings[other.sandbox_id] <= KERNEL_READY_BUDGET_SECONDS
        other_health = other._probe_health(HEALTH_PROBE_TIMEOUT_SECONDS)
        assert other_health is not None
        assert other_health.kernel_ready is True
        assert sandbox.run_code(RANDOM_CELL).text != other.run_code(RANDOM_CELL).text
        assert sandbox.run_code(NUMPY_RANDOM_CELL).text != other.run_code(NUMPY_RANDOM_CELL).text
    finally:
        with contextlib.suppress(SandboxNotFoundException):
            other.kill()


@pytest.mark.e2e
def test_m4_code(
    sandbox: Sandbox,
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    template_arn: str,
    boot_timings: BootTimings,
) -> None:
    check_readiness(sandbox, boot_timings)
    check_first_cell(sandbox)
    check_execute_result(sandbox)
    check_streams(sandbox)
    check_callbacks(sandbox)
    check_matplotlib(sandbox)
    check_pandas(sandbox)
    check_errors(sandbox)
    check_timeout_interrupts_and_keeps_state(sandbox)
    check_silent_cell_through_the_proxy(sandbox)
    context, other = check_contexts(sandbox)
    check_per_execution_envs(sandbox)
    check_restart_and_remove(sandbox, context, other)
    check_sequential_budget_and_identity(sandbox)
    check_kernel_death_converges(sandbox)
    check_metrics(sandbox)
    check_async_parity(sandbox)
    check_two_sandboxes_have_distinct_rng(
        sandbox, e2e_settings, control_plane, template_arn, boot_timings
    )

    assert sandbox.kill() is True
    final, seconds = wait_for_terminal_state(sandbox.sandbox_id, control_plane)
    report(f"terminate-microvm -> {final.state}", seconds)
    assert final.state in TERMINAL_STATES
