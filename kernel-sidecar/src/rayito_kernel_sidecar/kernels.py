"""One context = one kernel. ``ContextBase`` holds the rules that need no
kernel at all (the FIFO execution queue, interrupt of a queued cell, what a
restart or destroy does to in-flight cells, recovery after the kernel dies)
so they run on any host against a fake; ``KernelContext`` adds the real
``jupyter_client.AsyncKernelManager`` over ``ipc`` sockets (design D4) for
the kernel of the context's language (``languages.LANGUAGES``).
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

from rayito_kernel_sidecar.executions import (
    KIND_ABORT,
    KIND_DIED,
    KIND_IOPUB,
    KIND_SHELL,
    Emit,
    ExecutionOutcome,
    Inbox,
    run_execution,
    run_silent,
)
from rayito_kernel_sidecar.languages import LANGUAGES, PYTHON
from rayito_kernel_sidecar.logging import SidecarLogger
from rayito_kernel_sidecar.protocol import (
    SYNTHETIC_CONTEXT_DESTROYED,
    SYNTHETIC_KERNEL_RESTARTED,
    Event,
)

ContextState = Literal["starting", "ready", "restarting", "dead"]
ReseedPlan = Literal["idle", "busy", "unavailable"]
DiedCallback = Callable[[str, int | None, str | None], Awaitable[None]]

KERNEL_NAME: Final = LANGUAGES[PYTHON].kernel_name
KERNEL_READY_TIMEOUT_S: Final = 120.0
ABORT_DRAIN_TIMEOUT_S: Final = 10.0
RECOVERY_BACKOFF_MAX_S: Final = 30.0
INTERRUPTED: Final = "interrupted"

RESEED_CELL: Final = (
    "import random as _r; _r.seed()\n"
    "import sys as _s\n"
    'if "numpy" in _s.modules:\n'
    '    _s.modules["numpy"].random.seed()\n'
    "del _r, _s\n"
)


class ContextBusy(Exception):
    """A restart or destroy collided with another one on the same context."""


@dataclass
class ExecutionSlot:
    request_id: int
    execution_id: str
    turn: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    cancel_reason: str | None = None


class ContextBase(ABC):
    def __init__(
        self,
        context_id: str,
        language: str,
        cwd: str,
        envs: Mapping[str, str],
        logger: SidecarLogger,
        on_kernel_died: DiedCallback | None = None,
    ) -> None:
        self.context_id = context_id
        self._language = language
        self.cwd = cwd
        self.envs: dict[str, str] = dict(envs)
        self.state: ContextState = "starting"
        self.kernel_pid: int | None = None
        self._logger = logger
        self._on_kernel_died = on_kernel_died
        self._queue: deque[ExecutionSlot] = deque()
        self._running: ExecutionSlot | None = None
        self._ready = asyncio.Event()
        self._recovery: asyncio.Task[None] | None = None

    @property
    def language(self) -> str:
        return self._language

    @property
    def running_execution_id(self) -> str | None:
        return self._running.execution_id if self._running else None

    def queued_execution_ids(self) -> list[str]:
        return [slot.execution_id for slot in self._queue]

    async def start(self) -> int | None:
        started = time.monotonic()
        await self._start_kernel()
        self._mark_ready()
        self._logger.info(
            "kernel warm",
            context_id=self.context_id,
            kernel_pid=self.kernel_pid,
            warmup_ms=int((time.monotonic() - started) * 1000),
        )
        return self.kernel_pid

    async def wait_ready(self, timeout: float) -> bool:
        if self.state == "dead":
            return False
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
        except TimeoutError:
            return False
        return self.state == "ready"

    async def run(
        self,
        request_id: int,
        execution_id: str,
        code: str,
        envs: Mapping[str, str],
        emit: Emit,
    ) -> None:
        slot = ExecutionSlot(request_id=request_id, execution_id=execution_id)
        self._queue.append(slot)
        self._pump()
        await slot.turn.wait()
        try:
            if slot.cancel_reason is not None:
                await _emit_cancelled(slot, emit)
                return
            await self._run_cell(slot, code, envs, emit)
        finally:
            slot.done.set()
            if self._running is slot:
                self._running = None
                self._pump()

    async def interrupt(self, execution_id: str | None) -> None:
        running = self._running
        if running is not None and (execution_id is None or running.execution_id == execution_id):
            await self._interrupt_kernel()
            return
        for slot in list(self._queue):
            if slot.execution_id == execution_id:
                self._queue.remove(slot)
                slot.cancel_reason = INTERRUPTED
                slot.turn.set()
                return

    async def restart(self, envs: Mapping[str, str]) -> int | None:
        if self.state in ("restarting", "dead"):
            raise ContextBusy
        self._enter_restart()
        self.envs = dict(envs)
        await self._cancel_in_flight(SYNTHETIC_KERNEL_RESTARTED)
        await self._stop_kernel()
        await self._start_kernel()
        self._mark_ready()
        return self.kernel_pid

    async def destroy(self) -> None:
        if self.state in ("restarting", "dead"):
            raise ContextBusy
        self._enter_restart()
        await self._cancel_in_flight(SYNTHETIC_CONTEXT_DESTROYED)
        self.state = "dead"
        await self._stop_kernel()

    async def shutdown(self) -> None:
        if self.state != "dead":
            self.state = "dead"
            self._ready.clear()
            await self._stop_kernel()

    def reseed_plan(self) -> ReseedPlan:
        """How ``reseed`` should treat this context: ``idle`` (reseed inline,
        the cell takes milliseconds), ``busy`` (a cell is running or queued:
        defer behind it so the reply never waits for user code) or
        ``unavailable`` (restarting or dead)."""
        if self.state != "ready":
            return "unavailable"
        if self._running is not None or self._queue:
            return "busy"
        return "idle"

    async def reseed(self) -> bool:
        if self.state != "ready":
            return False
        outcome = await self.run_internal(RESEED_CELL)
        return outcome.ended_by == "kernel"

    async def probe(self, timeout: float) -> bool:
        if self.state != "ready":
            return False
        return await self._probe_kernel(timeout)

    async def run_internal(self, code: str) -> ExecutionOutcome:
        """A silent cell that takes its turn in the execution queue."""
        slot = ExecutionSlot(request_id=0, execution_id="")
        self._queue.append(slot)
        self._pump()
        await slot.turn.wait()
        try:
            if slot.cancel_reason is not None:
                return ExecutionOutcome(ended_by=slot.cancel_reason)
            return await self._run_silent_cell(code)
        finally:
            slot.done.set()
            if self._running is slot:
                self._running = None
                self._pump()

    def kernel_died(self, exit_code: int | None) -> None:
        """Called by the subclass when the kernel process went away outside a
        restart or destroy; the running cell already got the ``died``
        sentinel through the inbox."""
        if self.state in ("restarting", "dead") or self._recovery is not None:
            return
        execution_id = self.running_execution_id
        self.state = "restarting"
        self._ready.clear()
        self._recovery = asyncio.create_task(self._recover(exit_code, execution_id))

    def _enter_restart(self) -> None:
        self.state = "restarting"
        self._ready.clear()

    def _mark_ready(self) -> None:
        self.state = "ready"
        self._ready.set()
        self._pump()

    def _pump(self) -> None:
        if self._running is None and self._queue and self.state == "ready":
            slot = self._queue.popleft()
            self._running = slot
            slot.turn.set()

    async def _cancel_in_flight(self, reason: str) -> None:
        queued = list(self._queue)
        self._queue.clear()
        for slot in queued:
            slot.cancel_reason = reason
            slot.turn.set()
        running = self._running
        if running is not None:
            running.cancel_reason = reason
            await self._abort_running(reason)
            await _wait_done(running)
        for slot in queued:
            await _wait_done(slot)

    async def _recover(self, exit_code: int | None, execution_id: str | None) -> None:
        self._logger.warn(
            "kernel died",
            context_id=self.context_id,
            exit_code=exit_code,
            execution_id=execution_id,
        )
        running = self._running
        if running is not None:
            await _wait_done(running)
        try:
            await self._stop_kernel()
            if self._on_kernel_died is not None:
                await self._on_kernel_died(self.context_id, exit_code, execution_id)
            attempt = 0
            while self.state == "restarting":
                attempt += 1
                try:
                    await self._start_kernel()
                except Exception as error:
                    delay = min(0.5 * 2 ** (attempt - 1), RECOVERY_BACKOFF_MAX_S)
                    self._logger.error(
                        "kernel restart failed",
                        context_id=self.context_id,
                        attempt=attempt,
                        backoff_ms=int(delay * 1000),
                        reason=type(error).__name__,
                    )
                    await asyncio.sleep(delay)
                    continue
                self._mark_ready()
        finally:
            self._recovery = None

    @abstractmethod
    async def _start_kernel(self) -> None: ...

    @abstractmethod
    async def _stop_kernel(self) -> None: ...

    @abstractmethod
    async def _interrupt_kernel(self) -> None: ...

    @abstractmethod
    async def _abort_running(self, reason: str) -> None: ...

    @abstractmethod
    async def _run_cell(
        self, slot: ExecutionSlot, code: str, envs: Mapping[str, str], emit: Emit
    ) -> None: ...

    @abstractmethod
    async def _run_silent_cell(self, code: str) -> ExecutionOutcome: ...

    @abstractmethod
    async def _probe_kernel(self, timeout: float) -> bool: ...


async def _wait_done(slot: ExecutionSlot) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(slot.done.wait(), ABORT_DRAIN_TIMEOUT_S)


async def _emit_cancelled(slot: ExecutionSlot, emit: Emit) -> None:
    if slot.cancel_reason != INTERRUPTED:
        await emit(
            {
                "event": "error",
                "id": slot.request_id,
                "execution_id": slot.execution_id,
                "name": slot.cancel_reason,
                "value": "the execution was cancelled before it started",
                "traceback": [],
            }
        )
    await emit(
        {
            "event": "end",
            "id": slot.request_id,
            "execution_id": slot.execution_id,
            "execution_count": 0,
        }
    )


@dataclass(frozen=True)
class KernelPaths:
    """Where the kernelspecs, the IPython config and the sockets live."""

    socket_root: Path
    sidecar_root: Path

    @property
    def kernelspecs_dir(self) -> Path:
        return self.socket_root / "kernelspec" / "kernels"

    def kernelspec_dir_for(self, kernel_name: str) -> Path:
        return self.kernelspecs_dir / kernel_name

    @property
    def config_file(self) -> Path:
        return self.sidecar_root / "ipython" / "ipython_kernel_config.py"

    def template_for(self, kernel_name: str) -> Path:
        return self.sidecar_root / "jupyter" / "kernels" / kernel_name / "kernel.json"


IMAGE_SIDECAR_ROOT: Final = "/opt/rayito/sidecar"
TEMPLATE_PYTHON: Final = "python3"
MODULE_FLAG: Final = "-m"
CONNECTION_FILE_PLACEHOLDER: Final = "{connection_file}"


def install_kernelspecs(paths: KernelPaths) -> dict[str, Path]:
    """Writes one kernelspec per language the host can actually run and
    returns them by language name. Every template gets the same rewrites
    (``/opt/rayito/sidecar`` becomes ``--sidecar-root``, ``python3`` becomes
    this interpreter); Python is always available because its kernel is this
    interpreter, the others only when their program, every absolute path of
    their ``argv`` and their ``-m`` module resolve on this host."""
    written: dict[str, Path] = {}
    for language in LANGUAGES.values():
        template = paths.template_for(language.kernel_name)
        if not template.is_file():
            continue
        spec: dict[str, Any] = json.loads(template.read_text(encoding="utf-8"))
        argv = rewritten_argv(spec["argv"], paths.sidecar_root)
        if language.name != PYTHON and not kernel_available(argv):
            continue
        spec["argv"] = argv
        target_dir = paths.kernelspec_dir_for(language.kernel_name)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "kernel.json"
        target.write_text(json.dumps(spec, indent=2), encoding="utf-8")
        written[language.name] = target
    return written


def rewritten_argv(argv: list[Any], sidecar_root: Path) -> list[str]:
    rewritten = [str(arg).replace(IMAGE_SIDECAR_ROOT, str(sidecar_root)) for arg in argv]
    if rewritten and rewritten[0] == TEMPLATE_PYTHON:
        rewritten[0] = sys.executable
    return rewritten


def kernel_available(argv: list[str]) -> bool:
    """Whether a kernelspec's ``argv`` can start on this host: the program
    is on ``PATH`` or an existing absolute path, every absolute path among
    the arguments exists, and a ``python -m <module>`` launch can import its
    module."""
    if not argv or not _program_resolves(argv[0]):
        return False
    if not all(_argument_path_exists(arg) for arg in argv[1:]):
        return False
    module = _launched_module(argv)
    return module is None or importlib.util.find_spec(module) is not None


def _program_resolves(program: str) -> bool:
    if os.path.isabs(program):
        return Path(program).is_file()
    return shutil.which(program) is not None


def _argument_path_exists(argument: str) -> bool:
    if argument == CONNECTION_FILE_PLACEHOLDER:
        return True
    _, _, option_value = argument.partition("=")
    candidate = option_value if argument.startswith("-") else argument
    if not os.path.isabs(candidate):
        return True
    return Path(candidate).exists()


def _launched_module(argv: list[str]) -> str | None:
    if len(argv) >= 3 and argv[1] == MODULE_FLAG:
        return argv[2]
    return None


def kernel_environment(
    base: Mapping[str, str],
    home: str,
    socket_dir: Path,
    envs: Mapping[str, str],
    sidecar_src: Path | None = None,
) -> dict[str, str]:
    """The sidecar's own environment plus the Jupyter/matplotlib/BLAS knobs of
    design D4 and the context's ``envs`` (last wins). ``MPLBACKEND`` is never
    set so ``ipykernel`` installs the inline backend itself; the sidecar's
    ``src`` leads ``PYTHONPATH`` so the startup scripts can import the
    vendored chart extractor whatever launched the sidecar."""
    env = dict(base)
    if sidecar_src is not None:
        inherited = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
        env["PYTHONPATH"] = os.pathsep.join(
            [str(sidecar_src), *[p for p in inherited if p != str(sidecar_src)]]
        )
    env.update(
        {
            "JUPYTER_RUNTIME_DIR": str(socket_dir),
            "JUPYTER_DATA_DIR": str(Path(home) / ".local" / "share" / "jupyter"),
            "MPLCONFIGDIR": str(Path(home) / ".cache" / "matplotlib"),
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
        }
    )
    env.update(envs)
    env.pop("MPLBACKEND", None)
    return env


class KernelContext(ContextBase):
    """A real kernel behind ``AsyncKernelManager(transport="ipc")``: the
    ``ipykernel`` of the ``rayito`` spec for Python, ``bash_kernel`` or
    ``ijavascript`` for the other languages, all through the same channels
    and the same ``executions`` mapping."""

    def __init__(
        self,
        context_id: str,
        language: str,
        cwd: str,
        envs: Mapping[str, str],
        logger: SidecarLogger,
        paths: KernelPaths,
        on_kernel_died: DiedCallback | None = None,
    ) -> None:
        super().__init__(context_id, language, cwd, envs, logger, on_kernel_died)
        self._paths = paths
        self._kernel = LANGUAGES[language]
        self._socket_dir = paths.socket_root / context_id
        self._km: Any = None
        self._kc: Any = None
        self._inbox = Inbox()
        self._pumps: list[asyncio.Task[None]] = []
        self._watch: asyncio.Task[None] | None = None
        self._stopping = False

    @property
    def inbox(self) -> Inbox:
        return self._inbox

    @property
    def socket_dir(self) -> Path:
        return self._socket_dir

    @property
    def connection_file(self) -> Path:
        return self._socket_dir / "kernel.json"

    def submit(self, code: str, *, silent: bool, store_history: bool) -> str:
        msg_id: str = self._kc.execute(
            code,
            silent=silent,
            store_history=store_history,
            allow_stdin=False,
            stop_on_error=True,
        )
        return msg_id

    async def _start_kernel(self) -> None:
        from jupyter_client.kernelspec import KernelSpecManager
        from jupyter_client.manager import AsyncKernelManager

        self._socket_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self._socket_dir, 0o700)
        spec_manager = KernelSpecManager(
            kernel_dirs=[str(self._paths.kernelspecs_dir)], ensure_native_kernel=False
        )
        km = AsyncKernelManager(
            kernel_name=self._kernel.kernel_name,
            transport="ipc",
            ip=str(self._socket_dir / "k"),
            connection_file=str(self.connection_file),
            kernel_spec_manager=spec_manager,
        )
        env = kernel_environment(
            os.environ,
            os.environ.get("HOME", str(Path.home())),
            self._socket_dir,
            self.envs,
            self._paths.sidecar_root / "src",
        )
        await km.start_kernel(
            cwd=self.cwd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        kc = km.client()
        kc.start_channels(shell=True, iopub=True, stdin=False, hb=False, control=True)
        try:
            await kc.wait_for_ready(timeout=KERNEL_READY_TIMEOUT_S)
        except Exception:
            kc.stop_channels()
            await km.shutdown_kernel(now=True)
            raise
        self._km, self._kc = km, kc
        self._stopping = False
        self.kernel_pid = _kernel_pid(km)
        self._inbox = Inbox()
        self._pumps = [
            asyncio.create_task(self._pump_channel(KIND_IOPUB, kc.iopub_channel)),
            asyncio.create_task(self._pump_channel(KIND_SHELL, kc.shell_channel)),
        ]
        self._watch = asyncio.create_task(self._watch_death(km))
        await run_silent(self, self._kernel.probe_cell)

    async def _stop_kernel(self) -> None:
        self._stopping = True
        for task in [*self._pumps, self._watch]:
            if task is not None:
                task.cancel()
        self._pumps = []
        self._watch = None
        kc, km = self._kc, self._km
        self._kc = self._km = None
        self.kernel_pid = None
        if kc is not None:
            kc.stop_channels()
        if km is not None:
            try:
                await km.shutdown_kernel(now=True)
            except Exception as error:
                self._logger.warn(
                    "kernel shutdown failed",
                    context_id=self.context_id,
                    reason=type(error).__name__,
                )
        shutil.rmtree(self._socket_dir, ignore_errors=True)

    async def _interrupt_kernel(self) -> None:
        if self._km is not None:
            await self._km.interrupt_kernel()

    async def _abort_running(self, reason: str) -> None:
        self._inbox.put_sentinel((KIND_ABORT, reason))

    async def _run_cell(
        self, slot: ExecutionSlot, code: str, envs: Mapping[str, str], emit: Emit
    ) -> None:
        await run_execution(self, slot.request_id, slot.execution_id, code, envs, emit)

    async def _run_silent_cell(self, code: str) -> ExecutionOutcome:
        return await run_silent(self, code)

    async def _probe_kernel(self, timeout: float) -> bool:
        kc = self._kc
        if kc is None:
            return False
        msg = kc.session.msg("kernel_info_request")
        kc.control_channel.send(msg)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                reply = await kc.control_channel.get_msg(timeout=remaining)
            except Exception:
                return False
            if reply.get("parent_header", {}).get("msg_id") == msg["header"]["msg_id"]:
                return bool(reply.get("msg_type") == "kernel_info_reply")

    async def _pump_channel(self, kind: str, channel: Any) -> None:
        while True:
            msg = await channel.get_msg()
            await self._inbox.put((kind, msg))

    async def _watch_death(self, km: Any) -> None:
        exit_code = await km.provisioner.wait()
        if self._stopping or self._km is not km:
            return
        code = exit_code if isinstance(exit_code, int) else None
        if code is not None and code < 0:
            code = 128 - code
        self._inbox.put_sentinel((KIND_DIED, code))
        self.kernel_died(code)


def _kernel_pid(km: Any) -> int | None:
    provisioner = getattr(km, "provisioner", None)
    pid = getattr(provisioner, "pid", None)
    return int(pid) if isinstance(pid, int) else None


def context_summary(context: ContextBase) -> Event:
    return {
        "context_id": context.context_id,
        "language": context.language,
        "cwd": context.cwd,
        "kernel_pid": context.kernel_pid,
        "state": context.state,
    }
