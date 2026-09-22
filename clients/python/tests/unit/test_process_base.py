"""Helpers puros de `commands`: forma del `StartRequest`, deadlines, decoder
incremental, tabla `EndEvent` → resultado y clasificación de fallos de stream."""

from __future__ import annotations

from datetime import UTC, datetime

import grpc
import pytest

from rayito._models import CommandResult, ProcessInfo
from rayito._process_base import (
    DEFAULT_COMMAND_TIMEOUT_SECONDS,
    Chunk,
    CommandProgress,
    Ended,
    Nothing,
    OutputAccumulator,
    ProcessEvents,
    Suspending,
    build_start_request,
    deadline_at,
    encode_stdin,
    end_error_name,
    metrics_from_proto,
    outcome_from_end,
    pid_from_start_event,
    process_info_from_proto,
    remaining_deadline,
    resubscribe_from_seq,
    stream_deadline,
    stream_failure_exception,
    suspending_reason,
    timeout_to_ms,
    validate_from_seq,
    validate_pid,
)
from rayito._transport import is_stream_reset
from rayito.exceptions import (
    AuthenticationException,
    CommandExitException,
    InvalidArgumentException,
    NotFoundException,
    SandboxException,
    SandboxNotFoundException,
    SandboxStateException,
    TimeoutException,
)
from rayito.v1 import common_pb2, health_pb2, process_pb2

from .conftest import FakeRpcError


def test_start_request_wraps_the_command_in_a_login_shell() -> None:
    request = build_start_request("echo hola")
    assert request.process.cmd == "/bin/bash"
    assert list(request.process.args) == ["-l", "-c", "echo hola"]
    assert request.timeout_ms == int(DEFAULT_COMMAND_TIMEOUT_SECONDS * 1000)
    assert request.stdin is False
    assert not request.HasField("user")
    assert not request.HasField("tag")
    assert not request.process.HasField("cwd")
    assert dict(request.process.envs) == {}


def test_start_request_carries_every_option() -> None:
    request = build_start_request(
        "cat",
        envs={"FOO": "bar"},
        user="root",
        cwd="/tmp",
        stdin=True,
        timeout=2.5,
        tag="m2",
    )
    assert dict(request.process.envs) == {"FOO": "bar"}
    assert request.user.username == "root"
    assert request.process.cwd == "/tmp"
    assert request.stdin is True
    assert request.timeout_ms == 2500
    assert request.tag == "m2"


def test_start_request_omits_empty_user_and_cwd_and_disables_timeout() -> None:
    request = build_start_request("true", user="", cwd="", timeout=None)
    assert not request.HasField("user")
    assert not request.process.HasField("cwd")
    assert request.timeout_ms == 0
    assert build_start_request("true", timeout=0).timeout_ms == 0


@pytest.mark.parametrize("cmd", ["", "   "])
def test_start_request_rejects_empty_command(cmd: str) -> None:
    with pytest.raises(InvalidArgumentException, match="cmd"):
        build_start_request(cmd)


@pytest.mark.parametrize("timeout", [-1, True, "60"])
def test_timeout_validation(timeout: object) -> None:
    with pytest.raises(InvalidArgumentException, match="timeout"):
        timeout_to_ms(timeout)  # type: ignore[arg-type]


def test_timeout_rounds_to_milliseconds_and_never_collapses_to_zero() -> None:
    assert timeout_to_ms(0.2) == 200
    assert timeout_to_ms(0.0001) == 1
    assert timeout_to_ms(60) == 60_000


def test_stream_deadline_adds_the_grace_period() -> None:
    assert stream_deadline(60) == 65.0
    assert stream_deadline(2) == 7.0
    assert stream_deadline(None) is None
    assert stream_deadline(0) is None


def test_pid_and_from_seq_validation() -> None:
    assert validate_pid(1) == 1
    assert validate_from_seq(0) == 0
    for bad in (0, -1, True, 2**32, "1"):
        with pytest.raises(InvalidArgumentException):
            validate_pid(bad)
    for bad in (-1, True, "0"):
        with pytest.raises(InvalidArgumentException):
            validate_from_seq(bad)


def test_stdin_encoding() -> None:
    assert encode_stdin("hola\n") == b"hola\n"
    assert encode_stdin(b"\x00\xff") == b"\x00\xff"
    assert encode_stdin(bytearray(b"x")) == b"x"
    with pytest.raises(InvalidArgumentException):
        encode_stdin(1)  # type: ignore[arg-type]


def test_first_message_must_be_a_start_event() -> None:
    assert pid_from_start_event(process_pb2.ProcessEvent(start=process_pb2.StartEvent(pid=7))) == 7
    with pytest.raises(SandboxException, match="StartEvent"):
        pid_from_start_event(process_pb2.ProcessEvent(keepalive=common_pb2.KeepAlive()))


def test_decoder_reassembles_a_character_split_across_chunks() -> None:
    out: list[str] = []
    err: list[str] = []
    accumulator = OutputAccumulator(on_stdout=out.append, on_stderr=err.append)
    assert accumulator.feed(process_pb2.DataEvent(stdout=b"aaa\xc3", seq=1)) == ("aaa", None)
    assert accumulator.feed(process_pb2.DataEvent(stdout=b"\xa9\n", seq=2)) == ("é\n", None)
    assert accumulator.feed(process_pb2.DataEvent(stderr=b"e", seq=3)) == (None, "e")
    assert accumulator.feed(process_pb2.DataEvent(stdout=b"\xc3", seq=4)) == (None, None)
    assert accumulator.stdout == "aaaé\n"
    assert accumulator.stderr == "e"
    assert accumulator.last_seq == 4
    assert out == ["aaa", "é\n"]
    assert err == ["e"]
    outcome = accumulator.finish(process_pb2.EndEvent(exit_code=0, exited=True, status="exited"))
    assert isinstance(outcome, CommandResult)
    assert outcome.stdout == "aaaé\n�"
    assert out[-1] == "�"


def end(
    status: str,
    *,
    exit_code: int = 0,
    exited: bool = True,
    signal: int | None = None,
    error: str | None = None,
) -> process_pb2.EndEvent:
    event = process_pb2.EndEvent(exit_code=exit_code, exited=exited, status=status)
    if signal is not None:
        event.signal = signal
    if error is not None:
        event.error.code = error
        event.error.message = f"{error} happened"
    return event


def test_outcome_exit_zero_is_a_result() -> None:
    outcome = outcome_from_end(end("exited"), "out", "err")
    assert outcome == CommandResult(stdout="out", stderr="err", exit_code=0, error=None)


@pytest.mark.parametrize(
    ("event", "exit_code", "error"),
    [
        (end("exited", exit_code=3), 3, "exited"),
        (end("signaled", exit_code=137, signal=9), 137, "signaled"),
        (end("mystery", exit_code=2), 2, "mystery"),
    ],
)
def test_outcome_non_zero_is_command_exit(
    event: process_pb2.EndEvent, exit_code: int, error: str
) -> None:
    outcome = outcome_from_end(event, "out", "err")
    assert isinstance(outcome, CommandExitException)
    assert outcome.exit_code == exit_code
    assert outcome.stdout == "out"
    assert outcome.stderr == "err"
    assert outcome.error == error


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (
            end("timeout", exit_code=143, exited=False, signal=15, error="deadline_exceeded"),
            TimeoutException,
        ),
        (end("output_truncated", exited=False, error="output_truncated"), SandboxException),
        (end("suspending", exited=False, error="suspending"), SandboxStateException),
        (end("mystery", exited=False, error="not_found"), NotFoundException),
        (end("mystery", exited=False, error="permission_denied"), Exception),
    ],
)
def test_outcome_special_statuses(event: process_pb2.EndEvent, expected: type[Exception]) -> None:
    outcome = outcome_from_end(event, "", "")
    assert isinstance(outcome, expected)
    assert not isinstance(outcome, CommandExitException)


def test_truncated_outcome_names_the_recovery_path() -> None:
    outcome = outcome_from_end(
        end("output_truncated", exited=False, error="output_truncated"), "", ""
    )
    assert "commands.connect" in str(outcome)
    assert "output_truncated" in str(outcome)


def test_end_error_name() -> None:
    assert end_error_name(None) is None
    assert end_error_name(end("exited")) is None
    assert end_error_name(end("exited", exit_code=1)) == "exited"
    assert end_error_name(end("signaled", exit_code=137, signal=9)) == "signaled"
    assert end_error_name(end("timeout", exit_code=143, error="deadline_exceeded")) == (
        "deadline_exceeded"
    )


def test_progress_consumes_events_and_resolves_once() -> None:
    progress = CommandProgress(42, OutputAccumulator())
    assert isinstance(progress.adapter, ProcessEvents)
    assert isinstance(
        progress.consume(process_pb2.ProcessEvent(keepalive=common_pb2.KeepAlive())), Nothing
    )
    chunk = progress.consume(
        process_pb2.ProcessEvent(data=process_pb2.DataEvent(stdout=b"hi", seq=1))
    )
    assert chunk == Chunk(seq=1, stdout="hi", stderr=None, pty=None)
    assert chunk.as_output() == ("hi", None, None)
    assert isinstance(
        progress.consume(
            process_pb2.ProcessEvent(data=process_pb2.DataEvent(stdout=b"\xc3", seq=2))
        ),
        Nothing,
    )
    assert progress.is_finished() is False
    ended = progress.consume(process_pb2.ProcessEvent(end=end("exited")))
    assert isinstance(ended, Ended)
    assert progress.is_finished() is True
    assert progress.exit_code == 0
    assert progress.error is None
    assert progress.resolve() == CommandResult(stdout="hi\ufffd", stderr="", exit_code=0)
    assert progress.resolve() is progress.resolve()


def is_suspended(progress: CommandProgress) -> bool:
    """Lectura fresca: `assert progress.suspended is True` estrecha el atributo
    a `Literal[True]` y `resubscribed()` no lo invalida para mypy."""
    return progress.suspended


def test_progress_suspending_end_is_pending_reconnection_not_final() -> None:
    progress = CommandProgress(42, OutputAccumulator())
    consumed = progress.consume(
        process_pb2.ProcessEvent(end=end("suspending", exited=False, error="suspending"))
    )
    assert isinstance(consumed, Suspending)
    assert progress.suspended is True
    assert progress.is_finished() is False
    assert progress.end is None
    reason = suspending_reason(progress)
    assert isinstance(reason, SandboxStateException)
    assert "42" in str(reason)
    progress.resubscribed()
    assert is_suspended(progress) is False
    assert resubscribe_from_seq(progress.accumulator.last_seq) == 1


def test_stream_deadline_bookkeeping_for_reconnections() -> None:
    clock = {"now": 100.0}
    monotonic = lambda: clock["now"]  # noqa: E731
    assert deadline_at(None, monotonic) is None
    at = deadline_at(65.0, monotonic)
    assert at == 165.0
    clock["now"] = 130.0
    assert remaining_deadline(at, monotonic) == 35.0
    assert remaining_deadline(None, monotonic) is None
    clock["now"] = 200.0
    assert remaining_deadline(at, monotonic) == 0.0


def test_progress_without_end_or_after_disconnect_raises() -> None:
    progress = CommandProgress(42, OutputAccumulator())
    with pytest.raises(SandboxException, match="sin EndEvent"):
        progress.resolve()
    disconnected = CommandProgress(43, OutputAccumulator())
    disconnected.disconnected = True
    with pytest.raises(SandboxException, match="desconectado"):
        disconnected.resolve()


def test_progress_fail_caches_the_exception() -> None:
    progress = CommandProgress(42, OutputAccumulator())
    failure = TimeoutException("deadline")
    assert progress.fail(failure) is failure
    with pytest.raises(TimeoutException):
        progress.resolve()


def test_process_info_mapping_including_kind_and_optionals() -> None:
    info = process_pb2.ProcessInfo(
        pid=5,
        config=process_pb2.ProcessConfig(
            cmd="/bin/bash", args=["-l", "-c", "sleep 30"], envs={"A": "1"}, cwd="/tmp"
        ),
        tag="m2",
        kind=process_pb2.PROCESS_KIND_PROCESS,
    )
    assert process_info_from_proto(info) == ProcessInfo(
        pid=5,
        cmd="/bin/bash",
        args=("-l", "-c", "sleep 30"),
        envs={"A": "1"},
        cwd="/tmp",
        tag="m2",
        kind="process",
    )
    bare = process_pb2.ProcessInfo(
        pid=6, config=process_pb2.ProcessConfig(cmd="sh"), kind=process_pb2.PROCESS_KIND_PTY
    )
    mapped = process_info_from_proto(bare)
    assert (mapped.cwd, mapped.tag, mapped.kind, mapped.args) == (None, None, "pty", ())


def test_metrics_mapping_uses_utc_timestamps() -> None:
    response = health_pb2.MetricsResponse(
        cpu_used_pct=12.5,
        mem_used_bytes=1_000,
        mem_total_bytes=2_000,
        disk_used_bytes=3_000,
        disk_total_bytes=4_000,
        cpu_count=2,
        timestamp_unix_ms=1_789_000_000_123,
    )
    metrics = metrics_from_proto(response)
    assert metrics.cpu_used_pct == 12.5
    assert (metrics.mem_used_bytes, metrics.mem_total_bytes) == (1_000, 2_000)
    assert (metrics.disk_used_bytes, metrics.disk_total_bytes) == (3_000, 4_000)
    assert metrics.cpu_count == 2
    assert metrics.timestamp == datetime.fromtimestamp(1_789_000_000.123, tz=UTC)
    assert metrics.timestamp.tzinfo is UTC


def rpc_error(code: grpc.StatusCode, details: str = "boom", debug: str = "") -> FakeRpcError:
    return FakeRpcError(code, details=details, debug=debug)


def test_is_stream_reset_markers() -> None:
    assert is_stream_reset(rpc_error(grpc.StatusCode.UNAVAILABLE, "Socket closed"))
    assert not is_stream_reset(rpc_error(grpc.StatusCode.UNAVAILABLE, "suspending"))
    assert not is_stream_reset(rpc_error(grpc.StatusCode.UNAVAILABLE, "terminating"))
    assert not is_stream_reset(
        rpc_error(grpc.StatusCode.UNAVAILABLE, "kernel not ready: sidecar relaunching")
    )
    assert is_stream_reset(rpc_error(grpc.StatusCode.INTERNAL, "RST_STREAM received"))
    assert is_stream_reset(rpc_error(grpc.StatusCode.INTERNAL, "x", debug="GOAWAY"))
    assert not is_stream_reset(rpc_error(grpc.StatusCode.INTERNAL, "panic in rayd"))
    assert not is_stream_reset(rpc_error(grpc.StatusCode.NOT_FOUND))


@pytest.mark.parametrize(
    ("exc", "health_ok", "state", "expected"),
    [
        (rpc_error(grpc.StatusCode.UNAVAILABLE, "Socket closed"), True, None, SandboxException),
        (
            rpc_error(grpc.StatusCode.UNAVAILABLE, "suspending"),
            True,
            None,
            SandboxStateException,
        ),
        (
            rpc_error(grpc.StatusCode.UNAVAILABLE, "terminating"),
            True,
            None,
            SandboxStateException,
        ),
        (
            rpc_error(grpc.StatusCode.UNAVAILABLE, "Socket closed"),
            False,
            "TERMINATED",
            SandboxNotFoundException,
        ),
        (
            rpc_error(grpc.StatusCode.INTERNAL, "Received RST_STREAM"),
            False,
            "TERMINATING",
            SandboxNotFoundException,
        ),
        (
            rpc_error(grpc.StatusCode.UNAVAILABLE, "GOAWAY"),
            False,
            "SUSPENDED",
            SandboxStateException,
        ),
        (
            rpc_error(grpc.StatusCode.UNAVAILABLE, "GOAWAY"),
            False,
            "SUSPENDING",
            SandboxStateException,
        ),
        (rpc_error(grpc.StatusCode.UNAVAILABLE, "GOAWAY"), False, "RUNNING", SandboxException),
        (rpc_error(grpc.StatusCode.UNAVAILABLE, "GOAWAY"), False, None, SandboxException),
        (rpc_error(grpc.StatusCode.NOT_FOUND), True, None, NotFoundException),
        (rpc_error(grpc.StatusCode.OUT_OF_RANGE), True, None, NotFoundException),
        (rpc_error(grpc.StatusCode.FAILED_PRECONDITION), True, None, InvalidArgumentException),
        (rpc_error(grpc.StatusCode.DEADLINE_EXCEEDED), True, None, TimeoutException),
        (rpc_error(grpc.StatusCode.INTERNAL, "panic"), False, "TERMINATED", SandboxException),
    ],
)
def test_stream_failure_matrix(
    exc: FakeRpcError, health_ok: bool, state: str | None, expected: type[Exception]
) -> None:
    mapped = stream_failure_exception(exc, health_ok=health_ok, state=state)
    assert type(mapped) is expected
    assert isinstance(mapped, SandboxException | AuthenticationException)
    assert mapped.grpc_code is exc.code()


def test_stream_failure_alive_message_points_to_connect() -> None:
    mapped = stream_failure_exception(
        rpc_error(grpc.StatusCode.UNAVAILABLE, "Socket closed"), health_ok=True, state=None
    )
    assert "commands.connect" in str(mapped)
