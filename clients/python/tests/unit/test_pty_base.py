"""Helpers puros de `pty`: `PtySize`, forma del `PtyStart`, requests unarios y
el adaptador `PtyMessages` sobre `CommandProgress`."""

from __future__ import annotations

import pytest

from rayito import PtySize
from rayito._process_base import (
    Chunk,
    CommandProgress,
    Ended,
    Nothing,
    OutputAccumulator,
    Suspending,
)
from rayito._pty_base import (
    DEFAULT_PTY_TIMEOUT_SECONDS,
    PtyMessages,
    build_connect_request,
    build_kill_request,
    build_pty_start_request,
    build_resize_request,
    build_send_input_request,
    end_event_from_pty_exited,
    pid_from_pty_started,
    validate_pty_size,
    validate_shell,
)
from rayito.exceptions import (
    CommandExitException,
    InvalidArgumentException,
    SandboxException,
    SandboxStateException,
    TimeoutException,
)
from rayito.v1 import common_pb2, pty_pb2


def test_pty_size_defaults_and_bounds() -> None:
    assert PtySize() == PtySize(cols=80, rows=24)
    assert PtySize(cols=4096, rows=1).cols == 4096
    for cols, rows in ((0, 24), (4097, 24), (80, 0), (80, 5000), (-1, 1)):
        with pytest.raises(InvalidArgumentException):
            PtySize(cols=cols, rows=rows)
    with pytest.raises(InvalidArgumentException):
        PtySize(cols=True, rows=24)
    with pytest.raises(InvalidArgumentException):
        PtySize(cols="80", rows=24)  # type: ignore[arg-type]


def test_validate_pty_size_and_shell() -> None:
    assert validate_pty_size(None) is None
    assert validate_pty_size(PtySize(cols=1, rows=1)) == PtySize(cols=1, rows=1)
    with pytest.raises(InvalidArgumentException, match="PtySize"):
        validate_pty_size((80, 24))
    assert validate_shell(None) is None
    assert validate_shell("") is None
    assert validate_shell("/bin/zsh") == "/bin/zsh"
    for bad in ("bash", "bin/sh", "/bin/\x00sh", 7):
        with pytest.raises(InvalidArgumentException, match="shell"):
            validate_shell(bad)


def test_pty_start_request_defaults_omit_optional_fields() -> None:
    request = build_pty_start_request()
    assert not request.HasField("size")
    assert not request.HasField("user")
    assert not request.HasField("cwd")
    assert not request.HasField("shell")
    assert dict(request.envs) == {}
    assert request.timeout_ms == int(DEFAULT_PTY_TIMEOUT_SECONDS * 1000)


def test_pty_start_request_carries_every_option() -> None:
    request = build_pty_start_request(
        size=PtySize(cols=100, rows=30),
        user="user",
        cwd="/tmp",
        envs={"FOO": "bar"},
        shell="/bin/zsh",
        timeout=2.5,
    )
    assert (request.size.cols, request.size.rows) == (100, 30)
    assert request.user.username == "user"
    assert request.cwd == "/tmp"
    assert dict(request.envs) == {"FOO": "bar"}
    assert request.shell == "/bin/zsh"
    assert request.timeout_ms == 2500


def test_pty_start_request_timeout_none_and_empty_strings() -> None:
    request = build_pty_start_request(user="", cwd="", shell="", timeout=None)
    assert request.timeout_ms == 0
    assert not request.HasField("user")
    assert not request.HasField("cwd")
    assert not request.HasField("shell")
    assert build_pty_start_request(timeout=0).timeout_ms == 0
    with pytest.raises(InvalidArgumentException):
        build_pty_start_request(timeout=-1)
    with pytest.raises(InvalidArgumentException):
        build_pty_start_request(envs={"": "x"})


def test_unary_request_builders() -> None:
    connect = build_connect_request(7, 3)
    assert (connect.pid, connect.from_seq) == (7, 3)
    send = build_send_input_request(7, "hola\n")
    assert (send.pid, send.data) == (7, b"hola\n")
    assert build_send_input_request(7, b"\x00\xff").data == b"\x00\xff"
    resize = build_resize_request(7, PtySize(cols=120, rows=40))
    assert (resize.pid, resize.size.cols, resize.size.rows) == (7, 120, 40)
    assert build_kill_request(7).pid == 7
    for bad_pid in (0, -1, True):
        with pytest.raises(InvalidArgumentException):
            build_kill_request(bad_pid)
    with pytest.raises(InvalidArgumentException):
        build_resize_request(7, None)  # type: ignore[arg-type]


def test_first_message_must_be_started() -> None:
    started = pty_pb2.PtyServerMessage(started=pty_pb2.PtyStarted(pid=9))
    assert pid_from_pty_started(started) == 9
    with pytest.raises(SandboxException, match="started"):
        pid_from_pty_started(pty_pb2.PtyServerMessage(data=b"x", seq=1))


def test_exited_converts_to_end_event_with_optionals() -> None:
    exited = pty_pb2.PtyExited(
        exit_code=143,
        exited=False,
        status="timeout",
        signal=15,
        error=common_pb2.StreamError(code="deadline_exceeded", message="expired"),
    )
    end = end_event_from_pty_exited(exited)
    assert (end.exit_code, end.exited, end.status, end.signal) == (143, False, "timeout", 15)
    assert end.error.code == "deadline_exceeded"
    bare = end_event_from_pty_exited(pty_pb2.PtyExited(exit_code=0, exited=True, status="exited"))
    assert not bare.HasField("error")
    assert not bare.HasField("signal")


def test_pty_messages_adapter_drives_command_progress() -> None:
    chunks: list[bytes] = []
    adapter = PtyMessages(chunks.append)
    progress = CommandProgress(9, OutputAccumulator(), adapter)
    assert isinstance(
        progress.consume(pty_pb2.PtyServerMessage(keepalive=common_pb2.KeepAlive())), Nothing
    )
    first = progress.consume(pty_pb2.PtyServerMessage(data=b"hol\xc3", seq=1))
    assert first == Chunk(seq=1, stdout="hol", pty=b"hol\xc3")
    assert first.as_output() == (None, None, b"hol\xc3")
    second = progress.consume(pty_pb2.PtyServerMessage(data=b"\xa1\r\n", seq=2))
    assert second == Chunk(seq=2, stdout="á\r\n", pty=b"\xa1\r\n")
    assert chunks == [b"hol\xc3", b"\xa1\r\n"]
    assert progress.accumulator.last_seq == 2
    assert progress.accumulator.stdout == "holá\r\n"
    assert progress.accumulator.stderr == ""
    assert progress.is_finished() is False
    ended = progress.consume(
        pty_pb2.PtyServerMessage(
            exited=pty_pb2.PtyExited(exit_code=3, exited=True, status="exited")
        )
    )
    assert isinstance(ended, Ended)
    assert progress.is_finished() is True
    assert progress.exit_code == 3
    assert progress.error == "exited"
    with pytest.raises(CommandExitException) as excinfo:
        progress.resolve()
    assert excinfo.value.stdout == "holá\r\n"


def test_pty_messages_suspending_marks_progress_without_finishing() -> None:
    progress = CommandProgress(9, OutputAccumulator(), PtyMessages())
    consumed = progress.consume(
        pty_pb2.PtyServerMessage(
            exited=pty_pb2.PtyExited(
                exited=False,
                status="suspending",
                error=common_pb2.StreamError(code="suspending", message="bye"),
            )
        )
    )
    assert isinstance(consumed, Suspending)
    assert progress.suspended is True
    assert progress.is_finished() is False
    assert progress.end is None
    progress.resubscribed()
    assert progress.suspended is False


def test_pty_messages_timeout_status_is_timeout_exception() -> None:
    progress = CommandProgress(9, OutputAccumulator(), PtyMessages())
    progress.consume(
        pty_pb2.PtyServerMessage(
            exited=pty_pb2.PtyExited(
                exit_code=143,
                exited=False,
                status="timeout",
                signal=15,
                error=common_pb2.StreamError(code="deadline_exceeded", message="expired"),
            )
        )
    )
    with pytest.raises(TimeoutException):
        progress.resolve()
    assert progress.error == "deadline_exceeded"


def test_suspending_end_resolves_to_state_exception_when_nobody_reconnects() -> None:
    progress = CommandProgress(9, OutputAccumulator(), PtyMessages())
    consumed = progress.consume(
        pty_pb2.PtyServerMessage(exited=pty_pb2.PtyExited(exited=False, status="suspending"))
    )
    assert isinstance(consumed, Suspending)
    outcome = progress.accumulator.finish(consumed.end)
    assert isinstance(outcome, SandboxStateException)
