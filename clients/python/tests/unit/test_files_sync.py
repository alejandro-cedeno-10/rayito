"""`Sandbox.files` y `WatchHandle` contra el `rayd` falso (gRPC real en
loopback) y el plano de control con Stubber."""

from __future__ import annotations

import io
import os
import time
from collections.abc import Callable, Iterator
from datetime import UTC
from typing import Any

import grpc
import pytest

from rayito import (
    FilesystemEvent,
    FilesystemEventType,
    FileType,
    Sandbox,
    WatchHandle,
    WriteEntry,
)
from rayito._filesystem_base import READ_CHUNK_BYTES, WRITE_CHUNK_BYTES, file_request_deadline
from rayito._limits import DEFAULT_PORT
from rayito._payload import generate_access_token
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
from rayito.v1 import common_pb2, filesystem_pb2, filesystem_pb2_grpc

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
from .fake_filesystem import MODIFIED_UNIX_MS, FakeFilesystemService

HOME = "/home/user"
WATCH = f"{HOME}/watch"
CREATE = filesystem_pb2.FILESYSTEM_EVENT_TYPE_CREATE
WRITE = filesystem_pb2.FILESYSTEM_EVENT_TYPE_WRITE
REMOVE = filesystem_pb2.FILESYSTEM_EVENT_TYPE_REMOVE
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
def sandbox(control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint) -> Iterator[Sandbox]:
    stub_launch(control_plane, fake_rayd)
    created = Sandbox.create(
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
        created.kill()


@pytest.fixture
def fake_files(fake_rayd: RaydEndpoint) -> FakeFilesystemService:
    return fake_rayd.filesystem


def proxy_forbidden() -> FakeRpcError:
    return FakeRpcError(
        grpc.StatusCode.PERMISSION_DENIED,
        details="Received http2 header with status: 403",
        debug=f'{{"grpc_status":7,"description":"{PROXY_FORBIDDEN_MARKER}"}}',
    )


def running(handle: WatchHandle) -> bool:
    """Lectura fresca: `stop()` cambia `is_running` y mypy no lo sabe."""
    return handle.is_running


def stream_channel_of(sandbox: Sandbox) -> object:
    return sandbox._stream_channel


def failing_stream(error: grpc.RpcError) -> Iterator[Any]:
    yield from ()
    raise error


class ScriptedStream:
    """Un server-stream síncrono con mensajes fijos y un error final."""

    def __init__(self, events: list[Any], error: grpc.RpcError) -> None:
        self._events = list(events)
        self._error = error

    def __iter__(self) -> ScriptedStream:
        return self

    def __next__(self) -> Any:
        if self._events:
            return self._events.pop(0)
        raise self._error

    def cancel(self) -> bool:
        return True


def wait_until(predicate: Callable[[], bool], timeout: float = WAIT_BUDGET_SECONDS) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "la condición no se cumplió a tiempo"
        time.sleep(POLL_SECONDS)


def collect_events(handle: WatchHandle, count: int) -> list[FilesystemEvent]:
    collected: list[FilesystemEvent] = []
    deadline = time.monotonic() + WAIT_BUDGET_SECONDS
    while len(collected) < count:
        assert time.monotonic() < deadline, f"llegaron {len(collected)} de {count} eventos"
        collected.extend(handle.get_new_events())
        time.sleep(POLL_SECONDS)
    return collected


def unary_peers(fake_files: FakeFilesystemService) -> set[str]:
    return set().union(*(peers for rpc, peers in fake_files.peers.items() if rpc != "WatchDir"))


def test_write_returns_entry_info_and_reaches_the_fake(
    sandbox: Sandbox, fake_files: FakeFilesystemService
) -> None:
    info = sandbox.files.write(f"{HOME}/hola.txt", "hola ñ\n")
    assert info.name == "hola.txt"
    assert info.path == f"{HOME}/hola.txt"
    assert info.type is FileType.FILE
    assert info.size == len("hola ñ\n".encode())
    assert info.mode == 0o644
    assert info.permissions == "-rw-r--r--"
    assert (info.owner, info.group) == ("user", "user")
    assert info.symlink_target is None
    assert info.modified_time.tzinfo is UTC
    assert int(info.modified_time.timestamp() * 1000) == MODIFIED_UNIX_MS
    assert fake_files.file_bytes(f"{HOME}/hola.txt") == "hola ñ\n".encode()
    assert fake_files.write_streams[-1] == [(f"{HOME}/hola.txt", 8, None, None)]


def test_write_mode_user_file_objects_relative_paths_and_parents(
    sandbox: Sandbox, fake_files: FakeFilesystemService
) -> None:
    secret = sandbox.files.write(f"{HOME}/secret", b"s", mode=0o600, user="user")
    assert (secret.mode, secret.permissions) == (0o600, "-rw-------")
    assert fake_files.write_streams[-1] == [(f"{HOME}/secret", 1, 0o600, "user")]
    sandbox.files.write("rel.bin", io.BytesIO(b"bin"))
    assert sandbox.files.exists(f"{HOME}/rel.bin") is True
    assert sandbox.files.read(f"{HOME}/rel.bin", format="bytes") == b"bin"
    empty = sandbox.files.write(f"{HOME}/empty", b"")
    assert empty.size == 0
    assert fake_files.write_streams[-1] == [(f"{HOME}/empty", 0, None, None)]
    sandbox.files.write(f"{HOME}/nested/deep/file.txt", io.StringIO("x"))
    assert sandbox.files.get_info(f"{HOME}/nested/deep").type is FileType.DIR


def test_write_files_sends_one_stream_in_order(
    sandbox: Sandbox, fake_files: FakeFilesystemService
) -> None:
    files = [WriteEntry(f"{HOME}/many/f{i:02}.txt", f"file {i}\n".encode()) for i in range(50)]
    entries = sandbox.files.write_files(files)
    assert [entry.name for entry in entries] == [f"f{i:02}.txt" for i in range(50)]
    assert [entry.path for entry in entries] == [entry.path for entry in files]
    assert all(entry.size == len(f"file {i}\n") for i, entry in enumerate(entries))
    assert len(fake_files.write_streams) == 1
    assert [message[0] is not None for message in fake_files.write_streams[0]] == [True] * 50
    with pytest.raises(InvalidArgumentException):
        sandbox.files.write_files([])
    assert len(fake_files.write_streams) == 1


def test_write_chunks_at_one_mib_and_uses_the_size_deadline(
    sandbox: Sandbox, fake_files: FakeFilesystemService
) -> None:
    data = os.urandom(3 * WRITE_CHUNK_BYTES + 1)
    info = sandbox.files.write(f"{HOME}/big.bin", data)
    assert info.size == len(data)
    messages = fake_files.write_streams[-1]
    assert [(message[0] is not None, message[1]) for message in messages] == [
        (True, WRITE_CHUNK_BYTES),
        (False, WRITE_CHUNK_BYTES),
        (False, WRITE_CHUNK_BYTES),
        (False, 1),
    ]
    assert fake_files.file_bytes(f"{HOME}/big.bin") == data
    expected = file_request_deadline(len(data), None)
    remaining = fake_files.deadlines["Write"][-1]
    assert expected - DEADLINE_SLACK_SECONDS < remaining <= expected + DEADLINE_ROUNDING_SECONDS
    sandbox.files.write(f"{HOME}/small", b"x", request_timeout=7)
    assert (
        7 - DEADLINE_SLACK_SECONDS
        < fake_files.deadlines["Write"][-1]
        <= 7 + DEADLINE_ROUNDING_SECONDS
    )


def test_read_formats_chunking_and_deadline(
    sandbox: Sandbox, fake_files: FakeFilesystemService
) -> None:
    data = os.urandom(600 * 1024)
    fake_files.add_file(f"{HOME}/data.bin", data)
    assert sandbox.files.read(f"{HOME}/data.bin", format="bytes") == data
    chunks = list(sandbox.files.read(f"{HOME}/data.bin", format="stream"))
    assert [len(chunk) for chunk in chunks] == [READ_CHUNK_BYTES, READ_CHUNK_BYTES, 90_112]
    assert b"".join(chunks) == data
    expected = file_request_deadline(len(data), None)
    assert (
        expected - DEADLINE_SLACK_SECONDS
        < fake_files.deadlines["Read"][-1]
        <= expected + DEADLINE_ROUNDING_SECONDS
    )
    fake_files.add_file(f"{HOME}/hola.txt", "hola ñ\n".encode())
    assert sandbox.files.read(f"{HOME}/hola.txt") == "hola ñ\n"
    assert sandbox.files.read("hola.txt", format="text") == "hola ñ\n"
    fake_files.add_file(f"{HOME}/binary", b"\xff\xfe\x00")
    with pytest.raises(InvalidArgumentException, match="UTF-8"):
        sandbox.files.read(f"{HOME}/binary")
    fake_files.add_file(f"{HOME}/empty", b"")
    assert sandbox.files.read(f"{HOME}/empty", format="bytes") == b""
    assert sandbox.files.read(f"{HOME}/empty") == ""
    assert list(sandbox.files.read(f"{HOME}/empty", format="stream")) == []


def test_read_refuses_directories_and_symlinks_before_opening_the_stream(
    sandbox: Sandbox, fake_files: FakeFilesystemService
) -> None:
    fake_files.add_file(f"{HOME}/a.txt", b"a")
    fake_files.add_symlink(f"{HOME}/link", "a.txt")
    with pytest.raises(InvalidArgumentException, match="directorio"):
        sandbox.files.read(HOME)
    with pytest.raises(InvalidArgumentException, match="simb"):
        sandbox.files.read(f"{HOME}/link")
    with pytest.raises(FileNotFoundException):
        sandbox.files.read(f"{HOME}/missing")
    with pytest.raises(InvalidArgumentException, match="format"):
        sandbox.files.read(f"{HOME}/a.txt", format="nope")  # type: ignore[call-overload]
    assert fake_files.read_calls == 0
    assert sandbox.files.read(f"{HOME}/a.txt") == "a"
    assert fake_files.read_calls == 1


def test_list_depth_semantics_order_and_errors(
    sandbox: Sandbox, fake_files: FakeFilesystemService
) -> None:
    sandbox.files.write_files([WriteEntry(f"{HOME}/many/f{i:02}.txt", b"x") for i in range(3)])
    fake_files.add_file(f"{HOME}/big.bin", b"b")
    fake_files.add_file(f"{HOME}/hola.txt", b"h")
    fake_files.add_symlink(f"{HOME}/linkdir", "many")
    depth_two = sandbox.files.list(HOME, depth=2)
    assert [entry.name for entry in depth_two] == [
        "big.bin",
        "hola.txt",
        "linkdir",
        "many",
        "f00.txt",
        "f01.txt",
        "f02.txt",
    ]
    assert [entry.path for entry in depth_two[4:]] == [f"{HOME}/many/f0{i}.txt" for i in range(3)]
    assert depth_two[3].type is FileType.DIR
    assert depth_two[2].type is FileType.SYMLINK
    assert depth_two[2].symlink_target == "many"
    depth_one = sandbox.files.list(HOME)
    assert [entry.name for entry in depth_one] == ["big.bin", "hola.txt", "linkdir", "many"]
    assert sandbox.files.list(HOME, depth=0) == depth_one
    assert "etc" in [entry.name for entry in sandbox.files.list("/")]
    with pytest.raises(InvalidArgumentException):
        sandbox.files.list(f"{HOME}/big.bin")
    with pytest.raises(FileNotFoundException):
        sandbox.files.list(f"{HOME}/nope")
    with pytest.raises(InvalidArgumentException):
        sandbox.files.list(HOME, depth=-1)
    fake_files.max_list_entries = 2
    with pytest.raises(RateLimitException):
        sandbox.files.list(HOME)


def test_exists_get_info_and_symlink_target(
    sandbox: Sandbox, fake_files: FakeFilesystemService
) -> None:
    fake_files.add_file(f"{HOME}/a.txt", b"abc")
    fake_files.add_symlink(f"{HOME}/link", "a.txt")
    assert sandbox.files.exists(f"{HOME}/missing") is False
    assert sandbox.files.exists(f"{HOME}/a.txt") is True
    with pytest.raises(FileNotFoundException):
        sandbox.files.get_info(f"{HOME}/missing")
    link = sandbox.files.get_info(f"{HOME}/link")
    assert link.type is FileType.SYMLINK
    assert link.symlink_target == "a.txt"
    assert link.permissions == "lrwxrwxrwx"
    assert sandbox.files.get_info(f"{HOME}/a.txt").size == 3
    home = sandbox.files.get_info(HOME)
    assert (home.name, home.type, home.permissions) == ("user", FileType.DIR, "drwxr-xr-x")
    assert sandbox.files.get_info("/").name == "/"


def test_make_dir_true_then_false_and_invalid_targets(
    sandbox: Sandbox, fake_files: FakeFilesystemService
) -> None:
    assert sandbox.files.make_dir(f"{HOME}/dir") is True
    assert sandbox.files.make_dir(f"{HOME}/dir") is False
    fake_files.add_file(f"{HOME}/file", b"")
    with pytest.raises(InvalidArgumentException):
        sandbox.files.make_dir(f"{HOME}/file")
    with pytest.raises(InvalidArgumentException):
        sandbox.files.make_dir(f"{HOME}/file/sub")
    assert sandbox.files.make_dir(f"{HOME}/a/b/c") is True
    assert sandbox.files.get_info(f"{HOME}/a/b").type is FileType.DIR


def test_rename_moves_replaces_and_reports_conflicts(
    sandbox: Sandbox, fake_files: FakeFilesystemService
) -> None:
    fake_files.add_file(f"{HOME}/hola.txt", b"hola")
    fake_files.add_file(f"{HOME}/many/x", b"x")
    sandbox.files.make_dir(f"{HOME}/dir")
    moved = sandbox.files.rename(f"{HOME}/hola.txt", f"{HOME}/dir/hola.txt")
    assert (moved.path, moved.name) == (f"{HOME}/dir/hola.txt", "hola.txt")
    assert sandbox.files.exists(f"{HOME}/hola.txt") is False
    assert sandbox.files.read(f"{HOME}/dir/hola.txt") == "hola"
    with pytest.raises(FileNotFoundException):
        sandbox.files.rename(f"{HOME}/missing", f"{HOME}/x")
    with pytest.raises(InvalidArgumentException):
        sandbox.files.rename(f"{HOME}/dir/hola.txt", f"{HOME}/many")
    with pytest.raises(FileNotFoundException):
        sandbox.files.rename(f"{HOME}/dir/hola.txt", f"{HOME}/nodir/x")
    fake_files.add_file(f"{HOME}/old", b"old")
    sandbox.files.rename(f"{HOME}/dir/hola.txt", f"{HOME}/old")
    assert sandbox.files.read(f"{HOME}/old") == "hola"
    renamed_dir = sandbox.files.rename(f"{HOME}/many", f"{HOME}/moved")
    assert renamed_dir.type is FileType.DIR
    assert sandbox.files.read(f"{HOME}/moved/x") == "x"


def test_remove_recursive_default_flag_and_symlinks(
    sandbox: Sandbox, fake_files: FakeFilesystemService
) -> None:
    sandbox.files.write_files([WriteEntry(f"{HOME}/many/f{i}", b"x") for i in range(2)])
    fake_files.add_file(f"{HOME}/big.bin", b"b")
    fake_files.add_symlink(f"{HOME}/link", "big.bin")
    sandbox.files.remove(f"{HOME}/many/f0")
    assert sandbox.files.exists(f"{HOME}/many/f0") is False
    with pytest.raises(InvalidArgumentException):
        sandbox.files.remove(f"{HOME}/many", recursive=False)
    assert sandbox.files.exists(f"{HOME}/many") is True
    sandbox.files.remove(f"{HOME}/many")
    assert sandbox.files.exists(f"{HOME}/many") is False
    with pytest.raises(FileNotFoundException):
        sandbox.files.remove(f"{HOME}/many")
    sandbox.files.remove(f"{HOME}/link")
    assert sandbox.files.exists(f"{HOME}/link") is False
    assert sandbox.files.exists(f"{HOME}/big.bin") is True
    sandbox.files.make_dir(f"{HOME}/empty")
    sandbox.files.remove(f"{HOME}/empty", recursive=False)
    assert sandbox.files.exists(f"{HOME}/empty") is False


def test_path_policy_and_identity_are_enforced_by_the_agent(
    sandbox: Sandbox, fake_files: FakeFilesystemService
) -> None:
    with pytest.raises(AuthenticationException) as excinfo:
        sandbox.files.read("/etc/passwd")
    assert excinfo.value.proxy_rejected is False
    with pytest.raises(AuthenticationException):
        sandbox.files.write("/usr/local/bin/rayd", b"x")
    with pytest.raises(AuthenticationException):
        sandbox.files.list("/etc")
    with pytest.raises(AuthenticationException):
        sandbox.files.get_info("/etc/passwd")
    with pytest.raises(AuthenticationException):
        sandbox.files.make_dir("/etc/x")
    with pytest.raises(AuthenticationException):
        sandbox.files.remove("/etc/passwd")
    with pytest.raises(AuthenticationException):
        sandbox.files.rename("/etc/passwd", f"{HOME}/p")
    with pytest.raises(AuthenticationException):
        sandbox.files.watch_dir("/etc")
    fake_files.add_symlink(f"{HOME}/link", "/etc")
    with pytest.raises(AuthenticationException):
        sandbox.files.read(f"{HOME}/link/passwd")
    with pytest.raises(InvalidArgumentException):
        sandbox.files.read(f"{HOME}/../../etc/passwd")
    assert fake_files.read_calls == 0
    sandbox.files.write("m3/rel.txt", "r")
    assert sandbox.files.exists(f"{HOME}/m3/rel.txt") is True
    with pytest.raises(AuthenticationException) as root:
        sandbox.files.write(f"{HOME}/x", b"x", user="root")
    assert root.value.proxy_rejected is False
    with pytest.raises(InvalidArgumentException):
        sandbox.files.get_info(HOME, user="nobody")


def test_client_side_validation_never_reaches_the_agent(
    sandbox: Sandbox, fake_files: FakeFilesystemService
) -> None:
    calls_before = {rpc: len(seen) for rpc, seen in fake_files.deadlines.items()}
    with pytest.raises(InvalidArgumentException):
        sandbox.files.get_info("")
    with pytest.raises(InvalidArgumentException):
        sandbox.files.read("a\0b")
    with pytest.raises(InvalidArgumentException):
        sandbox.files.write(f"{HOME}/x", 123)  # type: ignore[arg-type]
    with pytest.raises(InvalidArgumentException):
        sandbox.files.write(f"{HOME}/x", b"", mode=0o10000)
    with pytest.raises(InvalidArgumentException):
        sandbox.files.write_files([("a", b"b")])  # type: ignore[list-item]
    with pytest.raises(InvalidArgumentException):
        sandbox.files.list(HOME, depth=True)
    with pytest.raises(InvalidArgumentException):
        sandbox.files.watch_dir(HOME, timeout=-1)
    with pytest.raises(InvalidArgumentException):
        sandbox.files.rename(f"{HOME}/a", "")
    assert {rpc: len(seen) for rpc, seen in fake_files.deadlines.items()} == calls_before
    assert fake_files.write_streams == []


def test_watch_dir_blocks_until_started_and_collects_events(
    sandbox: Sandbox, fake_files: FakeFilesystemService
) -> None:
    sandbox.files.make_dir(WATCH)
    handle = sandbox.files.watch_dir(WATCH)
    assert handle.is_running is True
    assert handle.path == WATCH
    assert handle.get_new_events() == []
    request = fake_files.watch_requests[-1]
    assert (request.path, request.recursive, request.include_entry) == (WATCH, False, False)
    fake_files.push_event(WATCH, "a.txt", CREATE)
    fake_files.push_keepalive(WATCH)
    fake_files.push_event(WATCH, "a.txt", WRITE)
    fake_files.push_event(WATCH, "a.txt", REMOVE)
    events = collect_events(handle, 3)
    assert [(event.name, event.type) for event in events] == [
        ("a.txt", FilesystemEventType.CREATE),
        ("a.txt", FilesystemEventType.WRITE),
        ("a.txt", FilesystemEventType.REMOVE),
    ]
    assert events[0].entry is None
    handle.stop()
    handle.stop()
    assert running(handle) is False
    assert handle.get_new_events() == []
    wait_until(lambda: fake_files.live_watches == 0)


def test_watch_dir_callback_include_entry_and_recursive(
    sandbox: Sandbox, fake_files: FakeFilesystemService
) -> None:
    seen: list[FilesystemEvent] = []
    with sandbox.files.watch_dir(
        HOME, on_event=seen.append, recursive=True, include_entry=True
    ) as handle:
        request = fake_files.watch_requests[-1]
        assert (request.recursive, request.include_entry) == (True, True)
        entry = common_pb2.EntryInfo(
            name="w.txt", type=common_pb2.FILE_TYPE_FILE, mode=0o600, permissions="-rw-------"
        )
        fake_files.push_event(HOME, "sub/w.txt", CHMOD, entry)
        wait_until(lambda: len(seen) == 1)
        assert seen[0].name == "sub/w.txt"
        assert seen[0].type is FilesystemEventType.CHMOD
        assert seen[0].entry is not None
        assert seen[0].entry.mode == 0o600
        assert handle.get_new_events() == []
    assert handle.is_running is False


def test_watch_callback_exceptions_do_not_stop_the_watch(
    sandbox: Sandbox, fake_files: FakeFilesystemService
) -> None:
    seen: list[FilesystemEvent] = []

    def on_event(event: FilesystemEvent) -> None:
        seen.append(event)
        raise RuntimeError("boom")

    handle = sandbox.files.watch_dir(HOME, on_event=on_event)
    fake_files.push_event(HOME, "a", CREATE)
    fake_files.push_event(HOME, "b", CREATE)
    wait_until(lambda: len(seen) == 2)
    assert handle.is_running is True
    handle.stop()


def test_watch_dir_terminal_error_surfaces_once_and_calls_on_exit(
    sandbox: Sandbox, fake_files: FakeFilesystemService
) -> None:
    exits: list[Exception] = []
    handle = sandbox.files.watch_dir(HOME, on_exit=exits.append)
    fake_files.push_event(HOME, "x", CREATE)
    fake_files.end_watch(HOME, grpc.StatusCode.NOT_FOUND, "watched directory removed")
    wait_until(lambda: not handle.is_running)
    assert len(exits) == 1
    assert isinstance(exits[0], FileNotFoundException)
    events = handle.get_new_events()
    assert [(event.name, event.type) for event in events] == [("x", FilesystemEventType.CREATE)]
    with pytest.raises(FileNotFoundException):
        handle.get_new_events()
    assert handle.get_new_events() == []
    handle.stop()
    assert fake_files.live_watches == 0


def test_watch_dir_unknown_event_type_ends_the_watch_and_releases_the_stream(
    sandbox: Sandbox, fake_files: FakeFilesystemService
) -> None:
    exits: list[Exception] = []
    handle = sandbox.files.watch_dir(HOME, on_exit=exits.append)
    fake_files.push_event(HOME, "x", filesystem_pb2.FILESYSTEM_EVENT_TYPE_UNSPECIFIED)
    wait_until(lambda: not handle.is_running)
    wait_until(lambda: fake_files.live_watches == 0)
    assert len(exits) == 1
    assert isinstance(exits[0], SandboxException)
    with pytest.raises(SandboxException, match="desconocido"):
        handle.get_new_events()
    assert handle.get_new_events() == []
    assert sandbox.files._watches == set()


def test_watch_dir_timeout_is_a_timeout_exception(sandbox: Sandbox) -> None:
    handle = sandbox.files.watch_dir(HOME, timeout=0.5)
    wait_until(lambda: not handle.is_running)
    with pytest.raises(TimeoutException):
        handle.get_new_events()
    assert handle.is_running is False


def test_watch_dir_errors_before_started_never_create_a_handle(
    sandbox: Sandbox, fake_files: FakeFilesystemService
) -> None:
    with pytest.raises(FileNotFoundException):
        sandbox.files.watch_dir(f"{HOME}/nope")
    fake_files.add_file(f"{HOME}/file", b"")
    with pytest.raises(InvalidArgumentException):
        sandbox.files.watch_dir(f"{HOME}/file")
    fake_files.watch_first_message = "keepalive"
    with pytest.raises(SandboxException, match="WatchStarted"):
        sandbox.files.watch_dir(HOME)
    wait_until(lambda: fake_files.live_watches == 0)
    assert sandbox.files._watches == set()


def test_watch_dir_uses_the_stream_channel_and_the_rest_the_unary_one(
    sandbox: Sandbox, fake_files: FakeFilesystemService
) -> None:
    fake_files.add_file(f"{HOME}/a", b"a")
    sandbox.files.get_info(HOME)
    sandbox.files.read(f"{HOME}/a")
    sandbox.files.write(f"{HOME}/b", b"b")
    sandbox.files.list(HOME)
    assert stream_channel_of(sandbox) is None
    handle = sandbox.files.watch_dir(HOME)
    assert stream_channel_of(sandbox) is not None
    assert len(unary_peers(fake_files)) == 1
    assert fake_files.peers["WatchDir"].isdisjoint(unary_peers(fake_files))
    second = sandbox.files.watch_dir("/tmp")
    assert len(fake_files.peers["WatchDir"]) == 1
    handle.stop()
    second.stop()


def test_close_stops_live_watches(sandbox: Sandbox, fake_files: FakeFilesystemService) -> None:
    first = sandbox.files.watch_dir(HOME)
    second = sandbox.files.watch_dir("/tmp")
    assert fake_files.live_watches == 2
    sandbox.close()
    assert first.is_running is False
    assert second.is_running is False
    assert first.get_new_events() == []
    assert second.get_new_events() == []
    wait_until(lambda: fake_files.live_watches == 0)


def test_proxy_403_on_write_remints_once_and_resends_the_whole_file(
    sandbox: Sandbox,
    fake_files: FakeFilesystemService,
    control_plane: StubbedControlPlane,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_remint(control_plane, "jwe-2")
    data = os.urandom(2 * WRITE_CHUNK_BYTES + 5)
    real = sandbox._files.Write
    failures = [proxy_forbidden()]

    def write(request_iterator: Iterator[Any], timeout: float | None = None) -> Any:
        if failures:
            next(request_iterator)
            raise failures.pop(0)
        return real(request_iterator, timeout=timeout)

    monkeypatch.setattr(sandbox._files, "Write", write)
    info = sandbox.files.write(f"{HOME}/big.bin", data)
    assert info.size == len(data)
    assert len(fake_files.write_streams) == 1
    assert fake_files.file_bytes(f"{HOME}/big.bin") == data
    assert fake_files.metadata["Write"][-1][PROXY_AUTH_KEY] == "jwe-2"
    control_plane.microvms.assert_no_pending_responses()


def test_proxy_403_on_read_and_watch_open_remints_once(
    sandbox: Sandbox,
    fake_files: FakeFilesystemService,
    control_plane: StubbedControlPlane,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_files.add_file(f"{HOME}/a", b"abc")
    real_read = sandbox._files.Read
    failures = [proxy_forbidden()]

    def read(request: Any, timeout: float | None = None) -> Any:
        if failures:
            return failing_stream(failures.pop(0))
        return real_read(request, timeout=timeout)

    monkeypatch.setattr(sandbox._files, "Read", read)
    stub_remint(control_plane, "jwe-2")
    assert sandbox.files.read(f"{HOME}/a", format="bytes") == b"abc"
    assert fake_files.metadata["Read"][-1][PROXY_AUTH_KEY] == "jwe-2"

    stream_stub = sandbox._stub(filesystem_pb2_grpc.FilesystemServiceStub, stream=True)
    real_watch = stream_stub.WatchDir
    watch_failures = [proxy_forbidden()]

    def watch(request: Any, timeout: float | None = None) -> Any:
        if watch_failures:
            return failing_stream(watch_failures.pop(0))
        return real_watch(request, timeout=timeout)

    monkeypatch.setattr(stream_stub, "WatchDir", watch)
    stub_remint(control_plane, "jwe-3")
    handle = sandbox.files.watch_dir(HOME)
    assert fake_files.metadata["WatchDir"][-1][PROXY_AUTH_KEY] == "jwe-3"
    handle.stop()
    control_plane.microvms.assert_no_pending_responses()


def test_stream_reset_mid_read_is_classified_by_probing_health(
    sandbox: Sandbox,
    fake_rayd: RaydEndpoint,
    fake_files: FakeFilesystemService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset = FakeRpcError(grpc.StatusCode.UNAVAILABLE, details="Socket closed")
    monkeypatch.setattr(
        sandbox._files,
        "Read",
        lambda request, timeout=None: ScriptedStream(
            [filesystem_pb2.ReadResponse(chunk=b"x")], reset
        ),
    )
    fake_files.add_file(f"{HOME}/a", b"xy")
    health_calls = len(fake_rayd.servicer.health_calls)
    with pytest.raises(SandboxException, match="stream cortado"):
        sandbox.files.read(f"{HOME}/a", format="bytes")
    assert len(fake_rayd.servicer.health_calls) == health_calls + 1


def test_watch_stream_reset_with_a_live_agent_reissues_then_gives_up(
    sandbox: Sandbox, fake_rayd: RaydEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Desde M5 un reset con el agente vivo reabre el `WatchDir`; si el
    stream se corta una y otra vez sin un resume de por medio, a la cuarta
    el watch termina con la clasificación de M2."""
    always_running(monkeypatch, sandbox, fake_rayd.host)
    reset = FakeRpcError(grpc.StatusCode.UNAVAILABLE, details="Socket closed")
    stream_stub = sandbox._stub(filesystem_pb2_grpc.FilesystemServiceStub, stream=True)
    monkeypatch.setattr(
        stream_stub,
        "WatchDir",
        lambda request, timeout=None: ScriptedStream(
            [filesystem_pb2.WatchDirResponse(started=filesystem_pb2.WatchStarted())], reset
        ),
    )
    exits: list[Exception] = []
    handle = sandbox.files.watch_dir(HOME, on_exit=exits.append)
    wait_until(lambda: not handle.is_running)
    assert handle.reconnects == 3
    assert len(exits) == 1
    assert "stream cortado" in str(exits[0])
    with pytest.raises(SandboxException, match="stream cortado"):
        handle.get_new_events()


def test_files_with_a_wrong_token_are_unauthenticated(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    control_plane.microvms.add_response(
        "get_microvm", microvm_response(endpoint=fake_rayd.host, state="RUNNING")
    )
    control_plane.microvms.add_response("create_microvm_auth_token", auth_token_response())
    other = Sandbox.connect(
        SANDBOX_ID,
        access_token=generate_access_token(),
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    )
    try:
        with pytest.raises(AuthenticationException) as excinfo:
            other.files.get_info(HOME)
        assert excinfo.value.grpc_code is grpc.StatusCode.UNAUTHENTICATED
        with pytest.raises(AuthenticationException):
            other.files.write(f"{HOME}/x", b"x")
    finally:
        other.close()


def test_an_older_agent_refuses_metadata_and_gzip_writes_before_any_byte(
    sandbox: Sandbox, fake_files: FakeFilesystemService
) -> None:
    """El `rayd` falso base no tiene transferencias (como un agente anterior
    a M9): la sonda `GetTransfer("")` responde `UNIMPLEMENTED`, así que los
    metadatos (que ignoraría en silencio) y el `Write` comprimido se
    rechazan antes de abrir el stream; un `read(gzip=True)` sigue valiendo."""
    with pytest.raises(UnimplementedError, match="actualiza la imagen"):
        sandbox.files.write(f"{HOME}/m.txt", "x", metadata={"owner": "alice"})
    with pytest.raises(UnimplementedError, match="actualiza la imagen"):
        sandbox.files.write(f"{HOME}/z.txt", "x", gzip=True)
    assert fake_files.write_streams == []
    fake_files.add_file(f"{HOME}/plain.txt", b"hola")
    assert sandbox.files.read(f"{HOME}/plain.txt", gzip=True, stream_idle_timeout=5) == "hola"
    assert dict(sandbox.files.get_info(f"{HOME}/plain.txt").metadata) == {}
    entry = sandbox.files.write(f"{HOME}/octet.txt", "x", use_octet_stream=True)
    assert entry.size == 1
