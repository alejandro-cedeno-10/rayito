"""`create(events=...)` wiring (m15-events-webhooks, ADR-020): `plan_features`
keeps the validated `LifecycleEvents` in `FeaturePlan.events`,
`planned_sections` turns it into a `LifecycleEventsSectionFactory` once the
`run-microvm` facts exist, and `_apply_configure_sections` (sync and async)
sends `k_sbx` in the same single `Configure` as every other 0.6 section —
terminating the VM when the agent lacks the flag, the stack is not deployed
or the section comes back `INVALID`.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any, cast

import pytest

import rayito.sandbox_async.main as async_main
import rayito.sandbox_sync.main as sync_main
from rayito import AsyncLifecycleEvents, AsyncSandbox, LifecycleEvents, Sandbox
from rayito._configure_base import AgentFeatures
from rayito._feature_options import FeatureOptions, LaunchFacts, plan_features, planned_sections
from rayito._lifecycle_events._keys import derive_sandbox_key
from rayito._lifecycle_events._section import (
    LifecycleEventsSection,
    LifecycleEventsSectionFactory,
)
from rayito._stacks._service import OptionalStacks
from rayito._telemetry_export import TelemetryExport, TelemetrySectionFactory
from rayito.exceptions import SandboxException, UnimplementedError, WebhookException
from rayito.v1 import configure_pb2

from .fake_lifecycle_events_aws import FakeAwsSession, FakeSecretsClient, FakeTable
from .fake_stacks import FakeStackProvisioner

STACK_KEY = b"the-stack-wide-hmac-secret"
SECRET_ID = "arn:aws:secretsmanager:us-east-1:123456789012:secret:stack-key-abc123"
SANDBOX_ID = "mvm-test-events"
IMAGE_ARN = "arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base"
IMAGE_VERSION = "7.0"
FACTS = LaunchFacts(
    sandbox_id=SANDBOX_ID, image_arn=IMAGE_ARN, image_version=IMAGE_VERSION, guest_memory_bytes=None
)


def deployed_events(secrets: FakeSecretsClient | None = None) -> LifecycleEvents:
    client = secrets if secrets is not None else FakeSecretsClient(secrets={SECRET_ID: STACK_KEY})
    events = LifecycleEvents(
        region="us-east-1",
        session=FakeAwsSession(table=FakeTable(), secrets_client=client),
        stacks=OptionalStacks(provisioner=FakeStackProvisioner()),
    )
    events._stack_key_secret_id = SECRET_ID
    return events


def undeployed_events() -> LifecycleEvents:
    return LifecycleEvents(
        region="us-east-1",
        session=FakeAwsSession(table=FakeTable(), secrets_client=FakeSecretsClient()),
        stacks=OptionalStacks(provisioner=FakeStackProvisioner()),
    )


# ---------------------------------------------------------------- planning


def test_a_valid_events_option_is_planned_not_sent_yet() -> None:
    events = deployed_events()
    plan = plan_features(FeatureOptions(events=events), logging="cloudwatch")
    assert plan.events is events
    assert plan.configure_sections == ()


def test_an_async_events_option_plans_its_sync_delegate() -> None:
    events = AsyncLifecycleEvents()
    plan = plan_features(FeatureOptions(events=events), logging="cloudwatch")
    assert plan.events is events._sync_events


def test_planned_sections_add_the_events_factory_with_the_launch_facts() -> None:
    events = deployed_events()
    plan = plan_features(FeatureOptions(events=events), logging="cloudwatch")
    (factory,) = planned_sections(plan, FACTS)
    assert factory == LifecycleEventsSectionFactory(
        events, sandbox_id=SANDBOX_ID, image_arn=IMAGE_ARN, image_version=IMAGE_VERSION
    )


def test_planned_sections_keep_telemetry_and_events_together_in_order() -> None:
    plan = plan_features(
        FeatureOptions(events=deployed_events(), telemetry=TelemetryExport()),
        logging="cloudwatch",
    )
    kinds = [type(entry) for entry in planned_sections(plan, FACTS)]
    assert kinds == [TelemetrySectionFactory, LifecycleEventsSectionFactory]


def test_no_events_option_plans_no_events_section() -> None:
    assert planned_sections(plan_features(FeatureOptions()), FACTS) == ()


# ------------------------------------------------------------------ section


def test_the_factory_derives_k_sbx_from_the_stack_key_and_the_sandbox_id() -> None:
    factory = LifecycleEventsSectionFactory(
        deployed_events(), sandbox_id=SANDBOX_ID, image_arn=IMAGE_ARN, image_version=IMAGE_VERSION
    )
    section = factory(cast(Any, None))
    request = configure_pb2.ConfigureRequest()
    section.fill(request)
    config = request.lifecycle_events
    assert config.sandbox_key == derive_sandbox_key(STACK_KEY, SANDBOX_ID)
    assert config.sandbox_key != STACK_KEY
    assert (config.sandbox_id, config.image_arn, config.image_version) == (
        SANDBOX_ID,
        IMAGE_ARN,
        IMAGE_VERSION,
    )


def test_the_section_repr_never_shows_the_sandbox_key() -> None:
    key = derive_sandbox_key(STACK_KEY, SANDBOX_ID)
    section = LifecycleEventsSection(
        sandbox_key=key, sandbox_id=SANDBOX_ID, image_arn=IMAGE_ARN, image_version=IMAGE_VERSION
    )
    assert repr(key) not in repr(section)
    assert key.hex() not in repr(section)


def test_an_invalid_section_result_raises() -> None:
    section = LifecycleEventsSection(
        sandbox_key=b"k", sandbox_id=SANDBOX_ID, image_arn=IMAGE_ARN, image_version=IMAGE_VERSION
    )
    with pytest.raises(SandboxException, match="lifecycle_events: invalid_section"):
        section.check_result(configure_pb2.SECTION_CODE_INVALID, "invalid_section")


def test_the_stack_key_is_read_once_per_lifecycle_events_instance() -> None:
    secrets = FakeSecretsClient(secrets={SECRET_ID: STACK_KEY})
    events = deployed_events(secrets)
    for sandbox_id in ("a", "b"):
        LifecycleEventsSectionFactory(
            events, sandbox_id=sandbox_id, image_arn=IMAGE_ARN, image_version=IMAGE_VERSION
        )(cast(Any, None))
    assert len(secrets.calls) == 1


# ----------------------------------------------------------- sync: apply


def bare_sync_sandbox(features: AgentFeatures) -> Sandbox:
    sandbox = Sandbox.__new__(Sandbox)
    sandbox._agent_features = features
    sandbox._secrets = cast(Any, SimpleNamespace(cache=object()))
    sandbox._control_plane = cast(Any, object())
    sandbox._configure = object()
    sandbox._logger = logging.getLogger("test.m15.events")
    sandbox._info = cast(Any, SimpleNamespace(sandbox_id=SANDBOX_ID))
    sandbox._section_handles = {}
    return sandbox


def bare_async_sandbox(features: AgentFeatures) -> AsyncSandbox:
    sandbox = AsyncSandbox.__new__(AsyncSandbox)
    sandbox._agent_features = features
    sandbox._secrets = cast(Any, SimpleNamespace(cache=object()))
    sandbox._control_plane = cast(Any, object())
    sandbox._configure = object()
    sandbox._logger = logging.getLogger("test.m15.events")
    sandbox._info = cast(Any, SimpleNamespace(sandbox_id=SANDBOX_ID))
    sandbox._section_handles = {}
    return sandbox


EVENTS_AGENT = AgentFeatures(configure=True, lifecycle_events=True)


def response(code: int, error_class: str = "") -> configure_pb2.ConfigureResponse:
    result = configure_pb2.ConfigureResponse()
    result.results.add(
        section=configure_pb2.CONFIG_SECTION_LIFECYCLE_EVENTS, code=code, error_class=error_class
    )
    return result


def factory_for(events: LifecycleEvents) -> tuple[LifecycleEventsSectionFactory, ...]:
    return (
        LifecycleEventsSectionFactory(
            events, sandbox_id=SANDBOX_ID, image_arn=IMAGE_ARN, image_version=IMAGE_VERSION
        ),
    )


def test_sync_apply_sends_k_sbx_in_a_single_configure(monkeypatch: pytest.MonkeyPatch) -> None:
    sandbox = bare_sync_sandbox(EVENTS_AGENT)
    sent: list[configure_pb2.ConfigureRequest] = []

    def record(stub: object, request: configure_pb2.ConfigureRequest, **_: object) -> Any:
        sent.append(request)
        return response(configure_pb2.SECTION_CODE_APPLIED)

    monkeypatch.setattr(sync_main, "call_configure", record)
    sandbox._apply_configure_sections(
        factory_for(deployed_events()), timeout=5.0, terminate_on_failure=True
    )
    assert len(sent) == 1
    assert sent[0].lifecycle_events.sandbox_key == derive_sandbox_key(STACK_KEY, SANDBOX_ID)


@pytest.mark.parametrize(
    ("features", "events", "configure_code", "error"),
    [
        (AgentFeatures(configure=True), "deployed", None, UnimplementedError),
        (None, "deployed", None, UnimplementedError),
        (EVENTS_AGENT, "undeployed", None, WebhookException),
        (EVENTS_AGENT, "deployed", configure_pb2.SECTION_CODE_INVALID, SandboxException),
    ],
    ids=["no-flag", "pre-0.6-agent", "stack-not-deployed", "invalid-section"],
)
def test_sync_apply_terminates_the_vm_on_any_events_failure(
    monkeypatch: pytest.MonkeyPatch,
    features: AgentFeatures | None,
    events: str,
    configure_code: int | None,
    error: type[Exception],
) -> None:
    sandbox = bare_sync_sandbox(cast(AgentFeatures, features))
    closed: list[None] = []
    terminated: list[str] = []
    sandbox.close = lambda: closed.append(None)  # type: ignore[method-assign]
    monkeypatch.setattr(
        sync_main,
        "terminate_quietly",
        lambda _plane, sandbox_id, _log: terminated.append(sandbox_id),
    )
    monkeypatch.setattr(
        sync_main,
        "call_configure",
        lambda *_a, **_k: response(cast(int, configure_code), "invalid_section"),
    )
    source = deployed_events() if events == "deployed" else undeployed_events()
    with pytest.raises(error):
        sandbox._apply_configure_sections(
            factory_for(source), timeout=5.0, terminate_on_failure=True
        )
    assert closed == [None]
    assert terminated == [SANDBOX_ID]


# ---------------------------------------------------------- async: apply


@pytest.mark.asyncio
async def test_async_apply_sends_k_sbx_in_a_single_configure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = bare_async_sandbox(EVENTS_AGENT)
    sent: list[configure_pb2.ConfigureRequest] = []

    async def record(stub: object, request: configure_pb2.ConfigureRequest, **_: object) -> Any:
        sent.append(request)
        return response(configure_pb2.SECTION_CODE_APPLIED)

    monkeypatch.setattr(async_main, "call_configure", record)
    events = AsyncLifecycleEvents(
        region="us-east-1",
        session=cast(
            Any,
            FakeAwsSession(
                table=FakeTable(), secrets_client=FakeSecretsClient(secrets={SECRET_ID: STACK_KEY})
            ),
        ),
        stacks=OptionalStacks(provisioner=FakeStackProvisioner()),
    )
    events._sync_events._stack_key_secret_id = SECRET_ID
    await sandbox._apply_configure_sections(
        factory_for(events._sync_events), timeout=5.0, terminate_on_failure=True
    )
    assert len(sent) == 1
    assert sent[0].lifecycle_events.sandbox_key == derive_sandbox_key(STACK_KEY, SANDBOX_ID)


@pytest.mark.asyncio
async def test_async_apply_terminates_the_vm_when_the_stack_is_not_deployed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = bare_async_sandbox(EVENTS_AGENT)
    closed: list[None] = []
    terminated: list[str] = []

    async def fake_close() -> None:
        closed.append(None)

    async def fake_to_thread(fn: Any, *args: Any, **kwargs: Any) -> Any:
        return fn(*args, **kwargs)

    sandbox.close = fake_close  # type: ignore[method-assign]
    monkeypatch.setattr(
        async_main,
        "terminate_quietly",
        lambda _plane, sandbox_id, _log: terminated.append(sandbox_id),
    )
    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    with pytest.raises(WebhookException, match="deploy"):
        await sandbox._apply_configure_sections(
            factory_for(undeployed_events()), timeout=5.0, terminate_on_failure=True
        )
    assert closed == [None]
    assert terminated == [SANDBOX_ID]
