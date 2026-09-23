"""`ProcessService` falso en proceso, con el contrato de `rayd` en M2.

Interpreta el wrapper `/bin/bash -l -c <cmd>` con una tabla mínima de scripts
(`echo`, `err`, `exit`, `sleep`, `cat`, `seq`, `big`, `split`, `truncate`,
`pwd`, `whoami`, `env`; lo demás es `command not found`, 127) y reproduce lo
que el SDK necesita observar: `StartEvent` primero, un `KeepAlive` que el
cliente debe ignorar, ring por pid con `seq` desde 1, `Connect(from_seq)` con
`OUT_OF_RANGE`, retención de terminados, `timeout_ms`, `SendSignal`, stdin
por pipe, cap de suscriptores y `x-access-token` en cada RPC. Desde M5
comparte el registro de pids con `FakePtyService` (`List` lista ambos,
`Connect`/`SendInput`/`CloseStdin` rechazan un pid de PTY con
`FAILED_PRECONDITION`, `SendSignal` acepta ambos) y `suspend()` cierra los
streams vivos con `EndEvent{suspending}` sin tocar los procesos.
"""

from __future__ import annotations

import hashlib
import json
import queue
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import grpc

from rayito._payload import build_run_hook_payload, decode_access_token
from rayito._transport import ACCESS_TOKEN_KEY, metadata_dict
from rayito.exceptions import InvalidArgumentException
from rayito.v1 import common_pb2, process_pb2, process_pb2_grpc

if TYPE_CHECKING:
    from .fake_pty import FakePty

CHUNK_SIZE = 32 * 1024
FIRST_PID = 1000
MAX_LIVE_PROCESSES = 256
MAX_SUBSCRIBERS_PER_PID = 8
SIGTERM = 15
KNOWN_DIRECTORIES = frozenset({"/", "/tmp", "/home/user"})
DEFAULT_USERNAME = "user"
DEFAULT_CWD = "/home/user"
STEP_SECONDS = 0.02
SEQ_PAUSE_SECONDS = 0.05


def installed_token_sha256(access_token: str) -> str:
    """Lo que `rayd` instala desde `runHookPayload`: el campo `token_sha256`."""
    payload = json.loads(build_run_hook_payload(access_token=access_token))
    return str(payload["token_sha256"])


def presented_token_sha256(metadata: dict[str, str]) -> str | None:
    """`sha256(base64url_decode(x-access-token))`, como `rayd`; None si falta o no decodifica."""
    presented = metadata.get(ACCESS_TOKEN_KEY)
    if presented is None:
        return None
    try:
        return hashlib.sha256(decode_access_token(presented)).hexdigest()
    except InvalidArgumentException:
        return None


def require_access_token(context: grpc.ServicerContext, token_sha256: str) -> dict[str, str]:
    metadata = metadata_dict(context.invocation_metadata())
    if presented_token_sha256(metadata) != token_sha256:
        context.abort(grpc.StatusCode.UNAUTHENTICATED, "x-access-token ausente o inválido")
    return metadata


def exited(exit_code: int) -> process_pb2.EndEvent:
    return process_pb2.EndEvent(exit_code=exit_code, exited=True, status="exited")


def signaled(signal: int) -> process_pb2.EndEvent:
    return process_pb2.EndEvent(
        exit_code=128 + signal, exited=True, status="signaled", signal=signal
    )


def timed_out() -> process_pb2.EndEvent:
    return process_pb2.EndEvent(
        exit_code=128 + SIGTERM,
        exited=False,
        status="timeout",
        signal=SIGTERM,
        error=common_pb2.StreamError(code="deadline_exceeded", message="timeout_ms expired"),
    )


def truncated(last_seq: int) -> process_pb2.EndEvent:
    return process_pb2.EndEvent(
        exited=False,
        status="output_truncated",
        error=common_pb2.StreamError(
            code="output_truncated", message=f"subscriber stalled for 30 s at seq {last_seq}"
        ),
    )


def suspending() -> process_pb2.EndEvent:
    return process_pb2.EndEvent(
        exit_code=0,
        exited=False,
        status="suspending",
        error=common_pb2.StreamError(
            code="suspending", message="sandbox suspending; reconnect with Connect(pid, from_seq)"
        ),
    )


def sandbox_timed_out() -> process_pb2.EndEvent:
    """El final con que `rayd` cierra un stream al vencer el plazo lógico
    del sandbox (ADR-011, tabla de cierres de design D6)."""
    return process_pb2.EndEvent(
        exit_code=0,
        exited=False,
        status="sandbox_timeout",
        error=common_pb2.StreamError(code="sandbox_timeout", message="sandbox timeout"),
    )


class ReplayOutOfRange(Exception):
    def __init__(self, oldest: int, next_seq: int) -> None:
        super().__init__(f"from_seq fuera de rango: oldest={oldest} next={next_seq}")
        self.oldest = oldest
        self.next_seq = next_seq


class FakeProcess:
    """Un proceso del `rayd` falso: ring, suscriptores, stdin y señal."""

    def __init__(self, pid: int, request: process_pb2.StartRequest) -> None:
        self.pid = pid
        self.request = request
        self.stdin_enabled = bool(request.stdin)
        self.stdin: queue.Queue[bytes | None] = queue.Queue()
        self.stdin_closed = False
        self.lock = threading.Lock()
        self.ring: list[process_pb2.DataEvent] = []
        self.next_seq = 1
        self.subscribers: list[queue.Queue[process_pb2.ProcessEvent]] = []
        self.signal = threading.Event()
        self.signal_number: int | None = None
        self.end: process_pb2.EndEvent | None = None
        self.ended_at: float | None = None

    @property
    def alive(self) -> bool:
        return self.end is None

    @property
    def username(self) -> str:
        if self.request.HasField("user") and self.request.user.username:
            return str(self.request.user.username)
        return DEFAULT_USERNAME

    @property
    def cwd(self) -> str:
        config = self.request.process
        return str(config.cwd) if config.HasField("cwd") else DEFAULT_CWD

    def deadline_seconds(self) -> float | None:
        timeout_ms = int(self.request.timeout_ms)
        return timeout_ms / 1000 if timeout_ms > 0 else None

    def publish(self, stream: str, payload: bytes) -> None:
        with self.lock:
            data = process_pb2.DataEvent(seq=self.next_seq, **{stream: payload})
            self.next_seq += 1
            self.ring.append(data)
            self._fan_out(process_pb2.ProcessEvent(data=data))

    def finish(self, end: process_pb2.EndEvent) -> None:
        with self.lock:
            self.end = end
            self.ended_at = time.monotonic()
            self._fan_out(process_pb2.ProcessEvent(end=end))
            self.subscribers = []

    def truncate_subscribers(self) -> None:
        with self.lock:
            self._fan_out(process_pb2.ProcessEvent(end=truncated(self.next_seq - 1)))
            self.subscribers = []

    def suspend(self) -> None:
        """Lo que hace `/suspend`: el final `suspending` a cada suscriptor y
        el stream cerrado; el proceso sigue corriendo."""
        with self.lock:
            self._fan_out(process_pb2.ProcessEvent(end=suspending()))
            self.subscribers = []

    def close_streams(self, end: process_pb2.EndEvent) -> None:
        """Cierra cada stream vivo con `end` sin terminar el proceso."""
        with self.lock:
            self._fan_out(process_pb2.ProcessEvent(end=end))
            self.subscribers = []

    def subscribe(
        self, from_seq: int
    ) -> tuple[queue.Queue[process_pb2.ProcessEvent] | None, list[process_pb2.ProcessEvent]]:
        """Registra el suscriptor y toma la foto del replay bajo el mismo lock
        (ni huecos ni duplicados). Sin cola si el proceso ya terminó."""
        with self.lock:
            oldest = int(self.ring[0].seq) if self.ring else self.next_seq
            if from_seq > 0 and (from_seq < oldest or from_seq > self.next_seq):
                raise ReplayOutOfRange(oldest, self.next_seq)
            replay = [
                process_pb2.ProcessEvent(data=data)
                for data in self.ring
                if from_seq > 0 and int(data.seq) >= from_seq
            ]
            if self.end is not None:
                return None, [*replay, process_pb2.ProcessEvent(end=self.end)]
            subscriber: queue.Queue[process_pb2.ProcessEvent] = queue.Queue()
            self.subscribers.append(subscriber)
            return subscriber, replay

    def unsubscribe(self, subscriber: queue.Queue[process_pb2.ProcessEvent]) -> None:
        with self.lock:
            if subscriber in self.subscribers:
                self.subscribers.remove(subscriber)

    def wait(self, seconds: float) -> str:
        """Espera `seconds` salvo señal o `timeout_ms`: "signaled" | "timeout" | "done"."""
        deadline = self.deadline_seconds()
        if deadline is not None and deadline < seconds:
            return "signaled" if self.signal.wait(deadline) else "timeout"
        return "signaled" if self.signal.wait(seconds) else "done"

    def end_after(self, outcome: str) -> process_pb2.EndEvent:
        if outcome == "signaled":
            return signaled(self.signal_number or SIGTERM)
        if outcome == "timeout":
            return timed_out()
        return exited(0)

    def _fan_out(self, event: process_pb2.ProcessEvent) -> None:
        for subscriber in self.subscribers:
            subscriber.put(event)


def chunks(payload: bytes) -> Iterator[bytes]:
    for offset in range(0, len(payload), CHUNK_SIZE):
        yield payload[offset : offset + CHUNK_SIZE]


def run_echo(process: FakeProcess, argument: str) -> None:
    process.publish("stdout", f"{argument}\n".encode())
    process.finish(exited(0))


def run_err(process: FakeProcess, argument: str) -> None:
    process.publish("stderr", f"{argument}\n".encode())
    process.finish(exited(0))


def run_exit(process: FakeProcess, argument: str) -> None:
    process.finish(exited(int(argument)))


def run_sleep(process: FakeProcess, argument: str) -> None:
    process.finish(process.end_after(process.wait(float(argument))))


def run_cat(process: FakeProcess, argument: str) -> None:
    """Con `stdin=false` el hijo lee `/dev/null` y `cat` termina al instante."""
    if not process.stdin_enabled:
        process.finish(exited(0))
        return
    while not process.signal.is_set():
        try:
            item = process.stdin.get(timeout=STEP_SECONDS)
        except queue.Empty:
            continue
        if item is None:
            process.finish(exited(0))
            return
        process.publish("stdout", item)
    process.finish(process.end_after("signaled"))


def run_seq(process: FakeProcess, argument: str) -> None:
    for number in range(1, int(argument) + 1):
        process.publish("stdout", f"{number}\n".encode())
        if process.signal.wait(SEQ_PAUSE_SECONDS):
            process.finish(process.end_after("signaled"))
            return
    process.finish(exited(0))


def run_big(process: FakeProcess, argument: str) -> None:
    for chunk in chunks(b"a" * int(argument)):
        process.publish("stdout", chunk)
    process.finish(exited(0))


def run_split(process: FakeProcess, argument: str) -> None:
    """Un carácter multibyte partido justo en el límite de 32 KiB."""
    for chunk in chunks(b"a" * (CHUNK_SIZE - 1) + "é\n".encode()):
        process.publish("stdout", chunk)
    process.finish(exited(0))


def run_truncate(process: FakeProcess, argument: str) -> None:
    process.publish("stdout", b"partial\n")
    process.truncate_subscribers()
    process.finish(process.end_after(process.wait(30.0)))


def run_pwd(process: FakeProcess, argument: str) -> None:
    process.publish("stdout", f"{process.cwd}\n".encode())
    process.finish(exited(0))


def run_whoami(process: FakeProcess, argument: str) -> None:
    process.publish("stdout", f"{process.username}\n".encode())
    process.finish(exited(0))


def run_env(process: FakeProcess, argument: str) -> None:
    envs = dict(process.request.process.envs)
    lines = "".join(f"{key}={envs[key]}\n" for key in sorted(envs))
    process.publish("stdout", lines.encode())
    process.finish(exited(0))


@dataclass(frozen=True)
class CannedReply:
    """Salida y exit fijos para los comandos que contienen un fragmento
    (`reply_when`): los tests de git no necesitan un git de verdad."""

    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0


def run_canned(process: FakeProcess, reply: CannedReply) -> None:
    if reply.stdout:
        process.publish("stdout", reply.stdout.encode())
    if reply.stderr:
        process.publish("stderr", reply.stderr.encode())
    process.finish(exited(reply.exit_code))


def run_unknown(process: FakeProcess, name: str) -> None:
    process.publish("stderr", f"bash: {name}: command not found\n".encode())
    process.finish(exited(127))


SCRIPTS: dict[str, Callable[[FakeProcess, str], None]] = {
    "echo": run_echo,
    "err": run_err,
    "exit": run_exit,
    "sleep": run_sleep,
    "cat": run_cat,
    "seq": run_seq,
    "big": run_big,
    "split": run_split,
    "truncate": run_truncate,
    "pwd": run_pwd,
    "whoami": run_whoami,
    "env": run_env,
}


def run_script(process: FakeProcess) -> None:
    command = str(process.request.process.args[-1])
    name, _, argument = command.partition(" ")
    script = SCRIPTS.get(name)
    if script is None:
        run_unknown(process, name)
        return
    script(process, argument)


def stream_events(
    process: FakeProcess,
    subscriber: queue.Queue[process_pb2.ProcessEvent] | None,
    prelude: list[process_pb2.ProcessEvent],
    context: grpc.ServicerContext,
) -> Iterator[process_pb2.ProcessEvent]:
    yield process_pb2.ProcessEvent(start=process_pb2.StartEvent(pid=process.pid))
    yield process_pb2.ProcessEvent(keepalive=common_pb2.KeepAlive())
    yield from prelude
    if subscriber is None:
        return
    try:
        while context.is_active():
            try:
                event = subscriber.get(timeout=STEP_SECONDS)
            except queue.Empty:
                continue
            yield event
            if event.HasField("end"):
                return
    finally:
        process.unsubscribe(subscriber)


def pty_info(pty: FakePty) -> process_pb2.ProcessInfo:
    """Una PTY en `List`: `cmd` es el shell, `args` `["-i", "-l"]`, sin tag."""
    config = process_pb2.ProcessConfig(cmd=pty.shell, args=["-i", "-l"], cwd=pty.cwd)
    config.envs.update(dict(pty.request.envs))
    return process_pb2.ProcessInfo(pid=pty.pid, config=config, kind=process_pb2.PROCESS_KIND_PTY)


def process_info(process: FakeProcess) -> process_pb2.ProcessInfo:
    info = process_pb2.ProcessInfo(
        pid=process.pid,
        config=process.request.process,
        kind=process_pb2.PROCESS_KIND_PROCESS,
    )
    if process.request.HasField("tag"):
        info.tag = process.request.tag
    return info


@dataclass
class FakeProcessService(process_pb2_grpc.ProcessServiceServicer):
    """`ProcessService` como lo implementa `rayd` en M2 (ver módulo)."""

    token_sha256: str
    allow_root: bool = False
    phase: str | None = None
    retention_seconds: float = 30.0
    start_requests: list[process_pb2.StartRequest] = field(default_factory=list)
    start_metadata: list[dict[str, str]] = field(default_factory=list)
    connect_requests: list[process_pb2.ConnectRequest] = field(default_factory=list)
    peers: set[str] = field(default_factory=set)
    processes: dict[int, FakeProcess] = field(default_factory=dict)
    ptys: dict[int, FakePty] = field(default_factory=dict)
    signal_requests: list[process_pb2.SendSignalRequest] = field(default_factory=list)
    signal_unavailable_calls: int = 0
    next_pid: int = FIRST_PID
    replies: list[tuple[str, CannedReply]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def Start(
        self, request: process_pb2.StartRequest, context: grpc.ServicerContext
    ) -> Iterator[process_pb2.ProcessEvent]:
        self.start_metadata.append(self._authenticate(context))
        self.start_requests.append(request)
        self._gate_phase(context)
        self._validate_start(request, context)
        process = self._spawn(request)
        subscriber, prelude = process.subscribe(0)
        threading.Thread(target=self._run, args=(process,), daemon=True).start()
        return stream_events(process, subscriber, prelude, context)

    def Connect(
        self, request: process_pb2.ConnectRequest, context: grpc.ServicerContext
    ) -> Iterator[process_pb2.ProcessEvent]:
        self._authenticate(context)
        self.connect_requests.append(request)
        self._gate_phase(context)
        self._refuse_pty_pid(int(request.pid), context)
        process = self._retained(int(request.pid), context)
        if len(process.subscribers) >= MAX_SUBSCRIBERS_PER_PID:
            context.abort(
                grpc.StatusCode.RESOURCE_EXHAUSTED,
                f"max {MAX_SUBSCRIBERS_PER_PID} subscribers per pid",
            )
        try:
            subscriber, prelude = process.subscribe(int(request.from_seq))
        except ReplayOutOfRange as exc:
            context.abort(grpc.StatusCode.OUT_OF_RANGE, str(exc))
        return stream_events(process, subscriber, prelude, context)

    def SendInput(
        self, request: process_pb2.SendInputRequest, context: grpc.ServicerContext
    ) -> process_pb2.SendInputResponse:
        self._authenticate(context)
        process = self._live(int(request.pid), context)
        if not process.stdin_enabled or process.stdin_closed:
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, "stdin is not open")
        process.stdin.put(bytes(request.data))
        return process_pb2.SendInputResponse()

    def CloseStdin(
        self, request: process_pb2.CloseStdinRequest, context: grpc.ServicerContext
    ) -> process_pb2.CloseStdinResponse:
        self._authenticate(context)
        process = self._live(int(request.pid), context)
        if not process.stdin_enabled:
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, "stdin is not open")
        if not process.stdin_closed:
            process.stdin_closed = True
            process.stdin.put(None)
        return process_pb2.CloseStdinResponse()

    def SendSignal(
        self, request: process_pb2.SendSignalRequest, context: grpc.ServicerContext
    ) -> process_pb2.SendSignalResponse:
        self._authenticate(context)
        self.signal_requests.append(request)
        if self.signal_unavailable_calls > 0:
            self.signal_unavailable_calls -= 1
            context.abort(grpc.StatusCode.UNAVAILABLE, "suspending")
        self._gate_phase(context)
        if not 1 <= int(request.signal) <= 64:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "signal must be in 1..=64")
        pty = self.ptys.get(int(request.pid))
        if pty is not None:
            if not pty.alive:
                context.abort(grpc.StatusCode.NOT_FOUND, f"pid {request.pid} not found")
            pty.kill(int(request.signal))
            return process_pb2.SendSignalResponse()
        process = self._live(int(request.pid), context)
        process.signal_number = int(request.signal)
        process.signal.set()
        return process_pb2.SendSignalResponse()

    def List(
        self, request: process_pb2.ListRequest, context: grpc.ServicerContext
    ) -> process_pb2.ListResponse:
        self._authenticate(context)
        return process_pb2.ListResponse(
            processes=[
                *(process_info(process) for process in self.live_processes()),
                *(pty_info(pty) for pty in self.live_ptys()),
            ]
        )

    # --------------------------------------------------------- test controls

    def reply_when(self, fragment: str, reply: CannedReply) -> None:
        """Los comandos cuyo texto contiene `fragment` responden `reply`; la
        primera regla que coincide gana y el resto sigue la tabla de scripts."""
        self.replies.append((fragment, reply))

    def commands(self) -> list[str]:
        """El comando de usuario (el `-c` del wrapper) de cada `Start`, en orden."""
        return [str(request.process.args[-1]) for request in self.start_requests]

    def suspend(self) -> None:
        """Lo que hace `/suspend`: los streams vivos terminan con
        `EndEvent{suspending}` y los nuevos se rechazan con el phase gate."""
        for process in self.live_processes():
            process.suspend()
        self.phase = "suspending"

    def resume(self) -> None:
        self.phase = None

    def allocate_pid(self) -> int:
        with self.lock:
            pid = self.next_pid
            self.next_pid += 1
            return pid

    def live_processes(self) -> list[FakeProcess]:
        with self.lock:
            return [process for process in self.processes.values() if process.alive]

    def live_ptys(self) -> list[FakePty]:
        with self.lock:
            return [pty for pty in self.ptys.values() if pty.alive]

    def live_count(self) -> int:
        return len(self.live_processes()) + len(self.live_ptys())

    def _run(self, process: FakeProcess) -> None:
        command = str(process.request.process.args[-1])
        for fragment, reply in self.replies:
            if fragment in command:
                run_canned(process, reply)
                return
        run_script(process)

    def _authenticate(self, context: grpc.ServicerContext) -> dict[str, str]:
        self.peers.add(str(context.peer()))
        return require_access_token(context, self.token_sha256)

    def _gate_phase(self, context: grpc.ServicerContext) -> None:
        """Mientras la sesión está `suspending` o `terminating` (design D7/D8)
        los streams nuevos y los unarios se rechazan con `UNAVAILABLE`."""
        if self.phase is not None:
            context.abort(grpc.StatusCode.UNAVAILABLE, self.phase)

    def _refuse_pty_pid(self, pid: int, context: grpc.ServicerContext) -> None:
        if pid in self.ptys:
            context.abort(
                grpc.StatusCode.FAILED_PRECONDITION, f"pid {pid} is a PTY; use PtyService"
            )

    def _validate_start(
        self, request: process_pb2.StartRequest, context: grpc.ServicerContext
    ) -> None:
        if not request.process.cmd:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "empty command")
        if request.HasField("user") and request.user.username == "root" and not self.allow_root:
            context.abort(grpc.StatusCode.PERMISSION_DENIED, "root is not allowed")
        if request.process.HasField("cwd") and request.process.cwd not in KNOWN_DIRECTORIES:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "cwd is not a directory")
        if self.live_count() >= MAX_LIVE_PROCESSES:
            context.abort(
                grpc.StatusCode.RESOURCE_EXHAUSTED, f"max {MAX_LIVE_PROCESSES} live processes"
            )

    def _spawn(self, request: process_pb2.StartRequest) -> FakeProcess:
        with self.lock:
            pid = self.next_pid
            self.next_pid += 1
            process = FakeProcess(pid, request)
            self.processes[pid] = process
            return process

    def _live(self, pid: int, context: grpc.ServicerContext) -> FakeProcess:
        self._refuse_pty_pid(pid, context)
        process = self.processes.get(pid)
        if process is None or not process.alive:
            context.abort(grpc.StatusCode.NOT_FOUND, f"pid {pid} not found")
        return process

    def _retained(self, pid: int, context: grpc.ServicerContext) -> FakeProcess:
        process = self.processes.get(pid)
        if process is None or not (process.alive or self._within_retention(process)):
            context.abort(grpc.StatusCode.NOT_FOUND, f"pid {pid} not found")
        return process

    def _within_retention(self, process: FakeProcess) -> bool:
        return (
            process.ended_at is not None
            and time.monotonic() - process.ended_at < self.retention_seconds
        )
