"""`reincarnate()` replays every 0.6 `ConfigureSandbox` section the original
`create()` sent (m15-reincarnate-configure-replay): the successor goes through
the very same `create()` path (`plan_features` → `planned_sections` →
`_apply_configure_sections`), so `events=` derives `k_sbx` from the *new*
`sandbox_id`, `mounts=` waits until `mounted` again and `telemetry=` is
re-resolved with the successor's launch facts — all in one `Configure`.

Real gRPC against the fake `rayd` and the Stubber control plane, with only
the `Configure`/`ConfigureStatus` adapters and the agent's feature gate
replaced by spies (the fake `rayd` serves no `ConfigureService`).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

import rayito.sandbox_async.main as async_main
import rayito.sandbox_sync.main as sync_main
from rayito import AsyncLifecycleEvents, AsyncSandbox, LifecycleEvents, S3Prefix, Sandbox
from rayito._configure_base import AgentFeatures
from rayito._feature_options import FeatureOptions, relaunch_features
from rayito._lifecycle_events._keys import derive_sandbox_key
from rayito._persistence_base import launch_kwargs
from rayito._s3_mounts import S3Mount
from rayito._stacks._service import OptionalStacks
from rayito._telemetry_export import TelemetryExport
from rayito.v1 import configure_pb2, s3_mounts_pb2

from .conftest import ACCESS_TOKEN, IMAGE_ARN, SANDBOX_ID, RaydEndpoint, StubbedControlPlane
from .fake_lifecycle_events_aws import FakeAwsSession, FakeSecretsClient, FakeTable
from .fake_stacks import FakeStackProvisioner
from .test_persistence_sync import BUCKET, ROLE, stub_launch, stub_terminate

SUCCESSOR_ID = "microvm-00000000-0000-0000-0000-000000000009"
STACK_KEY = b"the-stack-wide-hmac-secret"
SECRET_ID = "arn:aws:secretsmanager:us-east-1:123456789012:secret:stack-key-abc123"
MOUNT_PATH = "/mnt/data"
MOUNTS = {MOUNT_PATH: S3Mount(bucket="data-bucket")}
ALL_FEATURES = AgentFeatures(
    configure=True, s3_mounts=True, lifecycle_events=True, telemetry_export=True
)
REPLAYED_SECTIONS = (
    configure_pb2.CONFIG_SECTION_S3_MOUNTS,
    configure_pb2.CONFIG_SECTION_TELEMETRY_EXPORT,
    configure_pb2.CONFIG_SECTION_LIFECYCLE_EVENTS,
)


def mount_status(phase: s3_mounts_pb2.S3MountPhase) -> configure_pb2.ConfigureStatusResponse:
    return configure_pb2.ConfigureStatusResponse(
        s3_mounts=s3_mounts_pb2.S3MountsStatus(
            mounts=[s3_mounts_pb2.S3MountState(mount_path=MOUNT_PATH, phase=phase)]
        )
    )


@dataclass
class ConfigureSpy:
    """Every `Configure` request in order; mounts come back `PENDING` and
    each sandbox's first `ConfigureStatus` still reports them `PENDING`, the
    second `MOUNTED`, so `create()` can only return after polling twice."""

    requests: list[configure_pb2.ConfigureRequest] = field(default_factory=list)
    status_calls: int = 0

    def configure(self, request: configure_pb2.ConfigureRequest) -> Any:
        self.requests.append(request)
        response = configure_pb2.ConfigureResponse()
        for section in REPLAYED_SECTIONS:
            code = (
                configure_pb2.SECTION_CODE_PENDING
                if section == configure_pb2.CONFIG_SECTION_S3_MOUNTS
                else configure_pb2.SECTION_CODE_APPLIED
            )
            response.results.add(section=section, code=code)
        return response

    def status(self) -> configure_pb2.ConfigureStatusResponse:
        self.status_calls += 1
        settled = self.status_calls % 2 == 0
        return mount_status(
            s3_mounts_pb2.S3_MOUNT_PHASE_MOUNTED
            if settled
            else s3_mounts_pb2.S3_MOUNT_PHASE_PENDING
        )


def deployed_events() -> LifecycleEvents:
    events = LifecycleEvents(
        region="us-east-1",
        session=FakeAwsSession(
            table=FakeTable(), secrets_client=FakeSecretsClient(secrets={SECRET_ID: STACK_KEY})
        ),
        stacks=OptionalStacks(provisioner=FakeStackProvisioner()),
    )
    events._stack_key_secret_id = SECRET_ID
    return events


def deployed_async_events() -> AsyncLifecycleEvents:
    events = AsyncLifecycleEvents(
        region="us-east-1",
        session=FakeAwsSession(
            table=FakeTable(), secrets_client=FakeSecretsClient(secrets={SECRET_ID: STACK_KEY})
        ),
        stacks=OptionalStacks(provisioner=FakeStackProvisioner()),
    )
    events._sync_events._stack_key_secret_id = SECRET_ID
    return events


def launch_options(events: object) -> dict[str, Any]:
    return {
        "idle": None,
        "access_token": ACCESS_TOKEN,
        "execution_role_arn": ROLE,
        "logging": "cloudwatch",
        "persist": S3Prefix(BUCKET),
        "mounts": MOUNTS,
        "telemetry": TelemetryExport(),
        "events": events,
    }


def assert_every_section_replayed(spy: ConfigureSpy) -> None:
    original, successor = spy.requests
    for request in (original, successor):
        assert request.HasField("s3_mounts")
        assert request.HasField("telemetry_export")
        assert request.HasField("lifecycle_events")
    assert [m.mount_path for m in successor.s3_mounts.mounts] == [MOUNT_PATH]
    assert original.lifecycle_events.sandbox_id == SANDBOX_ID
    assert successor.lifecycle_events.sandbox_id == SUCCESSOR_ID
    assert successor.lifecycle_events.sandbox_key == derive_sandbox_key(STACK_KEY, SUCCESSOR_ID)
    assert successor.lifecycle_events.sandbox_key != original.lifecycle_events.sandbox_key
    assert successor.telemetry_export.image_arn == original.telemetry_export.image_arn
    # Two `ConfigureStatus` per sandbox: the mount was awaited until `mounted`
    # in the successor too, before `reincarnate()` returned.
    assert spy.status_calls == 4


@pytest.fixture
def no_settle_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync_main, "CONFIGURE_SETTLE_POLL_S", 0.0)
    monkeypatch.setattr(async_main, "CONFIGURE_SETTLE_POLL_S", 0.0)


@pytest.fixture
def sync_spy(monkeypatch: pytest.MonkeyPatch, no_settle_sleep: None) -> Iterator[ConfigureSpy]:
    spy = ConfigureSpy()
    monkeypatch.setattr(sync_main, "require_configure_support", lambda *_: ALL_FEATURES)
    monkeypatch.setattr(sync_main, "call_configure", lambda _stub, req, **_: spy.configure(req))
    monkeypatch.setattr(sync_main, "call_configure_status", lambda *_a, **_k: spy.status())
    yield spy


@pytest.fixture
def async_spy(monkeypatch: pytest.MonkeyPatch, no_settle_sleep: None) -> Iterator[ConfigureSpy]:
    spy = ConfigureSpy()

    async def configure(_stub: object, request: configure_pb2.ConfigureRequest, **_: Any) -> Any:
        return spy.configure(request)

    async def status(*_args: Any, **_kwargs: Any) -> configure_pb2.ConfigureStatusResponse:
        return spy.status()

    monkeypatch.setattr(async_main, "require_configure_support", lambda *_: ALL_FEATURES)
    monkeypatch.setattr(async_main, "call_configure", configure)
    monkeypatch.setattr(async_main, "call_configure_status", status)
    yield spy


def test_sync_reincarnate_replays_mounts_telemetry_and_events(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, sync_spy: ConfigureSpy
) -> None:
    stub_launch(control_plane, fake_rayd)
    original = Sandbox.create(
        IMAGE_ARN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
        **launch_options(deployed_events()),
    )
    stub_launch(control_plane, fake_rayd, sandbox_id=SUCCESSOR_ID)
    stub_terminate(control_plane, SANDBOX_ID)
    successor = original.reincarnate()
    try:
        assert successor.sandbox_id == SUCCESSOR_ID
        assert_every_section_replayed(sync_spy)
    finally:
        stub_terminate(control_plane, SUCCESSOR_ID)
        successor.kill()


@pytest.mark.asyncio
async def test_async_reincarnate_replays_mounts_telemetry_and_events(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, async_spy: ConfigureSpy
) -> None:
    stub_launch(control_plane, fake_rayd)
    original = await AsyncSandbox.create(
        IMAGE_ARN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
        **launch_options(deployed_async_events()),
    )
    stub_launch(control_plane, fake_rayd, sandbox_id=SUCCESSOR_ID)
    stub_terminate(control_plane, SANDBOX_ID)
    successor = await original.reincarnate()
    try:
        assert successor.sandbox_id == SUCCESSOR_ID
        assert_every_section_replayed(async_spy)
    finally:
        stub_terminate(control_plane, SUCCESSOR_ID)
        await successor.kill()


def test_a_sandbox_without_06_options_sends_no_configure_on_reincarnate(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, sync_spy: ConfigureSpy
) -> None:
    stub_launch(control_plane, fake_rayd)
    original = Sandbox.create(
        IMAGE_ARN,
        idle=None,
        access_token=ACCESS_TOKEN,
        execution_role_arn=ROLE,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
        persist=S3Prefix(BUCKET),
    )
    stub_launch(control_plane, fake_rayd, sandbox_id=SUCCESSOR_ID)
    stub_terminate(control_plane, SANDBOX_ID)
    successor = original.reincarnate()
    try:
        assert sync_spy.requests == []
    finally:
        stub_terminate(control_plane, SUCCESSOR_ID)
        successor.kill()


# ------------------------------------------------------------ pure helpers


def test_relaunch_features_keep_every_section_and_drop_the_size() -> None:
    gateways: dict[str, Any] = {"anthropic": object()}
    events = object()
    telemetry = object()
    original = FeatureOptions(
        mounts=MOUNTS, size="2gb", events=events, telemetry=telemetry, gateways=gateways
    )
    kept = relaunch_features(original)
    assert kept.mounts == MOUNTS and kept.mounts is not MOUNTS
    assert kept.gateways == gateways and kept.gateways is not gateways
    assert kept.events is events
    assert kept.telemetry is telemetry
    # Already inside the resolved template ARN; size + ARN is rejected.
    assert kept.size is None


def test_launch_kwargs_forward_every_feature_option_to_create() -> None:
    from rayito._models import LaunchOptions

    features = relaunch_features(FeatureOptions(mounts=MOUNTS, events="e", telemetry="t"))
    options = LaunchOptions(
        template="arn",
        template_version=None,
        timeout=1800,
        idle=None,
        envs=None,
        metadata=None,
        cpu_time_limit=None,
        execution_role_arn=None,
        allowed_ports=None,
        ingress=None,
        egress=None,
        logging="cloudwatch",
        access_token=None,
        ready_timeout=90.0,
        request_timeout=60.0,
        reconnect_timeout=60.0,
        keep_on_failure=False,
        control_plane="plane",
        transport="transport",
        features=features,
    )
    kwargs = launch_kwargs(options)
    assert kwargs["mounts"] == MOUNTS
    assert (kwargs["events"], kwargs["telemetry"]) == ("e", "t")
    assert kwargs["size"] is None
    assert {"volumes", "gateways", "domain"} <= kwargs.keys()
