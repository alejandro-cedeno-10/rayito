"""Helpers puros de `files`: deadlines, `coerce_data`, troceado de `Write`,
constructores de requests, conversión de protos, `decode_text`, `WatchState`
y `watch_failure`."""

from __future__ import annotations

import io
from datetime import UTC, datetime

import grpc
import pytest

from rayito._filesystem_base import (
    WRITE_CHUNK_BYTES,
    WatchState,
    build_write_requests,
    coerce_data,
    decode_text,
    entry_info_from_proto,
    file_request_deadline,
    file_type_from_proto,
    filesystem_event_from_proto,
    list_dir_request,
    move_request,
    notify_exit,
    prepare_write_entries,
    remove_request,
    require_regular_file,
    require_watch_started,
    stat_request,
    total_write_bytes,
    validate_depth,
    validate_mode,
    validate_path,
    validate_read_format,
    validate_watch_timeout,
    watch_dir_request,
    watch_failure,
)
from rayito._models import EntryInfo, FilesystemEvent, FilesystemEventType, FileType, WriteEntry
from rayito.exceptions import (
    FileNotFoundException,
    InvalidArgumentException,
    RateLimitException,
    SandboxException,
    TimeoutException,
)
from rayito.v1 import common_pb2, filesystem_pb2

from .conftest import FakeRpcError

MODIFIED_UNIX_MS = 1_789_000_000_123


def file_entry(name: str = "a", **overrides: object) -> EntryInfo:
    fields: dict[str, object] = {
        "name": name,
        "type": FileType.FILE,
        "path": f"/home/user/{name}",
        "size": 3,
        "mode": 0o644,
        "permissions": "-rw-r--r--",
        "owner": "user",
        "group": "user",
        "modified_time": datetime.fromtimestamp(MODIFIED_UNIX_MS / 1000, tz=UTC),
    }
    fields.update(overrides)
    return EntryInfo(**fields)  # type: ignore[arg-type]


def create_event(name: str) -> filesystem_pb2.WatchDirResponse:
    return filesystem_pb2.WatchDirResponse(
        filesystem=filesystem_pb2.FilesystemEvent(
            name=name, type=filesystem_pb2.FILESYSTEM_EVENT_TYPE_CREATE
        )
    )


def test_deadline_is_sixty_seconds_plus_one_per_megabyte() -> None:
    assert file_request_deadline(8_000_000, None) == 68.0
    assert file_request_deadline(0, None) == 60.0
    assert file_request_deadline(500_000, None) == 60.5
    assert file_request_deadline(8_000_000, 5) == 5


def test_watch_timeout_zero_and_none_mean_no_deadline() -> None:
    assert validate_watch_timeout(0) is None
    assert validate_watch_timeout(None) is None
    assert validate_watch_timeout(2) == 2.0
    with pytest.raises(InvalidArgumentException):
        validate_watch_timeout(-1)
    with pytest.raises(InvalidArgumentException):
        validate_watch_timeout("1")  # type: ignore[arg-type]


def test_coerce_data_accepts_str_bytes_and_file_objects() -> None:
    assert coerce_data("hola ñ") == "hola ñ".encode()
    assert coerce_data(b"x") == b"x"
    assert coerce_data(bytearray(b"y")) == b"y"
    assert coerce_data(memoryview(b"z")) == b"z"
    assert coerce_data(io.BytesIO(b"bin")) == b"bin"
    assert coerce_data(io.StringIO("txt ñ")) == "txt ñ".encode()
    with pytest.raises(InvalidArgumentException, match="data"):
        coerce_data(123)
    with pytest.raises(InvalidArgumentException):
        coerce_data(None)


def test_argument_validators() -> None:
    assert validate_path("/a") == "/a"
    assert validate_path("rel/x") == "rel/x"
    for bad_path in ("", "a\0b", 1, None):
        with pytest.raises(InvalidArgumentException):
            validate_path(bad_path)
    assert validate_mode(None) is None
    assert validate_mode(0o600) == 0o600
    assert validate_mode(0o7777) == 0o7777
    for bad_mode in (0o10000, -1, True, "644"):
        with pytest.raises(InvalidArgumentException):
            validate_mode(bad_mode)
    assert validate_depth(0) == 0
    assert validate_depth(3) == 3
    for bad_depth in (-1, True, 1.5):
        with pytest.raises(InvalidArgumentException):
            validate_depth(bad_depth)
    assert validate_read_format("stream") == "stream"
    with pytest.raises(InvalidArgumentException, match="format"):
        validate_read_format("nope")


def test_prepare_write_entries_materialises_data_and_rejects_bad_input() -> None:
    with pytest.raises(InvalidArgumentException):
        prepare_write_entries([])
    with pytest.raises(InvalidArgumentException):
        prepare_write_entries([("a", b"b")])  # type: ignore[list-item]
    prepared = prepare_write_entries(
        [WriteEntry("/a", "x"), WriteEntry("b", io.BytesIO(b"yy"), mode=0o600)]
    )
    assert prepared == [("/a", b"x", None), ("b", b"yy", 0o600)]
    assert total_write_bytes(prepared) == 3


def test_write_requests_chunk_at_one_mib_with_path_only_on_the_first() -> None:
    data = bytes(3 * WRITE_CHUNK_BYTES + 1)
    requests = list(build_write_requests([("/big", data, None)], None))
    assert [(request.HasField("path"), len(request.chunk)) for request in requests] == [
        (True, WRITE_CHUNK_BYTES),
        (False, WRITE_CHUNK_BYTES),
        (False, WRITE_CHUNK_BYTES),
        (False, 1),
    ]
    assert requests[0].path == "/big"
    assert not requests[0].HasField("user")
    assert not requests[0].HasField("mode")
    assert b"".join(request.chunk for request in requests) == data


def test_write_requests_per_entry_headers_and_empty_data() -> None:
    requests = list(build_write_requests([("/a", b"", 0o600), ("/b", b"xy", None)], "user"))
    assert [(request.path, len(request.chunk)) for request in requests] == [("/a", 0), ("/b", 2)]
    assert requests[0].mode == 0o600
    assert requests[0].user.username == "user"
    assert not requests[1].HasField("mode")
    assert requests[1].user.username == "user"


def test_request_builders_carry_user_only_when_given() -> None:
    assert not stat_request("/x", None).HasField("user")
    assert not stat_request("/x", "").HasField("user")
    assert stat_request("/x", "root").user.username == "root"
    listing = list_dir_request("/x", 2, None)
    assert (listing.path, listing.depth) == ("/x", 2)
    assert remove_request("/x", False, None).recursive is False
    watch = watch_dir_request("/x", True, True, None)
    assert (watch.recursive, watch.include_entry) == (True, True)
    move = move_request("/a", "/b", None)
    assert (move.source, move.destination) == ("/a", "/b")
    with pytest.raises(InvalidArgumentException, match="new_path"):
        move_request("/a", "", None)


def test_entry_info_from_proto_maps_types_and_symlink_target() -> None:
    proto = common_pb2.EntryInfo(
        name="a",
        type=common_pb2.FILE_TYPE_FILE,
        path="/home/user/a",
        size=3,
        mode=0o644,
        permissions="-rw-r--r--",
        owner="user",
        group="user",
        modified_time_unix_ms=MODIFIED_UNIX_MS,
    )
    assert entry_info_from_proto(proto) == file_entry("a")
    link = common_pb2.EntryInfo(name="l", type=common_pb2.FILE_TYPE_SYMLINK, symlink_target="a")
    converted = entry_info_from_proto(link)
    assert converted.type is FileType.SYMLINK
    assert converted.symlink_target == "a"
    assert file_type_from_proto(common_pb2.FILE_TYPE_DIRECTORY) is FileType.DIR
    assert file_type_from_proto(common_pb2.FILE_TYPE_UNSPECIFIED) is None
    assert entry_info_from_proto(common_pb2.EntryInfo()).type is None


def test_entry_info_from_proto_clamps_out_of_range_modified_time() -> None:
    far_future = common_pb2.EntryInfo(name="a", modified_time_unix_ms=300_000_000_000_000)
    assert entry_info_from_proto(far_future).modified_time == datetime.max.replace(tzinfo=UTC)
    far_past = common_pb2.EntryInfo(name="a", modified_time_unix_ms=-300_000_000_000_000)
    assert entry_info_from_proto(far_past).modified_time == datetime.min.replace(tzinfo=UTC)
    before_epoch = common_pb2.EntryInfo(name="a", modified_time_unix_ms=-1_500)
    assert entry_info_from_proto(before_epoch).modified_time == datetime(
        1969, 12, 31, 23, 59, 58, 500_000, tzinfo=UTC
    )


def test_filesystem_event_from_proto() -> None:
    kinds = {
        filesystem_pb2.FILESYSTEM_EVENT_TYPE_CREATE: FilesystemEventType.CREATE,
        filesystem_pb2.FILESYSTEM_EVENT_TYPE_WRITE: FilesystemEventType.WRITE,
        filesystem_pb2.FILESYSTEM_EVENT_TYPE_REMOVE: FilesystemEventType.REMOVE,
        filesystem_pb2.FILESYSTEM_EVENT_TYPE_RENAME: FilesystemEventType.RENAME,
        filesystem_pb2.FILESYSTEM_EVENT_TYPE_CHMOD: FilesystemEventType.CHMOD,
    }
    for proto_type, expected in kinds.items():
        event = filesystem_event_from_proto(
            filesystem_pb2.FilesystemEvent(name="a.txt", type=proto_type)
        )
        assert event == FilesystemEvent(name="a.txt", type=expected, entry=None)
    with_entry = filesystem_pb2.FilesystemEvent(
        name="sub/b",
        type=filesystem_pb2.FILESYSTEM_EVENT_TYPE_CHMOD,
        entry=common_pb2.EntryInfo(name="b", type=common_pb2.FILE_TYPE_FILE, mode=0o600),
    )
    converted = filesystem_event_from_proto(with_entry)
    assert converted.entry is not None
    assert converted.entry.mode == 0o600
    with pytest.raises(SandboxException):
        filesystem_event_from_proto(filesystem_pb2.FilesystemEvent(name="x"))


def test_decode_text_is_strict() -> None:
    assert decode_text("ñ".encode()) == "ñ"
    with pytest.raises(InvalidArgumentException, match="bytes"):
        decode_text(b"\xff\xfe")


def test_require_regular_file_refuses_everything_else() -> None:
    entry = file_entry()
    assert require_regular_file(entry) is entry
    with pytest.raises(InvalidArgumentException, match="directorio"):
        require_regular_file(file_entry(type=FileType.DIR))
    with pytest.raises(InvalidArgumentException, match="simb"):
        require_regular_file(file_entry(type=FileType.SYMLINK, symlink_target="a"))
    with pytest.raises(InvalidArgumentException, match="regular"):
        require_regular_file(file_entry(type=None))


def test_require_watch_started() -> None:
    require_watch_started(filesystem_pb2.WatchDirResponse(started=filesystem_pb2.WatchStarted()))
    with pytest.raises(SandboxException, match="WatchStarted"):
        require_watch_started(filesystem_pb2.WatchDirResponse(keepalive=common_pb2.KeepAlive()))
    with pytest.raises(SandboxException, match="WatchStarted"):
        require_watch_started(None)


def test_watch_state_queues_without_callback_and_ignores_keepalives() -> None:
    state = WatchState(None)
    assert state.is_running
    keepalive = filesystem_pb2.WatchDirResponse(keepalive=common_pb2.KeepAlive())
    assert state.feed(keepalive) is None
    event = state.feed(create_event("a"))
    assert event == FilesystemEvent(name="a", type=FilesystemEventType.CREATE)
    assert state.drain() == [event]
    assert state.drain() == []
    with pytest.raises(SandboxException):
        state.feed(filesystem_pb2.WatchDirResponse(started=filesystem_pb2.WatchStarted()))


def test_watch_state_delivers_to_the_callback_and_survives_its_errors() -> None:
    seen: list[FilesystemEvent] = []

    def on_event(event: FilesystemEvent) -> None:
        seen.append(event)
        raise RuntimeError("boom")

    state = WatchState(on_event)
    state.feed(create_event("a"))
    state.feed(create_event("b"))
    assert [event.name for event in seen] == ["a", "b"]
    assert state.drain() == []


def test_watch_state_raises_the_terminal_error_once_after_pending_events() -> None:
    state = WatchState(None)
    event = state.feed(create_event("a"))
    state.record_end(FileNotFoundException("gone"))
    assert state.ended
    assert not state.is_running
    assert state.drain() == [event]
    with pytest.raises(FileNotFoundException):
        state.drain()
    assert state.drain() == []
    clean = WatchState(None)
    clean.stopped = True
    clean.record_end(None)
    assert clean.drain() == []
    assert not clean.is_running


def test_watch_failure_table() -> None:
    cancelled = FakeRpcError(grpc.StatusCode.CANCELLED, details="Locally cancelled")
    assert watch_failure(cancelled, stopped=True) is None
    assert isinstance(watch_failure(cancelled, stopped=False), SandboxException)
    deadline = FakeRpcError(grpc.StatusCode.DEADLINE_EXCEEDED, details="Deadline Exceeded")
    assert isinstance(watch_failure(deadline, stopped=False), TimeoutException)
    assert isinstance(watch_failure(deadline, stopped=True), TimeoutException)
    gone = FakeRpcError(grpc.StatusCode.NOT_FOUND, details="watched directory removed")
    assert isinstance(watch_failure(gone, stopped=False), FileNotFoundException)
    overflow = FakeRpcError(grpc.StatusCode.RESOURCE_EXHAUSTED, details="watch queue overflowed")
    assert isinstance(watch_failure(overflow, stopped=False), RateLimitException)


def test_notify_exit_only_fires_on_failures_and_swallows_callback_errors() -> None:
    calls: list[Exception] = []
    notify_exit(calls.append, None)
    assert calls == []

    def bad(exc: Exception) -> None:
        calls.append(exc)
        raise RuntimeError("boom")

    notify_exit(bad, ValueError("x"))
    assert len(calls) == 1
    notify_exit(None, ValueError("x"))
