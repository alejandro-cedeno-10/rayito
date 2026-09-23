"""`AsyncSandbox.files` y `AsyncWatchHandle`: misma superficie que la versión
síncrona sobre `grpc.aio`, contra el mismo `rayd` falso."""

from __future__ import annotations

import asyncio
import io
import os
import time
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import grpc
import pytest

from rayito import (
    AsyncSandbox,
    AsyncWatchHandle,
    FilesystemEvent,
    FilesystemEventType,
    FileType,
    WriteEntry,
)
from rayito._filesystem_base import READ_CHUNK_BYTES, WRITE_CHUNK_BYTES, file_request_deadline
from rayito._limits import DEFAULT_PORT
from rayito._transport import PROXY_AUTH_KEY, PROXY_FORBIDDEN_MARKER
from rayito.exceptions import (
    AuthenticationException,
    FileNotFoundException,
    InvalidArgumentException,
    RateLimitException,
    SandboxException,
    TimeoutException,
    UnimplementedError,
)
from rayito.v1 import common_pb2, filesystem_pb2

from .conftest import (
    ACCESS_TOKEN,
    IMAGE_ARN,
    SANDBOX_ID,
    FakeRpcError,
    RaydEndpoint,
    StubbedControlPlane,
    always_running,
    auth_token_response,
    microvm_response,
)
from .fake_filesystem import FakeFilesystemService

HOME = "/home/user"
CREATE = filesystem_pb2.FILESYSTEM_EVENT_TYPE_CREATE
WRITE = filesystem_pb2.FILESYSTEM_EVENT_TYPE_WRITE
CHMOD = filesystem_pb2.FILESYSTEM_EVENT_TYPE_CHMOD
WAIT_BUDGET_SECONDS = 5.0
POLL_SECONDS = 0.02
DEADLINE_SLACK_SECONDS = 5.0
DEADLINE_ROUNDING_SECONDS = 1.0


def stub_launch(control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint) -> None:
    control_plane.microvms.add_response("run_microvm", microvm_response(endpoint=fake_rayd.host))
    control_plane.microvms.add_response(
        "create_microvm_auth_token",
        auth_token_response(),
        expected_params={
            "microvmIdentifier": SANDBOX_ID,
            "expirationInMinutes": 60,
            "allowedPorts": [{"port": DEFAULT_PORT}],
        },
    )


def stub_remint(control_plane: StubbedControlPlane, jwe: str) -> None:
    control_plane.microvms.add_response(
        "create_microvm_auth_token",
        auth_token_response(jwe),
        expected_params={
            "microvmIdentifier": SANDBOX_ID,
            "expirationInMinutes": 60,
            "allowedPorts": [{"port": DEFAULT_PORT}],
        },
    )


@pytest.fixture
async def sandbox(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> AsyncIterator[AsyncSandbox]:
    stub_launch(control_plane, fake_rayd)
    created = await AsyncSandbox.create(
        IMAGE_ARN,
        idle=None,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    )
    try:
        yield created
    finally:
        control_plane.microvms.add_response(
            "terminate_microvm", {}, expected_params={"microvmIdentifier": SANDBOX_ID}
        )
        await created.kill()


@pytest.fixture
def fake_files(fake_rayd: RaydEndpoint) -> FakeFilesystemService:
    return fake_rayd.filesystem


def proxy_forbidden() -> FakeRpcError:
    return FakeRpcError(
        grpc.StatusCode.PERMISSION_DENIED,
        details="Received http2 header with status: 403",
        debug=f'{{"grpc_status":7,"description":"{PROXY_FORBIDDEN_MARKER}"}}',
    )


async def failing_awaitable(error: grpc.RpcError) -> Any:
    raise error


class ScriptedCall:
    """Una `UnaryStreamCall` de `grpc.aio` con mensajes fijos y un error final."""

    def __init__(self, events: list[Any], error: grpc.RpcError) -> None:
        self._events = list(events)
        self._error = error

    async def read(self) -> Any:
        if self._events:
            return self._events.pop(0)
        raise self._error

    def cancel(self) -> bool:
        return True


async def wait_until(predicate: Callable[[], bool], timeout: float = WAIT_BUDGET_SECONDS) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "la condición no se cumplió a tiempo"
        await asyncio.sleep(POLL_SECONDS)


async def collect_events(handle: AsyncWatchHandle, count: int) -> list[FilesystemEvent]:
    collected: list[FilesystemEvent] = []
    deadline = time.monotonic() + WAIT_BUDGET_SECONDS
    while len(collected) < count:
        assert time.monotonic() < deadline, f"llegaron {len(collected)} de {count} eventos"
        collected.extend(await handle.get_new_events())
        await asyncio.sleep(POLL_SECONDS)
    return collected


def unary_peers(fake_files: FakeFilesystemService) -> set[str]:
    return set().union(*(peers for rpc, peers in fake_files.peers.items() if rpc != "WatchDir"))


async def test_async_write_read_round_trip_and_formats(
    sandbox: AsyncSandbox, fake_files: FakeFilesystemService
) -> None:
    data = os.urandom(600 * 1024)
    info = await sandbox.files.write(f"{HOME}/data.bin", data)
    assert (info.name, info.size, info.type) == ("data.bin", len(data), FileType.FILE)
    assert (info.mode, info.permissions, info.owner) == (0o644, "-rw-r--r--", "user")
    assert await sandbox.files.read(f"{HOME}/data.bin", format="bytes") == data
    stream = await sandbox.files.read(f"{HOME}/data.bin", format="stream")
    chunks = [chunk async for chunk in stream]
    assert [len(chunk) for chunk in chunks] == [READ_CHUNK_BYTES, READ_CHUNK_BYTES, 90_112]
    assert b"".join(chunks) == data
    await sandbox.files.write(f"{HOME}/hola.txt", "hola ñ\n")
    assert await sandbox.files.read(f"{HOME}/hola.txt") == "hola ñ\n"
    await sandbox.files.write("rel.txt", io.StringIO("rel"), mode=0o600)
    assert await sandbox.files.read(f"{HOME}/rel.txt") == "rel"
    assert (await sandbox.files.get_info(f"{HOME}/rel.txt")).mode == 0o600
    fake_files.add_file(f"{HOME}/binary", b"\xff\xfe")
    with pytest.raises(InvalidArgumentException, match="UTF-8"):
        await sandbox.files.read(f"{HOME}/binary")
    fake_files.add_file(f"{HOME}/empty", b"")
    assert await sandbox.files.read(f"{HOME}/empty", format="bytes") == b""
    empty_stream = await sandbox.files.read(f"{HOME}/empty", format="stream")
    assert [chunk async for chunk in empty_stream] == []
    expected = file_request_deadline(len(data), None)
    assert (
        expected - DEADLINE_SLACK_SECONDS
        < fake_files.deadlines["Write"][0]
        <= expected + DEADLINE_ROUNDING_SECONDS
    )
    assert (
        expected - DEADLINE_SLACK_SECONDS
        < fake_files.deadlines["Read"][0]
        <= expected + DEADLINE_ROUNDING_SECONDS
    )


async def test_async_read_refuses_directories_and_symlinks_client_side(
    sandbox: AsyncSandbox, fake_files: FakeFilesystemService
) -> None:
    fake_files.add_file(f"{HOME}/a", b"a")
    fake_files.add_symlink(f"{HOME}/link", "a")
    with pytest.raises(InvalidArgumentException):
        await sandbox.files.read(HOME)
    with pytest.raises(InvalidArgumentException):
        await sandbox.files.read(f"{HOME}/link")
    with pytest.raises(FileNotFoundException):
        await sandbox.files.read(f"{HOME}/missing")
    assert fake_files.read_calls == 0


async def test_async_write_files_list_exists_and_get_info(
    sandbox: AsyncSandbox, fake_files: FakeFilesystemService
) -> None:
    files = [WriteEntry(f"{HOME}/many/f{i:02}.txt", f"file {i}\n".encode()) for i in range(50)]
    entries = await sandbox.files.write_files(files)
    assert [entry.name for entry in entries] == [f"f{i:02}.txt" for i in range(50)]
    assert len(fake_files.write_streams) == 1
    fake_files.add_file(f"{HOME}/big.bin", b"b")
    fake_files.add_symlink(f"{HOME}/link", "big.bin")
    listing = await sandbox.files.list(HOME, depth=2)
    assert [entry.name for entry in listing[:3]] == ["big.bin", "link", "many"]
    assert listing[2].type is FileType.DIR
    assert [entry.path for entry in listing[3:]] == [entry.path for entry in files]
    assert [entry.name for entry in await sandbox.files.list(HOME)] == ["big.bin", "link", "many"]
    assert await sandbox.files.list(HOME, depth=0) == await sandbox.files.list(HOME)
    with pytest.raises(InvalidArgumentException):
        await sandbox.files.list(f"{HOME}/big.bin")
    with pytest.raises(FileNotFoundException):
        await sandbox.files.list(f"{HOME}/nope")
    fake_files.max_list_entries = 3
    with pytest.raises(RateLimitException):
        await sandbox.files.list(HOME, depth=2)
    assert await sandbox.files.exists(f"{HOME}/missing") is False
    assert await sandbox.files.exists(f"{HOME}/big.bin") is True
    with pytest.raises(FileNotFoundException):
        await sandbox.files.get_info(f"{HOME}/missing")
    link = await sandbox.files.get_info(f"{HOME}/link")
    assert (link.type, link.symlink_target) == (FileType.SYMLINK, "big.bin")


async def test_async_make_dir_rename_and_remove(
    sandbox: AsyncSandbox, fake_files: FakeFilesystemService
) -> None:
    assert await sandbox.files.make_dir(f"{HOME}/dir") is True
    assert await sandbox.files.make_dir(f"{HOME}/dir") is False
    fake_files.add_file(f"{HOME}/file", b"")
    with pytest.raises(InvalidArgumentException):
        await sandbox.files.make_dir(f"{HOME}/file")
    assert await sandbox.files.make_dir(f"{HOME}/a/b/c") is True
    await sandbox.files.write(f"{HOME}/hola.txt", "hola")
    moved = await sandbox.files.rename(f"{HOME}/hola.txt", f"{HOME}/dir/hola.txt")
    assert moved.path == f"{HOME}/dir/hola.txt"
    assert await sandbox.files.exists(f"{HOME}/hola.txt") is False
    assert await sandbox.files.read(f"{HOME}/dir/hola.txt") == "hola"
    with pytest.raises(FileNotFoundException):
        await sandbox.files.rename(f"{HOME}/missing", f"{HOME}/x")
    with pytest.raises(InvalidArgumentException):
        await sandbox.files.rename(f"{HOME}/dir/hola.txt", f"{HOME}/a")
    with pytest.raises(InvalidArgumentException):
        await sandbox.files.remove(f"{HOME}/dir", recursive=False)
    await sandbox.files.remove(f"{HOME}/dir")
    assert await sandbox.files.exists(f"{HOME}/dir") is False
    with pytest.raises(FileNotFoundException):
        await sandbox.files.remove(f"{HOME}/dir")
    fake_files.add_symlink(f"{HOME}/link", "file")
    await sandbox.files.remove(f"{HOME}/link")
    assert await sandbox.files.exists(f"{HOME}/file") is True


async def test_async_path_policy_and_client_validation(
    sandbox: AsyncSandbox, fake_files: FakeFilesystemService
) -> None:
    with pytest.raises(AuthenticationException) as excinfo:
        await sandbox.files.read("/etc/passwd")
    assert excinfo.value.proxy_rejected is False
    with pytest.raises(AuthenticationException):
        await sandbox.files.write("/usr/local/bin/rayd", b"x")
    with pytest.raises(AuthenticationException):
        await sandbox.files.list("/etc")
    with pytest.raises(InvalidArgumentException):
        await sandbox.files.read(f"{HOME}/../../etc/passwd")
    with pytest.raises(AuthenticationException) as root:
        await sandbox.files.write(f"{HOME}/x", b"x", user="root")
    assert root.value.proxy_rejected is False
    streams_before = len(fake_files.write_streams)
    with pytest.raises(InvalidArgumentException):
        await sandbox.files.write_files([])
    with pytest.raises(InvalidArgumentException):
        await sandbox.files.write(f"{HOME}/x", b"", mode=0o10000)
    with pytest.raises(InvalidArgumentException):
        await sandbox.files.read(f"{HOME}/x", format="nope")  # type: ignore[call-overload]
    assert len(fake_files.write_streams) == streams_before
    assert fake_files.read_calls == 0


def running(handle: AsyncWatchHandle) -> bool:
    """Lectura fresca: `stop()` cambia `is_running` y mypy no lo sabe."""
    return handle.is_running


def stream_channel_of(sandbox: AsyncSandbox) -> object:
    """Lectura fresca del canal de streams (se abre en el primer uso)."""
    return sandbox._stream_channel


async def test_async_watch_dir_events_stop_and_channel_usage(
    sandbox: AsyncSandbox, fake_files: FakeFilesystemService
) -> None:
    await sandbox.files.get_info(HOME)
    assert stream_channel_of(sandbox) is None
    handle = await sandbox.files.watch_dir(HOME, recursive=True)
    assert stream_channel_of(sandbox) is not None
    assert fake_files.peers["WatchDir"].isdisjoint(unary_peers(fake_files))
    assert handle.is_running is True
    assert handle.path == HOME
    assert fake_files.watch_requests[-1].recursive is True
    fake_files.push_event(HOME, "z.txt", CREATE)
    fake_files.push_keepalive(HOME)
    fake_files.push_event(HOME, "sub/z.txt", WRITE)
    events = await collect_events(handle, 2)
    assert [(event.name, event.type) for event in events] == [
        ("z.txt", FilesystemEventType.CREATE),
        ("sub/z.txt", FilesystemEventType.WRITE),
    ]
    await handle.stop()
    await handle.stop()
    assert running(handle) is False
    assert await handle.get_new_events() == []
    await wait_until(lambda: fake_files.live_watches == 0)


async def test_async_watch_dir_callback_include_entry_and_context_manager(
    sandbox: AsyncSandbox, fake_files: FakeFilesystemService
) -> None:
    seen: list[FilesystemEvent] = []
    async with await sandbox.files.watch_dir(
        HOME, on_event=seen.append, include_entry=True
    ) as handle:
        entry = common_pb2.EntryInfo(name="w", type=common_pb2.FILE_TYPE_FILE, mode=0o600)
        fake_files.push_event(HOME, "w", CHMOD, entry)
        await wait_until(lambda: len(seen) == 1)
        assert seen[0].type is FilesystemEventType.CHMOD
        assert seen[0].entry is not None
        assert seen[0].entry.mode == 0o600
        assert await handle.get_new_events() == []
    assert handle.is_running is False


async def test_async_watch_dir_terminal_error_timeout_and_early_errors(
    sandbox: AsyncSandbox, fake_files: FakeFilesystemService
) -> None:
    exits: list[Exception] = []
    handle = await sandbox.files.watch_dir(HOME, on_exit=exits.append)
    fake_files.push_event(HOME, "x", CREATE)
    fake_files.end_watch(HOME, grpc.StatusCode.NOT_FOUND, "watched directory removed")
    await wait_until(lambda: not handle.is_running)
    assert len(exits) == 1
    assert isinstance(exits[0], FileNotFoundException)
    assert [event.name for event in await handle.get_new_events()] == ["x"]
    with pytest.raises(FileNotFoundException):
        await handle.get_new_events()
    assert await handle.get_new_events() == []
    await handle.stop()

    timed = await sandbox.files.watch_dir(HOME, timeout=0.5)
    await wait_until(lambda: not timed.is_running)
    with pytest.raises(TimeoutException):
        await timed.get_new_events()

    with pytest.raises(FileNotFoundException):
        await sandbox.files.watch_dir(f"{HOME}/nope")
    fake_files.add_file(f"{HOME}/file", b"")
    with pytest.raises(InvalidArgumentException):
        await sandbox.files.watch_dir(f"{HOME}/file")
    fake_files.watch_first_message = "keepalive"
    with pytest.raises(SandboxException, match="WatchStarted"):
        await sandbox.files.watch_dir(HOME)
    await wait_until(lambda: fake_files.live_watches == 0)


async def test_async_watch_dir_unknown_event_type_ends_the_watch_and_releases_the_stream(
    sandbox: AsyncSandbox, fake_files: FakeFilesystemService
) -> None:
    exits: list[Exception] = []
    handle = await sandbox.files.watch_dir(HOME, on_exit=exits.append)
    fake_files.push_event(HOME, "x", filesystem_pb2.FILESYSTEM_EVENT_TYPE_UNSPECIFIED)
    await wait_until(lambda: not handle.is_running)
    await wait_until(lambda: fake_files.live_watches == 0)
    assert len(exits) == 1
    assert isinstance(exits[0], SandboxException)
    with pytest.raises(SandboxException, match="desconocido"):
        await handle.get_new_events()
    assert await handle.get_new_events() == []
    assert sandbox.files._watches == set()


@pytest.mark.parametrize("task_already_running", [True, False])
async def test_async_watch_task_cancelled_externally_fails_the_watch_and_releases_the_stream(
    sandbox: AsyncSandbox, fake_files: FakeFilesystemService, task_already_running: bool
) -> None:
    exits: list[Exception] = []
    handle = await sandbox.files.watch_dir(HOME, on_exit=exits.append)
    assert handle._task is not None
    if task_already_running:
        await asyncio.sleep(0)
    handle._task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await handle._task
    assert handle.is_running is False
    await wait_until(lambda: fake_files.live_watches == 0)
    assert len(exits) == 1
    assert isinstance(exits[0], SandboxException)
    with pytest.raises(SandboxException, match="stop"):
        await handle.get_new_events()
    assert sandbox.files._watches == set()


async def test_async_close_stops_live_watches(
    sandbox: AsyncSandbox, fake_files: FakeFilesystemService
) -> None:
    first = await sandbox.files.watch_dir(HOME)
    second = await sandbox.files.watch_dir("/tmp")
    assert fake_files.live_watches == 2
    await sandbox.close()
    assert first.is_running is False
    assert second.is_running is False
    assert await first.get_new_events() == []
    await wait_until(lambda: fake_files.live_watches == 0)


async def test_async_proxy_403_on_write_remints_once(
    sandbox: AsyncSandbox,
    fake_files: FakeFilesystemService,
    control_plane: StubbedControlPlane,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_remint(control_plane, "jwe-2")
    data = os.urandom(WRITE_CHUNK_BYTES + 5)
    real = sandbox._files.Write
    failures = [proxy_forbidden()]

    def write(request_iterator: Iterator[Any], timeout: float | None = None) -> Any:
        if failures:
            next(request_iterator)
            return failing_awaitable(failures.pop(0))
        return real(request_iterator, timeout=timeout)

    monkeypatch.setattr(sandbox._files, "Write", write)
    info = await sandbox.files.write(f"{HOME}/big.bin", data)
    assert info.size == len(data)
    assert len(fake_files.write_streams) == 1
    assert fake_files.file_bytes(f"{HOME}/big.bin") == data
    assert fake_files.metadata["Write"][-1][PROXY_AUTH_KEY] == "jwe-2"
    control_plane.microvms.assert_no_pending_responses()


async def test_async_stream_reset_on_read_and_watch(
    sandbox: AsyncSandbox,
    fake_rayd: RaydEndpoint,
    fake_files: FakeFilesystemService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset = FakeRpcError(grpc.StatusCode.UNAVAILABLE, details="Socket closed")
    monkeypatch.setattr(
        sandbox._files,
        "Read",
        lambda request, timeout=None: ScriptedCall(
            [filesystem_pb2.ReadResponse(chunk=b"x")], reset
        ),
    )
    fake_files.add_file(f"{HOME}/a", b"xy")
    health_calls = len(fake_rayd.servicer.health_calls)
    with pytest.raises(SandboxException, match="stream cortado"):
        await sandbox.files.read(f"{HOME}/a", format="bytes")
    assert len(fake_rayd.servicer.health_calls) == health_calls + 1

    stream_stub = sandbox._stub(type(sandbox._files), stream=True)
    monkeypatch.setattr(
        stream_stub,
        "WatchDir",
        lambda request, timeout=None: ScriptedCall(
            [filesystem_pb2.WatchDirResponse(started=filesystem_pb2.WatchStarted())], reset
        ),
    )
    always_running(monkeypatch, sandbox, fake_rayd.host)
    exits: list[Exception] = []
    handle = await sandbox.files.watch_dir(HOME, on_exit=exits.append)
    await wait_until(lambda: not handle.is_running)
    assert handle.reconnects == 3
    assert len(exits) == 1
    assert "stream cortado" in str(exits[0])
    with pytest.raises(SandboxException, match="stream cortado"):
        await handle.get_new_events()


async def test_async_older_agent_refuses_metadata_and_gzip_writes_before_any_byte(
    sandbox: AsyncSandbox, fake_files: FakeFilesystemService
) -> None:
    """Misma regla que la versión síncrona sobre el `rayd` falso base."""
    with pytest.raises(UnimplementedError, match="actualiza la imagen"):
        await sandbox.files.write(f"{HOME}/m.txt", "x", metadata={"owner": "alice"})
    with pytest.raises(UnimplementedError, match="actualiza la imagen"):
        await sandbox.files.write(f"{HOME}/z.txt", "x", gzip=True)
    assert fake_files.write_streams == []
    fake_files.add_file(f"{HOME}/plain.txt", b"hola")
    assert await sandbox.files.read(f"{HOME}/plain.txt", gzip=True, stream_idle_timeout=5) == "hola"
