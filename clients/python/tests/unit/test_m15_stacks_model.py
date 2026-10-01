"""`rayito._stacks._model`: dominio puro del convenio `OptionalStack`."""

from __future__ import annotations

from rayito._stacks._model import (
    CostStatement,
    StackComponent,
    StackStatus,
    plan_deploy,
    stack_tags,
)
from rayito._version import __version__


def component(name: str = "widget") -> StackComponent:
    return StackComponent(
        name=name, description="x", cost=CostStatement(creates=(), idle_monthly="$0")
    )


def test_default_stack_name_is_prefixed() -> None:
    assert component("metadata-index").default_stack_name == "rayito-metadata-index"


def test_plan_deploy_creates_when_the_stack_does_not_exist() -> None:
    assert plan_deploy(None).action == "create"


def test_plan_deploy_updates_an_existing_stack() -> None:
    status = StackStatus(name="rayito-x", state="CREATE_COMPLETE")
    assert plan_deploy(status).action == "update"


def test_plan_deploy_blocks_a_rollback_complete_stack() -> None:
    status = StackStatus(name="rayito-x", state="ROLLBACK_COMPLETE")
    plan = plan_deploy(status)
    assert plan.action == "blocked"
    assert plan.reason is not None
    assert "ROLLBACK_COMPLETE" in plan.reason
    assert "destroy" in plan.reason


def test_stack_status_exists_tracks_state_presence() -> None:
    assert not StackStatus(name="x", state=None).exists
    assert StackStatus(name="x", state="CREATE_COMPLETE").exists


def test_fixed_tags_always_win_over_user_tags() -> None:
    merged = stack_tags(
        component("metadata-index"),
        {"rayito:component": "spoofed", "team": "platform"},
    )
    assert merged["rayito:component"] == "metadata-index"
    assert merged["rayito:managed-by"] == "rayito-sdk"
    assert merged["rayito:sdk-version"] == __version__
    assert merged["team"] == "platform"


def test_stack_tags_with_no_user_tags_is_just_the_fixed_three() -> None:
    merged = stack_tags(component(), {})
    assert set(merged) == {"rayito:component", "rayito:managed-by", "rayito:sdk-version"}
