"""Paridad async de `test_metadata_sync.py`: `AsyncSandbox.create(metadata=)`,
`metadata`, `get_info()` en sus dos variantes y `list(metadata=)` con la
sonda `grpc.aio` por sandbox."""

from __future__ import annotations

import json
from typing import Any

import pytest

from rayito import AsyncSandbox
from rayito.exceptions import InvalidArgumentException, SandboxException

from .conftest import (
    ACCESS_TOKEN,
    IMAGE_ARN,
    SANDBOX_ID,
    FakeRaydFactory,
    RaydEndpoint,
    StubbedControlPlane,
    TrackingTransport,
    auth_token_response,
    list_item,
    microvm_response,
    stub_metadata_probe,
)

METADATA = {"env": "ci", "run": "42"}


def capture_launch(control_plane: StubbedControlPlane, endpoint: RaydEndpoint) -> dict[str, Any]:
    captured: dict[str, Any] = {}
    control_plane.microvms.add_response("run_microvm", microvm_response(endpoint=endpoint.host))
    control_plane.microvms.add_response("create_microvm_auth_token", auth_token_response())
    original = control_plane.plane.run_microvm

    def spy(request: Any) -> Any:
        captured.update(request.to_api())
        return original(request)

    control_plane.plane.run_microvm = spy  # type: ignore[method-assign]
    return captured


async def test_async_create_puts_metadata_in_the_payload_and_reads_it_back(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.metadata = dict(METADATA)
    captured = capture_launch(control_plane, fake_rayd)
    control_plane.microvms.add_response(
        "get_microvm", microvm_response(endpoint=fake_rayd.host, state="RUNNING")
    )
    control_plane.microvms.add_response("terminate_microvm", {})
    async with await AsyncSandbox.create(
        IMAGE_ARN,
        idle=None,
        metadata=METADATA,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    ) as sandbox:
        assert json.loads(str(captured["runHookPayload"]))["metadata"] == METADATA
        assert sandbox.metadata == METADATA
        assert (await sandbox.get_health()).metadata == METADATA
        assert (await sandbox.get_info()).metadata == METADATA


async def test_async_connect_populates_metadata(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.metadata = dict(METADATA)
    control_plane.microvms.add_response(
        "get_microvm", microvm_response(endpoint=fake_rayd.host, state="RUNNING")
    )
    control_plane.microvms.add_response("create_microvm_auth_token", auth_token_response())
    sandbox = await AsyncSandbox.connect(
        SANDBOX_ID,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    )
    try:
        assert sandbox.metadata == METADATA
    finally:
        await sandbox.close()


async def test_async_class_get_info_probes_only_running(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.metadata = {"a": "1"}
    transport = TrackingTransport.for_loopback()
    stub_metadata_probe(control_plane, SANDBOX_ID, fake_rayd)
    info = await AsyncSandbox.get_info(
        SANDBOX_ID, control_plane=control_plane.plane, transport=transport
    )
    assert info.metadata == {"a": "1"}
    assert transport.open_count == 1 and transport.all_closed

    stub_metadata_probe(control_plane, SANDBOX_ID, fake_rayd, state="SUSPENDED")
    paused = await AsyncSandbox.get_info(
        SANDBOX_ID, control_plane=control_plane.plane, transport=transport
    )
    assert paused.metadata is None
    assert transport.open_count == 1

    control_plane.microvms.add_response("get_microvm", microvm_response(state="RUNNING"))
    plain = await AsyncSandbox.get_info(
        SANDBOX_ID, read_metadata=False, control_plane=control_plane.plane
    )
    assert plain.metadata is None


async def test_async_list_by_metadata_probes_running_sandboxes(
    control_plane: StubbedControlPlane, fake_rayd_factory: FakeRaydFactory
) -> None:
    first = fake_rayd_factory({"env": "ci", "run": "1"})
    second = fake_rayd_factory({"env": "ci", "run": "2"})
    booting = fake_rayd_factory({"env": "ci", "run": "2"})
    booting.servicer.not_ready_calls = 1
    transport = TrackingTransport.for_loopback()
    control_plane.microvms.add_response(
        "list_microvms",
        {
            "items": [
                list_item("a", "RUNNING"),
                list_item("b", "RUNNING"),
                list_item("c", "RUNNING"),
                list_item("d", "RUNNING"),
                list_item("e", "RUNNING"),
            ]
        },
    )
    stub_metadata_probe(control_plane, "a", first)
    stub_metadata_probe(control_plane, "b", second)
    stub_metadata_probe(control_plane, "c", None, state="SUSPENDING")
    control_plane.microvms.add_client_error(
        "get_microvm", service_error_code="ResourceNotFoundException", http_status_code=404
    )
    stub_metadata_probe(control_plane, "e", booting)

    listed = await AsyncSandbox.list(
        metadata={"run": "2"}, control_plane=control_plane.plane, transport=transport
    )
    assert [item.sandbox_id for item in listed] == ["b"]
    assert listed[0].metadata == {"env": "ci", "run": "2"}
    assert transport.open_count == 3 and transport.all_closed


async def test_async_list_by_metadata_raises_on_a_failing_health(
    control_plane: StubbedControlPlane, fake_rayd_factory: FakeRaydFactory
) -> None:
    broken = fake_rayd_factory({"env": "ci"})
    broken.servicer.unavailable_calls = 10
    transport = TrackingTransport.for_loopback()
    control_plane.microvms.add_response("list_microvms", {"items": [list_item("a", "RUNNING")]})
    stub_metadata_probe(control_plane, "a", broken)
    with pytest.raises(SandboxException, match="sandbox a"):
        await AsyncSandbox.list(
            metadata={"env": "ci"},
            control_plane=control_plane.plane,
            transport=transport,
            request_timeout=0.5,
        )
    assert transport.all_closed


async def test_async_list_by_metadata_refuses_suspended_states(
    control_plane: StubbedControlPlane,
) -> None:
    with pytest.raises(InvalidArgumentException, match="RUNNING"):
        await AsyncSandbox.list(
            metadata={"a": "1"}, states=["SUSPENDED"], control_plane=control_plane.plane
        )


async def test_async_list_without_metadata_is_unchanged(
    control_plane: StubbedControlPlane,
) -> None:
    control_plane.microvms.add_response("list_microvms", {"items": [list_item("x", "RUNNING")]})
    listed = await AsyncSandbox.list(control_plane=control_plane.plane)
    assert [item.sandbox_id for item in listed] == ["x"]
    assert listed[0].metadata is None
