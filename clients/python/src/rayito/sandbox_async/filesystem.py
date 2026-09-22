"""`AsyncSandbox.files`: la misma superficie que `sandbox_sync.filesystem`
sobre `grpc.aio`. El stream de `Read` se lee con `read()` y el de `WatchDir`
lo consume una `asyncio.Task`."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Sequence
from typing import IO, TYPE_CHECKING, Any, Literal, overload

import grpc

from rayito._filesystem_base import (
    WATCH_STOP_JOIN_SECONDS,
    EventCallback,
    ExitCallback,
    WatchState,
    build_write_requests,
    decode_text,
    entry_info_from_proto,
    file_request_deadline,
    is_already_exists,
    list_dir_request,
    make_dir_request,
    move_request,
    next_watch_name,
    notify_exit,
    prepare_write_entries,
    read_request,
    remove_request,
    require_regular_file,
    require_watch_started,
    stat_request,
    total_write_bytes,
    validate_read_format,
    validate_watch_timeout,
    watch_dir_request,
    watch_failure,
)
from rayito._models import EntryInfo, FilesystemEvent, WriteEntry
from rayito._process_base import STREAM_EOF, deadline_at, remaining_deadline
from rayito._sandbox_base import GateRetry, ReconnectBudget
from rayito._transport import is_stream_reset, translate_rpc_error
from rayito.exceptions import FileNotFoundException, SandboxException, TimeoutException
from rayito.v1 import filesystem_pb2, filesystem_pb2_grpc

if TYPE_CHECKING:
    from rayito.sandbox_async.main import AsyncSandbox

FILES_STUB = filesystem_pb2_grpc.FilesystemServiceStub

logger = logging.getLogger("rayito.files")


class AsyncFilesystem:
    """Ficheros del sandbox (`FilesystemService`) como corrutinas."""

    def __init__(self, sandbox: AsyncSandbox) -> None:
        self._sandbox = sandbox
        self._watches: set[AsyncWatchHandle] = set()

    @overload
    async def read(
        self,
        path: str,
        *,
        format: Literal["text"] = "text",
        user: str | None = None,
        request_timeout: float | None = None,
    ) -> str: ...

    @overload
    async def read(
        self,
        path: str,
        *,
        format: Literal["bytes"],
        user: str | None = None,
        request_timeout: float | None = None,
    ) -> bytes: ...

    @overload
    async def read(
        self,
        path: str,
        *,
        format: Literal["stream"],
        user: str | None = None,
        request_timeout: float | None = None,
    ) -> AsyncIterator[bytes]: ...

    async def read(
        self,
        path: str,
        *,
        format: str = "text",
        user: str | None = None,
        request_timeout: float | None = None,
    ) -> str | bytes | AsyncIterator[bytes]:
        """Misma semántica que `Filesystem.read`; `format="stream"` devuelve
        un `AsyncIterator[bytes]`."""
        read_format = validate_read_format(format)
        request = read_request(path, user)
        entry = require_regular_file(
            await self.get_info(path, user=user, request_timeout=request_timeout)
        )
        chunks = await self._read_chunks(
            request, file_request_deadline(entry.size, request_timeout)
        )
        if read_format == "stream":
            return chunks
        data = b"".join([chunk async for chunk in chunks])
        return data if read_format == "bytes" else decode_text(data)

    async def write(
        self,
        path: str,
        data: str | bytes | IO[bytes] | IO[str],
        *,
        user: str | None = None,
        mode: int | None = None,
        request_timeout: float | None = None,
    ) -> EntryInfo:
        entries = await self.write_files(
            [WriteEntry(path, data, mode)], user=user, request_timeout=request_timeout
        )
        return entries[0]

    async def write_files(
        self,
        files: Sequence[WriteEntry],
        *,
        user: str | None = None,
        request_timeout: float | None = None,
    ) -> list[EntryInfo]:
        """Misma semántica que `Filesystem.write_files`: un solo stream, cada
        fichero atómico por separado."""
        prepared = prepare_write_entries(files)
        deadline = file_request_deadline(total_write_bytes(prepared), request_timeout)
        response = await self._sandbox._files_call(
            lambda stub, timeout: stub.Write(build_write_requests(prepared, user), timeout=timeout),
            deadline,
        )
        return [entry_info_from_proto(entry) for entry in response.entries]

    async def list(
        self,
        path: str,
        *,
        depth: int = 1,
        user: str | None = None,
        request_timeout: float | None = None,
    ) -> list[EntryInfo]:
        request = list_dir_request(path, depth, user)
        response = await self._sandbox._files_call(
            lambda stub, timeout: stub.ListDir(request, timeout=timeout), request_timeout
        )
        return [entry_info_from_proto(entry) for entry in response.entries]

    async def exists(
        self, path: str, *, user: str | None = None, request_timeout: float | None = None
    ) -> bool:
        try:
            await self.get_info(path, user=user, request_timeout=request_timeout)
        except FileNotFoundException:
            return False
        return True

    async def get_info(
        self, path: str, *, user: str | None = None, request_timeout: float | None = None
    ) -> EntryInfo:
        request = stat_request(path, user)
        response = await self._sandbox._files_call(
            lambda stub, timeout: stub.Stat(request, timeout=timeout), request_timeout
        )
        return entry_info_from_proto(response.entry)

    async def remove(
        self,
        path: str,
        *,
        recursive: bool = True,
        user: str | None = None,
        request_timeout: float | None = None,
    ) -> None:
        request = remove_request(path, recursive, user)
        await self._sandbox._files_call(
            lambda stub, timeout: stub.Remove(request, timeout=timeout), request_timeout
        )

    async def rename(
        self,
        old_path: str,
        new_path: str,
        *,
        user: str | None = None,
        request_timeout: float | None = None,
    ) -> EntryInfo:
        request = move_request(old_path, new_path, user)
        response = await self._sandbox._files_call(
            lambda stub, timeout: stub.Move(request, timeout=timeout), request_timeout
        )
        return entry_info_from_proto(response.entry)

    async def make_dir(
        self, path: str, *, user: str | None = None, request_timeout: float | None = None
    ) -> bool:
        request = make_dir_request(path, user)
        timeout = self._sandbox._resolve_request_timeout(request_timeout)
        try:
            await self._sandbox._call_unary(
                lambda: self._sandbox._files.MakeDir(request, timeout=timeout)
            )
        except grpc.RpcError as exc:
            if is_already_exists(exc):
                return False
            raise translate_rpc_error(exc, filesystem=True) from exc
        return True

    async def watch_dir(
        self,
        path: str,
        *,
        on_event: EventCallback | None = None,
        on_exit: ExitCallback | None = None,
        recursive: bool = False,
        include_entry: bool = False,
        user: str | None = None,
        timeout: float | None = 0,
        request_timeout: float | None = None,
    ) -> AsyncWatchHandle:
        """Misma semántica que `Filesystem.watch_dir`; el consumidor es una
        `asyncio.Task` y los callbacks corren en el loop."""
        request = watch_dir_request(path, recursive, include_entry, user)
        deadline = validate_watch_timeout(timeout)
        call = await self._open_watch(request, deadline)
        handle = AsyncWatchHandle(
            filesystem=self,
            call=call,
            path=path,
            state=WatchState(on_event),
            on_exit=on_exit,
            request=request,
            deadline_at=deadline_at(deadline, time.monotonic),
        )
        self._track(handle)
        handle._start()
        return handle

    async def _open_watch(
        self, request: filesystem_pb2.WatchDirRequest, deadline: float | None
    ) -> Any:
        call, first = await self._sandbox._open_stream(
            lambda stub: stub.WatchDir(request, timeout=deadline),
            service=FILES_STUB,
            stream=True,
            filesystem=True,
        )
        try:
            require_watch_started(first)
        except SandboxException:
            call.cancel()
            raise
        return call

    async def _read_chunks(self, request: Any, deadline: float) -> AsyncIterator[bytes]:
        call, first = await self._sandbox._open_stream(
            lambda stub: stub.Read(request, timeout=deadline),
            service=FILES_STUB,
            stream=False,
            allow_empty=True,
            filesystem=True,
        )
        return self._remaining_chunks(call, first)

    async def _remaining_chunks(self, call: Any, first: Any) -> AsyncIterator[bytes]:
        if first is None:
            return
        try:
            yield bytes(first.chunk)
            while True:
                try:
                    response = await call.read()
                except grpc.RpcError as exc:
                    raise await self._stream_failure(exc) from exc
                if response is STREAM_EOF:
                    return
                yield bytes(response.chunk)
        finally:
            call.cancel()

    async def _stream_failure(self, exc: grpc.RpcError) -> Exception:
        return await self._sandbox._stream_failure(exc, filesystem=True)

    def _track(self, handle: AsyncWatchHandle) -> None:
        self._watches.add(handle)

    def _untrack(self, handle: AsyncWatchHandle) -> None:
        self._watches.discard(handle)

    async def _stop_watches(self) -> None:
        for handle in list(self._watches):
            await handle.stop()


class AsyncWatchHandle:
    """`WatchHandle` sobre `grpc.aio`: `await get_new_events()`, `await stop()`
    y `async with`."""

    def __init__(
        self,
        *,
        filesystem: AsyncFilesystem,
        call: Any,
        path: str,
        state: WatchState,
        on_exit: ExitCallback | None,
        request: filesystem_pb2.WatchDirRequest | None = None,
        deadline_at: float | None = None,
    ) -> None:
        self._filesystem = filesystem
        self._call = call
        self._path = path
        self._state = state
        self._on_exit = on_exit
        self._request = request
        self._deadline_at = deadline_at
        self._generation = filesystem._sandbox.resume_generation
        self._reconnects = 0
        self._budget = ReconnectBudget()
        self._pending_cut: grpc.RpcError | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def path(self) -> str:
        return self._path

    @property
    def reconnects(self) -> int:
        return self._reconnects

    @property
    def is_running(self) -> bool:
        return self._state.is_running

    async def get_new_events(self) -> list[FilesystemEvent]:
        return self._state.drain()

    async def stop(self) -> None:
        if self._state.stopped:
            return
        self._state.stopped = True
        self._call.cancel()
        if self._task is not None and asyncio.current_task() is not self._task:
            await asyncio.wait({self._task}, timeout=WATCH_STOP_JOIN_SECONDS)
        self._filesystem._untrack(self)

    def _start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._consume(), name=next_watch_name())
        self._task.add_done_callback(self._finish_if_never_ran)

    async def _consume(self) -> None:
        """Un `cancel()` del RPC hace que `read()` levante `CancelledError` o
        `CANCELLED`; tras `stop()` ambos son el final limpio. Una cancelación
        de la Task sin `stop()` (p. ej. el loop cerrando) termina el watch
        con `SandboxException` y se propaga."""
        failure: Exception | None = None
        try:
            await self._pump()
        except asyncio.CancelledError:
            failure = self._external_cancellation()
            if failure is not None:
                raise
        except grpc.RpcError as exc:
            failure = await self._classify(exc)
        except Exception as exc:
            failure = exc
        finally:
            self._finish(failure)

    def _finish_if_never_ran(self, task: asyncio.Task[None]) -> None:
        """Una Task cancelada antes de su primer paso nunca entra en `_consume`,
        así que su `finally` no cierra el watch; este callback lo hace."""
        if not self._state.ended:
            self._finish(self._external_cancellation())

    def _external_cancellation(self) -> Exception | None:
        if self._state.stopped:
            return None
        return SandboxException("la tarea del watch fue cancelada sin llamar a stop()")

    async def _pump(self) -> None:
        while await self._pump_until_cut() and await self._reissue():
            pass

    async def _pump_until_cut(self) -> bool:
        """Consume el stream actual. True si terminó por un corte reconectable
        con el watch vivo (hay que reabrirlo); False si terminó limpiamente."""
        try:
            while True:
                response = await self._call.read()
                if response is STREAM_EOF:
                    return False
                self._state.feed(response)
        except grpc.RpcError as exc:
            if self._state.stopped or not self._sandbox._is_reconnectable(exc):
                raise
            self._pending_cut = exc
            return True

    async def _reissue(self) -> bool:
        """Espera al agente y vuelve a emitir el mismo `WatchDir`; False si
        alguien llamó a `stop()` mientras tanto (final limpio). Como el
        handle síncrono, un `WatchDir` rechazado por el phase gate se
        reintenta con backoff dentro de `reconnect_timeout`."""
        reason = self._pending_cut
        if reason is None or self._request is None:
            raise SandboxException("el watch no puede reabrirse sin su request original")
        outcome = await self._sandbox._reconnect(reason, self._generation, wake=False)
        if not outcome.resumed:
            raise self._sandbox._reconnect_error(outcome, reason) from reason
        self._generation = outcome.resume_generation
        if self._state.stopped:
            return False
        if not self._budget.allows(outcome):
            raise reason
        self._call = await self._reopen_through_the_gate(self._request)
        self._reconnects += 1
        self._cancel_if_stopped()
        return True

    async def _reopen_through_the_gate(self, request: filesystem_pb2.WatchDirRequest) -> Any:
        retry = GateRetry(self._sandbox._reconnect_timeout)
        while True:
            try:
                return await self._filesystem._open_watch(request, self._remaining_deadline())
            except SandboxException as exc:
                delay = retry.retry_delay(exc)
                if delay is None:
                    raise
                logger.info(
                    "watch %s: el gate del agente sigue cerrado (%s); reintento", self._path, exc
                )
                await asyncio.sleep(delay)

    def _cancel_if_stopped(self) -> None:
        """Un `stop()` que llegó mientras se reabría el watch cancela el
        stream nuevo; el bucle lo verá como el final limpio de M3."""
        if self._state.stopped:
            self._call.cancel()

    def _remaining_deadline(self) -> float | None:
        remaining = remaining_deadline(self._deadline_at, time.monotonic)
        if remaining is not None and remaining <= 0.0:
            raise TimeoutException(
                f"el deadline del watch de {self._path} venció durante la reconexión"
            )
        return remaining

    @property
    def _sandbox(self) -> AsyncSandbox:
        return self._filesystem._sandbox

    async def _classify(self, exc: grpc.RpcError) -> Exception | None:
        if not self._state.stopped and is_stream_reset(exc):
            return await self._filesystem._stream_failure(exc)
        return watch_failure(exc, stopped=self._state.stopped)

    def _finish(self, failure: Exception | None) -> None:
        """Cancela el RPC antes de dar el watch por terminado: si el consumidor
        muere por un mensaje inválido, el stream seguiría abierto y `rayd`
        retendría el inotify y una de sus 64 plazas hasta cerrar el canal."""
        self._call.cancel()
        self._state.record_end(failure)
        self._filesystem._untrack(self)
        notify_exit(self._on_exit, failure)

    async def __aenter__(self) -> AsyncWatchHandle:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()

    def __repr__(self) -> str:
        return f"AsyncWatchHandle(path={self._path!r}, is_running={self.is_running})"
