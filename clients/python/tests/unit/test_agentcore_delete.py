"""Borrar un agente de AgentCore toca sólo lo que Rayito creó con ese nombre:
el runtime (y espera a que desaparezca), la pila `rayito-agentcore-<nombre>` y el
grupo de logs del runtime; nunca un runtime ajeno."""

from __future__ import annotations

import pytest

from rayito._agentcore._domain import runtime_log_group_for
from rayito._agentcore._models import RuntimeListing
from rayito._agentcore._service import AgentCoreDeleter, list_agents
from rayito._stacks._condition import StackObservation
from rayito.exceptions import AgentCoreException
from tests.unit import fake_agentcore as fake


def owned(name: str = fake.AGENT, runtime_id: str = fake.RUNTIME_ID) -> RuntimeListing:
    runtime_name = f"rayito_{name.replace('-', '_')}"
    return RuntimeListing(
        runtime_id,
        f"arn:aws:bedrock-agentcore:{fake.REGION}:{fake.ACCOUNT}:runtime/{runtime_id}",
        runtime_name,
        "2",
        "READY",
        fake.runtime_description(name),
    )


def foreign(name: str = "rayito_other") -> RuntimeListing:
    return RuntimeListing("other-0123456789", "arn", name, "1", "READY", "mine")


def deployed() -> fake.FakeCloud:
    stack = StackObservation(fake.STACK_NAME, "CREATE_COMPLETE", dict(fake.DEFAULT_OUTPUTS), {})
    cloud = fake.cloud(stack=stack, listing=owned())
    cloud.runtimes.others = [foreign(), owned("other-bot", "other-bot-0123456789")]
    return cloud


def delete(cloud: fake.FakeCloud) -> tuple[bool, bool, bool]:
    deleter = AgentCoreDeleter(cloud.gateways, clock=cloud.clock, sleep=cloud.clock.sleep)
    plan = deleter.plan(fake.AGENT, fake.RUNTIME_NAME, fake.STACK_NAME)
    result = deleter.delete(plan)
    return result.runtime_deleted, result.stack_deleted, result.log_group_deleted


def test_delete_removes_exactly_the_owned_runtime_stack_and_log_group() -> None:
    cloud = deployed()
    assert delete(cloud) == (True, True, True)
    assert cloud.runtimes.deleted == [fake.RUNTIME_ID]
    assert cloud.recorder.count("delete_stack") == 1
    deleted_groups = [call for call in cloud.recorder.calls if call[0] == "delete_log_group"]
    assert deleted_groups == [("delete_log_group", runtime_log_group_for(fake.RUNTIME_ID))]
    assert cloud.recorder.count("delete_runtime") == 1


def test_the_runtime_goes_before_the_stack_and_the_log_group_last() -> None:
    cloud = deployed()
    delete(cloud)
    names = cloud.recorder.names()
    assert names.index("delete_runtime") < names.index("delete_stack")
    assert names.index("delete_stack") < names.index("delete_log_group")


def test_a_runtime_without_the_marker_is_name_conflict_and_nothing_is_deleted() -> None:
    cloud = fake.cloud(listing=foreign(fake.RUNTIME_NAME))
    deleter = AgentCoreDeleter(cloud.gateways, clock=cloud.clock, sleep=cloud.clock.sleep)
    with pytest.raises(AgentCoreException) as raised:
        deleter.plan(fake.AGENT, fake.RUNTIME_NAME, fake.STACK_NAME)
    assert raised.value.code == "name_conflict"
    assert not {"delete_runtime", "delete_stack", "delete_log_group"} & set(cloud.recorder.names())


def test_nothing_to_delete_is_an_empty_plan_and_no_writes() -> None:
    cloud = fake.cloud()
    deleter = AgentCoreDeleter(cloud.gateways, clock=cloud.clock, sleep=cloud.clock.sleep)
    plan = deleter.plan(fake.AGENT, fake.RUNTIME_NAME, fake.STACK_NAME)
    assert plan.nothing and plan.log_group is None
    result = deleter.delete(plan)
    assert (result.runtime_deleted, result.stack_deleted, result.log_group_deleted) == (
        False,
        False,
        False,
    )
    assert not {"delete_runtime", "delete_stack", "delete_log_group"} & set(cloud.recorder.names())


def test_a_stack_without_a_runtime_is_still_deleted() -> None:
    stack = StackObservation(fake.STACK_NAME, "CREATE_COMPLETE", dict(fake.DEFAULT_OUTPUTS), {})
    cloud = fake.cloud(stack=stack)
    assert delete(cloud) == (False, True, False)


def test_a_runtime_that_never_disappears_is_delete_failed() -> None:
    cloud = deployed()
    cloud.runtimes.ready_after_polls = 10**6
    with pytest.raises(AgentCoreException) as raised:
        delete(cloud)
    assert raised.value.code == "delete_failed"
    assert raised.value.fields["resource"] == "runtime"
    assert cloud.recorder.count("delete_stack") == 0


def test_an_invalid_name_is_name_invalid_before_any_call() -> None:
    cloud = fake.cloud()
    with pytest.raises(AgentCoreException) as raised:
        AgentCoreDeleter(cloud.gateways).plan("Bad.Name", "rayito_bad", "rayito-agentcore-bad")
    assert raised.value.code == "name_invalid" and cloud.recorder.calls == []


def test_progress_lines_name_each_resource() -> None:
    cloud = deployed()
    lines: list[str] = []
    deleter = AgentCoreDeleter(
        cloud.gateways, clock=cloud.clock, sleep=cloud.clock.sleep, on_progress=lines.append
    )
    deleter.delete(deleter.plan(fake.AGENT, fake.RUNTIME_NAME, fake.STACK_NAME))
    assert lines == [
        f"borrando el runtime {fake.RUNTIME_NAME}",
        f"borrando la pila {fake.STACK_NAME}",
        f"borrando el grupo de logs {runtime_log_group_for(fake.RUNTIME_ID)}",
    ]


def test_list_shows_only_runtimes_with_rayitos_prefix_and_marker() -> None:
    cloud = deployed()
    items = list_agents(cloud.runtimes)
    assert [(item.name, item.runtime_name) for item in items] == [
        ("other-bot", "rayito_other_bot"),
        (fake.AGENT, fake.RUNTIME_NAME),
    ]
