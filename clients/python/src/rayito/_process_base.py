"""Núcleo puro de `commands`, compartido por `Sandbox` y `AsyncSandbox`:
construcción de `StartRequest`, aritmética de deadlines, decodificación
incremental de la salida, el estado de un `CommandHandle` (y de un
`PtyHandle`, a través de un `StreamAdapter`) y el mapeo de `EndEvent` y de
fallos de stream a resultado o excepción. Sin I/O.
"""

from __future__ import annotations

import codecs
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Protocol

import grpc
import grpc.aio

from rayito._limits import SUSPENDED_STATES, TERMINAL_STATES
from rayito._models import CommandResult, ProcessInfo, ProcessKindName, SandboxMetrics
from rayito._payload import validated_envs
from rayito._transport import (
    SANDBOX_TIMEOUT_DETAIL,
    SANDBOX_TIMEOUT_MESSAGE,
    is_stream_reset,
    rpc_details,
    rpc_status,
    translate_rpc_error,
    translate_stream_error,
)
from rayito.exceptions import (
    CommandExitException,
    InvalidArgumentException,
    SandboxException,
    SandboxNotFoundException,
    SandboxStateException,
    TimeoutException,
)
from rayito.v1 import process_pb2

DEFAULT_COMMAND_TIMEOUT_SECONDS: Final = 60.0
STREAM_DEADLINE_GRACE_SECONDS: Final = 5.0
STREAM_PROBE_TIMEOUT_SECONDS: Final = 5.0
SIGKILL: Final = 9
SHELL: Final = "/bin/bash"
SHELL_ARGS: Final = ("-l", "-c")
PID_MAX: Final = 2**32 - 1

STATUS_EXITED: Final = "exited"
STATUS_SIGNALED: Final = "signaled"
STATUS_TIMEOUT: Final = "timeout"
STATUS_SUSPENDING: Final = "suspending"
STATUS_OUTPUT_TRUNCATED: Final = "output_truncated"
STATUS_SANDBOX_TIMEOUT: Final = SANDBOX_TIMEOUT_DETAIL

STREAM_EOF: Final[object] = grpc.aio.EOF  # type: ignore[attr-defined]

OutputCallback = Callable[[str], None]
CommandOutcome = CommandResult | Exception
OutputChunk = tuple[str | None, str | None, bytes | None]
Monotonic = Callable[[], float]


def build_start_request(
    cmd: str,
    *,
    envs: Mapping[str, str] | None = None,
    user: str | None = None,
    cwd: str | None = None,
    stdin: bool = False,
    timeout: float | None = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    tag: str | None = None,
) -> process_pb2.StartRequest:
    """`ProcessConfig{cmd:"/bin/bash", args:["-l","-c", cmd]}` con el
    `timeout_ms` que impone el servidor. `user=""` y `cwd=""` se omiten como
    si fueran `None`; el servidor resuelve los defaults del `/run` payload."""
    if not isinstance(cmd, str) or not cmd.strip():
        raise InvalidArgumentException("cmd no puede estar vacío")
    config = process_pb2.ProcessConfig(cmd=SHELL, args=[*SHELL_ARGS, cmd])
    if envs:
        config.envs.update(validated_envs(envs))
    if cwd:
        config.cwd = cwd
    request = process_pb2.StartRequest(
        process=config, timeout_ms=timeout_to_ms(timeout), stdin=bool(stdin)
    )
    if user:
        request.user.username = user
    if tag is not None:
        request.tag = tag
    return request


def timeout_to_ms(timeout: float | None) -> int:
    """`None` y `0` significan sin límite; negativo es un error del caller."""
    if timeout is None:
        return 0
    if isinstance(timeout, bool) or not isinstance(timeout, int | float):
        raise InvalidArgumentException(
            f"timeout debe ser un número de segundos o None, recibido {timeout!r}"
        )
    if timeout < 0:
        raise InvalidArgumentException(f"timeout no puede ser negativo, recibido {timeout!r}")
    if timeout == 0:
        return 0
    return max(1, round(timeout * 1000))


def stream_deadline(timeout: float | None) -> float | None:
    """Deadline gRPC de `Start`: `timeout + 5 s`, para que el `EndEvent` del
    servidor llegue antes que el `DEADLINE_EXCEEDED` del cliente."""
    if timeout is None or timeout_to_ms(timeout) == 0:
        return None
    return timeout + STREAM_DEADLINE_GRACE_SECONDS


def deadline_at(deadline: float | None, monotonic: Monotonic) -> float | None:
    """Instante monotónico en el que vence un deadline gRPC; `None` si no hay."""
    return None if deadline is None else monotonic() + deadline


def remaining_deadline(at: float | None, monotonic: Monotonic) -> float | None:
    """Lo que le queda a un stream re-emitido tras una reconexión. El deadline
    del cliente corre en el reloj del cliente (una pausa no lo alarga); `0`
    significa que ya venció."""
    if at is None:
        return None
    return max(0.0, at - monotonic())


def validate_pid(pid: object) -> int:
    if isinstance(pid, bool) or not isinstance(pid, int) or not 1 <= pid <= PID_MAX:
        raise InvalidArgumentException(
            f"pid debe ser un entero entre 1 y {PID_MAX}, recibido {pid!r}"
        )
    return pid


def validate_from_seq(from_seq: object) -> int:
    if isinstance(from_seq, bool) or not isinstance(from_seq, int) or from_seq < 0:
        raise InvalidArgumentException(f"from_seq debe ser un entero >= 0, recibido {from_seq!r}")
    return from_seq


def encode_stdin(data: str | bytes | bytearray) -> bytes:
    if isinstance(data, str):
        return data.encode("utf-8")
    if isinstance(data, bytes | bytearray | memoryview):
        return bytes(data)
    raise InvalidArgumentException(f"stdin acepta str o bytes, recibido {type(data).__name__}")


def pid_from_start_event(event: process_pb2.ProcessEvent) -> int:
    """El primer mensaje de `Start` y `Connect` es siempre `StartEvent{pid}`."""
    if event.WhichOneof("event") != "start":
        raise SandboxException(
            f"el stream no empezó con StartEvent (llegó {event.WhichOneof('event')!r})"
        )
    return int(event.start.pid)


class DecodedStream:
    """Un stream de bytes decodificado incrementalmente a texto (UTF-8 con
    `errors="replace"`); un carácter partido entre dos chunks se reconstruye."""

    def __init__(self, callback: OutputCallback | None) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._callback = callback
        self._parts: list[str] = []

    @property
    def text(self) -> str:
        return "".join(self._parts)

    def feed(self, payload: bytes) -> str | None:
        return self._emit(self._decoder.decode(payload))

    def flush(self) -> None:
        self._emit(self._decoder.decode(b"", final=True))

    def _emit(self, text: str) -> str | None:
        if not text:
            return None
        self._parts.append(text)
        if self._callback is not None:
            self._callback(text)
        return text


class OutputAccumulator:
    """Salida acumulada de un proceso: un `DecodedStream` por descriptor, el
    último `seq` visto (para `Connect(from_seq)`) y el cierre en `finish`."""

    def __init__(
        self,
        *,
        on_stdout: OutputCallback | None = None,
        on_stderr: OutputCallback | None = None,
    ) -> None:
        self._stdout = DecodedStream(on_stdout)
        self._stderr = DecodedStream(on_stderr)
        self._last_seq = 0

    @property
    def last_seq(self) -> int:
        return self._last_seq

    @property
    def stdout(self) -> str:
        return self._stdout.text

    @property
    def stderr(self) -> str:
        return self._stderr.text

    def feed(self, data: process_pb2.DataEvent) -> tuple[str | None, str | None]:
        """Decodifica un `DataEvent`; devuelve el texto nuevo de cada stream."""
        self._last_seq = max(self._last_seq, int(data.seq))
        if data.WhichOneof("output") == "stderr":
            return None, self._stderr.feed(bytes(data.stderr))
        return self._stdout.feed(bytes(data.stdout)), None

    def feed_terminal(self, seq: int, payload: bytes) -> str | None:
        """Bytes crudos de una PTY: van al decoder de stdout (una terminal no
        distingue descriptores) y avanzan `last_seq`."""
        self._last_seq = max(self._last_seq, int(seq))
        return self._stdout.feed(payload)

    def finish(self, end: process_pb2.EndEvent) -> CommandOutcome:
        self._stdout.flush()
        self._stderr.flush()
        return outcome_from_end(end, self.stdout, self.stderr)


def outcome_from_end(end: process_pb2.EndEvent, stdout: str, stderr: str) -> CommandOutcome:
    """Tabla cerrada de `EndEvent.status`: `exited`/`signaled` con exit 0 →
    `CommandResult`; distinto de cero → `CommandExitException`; `timeout` →
    `TimeoutException`; `output_truncated` → `SandboxException`; `suspending`
    → `SandboxStateException`; `sandbox_timeout` (el plazo lógico del
    sandbox venció, ADR-011) → `TimeoutException`. Un status desconocido con
    `error` sigue la tabla de `StreamError.code`."""
    status = str(end.status)
    exit_code = int(end.exit_code)
    detail = end_error_message(end)
    if status == STATUS_SANDBOX_TIMEOUT:
        return TimeoutException(SANDBOX_TIMEOUT_MESSAGE)
    if status == STATUS_TIMEOUT:
        return TimeoutException(
            f"el comando superó su timeout y fue terminado por el agente "
            f"(exit_code={exit_code}, signal={int(end.signal)}): {detail}"
        )
    if status == STATUS_OUTPUT_TRUNCATED:
        return SandboxException(
            f"output_truncated: el agente descartó este suscriptor ({detail}); el proceso "
            "sigue vivo, reconecta con commands.connect(pid, from_seq=last_seq + 1)"
        )
    if status == STATUS_SUSPENDING:
        return SandboxStateException(f"el sandbox se está suspendiendo: {detail}")
    if status not in (STATUS_EXITED, STATUS_SIGNALED) and end.HasField("error"):
        return translate_stream_error(str(end.error.code), str(end.error.message))
    if exit_code == 0:
        return CommandResult(stdout=stdout, stderr=stderr, exit_code=0, error=None)
    return CommandExitException(
        f"el comando terminó con exit_code={exit_code} ({status})",
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        error=status,
    )


def end_error_message(end: process_pb2.EndEvent) -> str:
    if end.HasField("error") and end.error.message:
        return str(end.error.message)
    return str(end.status)


def end_error_name(end: process_pb2.EndEvent | None) -> str | None:
    """Lo que expone `CommandHandle.error`: el `StreamError.code` si lo hubo,
    el status para un exit distinto de cero, `None` para un exit 0."""
    if end is None:
        return None
    if end.HasField("error"):
        return str(end.error.code)
    if int(end.exit_code) != 0:
        return str(end.status)
    return None


@dataclass(frozen=True)
class Chunk:
    """Salida nueva de un mensaje del stream: texto por descriptor para un
    proceso, bytes crudos (y su decodificación en `stdout`) para una PTY."""

    seq: int
    stdout: str | None = None
    stderr: str | None = None
    pty: bytes | None = None

    def as_output(self) -> OutputChunk:
        """Lo que entrega el iterador: `(stdout, stderr, None)` para un
        proceso y `(None, None, bytes)` para una PTY (el texto decodificado
        de la terminal sólo vive en `handle.stdout`)."""
        if self.pty is not None:
            return None, None, self.pty
        return self.stdout, self.stderr, None


@dataclass(frozen=True)
class WaitCallbacks:
    """Los callbacks de `wait(on_pty, on_stdout, on_stderr)` (contrato de
    E2B 2.x): reciben cada chunk que `wait()` consume, después de los de
    `run()`, y nunca los ya consumidos antes. Pueden devolver algo; el handle
    async espera los resultados awaitables."""

    on_pty: Callable[[bytes], Any] | None = None
    on_stdout: Callable[[str], Any] | None = None
    on_stderr: Callable[[str], Any] | None = None

    def deliver(self, chunk: OutputChunk) -> list[Any]:
        stdout, stderr, pty = chunk
        results: list[Any] = []
        if stdout is not None and self.on_stdout is not None:
            results.append(self.on_stdout(stdout))
        if stderr is not None and self.on_stderr is not None:
            results.append(self.on_stderr(stderr))
        if pty is not None and self.on_pty is not None:
            results.append(self.on_pty(pty))
        return results


@dataclass(frozen=True)
class Nothing:
    """Un `keepalive`, un `StartEvent`/`started` o un chunk sin caracteres completos."""


@dataclass(frozen=True)
class Ended:
    end: process_pb2.EndEvent


@dataclass(frozen=True)
class Suspending:
    """Un final en-stream con `status == "suspending"`: el agente cerró el
    stream porque el sandbox se suspende; el proceso sigue vivo y el handle
    debe reengancharse tras el `/resume`."""

    end: process_pb2.EndEvent


Consumed = Chunk | Nothing | Ended | Suspending
NOTHING: Final = Nothing()


class StreamAdapter(Protocol):
    """Traduce los mensajes de un stream (`ProcessEvent` o `PtyServerMessage`)
    al vocabulario común de `CommandProgress`."""

    def pid(self, first: Any) -> int: ...

    def consume(self, message: Any, accumulator: OutputAccumulator) -> Consumed: ...


def consumed_end(end: process_pb2.EndEvent) -> Consumed:
    if str(end.status) == STATUS_SUSPENDING:
        return Suspending(end)
    return Ended(end)


class ProcessEvents:
    """`StreamAdapter` de `ProcessService.Start`/`Connect`."""

    def pid(self, first: process_pb2.ProcessEvent) -> int:
        return pid_from_start_event(first)

    def consume(
        self, message: process_pb2.ProcessEvent, accumulator: OutputAccumulator
    ) -> Consumed:
        kind = message.WhichOneof("event")
        if kind == "data":
            stdout, stderr = accumulator.feed(message.data)
            if stdout is None and stderr is None:
                return NOTHING
            return Chunk(seq=int(message.data.seq), stdout=stdout, stderr=stderr)
        if kind == "end":
            return consumed_end(message.end)
        return NOTHING


class CommandProgress:
    """Estado de un `CommandHandle`, idéntico en sync y async: salida
    acumulada, `EndEvent` recibido, resultado o excepción final, si el
    handle fue desconectado a propósito y si el último mensaje fue un final
    `suspending` pendiente de reconexión."""

    def __init__(
        self,
        pid: int,
        accumulator: OutputAccumulator,
        adapter: StreamAdapter | None = None,
    ) -> None:
        self.pid = pid
        self.accumulator = accumulator
        self.adapter: StreamAdapter = adapter if adapter is not None else ProcessEvents()
        self.end: process_pb2.EndEvent | None = None
        self.outcome: CommandOutcome | None = None
        self.disconnected = False
        self.suspended = False

    def is_finished(self) -> bool:
        return self.outcome is not None

    @property
    def exit_code(self) -> int | None:
        return None if self.end is None else int(self.end.exit_code)

    @property
    def error(self) -> str | None:
        return end_error_name(self.end)

    def consume(self, message: Any) -> Consumed:
        """Aplica un mensaje del stream. Un `Ended` cierra el handle; un
        `Suspending` sólo lo marca (`suspended`) para que el consumidor
        reconecte; el resto no cambia el estado final."""
        consumed = self.adapter.consume(message, self.accumulator)
        if isinstance(consumed, Ended):
            self.end = consumed.end
            self.outcome = self.accumulator.finish(consumed.end)
        elif isinstance(consumed, Suspending):
            self.suspended = True
        return consumed

    def resubscribed(self) -> None:
        self.suspended = False

    def fail(self, failure: Exception) -> Exception:
        self.outcome = failure
        return failure

    def resolve(self) -> CommandResult:
        """Resultado final tras consumir el stream; idempotente."""
        if self.outcome is None:
            self.outcome = self._missing_outcome()
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    def _missing_outcome(self) -> Exception:
        if self.disconnected:
            return SandboxException(
                f"el handle del pid {self.pid} está desconectado; vuelve con commands.connect(pid)"
            )
        return SandboxException(f"el stream del pid {self.pid} terminó sin EndEvent")


def suspending_reason(progress: CommandProgress) -> Exception:
    """La excepción que representa el final `suspending` de un handle: es lo
    que se pasa a `_reconnect` como motivo y lo que queda si reconectar es
    imposible."""
    return SandboxStateException(
        f"el sandbox se está suspendiendo; el stream del pid {progress.pid} fue cerrado"
    )


def resubscribe_from_seq(last_seq: int) -> int:
    """Tras una reconexión se pide todo lo que no se vio: `last_seq + 1`."""
    return last_seq + 1


def process_info_from_proto(info: process_pb2.ProcessInfo) -> ProcessInfo:
    kind: ProcessKindName = "pty" if info.kind == process_pb2.PROCESS_KIND_PTY else "process"
    config = info.config
    return ProcessInfo(
        pid=int(info.pid),
        cmd=str(config.cmd),
        args=tuple(str(arg) for arg in config.args),
        envs={str(key): str(value) for key, value in config.envs.items()},
        cwd=str(config.cwd) if config.HasField("cwd") else None,
        tag=str(info.tag) if info.HasField("tag") else None,
        kind=kind,
    )


def metrics_from_proto(response: Any) -> SandboxMetrics:
    return SandboxMetrics(
        cpu_used_pct=float(response.cpu_used_pct),
        mem_used_bytes=int(response.mem_used_bytes),
        mem_total_bytes=int(response.mem_total_bytes),
        disk_used_bytes=int(response.disk_used_bytes),
        disk_total_bytes=int(response.disk_total_bytes),
        cpu_count=int(response.cpu_count),
        timestamp=datetime.fromtimestamp(int(response.timestamp_unix_ms) / 1000, tz=UTC),
        mem_cache_bytes=int(response.mem_cache_bytes),
    )


def stream_failure_exception(
    exc: grpc.RpcError, *, health_ok: bool, state: str | None
) -> Exception:
    """Clasifica un fallo de stream con lo que el sandbox ya averiguó: si no
    es un reset, la tabla unaria; si `Health` respondió, el sandbox vive y el
    cliente puede reengancharse; si no, el estado de `get-microvm` decide."""
    if not is_stream_reset(exc):
        return translate_rpc_error(exc)
    code = rpc_status(exc)
    detail = rpc_details(exc)
    if health_ok:
        return SandboxException(
            f"stream cortado ({detail}) pero el sandbox responde; reconecta con "
            "commands.connect(pid)",
            grpc_code=code,
        )
    if state in TERMINAL_STATES:
        return SandboxNotFoundException(
            f"el sandbox está {state}: stream cortado ({detail})", grpc_code=code
        )
    if state in SUSPENDED_STATES:
        return SandboxStateException(
            f"el sandbox está {state}: stream cortado ({detail})", grpc_code=code
        )
    return SandboxException(
        f"stream cortado ({detail}) y el agente no responde (estado {state or 'desconocido'})",
        grpc_code=code,
    )
