"""`PtyService` falso en proceso, con el contrato de `rayd` en M5
(`openspec/changes/m5-pty-suspend-resume/design.md` D1-D5).

Un "shell de eco": cada `SendInput` se devuelve como `data` con `\\n`
convertido en `\\r\\n` (el eco de la terminal) y cada línea completa se
interpreta con una tabla mínima (`echo`, `stty size`, `exit`, `id -u; tty`,
`sleep`; `$VAR` se expande con el entorno de la PTY; lo demás es `command
not found`). Reproduce lo que el SDK necesita observar: `started{pid}`
primero, `seq` desde 1 en `data` y 0 en el resto, ring por pid con
`Connect(from_seq)` y `OUT_OF_RANGE`, retención de terminadas, `Resize` que
el siguiente `stty size` refleja, `Kill` → `exited{signaled, 137}`,
`timeout_ms`, `FAILED_PRECONDITION` para un pid que no es PTY, el phase
gate, `suspend()` (termina los streams vivos con `exited{suspending}`) y
`x-access-token` en cada RPC. Comparte el registro de pids con
`FakeProcessService`.
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import grpc

from rayito.v1 import common_pb2, process_pb2, pty_pb2, pty_pb2_grpc

from .fake_process import (
    KNOWN_DIRECTORIES,
    MAX_LIVE_PROCESSES,
    MAX_SUBSCRIBERS_PER_PID,
    SIGTERM,
    STEP_SECONDS,
    ReplayOutOfRange,
    require_access_token,
)

if TYPE_CHECKING:
    from .fake_process import FakeProcessService

DEFAULT_COLS = 80
DEFAULT_ROWS = 24
MAX_DIMENSION = 4096
DEFAULT_SHELL = "/bin/bash"
FALSE_SHELL = "/bin/false"
DEFAULT_UID = "1000"
PTS_DEVICE = "/dev/pts/0"
CHUNK_SIZE = 16 * 1024
NO_DEADLINE_SECONDS = 1e9
BASE_ENV = {"TERM": "xterm-256color", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}


def pty_exited(exit_code: int) -> pty_pb2.PtyExited:
    return pty_pb2.PtyExited(exit_code=exit_code, exited=True, status="exited")


def pty_signaled(signal: int) -> pty_pb2.PtyExited:
    return pty_pb2.PtyExited(exit_code=128 + signal, exited=True, status="signaled", signal=signal)


def pty_timed_out() -> pty_pb2.PtyExited:
    return pty_pb2.PtyExited(
        exit_code=128 + SIGTERM,
        exited=False,
        status="timeout",
        signal=SIGTERM,
        error=common_pb2.StreamError(code="deadline_exceeded", message="timeout_ms expired"),
    )


def pty_suspending() -> pty_pb2.PtyExited:
    return pty_pb2.PtyExited(
        exit_code=0,
        exited=False,
        status="suspending",
        error=common_pb2.StreamError(
            code="suspending",
            message="sandbox suspending; reconnect with Connect(pid, from_seq)",
        ),
    )


def pty_sandbox_timed_out() -> pty_pb2.PtyExited:
    """El `PtyExited` con que `rayd` cierra una PTY al vencer el plazo lógico
    del sandbox (ADR-011, tabla de cierres de design D6)."""
    return pty_pb2.PtyExited(
        exited=False,
        status="sandbox_timeout",
        error=common_pb2.StreamError(code="sandbox_timeout", message="sandbox timeout"),
    )


def started_message(pid: int) -> pty_pb2.PtyServerMessage:
    return pty_pb2.PtyServerMessage(started=pty_pb2.PtyStarted(pid=pid))


def data_message(seq: int, payload: bytes) -> pty_pb2.PtyServerMessage:
    return pty_pb2.PtyServerMessage(data=payload, seq=seq)


def exited_message(exited: pty_pb2.PtyExited) -> pty_pb2.PtyServerMessage:
    return pty_pb2.PtyServerMessage(exited=exited)


def keepalive_message() -> pty_pb2.PtyServerMessage:
    return pty_pb2.PtyServerMessage(keepalive=common_pb2.KeepAlive())


class FakePty:
    """Una PTY del `rayd` falso: ring, suscriptores, tamaño, shell de eco."""

    def __init__(self, pid: int, request: pty_pb2.PtyStart, shell: str) -> None:
        self.pid = pid
        self.request = request
        self.shell = shell
        self.cols = int(request.size.cols) if request.HasField("size") else DEFAULT_COLS
        self.rows = int(request.size.rows) if request.HasField("size") else DEFAULT_ROWS
        self.lock = threading.Lock()
        self.ring: list[pty_pb2.PtyServerMessage] = []
        self.next_seq = 1
        self.subscribers: list[queue.Queue[pty_pb2.PtyServerMessage]] = []
        self.end: pty_pb2.PtyExited | None = None
        self.ended_at: float | None = None
        self.signal = threading.Event()
        self.signal_number: int | None = None
        self.inputs: list[bytes] = []
        self.pending_line = b""
        self.env = {**BASE_ENV, "SHELL": shell, **dict(request.envs)}

    @property
    def alive(self) -> bool:
        return self.end is None

    @property
    def cwd(self) -> str:
        return str(self.request.cwd) if self.request.HasField("cwd") else "/home/user"

    def deadline_seconds(self) -> float | None:
        timeout_ms = int(self.request.timeout_ms)
        return timeout_ms / 1000 if timeout_ms > 0 else None

    def publish(self, payload: bytes) -> None:
        for offset in range(0, len(payload), CHUNK_SIZE):
            self._publish_chunk(payload[offset : offset + CHUNK_SIZE])

    def _publish_chunk(self, chunk: bytes) -> None:
        with self.lock:
            if self.end is not None:
                return
            message = data_message(self.next_seq, chunk)
            self.next_seq += 1
            self.ring.append(message)
            self._fan_out(message)

    def finish(self, end: pty_pb2.PtyExited) -> None:
        with self.lock:
            if self.end is not None:
                return
            self.end = end
            self.ended_at = time.monotonic()
            self._fan_out(exited_message(end))
            self.subscribers = []
        self.signal.set()

    def suspend(self) -> None:
        with self.lock:
            self._fan_out(exited_message(pty_suspending()))
            self.subscribers = []

    def close_streams(self, exited: pty_pb2.PtyExited) -> None:
        """Cierra cada stream vivo con `exited` sin terminar la PTY."""
        with self.lock:
            self._fan_out(exited_message(exited))
            self.subscribers = []

    def subscribe(
        self, from_seq: int
    ) -> tuple[queue.Queue[pty_pb2.PtyServerMessage] | None, list[pty_pb2.PtyServerMessage]]:
        with self.lock:
            oldest = int(self.ring[0].seq) if self.ring else self.next_seq
            if from_seq > 0 and (from_seq < oldest or from_seq > self.next_seq):
                raise ReplayOutOfRange(oldest, self.next_seq)
            replay = [
                message for message in self.ring if from_seq > 0 and int(message.seq) >= from_seq
            ]
            if self.end is not None:
                return None, [*replay, exited_message(self.end)]
            subscriber: queue.Queue[pty_pb2.PtyServerMessage] = queue.Queue()
            self.subscribers.append(subscriber)
            return subscriber, replay

    def unsubscribe(self, subscriber: queue.Queue[pty_pb2.PtyServerMessage]) -> None:
        with self.lock:
            if subscriber in self.subscribers:
                self.subscribers.remove(subscriber)

    def feed_input(self, data: bytes) -> None:
        """El eco de la terminal y la interpretación de cada línea completa;
        un Ctrl-D con la línea vacía termina el shell, como bash."""
        self.inputs.append(data)
        if data == b"\x04" and not self.pending_line:
            self.finish(pty_exited(0))
            return
        self.publish(data.replace(b"\n", b"\r\n"))
        self.pending_line += data
        while b"\n" in self.pending_line:
            line, _, self.pending_line = self.pending_line.partition(b"\n")
            self.run_line(line.decode("utf-8", errors="replace").strip())
            if self.end is not None:
                return

    def run_line(self, line: str) -> None:
        for command in (part.strip() for part in line.split(";")):
            if command:
                self.run_command(command)
            if self.end is not None:
                return

    def run_command(self, command: str) -> None:
        name, _, argument = command.partition(" ")
        if name == "echo":
            self.publish(f"{self.expand(argument)}\r\n".encode())
        elif command == "stty size":
            self.publish(f"{self.rows} {self.cols}\r\n".encode())
        elif command == "id -u":
            self.publish(f"{DEFAULT_UID}\r\n".encode())
        elif command == "tty":
            self.publish(f"{PTS_DEVICE}\r\n".encode())
        elif name == "exit":
            self.finish(pty_exited(int(argument or "0")))
        elif name == "sleep":
            self.sleep(float(argument or "0"))
        else:
            self.publish(f"bash: {name}: command not found\r\n".encode())

    def expand(self, text: str) -> str:
        words = []
        for word in text.split():
            if word.startswith("$"):
                words.append(self.env.get(word[1:], ""))
            else:
                words.append(word)
        return " ".join(words)

    def sleep(self, seconds: float) -> None:
        if self.signal.wait(seconds):
            return

    def wait_for_timeout(self) -> None:
        """Como `rayd`: SIGTERM al grupo al vencer `timeout_ms`."""
        deadline = self.deadline_seconds()
        if deadline is None:
            return
        if not self.signal.wait(deadline):
            self.finish(pty_timed_out())

    def kill(self, signal: int) -> None:
        self.signal_number = signal
        self.finish(pty_signaled(signal))

    def _fan_out(self, message: pty_pb2.PtyServerMessage) -> None:
        for subscriber in self.subscribers:
            subscriber.put(message)


def stream_messages(
    pty: FakePty,
    subscriber: queue.Queue[pty_pb2.PtyServerMessage] | None,
    prelude: list[pty_pb2.PtyServerMessage],
    context: grpc.ServicerContext,
) -> Iterator[pty_pb2.PtyServerMessage]:
    yield started_message(pty.pid)
    yield keepalive_message()
    yield from prelude
    if subscriber is None:
        return
    try:
        while context.is_active():
            try:
                message = subscriber.get(timeout=STEP_SECONDS)
            except queue.Empty:
                continue
            yield message
            if message.HasField("exited"):
                return
    finally:
        pty.unsubscribe(subscriber)


@dataclass
class FakePtyService(pty_pb2_grpc.PtyServiceServicer):
    """`PtyService` como lo implementa `rayd` en M5 (ver módulo)."""

    token_sha256: str
    processes: FakeProcessService
    allow_root: bool = False
    pty_devices: bool = True
    create_requests: list[pty_pb2.PtyStart] = field(default_factory=list)
    create_metadata: list[dict[str, str]] = field(default_factory=list)
    connect_requests: list[process_pb2.ConnectRequest] = field(default_factory=list)
    resize_requests: list[pty_pb2.ResizeRequest] = field(default_factory=list)
    kill_requests: list[int] = field(default_factory=list)
    deadlines: dict[str, list[float | None]] = field(default_factory=dict)
    peers: set[str] = field(default_factory=set)

    @property
    def ptys(self) -> dict[int, FakePty]:
        return self.processes.ptys

    @property
    def phase(self) -> str | None:
        return self.processes.phase

    def Create(
        self, request: pty_pb2.PtyStart, context: grpc.ServicerContext
    ) -> Iterator[pty_pb2.PtyServerMessage]:
        self.create_metadata.append(self._authenticate(context, "Create"))
        self.create_requests.append(request)
        self._gate_phase(context)
        shell = self._validate_create(request, context)
        pty = FakePty(self.processes.allocate_pid(), request, shell)
        self.ptys[pty.pid] = pty
        subscriber, prelude = pty.subscribe(0)
        if shell == FALSE_SHELL:
            pty.finish(pty_exited(1))
        threading.Thread(target=pty.wait_for_timeout, daemon=True).start()
        return stream_messages(pty, subscriber, prelude, context)

    def Connect(
        self, request: process_pb2.ConnectRequest, context: grpc.ServicerContext
    ) -> Iterator[pty_pb2.PtyServerMessage]:
        self._authenticate(context, "Connect")
        self.connect_requests.append(request)
        self._gate_phase(context)
        pty = self._retained(int(request.pid), context)
        if len(pty.subscribers) >= MAX_SUBSCRIBERS_PER_PID:
            context.abort(
                grpc.StatusCode.RESOURCE_EXHAUSTED,
                f"max {MAX_SUBSCRIBERS_PER_PID} subscribers per pid",
            )
        try:
            subscriber, prelude = pty.subscribe(int(request.from_seq))
        except ReplayOutOfRange as exc:
            context.abort(grpc.StatusCode.OUT_OF_RANGE, str(exc))
        return stream_messages(pty, subscriber, prelude, context)

    def SendInput(
        self, request: process_pb2.SendInputRequest, context: grpc.ServicerContext
    ) -> process_pb2.SendInputResponse:
        self._authenticate(context, "SendInput")
        pty = self._live(int(request.pid), context)
        pty.feed_input(bytes(request.data))
        return process_pb2.SendInputResponse()

    def Resize(
        self, request: pty_pb2.ResizeRequest, context: grpc.ServicerContext
    ) -> pty_pb2.ResizeResponse:
        self._authenticate(context, "Resize")
        self.resize_requests.append(request)
        if not valid_dimensions(request.size):
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid pty size")
        pty = self._live(int(request.pid), context)
        pty.cols, pty.rows = int(request.size.cols), int(request.size.rows)
        return pty_pb2.ResizeResponse()

    def Kill(
        self, request: pty_pb2.KillPtyRequest, context: grpc.ServicerContext
    ) -> pty_pb2.KillPtyResponse:
        self._authenticate(context, "Kill")
        self.kill_requests.append(int(request.pid))
        pty = self._live(int(request.pid), context)
        pty.kill(9)
        return pty_pb2.KillPtyResponse()

    # --------------------------------------------------------- test controls

    def suspend(self) -> None:
        """Lo que hace `/suspend` con las PTY: cierra los streams vivos con
        `exited{suspending}`; las terminales siguen vivas."""
        for pty in list(self.ptys.values()):
            if pty.alive:
                pty.suspend()

    def live_ptys(self) -> list[FakePty]:
        return [pty for pty in self.ptys.values() if pty.alive]

    # -------------------------------------------------------------- internals

    def _authenticate(self, context: grpc.ServicerContext, rpc: str) -> dict[str, str]:
        self.peers.add(str(context.peer()))
        self.deadlines.setdefault(rpc, []).append(remaining_deadline(context))
        return require_access_token(context, self.token_sha256)

    def _gate_phase(self, context: grpc.ServicerContext) -> None:
        if self.phase is not None:
            context.abort(grpc.StatusCode.UNAVAILABLE, self.phase)

    def _validate_create(self, request: pty_pb2.PtyStart, context: grpc.ServicerContext) -> str:
        if request.HasField("size") and not valid_dimensions(request.size):
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid pty size")
        shell = str(request.shell) if request.HasField("shell") else DEFAULT_SHELL
        if not shell.startswith("/"):
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "shell must be an absolute path")
        if request.HasField("user") and request.user.username == "root" and not self.allow_root:
            context.abort(grpc.StatusCode.PERMISSION_DENIED, "root is not allowed")
        if request.HasField("cwd") and request.cwd not in KNOWN_DIRECTORIES:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "cwd is not a directory")
        if not self.pty_devices:
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, "pty devices unavailable")
        if self.processes.live_count() >= MAX_LIVE_PROCESSES:
            context.abort(
                grpc.StatusCode.RESOURCE_EXHAUSTED, f"max {MAX_LIVE_PROCESSES} live processes"
            )
        return shell

    def _live(self, pid: int, context: grpc.ServicerContext) -> FakePty:
        self._refuse_process_pid(pid, context)
        pty = self.ptys.get(pid)
        if pty is None or not pty.alive:
            context.abort(grpc.StatusCode.NOT_FOUND, f"pid {pid} not found")
        return pty

    def _retained(self, pid: int, context: grpc.ServicerContext) -> FakePty:
        self._refuse_process_pid(pid, context)
        pty = self.ptys.get(pid)
        if pty is None or not (pty.alive or self._within_retention(pty)):
            context.abort(grpc.StatusCode.NOT_FOUND, f"pid {pid} not found")
        return pty

    def _refuse_process_pid(self, pid: int, context: grpc.ServicerContext) -> None:
        if pid in self.processes.processes:
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, f"pid {pid} is not a PTY")

    def _within_retention(self, pty: FakePty) -> bool:
        return (
            pty.ended_at is not None
            and time.monotonic() - pty.ended_at < self.processes.retention_seconds
        )


def remaining_deadline(context: grpc.ServicerContext) -> float | None:
    """grpc devuelve un valor enorme cuando el RPC no lleva deadline."""
    remaining = context.time_remaining()
    if remaining is None or remaining > NO_DEADLINE_SECONDS:
        return None
    return float(remaining)


def valid_dimensions(size: pty_pb2.PtySize) -> bool:
    return 1 <= int(size.cols) <= MAX_DIMENSION and 1 <= int(size.rows) <= MAX_DIMENSION
