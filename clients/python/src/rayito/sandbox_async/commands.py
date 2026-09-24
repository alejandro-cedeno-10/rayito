"""`AsyncSandbox.commands`: la misma superficie que `sandbox_sync.commands`
sobre `grpc.aio`. El stream se lee con `read()` (nunca mezclado con
`async for` sobre la misma llamada, que `grpc.aio` prohíbe). La reconexión
tras un suspend/resume es la de `CommandHandle` como corrutinas."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import AsyncIterator, Callable, Mapping
from typing import TYPE_CHECKING, Any, Literal, overload

import grpc

from rayito._models import CommandResult, ProcessInfo
from rayito._process_base import (
    DEFAULT_COMMAND_TIMEOUT_SECONDS,
    SIGKILL,
    STREAM_EOF,
    Chunk,
    CommandProgress,
    OutputAccumulator,
    OutputCallback,
    OutputChunk,
    WaitCallbacks,
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
    from rayito.sandbox_async.main import AsyncSandbox

logger = logging.getLogger("rayito.commands")

StreamStarter = Callable[[Any], Any]
PROCESS_STUB = process_pb2_grpc.ProcessServiceStub


class AsyncCommands:
    """Comandos del sandbox (`ProcessService`) como corrutinas."""

    def __init__(self, sandbox: AsyncSandbox) -> None:
        self._sandbox = sandbox

    @overload
    async def run(
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
    async def run(
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
    ) -> AsyncCommandHandle: ...

    @overload
    async def run(
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
    ) -> CommandResult | AsyncCommandHandle: ...

    async def run(
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
    ) -> CommandResult | AsyncCommandHandle:
        """Misma semántica que `Commands.run`; los callbacks corren en el loop."""
        request = build_start_request(
            cmd, envs=envs, user=user, cwd=cwd, stdin=stdin, timeout=timeout, tag=tag
        )
        deadline = stream_deadline(timeout)
        handle = await self._attach(
            lambda stub: stub.Start(request, timeout=deadline),
            stream=background,
            deadline=deadline,
            on_stdout=on_stdout,
            on_stderr=on_stderr,
            request_timeout=request_timeout,
            foreground=not background,
        )
        return handle if background else await handle.wait()

    async def connect(
        self,
        pid: int,
        *,
        from_seq: int = 0,
        on_stdout: OutputCallback | None = None,
        on_stderr: OutputCallback | None = None,
        timeout: float | None = None,
        request_timeout: float | None = None,
    ) -> AsyncCommandHandle:
        """Misma semántica que `Commands.connect`."""
        return await self._attach(
            self._connect_starter(pid, from_seq, timeout),
            stream=True,
            deadline=timeout,
            on_stdout=on_stdout,
            on_stderr=on_stderr,
            request_timeout=request_timeout,
        )

    async def list(self, *, request_timeout: float | None = None) -> list[ProcessInfo]:
        response = await self._sandbox._process_call(
            lambda stub, timeout: stub.List(process_pb2.ListRequest(), timeout=timeout),
            request_timeout,
        )
        return [process_info_from_proto(info) for info in response.processes]

    async def kill(self, pid: int, *, request_timeout: float | None = None) -> bool:
        request = process_pb2.SendSignalRequest(pid=validate_pid(pid), signal=SIGKILL)
        try:
            await self._sandbox._process_call(
                lambda stub, timeout: stub.SendSignal(request, timeout=timeout), request_timeout
            )
        except NotFoundException:
            return False
        return True

    async def send_stdin(
        self, pid: int, data: str | bytes, *, request_timeout: float | None = None
    ) -> None:
        request = process_pb2.SendInputRequest(pid=validate_pid(pid), data=encode_stdin(data))
        await self._sandbox._process_call(
            lambda stub, timeout: stub.SendInput(request, timeout=timeout), request_timeout
        )

    async def close_stdin(self, pid: int, *, request_timeout: float | None = None) -> None:
        request = process_pb2.CloseStdinRequest(pid=validate_pid(pid))
        await self._sandbox._process_call(
            lambda stub, timeout: stub.CloseStdin(request, timeout=timeout), request_timeout
        )

    def _connect_starter(self, pid: int, from_seq: int, timeout: float | None) -> StreamStarter:
        request = process_pb2.ConnectRequest(
            pid=validate_pid(pid), from_seq=validate_from_seq(from_seq)
        )
        return lambda stub: stub.Connect(request, timeout=timeout)

    async def _open_connect(self, pid: int, from_seq: int, timeout: float | None) -> Any:
        call, first = await self._sandbox._open_stream(
            self._connect_starter(pid, from_seq, timeout), service=PROCESS_STUB, stream=True
        )
        if pid_from_start_event(first) != pid:
            raise SandboxException(f"Connect({pid}) respondió con otro pid")
        return call

    async def _attach(
        self,
        start: StreamStarter,
        *,
        stream: bool,
        deadline: float | None,
        on_stdout: OutputCallback | None,
        on_stderr: OutputCallback | None,
        request_timeout: float | None,
        foreground: bool = False,
    ) -> AsyncCommandHandle:
        call, first = await self._sandbox._open_stream(start, service=PROCESS_STUB, stream=stream)
        accumulator = OutputAccumulator(on_stdout=on_stdout, on_stderr=on_stderr)
        progress = CommandProgress(pid_from_start_event(first), accumulator)
        return AsyncCommandHandle(
            commands=self,
            call=call,
            progress=progress,
            request_timeout=request_timeout,
            deadline_at=deadline_at(deadline, time.monotonic),
            foreground=foreground,
        )

    async def _stream_failure(self, exc: grpc.RpcError) -> Exception:
        return await self._sandbox._stream_failure(exc)


class AsyncCommandHandle:
    """`CommandHandle` sobre `grpc.aio`: `await wait()`, `async for` y
    `await kill()/send_stdin()/close_stdin()`; `disconnect()` es síncrono.
    Reconecta como `CommandHandle` tras un suspend/resume."""

    def __init__(
        self,
        *,
        commands: AsyncCommands,
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
        return self._reconnects

    async def wait(
        self,
        on_pty: Callable[[bytes], Any] | None = None,
        on_stdout: Callable[[str], Any] | None = None,
        on_stderr: Callable[[str], Any] | None = None,
    ) -> CommandResult:
        """Como `CommandHandle.wait`; los callbacks pueden ser síncronos o
        `async` (su resultado se espera si es awaitable antes del siguiente
        chunk)."""
        callbacks = WaitCallbacks(on_pty=on_pty, on_stdout=on_stdout, on_stderr=on_stderr)
        async for chunk in self:
            for result in callbacks.deliver(chunk):
                if inspect.isawaitable(result):
                    await result
        return self._progress.resolve()

    async def kill(self) -> bool:
        return await self._commands.kill(self.pid, request_timeout=self._request_timeout)

    def disconnect(self) -> None:
        if self._progress.disconnected:
            return
        self._progress.disconnected = True
        self._call.cancel()

    async def send_stdin(self, data: str | bytes) -> None:
        await self._commands.send_stdin(self.pid, data, request_timeout=self._request_timeout)

    async def close_stdin(self) -> None:
        await self._commands.close_stdin(self.pid, request_timeout=self._request_timeout)

    def __aiter__(self) -> AsyncIterator[OutputChunk]:
        return self._chunks()

    async def _chunks(self) -> AsyncIterator[OutputChunk]:
        progress = self._progress
        while not (progress.is_finished() or progress.disconnected):
            async for chunk in self._consume_stream():
                yield chunk
            if progress.is_finished() or progress.disconnected or not self._cut():
                return
            await self._resubscribe_after_cut()

    def _cut(self) -> bool:
        return self._progress.suspended or self._pending_cut is not None

    async def _consume_stream(self) -> AsyncIterator[OutputChunk]:
        """Un `disconnect()` desde otra task hace que `read()` levante
        `CancelledError` (o `CANCELLED`); eso es fin del stream, no un fallo."""
        progress = self._progress
        while True:
            try:
                event = await self._call.read()
            except asyncio.CancelledError:
                if progress.disconnected:
                    return
                raise
            except grpc.RpcError as exc:
                if progress.disconnected:
                    return
                if self._sandbox._is_reconnectable(exc):
                    self._pending_cut = exc
                    return
                raise progress.fail(await self._commands._stream_failure(exc)) from exc
            if event is STREAM_EOF:
                return
            consumed = progress.consume(event)
            if isinstance(consumed, Chunk):
                yield consumed.as_output()
            if progress.is_finished() or progress.suspended:
                return

    async def _resubscribe_after_cut(self) -> None:
        reason = self._pending_cut or suspending_reason(self._progress)
        self._pending_cut = None
        outcome = await self._sandbox._reconnect(reason, self._generation, wake=self._wakes())
        if not outcome.resumed:
            raise self._progress.fail(self._sandbox._reconnect_error(outcome, reason)) from reason
        self._generation = outcome.resume_generation
        if self._progress.disconnected:
            return
        if not self._budget.allows(outcome):
            raise self._progress.fail(await self._futile_cut(reason)) from reason
        self._call = await self._resubscribe_through_the_gate()
        self._progress.resubscribed()
        self._reconnects += 1

    async def _resubscribe_through_the_gate(self) -> Any:
        """Como el handle síncrono: una resuscripción rechazada por el phase
        gate se reintenta con backoff dentro de `reconnect_timeout`."""
        retry = GateRetry(self._sandbox._reconnect_timeout)
        while True:
            try:
                return await self._resubscribe(resubscribe_from_seq(self.last_seq))
            except NotFoundException as exc:
                if exc.grpc_code is not grpc.StatusCode.OUT_OF_RANGE:
                    raise self._progress.fail(exc) from exc
                return await self._resubscribe_from_live(exc)
            except SandboxException as exc:
                delay = retry.retry_delay(exc)
                if delay is None:
                    raise self._progress.fail(exc) from exc
                self._sandbox._logger_or(logger).info(
                    "pid %s: el gate del agente sigue cerrado (%s); reintento", self.pid, exc
                )
                await asyncio.sleep(delay)

    async def _futile_cut(self, reason: Exception) -> Exception:
        if isinstance(reason, grpc.RpcError):
            return await self._commands._stream_failure(reason)
        return reason

    async def _resubscribe_from_live(self, exc: NotFoundException) -> Any:
        self._sandbox._logger_or(logger).warning(
            "pid %s: se perdió salida entre el seq %s y lo que el agente retiene; "
            "se sigue desde la salida nueva",
            self.pid,
            self.last_seq,
        )
        try:
            return await self._resubscribe(0)
        except NotFoundException as retry:
            raise self._progress.fail(retry) from exc

    async def _resubscribe(self, from_seq: int) -> Any:
        return await self._commands._open_connect(self.pid, from_seq, self._remaining_deadline())

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
    def _sandbox(self) -> AsyncSandbox:
        return self._commands._sandbox

    def __repr__(self) -> str:
        return f"AsyncCommandHandle(pid={self.pid}, exit_code={self.exit_code!r})"
