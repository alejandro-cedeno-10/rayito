"""Helpers puros de la reconexión: el calendario `ReconnectPoll`, la
clasificación del motivo del corte, por qué se detiene el sondeo y el mapeo
de `HealthResponse` a `SandboxHealth`."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime

import grpc
import pytest

from rayito import EgressEnforcement, IdlePolicy, SandboxHealth
from rayito._models import SandboxInfo
from rayito._sandbox_base import (
    ACCESS_TOKEN_ENV_VAR,
    DEFAULT_RECONNECT_TIMEOUT_SECONDS,
    GuestFacts,
    ReadinessPoll,
    ReconnectOutcome,
    ReconnectPoll,
    build_launch_plan,
    guest_facts_from_health,
    health_from_proto,
    health_reconnected,
    is_suspending_reason,
    list_states_for_metadata,
    metadata_from_health,
    metadata_matches,
    metadata_probe_failure,
    ready_guest_facts,
    reconnect_failure,
    require_access_token,
    resolve_access_token,
    warn_shared_access_token,
    with_guest_facts,
)
from rayito.exceptions import (
    AuthenticationException,
    InvalidArgumentException,
    SandboxException,
    SandboxNotFoundException,
    SandboxStateException,
)
from rayito.v1 import health_pb2, network_pb2

from .conftest import ACCESS_TOKEN, FakeClock, FakeRpcError

OTHER_ACCESS_TOKEN = "b" + ACCESS_TOKEN[1:]


def info(state: str, *, auto_resume: bool | None = None) -> SandboxInfo:
    idle = None if auto_resume is None else IdlePolicy(auto_resume=auto_resume)
    return SandboxInfo(
        sandbox_id="microvm-x",
        state=state,
        endpoint="host",
        template="arn:aws:lambda:us-east-1:1:microvm-image:img",
        template_version="1.0",
        started_at=datetime(2026, 9, 15, tzinfo=UTC),
        maximum_duration_seconds=900,
        state_reason="because",
        idle=idle,
    )


def test_reconnect_poll_schedule_doubles_to_four_seconds_with_jitter() -> None:
    clock = FakeClock(start=0.0)
    poll = ReconnectPoll(timeout=60, monotonic=clock, random=lambda: 0.5)
    delays = []
    for _ in range(5):
        delay = poll.next_delay()
        delays.append(delay)
        clock.advance(delay)
    assert delays == pytest.approx([0.5, 1.0, 2.0, 4.0, 4.0])
    low = ReconnectPoll(timeout=60, monotonic=clock, random=lambda: 0.0)
    high = ReconnectPoll(timeout=60, monotonic=clock, random=lambda: 1.0)
    assert low.next_delay() == pytest.approx(0.375)
    assert high.next_delay() == pytest.approx(0.625)
    assert poll.elapsed() == pytest.approx(11.5)


def test_reconnect_poll_deadline_state_checks_and_rpc_timeout() -> None:
    clock = FakeClock(start=100.0)
    poll = ReconnectPoll(timeout=10, monotonic=clock, random=lambda: 0.5)
    assert poll.should_check_state() is False
    assert poll.rpc_timeout() == 5.0
    clock.advance(5.0)
    assert poll.should_check_state() is True
    assert poll.should_check_state() is False
    clock.advance(4.0)
    assert poll.timed_out() is False
    assert poll.rpc_timeout() == 1.0
    assert poll.next_delay() <= 1.0
    clock.advance(1.0)
    assert poll.timed_out() is True
    assert poll.rpc_timeout() == 0.5
    assert poll.next_delay() == 0.0
    assert DEFAULT_RECONNECT_TIMEOUT_SECONDS == 60.0


def test_readiness_poll_keeps_its_schedule_without_jitter() -> None:
    clock = FakeClock(start=0.0)
    poll = ReadinessPoll(timeout=60, monotonic=clock, random=lambda: 0.0)
    assert [poll.next_delay() for _ in range(4)] == pytest.approx([0.25, 0.5, 1.0, 2.0])


def test_suspending_reason_classification() -> None:
    assert is_suspending_reason(FakeRpcError(grpc.StatusCode.UNAVAILABLE, details="suspending"))
    assert is_suspending_reason(SandboxStateException("el stream fue cerrado"))
    assert not is_suspending_reason(
        FakeRpcError(grpc.StatusCode.UNAVAILABLE, details="Socket closed")
    )
    assert not is_suspending_reason(SandboxException("otra cosa"))


def test_reconnect_failure_stops_on_terminal_states_and_no_auto_resume() -> None:
    reason = FakeRpcError(grpc.StatusCode.UNAVAILABLE, details="GOAWAY")
    terminal = reconnect_failure(reason, info=info("TERMINATED"))
    assert isinstance(terminal, SandboxNotFoundException)
    assert terminal.__cause__ is reason
    assert isinstance(reconnect_failure(reason, info=info("TERMINATING")), SandboxNotFoundException)
    suspended = reconnect_failure(reason, info=info("SUSPENDED", auto_resume=False))
    assert isinstance(suspended, SandboxStateException)
    assert "resume()" in str(suspended)
    assert isinstance(reconnect_failure(reason, info=info("SUSPENDED")), SandboxStateException)
    assert reconnect_failure(reason, info=info("SUSPENDED", auto_resume=True)) is None
    assert reconnect_failure(reason, info=info("SUSPENDING", auto_resume=True)) is None
    assert reconnect_failure(reason, info=info("RUNNING")) is None
    assert reconnect_failure(reason) is None


def test_reconnect_failure_lets_a_dormant_caller_wait_without_auto_resume() -> None:
    """Un handle que no despierta al VM espera al `resume()` sea cual sea la
    política de idle; sólo un estado terminal lo detiene."""
    reason = FakeRpcError(grpc.StatusCode.UNAVAILABLE, details="suspending")
    assert reconnect_failure(reason, info=info("SUSPENDED", auto_resume=False), wake=False) is None
    assert reconnect_failure(reason, info=info("SUSPENDED"), wake=False) is None
    assert isinstance(
        reconnect_failure(reason, info=info("TERMINATED"), wake=False), SandboxNotFoundException
    )


def test_reconnect_failure_on_timeout_depends_on_the_reason() -> None:
    reset = FakeRpcError(grpc.StatusCode.UNAVAILABLE, details="Socket closed")
    timed_out = reconnect_failure(reset, timeout=12.5)
    assert type(timed_out) is SandboxException
    assert "12.5 s" in str(timed_out)
    assert timed_out.__cause__ is reset
    gate = FakeRpcError(grpc.StatusCode.UNAVAILABLE, details="suspending")
    assert isinstance(reconnect_failure(gate, timeout=3), SandboxStateException)
    assert isinstance(
        reconnect_failure(SandboxStateException("suspending end"), timeout=3),
        SandboxStateException,
    )


def test_health_reconnected_requires_a_new_generation_after_a_suspend() -> None:
    ready = health_pb2.HealthResponse(agent_ready=True, kernel_ready=True, resume_generation=3)
    assert health_reconnected(ready, seen_generation=3, suspending=False) is True
    assert health_reconnected(ready, seen_generation=3, suspending=True) is False
    assert health_reconnected(ready, seen_generation=2, suspending=True) is True
    assert health_reconnected(None, seen_generation=2, suspending=False) is False
    not_ready = health_pb2.HealthResponse(agent_ready=True, kernel_ready=False)
    assert health_reconnected(not_ready, seen_generation=0, suspending=False) is False


def test_health_from_proto_maps_every_field() -> None:
    response = health_pb2.HealthResponse(
        agent_ready=True,
        kernel_ready=True,
        agent_version="0.5.0",
        uptime_ms=1234,
        sandbox_id="microvm-x",
        resume_generation=2,
        clock_offset_ms=-980,
        kernel_state_lost=True,
    )
    assert health_from_proto(response) == SandboxHealth(
        agent_ready=True,
        kernel_ready=True,
        agent_version="0.5.0",
        uptime_ms=1234,
        sandbox_id="microvm-x",
        resume_generation=2,
        clock_offset_ms=-980,
        kernel_state_lost=True,
    )


def test_reconnect_outcome_is_a_frozen_record() -> None:
    outcome = ReconnectOutcome(resumed=True, generation_changed=True, resume_generation=4)
    assert outcome.error is None
    with pytest.raises(AttributeError):
        outcome.resumed = False  # type: ignore[misc]


def test_health_from_proto_copies_metadata() -> None:
    response = health_pb2.HealthResponse(agent_ready=True, metadata={"env": "ci", "run": "1"})
    assert health_from_proto(response).metadata == {"env": "ci", "run": "1"}
    assert metadata_from_health(health_pb2.HealthResponse()) == {}


def test_metadata_matches_is_subset_equality_on_the_requested_keys() -> None:
    candidate = {"env": "ci", "run": "2"}
    assert metadata_matches(candidate, {"env": "ci"})
    assert metadata_matches(candidate, {"env": "ci", "run": "2"})
    assert metadata_matches(candidate, {})
    assert not metadata_matches(candidate, {"env": "CI"})
    assert not metadata_matches(candidate, {"run": "2", "owner": "x"})
    assert not metadata_matches({}, {"env": "ci"})
    assert not metadata_matches(None, {})


def test_list_states_for_metadata_only_accepts_running() -> None:
    assert list_states_for_metadata(None) == ("RUNNING",)
    assert list_states_for_metadata(["RUNNING"]) == ("RUNNING",)
    assert list_states_for_metadata(()) == ("RUNNING",)
    with pytest.raises(InvalidArgumentException, match="RUNNING"):
        list_states_for_metadata(["SUSPENDED"])
    with pytest.raises(InvalidArgumentException, match="despertaría"):
        list_states_for_metadata(["RUNNING", "PENDING"])


def test_metadata_probe_failure_names_the_sandbox_and_keeps_the_cause() -> None:
    cause = FakeRpcError(grpc.StatusCode.UNAVAILABLE, details="502")
    error = metadata_probe_failure("microvm-x", cause)
    assert isinstance(error, SandboxException)
    assert "microvm-x" in str(error)
    assert error.__cause__ is cause


@pytest.fixture
def forgotten_shared_token_warning() -> Iterator[None]:
    warn_shared_access_token.cache_clear()
    yield
    warn_shared_access_token.cache_clear()


@pytest.mark.usefixtures("forgotten_shared_token_warning")
def test_environment_token_warns_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(ACCESS_TOKEN_ENV_VAR, ACCESS_TOKEN)

    with caplog.at_level(logging.WARNING, logger="rayito.sandbox"):
        first = resolve_access_token(None)
        second = resolve_access_token(None)

    assert first == second == ACCESS_TOKEN
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert ACCESS_TOKEN_ENV_VAR in warnings[0].getMessage()
    assert ACCESS_TOKEN not in warnings[0].getMessage()


@pytest.mark.usefixtures("forgotten_shared_token_warning")
def test_explicit_and_generated_tokens_never_warn(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(ACCESS_TOKEN_ENV_VAR, ACCESS_TOKEN)

    with caplog.at_level(logging.WARNING, logger="rayito.sandbox"):
        explicit = resolve_access_token(OTHER_ACCESS_TOKEN)
        monkeypatch.delenv(ACCESS_TOKEN_ENV_VAR)
        generated = resolve_access_token(None)

    assert explicit == OTHER_ACCESS_TOKEN
    assert generated not in {ACCESS_TOKEN, OTHER_ACCESS_TOKEN}
    assert caplog.records == []


@pytest.mark.usefixtures("forgotten_shared_token_warning")
def test_connect_path_never_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(ACCESS_TOKEN_ENV_VAR, ACCESS_TOKEN)

    with caplog.at_level(logging.WARNING, logger="rayito.sandbox"):
        assert require_access_token(None) == ACCESS_TOKEN

    assert caplog.records == []


GUEST_MEMORY_BYTES = 8_405_385_216


def test_health_from_proto_maps_the_guest_facts() -> None:
    response = health_pb2.HealthResponse(cpu_count=2, memory_total_bytes=GUEST_MEMORY_BYTES)
    health = health_from_proto(response)
    assert (health.cpu_count, health.memory_total_bytes) == (2, GUEST_MEMORY_BYTES)
    bare = health_from_proto(health_pb2.HealthResponse())
    assert (bare.cpu_count, bare.memory_total_bytes) == (0, 0)


def test_guest_facts_turn_zeros_and_empty_strings_into_unknown() -> None:
    current = health_pb2.HealthResponse(
        agent_version="0.3.0", cpu_count=2, memory_total_bytes=GUEST_MEMORY_BYTES
    )
    assert guest_facts_from_health(current) == GuestFacts("0.3.0", 2, 8016)
    pre_m9 = health_pb2.HealthResponse(agent_version="0.2.0")
    assert guest_facts_from_health(pre_m9) == GuestFacts("0.2.0", None, None)
    assert guest_facts_from_health(health_pb2.HealthResponse()) == GuestFacts()
    tiny = health_pb2.HealthResponse(memory_total_bytes=1024)
    assert guest_facts_from_health(tiny).memory_mb is None


def test_guest_facts_come_only_from_a_ready_agent() -> None:
    response = health_pb2.HealthResponse(
        agent_ready=True, agent_version="0.3.0", cpu_count=2, memory_total_bytes=GUEST_MEMORY_BYTES
    )
    assert ready_guest_facts(None) == GuestFacts()
    assert ready_guest_facts(response) == GuestFacts("0.3.0", 2, 8016)
    booting = health_pb2.HealthResponse(agent_ready=False, agent_version="0.3.0", cpu_count=2)
    assert ready_guest_facts(booting) == GuestFacts()


def test_with_guest_facts_replaces_only_the_three_fields() -> None:
    base = info("RUNNING")
    filled = with_guest_facts(base, GuestFacts("0.3.0", 2, 8016))
    assert (filled.agent_version, filled.cpu_count, filled.memory_mb) == ("0.3.0", 2, 8016)
    assert filled.sandbox_id == base.sandbox_id and filled.state == base.state
    cleared = with_guest_facts(filled, GuestFacts())
    assert (cleared.agent_version, cleared.cpu_count, cleared.memory_mb) == (None, None, None)


def test_require_access_token_names_the_operation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ACCESS_TOKEN_ENV_VAR, raising=False)
    with pytest.raises(AuthenticationException, match="get_metrics_history"):
        require_access_token(None, operation="get_metrics_history()")
    with pytest.raises(AuthenticationException, match="connect"):
        require_access_token(None)


def test_health_from_proto_fills_egress_enforcement() -> None:
    enforced = health_pb2.HealthResponse(
        egress_enforcement=network_pb2.EGRESS_ENFORCEMENT_GUEST_ROUTES
    )
    assert health_from_proto(enforced).egress_enforcement is EgressEnforcement.GUEST_ROUTES
    older = health_from_proto(health_pb2.HealthResponse())
    assert older.egress_enforcement is EgressEnforcement.UNSPECIFIED


def test_launch_plan_repr_hides_the_access_token_and_the_payload() -> None:
    """`LaunchPlan` lleva el access token y su `LaunchRequest` el
    `runHookPayload` con los `envs`: ninguno sale en `repr()` (logs,
    trazas de pytest, depuradores)."""
    env_secret = "env-secret-value-1234"
    plan = build_launch_plan(
        image_arn="arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base",
        region="us-east-1",
        template_version=None,
        timeout=300,
        idle=None,
        envs={"API_KEY": env_secret},
        execution_role_arn=None,
        allowed_ports=None,
        ingress=None,
        egress=None,
        logging="disabled",
        access_token=ACCESS_TOKEN,
    )
    assert plan.access_token == ACCESS_TOKEN
    assert env_secret in plan.request.run_hook_payload
    text = repr(plan)
    assert ACCESS_TOKEN not in text
    assert env_secret not in text
    assert "access_token" not in text and "run_hook_payload" not in text
    assert "rayito-base" in text
