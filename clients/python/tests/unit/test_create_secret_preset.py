"""`--create-secret` (`ensure_model_secrets`) por el preset de la pasarela y
con el nombre exacto que la pasarela lee, contra
`testdata/agent-deploy/create-secret.json` (el mismo fichero que
`create-secret-preset.test.ts`). Ninguna prueba llama a AWS."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from rayito._agent._aws_resolve import (
    GATEWAY_SECRET_PREFIX,
    ensure_model_secrets,
    gateway_env_var,
    secret_name_for,
)
from rayito._agent._detect_project import GATEWAY_SECRET_NAME_TEMPLATE
from rayito._agent._project_domain import AgentProject
from rayito._secrets import DEFAULT_SECRET_PREFIX, SecretCache, SecretRef, SecretStore
from rayito.exceptions import InvalidArgumentException

VECTORS = json.loads(
    (
        Path(__file__).resolve().parents[4] / "testdata" / "agent-deploy" / "create-secret.json"
    ).read_text(encoding="utf-8")
)
CASES: list[dict[str, Any]] = VECTORS["cases"]


@dataclass
class RecordingStore:
    existing: set[str]
    created: list[tuple[str, str]] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)

    def exists(self, name: str) -> bool:
        self.checked.append(name)
        return name in self.existing

    def create(self, name: str, value: str, *, metadata: Mapping[str, str] | None = None) -> None:
        self.created.append((name, value))
        self.existing.add(name)


def _project(case: Mapping[str, Any]) -> AgentProject:
    document = json.loads(json.dumps(VECTORS["project"]))
    document["model"]["gateway"] = case["gateway"]
    document["gateways"] = {case["gateway"]: case["table"]}
    return AgentProject.from_dict(document, base_dir=".")


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_create_secret_follows_the_vector(case: Mapping[str, Any]) -> None:
    project = _project(case)
    expected = case["expected"]
    store = RecordingStore(existing=set(case["existing"]))
    preset = case["table"]["preset"]
    assert (
        gateway_env_var(case["gateway"], project.gateway_presets.get(case["gateway"]))
        == (expected["envVar"])
    )
    if "error" in expected:
        with pytest.raises(InvalidArgumentException) as raised:
            ensure_model_secrets(
                project,
                secret_names={},
                create_from_env=True,
                store=store,
                environ=case["environment"],
            )
        assert str(raised.value) == expected["error"]
        assert store.checked == expected["existsCalls"]
        return
    resolved = ensure_model_secrets(
        project, secret_names={}, create_from_env=True, store=store, environ=case["environment"]
    )
    assert project.gateway_presets == {case["gateway"]: preset}
    assert store.checked == expected["existsCalls"]
    created = [name for name, _ in store.created]
    assert created == ([] if expected["created"] is None else [expected["created"]])
    if expected["value"] is not None:
        assert store.created[0][1] == expected["value"]
    gateway_secret = _secret_of_gateways(resolved.gateways, case["gateway"])
    assert gateway_secret == expected["gatewaySecret"]
    assert VECTORS["secretPrefix"] + gateway_secret == expected["awsName"]


def _secret_of_gateways(gateways: Mapping[str, Any], gateway: str) -> str:
    (reference,) = gateways[gateway].headers.values()
    return str(reference.name if isinstance(reference, SecretRef) else reference)


def test_the_prefix_is_the_one_the_gateway_reads_with() -> None:
    """La pasarela lee su secreto con la `SecretCache` compartida (prefijo por
    defecto) y `--create-secret` crea con `GATEWAY_SECRET_PREFIX`: el mismo,
    así que nunca hay un `rayito/rayito/` por la creación."""
    assert GATEWAY_SECRET_PREFIX == VECTORS["secretPrefix"] == DEFAULT_SECRET_PREFIX
    assert SecretCache(region="us-east-1").store.prefix == GATEWAY_SECRET_PREFIX
    assert SecretStore(region="us-east-1", prefix=GATEWAY_SECRET_PREFIX).prefix == "rayito/"


def test_detection_writes_the_name_without_the_prefix() -> None:
    assert VECTORS["detectedSecretTemplate"] == GATEWAY_SECRET_NAME_TEMPLATE
    assert GATEWAY_SECRET_NAME_TEMPLATE.format(project="my-agent", provider="bedrock") == (
        secret_name_for("my-agent", "bedrock")
    )
    assert not GATEWAY_SECRET_NAME_TEMPLATE.startswith(DEFAULT_SECRET_PREFIX)


def test_gateway_presets_stay_out_of_the_document() -> None:
    project = _project(CASES[0])
    assert project.gateway_presets == {"model": "bedrock"}
    assert "gateway_presets" not in json.dumps(project.to_dict())
    rebuilt = AgentProject.from_dict(project.to_dict(), base_dir=".")
    assert rebuilt == project


def test_gateway_presets_reject_non_strings() -> None:
    project = _project(CASES[0])
    with pytest.raises(InvalidArgumentException):
        AgentProject(
            name=project.name,
            runtime=project.runtime,
            spec=project.spec,
            gateways=project.gateways,
            gateway_presets={"model": 1},  # type: ignore[dict-item]
        )


def test_values_and_names_never_reach_the_messages() -> None:
    case = next(case for case in CASES if "error" in case["expected"])
    project = _project(case)
    store = RecordingStore(existing=set())
    with pytest.raises(InvalidArgumentException) as raised:
        ensure_model_secrets(
            project, secret_names={}, create_from_env=False, store=store, environ={}
        )
    message = str(raised.value)
    assert "AWS_BEARER_TOKEN_BEDROCK" in message
    assert case["table"]["secret"] not in message
    environ = {"AWS_BEARER_TOKEN_BEDROCK": "abskexample"}
    created = RecordingStore(existing=set())
    ensure_model_secrets(
        project, secret_names={}, create_from_env=True, store=created, environ=environ
    )
    assert "abskexample" not in repr(created.checked)
