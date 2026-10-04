"""`Sandbox.create(volumes=...)` / `AsyncSandbox.create(volumes=...)` de punta
a punta (`m15-efs-volumes`): la IP de mount target se resuelve antes de
`run-microvm` (una `DescribeMountTargets` por sistema de ficheros), la sección
`efs_volumes` viaja en el único `Configure` de `create()` con un plazo que
cubre el montaje de `rayd`, un fallo termina el sandbox (salvo
`keep_on_failure`) y `reincarnate()` la vuelve a mandar.

gRPC real contra el `rayd` falso y el plano de control con Stubber; sólo los
adaptadores `Configure`/`ConfigureStatus`, la puerta de funciones del agente y
el cliente `efs` son espías (el `rayd` falso no sirve `ConfigureService`).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

import rayito._feature_options as feature_options
import rayito.sandbox_async.main as async_main
import rayito.sandbox_sync.main as sync_main
from rayito import AsyncSandbox, EfsVolume, S3Mount, S3Prefix, Sandbox, VolumeMountException
from rayito._configure_base import AgentFeatures
from rayito._volumes._section import VOLUME_APPLY_TIMEOUT_SECONDS
from rayito.exceptions import UnimplementedError, VolumeException
from rayito.v1 import configure_pb2, efs_volumes_pb2

from .conftest import ACCESS_TOKEN, IMAGE_ARN, SANDBOX_ID, RaydEndpoint, StubbedControlPlane
from .test_persistence_sync import BUCKET, ROLE, stub_launch, stub_terminate

SUCCESSOR_ID = "microvm-00000000-0000-0000-0000-000000000009"
#: Marcador de documentación (cuenta ficticia).
CONNECTOR = "arn:aws:lambda:us-east-1:123456789012:network-connector:rayito-efs"
VOLUME = EfsVolume(file_system_id="fs-0123abcd", access_point_id="fsap-0123abcd")
READ_ONLY = EfsVolume(file_system_id="fs-0123abcd", access_point_id="fsap-0456abcd", read_only=True)
PINNED = EfsVolume(
    file_system_id="fs-0789abcd", access_point_id="fsap-0789abcd", mount_target_ip="10.0.9.9"
)
MOUNT_TARGET_IP = "10.0.1.10"
EFS_FEATURES = AgentFeatures(configure=True, s3_mounts=True, efs_volumes=True)


@dataclass
class FakeEfs:
    calls: list[dict[str, Any]] = field(default_factory=list)

    def describe_mount_targets(self, **params: Any) -> dict[str, Any]:
        self.calls.append(params)
        return {
            "MountTargets": [
                {
                    "MountTargetId": "fsmt-1",
                    "LifeCycleState": "available",
                    "IpAddress": MOUNT_TARGET_IP,
                    "AvailabilityZoneId": "use1-az1",
                }
            ]
        }


@dataclass
class ConfigureSpy:
    """Each `Configure` with its deadline; `efs_volumes` answers `code`
    (and `s3_mounts`, when present, `APPLIED`)."""

    code: int = configure_pb2.SECTION_CODE_APPLIED
    error_class: str = ""
    requests: list[configure_pb2.ConfigureRequest] = field(default_factory=list)
    timeouts: list[float] = field(default_factory=list)

    def configure(self, request: configure_pb2.ConfigureRequest, timeout: float) -> Any:
        self.requests.append(request)
        self.timeouts.append(timeout)
        response = configure_pb2.ConfigureResponse()
        if request.HasField("efs_volumes"):
            response.results.add(
                section=configure_pb2.CONFIG_SECTION_EFS_VOLUMES,
                code=self.code,
                error_class=self.error_class,
            )
        if request.HasField("s3_mounts"):
            response.results.add(
                section=configure_pb2.CONFIG_SECTION_S3_MOUNTS,
                code=configure_pb2.SECTION_CODE_APPLIED,
            )
        return response

    def status(self) -> configure_pb2.ConfigureStatusResponse:
        mounted = efs_volumes_pb2.EFS_VOLUME_STATE_MOUNTED
        return configure_pb2.ConfigureStatusResponse(
            efs_volumes=efs_volumes_pb2.EfsVolumesStatus(
                volumes=[
                    efs_volumes_pb2.EfsVolumeStatus(mount_path=m.mount_path, state=mounted)
                    for m in (self.requests[-1].efs_volumes.mounts if self.requests else [])
                ]
            )
        )


@pytest.fixture
def efs(monkeypatch: pytest.MonkeyPatch) -> FakeEfs:
    fake = FakeEfs()

    class FakeLazyClient:
        def __init__(self, service: str, **_: Any) -> None:
            assert service == "efs"

        def get(self) -> FakeEfs:
            return fake

    monkeypatch.setattr(feature_options, "LazyClient", FakeLazyClient)
    return fake


@pytest.fixture
def features() -> dict[str, AgentFeatures]:
    return {"current": EFS_FEATURES}


@pytest.fixture
def sync_spy(
    monkeypatch: pytest.MonkeyPatch, features: dict[str, AgentFeatures]
) -> Iterator[ConfigureSpy]:
    spy = ConfigureSpy()
    monkeypatch.setattr(sync_main, "CONFIGURE_SETTLE_POLL_S", 0.0)
    monkeypatch.setattr(sync_main, "require_configure_support", lambda *_: features["current"])
    monkeypatch.setattr(
        sync_main, "call_configure", lambda _stub, req, *, timeout: spy.configure(req, timeout)
    )
    monkeypatch.setattr(sync_main, "call_configure_status", lambda *_a, **_k: spy.status())
    yield spy


@pytest.fixture
def async_spy(
    monkeypatch: pytest.MonkeyPatch, features: dict[str, AgentFeatures]
) -> Iterator[ConfigureSpy]:
    spy = ConfigureSpy()

    async def configure(_stub: object, request: Any, *, timeout: float) -> Any:
        return spy.configure(request, timeout)

    async def status(*_args: Any, **_kwargs: Any) -> configure_pb2.ConfigureStatusResponse:
        return spy.status()

    monkeypatch.setattr(async_main, "CONFIGURE_SETTLE_POLL_S", 0.0)
    monkeypatch.setattr(async_main, "require_configure_support", lambda *_: features["current"])
    monkeypatch.setattr(async_main, "call_configure", configure)
    monkeypatch.setattr(async_main, "call_configure_status", status)
    yield spy


def launch(**overrides: Any) -> dict[str, Any]:
    return {
        "idle": None,
        "access_token": ACCESS_TOKEN,
        "execution_role_arn": ROLE,
        "egress": [CONNECTOR],
        "volumes": {"/mnt/data": VOLUME, "/mnt/ro": READ_ONLY, "/home/user/p": PINNED},
        **overrides,
    }


def assert_volumes_sent(request: configure_pb2.ConfigureRequest) -> None:
    mounts = {m.mount_path: m for m in request.efs_volumes.mounts}
    assert set(mounts) == {"/mnt/data", "/mnt/ro", "/home/user/p"}
    assert mounts["/mnt/data"].mount_target_ip == MOUNT_TARGET_IP
    assert mounts["/mnt/ro"].mount_target_ip == MOUNT_TARGET_IP
    assert mounts["/mnt/ro"].read_only
    assert mounts["/home/user/p"].mount_target_ip == "10.0.9.9"


def test_create_sends_the_resolved_volumes_in_its_single_configure(
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
    sync_spy: ConfigureSpy,
    efs: FakeEfs,
) -> None:
    stub_launch(control_plane, fake_rayd)
    sandbox = Sandbox.create(
        IMAGE_ARN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
        mounts={"/mnt/s3": S3Mount(bucket="data-bucket")},
        **launch(),
    )
    try:
        assert efs.calls == [{"FileSystemId": "fs-0123abcd"}]
        (request,) = sync_spy.requests
        assert_volumes_sent(request)
        assert request.HasField("s3_mounts")
        assert sync_spy.timeouts == [VOLUME_APPLY_TIMEOUT_SECONDS]
        assert {path: s.state for path, s in sandbox.volumes.items()} == {
            "/mnt/data": "mounted",
            "/mnt/ro": "mounted",
            "/home/user/p": "mounted",
        }
    finally:
        stub_terminate(control_plane)
        sandbox.kill()


@pytest.mark.asyncio
async def test_async_create_sends_the_resolved_volumes_in_its_single_configure(
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
    async_spy: ConfigureSpy,
    efs: FakeEfs,
) -> None:
    stub_launch(control_plane, fake_rayd)
    sandbox = await AsyncSandbox.create(
        IMAGE_ARN, control_plane=control_plane.plane, transport=fake_rayd.transport, **launch()
    )
    try:
        assert efs.calls == [{"FileSystemId": "fs-0123abcd"}]
        (request,) = async_spy.requests
        assert_volumes_sent(request)
        assert async_spy.timeouts == [VOLUME_APPLY_TIMEOUT_SECONDS]
        assert (await sandbox.volumes())["/mnt/ro"].state == "mounted"
    finally:
        stub_terminate(control_plane)
        await sandbox.kill()


def test_pinned_ips_need_no_describe_mount_targets(
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
    sync_spy: ConfigureSpy,
    efs: FakeEfs,
) -> None:
    stub_launch(control_plane, fake_rayd)
    sandbox = Sandbox.create(
        IMAGE_ARN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
        **launch(volumes={"/mnt/p": PINNED}),
    )
    try:
        assert efs.calls == []
        assert len(sync_spy.requests) == 1
    finally:
        stub_terminate(control_plane)
        sandbox.kill()


def test_a_failed_mount_terminates_the_sandbox(
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
    sync_spy: ConfigureSpy,
    efs: FakeEfs,
) -> None:
    sync_spy.code = configure_pb2.SECTION_CODE_FAILED
    sync_spy.error_class = "iam_denied"
    stub_launch(control_plane, fake_rayd)
    stub_terminate(control_plane)
    with pytest.raises(VolumeMountException) as caught:
        Sandbox.create(
            IMAGE_ARN, control_plane=control_plane.plane, transport=fake_rayd.transport, **launch()
        )
    assert caught.value.code == "iam_denied"
    control_plane.microvms.assert_no_pending_responses()


def test_keep_on_failure_leaves_the_sandbox_running(
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
    sync_spy: ConfigureSpy,
    efs: FakeEfs,
) -> None:
    sync_spy.code = configure_pb2.SECTION_CODE_FAILED
    sync_spy.error_class = "network"
    stub_launch(control_plane, fake_rayd)
    with pytest.raises(VolumeMountException):
        Sandbox.create(
            IMAGE_ARN,
            control_plane=control_plane.plane,
            transport=fake_rayd.transport,
            keep_on_failure=True,
            **launch(),
        )
    control_plane.microvms.assert_no_pending_responses()


@pytest.mark.asyncio
async def test_an_image_without_efs_utils_terminates_with_unimplemented(
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
    async_spy: ConfigureSpy,
    efs: FakeEfs,
    features: dict[str, AgentFeatures],
) -> None:
    features["current"] = AgentFeatures(configure=True, s3_mounts=True)
    stub_launch(control_plane, fake_rayd)
    stub_terminate(control_plane)
    with pytest.raises(UnimplementedError, match="--with-efs"):
        await AsyncSandbox.create(
            IMAGE_ARN, control_plane=control_plane.plane, transport=fake_rayd.transport, **launch()
        )
    assert async_spy.requests == []
    control_plane.microvms.assert_no_pending_responses()


def test_a_file_system_without_mount_targets_fails_before_launching(
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
    sync_spy: ConfigureSpy,
    efs: FakeEfs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(efs, "describe_mount_targets", lambda **_: {"MountTargets": []})
    with pytest.raises(VolumeException, match="mount target"):
        Sandbox.create(
            IMAGE_ARN, control_plane=control_plane.plane, transport=fake_rayd.transport, **launch()
        )
    assert sync_spy.requests == []
    control_plane.microvms.assert_no_pending_responses()


def test_reincarnate_replays_the_volumes_section(
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
    sync_spy: ConfigureSpy,
    efs: FakeEfs,
) -> None:
    stub_launch(control_plane, fake_rayd)
    original = Sandbox.create(
        IMAGE_ARN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
        persist=S3Prefix(BUCKET),
        **launch(),
    )
    stub_launch(control_plane, fake_rayd, sandbox_id=SUCCESSOR_ID)
    stub_terminate(control_plane, SANDBOX_ID)
    successor = original.reincarnate()
    try:
        first, second = sync_spy.requests
        assert_volumes_sent(first)
        assert_volumes_sent(second)
        assert len(efs.calls) == 2
    finally:
        stub_terminate(control_plane, SUCCESSOR_ID)
        successor.kill()


def test_create_without_volumes_builds_no_efs_client(
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
    sync_spy: ConfigureSpy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("no efs client without volumes=")

    monkeypatch.setattr(feature_options, "LazyClient", refuse)
    stub_launch(control_plane, fake_rayd)
    sandbox = Sandbox.create(
        IMAGE_ARN,
        idle=None,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    )
    try:
        assert sync_spy.requests == []
    finally:
        stub_terminate(control_plane)
        sandbox.kill()
