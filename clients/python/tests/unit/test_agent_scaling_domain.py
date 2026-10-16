"""Lógica pura del escalado de agentes: los vectores compartidos con
TypeScript (`testdata/agent-fleet/routing.json`) y las invariantes de
`ScalingOptions`, `ServeOptions` y `DeployOptions`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rayito._agent._deploy_domain import DeployOptions
from rayito._agent._project_domain import ServeOptions
from rayito._agent._scaling_domain import (
    ScalingOptions,
    VmUsage,
    VmView,
    decide_queue,
    decode_handle,
    encode_handle,
    expired_vms,
    is_unhealthy,
    max_sessions_for_memory,
    place_session,
    replacements_needed,
    scale_in_deadlines,
    validate_caller_session_id,
)
from rayito._limits import (
    AGENT_FLEET_UNHEALTHY_PINGS,
    AGENT_SCALING_MAX_VMS_DEFAULT,
    AGENT_SCALING_MIN_VMS_DEFAULT,
    AGENT_SCALING_WARM_VMS_DEFAULT,
    AGENT_SERVICE_QUEUE_MAX,
    POOL_SIZE_MAX,
)
from rayito.exceptions import InvalidArgumentException

VECTORS = json.loads(
    (Path(__file__).resolve().parents[4] / "testdata" / "agent-fleet" / "routing.json").read_text(
        encoding="utf-8"
    )
)


def _case(group: str) -> list[Any]:
    return [pytest.param(case, id=case["case"]) for case in VECTORS[group]]


@pytest.mark.parametrize("entry", VECTORS["memory"], ids=lambda e: str(e["memory_mib"]))
def test_the_session_cap_follows_the_connection_cap_of_the_vm(entry: dict[str, int]) -> None:
    assert max_sessions_for_memory(entry["memory_mib"]) == entry["max_sessions"]


@pytest.mark.parametrize("value", [0, -1, True, "2048", 2.5])
def test_the_session_cap_rejects_a_bad_memory(value: Any) -> None:
    with pytest.raises(InvalidArgumentException):
        max_sessions_for_memory(value)


@pytest.mark.parametrize("entry", VECTORS["handles"]["encode"])
def test_encoding_a_handle_matches_the_vector(entry: dict[str, str]) -> None:
    handle = encode_handle(entry["sandbox_id"], entry["session_id"])
    assert handle == entry["handle"]
    decoded = decode_handle(handle)
    assert decoded is not None
    assert (decoded.sandbox_id, decoded.session_id) == (entry["sandbox_id"], entry["session_id"])


@pytest.mark.parametrize("entry", VECTORS["handles"]["decode"], ids=lambda e: e["value"])
def test_decoding_a_value_matches_the_vector(entry: dict[str, Any]) -> None:
    decoded = decode_handle(entry["value"])
    expected = entry["handle"]
    if expected is None:
        assert decoded is None
    else:
        assert decoded is not None
        assert {"sandbox_id": decoded.sandbox_id, "session_id": decoded.session_id} == expected


@pytest.mark.parametrize("entry", VECTORS["handles"]["encode_invalid"])
def test_an_unroutable_handle_cannot_be_encoded(entry: dict[str, str]) -> None:
    with pytest.raises(InvalidArgumentException):
        encode_handle(entry["sandbox_id"], entry["session_id"])


@pytest.mark.parametrize("value", VECTORS["handles"]["caller_valid"])
def test_a_caller_session_id_is_accepted(value: str) -> None:
    assert validate_caller_session_id(value) == value


@pytest.mark.parametrize("value", VECTORS["handles"]["caller_invalid"])
def test_a_caller_session_id_cannot_fake_the_reserved_prefix(value: str) -> None:
    with pytest.raises(InvalidArgumentException):
        validate_caller_session_id(value)


@pytest.mark.parametrize("case", _case("placement"))
def test_placement_matches_the_vector(case: dict[str, Any]) -> None:
    placement = place_session(
        case["session_id"],
        [VmView(**vm) for vm in case["vms"]],
        ScalingOptions(**case["scaling"]),
        max_concurrency=case["max_concurrency"],
    )
    assert {"action": placement.action, "sandbox_id": placement.sandbox_id} == case["expect"]


@pytest.mark.parametrize("case", _case("queue"))
def test_the_queue_decision_matches_the_vector(case: dict[str, Any]) -> None:
    decision = decide_queue(
        waiting=case["waiting"],
        elapsed_seconds=case["elapsed_seconds"],
        queue_max=case["queue_max"],
        queue_timeout_seconds=case["queue_timeout_seconds"],
        retry_after_seconds=case["retry_after_seconds"],
    )
    assert {"action": decision.action, "wait_seconds": decision.wait_seconds} == case["expect"]


def test_the_queue_bound_is_checked() -> None:
    with pytest.raises(InvalidArgumentException):
        decide_queue(
            waiting=0,
            elapsed_seconds=0,
            queue_max=AGENT_SERVICE_QUEUE_MAX + 1,
            queue_timeout_seconds=30,
            retry_after_seconds=1,
        )


@pytest.mark.parametrize("case", _case("scale_in"))
def test_scale_in_matches_the_vector(case: dict[str, Any]) -> None:
    scaling = ScalingOptions(**case["scaling"])
    deadlines = scale_in_deadlines([VmUsage(**usage) for usage in case["usages"]], scaling)
    assert dict(deadlines) == case["deadlines"]
    assert list(expired_vms(deadlines, case["now"])) == case["expired"]


@pytest.mark.parametrize("entry", VECTORS["health"]["threshold_offsets"], ids=str)
def test_a_vm_is_unhealthy_after_the_ping_threshold(entry: dict[str, Any]) -> None:
    assert is_unhealthy(AGENT_FLEET_UNHEALTHY_PINGS + entry["offset"]) is entry["unhealthy"]


@pytest.mark.parametrize("entry", VECTORS["health"]["replacements"], ids=str)
def test_replacements_bring_the_fleet_back_to_its_minimum(entry: dict[str, int]) -> None:
    scaling = ScalingOptions(min_vms=entry["min_vms"], max_vms=entry["min_vms"])
    assert replacements_needed(entry["healthy_running"], scaling) == entry["needed"]


def test_the_default_scaling_is_a_single_vm() -> None:
    scaling = ScalingOptions()
    assert (scaling.min_vms, scaling.max_vms, scaling.warm_vms) == (
        AGENT_SCALING_MIN_VMS_DEFAULT,
        AGENT_SCALING_MAX_VMS_DEFAULT,
        AGENT_SCALING_WARM_VMS_DEFAULT,
    )
    assert scaling.sessions == "shared"
    assert scaling.routes_by_handle is False


def test_more_than_one_vm_routes_by_handle() -> None:
    assert ScalingOptions(max_vms=2).routes_by_handle is True


@pytest.mark.parametrize(
    "kwargs",
    [
        {"min_vms": -1},
        {"min_vms": 2, "max_vms": 1},
        {"max_vms": 0},
        {"max_vms": POOL_SIZE_MAX + 1},
        {"min_vms": 1, "max_vms": 2, "warm_vms": 2},
        {"sessions": "exclusive"},
        {"scale_in_after_seconds": 1},
        {"scale_in_after_seconds": True},
        {"max_vms": True},
        {"max_vms": 2.0},
    ],
    ids=str,
)
def test_scaling_options_are_validated(kwargs: dict[str, Any]) -> None:
    with pytest.raises(InvalidArgumentException):
        ScalingOptions(**kwargs)


@pytest.mark.parametrize(
    "kwargs", [{"queue_max": -1}, {"queue_max": True}, {"queue_timeout_seconds": 0}], ids=str
)
def test_serve_queue_options_are_validated(kwargs: dict[str, Any]) -> None:
    with pytest.raises(InvalidArgumentException):
        ServeOptions(**kwargs)


def test_serve_queue_defaults_keep_the_zero_queue_behaviour() -> None:
    assert ServeOptions().queue_max == 0


def test_deploy_options_default_to_the_latest_base_and_ask_for_nothing_extra() -> None:
    options = DeployOptions()
    assert options.base_image_version == "latest"
    assert (options.create_secret_from_env, options.dry_run) == (False, False)
    assert options.ensure_base_image is True


def test_deploy_options_hide_the_access_token_in_repr() -> None:
    token = "t" * 32
    assert token not in repr(DeployOptions(access_token=token))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"bucket": "no"},
        {"bucket": "has space"},
        {"bucket": "a/b/c"},
        {"base_image_version": ""},
        {"secret_names": {"OpenAI": "name"}},
        {"secret_names": {"openai": "has space"}},
        {"secret_names": ["openai"]},
        {"dry_run": "yes"},
        {"access_token": "short"},
    ],
    ids=str,
)
def test_deploy_options_are_validated(kwargs: dict[str, Any]) -> None:
    with pytest.raises(InvalidArgumentException):
        DeployOptions(**kwargs)
