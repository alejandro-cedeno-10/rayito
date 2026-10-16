"""La lógica pura de la flota (`_fleet_base.py`, `_scaling_domain.py`): el id de
sesión enrutable, la etiqueta de sesión y la colocación de una sesión nueva,
contra los vectores compartidos con TypeScript (`testdata/agent-fleet/`).
Ninguna prueba llama a AWS."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rayito._agent._events import AgentFailed, AgentResult, Done, TokenUsage
from rayito._agent._fleet_base import (
    AGENT_FLEET_BUSY_RETRY_SECONDS,
    AGENT_FLEET_PING_TTL_SECONDS,
    AGENT_FLEET_QUEUE_TIMEOUT_SECONDS_DEFAULT,
    Member,
    SessionRoute,
    decode_handle,
    deficit,
    encode_handle,
    event_with_handle,
    fleet_settings_of,
    is_lost,
    mark_extras,
    plan_placement,
    pool_config_for,
    record_busy,
    record_ping,
    session_label,
    surplus,
    with_handle,
)
from rayito._agent._project_domain import ServeOptions
from rayito._agent._scaling_domain import ScalingOptions
from rayito._payload import generate_access_token
from rayito.exceptions import InvalidArgumentException
from tests.unit.fake_agent_project import SANDBOX_ID, project

VECTORS = Path(__file__).parents[4] / "testdata" / "agent-fleet"
HANDLES = json.loads((VECTORS / "handles.json").read_text(encoding="utf-8"))
PLACEMENT = json.loads((VECTORS / "placement.json").read_text(encoding="utf-8"))
NOW = 1000.0


def test_every_encode_vector_round_trips() -> None:
    for case in HANDLES["encode"]:
        handle = encode_handle(case["sandboxId"], case["sessionId"])

        assert handle == case["handle"]
        assert decode_handle(handle) == SessionRoute(case["sandboxId"], case["sessionId"])


@pytest.mark.parametrize("case", HANDLES["decode"], ids=lambda case: str(case["value"]))
def test_decode_matches_the_shared_vectors(case: dict[str, Any]) -> None:
    route = case["route"]
    expected = None if route is None else SessionRoute(route["sandboxId"], route["sessionId"])

    assert decode_handle(case["value"]) == expected


@pytest.mark.parametrize("sandbox_id", HANDLES["encodeInvalid"])
def test_a_sandbox_id_that_cannot_be_a_handle_is_rejected(sandbox_id: str) -> None:
    with pytest.raises(InvalidArgumentException):
        encode_handle(sandbox_id, "ses")


def test_session_labels_hash_the_caller_chosen_id() -> None:
    for case in HANDLES["sessionLabels"]:
        assert session_label(case["sessionId"]) == case["label"]
        assert case["sessionId"] not in case["label"]


def _member(data: dict[str, Any]) -> Member:
    age = data["pingAgeSeconds"]
    busy = data["busyRemainingSeconds"]
    return Member(
        sandbox_id=data["id"],
        inflight=data["inflight"],
        health=data["health"],
        bound=set(data["bound"]),
        pinged_at=None if age is None else NOW - age,
        busy_until=0.0 if busy is None else NOW + busy,
    )


@pytest.mark.parametrize("case", PLACEMENT["cases"], ids=lambda case: case["name"])
def test_placement_matches_the_shared_vectors(case: dict[str, Any]) -> None:
    raw = case["scaling"]
    scaling = ScalingOptions(
        min_vms=raw["minVms"],
        max_vms=raw["maxVms"],
        warm_vms=raw["warmVms"],
        sessions=raw["sessions"],
    )
    members = [_member(data) for data in case["members"]]

    plan = plan_placement(members, case["growing"], scaling, case["maxConcurrency"], NOW)

    assert plan.kind == case["expected"]["kind"]
    assert (plan.member.sandbox_id if plan.member else None) == case["expected"]["memberId"]


def test_the_result_and_the_terminal_events_carry_the_handle() -> None:
    usage = TokenUsage()
    result = AgentResult(session_id="ses_1", text="hola", steps=1, usage=usage, exit_code=0)
    done = Done(session_id="ses_1", exit_code=0, usage=usage)
    failed = AgentFailed(reason="timeout", session_id="ses_1")

    assert with_handle(result, SANDBOX_ID).session_id == f"rs1.{SANDBOX_ID}.ses_1"
    assert event_with_handle(done, SANDBOX_ID).session_id == f"rs1.{SANDBOX_ID}.ses_1"  # type: ignore[union-attr]
    assert event_with_handle(failed, SANDBOX_ID).session_id == f"rs1.{SANDBOX_ID}.ses_1"  # type: ignore[union-attr]
    assert event_with_handle(AgentFailed(reason="timeout"), SANDBOX_ID).session_id is None  # type: ignore[union-attr]


def test_pings_count_failures_and_a_healthy_one_resets_them() -> None:
    member = Member("microvm-a")

    for _ in range(2):
        record_ping(member, "unavailable", NOW)
    assert not is_lost(member)
    record_ping(member, "healthy", NOW)
    assert member.failed_pings == 0
    for _ in range(3):
        record_ping(member, "unavailable", NOW)

    assert is_lost(member)


def test_a_busy_answer_marks_the_member_without_counting_a_failure() -> None:
    member = Member("microvm-a")

    record_busy(member, NOW)

    assert member.health == "busy"
    assert member.failed_pings == 0
    assert member.busy_until == NOW + AGENT_FLEET_BUSY_RETRY_SECONDS


def test_a_busy_ping_lasts_until_the_ping_expires_and_a_healthy_one_clears_it() -> None:
    member = Member("microvm-a")

    record_ping(member, "busy", NOW)
    assert member.busy_until == NOW + AGENT_FLEET_PING_TTL_SECONDS
    record_ping(member, "healthy", NOW)

    assert member.busy_until == 0.0


def test_the_fleet_keeps_min_vms_and_marks_the_rest_as_extra() -> None:
    scaling = ScalingOptions(min_vms=2, max_vms=4)
    members = [Member(f"microvm-{i}") for i in range(4)]

    mark_extras(members, scaling)

    assert [member.extra for member in members] == [False, False, True, True]
    assert deficit(1, scaling) == 1
    assert deficit(3, scaling) == 0
    assert [m.sandbox_id for m in surplus(members, ScalingOptions(min_vms=1, max_vms=3))] == [
        "microvm-3"
    ]


def test_scaling_options_default_to_the_single_vm_of_deploy() -> None:
    scaling = ScalingOptions()

    assert (scaling.min_vms, scaling.max_vms, scaling.warm_vms) == (1, 1, 0)
    assert scaling.sessions == "shared"
    assert not scaling.routable
    assert ScalingOptions(max_vms=2).routable


@pytest.mark.parametrize(
    "options",
    [
        {"min_vms": 0},
        {"min_vms": 2, "max_vms": 1},
        {"max_vms": 1, "warm_vms": 1},
        {"max_vms": 65},
        {"sessions": "otra"},
        {"scale_in_after_seconds": 1},
        {"warm_vms": -1},
        {"min_vms": True},
    ],
)
def test_scaling_options_reject_invalid_values(options: dict[str, Any]) -> None:
    with pytest.raises(InvalidArgumentException):
        ScalingOptions(**options)


def test_settings_read_the_project_and_let_an_explicit_scaling_win() -> None:
    the_project = project(serve=ServeOptions(max_concurrency=3))
    explicit = ScalingOptions(max_vms=2)

    settings = fleet_settings_of(the_project, explicit)

    assert settings.scaling is explicit
    assert settings.max_concurrency == 3
    assert settings.queue_timeout_seconds == AGENT_FLEET_QUEUE_TIMEOUT_SECONDS_DEFAULT
    assert fleet_settings_of(None).scaling == ScalingOptions()


def test_the_warm_pool_launches_like_a_served_vm_with_the_fleet_token() -> None:
    the_project = project(allow_internet_access=False, serve=ServeOptions(timeout_seconds=3600))
    scaling = ScalingOptions(min_vms=1, max_vms=3, warm_vms=2)
    token = generate_access_token()

    config = pool_config_for(
        the_project, scaling, token, {"rayito.agent.deployment": "support-bot"}
    )

    assert config.size == 2
    assert config.template == "support-bot"
    assert config.timeout == 3600
    assert config.access_token == token
    assert not config.allow_internet_access
    assert config.metadata == {"rayito.agent.deployment": "support-bot"}
