"""Núcleo puro de `pty`, compartido por `Sandbox` y `AsyncSandbox`:
validación, construcción de los requests de `PtyService` y el
`StreamAdapter` que traduce `PtyServerMessage` al vocabulario de
`CommandProgress`. Sin I/O.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Final

from rayito._models import PtySize
from rayito._payload import validated_envs
from rayito._process_base import (
    NOTHING,
    Chunk,
    Consumed,
    OutputAccumulator,
    consumed_end,
    encode_stdin,
    timeout_to_ms,
    validate_pid,
)
from rayito.exceptions import InvalidArgumentException, SandboxException
from rayito.v1 import process_pb2, pty_pb2

DEFAULT_PTY_TIMEOUT_SECONDS: Final = 60.0
MAX_PTY_DIMENSION: Final = 4096

PtyDataCallback = Callable[[bytes], None]


def validate_pty_size(size: object) -> PtySize | None:
    """`None` deja que el agente aplique 80x24; cualquier otra cosa debe ser
    un `PtySize` (que ya se validó a sí mismo)."""
    if size is None:
        return None
    if not isinstance(size, PtySize):
        raise InvalidArgumentException(
            f"size debe ser un PtySize o None, recibido {type(size).__name__}"
        )
    return size


def validate_shell(shell: object) -> str | None:
    """`None` y `""` dejan el shell de login del usuario; si se indica, debe
    ser una ruta absoluta (el agente rechaza las relativas)."""
    if shell is None or shell == "":
        return None
    if not isinstance(shell, str) or "\x00" in shell or not shell.startswith("/"):
        raise InvalidArgumentException(f"shell debe ser una ruta absoluta, recibido {shell!r}")
    return shell


def build_pty_start_request(
    *,
    size: PtySize | None = None,
    user: str | None = None,
    cwd: str | None = None,
    envs: Mapping[str, str] | None = None,
    shell: str | None = None,
    timeout: float | None = DEFAULT_PTY_TIMEOUT_SECONDS,
) -> pty_pb2.PtyStart:
    """`PtyStart` con `timeout_ms` (0 = sin límite). `user=""`, `cwd=""` y
    `shell=""` se omiten como `None`; `size=None` omite el campo y el agente
    aplica 80x24."""
    request = pty_pb2.PtyStart(timeout_ms=timeout_to_ms(timeout))
    validated_size = validate_pty_size(size)
    if validated_size is not None:
        request.size.cols = validated_size.cols
        request.size.rows = validated_size.rows
    if envs:
        request.envs.update(validated_envs(envs))
    if cwd:
        request.cwd = cwd
    if user:
        request.user.username = user
    validated_shell = validate_shell(shell)
    if validated_shell is not None:
        request.shell = validated_shell
    return request


def build_connect_request(pid: int, from_seq: int) -> process_pb2.ConnectRequest:
    return process_pb2.ConnectRequest(pid=validate_pid(pid), from_seq=from_seq)


def build_send_input_request(pid: int, data: str | bytes) -> process_pb2.SendInputRequest:
    return process_pb2.SendInputRequest(pid=validate_pid(pid), data=encode_stdin(data))


def build_resize_request(pid: int, size: PtySize) -> pty_pb2.ResizeRequest:
    validated = validate_pty_size(size)
    if validated is None:
        raise InvalidArgumentException("resize necesita un PtySize")
    return pty_pb2.ResizeRequest(
        pid=validate_pid(pid), size=pty_pb2.PtySize(cols=validated.cols, rows=validated.rows)
    )


def build_kill_request(pid: int) -> pty_pb2.KillPtyRequest:
    return pty_pb2.KillPtyRequest(pid=validate_pid(pid))


def pid_from_pty_started(message: pty_pb2.PtyServerMessage) -> int:
    """El primer mensaje de `Create` y `Connect` es siempre `started{pid}`."""
    kind = message.WhichOneof("message")
    if kind != "started":
        raise SandboxException(f"el stream de la PTY no empezó con started (llegó {kind!r})")
    return int(message.started.pid)


def end_event_from_pty_exited(exited: pty_pb2.PtyExited) -> process_pb2.EndEvent:
    """`PtyExited` tiene la misma forma que `EndEvent`; convertirlo deja que
    `CommandProgress` aplique una única tabla de estados."""
    end = process_pb2.EndEvent(
        exit_code=int(exited.exit_code), exited=bool(exited.exited), status=str(exited.status)
    )
    if exited.HasField("error"):
        end.error.CopyFrom(exited.error)
    if exited.HasField("signal"):
        end.signal = int(exited.signal)
    return end


class PtyMessages:
    """`StreamAdapter` de `PtyService.Create`/`Connect`: `data` son bytes
    crudos de la terminal, entregados tal cual a `on_data` y al iterador y
    decodificados (UTF-8 con reemplazo) en `stdout`."""

    def __init__(self, on_data: PtyDataCallback | None = None) -> None:
        self._on_data = on_data

    def pid(self, first: pty_pb2.PtyServerMessage) -> int:
        return pid_from_pty_started(first)

    def consume(
        self, message: pty_pb2.PtyServerMessage, accumulator: OutputAccumulator
    ) -> Consumed:
        kind = message.WhichOneof("message")
        if kind == "data":
            payload = bytes(message.data)
            text = accumulator.feed_terminal(int(message.seq), payload)
            if self._on_data is not None:
                self._on_data(payload)
            return Chunk(seq=int(message.seq), stdout=text, pty=payload)
        if kind == "exited":
            return consumed_end(end_event_from_pty_exited(message.exited))
        return NOTHING
