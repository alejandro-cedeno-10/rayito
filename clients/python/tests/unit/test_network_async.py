"""Política de egress en `AsyncSandbox`: la misma superficie y los mismos
casos que `test_network_sync.py` sobre `grpc.aio`."""

from __future__ import annotations

import json
import logging
from typing import Any, cast

import grpc
import pytest

from rayito import (
    ALL_TRAFFIC,
    AsyncSandbox,
    EgressEnforcement,
    EgressProxy,
    NetworkPolicy,
    NetworkState,
    S3Prefix,
    UnimplementedError,
)
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
from .test_network_sync import (
    BUCKET,
    ROLE,
    SUCCESSOR_ID,
    UNENFORCED,
    capture_payloads,
    stub_launch,
    stub_terminate,
)


async def create(
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
    *,
    terminated: bool = False,
    **kwargs: Any,
) -> AsyncSandbox:
    stub_launch(control_plane, fake_rayd)
    if terminated:
        stub_terminate(control_plane)
    return await AsyncSandbox.create(
        IMAGE_ARN,
        idle=None,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
        **kwargs,
    )


async def kill(control_plane: StubbedControlPlane, sandbox: AsyncSandbox) -> None:
    stub_terminate(control_plane, sandbox.sandbox_id)
    await sandbox.kill()


# -------------------------------------------------------------- create gate


@pytest.mark.parametrize("enforcement", UNENFORCED)
async def test_async_unenforced_image_terminates_even_with_keep_on_failure(
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
    enforcement: network_pb2.EgressEnforcement,
) -> None:
    fake_rayd.servicer.egress_enforcement = enforcement
    payloads = capture_payloads(control_plane)

    with pytest.raises(UnimplementedError) as excinfo:
        await create(
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


async def test_async_enforcing_image_receives_the_policy_before_create_returns(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.egress_enforcement = network_pb2.EGRESS_ENFORCEMENT_GUEST_ROUTES
    payloads = capture_payloads(control_plane)

    sandbox = await create(
        control_plane,
        fake_rayd,
        network={"allow_out": ["1.2.3.4/32"], "deny_out": lambda ctx: [ctx.all_traffic]},
    )
    try:
        policy = fake_rayd.network.last_policy
        assert list(policy.allow_out) == ["1.2.3.4/32"]
        assert list(policy.deny_out) == [ALL_TRAFFIC]
        assert payloads[-1]["network"] == {"enforce": True}
        assert sandbox._launch_options is not None
        assert sandbox._launch_options.network == NetworkPolicy(
            allow_out=("1.2.3.4/32",), deny_out=(ALL_TRAFFIC,)
        )
    finally:
        await kill(control_plane, sandbox)


async def test_async_proxy_credentials_travel_only_in_update_network(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.egress_enforcement = network_pb2.EGRESS_ENFORCEMENT_GUEST_ROUTES_AND_PROXY
    fake_rayd.network.enforcement = network_pb2.EGRESS_ENFORCEMENT_GUEST_ROUTES_AND_PROXY
    payloads = capture_payloads(control_plane)

    sandbox = await create(
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
        await kill(control_plane, sandbox)


async def test_async_invalid_argument_from_update_network_terminates(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.egress_enforcement = network_pb2.EGRESS_ENFORCEMENT_GUEST_ROUTES
    fake_rayd.network.fail_with = (grpc.StatusCode.INVALID_ARGUMENT, "allow_out[0]: no es un CIDR")

    with pytest.raises(InvalidArgumentException, match=r"allow_out\[0\]"):
        await create(
            control_plane,
            fake_rayd,
            terminated=True,
            network={"allow_out": ["1.2.3.4"], "deny_out": [ALL_TRAFFIC]},
            keep_on_failure=True,
        )

    assert len(fake_rayd.network.update_requests) == 1


async def test_async_update_network_answering_none_terminates(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.egress_enforcement = network_pb2.EGRESS_ENFORCEMENT_GUEST_ROUTES
    fake_rayd.network.enforcement = network_pb2.EGRESS_ENFORCEMENT_NONE

    with pytest.raises(UnimplementedError) as excinfo:
        await create(
            control_plane, fake_rayd, terminated=True, network={"deny_out": ["10.0.0.0/8"]}
        )

    assert excinfo.value.feature == NETWORK_FEATURE


async def test_async_allow_out_only_sends_no_block_and_no_update(
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
    caplog: pytest.LogCaptureFixture,
) -> None:
    log_allow_only_notice.cache_clear()
    caplog.set_level(logging.INFO, logger="rayito.network")
    payloads = capture_payloads(control_plane)

    sandbox = await create(control_plane, fake_rayd, network={"allow_out": ["example.com"]})
    try:
        assert "network" not in payloads[-1]
        assert fake_rayd.network.update_requests == []
        assert "allow_out sin deny_out" in caplog.text
    finally:
        await kill(control_plane, sandbox)


async def test_async_a_bad_policy_fails_before_any_plane_call(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    with pytest.raises(InvalidArgumentException, match=r"deny_out\[0\]"):
        await AsyncSandbox.create(
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
        ({"allow_internet_access": False}, "allow_internet_access"),
    ],
)
async def test_async_pool_refuses_a_network_policy(kwargs: dict[str, Any], name: str) -> None:
    with pytest.raises(InvalidArgumentException, match=f"`{name}`"):
        await AsyncSandbox.create(pool=cast("Any", object()), **kwargs)


async def test_async_reincarnate_resends_the_policy(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.egress_enforcement = network_pb2.EGRESS_ENFORCEMENT_GUEST_ROUTES
    original = await create(
        control_plane,
        fake_rayd,
        execution_role_arn=ROLE,
        persist=S3Prefix(BUCKET),
        allow_internet_access=False,
    )
    stub_launch(control_plane, fake_rayd, sandbox_id=SUCCESSOR_ID)
    stub_terminate(control_plane, SANDBOX_ID)

    successor = await original.reincarnate()
    try:
        first, second = fake_rayd.network.update_requests
        assert list(first.policy.deny_out) == list(second.policy.deny_out) == [ALL_TRAFFIC]
        assert successor._launch_options is not None
        assert successor._launch_options.network == NetworkPolicy(deny_out=(ALL_TRAFFIC,))
    finally:
        await kill(control_plane, successor)


async def test_async_get_health_reports_egress_enforcement(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    sandbox = await create(control_plane, fake_rayd)
    try:
        fake_rayd.servicer.egress_enforcement = network_pb2.EGRESS_ENFORCEMENT_GUEST_ROUTES
        health = await sandbox.get_health()
        assert health.egress_enforcement is EgressEnforcement.GUEST_ROUTES
    finally:
        await kill(control_plane, sandbox)


# ------------------------------------------------------ update/get_network


async def test_async_update_and_get_network_replace_the_whole_policy(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.network.enforcement = network_pb2.EGRESS_ENFORCEMENT_GUEST_ROUTES_AND_PROXY
    sandbox = await create(control_plane, fake_rayd)
    try:
        state = await sandbox.update_network(
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
        assert await sandbox.get_network() == state

        closed = await sandbox.update_network(allow_internet_access=False)
        assert closed.deny_out == (ALL_TRAFFIC,)

        reopened = await sandbox.update_network(None)
        assert (reopened.allow_out, reopened.deny_out) == ((), ())
        assert reopened.enforcement is EgressEnforcement.NONE
    finally:
        await kill(control_plane, sandbox)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (grpc.StatusCode.FAILED_PRECONDITION, UnimplementedError),
        (grpc.StatusCode.UNIMPLEMENTED, UnimplementedError),
        (grpc.StatusCode.INVALID_ARGUMENT, InvalidArgumentException),
    ],
)
async def test_async_update_network_maps_agent_errors(
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
    code: grpc.StatusCode,
    expected: type[Exception],
) -> None:
    sandbox = await create(control_plane, fake_rayd)
    try:
        fake_rayd.network.fail_with = (code, "rechazado")
        with pytest.raises(expected) as excinfo:
            await sandbox.update_network({"deny_out": [ALL_TRAFFIC]})
        if isinstance(excinfo.value, UnimplementedError):
            assert excinfo.value.feature == UPDATE_NETWORK_FEATURE
    finally:
        await kill(control_plane, sandbox)


async def test_async_update_network_checks_the_shape_before_the_rpc(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    sandbox = await create(control_plane, fake_rayd)
    try:
        with pytest.raises(InvalidArgumentException, match="'rules'"):
            await sandbox.update_network({"rules": {}})
        with pytest.raises(InvalidArgumentException, match=r"deny_out\[0\]"):
            await sandbox.update_network({"deny_out": ["example.com"]})
        assert fake_rayd.network.update_requests == []
    finally:
        await kill(control_plane, sandbox)


async def test_async_class_update_network_connects_updates_and_closes_without_killing(
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
            "allowedPorts": [{"port": 8080}],
        },
    )

    state = await AsyncSandbox.update_network(
        SANDBOX_ID,
        {"deny_out": [ALL_TRAFFIC]},
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=transport,
    )

    assert state.deny_out == (ALL_TRAFFIC,)
    assert len(fake_rayd.network.update_requests) == 1
    assert transport.all_closed
