"""Reglas puras de `_persistence_base` (sin red)."""

from __future__ import annotations

import grpc
import pytest

from rayito import IdlePolicy, LaunchOptions, S3Prefix
from rayito._persistence_base import (
    INTERRUPTED_MESSAGE,
    UNIMPLEMENTED_MESSAGE,
    bind_persist,
    checkpoint_request,
    checkpoint_result_from_proto,
    handle_checkpoint_event,
    handle_restore_event,
    launch_kwargs,
    mid_stream_exception,
    require_named_persist,
    require_role_for_persist,
    require_started,
    resolve_target,
    restore_request,
    restore_result_from_proto,
    should_auto_restore,
    status_exception,
    stream_error_exception,
    validate_exclude,
    validate_persist_timeout,
)
from rayito._transport import PROXY_FORBIDDEN_MARKER
from rayito.exceptions import (
    AuthenticationException,
    InvalidArgumentException,
    NotFoundException,
    PersistenceException,
    SandboxException,
    TimeoutException,
)
from rayito.v1 import common_pb2, filesystem_pb2

from .conftest import FakeRpcError

BUCKET = "my-bucket"


def prefix(name: str | None = "n") -> S3Prefix:
    return S3Prefix(BUCKET, name=name)


# ---------------------------------------------------------------- requests


def test_checkpoint_request_carries_location_excludes_and_user() -> None:
    request = checkpoint_request(
        S3Prefix(BUCKET, prefix="base", name="x", region="eu-west-1"), ["skipme", "a/b"], "user"
    )
    assert request.target.bucket == BUCKET
    assert request.target.key_prefix == "base/x"
    assert request.target.region == "eu-west-1"
    assert list(request.exclude) == ["skipme", "a/b"]
    assert request.user.username == "user"
    bare = checkpoint_request(prefix(), (), None)
    assert not bare.HasField("user")
    assert not bare.target.HasField("region")


def test_restore_request_has_no_exclude() -> None:
    request = restore_request(prefix(), None)
    assert request.source.key_prefix == "rayito/n"
    assert not request.HasField("user")


@pytest.mark.parametrize(
    "exclude",
    [["/abs"], [""], ["../x"], ["a/../b"], ["."], ["a\0b"], ["a" * 4097]],
)
def test_exclude_follows_request_path_rules(exclude: list[str]) -> None:
    with pytest.raises(InvalidArgumentException):
        validate_exclude(exclude)


def test_exclude_count_is_capped_at_64() -> None:
    assert len(validate_exclude([f"d{i}" for i in range(64)])) == 64
    with pytest.raises(InvalidArgumentException, match="64"):
        validate_exclude([f"d{i}" for i in range(65)])


def test_resolve_target_prefers_explicit_and_requires_a_name() -> None:
    explicit = prefix("e")
    assert resolve_target(explicit, prefix("b")) is explicit
    assert resolve_target(None, explicit) is explicit
    with pytest.raises(InvalidArgumentException, match="destino"):
        resolve_target(None, None)
    with pytest.raises(InvalidArgumentException, match="name"):
        resolve_target(prefix(None), None)


def test_persist_timeout_validation() -> None:
    assert validate_persist_timeout(None) == 600.0
    assert validate_persist_timeout(30) == 30.0
    for bad in (0, -1, True, "x"):
        with pytest.raises(InvalidArgumentException):
            validate_persist_timeout(bad)  # type: ignore[arg-type]


# ------------------------------------------------------------ create rules


def test_persist_requires_an_execution_role() -> None:
    require_role_for_persist(None, None)
    require_role_for_persist(prefix(), "arn:aws:iam::1:role/x")
    with pytest.raises(InvalidArgumentException, match="execution_role_arn"):
        require_role_for_persist(prefix(), None)


def test_bind_defaults_the_name_to_the_sandbox_id() -> None:
    assert bind_persist(prefix(None), "mvm-1").name == "mvm-1"
    assert bind_persist(prefix("given"), "mvm-1").name == "given"
    assert should_auto_restore(prefix("given")) is True
    assert should_auto_restore(prefix(None)) is False
    assert require_named_persist(prefix("given")).name == "given"
    with pytest.raises(InvalidArgumentException, match="connect"):
        require_named_persist(prefix(None))


def test_launch_kwargs_reproduce_create() -> None:
    options = LaunchOptions(
        template="arn",
        template_version="3.0",
        timeout=1800,
        idle=IdlePolicy(),
        envs={"A": "1"},
        metadata={"k": "v"},
        cpu_time_limit=10,
        execution_role_arn="arn:aws:iam::1:role/x",
        allowed_ports=(8080,),
        ingress=("ALL_INGRESS",),
        egress=None,
        logging="disabled",
        access_token=None,
        ready_timeout=90.0,
        request_timeout=60.0,
        reconnect_timeout=60.0,
        keep_on_failure=False,
        control_plane="plane",
        transport="transport",
    )
    kwargs = launch_kwargs(options)
    assert kwargs["template"] == "arn"
    assert kwargs["execution_role_arn"] == "arn:aws:iam::1:role/x"
    assert kwargs["control_plane"] == "plane"
    assert "persist" not in kwargs and "pool" not in kwargs


def test_launch_options_repr_redacts_token_and_envs() -> None:
    token = "tok-secret-9f8e7d6c"
    options = LaunchOptions(
        template="arn",
        template_version=None,
        timeout=1800,
        idle=None,
        envs={"OPENAI_API_KEY": "sk-secret-value", "B": "2"},
        metadata=None,
        cpu_time_limit=None,
        execution_role_arn=None,
        allowed_ports=None,
        ingress=None,
        egress=None,
        logging=None,
        access_token=token,
        ready_timeout=90.0,
        request_timeout=60.0,
        reconnect_timeout=60.0,
        keep_on_failure=False,
        control_plane=None,
        transport=None,
    )
    for rendered in (repr(options), str(options), f"{options}"):
        assert token not in rendered
        assert "sk-secret-value" not in rendered
        assert "OPENAI_API_KEY" not in rendered
        assert "access_token='<redacted>'" in rendered
        assert "envs=<2 keys>" in rendered
    assert options.access_token == token
    without_token = LaunchOptions(**{**options.__dict__, "access_token": None, "envs": None})
    assert "access_token=None" in repr(without_token)
    assert "envs=<0 keys>" in repr(without_token)


# ------------------------------------------------------------------ events


def started(files: int = 2, size: int = 10) -> filesystem_pb2.CheckpointEvent:
    return filesystem_pb2.CheckpointEvent(
        started=filesystem_pb2.CheckpointStarted(files=files, bytes=size)
    )


def test_require_started_accepts_started_and_refuses_the_rest() -> None:
    require_started(started(), rpc="Checkpoint")
    with pytest.raises(SandboxException, match="sin mensajes"):
        require_started(None, rpc="Checkpoint")
    with pytest.raises(SandboxException, match="started"):
        require_started(
            filesystem_pb2.CheckpointEvent(keepalive=common_pb2.KeepAlive()), rpc="Checkpoint"
        )
    early = filesystem_pb2.CheckpointEvent(
        error=common_pb2.StreamError(code="permission_denied", message="x")
    )
    with pytest.raises(PersistenceException) as excinfo:
        require_started(early, rpc="Checkpoint")
    assert excinfo.value.code == "permission_denied"


def test_checkpoint_events_translate_to_progress_and_result() -> None:
    target = prefix()
    seen: list[int] = []
    progress = filesystem_pb2.CheckpointEvent(
        progress=filesystem_pb2.CheckpointProgress(files_done=1, bytes_read=5, bytes_uploaded=2)
    )
    assert handle_checkpoint_event(progress, target, lambda p: seen.append(p.files_done)) is None
    assert seen == [1]
    assert handle_checkpoint_event(progress, target, None) is None
    keepalive = filesystem_pb2.CheckpointEvent(keepalive=common_pb2.KeepAlive())
    assert handle_checkpoint_event(keepalive, target, None) is None
    done = filesystem_pb2.CheckpointEvent(
        done=filesystem_pb2.CheckpointDone(
            files=2, bytes_read=10, archive_bytes=7, sha256="ab" * 32, skipped=1, duration_ms=1500
        )
    )
    result = handle_checkpoint_event(done, target, None)
    assert result == checkpoint_result_from_proto(target, done.done)
    assert result is not None
    assert result.uri == "s3://my-bucket/rayito/n"
    assert result.duration == 1.5
    assert result.skipped == 1
    error = filesystem_pb2.CheckpointEvent(
        error=common_pb2.StreamError(code="internal", message="archive checksum mismatch")
    )
    with pytest.raises(PersistenceException) as excinfo:
        handle_checkpoint_event(error, target, None)
    assert excinfo.value.code == "internal"


def test_restore_events_translate_to_progress_and_result() -> None:
    source = prefix()
    seen: list[int] = []
    progress = filesystem_pb2.RestoreEvent(
        progress=filesystem_pb2.RestoreProgress(files_done=3, bytes_downloaded=9)
    )
    assert handle_restore_event(progress, source, lambda p: seen.append(p.bytes_downloaded)) is None
    assert seen == [9]
    done = filesystem_pb2.RestoreEvent(
        done=filesystem_pb2.RestoreDone(
            files=2, bytes_written=10, archive_bytes=7, sha256="cd" * 32, skipped=0, duration_ms=800
        )
    )
    result = handle_restore_event(done, source, None)
    assert result == restore_result_from_proto(source, done.done)
    assert result is not None
    assert result.bytes_written == 10 and result.duration == 0.8
    missing = filesystem_pb2.RestoreEvent(
        error=common_pb2.StreamError(code="not_found", message="archive missing")
    )
    with pytest.raises(NotFoundException):
        handle_restore_event(missing, source, None)


# ------------------------------------------------------------------ errors


@pytest.mark.parametrize(
    ("code", "expected", "persistence_code"),
    [
        ("not_found", NotFoundException, None),
        ("invalid_argument", InvalidArgumentException, None),
        ("deadline_exceeded", TimeoutException, None),
        ("permission_denied", PersistenceException, "permission_denied"),
        ("internal", PersistenceException, "internal"),
        ("suspending", PersistenceException, "interrupted"),
        ("unimplemented", PersistenceException, "unimplemented"),
        ("weird", PersistenceException, "weird"),
    ],
)
def test_stream_error_table(code: str, expected: type, persistence_code: str | None) -> None:
    error = stream_error_exception(common_pb2.StreamError(code=code, message="m"))
    assert isinstance(error, expected)
    if persistence_code is not None:
        assert isinstance(error, PersistenceException)
        assert error.code == persistence_code
    if code == "unimplemented":
        assert str(error) == UNIMPLEMENTED_MESSAGE


@pytest.mark.parametrize(
    ("status", "expected", "persistence_code"),
    [
        (grpc.StatusCode.NOT_FOUND, NotFoundException, None),
        (grpc.StatusCode.INVALID_ARGUMENT, InvalidArgumentException, None),
        (grpc.StatusCode.FAILED_PRECONDITION, PersistenceException, "failed_precondition"),
        (grpc.StatusCode.PERMISSION_DENIED, PersistenceException, "permission_denied"),
        (grpc.StatusCode.UNAUTHENTICATED, AuthenticationException, None),
        (grpc.StatusCode.UNIMPLEMENTED, PersistenceException, "unimplemented"),
        (grpc.StatusCode.DEADLINE_EXCEEDED, TimeoutException, None),
        (grpc.StatusCode.RESOURCE_EXHAUSTED, PersistenceException, "resource_exhausted"),
        (grpc.StatusCode.UNAVAILABLE, PersistenceException, "interrupted"),
        (grpc.StatusCode.INTERNAL, PersistenceException, "internal"),
    ],
)
def test_status_table(
    status: grpc.StatusCode, expected: type, persistence_code: str | None
) -> None:
    error = status_exception(FakeRpcError(status, details="detail"))
    assert isinstance(error, expected)
    if persistence_code is not None:
        assert isinstance(error, PersistenceException)
        assert error.code == persistence_code
        assert error.grpc_code is status


def test_proxy_403_is_authentication_not_persistence() -> None:
    error = status_exception(
        FakeRpcError(
            grpc.StatusCode.PERMISSION_DENIED,
            details="Received http2 header with status: 403",
            debug=f'{{"grpc_status":7,"description":"{PROXY_FORBIDDEN_MARKER}"}}',
        )
    )
    assert isinstance(error, AuthenticationException)
    assert error.proxy_rejected is True


def test_mid_stream_errors_are_interruptions_never_reconnections() -> None:
    cut = mid_stream_exception(FakeRpcError(grpc.StatusCode.UNAVAILABLE, details="suspending"))
    assert isinstance(cut, PersistenceException) and cut.code == "interrupted"
    assert str(cut) == INTERRUPTED_MESSAGE.format(reason="suspending")
    late = mid_stream_exception(FakeRpcError(grpc.StatusCode.DEADLINE_EXCEEDED, details="d"))
    assert isinstance(late, TimeoutException)
    cancelled = mid_stream_exception(FakeRpcError(grpc.StatusCode.CANCELLED, details="c"))
    assert isinstance(cancelled, SandboxException) and not isinstance(
        cancelled, PersistenceException
    )
