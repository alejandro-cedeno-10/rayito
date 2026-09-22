"""`Sandbox.create(persist=)`, `checkpoint_files`, `restore_files` y
`reincarnate` contra el `rayd` falso (gRPC real en loopback) y el plano de
control con Stubber."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import grpc
import pytest

from rayito import (
    AsyncSandbox,
    CheckpointProgress,
    CheckpointResult,
    RestoreProgress,
    RestoreResult,
    S3Prefix,
    Sandbox,
)
from rayito._limits import DEFAULT_PORT
from rayito._persistence_base import UNIMPLEMENTED_MESSAGE
from rayito._transport import PROXY_FORBIDDEN_MARKER
from rayito.exceptions import (
    InvalidArgumentException,
    NotFoundException,
    PersistenceException,
    SandboxException,
)
from rayito.v1 import filesystem_pb2_grpc

from .conftest import (
    ACCESS_TOKEN,
    IMAGE_ARN,
    SANDBOX_ID,
    FakeRpcError,
    RaydEndpoint,
    StubbedControlPlane,
    auth_token_response,
    microvm_response,
)
from .fake_persistence import FakePersistence

BUCKET = "my-bucket"
ROLE = "arn:aws:iam::123456789012:role/rayito-execution"
SUCCESSOR_ID = "microvm-00000000-0000-0000-0000-000000000002"


def stub_launch(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, *, sandbox_id: str = SANDBOX_ID
) -> None:
    response = microvm_response(endpoint=fake_rayd.host)
    response["microvmId"] = sandbox_id
    control_plane.microvms.add_response("run_microvm", response)
    control_plane.microvms.add_response(
        "create_microvm_auth_token",
        auth_token_response(),
        expected_params={
            "microvmIdentifier": sandbox_id,
            "expirationInMinutes": 60,
            "allowedPorts": [{"port": DEFAULT_PORT}],
        },
    )


def stub_terminate(control_plane: StubbedControlPlane, sandbox_id: str = SANDBOX_ID) -> None:
    control_plane.microvms.add_response(
        "terminate_microvm", {}, expected_params={"microvmIdentifier": sandbox_id}
    )


def create(control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, **kwargs: Any) -> Sandbox:
    stub_launch(control_plane, fake_rayd)
    return Sandbox.create(
        IMAGE_ARN,
        idle=None,
        access_token=ACCESS_TOKEN,
        execution_role_arn=ROLE,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
        **kwargs,
    )


@pytest.fixture
def fake_persistence(fake_rayd: RaydEndpoint) -> FakePersistence:
    return fake_rayd.filesystem.persistence


@pytest.fixture
def sandbox(control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint) -> Iterator[Sandbox]:
    created = create(control_plane, fake_rayd, persist=S3Prefix(BUCKET))
    try:
        yield created
    finally:
        stub_terminate(control_plane)
        created.kill()


# ------------------------------------------------------------ create rules


def test_persist_without_role_is_rejected_before_any_plane_call(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    with pytest.raises(InvalidArgumentException, match="execution_role_arn"):
        Sandbox.create(
            IMAGE_ARN,
            persist=S3Prefix(BUCKET),
            control_plane=control_plane.plane,
            transport=fake_rayd.transport,
        )
    assert fake_rayd.servicer.health_calls == []


def test_bind_without_name_uses_the_sandbox_id_and_does_not_restore(
    sandbox: Sandbox, fake_persistence: FakePersistence
) -> None:
    assert sandbox.persist == S3Prefix(BUCKET, name=SANDBOX_ID)
    assert sandbox.persist is not None
    assert sandbox.persist.uri == f"s3://{BUCKET}/rayito/{SANDBOX_ID}"
    assert sandbox.last_restore is None
    assert fake_persistence.restore_requests == []


def test_bind_with_name_auto_restores_and_swallows_not_found(
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
    fake_persistence: FakePersistence,
) -> None:
    first = create(control_plane, fake_rayd, persist=S3Prefix(BUCKET, name="agent-7"))
    try:
        assert first.last_restore is None
        assert len(fake_persistence.restore_requests) == 1
    finally:
        stub_terminate(control_plane)
        first.kill()
    fake_persistence.seed(BUCKET, "rayito/agent-7", files=5)
    second = create(control_plane, fake_rayd, persist=S3Prefix(BUCKET, name="agent-7"))
    try:
        restored = second.last_restore
        assert isinstance(restored, RestoreResult)
        assert restored.files == 5
        assert restored.key_prefix == "rayito/agent-7"
        assert restored.uri == f"s3://{BUCKET}/rayito/agent-7"
    finally:
        stub_terminate(control_plane)
        second.kill()


def test_auto_restore_failure_terminates_the_sandbox(
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
    fake_persistence: FakePersistence,
) -> None:
    fake_persistence.seed(BUCKET, "rayito/broken")
    fake_persistence.fail_after_started = ("internal", "archive checksum mismatch")
    stub_launch(control_plane, fake_rayd)
    stub_terminate(control_plane)
    with pytest.raises(PersistenceException) as excinfo:
        Sandbox.create(
            IMAGE_ARN,
            idle=None,
            access_token=ACCESS_TOKEN,
            execution_role_arn=ROLE,
            persist=S3Prefix(BUCKET, name="broken"),
            control_plane=control_plane.plane,
            transport=fake_rayd.transport,
        )
    assert excinfo.value.code == "internal"


def test_auto_restore_failure_keeps_the_vm_with_keep_on_failure(
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
    fake_persistence: FakePersistence,
) -> None:
    fake_persistence.has_credentials = False
    with pytest.raises(PersistenceException) as excinfo:
        create(
            control_plane,
            fake_rayd,
            persist=S3Prefix(BUCKET, name="norole"),
            keep_on_failure=True,
        )
    assert excinfo.value.code == "permission_denied"


def test_pool_and_persist_are_exclusive(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    with pytest.raises(InvalidArgumentException, match="pool"):
        Sandbox.create(
            IMAGE_ARN,
            execution_role_arn=ROLE,
            persist=S3Prefix(BUCKET),
            pool=object(),  # type: ignore[arg-type]
            control_plane=control_plane.plane,
            transport=fake_rayd.transport,
        )


def test_connect_binds_without_restoring_and_requires_a_name(
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
    fake_persistence: FakePersistence,
) -> None:
    fake_persistence.seed(BUCKET, "rayito/named")
    with pytest.raises(InvalidArgumentException, match="name"):
        Sandbox.connect(
            SANDBOX_ID,
            access_token=ACCESS_TOKEN,
            persist=S3Prefix(BUCKET),
            control_plane=control_plane.plane,
            transport=fake_rayd.transport,
        )
    control_plane.microvms.add_response(
        "get_microvm",
        microvm_response(endpoint=fake_rayd.host, state="RUNNING"),
        expected_params={"microvmIdentifier": SANDBOX_ID},
    )
    control_plane.microvms.add_response(
        "create_microvm_auth_token",
        auth_token_response(),
        expected_params={
            "microvmIdentifier": SANDBOX_ID,
            "expirationInMinutes": 60,
            "allowedPorts": [{"port": DEFAULT_PORT}],
        },
    )
    handle = Sandbox.connect(
        SANDBOX_ID,
        access_token=ACCESS_TOKEN,
        persist=S3Prefix(BUCKET, name="named"),
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    )
    try:
        assert handle.persist == S3Prefix(BUCKET, name="named")
        assert handle.last_restore is None
        assert fake_persistence.restore_requests == []
        with pytest.raises(InvalidArgumentException, match="create"):
            handle.reincarnate()
    finally:
        handle.close()


# ---------------------------------------------------------- checkpoint


def test_checkpoint_returns_the_done_fields_and_reports_progress(
    sandbox: Sandbox, fake_persistence: FakePersistence
) -> None:
    seen: list[CheckpointProgress] = []
    result = sandbox.checkpoint_files(exclude=["skipme", "data/raw"], on_progress=seen.append)
    assert isinstance(result, CheckpointResult)
    assert result.bucket == BUCKET
    assert result.key_prefix == f"rayito/{SANDBOX_ID}"
    assert result.files == 4
    assert result.bytes_read == 52_428_800
    assert result.archive_bytes == 52_428_800 // 3
    assert len(result.sha256) == 64
    assert result.skipped == 1
    assert result.duration == 1.5
    assert [progress.files_done for progress in seen] == [1, 2]
    request = fake_persistence.checkpoint_requests[-1]
    assert list(request.exclude) == ["skipme", "data/raw"]
    assert request.target.key_prefix == f"rayito/{SANDBOX_ID}"
    stored = fake_persistence.stored(BUCKET, f"rayito/{SANDBOX_ID}")
    assert stored is not None and stored.excluded == ("skipme", "data/raw")


def test_checkpoint_goes_through_the_stream_channel_with_the_deadline(
    sandbox: Sandbox, fake_rayd: RaydEndpoint
) -> None:
    sandbox.checkpoint_files(timeout=120)
    deadlines = fake_rayd.filesystem.deadlines["Checkpoint"]
    assert 100 < deadlines[-1] <= 122
    assert sandbox._stream_channel is not None
    assert fake_rayd.filesystem.peers["Checkpoint"]


def test_checkpoint_explicit_target_and_local_validation(
    sandbox: Sandbox, fake_persistence: FakePersistence
) -> None:
    result = sandbox.checkpoint_files(target=S3Prefix("other-bucket", prefix="p", name="q"))
    assert result.uri == "s3://other-bucket/p/q"
    with pytest.raises(InvalidArgumentException):
        sandbox.checkpoint_files(exclude=["../etc"])
    with pytest.raises(InvalidArgumentException):
        sandbox.checkpoint_files(exclude=[f"d{i}" for i in range(65)])
    with pytest.raises(InvalidArgumentException, match="name"):
        sandbox.checkpoint_files(target=S3Prefix(BUCKET))
    assert len(fake_persistence.checkpoint_requests) == 1


def test_no_credentials_is_permission_denied_before_started(
    sandbox: Sandbox, fake_persistence: FakePersistence
) -> None:
    fake_persistence.has_credentials = False
    with pytest.raises(PersistenceException) as excinfo:
        sandbox.checkpoint_files()
    assert excinfo.value.code == "permission_denied"
    assert excinfo.value.grpc_code is grpc.StatusCode.PERMISSION_DENIED
    assert "no execution role credentials" in str(excinfo.value)


def test_root_user_is_permission_denied(sandbox: Sandbox) -> None:
    with pytest.raises(PersistenceException) as excinfo:
        sandbox.checkpoint_files(user="root")
    assert excinfo.value.code == "permission_denied"


def test_busy_is_failed_precondition(sandbox: Sandbox, fake_persistence: FakePersistence) -> None:
    fake_persistence.busy = True
    with pytest.raises(PersistenceException) as excinfo:
        sandbox.checkpoint_files()
    assert excinfo.value.code == "failed_precondition"
    assert "persistence busy" in str(excinfo.value)


def test_unimplemented_names_the_image(sandbox: Sandbox, fake_persistence: FakePersistence) -> None:
    fake_persistence.unimplemented = True
    with pytest.raises(PersistenceException) as excinfo:
        sandbox.checkpoint_files()
    assert excinfo.value.code == "unimplemented"
    assert str(excinfo.value) == UNIMPLEMENTED_MESSAGE
    with pytest.raises(PersistenceException) as restore_info:
        sandbox.restore_files()
    assert restore_info.value.code == "unimplemented"


def test_stream_error_after_started_is_a_persistence_exception(
    sandbox: Sandbox, fake_persistence: FakePersistence
) -> None:
    fake_persistence.fail_after_started = ("permission_denied", "access denied by the bucket")
    with pytest.raises(PersistenceException) as excinfo:
        sandbox.checkpoint_files()
    assert excinfo.value.code == "permission_denied"
    assert "access denied by the bucket" in str(excinfo.value)
    assert fake_persistence.stored(BUCKET, f"rayito/{SANDBOX_ID}") is None


def test_suspend_mid_stream_is_interrupted_not_reconnected(
    sandbox: Sandbox, fake_persistence: FakePersistence
) -> None:
    fake_persistence.fail_after_started = ("suspending", "suspending")
    with pytest.raises(PersistenceException) as excinfo:
        sandbox.checkpoint_files()
    assert excinfo.value.code == "interrupted"
    assert len(fake_persistence.checkpoint_requests) == 1


def test_proxy_403_before_the_first_message_remints_once(
    sandbox: Sandbox,
    control_plane: StubbedControlPlane,
    fake_persistence: FakePersistence,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control_plane.microvms.add_response(
        "create_microvm_auth_token",
        auth_token_response("eyJ.second.jwe"),
        expected_params={
            "microvmIdentifier": SANDBOX_ID,
            "expirationInMinutes": 60,
            "allowedPorts": [{"port": DEFAULT_PORT}],
        },
    )
    stub = sandbox._stub(filesystem_pb2_grpc.FilesystemServiceStub, stream=True)
    real = stub.Checkpoint
    failures = [
        FakeRpcError(
            grpc.StatusCode.PERMISSION_DENIED,
            details="Received http2 header with status: 403",
            debug=f'{{"grpc_status":7,"description":"{PROXY_FORBIDDEN_MARKER}"}}',
        )
    ]

    def checkpoint(request: Any, timeout: float | None = None) -> Any:
        if failures:
            raise failures.pop(0)
        return real(request, timeout=timeout)

    monkeypatch.setattr(stub, "Checkpoint", checkpoint)
    result = sandbox.checkpoint_files()
    assert result.files == 4
    assert len(fake_persistence.checkpoint_requests) == 1


# -------------------------------------------------------------- restore


def test_restore_returns_the_done_fields_and_reports_progress(
    sandbox: Sandbox, fake_persistence: FakePersistence
) -> None:
    fake_persistence.seed(BUCKET, f"rayito/{SANDBOX_ID}", files=7, sha256="cd" * 32)
    seen: list[RestoreProgress] = []
    result = sandbox.restore_files(on_progress=seen.append)
    assert isinstance(result, RestoreResult)
    assert result.files == 7
    assert result.sha256 == "cd" * 32
    assert result.bytes_written == 1234
    assert result.archive_bytes == 999
    assert result.duration == 0.8
    assert [progress.bytes_downloaded for progress in seen] == [500, 1000]


def test_explicit_restore_of_a_missing_checkpoint_is_not_found(sandbox: Sandbox) -> None:
    with pytest.raises(NotFoundException):
        sandbox.restore_files(source=S3Prefix(BUCKET, name="never"))


def test_restore_error_after_started_is_a_persistence_exception(
    sandbox: Sandbox, fake_persistence: FakePersistence
) -> None:
    fake_persistence.seed(BUCKET, f"rayito/{SANDBOX_ID}")
    fake_persistence.fail_after_started = ("internal", "archive checksum mismatch")
    with pytest.raises(PersistenceException) as excinfo:
        sandbox.restore_files()
    assert excinfo.value.code == "internal"
    assert "checksum" in str(excinfo.value)


# ---------------------------------------------------------- reincarnate


def test_reincarnate_checkpoints_creates_restores_and_kills_in_order(
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
    fake_persistence: FakePersistence,
) -> None:
    original = create(
        control_plane,
        fake_rayd,
        persist=S3Prefix(BUCKET),
        metadata={"agent": "7"},
        timeout=1800,
    )
    stub_launch(control_plane, fake_rayd, sandbox_id=SUCCESSOR_ID)
    stub_terminate(control_plane, SANDBOX_ID)
    successor = original.reincarnate(exclude=["skipme"])
    try:
        assert successor is not original
        assert successor.sandbox_id == SUCCESSOR_ID
        assert successor.persist == original.persist == S3Prefix(BUCKET, name=SANDBOX_ID)
        restored = successor.last_restore
        assert isinstance(restored, RestoreResult)
        assert restored.files == 4
        assert successor.metadata == {}
        assert original._closed is True
        request = fake_persistence.checkpoint_requests[-1]
        assert list(request.exclude) == ["skipme"]
        assert fake_persistence.restore_requests[-1].source.key_prefix == f"rayito/{SANDBOX_ID}"
        assert successor._launch_options is not None
        assert successor._launch_options.metadata == {"agent": "7"}
        assert successor._launch_options.timeout == 1800
    finally:
        stub_terminate(control_plane, SUCCESSOR_ID)
        successor.kill()


def test_reincarnate_leaves_the_old_sandbox_alive_when_create_fails(
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
    fake_persistence: FakePersistence,
) -> None:
    original = create(control_plane, fake_rayd, persist=S3Prefix(BUCKET))
    control_plane.microvms.add_client_error(
        "run_microvm", service_error_code="ServiceQuotaExceededException", http_status_code=402
    )
    try:
        with pytest.raises(Exception) as excinfo:
            original.reincarnate()
        assert not isinstance(excinfo.value, (PersistenceException, NotFoundException))
        assert any("reincarnate()" in note for note in getattr(excinfo.value, "__notes__", []))
        assert original._closed is False
        assert fake_persistence.stored(BUCKET, f"rayito/{SANDBOX_ID}") is not None
        assert original.checkpoint_files().files == 4
    finally:
        stub_terminate(control_plane)
        original.kill()


def test_reincarnate_requires_persist(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    plain = create(control_plane, fake_rayd)
    try:
        with pytest.raises(InvalidArgumentException, match="persist"):
            plain.reincarnate()
        with pytest.raises(InvalidArgumentException, match="destino"):
            plain.checkpoint_files()
    finally:
        stub_terminate(control_plane)
        plain.kill()


def test_sync_and_async_expose_the_same_persistence_names() -> None:
    names = {"persist", "last_restore", "checkpoint_files", "restore_files", "reincarnate"}
    assert names <= set(dir(Sandbox))
    assert names <= set(dir(AsyncSandbox))
    for name in ("checkpoint_files", "restore_files", "reincarnate"):
        assert callable(getattr(Sandbox, name)) and callable(getattr(AsyncSandbox, name))


def test_checkpoint_stream_ending_without_done_is_interrupted(
    sandbox: Sandbox, fake_persistence: FakePersistence, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_persistence.progress_events = 0
    original = fake_persistence._checkpoint_events

    def truncated(request: Any, context: Any) -> Any:
        events = original(request, context)
        yield next(events)
        events.close()

    monkeypatch.setattr(fake_persistence, "_checkpoint_events", truncated)
    with pytest.raises(SandboxException) as excinfo:
        sandbox.checkpoint_files()
    assert isinstance(excinfo.value, PersistenceException)
    assert excinfo.value.code == "interrupted"
