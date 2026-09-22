"""`Sandbox.commands`: `ProcessService` en foreground y background sobre `grpc`.

Foreground (`run`) abre el stream en el canal de unarios y lo consume en el
hilo que llama; background y `connect` usan el segundo canal, el de streams
largos. Un handle que nadie lee queda sujeto a la regla de estancamiento del
agente: 30 s con el canal lleno terminan ese suscriptor con
`output_truncated` (el proceso sigue vivo). Un handle cuyo stream cierra un
`/suspend` (final `suspending`) o un corte del proxy espera a que el agente
vuelva y se reengancha con `Connect(pid, from_seq=last_seq + 1)` sin que el
consumidor note más que la latencia.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator, Mapping
from typing import TYPE_CHECKING, Any, Literal, overload

import grpc

from rayito._models import CommandResult, ProcessInfo
from rayito._process_base import (
    DEFAULT_COMMAND_TIMEOUT_SECONDS,
    SIGKILL,
    Chunk,
    CommandProgress,
    OutputAccumulator,
    OutputCallback,
    OutputChunk,
    build_start_request,
    deadline_at,
    encode_stdin,
    pid_from_start_event,
    process_info_from_proto,
    remaining_deadline,
    resubscribe_from_seq,
    stream_deadline,
    suspending_reason,
    validate_from_seq,
    validate_pid,
)
from rayito._sandbox_base import GateRetry, ReconnectBudget
from rayito.exceptions import NotFoundException, SandboxException, TimeoutException
from rayito.v1 import process_pb2, process_pb2_grpc

if TYPE_CHECKING:
    from rayito.sandbox_sync.main import Sandbox

logger = logging.getLogger("rayito.commands")

StreamStarter = Callable[[Any], Any]
PROCESS_STUB = process_pb2_grpc.ProcessServiceStub


class Commands:
    """Comandos del sandbox (`ProcessService`)."""

    def __init__(self, sandbox: Sandbox) -> None:
        self._sandbox = sandbox

    @overload
    def run(
        self,
        cmd: str,
        *,
        background: Literal[False] = False,
        envs: Mapping[str, str] | None = None,
        user: str | None = None,
        cwd: str | None = None,
        on_stdout: OutputCallback | None = None,
        on_stderr: OutputCallback | None = None,
        stdin: bool = False,
        timeout: float | None = DEFAULT_COMMAND_TIMEOUT_SECONDS,
        request_timeout: float | None = None,
        tag: str | None = None,
    ) -> CommandResult: ...

    @overload
    def run(
        self,
        cmd: str,
        *,
        background: Literal[True],
        envs: Mapping[str, str] | None = None,
        user: str | None = None,
        cwd: str | None = None,
        on_stdout: OutputCallback | None = None,
        on_stderr: OutputCallback | None = None,
        stdin: bool = False,
        timeout: float | None = DEFAULT_COMMAND_TIMEOUT_SECONDS,
        request_timeout: float | None = None,
        tag: str | None = None,
    ) -> CommandHandle: ...

    @overload
    def run(
        self,
        cmd: str,
        *,
        background: bool,
        envs: Mapping[str, str] | None = None,
        user: str | None = None,
        cwd: str | None = None,
        on_stdout: OutputCallback | None = None,
        on_stderr: OutputCallback | None = None,
        stdin: bool = False,
        timeout: float | None = DEFAULT_COMMAND_TIMEOUT_SECONDS,
        request_timeout: float | None = None,
        tag: str | None = None,
    ) -> CommandResult | CommandHandle: ...

    def run(
        self,
        cmd: str,
        *,
        background: bool = False,
        envs: Mapping[str, str] | None = None,
        user: str | None = None,
        cwd: str | None = None,
        on_stdout: OutputCallback | None = None,
        on_stderr: OutputCallback | None = None,
        stdin: bool = False,
        timeout: float | None = DEFAULT_COMMAND_TIMEOUT_SECONDS,
        request_timeout: float | None = None,
        tag: str | None = None,
    ) -> CommandResult | CommandHandle:
        """Ejecuta `cmd` con `/bin/bash -l -c` como `user` (uid 1000 por defecto).

        `timeout` lo impone el agente sobre el reloj corrido (SIGTERM al
        vencer, SIGKILL 5 s después; una pausa no lo consume) y por defecto
        son 60 s; `None` o `0` lo desactivan. En foreground el resultado
        llega con la salida completa y un exit distinto de cero es
        `CommandExitException`; el timeout es `TimeoutException`. En background
        devuelve un `CommandHandle` que consume el stream cuando se itera o se
        llama a `wait()`: mientras nadie lo lee, el agente aplica backpressure
        y, tras 30 s con el canal lleno, cierra ese suscriptor con
        `output_truncated`. Un stream abierto cuenta como actividad para la
        política de idle del MicroVM. `request_timeout` acota los unarios que
        el handle haga después (`kill`, `send_stdin`, `close_stdin`).
        """
        request = build_start_request(
            cmd, envs=envs, user=user, cwd=cwd, stdin=stdin, timeout=timeout, tag=tag
        )
        deadline = stream_deadline(timeout)
        handle = self._attach(
            lambda stub: stub.Start(request, timeout=deadline),
            stream=background,
            deadline=deadline,
            on_stdout=on_stdout,
            on_stderr=on_stderr,
            request_timeout=request_timeout,
            foreground=not background,
        )
        return handle if background else handle.wait()

    def connect(
        self,
        pid: int,
        *,
        from_seq: int = 0,
        on_stdout: OutputCallback | None = None,
        on_stderr: OutputCallback | None = None,
        timeout: float | None = None,
        request_timeout: float | None = None,
    ) -> CommandHandle:
        """Se engancha a un proceso vivo o terminado hace menos de 30 s.

        `from_seq=0` entrega sólo salida nueva; `N` reenvía primero lo retenido
        con `seq >= N` (último MiB). Un `seq` ya descartado o un pid
        desconocido es `NotFoundException`. `timeout` es aquí el deadline gRPC
        del stream (no hay timeout de servidor en `Connect`).
        """
        return self._attach(
            self._connect_starter(pid, from_seq, timeout),
            stream=True,
            deadline=timeout,
            on_stdout=on_stdout,
            on_stderr=on_stderr,
            request_timeout=request_timeout,
        )

    def list(self, *, request_timeout: float | None = None) -> list[ProcessInfo]:
        """Procesos y PTY vivos (los terminados desaparecen aunque sigan conectables)."""
        response = self._sandbox._process_call(
            lambda stub, timeout: stub.List(process_pb2.ListRequest(), timeout=timeout),
            request_timeout,
        )
        return [process_info_from_proto(info) for info in response.processes]

    def kill(self, pid: int, *, request_timeout: float | None = None) -> bool:
        """SIGKILL al grupo del proceso (o de la PTY). False si el pid no
        existe o ya terminó."""
        request = process_pb2.SendSignalRequest(pid=validate_pid(pid), signal=SIGKILL)
        try:
            self._sandbox._process_call(
                lambda stub, timeout: stub.SendSignal(request, timeout=timeout), request_timeout
            )
        except NotFoundException:
            return False
        return True

    def send_stdin(
        self, pid: int, data: str | bytes, *, request_timeout: float | None = None
    ) -> None:
        """Escribe en el stdin de un proceso lanzado con `stdin=True` (`str` va
        en UTF-8). Un pid de PTY es `InvalidArgumentException`: usa `pty.send_input`."""
        request = process_pb2.SendInputRequest(pid=validate_pid(pid), data=encode_stdin(data))
        self._sandbox._process_call(
            lambda stub, timeout: stub.SendInput(request, timeout=timeout), request_timeout
        )

    def close_stdin(self, pid: int, *, request_timeout: float | None = None) -> None:
        """EOF en el stdin del proceso; idempotente."""
        request = process_pb2.CloseStdinRequest(pid=validate_pid(pid))
        self._sandbox._process_call(
            lambda stub, timeout: stub.CloseStdin(request, timeout=timeout), request_timeout
        )

    def _connect_starter(self, pid: int, from_seq: int, timeout: float | None) -> StreamStarter:
        request = process_pb2.ConnectRequest(
            pid=validate_pid(pid), from_seq=validate_from_seq(from_seq)
        )
        return lambda stub: stub.Connect(request, timeout=timeout)

    def _open_connect(self, pid: int, from_seq: int, timeout: float | None) -> Any:
        """`Connect` ya consumido hasta su `StartEvent`; es lo que un handle
        usa para reengancharse conservando su estado."""
        call, first = self._sandbox._open_stream(
            self._connect_starter(pid, from_seq, timeout), service=PROCESS_STUB, stream=True
        )
        if pid_from_start_event(first) != pid:
            raise SandboxException(f"Connect({pid}) respondió con otro pid")
        return call

    def _attach(
        self,
        start: StreamStarter,
        *,
        stream: bool,
        deadline: float | None,
        on_stdout: OutputCallback | None,
        on_stderr: OutputCallback | None,
        request_timeout: float | None,
        foreground: bool = False,
    ) -> CommandHandle:
        call, first = self._sandbox._open_stream(start, service=PROCESS_STUB, stream=stream)
        accumulator = OutputAccumulator(on_stdout=on_stdout, on_stderr=on_stderr)
        progress = CommandProgress(pid_from_start_event(first), accumulator)
        return CommandHandle(
            commands=self,
            call=call,
            progress=progress,
            request_timeout=request_timeout,
            deadline_at=deadline_at(deadline, time.monotonic),
            foreground=foreground,
        )

    def _stream_failure(self, exc: grpc.RpcError) -> Exception:
        return self._sandbox._stream_failure(exc)


class CommandHandle:
    """Un proceso en background o enganchado con `connect`.

    Consume el stream perezosamente en el hilo que itera o llama a `wait()`;
    `disconnect()` cancela el stream sin tocar el proceso, que sigue vivo y
    listado hasta que termine o alguien lo mate. Si el agente cierra el
    stream por un `/suspend` o el proxy lo corta, la siguiente lectura espera
    al agente y se reengancha con `Connect(pid, from_seq=last_seq + 1)`;
    `reconnects` cuenta esas veces. Leer un handle en background nunca
    despierta un sandbox suspendido: la lectura se bloquea (sin consumir
    `reconnect_timeout`) hasta el `resume()`, el auto-resume que provoque
    otra llamada o el fin del MicroVM.
    """

    def __init__(
        self,
        *,
        commands: Commands,
        call: Any,
        progress: CommandProgress,
        request_timeout: float | None,
        deadline_at: float | None = None,
        foreground: bool = False,
    ) -> None:
        self._commands = commands
        self._call = call
        self._progress = progress
        self._request_timeout = request_timeout
        self._deadline_at = deadline_at
        self._foreground = foreground
        self._generation = commands._sandbox.resume_generation
        self._pending_cut: Exception | None = None
        self._reconnects = 0
        self._budget = ReconnectBudget()

    @property
    def pid(self) -> int:
        return self._progress.pid

    @property
    def last_seq(self) -> int:
        """Último `seq` recibido; `connect(pid, from_seq=last_seq + 1)` reanuda sin huecos."""
        return self._progress.accumulator.last_seq

    @property
    def stdout(self) -> str:
        return self._progress.accumulator.stdout

    @property
    def stderr(self) -> str:
        return self._progress.accumulator.stderr

    @property
    def exit_code(self) -> int | None:
        return self._progress.exit_code

    @property
    def error(self) -> str | None:
        return self._progress.error

    @property
    def reconnects(self) -> int:
        """Veces que el handle se reenganchó tras un suspend/resume o un corte."""
        return self._reconnects

    def wait(self) -> CommandResult:
        """Consume el stream hasta el `EndEvent`. Exit distinto de cero →
        `CommandExitException`; timeout del agente → `TimeoutException`;
        `output_truncated` → `SandboxException`. Idempotente."""
        for _ in self:
            pass
        return self._progress.resolve()

    def kill(self) -> bool:
        return self._commands.kill(self.pid, request_timeout=self._request_timeout)

    def disconnect(self) -> None:
        """Cancela el stream; el proceso sigue corriendo en el sandbox. Un
        handle desconectado nunca reconecta."""
        if self._progress.disconnected:
            return
        self._progress.disconnected = True
        self._call.cancel()

    def send_stdin(self, data: str | bytes) -> None:
        self._commands.send_stdin(self.pid, data, request_timeout=self._request_timeout)

    def close_stdin(self) -> None:
        self._commands.close_stdin(self.pid, request_timeout=self._request_timeout)

    def __iter__(self) -> Iterator[OutputChunk]:
        """`(stdout, stderr, pty)` por chunk decodificado; `pty` es siempre
        `None` en comandos y los keepalives se saltan. Un corte reconectable
        se resuelve aquí mismo: se espera al agente y se sigue iterando."""
        progress = self._progress
        while not (progress.is_finished() or progress.disconnected):
            yield from self._consume_stream()
            if progress.is_finished() or progress.disconnected or not self._cut():
                return
            self._resubscribe_after_cut()

    def _cut(self) -> bool:
        """True si el stream actual terminó por un `suspending` o un corte
        reconectable; False si terminó limpiamente (con o sin `EndEvent`)."""
        return self._progress.suspended or self._pending_cut is not None

    def _consume_stream(self) -> Iterator[OutputChunk]:
        """Itera el stream actual hasta el final, un `suspending` o un corte
        reconectable (que queda en `_pending_cut`). Un `disconnect()` desde
        otro hilo aborta con `CANCELLED`; eso es fin, no un fallo."""
        progress = self._progress
        try:
            for event in self._call:
                consumed = progress.consume(event)
                if isinstance(consumed, Chunk):
                    yield consumed.as_output()
                if progress.is_finished() or progress.suspended:
                    return
        except grpc.RpcError as exc:
            if progress.disconnected:
                return
            if self._sandbox._is_reconnectable(exc):
                self._pending_cut = exc
                return
            raise progress.fail(self._commands._stream_failure(exc)) from exc

    def _resubscribe_after_cut(self) -> None:
        """Espera al agente y reabre el stream desde `last_seq + 1`; si no
        vuelve, la excepción del sondeo es el resultado del handle. Una
        resuscripción rechazada por el phase gate (`UNAVAILABLE suspending`:
        el agente vive con el gate aún cerrado tras un `/suspend` que nadie
        checkpointeó) se reintenta con el backoff de `ReconnectPoll` dentro
        de `reconnect_timeout`."""
        reason = self._pending_cut or suspending_reason(self._progress)
        self._pending_cut = None
        outcome = self._sandbox._reconnect(reason, self._generation, wake=self._wakes())
        if not outcome.resumed:
            raise self._progress.fail(self._sandbox._reconnect_error(outcome, reason)) from reason
        self._generation = outcome.resume_generation
        if self._progress.disconnected:
            return
        if not self._budget.allows(outcome):
            raise self._progress.fail(self._futile_cut(reason)) from reason
        self._call = self._resubscribe_through_the_gate()
        self._progress.resubscribed()
        self._reconnects += 1

    def _resubscribe_through_the_gate(self) -> Any:
        retry = GateRetry(self._sandbox._reconnect_timeout)
        while True:
            try:
                return self._resubscribe(resubscribe_from_seq(self.last_seq))
            except NotFoundException as exc:
                if exc.grpc_code is not grpc.StatusCode.OUT_OF_RANGE:
                    raise self._progress.fail(exc) from exc
                return self._resubscribe_from_live(exc)
            except SandboxException as exc:
                delay = retry.retry_delay(exc)
                if delay is None:
                    raise self._progress.fail(exc) from exc
                logger.info(
                    "pid %s: el gate del agente sigue cerrado (%s); reintento", self.pid, exc
                )
                time.sleep(delay)

    def _futile_cut(self, reason: Exception) -> Exception:
        """Cortes repetidos sin resume de por medio: la clasificación de M2."""
        if isinstance(reason, grpc.RpcError):
            return self._commands._stream_failure(reason)
        return reason

    def _resubscribe_from_live(self, exc: NotFoundException) -> Any:
        logger.warning(
            "pid %s: se perdió salida entre el seq %s y lo que el agente retiene; "
            "se sigue desde la salida nueva",
            self.pid,
            self.last_seq,
        )
        try:
            return self._resubscribe(0)
        except NotFoundException as retry:
            raise self._progress.fail(retry) from exc

    def _resubscribe(self, from_seq: int) -> Any:
        """`Connect(pid, from_seq)` con lo que quede del deadline original.
        `PtyHandle` lo redefine sobre `PtyService.Connect`."""
        remaining = self._remaining_deadline()
        return self._commands._open_connect(self.pid, from_seq, remaining)

    def _remaining_deadline(self) -> float | None:
        remaining = remaining_deadline(self._deadline_at, time.monotonic)
        if remaining is not None and remaining <= 0.0:
            raise self._progress.fail(
                TimeoutException(
                    f"el deadline del stream del pid {self.pid} venció durante la reconexión"
                )
            )
        return remaining

    def _wakes(self) -> bool:
        """Sólo un `run` en foreground puede despertar al VM, y no mientras el
        sandbox tenga una pausa pendiente."""
        return self._foreground and self._sandbox._foreground_stream_wakes()

    @property
    def _sandbox(self) -> Sandbox:
        return self._commands._sandbox

    def __repr__(self) -> str:
        return f"CommandHandle(pid={self.pid}, exit_code={self.exit_code!r})"
