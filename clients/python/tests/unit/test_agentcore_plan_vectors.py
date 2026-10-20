"""Los vectores del plan de `--target agentcore`
(`testdata/agentcore-deploy/plan-vectors.json`): el plan, o el error con sus
campos, y que planificar no crea ningún cliente de AWS."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import boto3
import pytest

from rayito import (
    AgentCoreOptions,
    AgentDeployOptions,
    AgentProject,
    ScalingOptions,
    plan_deploy,
)
from rayito._agentcore._messages import SECURITY_TEXTS, WARNING_TEXTS
from rayito.exceptions import AgentCoreException
from tests.unit.fake_agentcore import DATA

CASES: list[dict[str, Any]] = json.loads((DATA / "plan-vectors.json").read_text("utf-8"))["cases"]
SCALING_FIELDS = {"minVms": "min_vms", "maxVms": "max_vms", "warmVms": "warm_vms"}


def scaling_of(call: Mapping[str, Any]) -> ScalingOptions:
    """Un `ScalingOptions` con los valores del vector, sin la validación del
    constructor: un vector sólo pone el campo que prueba."""
    scaling = ScalingOptions()
    for key, value in call.items():
        object.__setattr__(scaling, SCALING_FIELDS.get(key, key), value)
    return scaling


def options_of(call: Mapping[str, Any]) -> AgentDeployOptions:
    kwargs: dict[str, Any] = {}
    if "scaling" in call:
        kwargs["scaling"] = scaling_of(call["scaling"])
    for key, name in (
        ("secretNames", "secret_names"),
        ("accessToken", "access_token"),
        ("keepOnFailure", "keep_on_failure"),
        ("bucket", "bucket"),
    ):
        if key in call:
            kwargs[name] = call[key]
    return AgentDeployOptions(**kwargs)


def agentcore_of(call: Mapping[str, Any]) -> AgentCoreOptions | None:
    raw = call.get("agentcore")
    if raw is None:
        return None
    return AgentCoreOptions(
        bedrock_models=tuple(raw.get("bedrockModels", ())),
        readable_secrets=tuple(raw.get("readableSecrets", ())),
        idle_session_timeout_seconds=raw.get("idleSessionTimeoutSeconds"),
        max_session_lifetime_seconds=raw.get("maxSessionLifetimeSeconds"),
        tags=raw.get("tags", {}),
    )


@pytest.fixture(autouse=True)
def no_aws_client(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    created: list[str] = []

    def forbidden(self: Any, service: str, *args: Any, **kwargs: Any) -> None:
        created.append(service)
        raise AssertionError(f"planificar no debe crear el cliente {service}")

    monkeypatch.setattr(boto3.session.Session, "client", forbidden)
    return created


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_the_plan_vectors(case: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "environ", dict(case["environ"]))
    project = AgentProject.from_dict(case["project"], base_dir=str(DATA))
    call = case["call"]
    expected = case["expected"]

    def run() -> Any:
        return plan_deploy(
            project=project,
            options=options_of(call),
            region=case["region"],
            target=case["target"],
            agentcore=agentcore_of(call),
        )

    if "error" in expected:
        with pytest.raises(AgentCoreException) as raised:
            run()
        error = expected["error"]
        assert raised.value.code == error["code"]
        for key, value in error.items():
            if key != "code":
                assert raised.value.fields[key] == value
        return
    plan = run()
    assert plan.target == expected["target"]
    assert (plan.name, plan.region) == (expected["name"], expected["region"])
    assert plan.runtime_name == expected["runtimeName"]
    assert plan.stack_name == expected["stackName"]
    assert plan.repository_name == expected["repositoryName"]
    assert plan.build_project_name == expected["buildProjectName"]
    assert (plan.bucket, plan.bucket_source) == (expected["bucket"], expected["bucketSource"])
    assert plan.network == expected["network"]
    assert list(plan.bedrock_models) == expected["bedrockModels"]
    assert list(plan.readable_secrets) == expected["readableSecrets"]
    assert dict(plan.environment) == expected["environment"]
    assert (
        plan.lifecycle.idle_session_timeout_seconds
        == (expected["lifecycle"]["idleSessionTimeoutSeconds"])
    )
    assert (
        plan.lifecycle.max_session_lifetime_seconds
        == (expected["lifecycle"]["maxSessionLifetimeSeconds"])
    )
    assert dict(plan.tags) == expected["tags"]
    assert [
        {"resource": s.resource, "name": s.name, "action": s.action, "status": s.status}
        for s in plan.steps
    ] == expected["steps"]
    assert list(plan.security) == expected["security"]
    assert list(plan.warnings) == expected["warnings"]
    assert set(plan.security) <= set(SECURITY_TEXTS)
    assert set(plan.warnings) <= set(WARNING_TEXTS)
    assert plan.image_tag.startswith("src-")
    assert plan.cost


def test_the_plan_is_the_same_for_the_same_project(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "environ", {})
    project = AgentProject.from_dict(CASES[0]["project"], base_dir=str(Path(DATA)))
    first = plan_deploy(project=project, region="us-east-1", target="agentcore")
    second = plan_deploy(project=project, region="us-east-1", target="agentcore")
    assert first == second
    assert first.bucket is None and first.bucket_source == "discover"
