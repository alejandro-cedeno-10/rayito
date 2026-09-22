"""Núcleo puro de `files`, compartido por `Sandbox` y `AsyncSandbox`:
validación de argumentos, aritmética de deadlines, construcción de requests,
troceado del stream de `Write`, conversión de protos y el estado de un
`WatchHandle`. Sin I/O.
"""

from __future__ import annotations

import itertools
import logging
import threading
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

import grpc

from rayito._models import EntryInfo, FilesystemEvent, FilesystemEventType, FileType, WriteEntry
from rayito._transport import rpc_status, translate_rpc_error
from rayito.exceptions import InvalidArgumentException, SandboxException
from rayito.v1 import common_pb2, filesystem_pb2

logger = logging.getLogger("rayito.files")

FILE_REQUEST_BASE_SECONDS: Final = 60.0
FILE_REQUEST_SECONDS_PER_MB: Final = 1.0
MB: Final = 1_000_000
WRITE_CHUNK_BYTES: Final = 1_048_576
READ_CHUNK_BYTES: Final = 262_144
WATCH_STOP_JOIN_SECONDS: Final = 5.0
MODE_MAX: Final = 0o7777
DEFAULT_DEPTH: Final = 1
READ_FORMATS: Final = ("text", "bytes", "stream")
UNIX_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
EARLIEST_MODIFIED_TIME: Final = datetime.min.replace(tzinfo=UTC)
LATEST_MODIFIED_TIME: Final = datetime.max.replace(tzinfo=UTC)

ReadFormat = Literal["text", "bytes", "stream"]
EventCallback = Callable[[FilesystemEvent], None]
ExitCallback = Callable[[Exception], None]
PreparedWrite = tuple[str, bytes, int | None]

FILE_TYPES: Final[dict[int, FileType]] = {
    common_pb2.FILE_TYPE_FILE: FileType.FILE,
    common_pb2.FILE_TYPE_DIRECTORY: FileType.DIR,
    common_pb2.FILE_TYPE_SYMLINK: FileType.SYMLINK,
}
EVENT_TYPES: Final[dict[int, FilesystemEventType]] = {
    filesystem_pb2.FILESYSTEM_EVENT_TYPE_CREATE: FilesystemEventType.CREATE,
    filesystem_pb2.FILESYSTEM_EVENT_TYPE_WRITE: FilesystemEventType.WRITE,
    filesystem_pb2.FILESYSTEM_EVENT_TYPE_REMOVE: FilesystemEventType.REMOVE,
    filesystem_pb2.FILESYSTEM_EVENT_TYPE_RENAME: FilesystemEventType.RENAME,
    filesystem_pb2.FILESYSTEM_EVENT_TYPE_CHMOD: FilesystemEventType.CHMOD,
}

_watch_counter = itertools.count(1)


def next_watch_name() -> str:
    return f"rayito-watch-{next(_watch_counter)}"


# ------------------------------------------------------------------ deadlines


def file_request_deadline(total_bytes: int, request_timeout: float | None) -> float:
    """`60 s + 1 s por MB` salvo que el caller fije `request_timeout`."""
    if request_timeout is not None:
        return request_timeout
    return FILE_REQUEST_BASE_SECONDS + FILE_REQUEST_SECONDS_PER_MB * total_bytes / MB


def validate_watch_timeout(timeout: float | None) -> float | None:
    """`0` y `None` significan sin deadline gRPC; `> 0` es el deadline del stream."""
    if timeout is None:
        return None
    if isinstance(timeout, bool) or not isinstance(timeout, int | float):
        raise InvalidArgumentException(
            f"timeout debe ser un número de segundos o None, recibido {timeout!r}"
        )
    if timeout < 0:
        raise InvalidArgumentException(f"timeout no puede ser negativo, recibido {timeout!r}")
    return None if timeout == 0 else float(timeout)


# ----------------------------------------------------------------- validation


def validate_path(path: object, *, field: str = "path") -> str:
    if not isinstance(path, str) or not path:
        raise InvalidArgumentException(f"{field} debe ser una cadena no vacía, recibido {path!r}")
    if "\0" in path:
        raise InvalidArgumentException(f"{field} no puede contener NUL")
    return path


def validate_mode(mode: object) -> int | None:
    if mode is None:
        return None
    if isinstance(mode, bool) or not isinstance(mode, int) or not 0 <= mode <= MODE_MAX:
        raise InvalidArgumentException(
            f"mode debe ser un entero entre 0 y 0o7777, recibido {mode!r}"
        )
    return mode


def validate_depth(depth: object) -> int:
    """`0` se envía tal cual: el agente lo interpreta como `1`."""
    if isinstance(depth, bool) or not isinstance(depth, int) or depth < 0:
        raise InvalidArgumentException(f"depth debe ser un entero >= 0, recibido {depth!r}")
    return depth


def validate_read_format(format: object) -> ReadFormat:
    if format not in READ_FORMATS:
        raise InvalidArgumentException(
            f"format debe ser 'text', 'bytes' o 'stream', recibido {format!r}"
        )
    return format


def coerce_data(data: object) -> bytes:
    """`str` va en UTF-8; `bytes`-like tal cual; un fichero abierto se lee
    entero (en modo texto se codifica en UTF-8). Siempre se materializa en
    memoria para que el tamaño, el deadline y el reintento del 403 estén
    definidos."""
    if isinstance(data, str):
        return data.encode("utf-8")
    if isinstance(data, bytes | bytearray | memoryview):
        return bytes(data)
    read = getattr(data, "read", None)
    if callable(read):
        return _coerce_read_result(read())
    raise InvalidArgumentException(
        f"data acepta str, bytes o un fichero abierto, recibido {type(data).__name__}"
    )


def _coerce_read_result(content: object) -> bytes:
    if isinstance(content, str):
        return content.encode("utf-8")
    if isinstance(content, bytes | bytearray | memoryview):
        return bytes(content)
    raise InvalidArgumentException(
        f"el fichero abierto devolvió {type(content).__name__} en read(), se esperaba str o bytes"
    )


def prepare_write_entries(files: Sequence[WriteEntry]) -> list[PreparedWrite]:
    if not files:
        raise InvalidArgumentException("write_files necesita al menos un fichero")
    prepared: list[PreparedWrite] = []
    for entry in files:
        if not isinstance(entry, WriteEntry):
            raise InvalidArgumentException(
                f"write_files acepta WriteEntry, recibido {type(entry).__name__}"
            )
        prepared.append(
            (validate_path(entry.path), coerce_data(entry.data), validate_mode(entry.mode))
        )
    return prepared


def total_write_bytes(entries: Sequence[PreparedWrite]) -> int:
    return sum(len(data) for _, data, _ in entries)


# ------------------------------------------------------------------- requests


def apply_user(request: Any, user: str | None) -> None:
    """`user=""` se omite como `None`: el agente resuelve el default del `/run` payload."""
    if user:
        request.user.username = user


def read_request(path: str, user: str | None) -> filesystem_pb2.ReadRequest:
    request = filesystem_pb2.ReadRequest(path=validate_path(path))
    apply_user(request, user)
    return request


def stat_request(path: str, user: str | None) -> filesystem_pb2.StatRequest:
    request = filesystem_pb2.StatRequest(path=validate_path(path))
    apply_user(request, user)
    return request


def list_dir_request(path: str, depth: int, user: str | None) -> filesystem_pb2.ListDirRequest:
    request = filesystem_pb2.ListDirRequest(path=validate_path(path), depth=validate_depth(depth))
    apply_user(request, user)
    return request


def make_dir_request(path: str, user: str | None) -> filesystem_pb2.MakeDirRequest:
    request = filesystem_pb2.MakeDirRequest(path=validate_path(path))
    apply_user(request, user)
    return request


def move_request(source: str, destination: str, user: str | None) -> filesystem_pb2.MoveRequest:
    request = filesystem_pb2.MoveRequest(
        source=validate_path(source, field="old_path"),
        destination=validate_path(destination, field="new_path"),
    )
    apply_user(request, user)
    return request


def remove_request(path: str, recursive: bool, user: str | None) -> filesystem_pb2.RemoveRequest:
    request = filesystem_pb2.RemoveRequest(path=validate_path(path), recursive=bool(recursive))
    apply_user(request, user)
    return request


def watch_dir_request(
    path: str, recursive: bool, include_entry: bool, user: str | None
) -> filesystem_pb2.WatchDirRequest:
    request = filesystem_pb2.WatchDirRequest(
        path=validate_path(path), recursive=bool(recursive), include_entry=bool(include_entry)
    )
    apply_user(request, user)
    return request


def build_write_requests(
    entries: Sequence[PreparedWrite], user: str | None
) -> Iterator[filesystem_pb2.WriteRequest]:
    """Un `WriteRequest` con `path` (y `user`/`mode` si los hay) abre cada
    fichero con su primer chunk, posiblemente vacío; el resto viaja en
    chunks de 1 MiB sin `path`. Es un generador nuevo por llamada, que es
    lo que el reintento del 403 necesita."""
    for path, data, mode in entries:
        first = filesystem_pb2.WriteRequest(path=path, chunk=data[:WRITE_CHUNK_BYTES])
        if mode is not None:
            first.mode = mode
        apply_user(first, user)
        yield first
        for offset in range(WRITE_CHUNK_BYTES, len(data), WRITE_CHUNK_BYTES):
            yield filesystem_pb2.WriteRequest(chunk=data[offset : offset + WRITE_CHUNK_BYTES])


# ---------------------------------------------------------------- conversions


def file_type_from_proto(file_type: int) -> FileType | None:
    return FILE_TYPES.get(int(file_type))


def modified_time_from_ms(unix_ms: int) -> datetime:
    """Un mtime corrupto en el sandbox (`touch -d @300000000000`, un tar con
    fechas basura) no debe tumbar `list`/`get_info`/`read` ni el consumidor
    de un watch: fuera del rango de `datetime` se recorta a `datetime.min` o
    `datetime.max` en UTC. La suma sobre el epoch, en lugar de
    `fromtimestamp`, hace que los negativos se comporten igual en Windows y
    en Linux."""
    try:
        return UNIX_EPOCH + timedelta(milliseconds=unix_ms)
    except (OverflowError, OSError, ValueError):
        return EARLIEST_MODIFIED_TIME if unix_ms < 0 else LATEST_MODIFIED_TIME


def entry_info_from_proto(entry: common_pb2.EntryInfo) -> EntryInfo:
    return EntryInfo(
        name=str(entry.name),
        type=file_type_from_proto(entry.type),
        path=str(entry.path),
        size=int(entry.size),
        mode=int(entry.mode),
        permissions=str(entry.permissions),
        owner=str(entry.owner),
        group=str(entry.group),
        modified_time=modified_time_from_ms(int(entry.modified_time_unix_ms)),
        symlink_target=str(entry.symlink_target) if entry.HasField("symlink_target") else None,
    )


def filesystem_event_from_proto(event: filesystem_pb2.FilesystemEvent) -> FilesystemEvent:
    kind = EVENT_TYPES.get(int(event.type))
    if kind is None:
        raise SandboxException(f"tipo de evento de watch desconocido: {int(event.type)}")
    entry = entry_info_from_proto(event.entry) if event.HasField("entry") else None
    return FilesystemEvent(name=str(event.name), type=kind, entry=entry)


def decode_text(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidArgumentException("el fichero no es UTF-8 válido; usa format='bytes'") from exc


def require_regular_file(entry: EntryInfo) -> EntryInfo:
    """`read` sólo abre ficheros regulares: el agente abre con `O_NOFOLLOW`,
    así que un directorio, un symlink o un FIFO se rechazan aquí sin abrir
    el stream."""
    if entry.type is FileType.FILE:
        return entry
    if entry.type is FileType.DIR:
        raise InvalidArgumentException(f"{entry.path} es un directorio")
    if entry.type is FileType.SYMLINK:
        raise InvalidArgumentException(
            f"{entry.path} es un enlace simbólico; el agente no sigue symlinks al leer"
        )
    raise InvalidArgumentException(f"{entry.path} no es un fichero regular")


def require_watch_started(response: Any) -> None:
    """El primer mensaje de `WatchDir` es siempre `WatchStarted`."""
    kind = None if response is None else response.WhichOneof("event")
    if kind != "started":
        raise SandboxException(f"el stream de WatchDir no empezó con WatchStarted (llegó {kind!r})")


def is_already_exists(exc: grpc.RpcError) -> bool:
    return rpc_status(exc) is grpc.StatusCode.ALREADY_EXISTS


# ------------------------------------------------------------------- watching


class WatchState:
    """Estado de un `WatchHandle`, idéntico en sync y async.

    Los eventos sólo se encolan cuando no hay `on_event`; con callback se
    entregan al vuelo y una excepción dentro del callback se loguea sin
    parar el watch. El consumidor llama a `feed` por mensaje y a
    `record_end` al terminar el stream; `drain` devuelve lo pendiente y,
    cuando no queda nada, levanta una sola vez la excepción terminal.
    """

    def __init__(self, on_event: EventCallback | None) -> None:
        self._on_event = on_event
        self._pending: deque[FilesystemEvent] = deque()
        self._failure: Exception | None = None
        self._failure_pending = False
        self._lock = threading.Lock()
        self.stopped = False
        self.ended = False

    @property
    def is_running(self) -> bool:
        return not (self.stopped or self.ended)

    def feed(self, response: Any) -> FilesystemEvent | None:
        kind = response.WhichOneof("event")
        if kind == "started":
            raise SandboxException("WatchStarted repetido en mitad del stream")
        if kind != "filesystem":
            return None
        event = filesystem_event_from_proto(response.filesystem)
        self._dispatch(event)
        return event

    def record_end(self, failure: Exception | None) -> None:
        with self._lock:
            self.ended = True
            self._failure = failure
            self._failure_pending = failure is not None

    def drain(self) -> list[FilesystemEvent]:
        with self._lock:
            events = list(self._pending)
            self._pending.clear()
            failure = None if events else self._take_failure()
        if failure is not None:
            raise failure
        return events

    def _take_failure(self) -> Exception | None:
        if not self._failure_pending:
            return None
        self._failure_pending = False
        return self._failure

    def _dispatch(self, event: FilesystemEvent) -> None:
        if self._on_event is None:
            with self._lock:
                self._pending.append(event)
            return
        try:
            self._on_event(event)
        except Exception:
            logger.warning("on_event levantó una excepción; el watch sigue", exc_info=True)


def watch_failure(exc: grpc.RpcError, *, stopped: bool) -> Exception | None:
    """`CANCELLED` tras `stop()` es el final limpio; lo demás sigue la tabla
    unaria con `filesystem=True` (`DEADLINE_EXCEEDED` → `TimeoutException`,
    `NOT_FOUND` → `FileNotFoundException`)."""
    if stopped and rpc_status(exc) is grpc.StatusCode.CANCELLED:
        return None
    return translate_rpc_error(exc, filesystem=True)


def notify_exit(on_exit: ExitCallback | None, failure: Exception | None) -> None:
    if failure is None or on_exit is None:
        return
    try:
        on_exit(failure)
    except Exception:
        logger.warning("on_exit levantó una excepción", exc_info=True)
