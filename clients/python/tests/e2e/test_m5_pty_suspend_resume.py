"""M5 "PTY y suspend/resume": `PtyService` completo a través del proxy de AWS
(shell de login sobre una terminal real, resize, kill, re-attach), el ciclo
`pause()`/`resume()` con el kernel, los procesos, la PTY y el watch vivos al
otro lado, los deadlines que excluyen el tiempo suspendido, `run_code`
continuado con `Reattach`, el auto-resume por inactividad y la paridad async.
Un MicroVM de ≈ 6 min y otro de ≈ 4 min; cuatro ciclos suspend/resume.

Los 15 bloques de `test_pty_suspend_resume` y el test de auto-resume siguen
`openspec/changes/m5-pty-suspend-resume/design.md` "Acceptance test list",
en el mismo orden; cada bloque es una función para que un fallo diga qué
contrato se rompió. Cada tiempo medido se imprime con su nombre de
`MILESTONES.md`/`AWS_API_NOTES.md` (Q37-Q41). `get-microvm` es eventualmente
consistente: tras un resume puede seguir en `PENDING` cuando `rayd` ya
respondió el `/resume` y `Health` trae la generación nueva (regresión de M9,
2026-09-24), así que `RUNNING` se sondea con plazo (§6)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import queue
import re
import threading
import time
from collections.abc import Callable, Iterator

import pytest

from rayito import (
    AsyncPtyHandle,
    AsyncSandbox,
    CommandHandle,
    Execution,
    IdlePolicy,
    PtyHandle,
    PtySize,
    Sandbox,
    WatchHandle,
)
from rayito._aws import LambdaMicrovmsControlPlane
from rayito.exceptions import (
    CommandExitException,
    InvalidArgumentException,
    SandboxNotFoundException,
    TimeoutException,
)

from .conftest import BootTimings, E2ESettings, create_test_sandbox
from .test_m1_hello import wait_for_terminal_state

M5_SANDBOX_TIMEOUT_SECONDS = 1800
M5_IDLE_SECONDS = 600
AUTO_RESUME_SANDBOX_TIMEOUT_SECONDS = 900
AUTO_RESUME_IDLE_SECONDS = 60
AUTO_RESUME_SUSPENDED_SECONDS = 600
PTY_CREATE_BUDGET_SECONDS = 5.0
PTY_ECHO_BUDGET_SECONDS = 5.0
PTY_READ_BUDGET_SECONDS = 10.0
PAUSE_BUDGET_SECONDS = 30.0
RESUME_BUDGET_SECONDS = 30.0
PAUSED_FOR_SECONDS = 30
REARM_COMMAND_TIMEOUT_SECONDS = 25
REARM_BUDGET_SECONDS = 30.0
RESUBSCRIBE_BUDGET_SECONDS = 10.0
KERNEL_ALIVE_BUDGET_SECONDS = 10.0
LIVE_HANDLE_BUDGET_SECONDS = 15.0
WATCH_EVENT_BUDGET_SECONDS = 10.0
REATTACH_BUDGET_SECONDS = 60.0
REATTACH_CELL_SECONDS = 25
REATTACH_PAUSE_AFTER_SECONDS = 3.0
REATTACH_PAUSED_FOR_SECONDS = 5.0
IDLE_SUSPEND_BUDGET_SECONDS = 240.0
IDLE_POLL_SECONDS = 5.0
AUTO_RESUME_BUDGET_SECONDS = 30.0
RUNNING_STATE_BUDGET_SECONDS = 30.0
TERMINATE_BUDGET_SECONDS = 60.0
CLOCK_OFFSET_TOLERANCE_MS = 5000
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
POLL_SECONDS = 0.2


def report(label: str, seconds: float) -> None:
    print(f"\n[m5] {label}: {seconds:.2f} s", flush=True)


def timed(label: str, action: Callable[[], object]) -> float:
    started = time.perf_counter()
    action()
    elapsed = time.perf_counter() - started
    report(label, elapsed)
    return elapsed


def wait_until(predicate: Callable[[], bool], budget: float, what: str) -> float:
    started = time.perf_counter()
    while not predicate():
        elapsed = time.perf_counter() - started
        assert elapsed < budget, f"{what} no ocurrió en {budget:g} s"
        time.sleep(POLL_SECONDS)
    return time.perf_counter() - started


def wait_running(sbx: Sandbox, label: str) -> None:
    running_s = wait_until(
        lambda: sbx.get_info().state == "RUNNING",
        RUNNING_STATE_BUDGET_SECONDS,
        "get-microvm RUNNING",
    )
    report(label, running_s)


Needle = bytes | re.Pattern[bytes]
PtyMessage = tuple[str | None, str | None, bytes | None]


def seen(buffer: bytes, needle: Needle) -> bool:
    return needle in buffer if isinstance(needle, bytes) else needle.search(buffer) is not None


def next_message(iterator: Iterator[PtyMessage], timeout: float) -> PtyMessage | None:
    """`next()` con plazo: un `PtyHandle` sin `timeout` bloquea hasta el
    siguiente mensaje y la PTY no manda nada más si la salida esperada no
    llega; el hilo daemon que queda bloqueado sólo sobrevive a un test ya
    fallido."""
    box: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=1)

    def pull() -> None:
        try:
            box.put(("message", next(iterator)))
        except BaseException as exc:
            box.put(("error", exc))

    threading.Thread(target=pull, name="m5-pty-read", daemon=True).start()
    try:
        kind, value = box.get(timeout=timeout)
    except queue.Empty:
        return None
    if kind == "error":
        assert isinstance(value, BaseException)
        raise value
    assert isinstance(value, tuple)
    return value


def read_until(
    handle: PtyHandle, needle: Needle, timeout: float = PTY_READ_BUDGET_SECONDS
) -> bytes:
    """Itera un `PtyHandle` acumulando los bytes hasta que aparece `needle`."""
    buffer = b""
    deadline = time.monotonic() + timeout
    iterator: Iterator[PtyMessage] = iter(handle)
    while not seen(buffer, needle):
        remaining = deadline - time.monotonic()
        assert remaining > 0, f"{needle!r} no llegó en {timeout:g} s: {buffer!r}"
        try:
            message = next_message(iterator, remaining)
        except StopIteration:
            raise AssertionError(f"la PTY terminó sin {needle!r}: {buffer!r}") from None
        assert message is not None, f"{needle!r} no llegó en {timeout:g} s: {buffer!r}"
        _, _, data = message
        if data is not None:
            buffer += data
    return buffer


def output_line(text: str) -> re.Pattern[bytes]:
    """Una línea de salida del shell, nunca el eco de la entrada: bash 5.2
    envuelve cada comando con el bracketed paste de readline, así que entre
    el eco `echo hola\\r\\n` y la salida va `\\x1b[?2004l\\r` (medido en
    `rayito-base` 10.0, Q37); sin bracketed paste la salida sigue a `\\n`."""
    return re.compile(rb"[\r\n]" + re.escape(text.encode()) + rb"\r\n")


class Collector:
    """Itera un `CommandHandle` en un hilo daemon acumulando stdout."""

    def __init__(self, handle: CommandHandle) -> None:
        self.handle = handle
        self.chunks: list[str] = []
        self.error: Exception | None = None
        self.thread = threading.Thread(target=self._run, name="m5-collector", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        try:
            for stdout, _, _ in self.handle:
                if stdout is not None:
                    self.chunks.append(stdout)
        except Exception as exc:
            self.error = exc


@pytest.fixture
def m5_sandbox(
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    template_arn: str,
    boot_timings: BootTimings,
) -> Iterator[Sandbox]:
    created = create_test_sandbox(
        e2e_settings,
        control_plane,
        template_arn,
        boot_timings,
        timeout=M5_SANDBOX_TIMEOUT_SECONDS,
        idle=IdlePolicy(max_idle_seconds=M5_IDLE_SECONDS, auto_resume=True),
    )
    try:
        yield created
    finally:
        with contextlib.suppress(SandboxNotFoundException):
            created.kill()


# ------------------------------------------------------------------ blocks


def check_platform(sbx: Sandbox) -> None:
    devpts = sbx.commands.run("test -c /dev/ptmx && grep -c devpts /proc/mounts").stdout.strip()
    report(f"Q37 devpts en el guest: /dev/ptmx presente, devpts mounts = {devpts!r}", 0.0)
    assert devpts != "0"
    opened = sbx.commands.run(
        "python3 -c 'import pty, os; m, s = pty.openpty(); print(os.ttyname(s))'"
    ).stdout.strip()
    assert opened.startswith("/dev/pts/"), opened


def check_csv_code_and_plot(sbx: Sandbox) -> None:
    sbx.files.write("/home/user/data.csv", "a,b\n1,3.5\n2,4.5\n")
    described = sbx.run_code(
        "import pandas as pd; df = pd.read_csv('/home/user/data.csv'); df.describe()"
    )
    assert described.error is None
    assert described.text is not None and "a" in described.text
    plotted = sbx.run_code("df.plot(); import matplotlib.pyplot as plt; plt.show()")
    assert plotted.error is None
    assert plotted.results[0].png is not None
    assert base64.b64decode(plotted.results[0].png)[: len(PNG_SIGNATURE)] == PNG_SIGNATURE
    printed = sbx.run_code("print(len(df))")
    assert "2" in "".join(printed.logs.stdout)
    assert printed.text is None


def check_state_before_pause(sbx: Sandbox) -> int:
    assert sbx.run_code("x = 42").error is None
    assert sbx.run_code("x").text == "42"
    health = sbx.get_health()
    assert health.agent_ready and health.kernel_ready
    assert health.kernel_state_lost is False
    return health.resume_generation


def check_pty(sbx: Sandbox) -> PtyHandle:
    chunks: list[bytes] = []
    started = time.perf_counter()
    pty = sbx.pty.create(size=PtySize(cols=100, rows=30), on_data=chunks.append, timeout=None)
    create_s = time.perf_counter() - started
    report("pty.create -> started (pty_create_s)", create_s)
    assert pty.pid > 0
    assert create_s <= PTY_CREATE_BUDGET_SECONDS
    started = time.perf_counter()
    sbx.pty.send_input(pty.pid, "echo hola\n")
    read_until(pty, output_line("hola"))
    echo_s = time.perf_counter() - started
    report("echo hola por la PTY (pty_echo_s)", echo_s)
    assert echo_s <= PTY_ECHO_BUDGET_SECONDS
    assert chunks and all(isinstance(chunk, bytes) for chunk in chunks)
    assert any(info.pid == pty.pid and info.kind == "pty" for info in sbx.commands.list())
    sbx.pty.send_input(pty.pid, "stty size\n")
    assert b"30 100" in read_until(pty, b"30 100")
    sbx.pty.resize(pty.pid, PtySize(cols=120, rows=40))
    sbx.pty.send_input(pty.pid, "stty size\n")
    assert b"40 120" in read_until(pty, b"40 120")
    sbx.pty.send_input(pty.pid, "id -u; tty\n")
    identity = read_until(pty, b"/dev/pts/")
    assert b"1000" in identity
    sbx.pty.send_input(pty.pid, "echo $TERM $LANG\n")
    read_until(pty, output_line("xterm-256color C.UTF-8"))
    with pytest.raises(InvalidArgumentException):
        sbx.commands.send_stdin(pty.pid, "x")
    assert pty.last_seq > 0
    assert "hola" in pty.stdout
    return pty


def check_handles_before_pause(
    sbx: Sandbox,
) -> tuple[CommandHandle, CommandHandle, WatchHandle, CommandHandle, Collector]:
    background = sbx.commands.run("sleep 4000", background=True, timeout=None)
    background.disconnect()
    rearm = sbx.commands.run("sleep 60", background=True, timeout=REARM_COMMAND_TIMEOUT_SECONDS)
    rearm.disconnect()
    watch = sbx.files.watch_dir("/home/user")
    live = sbx.commands.run(
        "for i in $(seq 1 300); do echo tick $i; sleep 1; done", background=True, timeout=None
    )
    ticks = Collector(live)
    wait_until(lambda: len(ticks.chunks) >= 2, LIVE_HANDLE_BUDGET_SECONDS, "dos ticks")
    return background, rearm, watch, live, ticks


def check_pause(sbx: Sandbox) -> None:
    started = time.perf_counter()
    assert sbx.pause() is True
    pause_s = time.perf_counter() - started
    report("pause() -> SUSPENDED (pause_s)", pause_s)
    assert pause_s <= PAUSE_BUDGET_SECONDS
    assert sbx.get_info().state == "SUSPENDED"
    assert sbx.pause() is False
    time.sleep(PAUSED_FOR_SECONDS)
    assert sbx.get_info().state == "SUSPENDED", "un handle, la PTY o el watch despertaron al VM"


def check_resume(sbx: Sandbox, generation_before: int) -> None:
    resume_s = timed("resume() -> Health con la generación nueva (resume_s)", sbx.resume)
    assert resume_s <= RESUME_BUDGET_SECONDS
    health = sbx.get_health()
    assert health.resume_generation == generation_before + 1
    assert health.kernel_state_lost is False
    report(f"Q41 clock_offset_ms = {health.clock_offset_ms}", 0.0)
    assert abs(health.clock_offset_ms) <= CLOCK_OFFSET_TOLERANCE_MS
    wait_running(sbx, "resume() -> get-microvm RUNNING (running_after_resume_s)")


def check_kernel_alive(sbx: Sandbox) -> None:
    started = time.perf_counter()
    assert sbx.run_code("x").text == "42"
    elapsed = time.perf_counter() - started
    report("primera celda tras resume (kernel_alive_s)", elapsed)
    assert elapsed <= KERNEL_ALIVE_BUDGET_SECONDS
    assert sbx.run_code("len(df)").text == "2"


def check_processes_alive_and_timeout_rearmed(
    sbx: Sandbox, background: CommandHandle, rearm: CommandHandle
) -> None:
    pids = [info.pid for info in sbx.commands.list()]
    assert background.pid in pids
    assert rearm.pid in pids
    started = time.perf_counter()
    reconnected = sbx.commands.connect(background.pid)
    resubscribe_s = time.perf_counter() - started
    report("commands.connect tras resume (resubscribe_s)", resubscribe_s)
    assert resubscribe_s <= RESUBSCRIBE_BUDGET_SECONDS
    assert reconnected.pid == background.pid
    assert sbx.commands.kill(background.pid) is True
    with pytest.raises(CommandExitException) as killed:
        reconnected.wait()
    assert killed.value.exit_code == 137
    started = time.perf_counter()
    with pytest.raises(TimeoutException):
        sbx.commands.connect(rearm.pid).wait()
    rearm_s = time.perf_counter() - started
    report("timeout re-armado tras resume (rearm_timeout_s)", rearm_s)
    assert rearm_s <= REARM_BUDGET_SECONDS
    assert rearm.pid not in [info.pid for info in sbx.commands.list()]


def check_pty_reattached(sbx: Sandbox, pty: PtyHandle) -> None:
    started = time.perf_counter()
    reattached = sbx.pty.connect(pty.pid, from_seq=pty.last_seq + 1)
    sbx.pty.send_input(reattached.pid, "echo resumed-$((6*7))\n")
    read_until(reattached, b"resumed-42")
    report("pty.connect + echo tras resume (pty_reattach_s)", time.perf_counter() - started)
    read_until(pty, b"resumed-42")
    assert pty.reconnects == 1
    assert sbx.pty.kill(pty.pid) is True
    with pytest.raises(CommandExitException) as killed:
        reattached.wait()
    assert killed.value.exit_code == 137
    assert sbx.pty.kill(pty.pid) is False


def check_live_handle(
    sbx: Sandbox, live: CommandHandle, ticks: Collector, ticks_at_pause: int
) -> None:
    grew_s = wait_until(
        lambda: len(ticks.chunks) > ticks_at_pause, LIVE_HANDLE_BUDGET_SECONDS, "ticks nuevos"
    )
    report("handle vivo: primer tick tras resume (live_handle_s)", grew_s)
    assert live.reconnects == 1
    assert ticks.error is None
    assert sbx.commands.kill(live.pid) is True


def check_watch_reissued(sbx: Sandbox, watch: WatchHandle) -> None:
    sbx.files.write("/home/user/after-resume.txt", "x")
    seen: list[str] = []

    def arrived() -> bool:
        seen.extend(event.name for event in watch.get_new_events())
        return "after-resume.txt" in seen

    watch_s = wait_until(arrived, WATCH_EVENT_BUDGET_SECONDS, "el evento del watch")
    report("watch re-emitido: evento tras resume (watch_reissue_s)", watch_s)
    assert watch.is_running is True
    assert watch.reconnects == 1
    watch.stop()


def check_run_code_across_a_pause(
    sbx: Sandbox, generation_before: int, caplog: pytest.LogCaptureFixture
) -> None:
    """`pause()` con una celda en curso: el hilo de `run_code` ve el final
    `suspending`, y como la pausa la pidió este `Sandbox` no sondea `Health`
    (con auto-resume la desharía): el VM llega a `SUSPENDED` y sigue así
    hasta `resume()`; entonces la celda continúa con `Reattach` (el log de
    `rayito.code` lo atestigua) y la generación avanza exactamente una vez."""
    results: list[Execution] = []
    errors: list[Exception] = []

    def run() -> None:
        try:
            results.append(
                sbx.run_code(
                    f"import time; time.sleep({REATTACH_CELL_SECONDS}); 'slept'", timeout=120
                )
            )
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=run, name="m5-run-code", daemon=True)
    with caplog.at_level(logging.INFO, logger="rayito.code"):
        thread.start()
        time.sleep(REATTACH_PAUSE_AFTER_SECONDS)
        assert sbx.pause() is True
        time.sleep(REATTACH_PAUSED_FOR_SECONDS)
        assert thread.is_alive()
        assert sbx.get_info().state == "SUSPENDED", "la celda en curso despertó al VM (Q40)"
        started = time.perf_counter()
        sbx.resume()
        thread.join(REATTACH_BUDGET_SECONDS)
    report("run_code continuado con Reattach (reattach_s)", time.perf_counter() - started)
    assert not thread.is_alive()
    assert errors == [], errors
    assert results[0].text == "'slept'"
    assert results[0].error is None
    reattach_records = [record for record in caplog.records if "Reattach" in record.message]
    assert len(reattach_records) == 1, [record.message for record in caplog.records]
    report(f"Q40 run_code tras pause(): {reattach_records[0].message}", 0.0)
    assert sbx.get_health().resume_generation == generation_before + 2
    assert sbx.commands.run("echo ok").stdout.strip() == "ok"


async def async_read_until(handle: AsyncPtyHandle, needle: Needle) -> bytes:
    buffer = b""
    deadline = time.monotonic() + PTY_READ_BUDGET_SECONDS
    iterator = handle.__aiter__()
    while not seen(buffer, needle):
        remaining = deadline - time.monotonic()
        assert remaining > 0, f"{needle!r} no llegó: {buffer!r}"
        try:
            _, _, data = await asyncio.wait_for(iterator.__anext__(), remaining)
        except StopAsyncIteration:
            raise AssertionError(f"la PTY terminó sin {needle!r}: {buffer!r}") from None
        except TimeoutError:
            raise AssertionError(f"{needle!r} no llegó: {buffer!r}") from None
        if data is not None:
            buffer += data
    return buffer


async def async_parity(sandbox_id: str, access_token: str, region: str, generation: int) -> None:
    sandbox = await AsyncSandbox.connect(sandbox_id, access_token=access_token, region=region)
    try:
        pty = await sandbox.pty.create(size=PtySize(cols=80, rows=24), timeout=None)
        await sandbox.pty.send_input(pty.pid, "echo async-pty\n")
        await async_read_until(pty, output_line("async-pty"))
        assert await sandbox.pty.kill(pty.pid) is True
        assert await sandbox.pause() is True
        started = time.perf_counter()
        await sandbox.resume()
        report("async pause/resume (async_resume_s)", time.perf_counter() - started)
        assert (await sandbox.run_code("x")).text == "42"
        assert (await sandbox.get_health()).resume_generation == generation + 3
    finally:
        await sandbox.close()


def check_async_parity(sbx: Sandbox, generation_before: int) -> None:
    asyncio.run(async_parity(sbx.sandbox_id, sbx.access_token, sbx.region, generation_before))


def check_teardown(
    sbx: Sandbox, control_plane: LambdaMicrovmsControlPlane, template_arn: str
) -> None:
    assert sbx.kill() is True
    final, seconds = wait_for_terminal_state(sbx.sandbox_id, control_plane)
    report(f"terminate-microvm -> {final.state}", seconds)
    deadline = time.monotonic() + TERMINATE_BUDGET_SECONDS
    while final.state != "TERMINATED":
        assert time.monotonic() < deadline, f"el sandbox sigue {final.state}"
        time.sleep(1.0)
        final = Sandbox.get_info(sbx.sandbox_id, read_metadata=False, control_plane=control_plane)
    listed = [
        item.sandbox_id for item in Sandbox.list(template=template_arn, control_plane=control_plane)
    ]
    assert sbx.sandbox_id not in listed


@pytest.mark.e2e
def test_pty_suspend_resume(
    m5_sandbox: Sandbox,
    control_plane: LambdaMicrovmsControlPlane,
    template_arn: str,
    boot_timings: BootTimings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sbx = m5_sandbox
    report("run-microvm -> kernel_ready (kernel_ready_s)", boot_timings[sbx.sandbox_id])
    check_platform(sbx)
    check_csv_code_and_plot(sbx)
    generation_before = check_state_before_pause(sbx)
    pty = check_pty(sbx)
    background, rearm, watch, live, ticks = check_handles_before_pause(sbx)
    ticks_at_pause = len(ticks.chunks)
    check_pause(sbx)
    check_resume(sbx, generation_before)
    check_kernel_alive(sbx)
    check_processes_alive_and_timeout_rearmed(sbx, background, rearm)
    check_pty_reattached(sbx, pty)
    check_live_handle(sbx, live, ticks, ticks_at_pause)
    check_watch_reissued(sbx, watch)
    check_run_code_across_a_pause(sbx, generation_before, caplog)
    check_async_parity(sbx, generation_before)
    check_teardown(sbx, control_plane, template_arn)


@pytest.mark.e2e
def test_auto_resume(
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    template_arn: str,
    boot_timings: BootTimings,
) -> None:
    sbx = create_test_sandbox(
        e2e_settings,
        control_plane,
        template_arn,
        boot_timings,
        timeout=AUTO_RESUME_SANDBOX_TIMEOUT_SECONDS,
        idle=IdlePolicy(
            max_idle_seconds=AUTO_RESUME_IDLE_SECONDS,
            suspended_duration_seconds=AUTO_RESUME_SUSPENDED_SECONDS,
            auto_resume=True,
        ),
    )
    try:
        assert sbx.run_code("y = 7").error is None
        assert sbx.commands.run("echo warm").stdout.strip() == "warm"
        started = time.perf_counter()
        while sbx.get_info().state != "SUSPENDED":
            assert time.perf_counter() - started < IDLE_SUSPEND_BUDGET_SECONDS, sbx.info.state
            time.sleep(IDLE_POLL_SECONDS)
        report("idle -> SUSPENDED (idle_suspend_s)", time.perf_counter() - started)
        started = time.perf_counter()
        assert sbx.commands.run("echo back").stdout.strip() == "back"
        auto_resume_s = time.perf_counter() - started
        report("primer comando tras el auto-resume (auto_resume_s; Q40)", auto_resume_s)
        assert auto_resume_s <= AUTO_RESUME_BUDGET_SECONDS
        assert sbx.run_code("y").text == "7"
        assert sbx.get_health().resume_generation == 1
        wait_running(sbx, "auto-resume -> get-microvm RUNNING (running_after_resume_s)")
        assert sbx.kill() is True
    finally:
        with contextlib.suppress(SandboxNotFoundException):
            sbx.kill()
