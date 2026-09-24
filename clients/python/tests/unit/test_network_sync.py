"""Política de egress en `Sandbox` (design D13): la compuerta de `create()`,
`update_network`/`get_network` y la variante de clase, contra el `rayd`
falso (gRPC real en loopback) y el plano de control con Stubber."""

from __future__ import annotations

import json
import logging
from typing import Any, cast

import grpc
import pytest

from rayito import (
    ALL_TRAFFIC,
    EgressEnforcement,
    EgressProxy,
    NetworkPolicy,
    NetworkState,
    S3Prefix,
    Sandbox,
    UnimplementedError,
)
from rayito._limits import DEFAULT_PORT
from rayito._network_base import (
    ALLOW_INTERNET_ACCESS_FEATURE,
    NETWORK_FEATURE,
    UPDATE_NETWORK_FEATURE,
    log_allow_only_notice,
)
from rayito.exceptions import InvalidArgumentException
from rayito.v1 import network_pb2

from .conftest import (
    ACCESS_TOKEN,
    IMAGE_ARN,
    SANDBOX_ID,
    RaydEndpoint,
    StubbedControlPlane,
    TrackingTransport,
    auth_token_response,
    endpoint_of,
    microvm_response,
)
from .fake_network import DEFAULT_LOCAL_PROXY_PORT

ROLE = "arn:aws:iam::123456789012:role/rayito-execution"
BUCKET = "my-bucket"
SUCCESSOR_ID = "microvm-00000000-0000-0000-0000-000000000002"
UNENFORCED = [network_pb2.EGRESS_ENFORCEMENT_NONE, network_pb2.EGRESS_ENFORCEMENT_UNSPECIFIED]


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


def capture_payloads(control_plane: StubbedControlPlane) -> list[dict[str, Any]]:
    """Cada `runHookPayload` que llega a `run-microvm`, ya parseado."""
    payloads: list[dict[str, Any]] = []
    original = control_plane.plane.run_microvm

    def spy(request: Any) -> Any:
        payloads.append(json.loads(request.run_hook_payload))
        return original(request)

    control_plane.plane.run_microvm = spy  # type: ignore[method-assign]
    return payloads


def create(
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
    *,
    terminated: bool = False,
    **kwargs: Any,
) -> Sandbox:
    """`Sandbox.create` contra el plano con Stubber; `terminated` encola el
    `terminate-microvm` que la compuerta debe mandar tras el arranque."""
    stub_launch(control_plane, fake_rayd)
    if terminated:
        stub_terminate(control_plane)
    return Sandbox.create(
        IMAGE_ARN,
        idle=None,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
        **kwargs,
    )


def kill(control_plane: StubbedControlPlane, sandbox: Sandbox) -> None:
    stub_terminate(control_plane, sandbox.sandbox_id)
    sandbox.kill()


# -------------------------------------------------------------- create gate


@pytest.mark.parametrize("enforcement", UNENFORCED)
def test_unenforced_image_terminates_even_with_keep_on_failure(
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
    enforcement: network_pb2.EgressEnforcement,
) -> None:
    fake_rayd.servicer.egress_enforcement = enforcement
    payloads = capture_payloads(control_plane)

    with pytest.raises(UnimplementedError) as excinfo:
        create(
            control_plane,
            fake_rayd,
            terminated=True,
            allow_internet_access=False,
            keep_on_failure=True,
        )

    assert excinfo.value.feature == ALLOW_INTERNET_ACCESS_FEATURE
    assert "rayito-base-caps" in excinfo.value.reason
    assert fake_rayd.network.update_requests == []
    assert payloads[-1]["network"] == {"enforce": True}


def test_enforcing_image_receives_the_policy_before_create_returns(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.egress_enforcement = network_pb2.EGRESS_ENFORCEMENT_GUEST_ROUTES
    payloads = capture_payloads(control_plane)

    sandbox = create(
        control_plane,
        fake_rayd,
        network={"allow_out": ["1.2.3.4/32"], "deny_out": lambda ctx: [ctx.all_traffic]},
    )
    try:
        policy = fake_rayd.network.last_policy
        assert list(policy.allow_out) == ["1.2.3.4/32"]
        assert list(policy.deny_out) == [ALL_TRAFFIC]
        assert not policy.HasField("egress_proxy")
        assert payloads[-1]["network"] == {"enforce": True}
        assert "1.2.3.4" not in json.dumps(payloads[-1])
        assert sandbox._launch_options is not None
        assert sandbox._launch_options.network == NetworkPolicy(
            allow_out=("1.2.3.4/32",), deny_out=(ALL_TRAFFIC,)
        )
    finally:
        kill(control_plane, sandbox)


def test_proxy_credentials_travel_only_in_update_network(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.egress_enforcement = network_pb2.EGRESS_ENFORCEMENT_GUEST_ROUTES_AND_PROXY
    fake_rayd.network.enforcement = network_pb2.EGRESS_ENFORCEMENT_GUEST_ROUTES_AND_PROXY
    payloads = capture_payloads(control_plane)

    sandbox = create(
        control_plane,
        fake_rayd,
        network={"egress_proxy": {"address": "10.0.0.5:1080", "username": "u", "password": "p"}},
    )
    try:
        proxy = fake_rayd.network.last_policy.egress_proxy
        assert (proxy.address, proxy.username, proxy.password) == ("10.0.0.5:1080", "u", "p")
        assert payloads[-1]["network"] == {"enforce": True}
        assert "10.0.0.5" not in json.dumps(payloads[-1])
        assert "password='p'" not in repr(sandbox._launch_options)
    finally:
        kill(control_plane, sandbox)


def test_invalid_argument_from_update_network_terminates(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.egress_enforcement = network_pb2.EGRESS_ENFORCEMENT_GUEST_ROUTES
    fake_rayd.network.fail_with = (grpc.StatusCode.INVALID_ARGUMENT, "allow_out[0]: no es un CIDR")

    with pytest.raises(InvalidArgumentException, match=r"allow_out\[0\]"):
        create(
            control_plane,
            fake_rayd,
            terminated=True,
            network={"allow_out": ["1.2.3.4"], "deny_out": [ALL_TRAFFIC]},
            keep_on_failure=True,
        )

    assert len(fake_rayd.network.update_requests) == 1


def test_update_network_answering_none_terminates(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.egress_enforcement = network_pb2.EGRESS_ENFORCEMENT_GUEST_ROUTES
    fake_rayd.network.enforcement = network_pb2.EGRESS_ENFORCEMENT_NONE

    with pytest.raises(UnimplementedError) as excinfo:
        create(control_plane, fake_rayd, terminated=True, network={"deny_out": ["10.0.0.0/8"]})

    assert excinfo.value.feature == NETWORK_FEATURE


def test_allow_out_only_sends_no_block_and_no_update(
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
    caplog: pytest.LogCaptureFixture,
) -> None:
    log_allow_only_notice.cache_clear()
    caplog.set_level(logging.INFO, logger="rayito.network")
    payloads = capture_payloads(control_plane)

    sandbox = create(control_plane, fake_rayd, network={"allow_out": ["example.com"]})
    try:
        assert "network" not in payloads[-1]
        assert fake_rayd.network.update_requests == []
        assert "allow_out sin deny_out" in caplog.text
        assert "example.com" not in caplog.text
    finally:
        kill(control_plane, sandbox)


def test_a_bad_policy_fails_before_any_plane_call(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    with pytest.raises(InvalidArgumentException, match=r"deny_out\[0\]"):
        Sandbox.create(
            IMAGE_ARN,
            network={"deny_out": ["example.com"]},
            control_plane=control_plane.plane,
            transport=fake_rayd.transport,
        )
    assert fake_rayd.servicer.health_calls == []


@pytest.mark.parametrize(
    ("kwargs", "name"),
    [
        ({"network": {"deny_out": [ALL_TRAFFIC]}}, "network"),
        ({"network": {"allow_out": ["1.2.3.4"]}}, "network"),
        ({"allow_internet_access": False}, "allow_internet_access"),
    ],
)
def test_pool_refuses_a_network_policy(kwargs: dict[str, Any], name: str) -> None:
    with pytest.raises(InvalidArgumentException, match=f"`{name}`"):
        Sandbox.create(pool=cast("Any", object()), **kwargs)


def test_reincarnate_resends_the_policy(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.egress_enforcement = network_pb2.EGRESS_ENFORCEMENT_GUEST_ROUTES
    original = create(
        control_plane,
        fake_rayd,
        execution_role_arn=ROLE,
        persist=S3Prefix(BUCKET),
        allow_internet_access=False,
    )
    stub_launch(control_plane, fake_rayd, sandbox_id=SUCCESSOR_ID)
    stub_terminate(control_plane, SANDBOX_ID)

    successor = original.reincarnate()
    try:
        first, second = fake_rayd.network.update_requests
        assert list(first.policy.deny_out) == list(second.policy.deny_out) == [ALL_TRAFFIC]
        assert successor._launch_options is not None
        assert successor._launch_options.network == NetworkPolicy(deny_out=(ALL_TRAFFIC,))
    finally:
        kill(control_plane, successor)


def test_get_health_reports_egress_enforcement(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    sandbox = create(control_plane, fake_rayd)
    try:
        assert sandbox.get_health().egress_enforcement is EgressEnforcement.UNSPECIFIED
        fake_rayd.servicer.egress_enforcement = (
            network_pb2.EGRESS_ENFORCEMENT_GUEST_ROUTES_AND_PROXY
        )
        assert sandbox.get_health().egress_enforcement is EgressEnforcement.GUEST_ROUTES_AND_PROXY
    finally:
        kill(control_plane, sandbox)


# ------------------------------------------------------ update/get_network


def test_update_and_get_network_replace_the_whole_policy(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.network.enforcement = network_pb2.EGRESS_ENFORCEMENT_GUEST_ROUTES_AND_PROXY
    sandbox = create(control_plane, fake_rayd)
    try:
        state = sandbox.update_network(
            {
                "allow_out": ["api.example.com"],
                "deny_out": [ALL_TRAFFIC],
                "egress_proxy": EgressProxy("10.0.0.5:1080", username="u", password="p"),
            }
        )
        assert state == NetworkState(
            allow_out=("api.example.com",),
            deny_out=(ALL_TRAFFIC,),
            egress_proxy_configured=True,
            enforcement=EgressEnforcement.GUEST_ROUTES_AND_PROXY,
            local_proxy_port=DEFAULT_LOCAL_PROXY_PORT,
        )
        assert sandbox.get_network() == state
        assert fake_rayd.network.get_calls == 1

        closed = sandbox.update_network(allow_internet_access=False)
        assert closed.deny_out == (ALL_TRAFFIC,)
        assert not fake_rayd.network.last_policy.HasField("egress_proxy")

        reopened = sandbox.update_network(None)
        assert (reopened.allow_out, reopened.deny_out) == ((), ())
        assert reopened.enforcement is EgressEnforcement.NONE
        assert reopened.local_proxy_port is None
    finally:
        kill(control_plane, sandbox)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (grpc.StatusCode.FAILED_PRECONDITION, UnimplementedError),
        (grpc.StatusCode.UNIMPLEMENTED, UnimplementedError),
        (grpc.StatusCode.INVALID_ARGUMENT, InvalidArgumentException),
    ],
)
def test_update_network_maps_agent_errors(
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
    code: grpc.StatusCode,
    expected: type[Exception],
) -> None:
    sandbox = create(control_plane, fake_rayd)
    try:
        fake_rayd.network.fail_with = (code, "rechazado")
        with pytest.raises(expected) as excinfo:
            sandbox.update_network({"deny_out": [ALL_TRAFFIC]})
        if isinstance(excinfo.value, UnimplementedError):
            assert excinfo.value.feature == UPDATE_NETWORK_FEATURE
            assert "rayito-base-caps" in excinfo.value.reason
    finally:
        kill(control_plane, sandbox)


def test_update_network_checks_the_shape_before_the_rpc(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    sandbox = create(control_plane, fake_rayd)
    try:
        with pytest.raises(InvalidArgumentException, match="'rules'"):
            sandbox.update_network({"rules": {}})
        with pytest.raises(InvalidArgumentException, match=r"deny_out\[0\]"):
            sandbox.update_network({"deny_out": ["example.com"]})
        assert fake_rayd.network.update_requests == []
    finally:
        kill(control_plane, sandbox)


def test_class_update_network_connects_updates_and_closes_without_killing(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    transport = TrackingTransport.for_loopback()
    control_plane.microvms.add_response(
        "get_microvm",
        microvm_response(state="RUNNING", endpoint=endpoint_of(fake_rayd)),
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

    state = Sandbox.update_network(
        SANDBOX_ID,
        {"deny_out": [ALL_TRAFFIC]},
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=transport,
    )

    assert isinstance(state, NetworkState)
    assert state.deny_out == (ALL_TRAFFIC,)
    assert len(fake_rayd.network.update_requests) == 1
    assert transport.open_count >= 1
    assert transport.all_closed
