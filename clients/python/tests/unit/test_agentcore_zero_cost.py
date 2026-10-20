"""Sin `target="agentcore"` no se crea ni se carga ningún cliente de AgentCore,
CodeBuild o ECR, y planificar para AgentCore (la CLI con `--dry-run`) tampoco
llama a AWS (`design.md` D5 y D13)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import boto3
import pytest

from rayito import AgentCoreOptions, AgentDeployPlan, AgentProject, plan_deploy
from rayito._agent._deploy_facade import DeployPorts
from rayito.exceptions import AgentCoreException
from tests.unit.fake_agentcore import DATA
from tests.unit.test_agent_deploy import Harness

AGENTCORE_SERVICES = ("bedrock-agentcore-control", "bedrock-agentcore", "codebuild", "ecr")
AGENTCORE_MODULES = (
    "rayito._agentcore._aws",
    "rayito._agentcore._deploy",
    "rayito._agentcore._domain",
    "rayito._agentcore._fleet",
    "rayito._agentcore._fleet_async",
)


@pytest.fixture
def clients(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    created: list[str] = []
    original = boto3.session.Session.client

    def spy(self: Any, service: str, *args: Any, **kwargs: Any) -> Any:
        created.append(service)
        return original(self, service, *args, **kwargs)

    monkeypatch.setattr(boto3.session.Session, "client", spy)
    return created


def project() -> AgentProject:
    document = {
        "schema": "rayito.agent-project/1",
        "name": "support-bot",
        "runtime": {"kind": "deepagents"},
        "model": {
            "provider": "bedrock",
            "id": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
            "region": "us-east-1",
            "gateway": "bedrock",
        },
        "gateways": {
            "bedrock": {
                "preset": "bedrock",
                "secret": "rayito/bedrock-key",
                "region": "us-east-1",
                "models": ["us.anthropic.claude-haiku-4-5-20251001-v1:0"],
            }
        },
    }
    return AgentProject.from_dict(document, base_dir=str(Path(DATA)))


def test_the_default_plan_never_touches_the_agentcore_machinery(clients: list[str]) -> None:
    plan_deploy(project=project(), region="us-east-1")
    assert not set(clients) & set(AGENTCORE_SERVICES)
    assert clients == []


def test_an_agentcore_plan_makes_no_aws_call_and_loads_no_client(clients: list[str]) -> None:
    plan_deploy(project=project(), region="us-east-1", target="agentcore")
    assert clients == []


def test_the_default_deploy_creates_no_agentcore_client(
    monkeypatch: pytest.MonkeyPatch, clients: list[str]
) -> None:
    harness = Harness("sync", monkeypatch)
    harness.deploy()
    assert not set(clients) & set(AGENTCORE_SERVICES)


def test_the_default_target_never_imports_the_agentcore_adapters() -> None:
    probe = (
        "import json, sys, rayito; "
        "from rayito import plan_deploy; "
        "from rayito._agent._deploy_facade import deploy_agent; "
        "print(json.dumps(sorted(m for m in sys.modules if m.startswith('rayito._agentcore'))))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    loaded = json.loads(result.stdout)
    assert not [module for module in loaded if module in AGENTCORE_MODULES]
    assert "rayito._agentcore._service" not in loaded


def test_agentcore_options_with_the_default_target_fail_before_any_call(
    clients: list[str],
) -> None:
    with pytest.raises(AgentCoreException) as raised:
        plan_deploy(  # type: ignore[call-overload]
            project=project(),
            region="us-east-1",
            agentcore=AgentCoreOptions(idle_session_timeout_seconds=600),
        )
    assert raised.value.code == "option_requires_agentcore"
    assert raised.value.fields == {"option": "agentcore"}
    assert clients == []


def test_an_unknown_target_is_an_invalid_argument() -> None:
    with pytest.raises(Exception, match="target"):
        plan_deploy(project=project(), region="us-east-1", target="ecs")  # type: ignore[call-overload]


def test_the_default_plan_is_the_microvm_plan_with_the_same_type() -> None:
    assert isinstance(plan_deploy(project=project(), region="us-east-1"), AgentDeployPlan)
    assert isinstance(
        plan_deploy(project=project(), region="us-east-1", target="microvm"), AgentDeployPlan
    )


def test_deploy_ports_default_is_unchanged() -> None:
    assert DeployPorts().aws is None
