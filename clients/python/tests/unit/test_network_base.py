"""Núcleo puro de la política de egress (`_network_base.py`, design D13)."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

import grpc
import pytest

from rayito._limits import (
    EGRESS_HOSTNAME_MAX_CHARS,
    EGRESS_MAX_ENTRIES_PER_LIST,
    EGRESS_MAX_HOSTNAME_ENTRIES,
    EGRESS_PROXY_CREDENTIAL_MAX_BYTES,
)
from rayito._models import (
    ALL_TRAFFIC,
    EgressEnforcement,
    EgressProxy,
    NetworkPolicy,
    NetworkSelectorContext,
    NetworkState,
)
from rayito._network_base import (
    ALLOW_INTERNET_ACCESS_FEATURE,
    EGRESS_UNAVAILABLE_REASON,
    NETWORK_FEATURE,
    UPDATE_NETWORK_FEATURE,
    egress_gate_error,
    enforcement_from_proto,
    log_allow_only_notice,
    network_rpc_error,
    plan_network_launch,
    policy_to_proto,
    pool_network_kwarg,
    requires_enforcement,
    resolve_network,
    resolve_selector,
    state_from_proto,
    update_policy,
    validate_policy_shape,
)
from rayito.exceptions import (
    InvalidArgumentException,
    SandboxException,
    UnimplementedError,
)
from rayito.v1 import network_pb2

from .conftest import FakeRpcError

CONTEXT = NetworkSelectorContext()


# ------------------------------------------------------------------ selectors


def test_all_traffic_is_the_e2b_constant() -> None:
    assert ALL_TRAFFIC == "0.0.0.0/0"
    assert CONTEXT.all_traffic == ALL_TRAFFIC
    assert dict(CONTEXT.rules) == {}


def test_a_list_selector_becomes_a_tuple() -> None:
    assert resolve_selector(["1.2.3.4", "10.0.0.0/8"], CONTEXT, field="allow_out") == (
        "1.2.3.4",
        "10.0.0.0/8",
    )
    assert resolve_selector(None, CONTEXT, field="allow_out") == ()


def test_a_callable_selector_receives_the_context() -> None:
    seen: list[NetworkSelectorContext] = []

    def selector(ctx: NetworkSelectorContext) -> Sequence[str]:
        seen.append(ctx)
        return [ctx.all_traffic]

    assert resolve_selector(selector, CONTEXT, field="deny_out") == (ALL_TRAFFIC,)
    assert seen == [CONTEXT]


@pytest.mark.parametrize(
    "selector",
    [
        "0.0.0.0/0",
        [1, 2],
        ["ok", None],
        42,
        lambda ctx: "0.0.0.0/0",
        lambda ctx: [b"0.0.0.0/0"],
    ],
)
def test_a_selector_that_is_not_a_sequence_of_str_is_rejected(selector: Any) -> None:
    with pytest.raises(InvalidArgumentException, match="deny_out"):
        resolve_selector(selector, CONTEXT, field="deny_out")


# ------------------------------------------------------------ resolve_network


def test_none_resolves_to_the_unrestricted_policy() -> None:
    assert resolve_network(None, allow_internet_access=True) == NetworkPolicy()


def test_allow_internet_access_false_appends_all_traffic_once() -> None:
    assert resolve_network(None, allow_internet_access=False) == NetworkPolicy(
        deny_out=(ALL_TRAFFIC,)
    )
    merged = resolve_network(
        {"allow_out": ["1.2.3.4/32"], "deny_out": ["10.0.0.0/8"]}, allow_internet_access=False
    )
    assert merged == NetworkPolicy(allow_out=("1.2.3.4/32",), deny_out=("10.0.0.0/8", ALL_TRAFFIC))
    already = resolve_network({"deny_out": [ALL_TRAFFIC]}, allow_internet_access=False)
    assert already.deny_out == (ALL_TRAFFIC,)


def test_a_mapping_resolves_callables_and_the_proxy_mapping() -> None:
    policy = resolve_network(
        {
            "allow_out": ["api.example.com"],
            "deny_out": lambda ctx: [ctx.all_traffic],
            "egress_proxy": {"address": "10.0.0.5:1080", "username": "u", "password": "p"},
        },
        allow_internet_access=True,
    )
    assert policy == NetworkPolicy(
        allow_out=("api.example.com",),
        deny_out=(ALL_TRAFFIC,),
        egress_proxy=EgressProxy(address="10.0.0.5:1080", username="u", password="p"),
    )


def test_a_network_policy_passes_through_normalized() -> None:
    policy = NetworkPolicy(allow_out=("1.2.3.4",), egress_proxy=EgressProxy("proxy:1080"))
    assert resolve_network(policy, allow_internet_access=True) == policy


def test_unknown_network_keys_are_rejected_by_name() -> None:
    with pytest.raises(InvalidArgumentException, match="'rules'"):
        resolve_network({"rules": {}}, allow_internet_access=True)
    with pytest.raises(InvalidArgumentException, match="'port'"):
        resolve_network(
            {"egress_proxy": {"address": "x:1", "port": "1"}}, allow_internet_access=True
        )


@pytest.mark.parametrize(
    "network",
    [
        ["0.0.0.0/0"],
        "0.0.0.0/0",
        {"egress_proxy": "10.0.0.5:1080"},
        {"egress_proxy": {"username": "u"}},
        {"egress_proxy": {"address": 1080}},
    ],
)
def test_malformed_network_values_are_rejected(network: Any) -> None:
    with pytest.raises(InvalidArgumentException):
        resolve_network(network, allow_internet_access=True)


# ------------------------------------------------------- requires_enforcement


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        (NetworkPolicy(), False),
        (NetworkPolicy(allow_out=("example.com",)), False),
        (NetworkPolicy(deny_out=("10.0.0.0/8",)), True),
        (NetworkPolicy(egress_proxy=EgressProxy("proxy:1080")), True),
    ],
)
def test_requires_enforcement_mirrors_rayd(policy: NetworkPolicy, expected: bool) -> None:
    assert requires_enforcement(policy) is expected


def test_pool_network_kwarg_is_none_only_for_an_empty_request() -> None:
    assert pool_network_kwarg(None) is None
    assert pool_network_kwarg({}) is None
    assert pool_network_kwarg(NetworkPolicy()) is None
    assert pool_network_kwarg({"allow_out": ["1.2.3.4"]}) == NetworkPolicy(allow_out=("1.2.3.4",))


# ------------------------------------------------------------- shape checks


def test_validate_policy_shape_accepts_the_e2b_grammar() -> None:
    policy = NetworkPolicy(
        allow_out=("1.2.3.4", "10.0.0.0/8", "::1", "api.example.com", "*.example.org"),
        deny_out=(ALL_TRAFFIC, "::/0", "192.168.1.7/24"),
        egress_proxy=EgressProxy("proxy.example:1080", username="u", password="p"),
    )
    assert validate_policy_shape(policy) is policy


def test_a_hostname_in_deny_out_names_the_index_but_never_the_entry() -> None:
    policy = NetworkPolicy(deny_out=("10.0.0.0/8", "secret.example.com"))
    with pytest.raises(InvalidArgumentException, match=r"deny_out\[1\]") as excinfo:
        validate_policy_shape(policy)
    assert "secret" not in str(excinfo.value)


def test_empty_entries_are_rejected_by_index() -> None:
    with pytest.raises(InvalidArgumentException, match=r"allow_out\[1\]"):
        validate_policy_shape(NetworkPolicy(allow_out=("1.2.3.4", "")))


def test_list_and_hostname_caps_come_from_limits() -> None:
    too_many = tuple(f"10.0.{index // 256}.{index % 256}" for index in range(257))
    assert EGRESS_MAX_ENTRIES_PER_LIST == 256
    with pytest.raises(InvalidArgumentException, match="256"):
        validate_policy_shape(NetworkPolicy(deny_out=too_many))
    hostnames = tuple(f"h{index}.example.com" for index in range(EGRESS_MAX_HOSTNAME_ENTRIES + 1))
    with pytest.raises(InvalidArgumentException, match=str(EGRESS_MAX_HOSTNAME_ENTRIES)):
        validate_policy_shape(NetworkPolicy(allow_out=hostnames))
    at_cap = hostnames[:EGRESS_MAX_HOSTNAME_ENTRIES]
    validate_policy_shape(NetworkPolicy(allow_out=at_cap))


def test_hostname_length_cap_ignores_the_wildcard_and_the_trailing_dot() -> None:
    label = "a" * 63
    name = ".".join([label, label, label, "b" * 61])
    assert len(name) == EGRESS_HOSTNAME_MAX_CHARS
    validate_policy_shape(NetworkPolicy(allow_out=(name, f"*.{name}", f"{name}.")))
    with pytest.raises(InvalidArgumentException, match=r"allow_out\[0\]"):
        validate_policy_shape(NetworkPolicy(allow_out=(f"c{name}",)))


@pytest.mark.parametrize(
    "proxy",
    [
        EgressProxy(""),
        EgressProxy("proxy:1080", password="p"),
        EgressProxy("proxy:1080", username=""),
        EgressProxy("proxy:1080", username="u" * (EGRESS_PROXY_CREDENTIAL_MAX_BYTES + 1)),
        EgressProxy("proxy:1080", username="u", password="ñ" * 128),
    ],
)
def test_proxy_shape_errors_name_egress_proxy_without_secrets(proxy: EgressProxy) -> None:
    with pytest.raises(InvalidArgumentException, match="egress_proxy") as excinfo:
        validate_policy_shape(NetworkPolicy(egress_proxy=proxy))
    assert "ñ" not in str(excinfo.value)


# --------------------------------------------------------------- proto I/O


def test_policy_to_proto_sets_optional_credentials_only_when_given() -> None:
    bare = policy_to_proto(NetworkPolicy(deny_out=(ALL_TRAFFIC,)))
    assert list(bare.deny_out) == [ALL_TRAFFIC]
    assert not bare.HasField("egress_proxy")
    full = policy_to_proto(
        NetworkPolicy(
            allow_out=("a.example.com",),
            egress_proxy=EgressProxy("proxy:1080", username="u", password="p"),
        )
    )
    assert list(full.allow_out) == ["a.example.com"]
    assert full.egress_proxy.address == "proxy:1080"
    assert full.egress_proxy.HasField("username")
    assert full.egress_proxy.password == "p"
    anonymous = policy_to_proto(NetworkPolicy(egress_proxy=EgressProxy("proxy:1080")))
    assert not anonymous.egress_proxy.HasField("username")
    assert not anonymous.egress_proxy.HasField("password")


def test_state_from_proto_maps_every_field() -> None:
    response = network_pb2.NetworkState(
        allow_out=["a.example.com"],
        deny_out=[ALL_TRAFFIC],
        egress_proxy_configured=True,
        enforcement=network_pb2.EGRESS_ENFORCEMENT_GUEST_ROUTES_AND_PROXY,
        local_proxy_port=41234,
    )
    assert state_from_proto(response) == NetworkState(
        allow_out=("a.example.com",),
        deny_out=(ALL_TRAFFIC,),
        egress_proxy_configured=True,
        enforcement=EgressEnforcement.GUEST_ROUTES_AND_PROXY,
        local_proxy_port=41234,
    )
    empty = state_from_proto(network_pb2.NetworkState())
    assert empty.local_proxy_port is None
    assert empty.enforcement is EgressEnforcement.UNSPECIFIED


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (network_pb2.EGRESS_ENFORCEMENT_UNSPECIFIED, EgressEnforcement.UNSPECIFIED),
        (network_pb2.EGRESS_ENFORCEMENT_NONE, EgressEnforcement.NONE),
        (network_pb2.EGRESS_ENFORCEMENT_GUEST_ROUTES, EgressEnforcement.GUEST_ROUTES),
        (
            network_pb2.EGRESS_ENFORCEMENT_GUEST_ROUTES_AND_PROXY,
            EgressEnforcement.GUEST_ROUTES_AND_PROXY,
        ),
        (99, EgressEnforcement.UNSPECIFIED),
    ],
)
def test_enforcement_from_proto_fails_closed_on_unknown_values(
    value: int, expected: EgressEnforcement
) -> None:
    assert enforcement_from_proto(value) is expected


# -------------------------------------------------------------------- gate


@pytest.mark.parametrize("enforcement", [EgressEnforcement.UNSPECIFIED, EgressEnforcement.NONE])
def test_gate_error_for_unenforced_images_names_caps_and_the_vpc_connector(
    enforcement: EgressEnforcement,
) -> None:
    error = egress_gate_error("microvm-x", enforcement, NETWORK_FEATURE)
    assert isinstance(error, UnimplementedError)
    assert isinstance(error, NotImplementedError)
    assert error.feature == NETWORK_FEATURE
    assert error.reason == EGRESS_UNAVAILABLE_REASON
    assert "rayito-base-caps" in str(error)
    assert "infra/egress-connector.yaml" in str(error)
    assert any("microvm-x" in note for note in error.__notes__)


@pytest.mark.parametrize(
    "enforcement", [EgressEnforcement.GUEST_ROUTES, EgressEnforcement.GUEST_ROUTES_AND_PROXY]
)
def test_gate_passes_for_enforcing_images(enforcement: EgressEnforcement) -> None:
    assert egress_gate_error("microvm-x", enforcement, NETWORK_FEATURE) is None


def test_launch_plan_names_the_flag_only_when_it_alone_restricts() -> None:
    by_flag = plan_network_launch(None, allow_internet_access=False)
    assert by_flag.enforce is True
    assert by_flag.feature == ALLOW_INTERNET_ACCESS_FEATURE
    same_as_flag = plan_network_launch({"deny_out": [ALL_TRAFFIC]}, allow_internet_access=False)
    assert same_as_flag.feature == ALLOW_INTERNET_ACCESS_FEATURE
    with_allow = plan_network_launch({"allow_out": ["1.2.3.4"]}, allow_internet_access=False)
    assert with_allow.enforce is True
    assert with_allow.feature == NETWORK_FEATURE
    with_proxy = plan_network_launch(
        {"egress_proxy": {"address": "10.0.0.5:1080"}}, allow_internet_access=False
    )
    assert with_proxy.feature == NETWORK_FEATURE
    denied_without_flag = plan_network_launch(
        {"deny_out": [ALL_TRAFFIC]}, allow_internet_access=True
    )
    assert denied_without_flag.feature == NETWORK_FEATURE
    by_network = plan_network_launch({"deny_out": ["10.0.0.0/8"]}, allow_internet_access=False)
    assert by_network.feature == NETWORK_FEATURE
    assert by_network.policy.deny_out == ("10.0.0.0/8", ALL_TRAFFIC)
    unrestricted = plan_network_launch(None, allow_internet_access=True)
    assert unrestricted.enforce is False
    assert unrestricted.stored_policy is None
    assert by_network.stored_policy == by_network.policy


def test_launch_plan_validates_before_any_call() -> None:
    with pytest.raises(InvalidArgumentException, match=r"deny_out\[0\]"):
        plan_network_launch({"deny_out": ["example.com"]}, allow_internet_access=True)


def test_update_policy_merges_only_an_explicit_false() -> None:
    assert update_policy(None, None) == NetworkPolicy()
    assert update_policy({}, True) == NetworkPolicy()
    assert update_policy(None, False) == NetworkPolicy(deny_out=(ALL_TRAFFIC,))


def test_allow_only_notice_is_logged_once(caplog: pytest.LogCaptureFixture) -> None:
    log_allow_only_notice.cache_clear()
    caplog.set_level(logging.INFO, logger="rayito.network")
    plan_network_launch({"allow_out": ["secret.example.com"]}, allow_internet_access=True)
    update_policy({"allow_out": ["secret.example.com"]}, None)
    notices = [record for record in caplog.records if record.name == "rayito.network"]
    assert len(notices) == 1
    assert "allow_out sin deny_out" in notices[0].getMessage()
    assert "secret" not in caplog.text


# ------------------------------------------------------------- RPC errors


def test_failed_precondition_becomes_unimplemented_naming_caps() -> None:
    error = network_rpc_error(
        FakeRpcError(
            grpc.StatusCode.FAILED_PRECONDITION, details="la imagen no tiene CAP_NET_ADMIN"
        )
    )
    assert isinstance(error, UnimplementedError)
    assert error.feature == UPDATE_NETWORK_FEATURE
    assert "rayito-base-caps" in error.reason


def test_unimplemented_names_an_m9_caps_image() -> None:
    error = network_rpc_error(
        FakeRpcError(grpc.StatusCode.UNIMPLEMENTED, details="UpdateNetwork"), feature="get_network"
    )
    assert isinstance(error, UnimplementedError)
    assert error.feature == "get_network"
    assert "una imagen M9 de rayito-base-caps" in error.reason


def test_other_codes_follow_the_unary_table() -> None:
    invalid = network_rpc_error(
        FakeRpcError(grpc.StatusCode.INVALID_ARGUMENT, details="deny_out[1]: nombres de host")
    )
    assert isinstance(invalid, InvalidArgumentException)
    internal = network_rpc_error(
        FakeRpcError(grpc.StatusCode.INTERNAL, details="egress_update_failed: fill_table")
    )
    assert type(internal) is SandboxException
    assert internal.grpc_code is grpc.StatusCode.INTERNAL
