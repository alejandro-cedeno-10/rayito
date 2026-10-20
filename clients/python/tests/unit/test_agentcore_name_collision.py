"""`support-bot` y `support_bot` dan el mismo runtime y la misma pila: todo
nombre derivado sale de la forma canónica y el segundo agente falla con
`name_conflict` en vez de pisar al primero."""

from __future__ import annotations

import pytest

from rayito._agentcore import _domain as domain
from rayito._agentcore._models import RuntimeListing
from rayito._agentcore._service import AgentCoreDeleter, AgentCoreDeployer
from rayito._stacks._condition import StackObservation
from rayito.exceptions import AgentCoreException
from tests.unit import fake_agentcore as fake

OTHER = "support_bot"


def test_every_derived_name_comes_from_the_canonical_form() -> None:
    assert domain.canonical_agent_name(OTHER) == fake.AGENT
    assert domain.stack_name_for(OTHER) == domain.stack_name_for(fake.AGENT) == fake.STACK_NAME
    assert domain.runtime_name_for(OTHER) == fake.RUNTIME_NAME
    assert domain.repository_name_for(OTHER) == fake.REPOSITORY_NAME
    assert domain.build_project_name_for(OTHER) == fake.STACK_NAME
    assert domain.source_key_for(OTHER, "t") == domain.source_key_for(fake.AGENT, "t")


def test_hyphen_only_names_keep_the_names_they_always_had() -> None:
    assert domain.stack_name_for("a-b") == "rayito-agentcore-a-b"
    assert domain.runtime_name_for("a-b") == "rayito_a_b"
    assert domain.repository_name_for("a-b") == "rayito/agentcore/a-b"
    assert domain.source_key_for("a-b", "t").split("/")[-2] == "a-b"


def _owned_by(name: str) -> RuntimeListing:
    return RuntimeListing(
        fake.RUNTIME_ID, "arn", fake.RUNTIME_NAME, "1", "READY", fake.runtime_description(name)
    )


def test_a_runtime_of_the_other_spelling_is_a_name_conflict_before_anything_is_written() -> None:
    cloud = fake.cloud(listing=_owned_by(fake.AGENT))
    deployer = AgentCoreDeployer(cloud.gateways, clock=cloud.clock, sleep=cloud.clock.sleep)
    plan = fake.make_plan()
    other = type(plan)(**{**plan.__dict__, "name": OTHER})
    with pytest.raises(AgentCoreException) as raised:
        deployer.apply(other, fake.make_request())
    assert raised.value.code == "name_conflict"
    assert not {"create_stack", "update_stack", "put_object", "start_build"} & set(
        cloud.recorder.names()
    )


def test_a_stack_of_the_other_spelling_is_a_name_conflict_even_without_a_runtime() -> None:
    parameters = {domain.STACK_PARAMETER_AGENT_DISPLAY_NAME: fake.AGENT}
    stack = StackObservation(
        fake.STACK_NAME, "CREATE_COMPLETE", dict(fake.DEFAULT_OUTPUTS), parameters
    )
    cloud = fake.cloud(stack=stack)
    deployer = AgentCoreDeployer(cloud.gateways, clock=cloud.clock, sleep=cloud.clock.sleep)
    plan = fake.make_plan()
    other = type(plan)(**{**plan.__dict__, "name": OTHER})
    with pytest.raises(AgentCoreException) as raised:
        deployer.apply(other, fake.make_request())
    assert raised.value.code == "name_conflict"
    assert "update_stack" not in cloud.recorder.names()


def test_the_stack_records_the_name_as_written_and_names_resources_from_the_canonical() -> None:
    cloud = fake.cloud()
    deployer = AgentCoreDeployer(cloud.gateways, clock=cloud.clock, sleep=cloud.clock.sleep)
    plan = fake.make_plan()
    other = type(plan)(**{**plan.__dict__, "name": OTHER})
    deployer.apply(other, fake.make_request())
    seen = cloud.stack.parameters_seen[0]
    assert seen[domain.STACK_PARAMETER_AGENT_NAME] == fake.AGENT
    assert seen[domain.STACK_PARAMETER_AGENT_DISPLAY_NAME] == OTHER


def test_delete_never_touches_the_resources_of_the_other_spelling() -> None:
    stack = StackObservation(
        fake.STACK_NAME,
        "CREATE_COMPLETE",
        dict(fake.DEFAULT_OUTPUTS),
        {domain.STACK_PARAMETER_AGENT_DISPLAY_NAME: fake.AGENT},
    )
    cloud = fake.cloud(stack=stack, listing=_owned_by(fake.AGENT))
    deleter = AgentCoreDeleter(cloud.gateways, clock=cloud.clock, sleep=cloud.clock.sleep)
    with pytest.raises(AgentCoreException) as raised:
        deleter.plan(OTHER, fake.RUNTIME_NAME, fake.STACK_NAME)
    assert raised.value.code == "name_conflict"
    assert not {"delete_runtime", "delete_stack"} & set(cloud.recorder.names())
