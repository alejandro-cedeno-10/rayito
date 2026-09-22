"""`AsyncSandbox.pty`: la misma superficie que `sandbox_sync.pty` sobre
`grpc.aio`. `AsyncPtyHandle` es un `AsyncCommandHandle` con `send_input`,
`resize` y `kill` sobre `PtyService`; `on_data` sigue siendo síncrono."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from rayito._models import PtySize
from rayito._process_base import (
    CommandProgress,
    OutputAccumulator,
    deadline_at,
    stream_deadline,
    validate_from_seq,
)
from rayito._pty_base import (
    DEFAULT_PTY_TIMEOUT_SECONDS,
    PtyDataCallback,
    PtyMessages,
    build_connect_request,
    build_kill_request,
    build_pty_start_request,
    build_resize_request,
    build_send_input_request,
    pid_from_pty_started,
)
from rayito.exceptions import NotFoundException, SandboxException
from rayito.sandbox_async.commands import AsyncCommandHandle, StreamStarter
from rayito.v1 import pty_pb2_grpc

if TYPE_CHECKING:
    from rayito.sandbox_async.main import AsyncSandbox

PTY_STUB = pty_pb2_grpc.PtyServiceStub


class AsyncPty:
    """Terminales interactivas del sandbox (`PtyService`) como corrutinas."""

    def __init__(self, sandbox: AsyncSandbox) -> None:
        self._sandbox = sandbox

    async def create(
        self,
        *,
        size: PtySize | None = None,
        user: str | None = None,
        cwd: str | None = None,
        envs: Mapping[str, str] | None = None,
        shell: str | None = None,
        on_data: PtyDataCallback | None = None,
        timeout: float | None = DEFAULT_PTY_TIMEOUT_SECONDS,
        request_timeout: float | None = None,
    ) -> AsyncPtyHandle:
        """Misma semántica que `Pty.create`; `on_data` corre en el loop."""
        request = build_pty_start_request(
            size=size, user=user, cwd=cwd, envs=envs, shell=shell, timeout=timeout
        )
        deadline = stream_deadline(timeout)
        return await self._attach(
            lambda stub: stub.Create(request, timeout=deadline),
            deadline=deadline,
            on_data=on_data,
            request_timeout=request_timeout,
        )

    async def connect(
        self,
        pid: int,
        *,
        from_seq: int = 0,
        on_data: PtyDataCallback | None = None,
        timeout: float | None = None,
        request_timeout: float | None = None,
    ) -> AsyncPtyHandle:
        """Misma semántica que `Pty.connect`."""
        return await self._attach(
            self._connect_starter(pid, from_seq, timeout),
            deadline=timeout,
            on_data=on_data,
            request_timeout=request_timeout,
        )

    async def send_input(
        self, pid: int, data: str | bytes, *, request_timeout: float | None = None
    ) -> None:
        request = build_send_input_request(pid, data)
        await self._sandbox._pty_call(
            lambda stub, timeout: stub.SendInput(request, timeout=timeout), request_timeout
        )

    send_stdin = send_input

    async def resize(
        self, pid: int, size: PtySize, *, request_timeout: float | None = None
    ) -> None:
        request = build_resize_request(pid, size)
        await self._sandbox._pty_call(
            lambda stub, timeout: stub.Resize(request, timeout=timeout), request_timeout
        )

    async def kill(self, pid: int, *, request_timeout: float | None = None) -> bool:
        request = build_kill_request(pid)
        try:
            await self._sandbox._pty_call(
                lambda stub, timeout: stub.Kill(request, timeout=timeout), request_timeout
            )
        except NotFoundException:
            return False
        return True

    def _connect_starter(self, pid: int, from_seq: int, timeout: float | None) -> StreamStarter:
        request = build_connect_request(pid, validate_from_seq(from_seq))
        return lambda stub: stub.Connect(request, timeout=timeout)

    async def _open_connect(self, pid: int, from_seq: int, timeout: float | None) -> Any:
        call, first = await self._sandbox._open_stream(
            self._connect_starter(pid, from_seq, timeout), service=PTY_STUB, stream=True
        )
        if pid_from_pty_started(first) != pid:
            raise SandboxException(f"Pty.Connect({pid}) respondió con otro pid")
        return call

    async def _attach(
        self,
        start: StreamStarter,
        *,
        deadline: float | None,
        on_data: PtyDataCallback | None,
        request_timeout: float | None,
    ) -> AsyncPtyHandle:
        call, first = await self._sandbox._open_stream(start, service=PTY_STUB, stream=True)
        adapter = PtyMessages(on_data)
        progress = CommandProgress(adapter.pid(first), OutputAccumulator(), adapter)
        return AsyncPtyHandle(
            pty=self,
            call=call,
            progress=progress,
            request_timeout=request_timeout,
            deadline_at=deadline_at(deadline, time.monotonic),
        )


class AsyncPtyHandle(AsyncCommandHandle):
    """`PtyHandle` sobre `grpc.aio`: `async for` entrega `(None, None, bytes)`."""

    def __init__(
        self,
        *,
        pty: AsyncPty,
        call: Any,
        progress: CommandProgress,
        request_timeout: float | None,
        deadline_at: float | None = None,
    ) -> None:
        super().__init__(
            commands=pty._sandbox.commands,
            call=call,
            progress=progress,
            request_timeout=request_timeout,
            deadline_at=deadline_at,
        )
        self._pty = pty

    async def kill(self) -> bool:
        return await self._pty.kill(self.pid, request_timeout=self._request_timeout)

    async def send_input(self, data: str | bytes) -> None:
        await self._pty.send_input(self.pid, data, request_timeout=self._request_timeout)

    send_stdin = send_input

    async def resize(self, size: PtySize) -> None:
        await self._pty.resize(self.pid, size, request_timeout=self._request_timeout)

    async def _resubscribe(self, from_seq: int) -> Any:
        return await self._pty._open_connect(self.pid, from_seq, self._remaining_deadline())

    def __repr__(self) -> str:
        return f"AsyncPtyHandle(pid={self.pid}, exit_code={self.exit_code!r})"
