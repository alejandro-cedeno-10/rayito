"""M2 "procesos": `ProcessService` completo a través del proxy de AWS, con la
postura de seguridad final del spawn (uid 1000, entorno desde cero, rlimits,
grupos de procesos, timeout del servidor), `get_host` con token por puerto,
`Metrics` real y el presupuesto de conexiones. Un único MicroVM (≈ 3 min).

Las 20 aserciones siguen `openspec/changes/m2-processes-lifecycle/design.md`
"Acceptance test list", en el mismo orden; cada bloque es una función para
que un fallo diga qué contrato se rompió."""

from __future__ import annotations

import asyncio
import time
import urllib.error
import urllib.request
from collections.abc import Callable

import pytest

from rayito import AsyncSandbox, Sandbox
from rayito._aws import LambdaMicrovmsControlPlane
from rayito._limits import TERMINAL_STATES
from rayito._payload import generate_access_token
from rayito.exceptions import (
    AuthenticationException,
    CommandExitException,
    InvalidArgumentException,
    NotFoundException,
    TimeoutException,
)

from .test_m1_hello import wait_for_terminal_state

TIMEOUT_LATENCY_BUDGET_SECONDS = 4.0
LARGE_OUTPUT_BYTES = 3_000_000
KEEPALIVE_SLEEP_SECONDS = 35
HOST_READY_TIMEOUT_SECONDS = 10.0
HOST_POLL_INTERVAL_SECONDS = 0.5
HTTP_TIMEOUT_SECONDS = 10
SEQUENTIAL_COMMANDS = 30
SEQUENTIAL_BUDGET_SECONDS = 45.0
METRICS_CLOCK_SKEW_SECONDS = 60.0
NOFILE_TARGET = "4096"
NOFILE_PLATFORM_HARD_LIMIT = "1024"


def report(label: str, seconds: float) -> None:
    print(f"\n[m2] {label}: {seconds:.2f} s", flush=True)


def timed(label: str, action: Callable[[], object]) -> float:
    started = time.perf_counter()
    action()
    elapsed = time.perf_counter() - started
    report(label, elapsed)
    return elapsed


def live_pids(sandbox: Sandbox) -> set[int]:
    return {info.pid for info in sandbox.commands.list()}


def http_status(url: str, headers: dict[str, str]) -> int:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return int(response.status)
    except urllib.error.HTTPError as error:
        return int(error.code)


def wait_for_http(url: str, headers: dict[str, str]) -> int:
    deadline = time.monotonic() + HOST_READY_TIMEOUT_SECONDS
    status = 0
    while time.monotonic() < deadline:
        try:
            status = http_status(url, headers)
        except (urllib.error.URLError, TimeoutError, OSError):
            status = 0
        if status == 200:
            return status
        time.sleep(HOST_POLL_INTERVAL_SECONDS)
    return status


def check_basic_output(sandbox: Sandbox) -> None:
    result = sandbox.commands.run("echo hola")
    assert result.stdout.strip() == "hola"
    assert result.stderr == ""
    assert result.exit_code == 0
    assert result.error is None


def check_identity(sandbox: Sandbox) -> None:
    assert sandbox.commands.run("whoami").stdout.strip() == "user"
    assert sandbox.commands.run("id -u").stdout.strip() == "1000"
    assert "1000" in sandbox.commands.run("id -G").stdout.split()


def check_environment_from_scratch(sandbox: Sandbox) -> None:
    lines = sandbox.commands.run("env").stdout.splitlines()
    assert "HOME=/home/user" in lines
    assert "USER=user" in lines
    assert "LOGNAME=user" in lines
    assert any(line.startswith("PATH=") for line in lines)
    assert not any(line.startswith("RAYD_LOG=") for line in lines)
    assert not any(line.startswith("AWS_LAMBDA_MICROVM_IMAGE_ARN=") for line in lines)
    assert sandbox.commands.run("pwd").stdout.strip() == "/home/user"


def check_envs_and_cwd(sandbox: Sandbox) -> None:
    assert sandbox.commands.run("echo $FOO", envs={"FOO": "bar"}).stdout.strip() == "bar"
    assert sandbox.commands.run("pwd", cwd="/tmp").stdout.strip() == "/tmp"
    with pytest.raises(InvalidArgumentException):
        sandbox.commands.run("true", cwd="/does/not/exist")


def check_resource_limits(sandbox: Sandbox) -> None:
    """`RLIMIT_NOFILE` pide 4096 pero el root del MicroVM no tiene
    `CAP_SYS_RESOURCE` y hereda hard=1024 (medido 2026-09-15, AWS_API_NOTES.md
    §9): `rayd` lo recorta al hard heredado y soft == hard."""
    lines = sandbox.commands.run("ulimit -Sn; ulimit -Hn; ulimit -u; ulimit -c").stdout.split()
    nofile_soft, nofile_hard, nproc, core = lines
    assert nofile_soft == nofile_hard
    assert nofile_hard in {NOFILE_PLATFORM_HARD_LIMIT, NOFILE_TARGET}
    assert (nproc, core) == ("512", "0")
    print(f"\n[m2] RLIMIT_NOFILE efectivo: {nofile_hard} (objetivo {NOFILE_TARGET})", flush=True)


def check_non_zero_exit(sandbox: Sandbox) -> None:
    with pytest.raises(CommandExitException) as excinfo:
        sandbox.commands.run("echo err >&2; exit 3")
    assert excinfo.value.exit_code == 3
    assert excinfo.value.stderr.strip() == "err"
    assert excinfo.value.error == "exited"


def check_server_timeout(sandbox: Sandbox) -> None:
    started = time.perf_counter()
    with pytest.raises(TimeoutException):
        sandbox.commands.run("sleep 10", timeout=2)
    elapsed = time.perf_counter() - started
    report("timeout=2 sobre sleep 10", elapsed)
    assert elapsed <= TIMEOUT_LATENCY_BUDGET_SECONDS
    handle = sandbox.commands.run("sleep 10", background=True, timeout=2)
    with pytest.raises(TimeoutException):
        handle.wait()
    assert handle.pid not in live_pids(sandbox)


def check_background_list_and_kill(sandbox: Sandbox) -> None:
    handle = sandbox.commands.run("sleep 30", background=True, tag="m2")
    assert any(
        info.pid == handle.pid and info.kind == "process" and info.tag == "m2"
        for info in sandbox.commands.list()
    )
    assert sandbox.commands.kill(handle.pid) is True
    with pytest.raises(CommandExitException) as excinfo:
        handle.wait()
    assert excinfo.value.exit_code == 137
    assert excinfo.value.error == "signaled"
    assert sandbox.commands.kill(handle.pid) is False


def check_stdin(sandbox: Sandbox) -> None:
    handle = sandbox.commands.run("cat", background=True, stdin=True)
    handle.send_stdin("hola\n")
    handle.close_stdin()
    assert handle.wait().stdout == "hola\n"
    without_stdin = sandbox.commands.run("sleep 30", background=True)
    with pytest.raises(InvalidArgumentException):
        without_stdin.send_stdin("x")
    assert without_stdin.kill() is True


def check_connect_replay(sandbox: Sandbox) -> None:
    handle = sandbox.commands.run("for i in 1 2 3; do echo $i; sleep 1; done", background=True)
    full = sandbox.commands.connect(handle.pid, from_seq=1)
    assert full.wait().stdout == "1\n2\n3\n"
    assert handle.wait().stdout == "1\n2\n3\n"
    retained = sandbox.commands.connect(handle.pid)
    result = retained.wait()
    assert result.exit_code == 0
    assert result.stdout == ""
    with pytest.raises(NotFoundException):
        sandbox.commands.connect(999_999)


def check_callbacks(sandbox: Sandbox) -> None:
    out: list[str] = []
    err: list[str] = []
    sandbox.commands.run("echo a; echo b >&2", on_stdout=out.append, on_stderr=err.append)
    assert "".join(out) == "a\n"
    assert "".join(err) == "b\n"


def check_large_output(sandbox: Sandbox) -> None:
    started = time.perf_counter()
    result = sandbox.commands.run(f"head -c {LARGE_OUTPUT_BYTES} /dev/zero | tr '\\0' a")
    report(f"{LARGE_OUTPUT_BYTES} bytes de stdout", time.perf_counter() - started)
    assert len(result.stdout) == LARGE_OUTPUT_BYTES


def check_keepalive_through_the_proxy(sandbox: Sandbox) -> None:
    result = sandbox.commands.run(f"sleep {KEEPALIVE_SLEEP_SECONDS}", timeout=None)
    assert result.exit_code == 0


def check_root_policy(sandbox: Sandbox) -> None:
    with pytest.raises(AuthenticationException) as excinfo:
        sandbox.commands.run("whoami", user="root")
    assert excinfo.value.proxy_rejected is False


def check_get_host(sandbox: Sandbox) -> None:
    server = sandbox.commands.run(
        "python3 -m http.server 3000 --bind 0.0.0.0", background=True, timeout=None
    )
    try:
        host = sandbox.get_host(3000)
        assert wait_for_http(host.url, host.headers) == 200
        assert http_status(host.url, {}) == 403
    finally:
        assert server.kill() is True


def check_metrics(sandbox: Sandbox) -> None:
    metrics = sandbox.get_metrics()
    assert metrics.cpu_count >= 1
    assert metrics.mem_total_bytes > 0
    assert metrics.mem_used_bytes <= metrics.mem_total_bytes
    assert metrics.disk_total_bytes > 0
    assert 0 <= metrics.cpu_used_pct <= 100
    assert abs(time.time() - metrics.timestamp.timestamp()) < METRICS_CLOCK_SKEW_SECONDS


def check_connection_budget(sandbox: Sandbox) -> None:
    def run_all() -> None:
        for index in range(SEQUENTIAL_COMMANDS):
            assert sandbox.commands.run(f"echo {index}").stdout.strip() == str(index)

    elapsed = timed(f"{SEQUENTIAL_COMMANDS} comandos secuenciales", run_all)
    assert elapsed <= SEQUENTIAL_BUDGET_SECONDS
    assert sandbox.commands.list() == []


def check_cross_process_connect(
    sandbox: Sandbox, control_plane: LambdaMicrovmsControlPlane
) -> None:
    reconnected = Sandbox.connect(
        sandbox.sandbox_id, access_token=sandbox.access_token, control_plane=control_plane
    )
    try:
        assert reconnected.commands.run("echo hola").stdout.strip() == "hola"
    finally:
        reconnected.close()
    impostor = Sandbox.connect(
        sandbox.sandbox_id, access_token=generate_access_token(), control_plane=control_plane
    )
    try:
        with pytest.raises(AuthenticationException):
            impostor.commands.run("true")
    finally:
        impostor.close()


async def async_parity(sandbox_id: str, access_token: str, region: str) -> None:
    sandbox = await AsyncSandbox.connect(sandbox_id, access_token=access_token, region=region)
    try:
        result = await sandbox.commands.run("echo async")
        assert result.stdout.strip() == "async"
        handle = await sandbox.commands.run("sleep 5", background=True)
        assert await handle.kill() is True
        with pytest.raises(CommandExitException):
            await handle.wait()
    finally:
        await sandbox.close()


def check_async_parity(sandbox: Sandbox) -> None:
    asyncio.run(async_parity(sandbox.sandbox_id, sandbox.access_token, sandbox.region))


@pytest.mark.e2e
def test_m2_processes(sandbox: Sandbox, control_plane: LambdaMicrovmsControlPlane) -> None:
    check_basic_output(sandbox)
    check_identity(sandbox)
    check_environment_from_scratch(sandbox)
    check_envs_and_cwd(sandbox)
    check_resource_limits(sandbox)
    check_non_zero_exit(sandbox)
    check_server_timeout(sandbox)
    check_background_list_and_kill(sandbox)
    check_stdin(sandbox)
    check_connect_replay(sandbox)
    check_callbacks(sandbox)
    check_large_output(sandbox)
    check_keepalive_through_the_proxy(sandbox)
    check_root_policy(sandbox)
    check_get_host(sandbox)
    check_metrics(sandbox)
    check_connection_budget(sandbox)
    check_cross_process_connect(sandbox, control_plane)
    check_async_parity(sandbox)

    assert sandbox.kill() is True
    final, seconds = wait_for_terminal_state(sandbox.sandbox_id, control_plane)
    report(f"terminate-microvm -> {final.state}", seconds)
    assert final.state in TERMINAL_STATES
