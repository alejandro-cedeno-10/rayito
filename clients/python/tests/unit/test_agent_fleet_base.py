"""La lógica pura de la flota (`_fleet_base.py`, `_scaling_domain.py`): el id de
sesión enrutable, la etiqueta de sesión y la colocación de una sesión nueva,
contra los vectores compartidos con TypeScript (`testdata/agent-fleet/`).
Ninguna prueba llama a AWS."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rayito._agent._events import AgentFailed, AgentResult, Done, TokenUsage
from rayito._agent._fleet_base import (
    AGENT_FLEET_BUSY_RETRY_SECONDS,
    FleetLifetime,
    Member,
    MemberListing,
    corrected_cap,
    event_with_handle,
    fleet_lifetime,
    fleet_settings_of,
    hold_seconds,
    is_lost,
    lifetime_seconds,
    listed_cap,
    mark_extras,
    owned_ids,
    plan_placement,
    pool_config_for,
    record_busy,
    record_ping,
    restore_seconds,
    sandbox_cap,
    select_members,
    session_label,
    surplus,
    with_handle,
)
from rayito._agent._project_domain import ServeOptions
from rayito._agent._scaling_domain import ScalingOptions, replacements_needed
from rayito._lifecycle_base import TimeoutRequest, beyond_cap_error
from rayito._limits import (
    AGENT_FLEET_PING_TTL_SECONDS,
    AGENT_SERVICE_QUEUE_TIMEOUT_SECONDS_DEFAULT,
)
from rayito._models import ALL_TRAFFIC, NetworkPolicy
from rayito._payload import generate_access_token
from rayito.exceptions import InvalidArgumentException
from tests.unit.fake_agent_project import SANDBOX_ID, project

VECTORS = Path(__file__).parents[4] / "testdata" / "agent-fleet"
SESSION_LABELS = json.loads((VECTORS / "session-labels.json").read_text(encoding="utf-8"))
PLACEMENT = json.loads((VECTORS / "placement.json").read_text(encoding="utf-8"))
MEMBERS = json.loads((VECTORS / "members.json").read_text(encoding="utf-8"))
LIFETIME = json.loads((VECTORS / "lifetime.json").read_text(encoding="utf-8"))
NOW = 1000.0


def test_session_labels_hash_the_caller_chosen_id() -> None:
    for case in SESSION_LABELS["sessionLabels"]:
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
    assert replacements_needed(1, scaling) == 1
    assert replacements_needed(3, scaling) == 0
    assert [m.sandbox_id for m in surplus(members, ScalingOptions(min_vms=1, max_vms=3))] == [
        "microvm-3"
    ]


def test_scaling_options_default_to_the_single_vm_of_deploy() -> None:
    scaling = ScalingOptions()

    assert (scaling.min_vms, scaling.max_vms, scaling.warm_vms) == (1, 1, 0)
    assert scaling.sessions == "shared"
    assert not scaling.routes_by_handle
    assert ScalingOptions(max_vms=2).routes_by_handle


@pytest.mark.parametrize(
    "options",
    [
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
    assert settings.queue_timeout_seconds == AGENT_SERVICE_QUEUE_TIMEOUT_SECONDS_DEFAULT
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


def test_the_warm_pool_launches_the_pinned_image_version_if_any() -> None:
    scaling = ScalingOptions(min_vms=1, max_vms=3, warm_vms=2)
    token = generate_access_token()

    pinned = pool_config_for(project(), scaling, token, {}, template_version="2.0")

    assert pinned.template_version == "2.0"
    assert pool_config_for(project(), scaling, token, {}).template_version is None


def test_the_warm_pool_carries_the_project_env_and_network() -> None:
    policy = NetworkPolicy(allow_out=("api.example.com",), deny_out=(ALL_TRAFFIC,))
    the_project = project(env={"LOG_LEVEL": "debug"}, network=policy)

    config = pool_config_for(
        the_project, ScalingOptions(max_vms=2, warm_vms=1), generate_access_token(), {}
    )

    assert config.envs == {"LOG_LEVEL": "debug"}
    assert config.network == policy


def test_the_warm_pool_sends_no_env_or_network_by_default() -> None:
    config = pool_config_for(
        project(), ScalingOptions(max_vms=2, warm_vms=1), generate_access_token(), {}
    )

    assert config.envs is None
    assert config.network is None


@dataclass(frozen=True)
class Probed:
    sandbox_id: str
    metadata: dict[str, str] | None


@pytest.mark.parametrize("case", MEMBERS["ownedIds"], ids=lambda case: case["name"])
def test_only_agent_labels_prove_a_vm_belongs_to_a_fleet(case: dict[str, Any]) -> None:
    items = [Probed(item["id"], item["metadata"]) for item in case["items"]]

    assert owned_ids(items) == set(case["expected"])


@pytest.mark.parametrize("case", MEMBERS["selectMembers"], ids=lambda case: case["name"])
def test_a_vm_is_a_member_only_when_the_image_is_not_shared(case: dict[str, Any]) -> None:
    listing = [
        MemberListing(item["id"], item["state"], datetime(2026, 10, 16, tzinfo=UTC))
        for item in case["items"]
    ]

    selected = select_members(listing, set(case["owned"]))

    assert [item.sandbox_id for item in selected] == case["expected"]


# ---------------------------------------------------------------- lifetime


@pytest.mark.parametrize("case", LIFETIME["createArgs"], ids=lambda case: case["name"])
def test_fleet_lifetime_matches_the_shared_vectors(case: dict[str, Any]) -> None:
    the_project = project(serve=ServeOptions(timeout_seconds=case["serveTimeoutSeconds"]))
    scaling = ScalingOptions(min_vms=case["minVms"], max_vms=case["maxVms"])

    lifetime = fleet_lifetime(the_project, scaling)

    expected = case["expected"]
    if expected is None:
        assert lifetime is None
        return
    assert lifetime is not None
    assert lifetime.max_lifetime == expected["maxLifetimeSeconds"]
    assert lifetime.on_timeout == expected["onTimeout"]


@pytest.mark.parametrize("case", LIFETIME["lifetimeSeconds"], ids=lambda case: case["name"])
def test_lifetime_seconds_matches_the_shared_vectors(case: dict[str, Any]) -> None:
    now = datetime(2026, 10, 22, tzinfo=UTC)
    cap_in = case["capInSeconds"]
    cap = None if cap_in is None else now + timedelta(seconds=cap_in)

    assert lifetime_seconds(case["desiredSeconds"], cap, now) == case["expected"]


@pytest.mark.parametrize("case", LIFETIME["restoreSeconds"], ids=lambda case: case["name"])
def test_restore_seconds_matches_the_shared_vectors(case: dict[str, Any]) -> None:
    now = datetime(2026, 10, 22, tzinfo=UTC)
    cap_in = case["capInSeconds"]
    cap = None if cap_in is None else now + timedelta(seconds=cap_in)

    assert restore_seconds(cap, now) == case["expected"]


@pytest.mark.parametrize("case", LIFETIME["holdSeconds"], ids=lambda case: case["name"])
def test_hold_seconds_matches_the_shared_vectors(case: dict[str, Any]) -> None:
    scaling = ScalingOptions(max_vms=2, scale_in_after_seconds=case["scaleInSeconds"])

    assert hold_seconds(scaling, case["requestTimeoutSeconds"]) == case["expected"]


@pytest.mark.parametrize("case", LIFETIME["listedCap"], ids=lambda case: case["name"])
def test_listed_cap_is_rayd_cap_from_the_shared_vectors(case: dict[str, Any]) -> None:
    started = datetime(2026, 10, 22, tzinfo=UTC)
    max_lifetime = case["maxLifetimeSeconds"]
    lifetime = None if max_lifetime is None else FleetLifetime(max_lifetime=max_lifetime)

    cap = listed_cap(started, lifetime)

    expected = case["expectedCapAfterStartSeconds"]
    assert cap == (None if expected is None else started + timedelta(seconds=expected))


@pytest.mark.parametrize("case", LIFETIME["sandboxCap"], ids=lambda case: case["name"])
def test_sandbox_cap_matches_the_shared_vectors(case: dict[str, Any]) -> None:
    now = datetime(2026, 10, 22, tzinfo=UTC)

    def at(seconds: float | None) -> datetime | None:
        return None if seconds is None else now + timedelta(seconds=seconds)

    lifecycle = SimpleNamespace(cap=at(case["reportedCapInSeconds"]))
    info = SimpleNamespace(
        platform_expires_at=at(case["platformCapInSeconds"]), lifecycle=lifecycle
    )

    assert sandbox_cap(SimpleNamespace(info=info)) == at(case["expected"])


@pytest.mark.parametrize("case", LIFETIME["promotedRestore"], ids=lambda case: case["name"])
def test_a_restore_is_bounded_by_rayd_cap_from_the_shared_vectors(case: dict[str, Any]) -> None:
    now = datetime(2026, 10, 22, tzinfo=UTC)
    started = now - timedelta(seconds=case["startedSecondsAgo"])
    cap = listed_cap(started, FleetLifetime(max_lifetime=case["maxLifetimeSeconds"]))

    assert lifetime_seconds(case["desiredSeconds"], cap, now) == case["expected"]


@pytest.mark.parametrize("case", LIFETIME["correctedCap"], ids=lambda case: case["name"])
def test_corrected_cap_matches_the_shared_vectors(case: dict[str, Any]) -> None:
    now = datetime(2026, 10, 22, tzinfo=UTC)

    def at(seconds: float | None) -> datetime | None:
        return None if seconds is None else now + timedelta(seconds=seconds)

    reported = at(case["reportedCapInSeconds"])
    error = beyond_cap_error(
        TimeoutRequest("exact", 1000),
        None if reported is None else int(reported.timestamp() * 1000),
    )

    assert corrected_cap(error, at(case["currentCapInSeconds"])) == at(case["expected"])


def test_the_warm_pool_of_a_fleet_that_can_grow_carries_the_lifetime() -> None:
    config = pool_config_for(
        project(), ScalingOptions(max_vms=3, warm_vms=1), generate_access_token(), {}
    )

    assert config.max_lifetime == 3660
    assert config.on_timeout == "kill"
