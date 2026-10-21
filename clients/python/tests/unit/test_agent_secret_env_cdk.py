"""`RayitoAgent` con un proyecto que usa `secret_env` (ADR-033 D4): el
runtime lleva `RAYITO_SECRET_ENV` (sólo nombres y ARNs) y la pila anidada pasa
los secretos referenciados en `ReadableSecretArns`, sin ninguna sentencia nueva
de IAM. Sintetizado sin AWS."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("aws_cdk")

from rayito._agent._project_domain import AgentProject
from rayito._limits import AGENT_SECRET_ENV_VARIABLE
from rayito.cdk import RayitoAgent
from tests.unit.test_cdk_agent import MODEL, STRANDS, new_stack, of_type, templates

SECRET_ENV = {"DATABASE_URL": "app/db-url"}
SECRET_ENV_FROM = [{"secret": "app/config", "prefix": "APP_", "keys": ["DB_HOST"]}]


def _project_with_secret_env(tmp_path: Path) -> Path:
    directory = tmp_path / "strands"
    shutil.copytree(STRANDS, directory)
    detected, _ = AgentProject.detect(directory, region="us-east-1")
    document = detected.to_dict()
    document["secret_env"] = SECRET_ENV
    document["secret_env_from"] = SECRET_ENV_FROM
    (directory / "rayito-agent.json").write_text(json.dumps(document), encoding="utf-8")
    return directory


def _runtime_environment(root: dict[str, Any]) -> dict[str, str]:
    (runtime,) = of_type(root, "AWS::BedrockAgentCore::Runtime").values()
    return dict(runtime["Properties"]["EnvironmentVariables"])


def test_the_construct_carries_the_spec_and_the_readable_secrets(tmp_path: Path) -> None:
    _, stack = new_stack()
    RayitoAgent(stack, "Agent", project=_project_with_secret_env(tmp_path), bedrock_models=[MODEL])
    root, nested = templates(stack)
    spec = json.loads(_runtime_environment(root)[AGENT_SECRET_ENV_VARIABLE])
    assert spec["env"] == {"DATABASE_URL": {"secret": "app/db-url"}}
    assert spec["from"] == SECRET_ENV_FROM
    policy = nested["Resources"]["RuntimeRole"]["Properties"]["Policies"][0]
    statements = policy["PolicyDocument"]["Statement"]
    rendered = json.dumps(statements)
    assert "secret:app/db-url-??????" in rendered
    assert "secret:app/config-??????" in rendered
    assert "secretsmanager:*" not in rendered and "kms:" not in rendered


def test_without_secret_env_the_runtime_environment_is_unchanged() -> None:
    _, stack = new_stack()
    RayitoAgent(stack, "Agent", project=STRANDS, bedrock_models=[MODEL])
    root, _ = templates(stack)
    assert AGENT_SECRET_ENV_VARIABLE not in _runtime_environment(root)
