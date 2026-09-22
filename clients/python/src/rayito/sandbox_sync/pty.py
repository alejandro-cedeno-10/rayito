"""`Sandbox.pty`: `PtyService` sobre `grpc`.

Una PTY es una terminal real (`openpty`, shell de login `-i -l` como el
usuario del sandbox) que vive en el canal de streams largos; `Create` y
`Connect` son server-streams y la entrada, el tamaño y el kill son unarios,
igual que en envd de E2B. El `PtyHandle` es un `CommandHandle`: mismo
`pid`, `last_seq`, `wait()`, iteración y reconexión tras un suspend/resume
(`Pty.Connect(pid, from_seq)`), con bytes crudos en lugar de texto por
descriptor.
"""

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
from rayito.sandbox_sync.commands import CommandHandle, StreamStarter
from rayito.v1 import pty_pb2_grpc

if TYPE_CHECKING:
    from rayito.sandbox_sync.main import Sandbox

PTY_STUB = pty_pb2_grpc.PtyServiceStub


class Pty:
    """Terminales interactivas del sandbox (`PtyService`)."""

    def __init__(self, sandbox: Sandbox) -> None:
        self._sandbox = sandbox

    def create(
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
    ) -> PtyHandle:
        """Abre una terminal con el shell de login de `user` (uid 1000 por
        defecto) como `<shell> -i -l`, `TERM=xterm-256color` y `LANG`/
        `LC_ALL=C.UTF-8` (los `envs` del request ganan). `size` es 80x24 si
        se omite; `shell` debe ser una ruta absoluta.

        `timeout` (60 s por defecto, como en E2B) lo impone el agente sobre el
        reloj corrido: al vencer manda SIGTERM al grupo y `wait()` termina en
        `TimeoutException`. Para un shell que deba sobrevivir, `timeout=None`.
        `on_data` recibe cada chunk de bytes de la terminal mientras el
        handle se itera o se espera; el SDK nunca los loguea.
        """
        request = build_pty_start_request(
            size=size, user=user, cwd=cwd, envs=envs, shell=shell, timeout=timeout
        )
        deadline = stream_deadline(timeout)
        return self._attach(
            lambda stub: stub.Create(request, timeout=deadline),
            deadline=deadline,
            on_data=on_data,
            request_timeout=request_timeout,
        )

    def connect(
        self,
        pid: int,
        *,
        from_seq: int = 0,
        on_data: PtyDataCallback | None = None,
        timeout: float | None = None,
        request_timeout: float | None = None,
    ) -> PtyHandle:
        """Se reengancha a una PTY viva o terminada hace menos de 30 s.
        `from_seq=0` entrega sólo salida nueva; `N` reenvía lo retenido con
        `seq >= N`. `timeout` es el deadline gRPC de este stream."""
        return self._attach(
            self._connect_starter(pid, from_seq, timeout),
            deadline=timeout,
            on_data=on_data,
            request_timeout=request_timeout,
        )

    def send_input(
        self, pid: int, data: str | bytes, *, request_timeout: float | None = None
    ) -> None:
        """Escribe en la terminal (`str` va en UTF-8; un `\\n` final ejecuta la
        línea). Una PTY terminada es `NotFoundException`; el pid de un proceso
        normal es `InvalidArgumentException`."""
        request = build_send_input_request(pid, data)
        self._sandbox._pty_call(
            lambda stub, timeout: stub.SendInput(request, timeout=timeout), request_timeout
        )

    send_stdin = send_input

    def resize(self, pid: int, size: PtySize, *, request_timeout: float | None = None) -> None:
        """`TIOCSWINSZ` sobre la terminal; el shell recibe `SIGWINCH`."""
        request = build_resize_request(pid, size)
        self._sandbox._pty_call(
            lambda stub, timeout: stub.Resize(request, timeout=timeout), request_timeout
        )

    def kill(self, pid: int, *, request_timeout: float | None = None) -> bool:
        """SIGKILL al grupo de la terminal. False si el pid no existe o ya terminó."""
        request = build_kill_request(pid)
        try:
            self._sandbox._pty_call(
                lambda stub, timeout: stub.Kill(request, timeout=timeout), request_timeout
            )
        except NotFoundException:
            return False
        return True

    def _connect_starter(self, pid: int, from_seq: int, timeout: float | None) -> StreamStarter:
        request = build_connect_request(pid, validate_from_seq(from_seq))
        return lambda stub: stub.Connect(request, timeout=timeout)

    def _open_connect(self, pid: int, from_seq: int, timeout: float | None) -> Any:
        """`Pty.Connect` ya consumido hasta su `started`; es lo que un
        `PtyHandle` usa para reengancharse conservando su estado."""
        call, first = self._sandbox._open_stream(
            self._connect_starter(pid, from_seq, timeout), service=PTY_STUB, stream=True
        )
        if pid_from_pty_started(first) != pid:
            raise SandboxException(f"Pty.Connect({pid}) respondió con otro pid")
        return call

    def _attach(
        self,
        start: StreamStarter,
        *,
        deadline: float | None,
        on_data: PtyDataCallback | None,
        request_timeout: float | None,
    ) -> PtyHandle:
        call, first = self._sandbox._open_stream(start, service=PTY_STUB, stream=True)
        adapter = PtyMessages(on_data)
        progress = CommandProgress(adapter.pid(first), OutputAccumulator(), adapter)
        return PtyHandle(
            pty=self,
            call=call,
            progress=progress,
            request_timeout=request_timeout,
            deadline_at=deadline_at(deadline, time.monotonic),
        )


class PtyHandle(CommandHandle):
    """Una terminal abierta con `pty.create` o `pty.connect`.

    Es un `CommandHandle` (`pid`, `last_seq`, `stdout` con la salida de la
    terminal decodificada, `stderr` vacío, `exit_code`, `error`, `wait()`,
    `disconnect()`, `reconnects`) cuya iteración entrega `(None, None, bytes)`
    por mensaje y cuyo `kill()` es `PtyService.Kill`. `send_input` escribe en
    la terminal y `resize` cambia su tamaño.
    """

    def __init__(
        self,
        *,
        pty: Pty,
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

    def kill(self) -> bool:
        return self._pty.kill(self.pid, request_timeout=self._request_timeout)

    def send_input(self, data: str | bytes) -> None:
        self._pty.send_input(self.pid, data, request_timeout=self._request_timeout)

    send_stdin = send_input

    def resize(self, size: PtySize) -> None:
        self._pty.resize(self.pid, size, request_timeout=self._request_timeout)

    def _resubscribe(self, from_seq: int) -> Any:
        return self._pty._open_connect(self.pid, from_seq, self._remaining_deadline())

    def __repr__(self) -> str:
        return f"PtyHandle(pid={self.pid}, exit_code={self.exit_code!r})"
