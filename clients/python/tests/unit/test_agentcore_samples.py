"""Los quince agentes de ejemplo de 0.14 (`testdata/agent-samples/`) planifican
para AgentCore sin cambiar una línea: cada framework sale con un contexto de
build determinista, y sólo el AI SDK de Vercel pide la herramienta de
credenciales del rol."""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any

import pytest

from rayito import AgentProject, plan_deploy
from rayito._agent._export import render_export

ROOT = Path(__file__).resolve().parents[4] / "testdata" / "agent-samples"
SAMPLES: list[dict[str, Any]] = json.loads((ROOT / "manifest.json").read_text("utf-8"))["samples"]
REGION = "us-east-1"
ENV_CREDENTIAL_SAMPLES = {"typescript-vercel-ai-sdk"}


@pytest.fixture(autouse=True)
def quiet() -> Any:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        yield


@pytest.mark.parametrize("sample", SAMPLES, ids=lambda sample: sample["id"])
def test_every_sample_plans_for_agentcore(sample: dict[str, Any]) -> None:
    plan = plan_deploy(ROOT / sample["dir"], region=REGION, target="agentcore")
    assert plan.region == REGION and plan.network == "PUBLIC"
    assert plan.security[0] == "noGateway" and plan.security[1] == "bedrockModels"
    assert (plan.security[-1] == "envCredentials") == (sample["id"] in ENV_CREDENTIAL_SAMPLES)
    assert plan.warnings == ()
    assert plan.image_tag.startswith("src-") and len(plan.image_tag) == 20
    assert dict(plan.environment) == {"AWS_DEFAULT_REGION": REGION, "AWS_REGION": REGION}


@pytest.mark.parametrize("sample", SAMPLES, ids=lambda sample: sample["id"])
def test_the_build_context_is_the_same_every_time(sample: dict[str, Any]) -> None:
    project, _ = AgentProject.detect(ROOT / sample["dir"], region=REGION)
    first = render_export(project, target="agentcore")
    assert first == render_export(project, target="agentcore")
    dockerfile = first["Dockerfile"].decode()
    assert "ARG AGENT_BASE_IMAGE=" in dockerfile
    assert "FROM --platform=linux/arm64 ${AGENT_BASE_IMAGE}" in dockerfile
    assert ("aws_env_credentials.py" in first) == (sample["id"] in ENV_CREDENTIAL_SAMPLES)
