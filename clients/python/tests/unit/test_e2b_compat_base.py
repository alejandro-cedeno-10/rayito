"""Helpers puros del shim E2B (`rayito.e2b._compat`): la tabla de kwargs 2.x,
el ciclo de vida, la red, la conversión de estados/info/métricas/PtySize,
`states_for` y `list_mapping`."""

from __future__ import annotations

import dataclasses
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import grpc
import pytest

from rayito import PtySize as NativePtySize
from rayito import SandboxException
from rayito._models import (
    ALL_TRAFFIC,
    EgressEnforcement,
    IdlePolicy,
    NetworkSelectorContext,
    NetworkState,
    SandboxLifecycle,
    SandboxListItem,
)
from rayito._models import SandboxInfo as NativeSandboxInfo
from rayito._models import SandboxMetrics as NativeSandboxMetrics
from rayito.e2b import (
    NotFoundException,
    PtySize,
    SandboxInfo,
    SandboxMetrics,
    SandboxQuery,
    SandboxState,
    UnimplementedError,
)
from rayito.e2b import _compat as compat
from rayito.e2b._compat import (
    AVAILABLE_KERNELS_REASON,
    E2B_DEFAULT_MAX_LIFETIME_SECONDS,
    E2B_DEFAULT_TIMEOUT_SECONDS,
    SECURE_FALSE_WARNING,
    ShimLifecycle,
    default_max_lifetime,
    info_from_native,
    map_create_kwargs,
    map_lifecycle,
    map_network,
    map_network_update,
    metrics_from_native,
    normalized_language_or_unimplemented,
    pty_size_to_native,
    sandbox_state_from_aws,
    states_for,
    unimplemented_language,
)
from rayito.e2b._unimplemented import UNIMPLEMENTED_REASONS, unimplemented
from rayito.exceptions import InvalidArgumentException

IMAGE_ARN = "arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base"
STARTED_AT = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)
INTERNET_EGRESS_ARN = (
    "arn:aws:lambda:us-east-1:aws:network-connector:aws-network-connector:INTERNET_EGRESS"
)


def native_info(
    state: str = "RUNNING", metadata: dict[str, str] | None = None
) -> NativeSandboxInfo:
    return NativeSandboxInfo(
        sandbox_id="microvm-1",
        state=state,
        endpoint="host",
        template=IMAGE_ARN,
        template_version="11.0",
        started_at=STARTED_AT,
        maximum_duration_seconds=900,
        metadata=metadata,
    )


def test_map_create_kwargs_defaults_follow_e2b() -> None:
    mapping = map_create_kwargs()
    assert mapping.warnings == ()
    native = mapping.native_kwargs
    assert native["timeout"] == E2B_DEFAULT_TIMEOUT_SECONDS == 300
    assert native["on_timeout"] == "kill"
    assert native["idle"] is None
    assert native["max_lifetime"] == E2B_DEFAULT_MAX_LIFETIME_SECONDS == 3600
    assert "_default_max_lifetime" not in native
    assert native["ingress"] == ["ALL_INGRESS"]
    assert native["egress"] == ["INTERNET_EGRESS"]
    assert native["allow_internet_access"] is True
    assert native["template"] is None
    assert "network" not in native
    assert "request_timeout" not in native
    assert "ready_timeout" not in native


def test_map_create_kwargs_follows_the_2x_positional_order() -> None:
    logger = logging.getLogger("app")
    mapping = map_create_kwargs(
        "tpl", 120, {"a": "1"}, {"K": "v"}, None, None, None, None, None, None, None, logger
    )
    native = mapping.native_kwargs
    assert native["template"] == "tpl"
    assert native["timeout"] == 120
    assert native["metadata"] == {"a": "1"}
    assert native["envs"] == {"K": "v"}
    assert native["logger"] is logger


def test_map_create_kwargs_passes_through_the_native_keywords() -> None:
    mapping = map_create_kwargs(
        template="img",
        timeout=900,
        ingress=["NO_INGRESS"],
        region="us-east-1",
        template_version="2.0",
        execution_role_arn="arn:role",
        allowed_ports=[3000],
        logging="cloudwatch",
        access_token="t",
        ready_timeout=30.0,
        reconnect_timeout=20.0,
        keep_on_failure=True,
        control_plane="plane",
        transport="transport",
        max_lifetime=7200,
    )
    native = mapping.native_kwargs
    assert native["timeout"] == 900
    assert native["max_lifetime"] == 7200
    assert native["egress"] == ["INTERNET_EGRESS"]
    assert native["ingress"] == ["NO_INGRESS"]
    assert native["region"] == "us-east-1"
    assert native["template_version"] == "2.0"
    assert native["execution_role_arn"] == "arn:role"
    assert native["allowed_ports"] == [3000]
    assert native["logging"] == "cloudwatch"
    assert native["access_token"] == "t"
    assert native["ready_timeout"] == 30.0
    assert native["reconnect_timeout"] == 20.0
    assert native["keep_on_failure"] is True
    assert native["control_plane"] == "plane"
    assert native["transport"] == "transport"
    assert mapping.warnings == ()


def test_secure_false_warns_and_secure_none_or_true_do_not() -> None:
    assert map_create_kwargs(secure=False).warnings == (SECURE_FALSE_WARNING,)
    assert map_create_kwargs(secure=None).warnings == ()
    assert map_create_kwargs(secure=True).warnings == ()


def test_allow_internet_access_false_is_forwarded_with_the_connector() -> None:
    """ADR-012: el conector `INTERNET_EGRESS` se queda y la política del
    guest (deny-all) la aplica `rayd`; el shim ya no lo rechaza."""
    native = map_create_kwargs(allow_internet_access=False).native_kwargs
    assert native["allow_internet_access"] is False
    assert native["egress"] == ["INTERNET_EGRESS"]
    assert map_create_kwargs(allow_internet_access=None).native_kwargs["allow_internet_access"]


def test_map_create_kwargs_rejects_idle_and_egress() -> None:
    with pytest.raises(TypeError):
        map_create_kwargs(idle=None)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        map_create_kwargs(egress=["INTERNET_EGRESS"])  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("lifecycle", "auto_pause", "expected"),
    [
        (None, None, ShimLifecycle("kill", False)),
        ({}, None, ShimLifecycle("kill", False)),
        ({"on_timeout": None}, None, ShimLifecycle("kill", False)),
        ({"on_timeout": "kill"}, None, ShimLifecycle("kill", False)),
        ({"on_timeout": "pause"}, None, ShimLifecycle("pause", False)),
        ({"on_timeout": "pause", "auto_resume": True}, None, ShimLifecycle("pause", True)),
        ({"on_timeout": {"action": "pause"}}, None, ShimLifecycle("pause", False)),
        (
            {"on_timeout": {"action": "pause", "keep_memory": True}},
            None,
            ShimLifecycle("pause", False),
        ),
        ({"on_timeout": {"action": "kill"}}, None, ShimLifecycle("kill", False)),
        (None, True, ShimLifecycle("pause", False)),
        (None, False, ShimLifecycle("kill", False)),
    ],
)
def test_lifecycle_mapping_table(
    lifecycle: dict[str, Any] | None, auto_pause: bool | None, expected: ShimLifecycle
) -> None:
    assert map_lifecycle(lifecycle, auto_pause=auto_pause) == expected


@pytest.mark.parametrize(
    "lifecycle",
    [
        {"on_timeout": "freeze"},
        {"on_timeout": {"action": "freeze"}},
        {"on_timeout": "kill", "auto_resume": True},
        {"auto_resume": True},
        {"on_timeout": {"action": "kill", "keep_memory": True}},
        {"on_timeout": {"action": "pause", "extra": 1}},
        {"timeout": 1},
        {"on_timeout": "pause", "auto_resume": "yes"},
        "pause",
    ],
)
def test_lifecycle_validation_follows_e2b(lifecycle: Any) -> None:
    with pytest.raises(InvalidArgumentException):
        map_lifecycle(lifecycle)
    with pytest.raises(InvalidArgumentException):
        map_create_kwargs(lifecycle=lifecycle)


def test_auto_pause_and_lifecycle_do_not_combine() -> None:
    with pytest.raises(InvalidArgumentException, match="auto_pause"):
        map_lifecycle({"on_timeout": "pause"}, auto_pause=True)


def test_keep_memory_false_is_unimplemented() -> None:
    with pytest.raises(UnimplementedError) as excinfo:
        map_create_kwargs(lifecycle={"on_timeout": {"action": "pause", "keep_memory": False}})
    assert excinfo.value.feature == "lifecycle.on_timeout.keep_memory=False"
    assert "suspend-microvm" in str(excinfo.value)
    assert excinfo.value.reason == UNIMPLEMENTED_REASONS["lifecycle.on_timeout.keep_memory=False"]


@pytest.mark.parametrize(
    ("timeout", "expected"),
    [(None, 3600), (60, 3600), (3540, 3600), (3600, 3660), (7200, 7260), (28800, 28800)],
)
def test_default_max_lifetime_is_3600_or_timeout_plus_margin(
    timeout: int | None, expected: int
) -> None:
    native = map_create_kwargs(timeout=timeout).native_kwargs
    assert native["max_lifetime"] == expected
    if timeout is not None:
        assert default_max_lifetime(timeout) == expected


def test_pause_lifecycle_maps_to_an_idle_policy() -> None:
    native = map_create_kwargs(
        timeout=60, lifecycle={"on_timeout": "pause", "auto_resume": True}
    ).native_kwargs
    assert native["on_timeout"] == "pause"
    assert native["idle"] == IdlePolicy(max_idle_seconds=300, auto_resume=True)
    assert native["max_lifetime"] == 3600


def test_network_mapping_passes_the_policy_keys_and_the_e2b_selector() -> None:
    def everything(ctx: NetworkSelectorContext) -> list[str]:
        return [ctx.all_traffic]

    mapped = map_network(
        {
            "allow_out": ["api.example.com"],
            "deny_out": everything,
            "egress_proxy": {"address": "proxy:1080"},
            "allow_public_traffic": False,
        }
    )
    assert mapped is not None
    assert mapped["allow_out"] == ["api.example.com"]
    assert mapped["egress_proxy"] == {"address": "proxy:1080"}
    selector = mapped["deny_out"]
    assert callable(selector) and selector(NetworkSelectorContext()) == [ALL_TRAFFIC]
    assert map_network(None) is None
    assert map_network({"allow_public_traffic": False}) is None
    assert map_create_kwargs(network={"deny_out": [ALL_TRAFFIC]}).native_kwargs["network"] == {
        "deny_out": [ALL_TRAFFIC]
    }


@pytest.mark.parametrize(
    ("network", "feature"),
    [
        ({"rules": {}}, "network.rules"),
        ({"rules": None}, "network.rules"),
        ({"mask_request_host": "x"}, "network.mask_request_host"),
        ({"allow_public_traffic": True}, "network.allow_public_traffic=True"),
    ],
)
def test_network_keys_without_primitive_are_unimplemented(
    network: dict[str, Any], feature: str
) -> None:
    with pytest.raises(UnimplementedError) as excinfo:
        map_network(network)
    assert excinfo.value.feature == feature
    assert excinfo.value.reason == UNIMPLEMENTED_REASONS[feature]
    with pytest.raises(UnimplementedError):
        map_create_kwargs(network=network)


def test_unknown_network_key_is_a_type_error() -> None:
    with pytest.raises(TypeError, match="network: clave desconocida 'firewall'"):
        map_network({"firewall": True})
    with pytest.raises(InvalidArgumentException):
        map_network(["deny_out"])  # type: ignore[arg-type]


def test_https_ports_follow_the_measurement_constant(monkeypatch: pytest.MonkeyPatch) -> None:
    assert map_network({"https_ports": []}) is None
    monkeypatch.setattr(compat, "HTTPS_PORTS_SUPPORTED", False)
    with pytest.raises(UnimplementedError) as excinfo:
        map_network({"https_ports": [8443]})
    assert excinfo.value.feature == "network.https_ports"
    assert "QE2" in excinfo.value.reason
    monkeypatch.setattr(compat, "HTTPS_PORTS_SUPPORTED", True)
    assert map_network({"https_ports": [8443], "deny_out": []}) == {"deny_out": []}
    with pytest.raises(InvalidArgumentException):
        map_network({"https_ports": [0]})
    with pytest.raises(InvalidArgumentException):
        map_network({"https_ports": "8443"})


def test_network_update_splits_the_internet_flag() -> None:
    assert map_network_update({"deny_out": [ALL_TRAFFIC], "allow_internet_access": False}) == (
        {"deny_out": [ALL_TRAFFIC]},
        False,
    )
    assert map_network_update(None) == (None, None)
    with pytest.raises(UnimplementedError, match=r"network\.rules"):
        map_network_update({"rules": {}})
    with pytest.raises(TypeError):
        map_network_update({"mask_request_host": "x"})


@pytest.mark.parametrize(
    ("kwarg", "feature"), [("mcp", "mcp"), ("iam", "iam"), ("volume_mounts", "volume_mounts")]
)
def test_resource_kwargs_are_unimplemented_before_mapping(kwarg: str, feature: str) -> None:
    kwargs: dict[str, Any] = {kwarg: {"x": {}}, "lifecycle": {"on_timeout": "freeze"}}
    with pytest.raises(UnimplementedError) as excinfo:
        map_create_kwargs(**kwargs)
    assert excinfo.value.feature == feature
    assert excinfo.value.reason == UNIMPLEMENTED_REASONS[feature]


@pytest.mark.parametrize(
    ("aws_state", "expected"),
    [
        ("PENDING", SandboxState.RUNNING),
        ("RUNNING", SandboxState.RUNNING),
        ("SUSPENDING", SandboxState.PAUSED),
        ("SUSPENDED", SandboxState.PAUSED),
    ],
)
def test_sandbox_state_from_aws(aws_state: str, expected: SandboxState) -> None:
    assert sandbox_state_from_aws(aws_state, sandbox_id="x") is expected


@pytest.mark.parametrize("aws_state", ["TERMINATING", "TERMINATED"])
def test_terminal_states_are_not_found(aws_state: str) -> None:
    with pytest.raises(NotFoundException, match="microvm-1"):
        info_from_native(native_info(aws_state))


def test_info_from_native_sandbox_info() -> None:
    info = info_from_native(native_info("RUNNING", {"a": "1"}))
    assert info.sandbox_id == "microvm-1"
    assert info.sandbox_domain == "host"
    assert info.template_id == IMAGE_ARN
    assert info.name == "rayito-base"
    assert info.metadata == {"a": "1"}
    assert info.started_at == STARTED_AT
    assert info.end_at == STARTED_AT + timedelta(seconds=900)
    assert info.state is SandboxState.RUNNING
    assert info.raw_state == "RUNNING"
    assert (info.cpu_count, info.memory_mb, info.envd_version) == (None, None, None)
    assert info.lifecycle is None and info.network is None
    assert info.allow_internet_access is None
    assert info.volume_mounts == []
    assert info_from_native(native_info("SUSPENDED")).metadata is None


def test_info_from_native_sandbox_info_field_order() -> None:
    names = [item.name for item in dataclasses.fields(SandboxInfo)]
    assert names == [
        "sandbox_id",
        "sandbox_domain",
        "template_id",
        "name",
        "metadata",
        "started_at",
        "end_at",
        "state",
        "cpu_count",
        "memory_mb",
        "envd_version",
        "allow_internet_access",
        "network",
        "lifecycle",
        "volume_mounts",
        "raw_state",
    ]


def test_info_from_native_reads_guest_facts_and_the_logical_deadline() -> None:
    deadline = STARTED_AT + timedelta(seconds=120)
    lifecycle = SandboxLifecycle(
        phase="active",
        deadline=deadline,
        cap=STARTED_AT + timedelta(seconds=3540),
        timeout_seconds=120,
        on_timeout="pause",
        auto_resume=True,
        extensions=0,
    )
    native = dataclasses.replace(
        native_info(), agent_version="0.3.0", cpu_count=2, memory_mb=1987, lifecycle=lifecycle
    )
    info = info_from_native(native)
    assert (info.cpu_count, info.memory_mb, info.envd_version) == (2, 1987, "0.3.0")
    assert info.end_at == deadline
    assert info.lifecycle == {"on_timeout": "pause", "auto_resume": True}
    unmanaged = dataclasses.replace(lifecycle, phase="unmanaged", deadline=None, cap=None)
    assert info_from_native(dataclasses.replace(native, lifecycle=unmanaged)).lifecycle is None


def network_state(allow: tuple[str, ...], deny: tuple[str, ...]) -> NetworkState:
    return NetworkState(
        allow_out=allow,
        deny_out=deny,
        egress_proxy_configured=False,
        enforcement=EgressEnforcement.GUEST_ROUTES,
        local_proxy_port=None,
    )


@pytest.mark.parametrize(
    ("network", "read", "egress", "expected"),
    [
        (network_state((), (ALL_TRAFFIC,)), True, (INTERNET_EGRESS_ARN,), False),
        (network_state(("1.1.1.1",), (ALL_TRAFFIC,)), True, (INTERNET_EGRESS_ARN,), True),
        (network_state((), ("10.0.0.0/8",)), True, (INTERNET_EGRESS_ARN,), True),
        (network_state((), ()), True, (), True),
        (None, True, (INTERNET_EGRESS_ARN,), True),
        (None, True, (), None),
        (None, False, (INTERNET_EGRESS_ARN,), None),
    ],
)
def test_allow_internet_access_truth_table(
    network: NetworkState | None, read: bool, egress: tuple[str, ...], expected: bool | None
) -> None:
    native = dataclasses.replace(native_info(), egress=egress)
    info = info_from_native(native, network=network, network_read=read)
    assert info.allow_internet_access is expected
    if network is not None:
        assert info.network == {
            "allow_out": list(network.allow_out),
            "deny_out": list(network.deny_out),
        }


def test_info_from_native_list_item_has_no_end_at() -> None:
    item = SandboxListItem(
        sandbox_id="microvm-2",
        state="SUSPENDED",
        template=IMAGE_ARN,
        template_version="11.0",
        started_at=STARTED_AT,
        metadata={"env": "ci"},
    )
    info = info_from_native(item)
    assert info.end_at is None
    assert info.sandbox_domain is None
    assert info.state is SandboxState.PAUSED
    assert info.raw_state == "SUSPENDED"
    assert info.metadata == {"env": "ci"}
    assert info.allow_internet_access is None
    assert info.volume_mounts == []


def test_metrics_from_native_uses_e2b_names_in_bytes() -> None:
    native = NativeSandboxMetrics(
        cpu_used_pct=12.5,
        mem_used_bytes=1,
        mem_total_bytes=2,
        disk_used_bytes=3,
        disk_total_bytes=4,
        cpu_count=1,
        timestamp=STARTED_AT,
    )
    metrics = metrics_from_native(native)
    assert (metrics.mem_used, metrics.mem_total, metrics.disk_used, metrics.disk_total) == (
        1,
        2,
        3,
        4,
    )
    assert metrics.cpu_used_pct == 12.5
    assert metrics.cpu_count == 1
    assert metrics.timestamp == STARTED_AT
    assert metrics.mem_cache == 0
    cached = metrics_from_native(dataclasses.replace(native, mem_cache_bytes=7))
    assert cached.mem_cache == 7


def test_e2b_metrics_positional_construction_keeps_working() -> None:
    metrics = SandboxMetrics(STARTED_AT, 1.0, 1, 2, 3, 4, 5)
    assert metrics.mem_cache == 0


def test_pty_size_field_order_and_conversion() -> None:
    assert PtySize() == PtySize(rows=24, cols=80)
    assert PtySize(40, 120) == PtySize(rows=40, cols=120)
    native = pty_size_to_native(PtySize(rows=40, cols=120))
    assert native == NativePtySize(cols=120, rows=40)
    assert pty_size_to_native(NativePtySize(cols=1, rows=2)) == NativePtySize(cols=1, rows=2)
    with pytest.raises(InvalidArgumentException):
        PtySize(rows=0, cols=80)
    with pytest.raises(InvalidArgumentException, match="PtySize"):
        pty_size_to_native((24, 80))  # type: ignore[arg-type]


def test_states_for_maps_e2b_states_to_aws() -> None:
    assert states_for(None, None) is None
    assert states_for([SandboxState.RUNNING], None) == ("PENDING", "RUNNING")
    assert states_for([SandboxState.PAUSED], None) == ("SUSPENDING", "SUSPENDED")
    assert states_for([SandboxState.RUNNING, SandboxState.PAUSED], None) == (
        "PENDING",
        "RUNNING",
        "SUSPENDING",
        "SUSPENDED",
    )
    assert states_for(None, SandboxQuery(metadata={"a": "1"})) == ("RUNNING",)
    assert states_for([SandboxState.RUNNING], SandboxQuery(metadata={"a": "1"})) == ("RUNNING",)
    assert states_for([SandboxState.PAUSED], SandboxQuery()) == ("SUSPENDING", "SUSPENDED")
    with pytest.raises(UnimplementedError, match="PAUSED"):
        states_for([SandboxState.PAUSED], SandboxQuery(metadata={"a": "1"}))


def test_language_gate_forwards_the_three_kernels_and_refuses_the_rest() -> None:
    assert normalized_language_or_unimplemented(None, "run_code") is None
    assert normalized_language_or_unimplemented("python", "run_code") is None
    assert normalized_language_or_unimplemented("Python", "run_code") is None
    assert normalized_language_or_unimplemented("bash", "run_code") == "bash"
    assert normalized_language_or_unimplemented("JS", "run_code") == "javascript"
    assert normalized_language_or_unimplemented("javascript", "run_code") == "javascript"
    with pytest.raises(UnimplementedError) as excinfo:
        normalized_language_or_unimplemented("r", "run_code")
    assert excinfo.value.feature == "run_code(language='r')"
    assert "rayito-base-poly" in excinfo.value.reason
    assert "bash" in excinfo.value.reason
    with pytest.raises(UnimplementedError):
        normalized_language_or_unimplemented("java", "create_code_context")


def test_language_gate_forwards_typescript_and_its_alias() -> None:
    assert normalized_language_or_unimplemented("ts", "run_code") == "typescript"
    assert normalized_language_or_unimplemented("TypeScript", "run_code") == "typescript"
    assert "typescript" in AVAILABLE_KERNELS_REASON
    assert "rayito-base-poly" in AVAILABLE_KERNELS_REASON


def test_unimplemented_language_maps_only_an_agent_unimplemented() -> None:
    not_shipped = InvalidArgumentException(
        "language typescript is not installed in this image; use rayito-base-poly",
        grpc_code=grpc.StatusCode.UNIMPLEMENTED,
    )
    mapped = unimplemented_language(not_shipped, "run_code", "typescript")
    assert isinstance(mapped, UnimplementedError)
    assert mapped.feature == "run_code(language='typescript')"
    assert "rayito-base-poly" in mapped.reason
    rejected = InvalidArgumentException(
        "language must be one of python, bash, javascript",
        grpc_code=grpc.StatusCode.INVALID_ARGUMENT,
    )
    assert unimplemented_language(rejected, "run_code", "typescript") is None
    assert unimplemented_language(InvalidArgumentException("x"), "run_code", "bash") is None


def test_unimplemented_is_not_a_sandbox_exception() -> None:
    error = unimplemented("set_timeout", "no existe UpdateMicrovm")
    assert isinstance(error, UnimplementedError)
    assert isinstance(error, NotImplementedError)
    assert not isinstance(error, SandboxException)
    assert error.feature == "set_timeout"
    assert error.reason == "no existe UpdateMicrovm"
    assert "set_timeout" in str(error) and "UpdateMicrovm" in str(error)
    assert "e2b-compat.md" in str(error)


# ------------------------------------------------------- shim unaware of pools


def test_shim_rejects_pool_kwarg_as_unknown() -> None:
    import rayito.e2b

    with pytest.raises(TypeError, match="pool"):
        rayito.e2b.Sandbox.create(pool=object())  # type: ignore[call-arg]
    with pytest.raises(TypeError, match="pool"):
        rayito.e2b.Sandbox(pool=object())  # type: ignore[call-arg]


async def test_async_shim_rejects_pool_kwarg_as_unknown() -> None:
    import rayito.e2b

    with pytest.raises(TypeError, match="pool"):
        await rayito.e2b.AsyncSandbox.create(pool=object())  # type: ignore[call-arg]


def test_shim_exports_no_pool_name() -> None:
    import rayito.e2b
    import rayito.e2b.exceptions

    assert not any("Pool" in name for name in rayito.e2b.__all__)
    assert not any("Pool" in name for name in rayito.e2b.exceptions.__all__)
    assert not hasattr(rayito.e2b, "SandboxPool")
