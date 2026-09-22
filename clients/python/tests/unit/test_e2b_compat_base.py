"""Helpers puros del shim E2B (`rayito.e2b._compat`): la tabla de kwargs,
los avisos, la conversión de estados/info/métricas/PtySize y `states_for`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from rayito import PtySize as NativePtySize
from rayito import SandboxException
from rayito._models import SandboxInfo as NativeSandboxInfo
from rayito._models import SandboxListItem
from rayito._models import SandboxMetrics as NativeSandboxMetrics
from rayito.e2b import (
    NotFoundException,
    PtySize,
    SandboxQuery,
    SandboxState,
    UnimplementedError,
)
from rayito.e2b._compat import (
    E2B_DEFAULT_TIMEOUT_SECONDS,
    ignored_kwarg_warnings,
    info_from_native,
    map_create_kwargs,
    metrics_from_native,
    normalized_language_or_unimplemented,
    pty_size_to_native,
    sandbox_state_from_aws,
    states_for,
    unimplemented,
)
from rayito.exceptions import InvalidArgumentException

IMAGE_ARN = "arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base"
STARTED_AT = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)


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
    assert mapping.native_kwargs["timeout"] == E2B_DEFAULT_TIMEOUT_SECONDS == 300
    assert mapping.native_kwargs["idle"] is None
    assert mapping.native_kwargs["ingress"] == ["ALL_INGRESS"]
    assert mapping.native_kwargs["egress"] == ["INTERNET_EGRESS"]
    assert mapping.native_kwargs["template"] is None
    assert "request_timeout" not in mapping.native_kwargs
    assert "ready_timeout" not in mapping.native_kwargs


def test_map_create_kwargs_passes_through_and_maps_internet_access() -> None:
    mapping = map_create_kwargs(
        template="img",
        timeout=900,
        metadata={"a": "1"},
        envs={"E": "1"},
        request_timeout=10,
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
    )
    native = mapping.native_kwargs
    assert native["template"] == "img"
    assert native["timeout"] == 900
    assert native["metadata"] == {"a": "1"}
    assert native["envs"] == {"E": "1"}
    assert native["request_timeout"] == 10
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


def test_map_create_kwargs_warns_once_per_ignored_kwarg_without_values() -> None:
    mapping = map_create_kwargs(
        api_key="e2b_secret", domain="e2b.dev", debug=True, proxy="http://p", secure=False
    )
    assert len(mapping.warnings) == 5
    assert [message.split(" ")[0] for message in mapping.warnings] == [
        "api_key",
        "domain",
        "debug",
        "proxy",
        "secure=False",
    ]
    assert not any("e2b_secret" in message or "e2b.dev" in message for message in mapping.warnings)
    assert (
        ignored_kwarg_warnings(api_key=None, domain=None, debug=False, proxy=None, secure=True)
        == ()
    )


def test_allow_internet_access_false_is_unimplemented() -> None:
    """Q44 (medido 2026-09-16): sin `egressNetworkConnectors` el MicroVM hereda
    el conector de la versión de imagen y sigue saliendo a internet."""
    with pytest.raises(UnimplementedError) as excinfo:
        map_create_kwargs(allow_internet_access=False)
    assert excinfo.value.feature == "allow_internet_access=False"
    assert "Q44" in excinfo.value.reason


def test_map_create_kwargs_rejects_idle_and_egress() -> None:
    with pytest.raises(TypeError):
        map_create_kwargs(idle=None)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        map_create_kwargs(egress=["INTERNET_EGRESS"])  # type: ignore[call-arg]


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
    assert info.template_id == IMAGE_ARN
    assert info.name == "rayito-base"
    assert info.metadata == {"a": "1"}
    assert info.started_at == STARTED_AT
    assert info.end_at == STARTED_AT + timedelta(seconds=900)
    assert info.state is SandboxState.RUNNING
    assert info.raw_state == "RUNNING"
    assert info_from_native(native_info("SUSPENDED")).metadata is None


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
    assert info.state is SandboxState.PAUSED
    assert info.raw_state == "SUSPENDED"
    assert info.metadata == {"env": "ci"}


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
        rayito.e2b.Sandbox.create(pool=object())
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
