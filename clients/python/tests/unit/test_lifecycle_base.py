"""Reglas puras del plazo lógico (ADR-011): plan de lanzamiento, lectura de
`LifecycleState`, extensión de `connect()`, disparador y mensajes."""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import grpc
import pytest

from rayito import IdlePolicy, SandboxInfo, SandboxLifecycle
from rayito._lifecycle_base import (
    LifecycleBlock,
    TimeoutRequest,
    auto_resume_reopen,
    beyond_cap_error,
    connect_extension,
    lifecycle_from_proto,
    older_agent_error,
    pause_trigger_delay,
    resolve_lifecycle,
    translate_set_timeout_error,
    validate_set_timeout_seconds,
)
from rayito._payload import build_run_hook_payload
from rayito._sandbox_base import build_launch_plan, terminal_state_error
from rayito.exceptions import (
    InvalidArgumentException,
    LifecycleUnsupportedException,
    SandboxLifetimeException,
    TimeoutException,
)
from rayito.v1 import health_pb2, lifecycle_pb2

from .conftest import ACCESS_TOKEN, IMAGE_ARN, REGION, STARTED_AT, FakeRpcError

NOW_MS = 1_789_000_000_000
NOW = datetime.fromtimestamp(NOW_MS / 1000, tz=UTC)


def plan_payload(**kwargs: Any) -> tuple[int, dict[str, Any], Any]:
    plan = build_launch_plan(
        image_arn=IMAGE_ARN,
        region=REGION,
        template_version=None,
        envs=None,
        execution_role_arn=None,
        allowed_ports=None,
        ingress=None,
        egress=None,
        logging="disabled",
        access_token=ACCESS_TOKEN,
        **kwargs,
    )
    api = plan.request.to_api()
    payload = json.loads(str(api["runHookPayload"]))
    return int(api["maximumDurationInSeconds"]), payload, api.get("idlePolicy")


def lifecycle(
    phase: Any = "active",
    *,
    deadline_in: float | None = 60.0,
    cap_in: float = 840.0,
    timeout: float = 60.0,
    on_timeout: Any = "kill",
    auto_resume: bool = False,
) -> SandboxLifecycle:
    return SandboxLifecycle(
        phase=phase,
        deadline=None if deadline_in is None else NOW + timedelta(seconds=deadline_in),
        cap=NOW + timedelta(seconds=cap_in),
        timeout_seconds=timeout,
        on_timeout=on_timeout,
        auto_resume=auto_resume,
        extensions=0,
    )


def test_no_block_without_max_lifetime_or_on_timeout() -> None:
    duration, payload, idle = plan_payload(timeout=900, idle=IdlePolicy(max_idle_seconds=120))
    assert duration == 900
    assert "lifecycle" not in payload
    assert idle == {
        "maxIdleDurationSeconds": 120,
        "suspendedDurationSeconds": 780,
        "autoResumeEnabled": True,
    }


def test_kill_block_and_platform_duration() -> None:
    duration, payload, idle = plan_payload(
        timeout=60, max_lifetime=900, on_timeout="kill", idle=None
    )
    assert duration == 900
    assert idle is None
    assert payload["lifecycle"] == {
        "auto_resume": False,
        "cap_s": 900,
        "on_timeout": "kill",
        "timeout_s": 60,
    }


def test_kill_resolves_the_idle_policy_against_max_lifetime() -> None:
    duration, _, idle = plan_payload(
        timeout=60, max_lifetime=900, idle=IdlePolicy(max_idle_seconds=300, auto_resume=True)
    )
    assert duration == 900
    assert idle == {
        "maxIdleDurationSeconds": 300,
        "suspendedDurationSeconds": 600,
        "autoResumeEnabled": True,
    }


def test_default_max_lifetime_is_timeout_plus_margin() -> None:
    for timeout, expected in ((60, 120), (600, 660), (28_800, 28_800)):
        plan = resolve_lifecycle(timeout=timeout, max_lifetime=None, on_timeout="kill", idle=None)
        assert plan.platform_duration == expected
    explicit = resolve_lifecycle(timeout=300, max_lifetime=3600, on_timeout="kill", idle=None)
    assert explicit.platform_duration == 3600
    assert explicit.block == LifecycleBlock(
        timeout_s=300, cap_s=3600, on_timeout="kill", auto_resume=False
    )


def test_resolve_lifecycle_has_no_e2b_default_parameter() -> None:
    assert list(inspect.signature(resolve_lifecycle).parameters) == [
        "timeout",
        "max_lifetime",
        "on_timeout",
        "idle",
    ]


def test_max_lifetime_alone_defaults_to_kill() -> None:
    plan = resolve_lifecycle(timeout=60, max_lifetime=300, on_timeout=None, idle=None)
    assert plan.block is not None
    assert plan.block.on_timeout == "kill"


def test_pause_requires_idle() -> None:
    with pytest.raises(InvalidArgumentException, match="idle=IdlePolicy"):
        resolve_lifecycle(timeout=60, max_lifetime=900, on_timeout="pause", idle=None)


def test_pause_forces_platform_auto_resume_and_keeps_the_logical_flag() -> None:
    duration, payload, idle = plan_payload(
        timeout=60, max_lifetime=900, on_timeout="pause", idle=IdlePolicy(auto_resume=False)
    )
    assert duration == 900
    assert idle == {
        "maxIdleDurationSeconds": 300,
        "suspendedDurationSeconds": 600,
        "autoResumeEnabled": True,
    }
    assert payload["lifecycle"] == {
        "auto_resume": False,
        "cap_s": 900,
        "on_timeout": "pause",
        "timeout_s": 60,
    }


def test_pause_rejects_an_explicit_suspended_duration() -> None:
    with pytest.raises(InvalidArgumentException, match="suspended_duration_seconds"):
        resolve_lifecycle(
            timeout=60,
            max_lifetime=900,
            on_timeout="pause",
            idle=IdlePolicy(suspended_duration_seconds=100),
        )


def test_pause_needs_max_idle_below_max_lifetime() -> None:
    with pytest.raises(InvalidArgumentException, match="max_lifetime=200"):
        resolve_lifecycle(
            timeout=60, max_lifetime=200, on_timeout="pause", idle=IdlePolicy(max_idle_seconds=300)
        )


def test_on_timeout_must_be_kill_or_pause() -> None:
    with pytest.raises(InvalidArgumentException, match="'kill' o 'pause'"):
        resolve_lifecycle(timeout=60, max_lifetime=900, on_timeout="stop", idle=None)


def test_max_lifetime_bounds() -> None:
    with pytest.raises(SandboxLifetimeException, match=r"max_lifetime=28801.*28800"):
        resolve_lifecycle(timeout=60, max_lifetime=28_801, on_timeout="kill", idle=None)
    with pytest.raises(InvalidArgumentException, match=">= 120"):
        resolve_lifecycle(timeout=60, max_lifetime=119, on_timeout="kill", idle=None)
    with pytest.raises(InvalidArgumentException, match="no puede superar max_lifetime"):
        resolve_lifecycle(timeout=901, max_lifetime=900, on_timeout="kill", idle=None)
    with pytest.raises(InvalidArgumentException, match="entero"):
        resolve_lifecycle(timeout=60, max_lifetime=True, on_timeout="kill", idle=None)
    exact = resolve_lifecycle(timeout=120, max_lifetime=120, on_timeout="kill", idle=None)
    assert exact.platform_duration == 120


def test_payload_serialises_the_block_with_sorted_keys() -> None:
    block = LifecycleBlock(timeout_s=60, cap_s=900, on_timeout="kill", auto_resume=False)
    text = build_run_hook_payload(access_token=ACCESS_TOKEN, lifecycle=block)
    assert (
        '"lifecycle":{"auto_resume":false,"cap_s":900,"on_timeout":"kill","timeout_s":60}' in text
    )
    assert '"v":1' in text


def test_lifecycle_from_proto_is_none_on_an_older_agent() -> None:
    assert lifecycle_from_proto(health_pb2.HealthResponse(agent_ready=True)) is None
    unmanaged = lifecycle_from_proto(
        health_pb2.HealthResponse(
            lifecycle=lifecycle_pb2.LifecycleState(phase=lifecycle_pb2.LIFECYCLE_PHASE_UNMANAGED)
        )
    )
    assert unmanaged == SandboxLifecycle(
        phase="unmanaged",
        deadline=None,
        cap=None,
        timeout_seconds=0.0,
        on_timeout=None,
        auto_resume=False,
        extensions=0,
    )
    grace = lifecycle_from_proto(
        health_pb2.HealthResponse(
            lifecycle=lifecycle_pb2.LifecycleState(
                phase=lifecycle_pb2.LIFECYCLE_PHASE_RESUME_GRACE,
                deadline_unix_ms=NOW_MS,
                cap_unix_ms=NOW_MS + 60_000,
                timeout_ms=90_500,
                on_timeout=lifecycle_pb2.TIMEOUT_ACTION_PAUSE,
                auto_resume=True,
                extensions=3,
            )
        )
    )
    assert grace == SandboxLifecycle(
        phase="resume_grace",
        deadline=NOW,
        cap=NOW + timedelta(seconds=60),
        timeout_seconds=90.5,
        on_timeout="pause",
        auto_resume=True,
        extensions=3,
    )


def test_connect_extension_table() -> None:
    assert connect_extension(None, None, NOW_MS) is None
    with pytest.raises(LifecycleUnsupportedException, match="imagen M9"):
        connect_extension(None, 300, NOW_MS)
    unmanaged = lifecycle("unmanaged", deadline_in=None)
    assert connect_extension(unmanaged, None, NOW_MS) is None
    with pytest.raises(InvalidArgumentException, match="max_lifetime"):
        connect_extension(unmanaged, 300, NOW_MS)
    assert connect_extension(lifecycle("active"), None, NOW_MS) is None
    assert connect_extension(lifecycle("active"), 300, NOW_MS) == TimeoutRequest(
        "at_least", 300_000
    )
    for phase in ("resume_grace", "expired"):
        assert connect_extension(lifecycle(phase), 120, NOW_MS) == TimeoutRequest(
            "at_least", 120_000
        )
        assert connect_extension(lifecycle(phase, timeout=60), None, NOW_MS) == TimeoutRequest(
            "at_least", 60_000
        )
        near_cap = lifecycle(phase, timeout=600, cap_in=40)
        assert connect_extension(near_cap, None, NOW_MS) == TimeoutRequest("at_least", 35_000)
        with pytest.raises(SandboxLifetimeException, match=r"reincarnate\(\)"):
            connect_extension(lifecycle(phase, cap_in=5.5), None, NOW_MS)


def test_connect_extension_validates_the_requested_timeout() -> None:
    with pytest.raises(InvalidArgumentException, match=r"connect\(timeout=28801\).*28800"):
        connect_extension(lifecycle("active"), 28_801, NOW_MS)
    with pytest.raises(InvalidArgumentException, match=">= 1"):
        connect_extension(lifecycle("active"), 0, NOW_MS)


def test_pause_trigger_delay() -> None:
    assert pause_trigger_delay(None, NOW_MS) is None
    assert pause_trigger_delay(lifecycle("active", on_timeout="kill"), NOW_MS) is None
    ahead = lifecycle("active", deadline_in=30, on_timeout="pause")
    assert pause_trigger_delay(ahead, NOW_MS) == pytest.approx(31.0)
    passed = lifecycle("active", deadline_in=-5, on_timeout="pause")
    assert pause_trigger_delay(passed, NOW_MS) == pytest.approx(1.0)
    assert pause_trigger_delay(lifecycle("expired", on_timeout="pause"), NOW_MS) == 0.0
    assert pause_trigger_delay(lifecycle("resume_grace", on_timeout="pause"), NOW_MS) is None
    assert pause_trigger_delay(lifecycle("unmanaged", on_timeout=None), NOW_MS) is None


def test_auto_resume_reopen_applies_the_e2b_rule_after_a_resumed_deadline_pause() -> None:
    expired = lifecycle("expired", deadline_in=-3, on_timeout="pause", auto_resume=True)

    def reopen(state: SandboxLifecycle | None, paused: int | None, generation: int) -> Any:
        return auto_resume_reopen(
            state, paused_generation=paused, generation=generation, now_ms=NOW_MS
        )

    assert reopen(expired, 0, 1) == TimeoutRequest("exact", 300_000)
    longer = lifecycle("expired", timeout=600, on_timeout="pause", auto_resume=True)
    assert reopen(longer, 2, 3) == TimeoutRequest("exact", 600_000)
    near_cap = lifecycle("expired", cap_in=100, on_timeout="pause", auto_resume=True)
    assert reopen(near_cap, 0, 1) == TimeoutRequest("exact", 95_000)
    exhausted = lifecycle("expired", cap_in=5.5, on_timeout="pause", auto_resume=True)
    assert reopen(exhausted, 0, 1) is None
    assert reopen(expired, None, 1) is None
    assert reopen(expired, 1, 1) is None
    assert reopen(None, 0, 1) is None
    assert reopen(lifecycle("active", on_timeout="pause", auto_resume=True), 0, 1) is None
    assert reopen(lifecycle("resume_grace", on_timeout="pause", auto_resume=True), 0, 1) is None
    assert reopen(lifecycle("expired", on_timeout="pause"), 0, 1) is None
    assert reopen(lifecycle("expired", on_timeout="kill", auto_resume=True), 0, 1) is None


def test_beyond_cap_message_names_max_lifetime_and_28800() -> None:
    message = str(beyond_cap_error(TimeoutRequest("exact", 2_000_000), NOW_MS))
    assert message.startswith("set_timeout(2000) supera el max_lifetime")
    assert "28800" in message
    assert "reincarnate()" in message
    assert NOW.isoformat(timespec="seconds") in message
    with pytest.raises(InvalidArgumentException, match=r"set_timeout\(28801\).*28800"):
        validate_set_timeout_seconds(28_801)


def test_set_timeout_seconds_validation() -> None:
    assert validate_set_timeout_seconds(1) == 1
    for invalid in (0, -5, True, 1.5, "10"):
        with pytest.raises(InvalidArgumentException):
            validate_set_timeout_seconds(invalid)


def test_set_timeout_rejections_map_to_the_d8_table() -> None:
    exact = TimeoutRequest("exact", 2_000_000)
    beyond = translate_set_timeout_error(
        FakeRpcError(
            grpc.StatusCode.INVALID_ARGUMENT, details=f"timeout beyond cap; cap_unix_ms={NOW_MS}"
        ),
        exact,
    )
    assert isinstance(beyond, InvalidArgumentException)
    assert "max_lifetime" in str(beyond)
    assert NOW.isoformat(timespec="seconds") in str(beyond)
    unmanaged = translate_set_timeout_error(
        FakeRpcError(grpc.StatusCode.FAILED_PRECONDITION, details="lifecycle_unmanaged"), exact
    )
    assert type(unmanaged) is InvalidArgumentException
    assert "max_lifetime" in str(unmanaged)
    older = translate_set_timeout_error(
        FakeRpcError(grpc.StatusCode.UNIMPLEMENTED, details="Method not found"), exact
    )
    assert isinstance(older, LifecycleUnsupportedException)
    expired = translate_set_timeout_error(
        FakeRpcError(grpc.StatusCode.FAILED_PRECONDITION, details="sandbox_timeout"), exact
    )
    assert isinstance(expired, TimeoutException)


def test_older_agent_error_names_template_version_and_the_fix() -> None:
    error = older_agent_error("rayito-base", "0.2.0")
    assert isinstance(error, LifecycleUnsupportedException)
    assert isinstance(error, InvalidArgumentException)
    assert "rayito-base" in str(error)
    assert "0.2.0" in str(error)
    assert "publica una imagen M9" in str(error)


def info_with(logical: SandboxLifecycle | None, state_reason: str | None = None) -> SandboxInfo:
    return SandboxInfo(
        sandbox_id="microvm-1",
        state="TERMINATED" if state_reason else "RUNNING",
        endpoint="x",
        template=IMAGE_ARN,
        template_version="1",
        started_at=STARTED_AT,
        maximum_duration_seconds=900,
        state_reason=state_reason,
        lifecycle=logical,
    )


def test_expires_at_is_the_logical_deadline_when_managed() -> None:
    deadline = STARTED_AT + timedelta(seconds=90)
    logical = SandboxLifecycle(
        phase="active",
        deadline=deadline,
        cap=STARTED_AT + timedelta(seconds=840),
        timeout_seconds=90.0,
        on_timeout="kill",
        auto_resume=False,
        extensions=0,
    )
    info = info_with(logical)
    assert info.expires_at == deadline
    assert info.platform_expires_at == STARTED_AT + timedelta(seconds=900)
    assert info.remaining_seconds(STARTED_AT) == pytest.approx(90.0)
    unmanaged = info_with(lifecycle("unmanaged", deadline_in=None))
    assert unmanaged.expires_at == STARTED_AT + timedelta(seconds=900)
    assert info_with(None).expires_at == STARTED_AT + timedelta(seconds=900)


def test_timed_out_reads_the_exit_code_124() -> None:
    timed_out = info_with(None, "Container Stopped with Exit Code: 124")
    assert timed_out.timed_out is True
    assert "alcanzó su timeout" in str(terminal_state_error(timed_out))
    crashed = info_with(None, "Container Stopped with Exit Code: 0")
    assert crashed.timed_out is False
    assert "alcanzó su timeout" not in str(terminal_state_error(crashed))
