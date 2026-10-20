"""`deploy_agent(target="agentcore")` y `deploy_agent_async` de punta a punta
sobre dobles de los puertos (`_agentcore/_deploy.py`): planifica, aplica y
devuelve la flota con la misma API que `AgentFleet`; el progreso llega como
`AgentDeployStep` y las versiones asíncronas hacen lo mismo."""

from __future__ import annotations

import asyncio
import dataclasses
import os
from pathlib import Path

import pytest

from rayito import (
    AgentCoreFleet,
    AgentCoreOptions,
    AgentDeployOptions,
    AgentDeployStep,
    AsyncAgentCoreFleet,
)
from rayito._agent._events import Done
from rayito._agent._project_domain import AgentProject
from rayito._agentcore._deploy import (
    AgentCorePorts,
    apply_request,
    deploy_agentcore,
    deploy_agentcore_async,
    plan_agentcore,
)
from rayito._agentcore._service import list_agents
from rayito._agentcore._service_async import (
    AsyncAgentCoreDeleter,
    AsyncAgentCoreDeployer,
    list_agents_async,
)
from rayito.exceptions import AgentCoreException
from tests.unit import fake_agentcore as fake
from tests.unit.fake_agentcore import DATA
from tests.unit.test_agentcore_invoke import json_body, sse_body
from tests.unit.test_agentcore_zero_cost import project

ENVIRON = {"RAYITO_BUCKET": fake.BUCKET}


def ports(cloud: fake.FakeCloud, body: bytes | None = None) -> AgentCorePorts:
    invoker = fake.FakeInvoker(body or json_body())
    return AgentCorePorts(
        gateways=cloud.gateways,
        invoker=invoker,
        environ=ENVIRON,
        clock=cloud.clock,
        sleep=cloud.clock.sleep,
    )


def test_deploy_returns_a_fleet_that_invokes_with_the_agent_fleet_api() -> None:
    cloud = fake.cloud()
    steps: list[AgentDeployStep] = []
    fleet = deploy_agentcore(
        ports(cloud),
        Path(DATA),
        project=project(),
        options=AgentDeployOptions(on_step=steps.append),
        region=fake.REGION,
    )
    assert isinstance(fleet, AgentCoreFleet)
    assert fleet.name == "support-bot" and fleet.runtime_id == fake.RUNTIME_ID
    assert fleet.invoke("hola").text == "42"
    kinds = [step.kind for step in steps]
    assert kinds[0] == "stackDeploy" and kinds[-1] == "runtimeReady"
    assert all(isinstance(step.detail, str) and step.detail for step in steps)
    assert cloud.recorder.count("create_runtime") == 1


def test_deploy_uses_the_options_of_agentcore_for_the_role_and_the_lifecycle() -> None:
    cloud = fake.cloud()
    deploy_agentcore(
        ports(cloud),
        Path(DATA),
        project=project(),
        region=fake.REGION,
        agentcore=AgentCoreOptions(
            bedrock_models=("amazon.nova-lite-v1:0",),
            readable_secrets=("rayito/agents/support/db",),
            idle_session_timeout_seconds=600,
            max_session_lifetime_seconds=7200,
            tags={"team": "agents"},
        ),
    )
    parameters = cloud.stack.parameters_seen[0]
    assert "amazon.nova-lite-v1:0" in parameters["BedrockModelArns"]
    assert "inference-profile/us.anthropic" in parameters["BedrockModelArns"]
    assert "rayito/agents/support/db" in parameters["ReadableSecretArns"]
    spec = cloud.runtimes.specs[0]
    assert (spec.idle_runtime_session_timeout, spec.max_lifetime) == (600, 7200)
    assert spec.tags["team"] == "agents" and spec.tags["rayito:managed-by"] == "rayito-sdk"
    assert spec.description == "Rayito agent support-bot (rayito.agentcore/1)"


def test_a_failure_is_an_agentcore_exception_with_a_closed_code() -> None:
    cloud = fake.cloud()
    cloud.builder.statuses = ["FAILED"]
    with pytest.raises(AgentCoreException) as raised:
        deploy_agentcore(ports(cloud), Path(DATA), project=project(), region=fake.REGION)
    assert raised.value.code == "image_build_failed"


def test_the_plan_alone_never_touches_the_gateways() -> None:
    cloud = fake.cloud()
    planned = plan_agentcore(Path(DATA), project=project(), region=fake.REGION, environ=ENVIRON)
    assert planned.plan.name == "support-bot" and planned.tree["Dockerfile"]
    assert cloud.recorder.calls == []


SENSITIVE_PATHS = (".env", ".env.local", ".git/config")


def project_with_sensitive_files(root: Path, *, with_sensitive: bool) -> AgentProject:
    source = root / "app"
    source.mkdir()
    (source / "agent.py").write_text("print('hi')\n", encoding="utf-8")
    if with_sensitive:
        for relative in SENSITIVE_PATHS:
            target = source / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("not-a-real-value\n", encoding="utf-8")
    return dataclasses.replace(project(), source=str(source))


def test_the_plan_leaves_secret_files_out_of_the_build_context(tmp_path: Path) -> None:
    clean = tmp_path / "clean"
    dirty = tmp_path / "dirty"
    clean.mkdir()
    dirty.mkdir()
    plans = [
        plan_agentcore(root, project=found, region=fake.REGION, environ=ENVIRON)
        for root, found in (
            (clean, project_with_sensitive_files(clean, with_sensitive=False)),
            (dirty, project_with_sensitive_files(dirty, with_sensitive=True)),
        )
    ]
    for planned in plans:
        assert "app/agent.py" in planned.tree
        assert not [path for path in planned.tree if path.startswith(("app/.env", "app/.git"))]
    assert plans[0].plan.image_tag == plans[1].plan.image_tag


def test_the_plan_reads_the_bucket_from_the_environment_it_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "environ", {})
    planned = plan_agentcore(
        Path(DATA), project=project(), region=fake.REGION, environ={"RAYITO_BUCKET": "other-bucket"}
    )
    assert (planned.plan.bucket, planned.plan.bucket_source) == ("other-bucket", "environment")


def test_deploy_async_returns_the_async_fleet() -> None:
    async def scenario() -> None:
        cloud = fake.cloud()
        fleet = await deploy_agentcore_async(
            ports(cloud), Path(DATA), project=project(), region=fake.REGION
        )
        assert isinstance(fleet, AsyncAgentCoreFleet)
        assert (await fleet.invoke("hola")).text == "42"
        assert (await fleet.status()).status == "READY"

    asyncio.run(scenario())


def test_the_async_fleet_streams_what_was_deployed() -> None:
    async def scenario() -> None:
        cloud = fake.cloud()
        fleet = await deploy_agentcore_async(
            ports(cloud, sse_body()), Path(DATA), project=project(), region=fake.REGION
        )
        stream = await fleet.stream("hola")
        async with stream as events:
            collected = [event async for event in events]
        assert isinstance(collected[-1], Done)

    asyncio.run(scenario())


def test_the_async_deployer_and_deleter_delegate_to_the_sync_service() -> None:
    async def scenario() -> None:
        cloud = fake.cloud()
        planned = plan_agentcore(Path(DATA), project=project(), region=fake.REGION, environ=ENVIRON)
        deployer = AsyncAgentCoreDeployer(
            cloud.gateways, clock=cloud.clock, sleep=cloud.clock.sleep
        )
        deployment = await deployer.apply(planned.plan, apply_request(planned, None))
        assert deployment.status == "READY"
        items = await list_agents_async(cloud.runtimes)
        assert (
            [item.name for item in items]
            == ["support-bot"]
            == [item.name for item in list_agents(cloud.runtimes)]
        )
        deleter = AsyncAgentCoreDeleter(cloud.gateways)
        plan = await deleter.plan(deployment.name, deployment.runtime_name, deployment.stack_name)
        result = await deleter.delete(plan)
        assert (result.runtime_deleted, result.stack_deleted) == (True, True)

    asyncio.run(scenario())


def test_fleet_delete_removes_the_agent_it_deployed() -> None:
    cloud = fake.cloud()
    fleet = deploy_agentcore(ports(cloud), Path(DATA), project=project(), region=fake.REGION)
    result = fleet.delete()
    assert (result.name, result.runtime_deleted, result.stack_deleted) == (
        "support-bot",
        True,
        True,
    )
    assert cloud.runtimes.deleted == [fake.RUNTIME_ID]
