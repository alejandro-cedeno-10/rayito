"""`AsyncSandbox.create(persist=)`, `checkpoint_files`, `restore_files` y
`reincarnate`: los mismos escenarios que `test_persistence_sync` sobre
`grpc.aio`."""

from __future__ import annotations

from collections.abc import AsyncIterator
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
)
from rayito._limits import DEFAULT_PORT
from rayito._persistence_base import UNIMPLEMENTED_MESSAGE
from rayito._transport import PROXY_FORBIDDEN_MARKER
from rayito.exceptions import (
    InvalidArgumentException,
    NotFoundException,
    PersistenceException,
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
SUCCESSOR_ID = "microvm-00000000-0000-0000-0000-000000000003"


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


async def create(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, **kwargs: Any
) -> AsyncSandbox:
    stub_launch(control_plane, fake_rayd)
    return await AsyncSandbox.create(
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
async def sandbox(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> AsyncIterator[AsyncSandbox]:
    created = await create(control_plane, fake_rayd, persist=S3Prefix(BUCKET))
    try:
        yield created
    finally:
        stub_terminate(control_plane)
        await created.kill()


async def test_async_persist_without_role_is_rejected_before_any_plane_call(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    with pytest.raises(InvalidArgumentException, match="execution_role_arn"):
        await AsyncSandbox.create(
            IMAGE_ARN,
            persist=S3Prefix(BUCKET),
            control_plane=control_plane.plane,
            transport=fake_rayd.transport,
        )
    assert fake_rayd.servicer.health_calls == []


async def test_async_bind_without_name_uses_the_sandbox_id(
    sandbox: AsyncSandbox, fake_persistence: FakePersistence
) -> None:
    assert sandbox.persist == S3Prefix(BUCKET, name=SANDBOX_ID)
    assert sandbox.last_restore is None
    assert fake_persistence.restore_requests == []


async def test_async_bind_with_name_auto_restores_and_swallows_not_found(
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
    fake_persistence: FakePersistence,
) -> None:
    first = await create(control_plane, fake_rayd, persist=S3Prefix(BUCKET, name="agent-7"))
    try:
        assert first.last_restore is None
        assert len(fake_persistence.restore_requests) == 1
    finally:
        stub_terminate(control_plane)
        await first.kill()
    fake_persistence.seed(BUCKET, "rayito/agent-7", files=5)
    second = await create(control_plane, fake_rayd, persist=S3Prefix(BUCKET, name="agent-7"))
    try:
        restored = second.last_restore
        assert isinstance(restored, RestoreResult)
        assert restored.files == 5
    finally:
        stub_terminate(control_plane)
        await second.kill()


async def test_async_auto_restore_failure_terminates_the_sandbox(
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
    fake_persistence: FakePersistence,
) -> None:
    fake_persistence.seed(BUCKET, "rayito/broken")
    fake_persistence.fail_after_started = ("internal", "archive checksum mismatch")
    stub_launch(control_plane, fake_rayd)
    stub_terminate(control_plane)
    with pytest.raises(PersistenceException) as excinfo:
        await AsyncSandbox.create(
            IMAGE_ARN,
            idle=None,
            access_token=ACCESS_TOKEN,
            execution_role_arn=ROLE,
            persist=S3Prefix(BUCKET, name="broken"),
            control_plane=control_plane.plane,
            transport=fake_rayd.transport,
        )
    assert excinfo.value.code == "internal"


async def test_async_connect_binds_without_restoring_and_requires_a_name(
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
    fake_persistence: FakePersistence,
) -> None:
    with pytest.raises(InvalidArgumentException, match="name"):
        await AsyncSandbox.connect(
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
    handle = await AsyncSandbox.connect(
        SANDBOX_ID,
        access_token=ACCESS_TOKEN,
        persist=S3Prefix(BUCKET, name="named"),
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    )
    try:
        assert handle.persist == S3Prefix(BUCKET, name="named")
        assert fake_persistence.restore_requests == []
        with pytest.raises(InvalidArgumentException, match="create"):
            await handle.reincarnate()
    finally:
        await handle.close()


async def test_async_checkpoint_returns_the_done_fields_and_reports_progress(
    sandbox: AsyncSandbox, fake_persistence: FakePersistence
) -> None:
    seen: list[CheckpointProgress] = []
    result = await sandbox.checkpoint_files(exclude=["skipme"], on_progress=seen.append)
    assert isinstance(result, CheckpointResult)
    assert result.files == 4
    assert result.key_prefix == f"rayito/{SANDBOX_ID}"
    assert [progress.files_done for progress in seen] == [1, 2]
    assert list(fake_persistence.checkpoint_requests[-1].exclude) == ["skipme"]
    with pytest.raises(InvalidArgumentException):
        await sandbox.checkpoint_files(exclude=["../x"])


async def test_async_status_and_stream_errors(
    sandbox: AsyncSandbox, fake_persistence: FakePersistence
) -> None:
    fake_persistence.has_credentials = False
    with pytest.raises(PersistenceException) as denied:
        await sandbox.checkpoint_files()
    assert denied.value.code == "permission_denied"
    fake_persistence.has_credentials = True
    fake_persistence.busy = True
    with pytest.raises(PersistenceException) as busy:
        await sandbox.checkpoint_files()
    assert busy.value.code == "failed_precondition"
    fake_persistence.busy = False
    fake_persistence.unimplemented = True
    with pytest.raises(PersistenceException) as old:
        await sandbox.restore_files()
    assert old.value.code == "unimplemented" and str(old.value) == UNIMPLEMENTED_MESSAGE
    fake_persistence.unimplemented = False
    fake_persistence.fail_after_started = ("suspending", "suspending")
    with pytest.raises(PersistenceException) as cut:
        await sandbox.checkpoint_files()
    assert cut.value.code == "interrupted"
    fake_persistence.fail_after_started = None
    with pytest.raises(NotFoundException):
        await sandbox.restore_files(source=S3Prefix(BUCKET, name="never"))


async def test_async_proxy_403_before_the_first_message_remints_once(
    sandbox: AsyncSandbox,
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

    class FailingCall:
        async def read(self) -> Any:
            raise failures.pop(0)

        def cancel(self) -> None:
            pass

    def checkpoint(request: Any, timeout: float | None = None) -> Any:
        if failures:
            return FailingCall()
        return real(request, timeout=timeout)

    monkeypatch.setattr(stub, "Checkpoint", checkpoint)
    result = await sandbox.checkpoint_files()
    assert result.files == 4
    assert len(fake_persistence.checkpoint_requests) == 1


async def test_async_restore_reports_progress(
    sandbox: AsyncSandbox, fake_persistence: FakePersistence
) -> None:
    fake_persistence.seed(BUCKET, f"rayito/{SANDBOX_ID}", files=7)
    seen: list[RestoreProgress] = []
    result = await sandbox.restore_files(on_progress=seen.append)
    assert result.files == 7
    assert [progress.files_done for progress in seen] == [1, 2]


async def test_async_reincarnate_orders_checkpoint_create_restore_and_kill(
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
    fake_persistence: FakePersistence,
) -> None:
    original = await create(control_plane, fake_rayd, persist=S3Prefix(BUCKET))
    stub_launch(control_plane, fake_rayd, sandbox_id=SUCCESSOR_ID)
    stub_terminate(control_plane, SANDBOX_ID)
    successor = await original.reincarnate()
    try:
        assert successor.sandbox_id == SUCCESSOR_ID
        assert successor.persist == original.persist
        assert isinstance(successor.last_restore, RestoreResult)
        assert original._closed is True
        assert fake_persistence.restore_requests[-1].source.key_prefix == f"rayito/{SANDBOX_ID}"
    finally:
        stub_terminate(control_plane, SUCCESSOR_ID)
        await successor.kill()


async def test_async_reincarnate_leaves_the_old_sandbox_alive_when_create_fails(
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
    fake_persistence: FakePersistence,
) -> None:
    original = await create(control_plane, fake_rayd, persist=S3Prefix(BUCKET))
    control_plane.microvms.add_client_error(
        "run_microvm", service_error_code="ServiceQuotaExceededException", http_status_code=402
    )
    try:
        with pytest.raises(Exception) as excinfo:
            await original.reincarnate()
        assert any("reincarnate()" in note for note in getattr(excinfo.value, "__notes__", []))
        assert original._closed is False
        assert fake_persistence.stored(BUCKET, f"rayito/{SANDBOX_ID}") is not None
    finally:
        stub_terminate(control_plane)
        await original.kill()
