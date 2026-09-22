"""Metadatos por sandbox (M6): `create(metadata=)` en el `runHookPayload`,
`sbx.metadata` desde `Health`, `get_info()` en sus dos variantes y
`Sandbox.list(metadata=)` con la sonda O(n) sobre sandboxes `RUNNING`."""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from rayito import Sandbox
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
    endpoint_of,
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


def test_create_puts_metadata_in_the_run_hook_payload(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.metadata = dict(METADATA)
    captured = capture_launch(control_plane, fake_rayd)
    control_plane.microvms.add_response("terminate_microvm", {})
    with Sandbox.create(
        IMAGE_ARN,
        idle=None,
        metadata={"run": "42", "env": "ci"},
        envs={"E": "1"},
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    ) as sandbox:
        payload = json.loads(str(captured["runHookPayload"]))
        assert payload["metadata"] == METADATA
        assert payload["envs"] == {"E": "1"}
        assert sandbox.metadata == METADATA
        assert sandbox.get_health().metadata == METADATA


def test_create_without_metadata_omits_the_key_and_reads_empty(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    captured = capture_launch(control_plane, fake_rayd)
    control_plane.microvms.add_response("terminate_microvm", {})
    with Sandbox.create(
        IMAGE_ARN,
        idle=None,
        metadata={},
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    ) as sandbox:
        assert "metadata" not in json.loads(str(captured["runHookPayload"]))
        assert sandbox.metadata == {}


def test_oversized_metadata_fails_before_any_aws_call(control_plane: StubbedControlPlane) -> None:
    with pytest.raises(InvalidArgumentException, match="metadata"):
        Sandbox.create(
            IMAGE_ARN,
            metadata={"k": "x" * 4096},
            access_token=ACCESS_TOKEN,
            control_plane=control_plane.plane,
        )


def test_instance_get_info_carries_metadata_without_extra_rpc(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.metadata = dict(METADATA)
    capture_launch(control_plane, fake_rayd)
    control_plane.microvms.add_response(
        "get_microvm", microvm_response(endpoint=fake_rayd.host, state="RUNNING")
    )
    control_plane.microvms.add_response("terminate_microvm", {})
    with Sandbox.create(
        IMAGE_ARN,
        idle=None,
        metadata=METADATA,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    ) as sandbox:
        health_calls = len(fake_rayd.servicer.health_calls)
        info = sandbox.get_info()
        assert info.state == "RUNNING"
        assert info.metadata == METADATA
        assert sandbox.info.metadata == METADATA
        assert len(fake_rayd.servicer.health_calls) == health_calls


def test_connect_populates_metadata_from_readiness_health(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.metadata = dict(METADATA)
    control_plane.microvms.add_response(
        "get_microvm", microvm_response(endpoint=fake_rayd.host, state="RUNNING")
    )
    control_plane.microvms.add_response("create_microvm_auth_token", auth_token_response())
    sandbox = Sandbox.connect(
        SANDBOX_ID,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    )
    try:
        assert sandbox.metadata == METADATA
    finally:
        sandbox.close()


def test_class_get_info_probes_health_only_when_running(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.metadata = {"a": "1"}
    transport = TrackingTransport.for_loopback()
    stub_metadata_probe(control_plane, SANDBOX_ID, fake_rayd)
    info = Sandbox.get_info(SANDBOX_ID, control_plane=control_plane.plane, transport=transport)
    assert info.metadata == {"a": "1"}
    assert transport.open_count == 1 and transport.all_closed
    assert len(fake_rayd.servicer.health_calls) == 1
    assert "x-access-token" not in fake_rayd.servicer.health_calls[0]

    stub_metadata_probe(control_plane, SANDBOX_ID, fake_rayd, state="SUSPENDED")
    paused = Sandbox.get_info(SANDBOX_ID, control_plane=control_plane.plane, transport=transport)
    assert paused.metadata is None
    assert transport.open_count == 1
    assert len(fake_rayd.servicer.health_calls) == 1


def test_class_get_info_without_read_metadata_never_mints(
    control_plane: StubbedControlPlane,
) -> None:
    control_plane.microvms.add_response("get_microvm", microvm_response(state="RUNNING"))
    info = Sandbox.get_info(SANDBOX_ID, read_metadata=False, control_plane=control_plane.plane)
    assert info.metadata is None


def test_class_get_info_reports_a_booting_agent_as_none(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, caplog: pytest.LogCaptureFixture
) -> None:
    fake_rayd.servicer.metadata = {"a": "1"}
    fake_rayd.servicer.not_ready_calls = 1
    stub_metadata_probe(control_plane, SANDBOX_ID, fake_rayd)
    with caplog.at_level(logging.INFO, logger="rayito.sandbox"):
        info = Sandbox.get_info(
            SANDBOX_ID,
            control_plane=control_plane.plane,
            transport=TrackingTransport.for_loopback(),
        )
    assert info.metadata is None
    messages = [record.getMessage() for record in caplog.records]
    assert any(SANDBOX_ID in message and "sin metadatos" in message for message in messages)


def test_list_by_metadata_probes_every_running_sandbox_sequentially(
    control_plane: StubbedControlPlane, fake_rayd_factory: FakeRaydFactory
) -> None:
    first = fake_rayd_factory({"env": "ci", "run": "1"})
    second = fake_rayd_factory({"env": "ci", "run": "2"})
    third = fake_rayd_factory({})
    transport = TrackingTransport.for_loopback()
    control_plane.microvms.add_response(
        "list_microvms",
        {
            "items": [
                list_item("a", "RUNNING"),
                list_item("b", "RUNNING"),
                list_item("c", "RUNNING"),
            ]
        },
        expected_params={"maxResults": 50},
    )
    stub_metadata_probe(control_plane, "a", first)
    stub_metadata_probe(control_plane, "b", second)
    stub_metadata_probe(control_plane, "c", third)

    listed = list(
        Sandbox.list(
            metadata={"env": "ci", "run": "2"},
            control_plane=control_plane.plane,
            transport=transport,
        )
    )
    assert [item.sandbox_id for item in listed] == ["b"]
    assert listed[0].metadata == {"env": "ci", "run": "2"}
    assert transport.open_count == 3 and transport.all_closed
    for endpoint in (first, second, third):
        assert len(endpoint.servicer.health_calls) == 1


def test_list_by_metadata_only_lists_running_and_skips_state_changes(
    control_plane: StubbedControlPlane, fake_rayd_factory: FakeRaydFactory
) -> None:
    live = fake_rayd_factory({"env": "ci"})
    transport = TrackingTransport.for_loopback()
    control_plane.microvms.add_response(
        "list_microvms",
        {"items": [list_item("a", "RUNNING"), list_item("b", "RUNNING")]},
        expected_params={"maxResults": 50, "imageIdentifier": IMAGE_ARN},
    )
    stub_metadata_probe(control_plane, "a", None, state="SUSPENDED")
    stub_metadata_probe(control_plane, "b", live)

    listed = list(
        Sandbox.list(
            template=IMAGE_ARN,
            metadata={"env": "ci"},
            states=["RUNNING"],
            control_plane=control_plane.plane,
            transport=transport,
        )
    )
    assert [item.sandbox_id for item in listed] == ["b"]
    assert transport.open_count == 1 and transport.all_closed


def test_list_by_metadata_skips_vanished_and_booting_sandboxes(
    control_plane: StubbedControlPlane, fake_rayd_factory: FakeRaydFactory
) -> None:
    booting = fake_rayd_factory({"env": "ci"})
    booting.servicer.not_ready_calls = 1
    control_plane.microvms.add_response(
        "list_microvms",
        {"items": [list_item("gone", "RUNNING"), list_item("boot", "RUNNING")]},
    )
    control_plane.microvms.add_client_error(
        "get_microvm", service_error_code="ResourceNotFoundException", http_status_code=404
    )
    stub_metadata_probe(control_plane, "boot", booting)

    listed = list(
        Sandbox.list(
            metadata={"env": "ci"},
            control_plane=control_plane.plane,
            transport=TrackingTransport.for_loopback(),
        )
    )
    assert listed == []


def test_list_by_metadata_raises_when_a_health_fails(
    control_plane: StubbedControlPlane, fake_rayd_factory: FakeRaydFactory
) -> None:
    broken = fake_rayd_factory({"env": "ci"})
    broken.servicer.unavailable_calls = 10
    transport = TrackingTransport.for_loopback()
    control_plane.microvms.add_response(
        "list_microvms", {"items": [list_item("a", "RUNNING"), list_item("b", "RUNNING")]}
    )
    stub_metadata_probe(control_plane, "a", broken)
    with pytest.raises(SandboxException) as excinfo:
        list(
            Sandbox.list(
                metadata={"env": "ci"},
                control_plane=control_plane.plane,
                transport=transport,
                request_timeout=0.5,
            )
        )
    assert "sandbox a" in str(excinfo.value)
    assert transport.all_closed


def test_list_by_metadata_refuses_suspended_states_before_any_call(
    control_plane: StubbedControlPlane,
) -> None:
    with pytest.raises(InvalidArgumentException, match="RUNNING"):
        Sandbox.list(metadata={"a": "1"}, states=["SUSPENDED"], control_plane=control_plane.plane)
    with pytest.raises(InvalidArgumentException):
        Sandbox.list(metadata={"": "1"}, control_plane=control_plane.plane)


def test_list_without_metadata_is_unchanged(control_plane: StubbedControlPlane) -> None:
    control_plane.microvms.add_response(
        "list_microvms", {"items": [list_item("x", "RUNNING"), list_item("y", "SUSPENDED")]}
    )
    listed = list(Sandbox.list(control_plane=control_plane.plane))
    assert [item.sandbox_id for item in listed] == ["x", "y"]
    assert all(item.metadata is None for item in listed)


def test_endpoint_helper_formats_host_and_port(fake_rayd: RaydEndpoint) -> None:
    assert endpoint_of(fake_rayd) == f"127.0.0.1:{fake_rayd.port}"
