"""`FilesystemService` falso en proceso, con el contrato de `rayd` en M3
(`openspec/changes/m3-filesystem/design.md` D1-D10).

Árbol en memoria sembrado con `/home/user`, `/tmp` y `/etc/passwd`; lista de
denegación `/etc` y `/usr` sobre la ruta canónica; rutas relativas al home;
`..` rechazado; `Read` en chunks de 256 KiB sin seguir el symlink final;
`Write` con la máquina de estados de un fichero por mensaje con `path` y el
tope de 1 MiB por chunk; `ListDir` con la semántica de `depth` y sin
descender por symlinks; `MakeDir`/`Move`/`Remove` con sus status; `WatchDir`
que emite `WatchStarted`, un `keepalive` y luego lo que el test empuje con
`push_event`/`end_watch`. Cada RPC exige `x-access-token` y registra el peer
del cliente y el deadline recibido.
"""

from __future__ import annotations

import queue
import threading
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field

import grpc

from rayito.v1 import common_pb2, filesystem_pb2, filesystem_pb2_grpc

from .fake_persistence import FakePersistence
from .fake_process import require_access_token

HOME = "/home/user"
DENIED_PREFIXES = ("/etc", "/usr")
READ_CHUNK_BYTES = 262_144
MAX_WRITE_CHUNK_BYTES = 1_048_576
MODE_MAX = 0o7777
DEFAULT_FILE_MODE = 0o644
DEFAULT_DIR_MODE = 0o755
MAX_LIST_ENTRIES = 10_000
KNOWN_USERS = frozenset({"user", "root"})
WATCH_POLL_SECONDS = 0.02
MODIFIED_UNIX_MS = 1_789_000_000_000
DIRECTORY_SIZE = 4096
PERMISSION_BITS = "rwxrwxrwx"


class FakeStatus(Exception):
    """Un status gRPC decidido por el fake; se convierte en `context.abort`."""

    def __init__(self, code: grpc.StatusCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def invalid(message: str) -> FakeStatus:
    return FakeStatus(grpc.StatusCode.INVALID_ARGUMENT, message)


def not_found() -> FakeStatus:
    return FakeStatus(grpc.StatusCode.NOT_FOUND, "path not found")


def denied() -> FakeStatus:
    return FakeStatus(grpc.StatusCode.PERMISSION_DENIED, "path is denied by policy")


def failed_precondition(message: str) -> FakeStatus:
    return FakeStatus(grpc.StatusCode.FAILED_PRECONDITION, message)


@dataclass
class FakeFile:
    data: bytes = b""
    mode: int = DEFAULT_FILE_MODE
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass
class FakeDirectory:
    mode: int = DEFAULT_DIR_MODE


@dataclass
class FakeSymlink:
    target: str


Node = FakeFile | FakeDirectory | FakeSymlink


def normalize(raw: str) -> str:
    if not raw:
        raise invalid("path is empty")
    if "\0" in raw:
        raise invalid("path contains NUL")
    absolute = raw if raw.startswith("/") else f"{HOME}/{raw}"
    parts: list[str] = []
    for component in absolute.split("/"):
        if component in ("", "."):
            continue
        if component == "..":
            raise invalid("parent references are not allowed")
        parts.append(component)
    return "/" + "/".join(parts)


def parent_of(path: str) -> str:
    return path.rsplit("/", 1)[0] or "/"


def name_of(path: str) -> str:
    return path.rsplit("/", 1)[-1] or "/"


def join(parent: str, name: str) -> str:
    return f"/{name}" if parent == "/" else f"{parent}/{name}"


def is_denied(canonical: str) -> bool:
    return any(
        canonical == prefix or canonical.startswith(prefix + "/") for prefix in DENIED_PREFIXES
    )


def permissions_string(node: Node) -> str:
    if isinstance(node, FakeSymlink):
        return "lrwxrwxrwx"
    kind = "d" if isinstance(node, FakeDirectory) else "-"
    bits = "".join(
        PERMISSION_BITS[index] if node.mode & (1 << (8 - index)) else "-" for index in range(9)
    )
    return kind + bits


def entry_info(path: str, node: Node) -> common_pb2.EntryInfo:
    if isinstance(node, FakeFile):
        file_type, size, mode = common_pb2.FILE_TYPE_FILE, len(node.data), node.mode
    elif isinstance(node, FakeDirectory):
        file_type, size, mode = common_pb2.FILE_TYPE_DIRECTORY, DIRECTORY_SIZE, node.mode
    else:
        file_type, size, mode = common_pb2.FILE_TYPE_SYMLINK, len(node.target), 0o777
    info = common_pb2.EntryInfo(
        name=name_of(path),
        type=file_type,
        path=path,
        size=size,
        mode=mode,
        permissions=permissions_string(node),
        owner="user",
        group="user",
        modified_time_unix_ms=MODIFIED_UNIX_MS,
    )
    if isinstance(node, FakeSymlink):
        info.symlink_target = node.target
    if isinstance(node, FakeFile):
        info.metadata.update(node.metadata)
    return info


class FakeTree:
    """Árbol de nodos indexado por ruta canónica."""

    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {
            "/": FakeDirectory(),
            "/home": FakeDirectory(),
            HOME: FakeDirectory(),
            "/tmp": FakeDirectory(),
            "/etc": FakeDirectory(),
            "/etc/passwd": FakeFile(b"root:x:0:0:root:/root:/bin/bash\n"),
            "/usr": FakeDirectory(),
        }

    def resolve(self, path: str) -> str:
        """`realpath` léxico: sigue symlinks en cada componente y anexa tal cual
        los componentes que no existen (para `Write`/`MakeDir` con padres nuevos)."""
        current = "/"
        for component in path.split("/"):
            if not component:
                continue
            current = join(current, component)
            node = self.nodes.get(current)
            if isinstance(node, FakeSymlink):
                target = node.target
                absolute = target if target.startswith("/") else join(parent_of(current), target)
                current = self.resolve(normalize(absolute))
        return current

    def canonical(self, path: str) -> str:
        """Padre resuelto + nombre final sin resolver (el símil de `O_NOFOLLOW`)."""
        if path == "/":
            return "/"
        return join(self.resolve(parent_of(path)), name_of(path))

    def children(self, canonical: str) -> list[str]:
        names = [
            name_of(path) for path in self.nodes if path != "/" and parent_of(path) == canonical
        ]
        return sorted(names, key=lambda name: name.encode())

    def subtree(self, canonical: str) -> list[str]:
        return [
            path
            for path in self.nodes
            if path == canonical or path.startswith(canonical.rstrip("/") + "/")
        ]

    def ensure_parents(self, canonical: str) -> None:
        parent = parent_of(canonical)
        missing: list[str] = []
        while parent not in self.nodes:
            missing.append(parent)
            parent = parent_of(parent)
        if not isinstance(self.nodes[parent], FakeDirectory):
            raise invalid("a parent component is not a directory")
        for path in reversed(missing):
            self.nodes[path] = FakeDirectory()


@dataclass
class OpenWrite:
    path: str
    canonical: str
    mode: int
    data: bytearray = field(default_factory=bytearray)
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass
class WatchEnd:
    code: grpc.StatusCode
    message: str


@dataclass
class FakeWatch:
    path: str
    request: filesystem_pb2.WatchDirRequest
    events: queue.Queue[filesystem_pb2.WatchDirResponse | WatchEnd] = field(
        default_factory=queue.Queue
    )


WriteMessageSummary = tuple[str | None, int, int | None, str | None]


@dataclass
class FakeFilesystemService(filesystem_pb2_grpc.FilesystemServiceServicer):
    """`FilesystemService` como lo implementa `rayd` en M3 (ver módulo)."""

    token_sha256: str
    allow_root: bool = False
    phase: str | None = None
    max_list_entries: int = MAX_LIST_ENTRIES
    watch_first_message: str = "started"
    tree: FakeTree = field(default_factory=FakeTree)
    read_calls: int = 0
    write_streams: list[list[WriteMessageSummary]] = field(default_factory=list)
    deadlines: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    metadata: dict[str, list[dict[str, str]]] = field(default_factory=lambda: defaultdict(list))
    peers: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    watches: dict[str, list[FakeWatch]] = field(default_factory=lambda: defaultdict(list))
    watch_requests: list[filesystem_pb2.WatchDirRequest] = field(default_factory=list)
    live_watches: int = 0
    persistence: FakePersistence = field(default_factory=FakePersistence)
    lock: threading.Lock = field(default_factory=threading.Lock)

    # ------------------------------------------------------------------ RPCs

    def Read(
        self, request: filesystem_pb2.ReadRequest, context: grpc.ServicerContext
    ) -> Iterator[filesystem_pb2.ReadResponse]:
        self._enter("Read", context)
        self._gate_phase(context)
        try:
            data = self._readable(request)
        except FakeStatus as status:
            context.abort(status.code, status.message)
        self.read_calls += 1
        return self._read_chunks(data)

    def Write(
        self,
        request_iterator: Iterator[filesystem_pb2.WriteRequest],
        context: grpc.ServicerContext,
    ) -> filesystem_pb2.WriteResponse:
        self._enter("Write", context)
        self._gate_phase(context)
        messages: list[WriteMessageSummary] = []
        entries: list[common_pb2.EntryInfo] = []
        current: OpenWrite | None = None
        try:
            for request in request_iterator:
                messages.append(summarize_write(request))
                chunk = bytes(request.chunk)
                if len(chunk) > MAX_WRITE_CHUNK_BYTES:
                    raise invalid("chunk exceeds 1 MiB")
                if request.HasField("path"):
                    if current is not None:
                        entries.append(self._commit(current))
                    current = self._begin(request)
                elif current is None:
                    raise invalid("first message must carry path")
                elif request.HasField("user") or request.HasField("mode") or request.metadata:
                    raise invalid("user, mode and metadata only travel with path")
                current.data.extend(chunk)
            if current is None:
                raise invalid("stream carried no files")
            entries.append(self._commit(current))
        except FakeStatus as status:
            context.abort(status.code, status.message)
        finally:
            self.write_streams.append(messages)
        return filesystem_pb2.WriteResponse(entries=entries)

    def Stat(
        self, request: filesystem_pb2.StatRequest, context: grpc.ServicerContext
    ) -> filesystem_pb2.StatResponse:
        self._enter("Stat", context)
        try:
            self._identity(request)
            path = normalize(request.path)
            node = self._existing(self._target(request.path))
        except FakeStatus as status:
            context.abort(status.code, status.message)
        return filesystem_pb2.StatResponse(entry=entry_info(path, node))

    def ListDir(
        self, request: filesystem_pb2.ListDirRequest, context: grpc.ServicerContext
    ) -> filesystem_pb2.ListDirResponse:
        self._enter("ListDir", context)
        try:
            self._identity(request)
            path = normalize(request.path)
            canonical = self._directory_root(path)
            entries = self._walk(path, canonical, max(1, int(request.depth)))
        except FakeStatus as status:
            context.abort(status.code, status.message)
        return filesystem_pb2.ListDirResponse(entries=entries)

    def MakeDir(
        self, request: filesystem_pb2.MakeDirRequest, context: grpc.ServicerContext
    ) -> filesystem_pb2.MakeDirResponse:
        self._enter("MakeDir", context)
        try:
            self._identity(request)
            path = normalize(request.path)
            canonical = self._target(request.path)
            existing = self.tree.nodes.get(canonical)
            if isinstance(existing, FakeDirectory):
                raise FakeStatus(grpc.StatusCode.ALREADY_EXISTS, "directory already exists")
            if existing is not None:
                raise invalid("exists and is not a directory")
            self.tree.ensure_parents(canonical)
            self.tree.nodes[canonical] = FakeDirectory()
        except FakeStatus as status:
            context.abort(status.code, status.message)
        return filesystem_pb2.MakeDirResponse(entry=entry_info(path, self.tree.nodes[canonical]))

    def Move(
        self, request: filesystem_pb2.MoveRequest, context: grpc.ServicerContext
    ) -> filesystem_pb2.MoveResponse:
        self._enter("Move", context)
        try:
            self._identity(request)
            destination = normalize(request.destination)
            source_canonical = self._target(request.source)
            source = self._existing(source_canonical)
            destination_canonical = self._target(request.destination)
            self._check_move_destination(source, destination_canonical)
            self._relocate(source_canonical, destination_canonical)
        except FakeStatus as status:
            context.abort(status.code, status.message)
        return filesystem_pb2.MoveResponse(
            entry=entry_info(destination, self.tree.nodes[destination_canonical])
        )

    def Remove(
        self, request: filesystem_pb2.RemoveRequest, context: grpc.ServicerContext
    ) -> filesystem_pb2.RemoveResponse:
        self._enter("Remove", context)
        try:
            self._identity(request)
            canonical = self._target(request.path)
            node = self._existing(canonical)
            if isinstance(node, FakeDirectory):
                if self.tree.children(canonical) and not request.recursive:
                    raise failed_precondition("directory not empty; use recursive")
                for path in self.tree.subtree(canonical):
                    del self.tree.nodes[path]
            else:
                del self.tree.nodes[canonical]
        except FakeStatus as status:
            context.abort(status.code, status.message)
        return filesystem_pb2.RemoveResponse()

    def WatchDir(
        self, request: filesystem_pb2.WatchDirRequest, context: grpc.ServicerContext
    ) -> Iterator[filesystem_pb2.WatchDirResponse]:
        self._enter("WatchDir", context)
        self._gate_phase(context)
        try:
            self._identity(request)
            path = normalize(request.path)
            self._directory_root(path)
        except FakeStatus as status:
            context.abort(status.code, status.message)
        watch = FakeWatch(path=path, request=request)
        with self.lock:
            self.watch_requests.append(request)
            self.watches[path].append(watch)
            self.live_watches += 1
        return self._watch_stream(watch, context)

    def Checkpoint(
        self, request: filesystem_pb2.CheckpointRequest, context: grpc.ServicerContext
    ) -> Iterator[filesystem_pb2.CheckpointEvent]:
        self._enter("Checkpoint", context)
        self._gate_phase(context)
        return self.persistence.checkpoint(request, context)

    def Restore(
        self, request: filesystem_pb2.RestoreRequest, context: grpc.ServicerContext
    ) -> Iterator[filesystem_pb2.RestoreEvent]:
        self._enter("Restore", context)
        self._gate_phase(context)
        return self.persistence.restore(request, context)

    # --------------------------------------------------------- test controls

    def push_event(
        self,
        path: str,
        name: str,
        event_type: filesystem_pb2.FilesystemEventType,
        entry: common_pb2.EntryInfo | None = None,
    ) -> None:
        """Entrega un `FilesystemEvent` a todos los watches vivos de `path`."""
        event = filesystem_pb2.FilesystemEvent(name=name, type=event_type)
        if entry is not None:
            event.entry.CopyFrom(entry)
        self._broadcast(path, filesystem_pb2.WatchDirResponse(filesystem=event))

    def push_keepalive(self, path: str) -> None:
        self._broadcast(path, filesystem_pb2.WatchDirResponse(keepalive=common_pb2.KeepAlive()))

    def end_watch(self, path: str, code: grpc.StatusCode, message: str = "watch ended") -> None:
        """Termina los watches de `path` con un status gRPC (p. ej. `NOT_FOUND`
        cuando el directorio observado desaparece)."""
        self._broadcast(path, WatchEnd(code, message))

    def suspend(self) -> None:
        """Lo que hace `/suspend`: todo `WatchDir` vivo termina con
        `UNAVAILABLE suspending` y los streams nuevos se rechazan igual."""
        with self.lock:
            targets = [watch for watches in self.watches.values() for watch in watches]
        for watch in targets:
            watch.events.put(WatchEnd(grpc.StatusCode.UNAVAILABLE, "suspending"))
        self.phase = "suspending"

    def resume(self) -> None:
        self.phase = None

    @property
    def watch_calls(self) -> list[filesystem_pb2.WatchDirRequest]:
        return self.watch_requests

    def node_at(self, path: str) -> Node | None:
        return self.tree.nodes.get(self.tree.canonical(normalize(path)))

    def file_bytes(self, path: str) -> bytes:
        node = self.node_at(path)
        assert isinstance(node, FakeFile), f"{path} no es un fichero del fake"
        return node.data

    def add_file(self, path: str, data: bytes, mode: int = DEFAULT_FILE_MODE) -> None:
        canonical = self.tree.canonical(normalize(path))
        self.tree.ensure_parents(canonical)
        self.tree.nodes[canonical] = FakeFile(data, mode)

    def add_dir(self, path: str) -> None:
        canonical = self.tree.canonical(normalize(path))
        self.tree.ensure_parents(canonical)
        self.tree.nodes[canonical] = FakeDirectory()

    def add_symlink(self, path: str, target: str) -> None:
        canonical = self.tree.canonical(normalize(path))
        self.tree.ensure_parents(canonical)
        self.tree.nodes[canonical] = FakeSymlink(target)

    def add_denied(self, path: str, data: bytes) -> None:
        """Siembra un fichero bajo un prefijo denegado sin pasar por la política."""
        canonical = normalize(path)
        self.tree.ensure_parents(canonical)
        self.tree.nodes[canonical] = FakeFile(data)

    # -------------------------------------------------------------- internals

    def _enter(self, rpc: str, context: grpc.ServicerContext) -> None:
        metadata = require_access_token(context, self.token_sha256)
        with self.lock:
            self.metadata[rpc].append(metadata)
            self.peers[rpc].add(str(context.peer()))
            self.deadlines[rpc].append(float(context.time_remaining()))

    def _gate_phase(self, context: grpc.ServicerContext) -> None:
        if self.phase is not None:
            context.abort(grpc.StatusCode.UNAVAILABLE, self.phase)

    def _identity(self, request: object) -> None:
        if not request.HasField("user"):  # type: ignore[attr-defined]
            return
        username = str(request.user.username)  # type: ignore[attr-defined]
        if username == "root" and not self.allow_root:
            raise FakeStatus(grpc.StatusCode.PERMISSION_DENIED, "root is not allowed")
        if username not in KNOWN_USERS:
            raise invalid("unknown user")

    def _target(self, raw: str) -> str:
        """Ruta canónica de un componente final sin resolver, ya pasada por la
        lista de denegación."""
        canonical = self.tree.canonical(normalize(raw))
        if is_denied(canonical):
            raise denied()
        return canonical

    def _directory_root(self, path: str) -> str:
        """Raíz de `ListDir`/`WatchDir`: se sigue el symlink final a propósito."""
        canonical = self.tree.resolve(path)
        if is_denied(canonical):
            raise denied()
        node = self.tree.nodes.get(canonical)
        if node is None:
            raise not_found()
        if not isinstance(node, FakeDirectory):
            raise invalid("path is not a directory")
        return canonical

    def _existing(self, canonical: str) -> Node:
        node = self.tree.nodes.get(canonical)
        if node is None:
            raise not_found()
        return node

    def _readable(self, request: filesystem_pb2.ReadRequest) -> bytes:
        self._identity(request)
        node = self._existing(self._target(request.path))
        if isinstance(node, FakeDirectory):
            raise invalid("path is a directory")
        if isinstance(node, FakeSymlink):
            raise invalid("path is a symlink")
        return node.data

    def _read_chunks(self, data: bytes) -> Iterator[filesystem_pb2.ReadResponse]:
        for offset in range(0, len(data), READ_CHUNK_BYTES):
            yield filesystem_pb2.ReadResponse(chunk=data[offset : offset + READ_CHUNK_BYTES])

    def _begin(self, request: filesystem_pb2.WriteRequest) -> OpenWrite:
        self._identity(request)
        mode = int(request.mode) if request.HasField("mode") else DEFAULT_FILE_MODE
        if mode > MODE_MAX:
            raise invalid("mode exceeds 0o7777")
        path = normalize(request.path)
        canonical = self._target(request.path)
        if isinstance(self.tree.nodes.get(canonical), FakeDirectory):
            raise invalid("destination is a directory")
        self.tree.ensure_parents(canonical)
        metadata = {str(key).lower(): str(value) for key, value in request.metadata.items()}
        return OpenWrite(path=path, canonical=canonical, mode=mode, metadata=metadata)

    def _commit(self, open_write: OpenWrite) -> common_pb2.EntryInfo:
        node = FakeFile(bytes(open_write.data), open_write.mode, dict(open_write.metadata))
        self.tree.nodes[open_write.canonical] = node
        return entry_info(open_write.path, node)

    def _walk(self, root: str, canonical_root: str, depth: int) -> list[common_pb2.EntryInfo]:
        entries: list[common_pb2.EntryInfo] = []
        self._visit(root, canonical_root, 1, depth, entries)
        return entries

    def _visit(
        self,
        path: str,
        canonical: str,
        level: int,
        depth: int,
        entries: list[common_pb2.EntryInfo],
    ) -> None:
        for name in self.tree.children(canonical):
            child_canonical = join(canonical, name)
            node = self.tree.nodes[child_canonical]
            child_path = join(path, name)
            entries.append(entry_info(child_path, node))
            if len(entries) > self.max_list_entries:
                raise FakeStatus(
                    grpc.StatusCode.RESOURCE_EXHAUSTED,
                    f"listing exceeds {self.max_list_entries} entries; reduce depth",
                )
            descend = isinstance(node, FakeDirectory) and level < depth
            if descend and not is_denied(child_canonical):
                self._visit(child_path, child_canonical, level + 1, depth, entries)

    def _check_move_destination(self, source: Node, destination_canonical: str) -> None:
        parent = self.tree.nodes.get(parent_of(destination_canonical))
        if parent is None:
            raise not_found()
        if not isinstance(parent, FakeDirectory):
            raise failed_precondition("destination conflicts with an existing entry")
        existing = self.tree.nodes.get(destination_canonical)
        if existing is None:
            return
        source_is_dir = isinstance(source, FakeDirectory)
        existing_is_dir = isinstance(existing, FakeDirectory)
        if source_is_dir != existing_is_dir:
            raise failed_precondition("destination conflicts with an existing entry")
        if existing_is_dir and self.tree.children(destination_canonical):
            raise failed_precondition("destination conflicts with an existing entry")

    def _relocate(self, source_canonical: str, destination_canonical: str) -> None:
        moved = {
            destination_canonical + path[len(source_canonical) :]: self.tree.nodes.pop(path)
            for path in self.tree.subtree(source_canonical)
        }
        self.tree.nodes.update(moved)

    def _watch_stream(
        self, watch: FakeWatch, context: grpc.ServicerContext
    ) -> Iterator[filesystem_pb2.WatchDirResponse]:
        try:
            yield self._first_watch_message()
            yield filesystem_pb2.WatchDirResponse(keepalive=common_pb2.KeepAlive())
            while context.is_active():
                try:
                    item = watch.events.get(timeout=WATCH_POLL_SECONDS)
                except queue.Empty:
                    continue
                if isinstance(item, WatchEnd):
                    context.abort(item.code, item.message)
                yield item
        finally:
            self._release(watch)

    def _first_watch_message(self) -> filesystem_pb2.WatchDirResponse:
        if self.watch_first_message == "started":
            return filesystem_pb2.WatchDirResponse(started=filesystem_pb2.WatchStarted())
        return filesystem_pb2.WatchDirResponse(keepalive=common_pb2.KeepAlive())

    def _release(self, watch: FakeWatch) -> None:
        with self.lock:
            if watch in self.watches[watch.path]:
                self.watches[watch.path].remove(watch)
                self.live_watches -= 1

    def _broadcast(self, path: str, item: filesystem_pb2.WatchDirResponse | WatchEnd) -> None:
        with self.lock:
            targets = list(self.watches[normalize(path)])
        for watch in targets:
            watch.events.put(item)


def summarize_write(request: filesystem_pb2.WriteRequest) -> WriteMessageSummary:
    return (
        str(request.path) if request.HasField("path") else None,
        len(request.chunk),
        int(request.mode) if request.HasField("mode") else None,
        str(request.user.username) if request.HasField("user") else None,
    )
