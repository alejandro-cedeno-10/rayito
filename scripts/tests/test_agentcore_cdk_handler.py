"""`infra/agentcore-cdk-handler.py` (ADR-032) con clientes falsos: el build se
salta si la etiqueta ya está en ECR, se arranca con los parámetros de
`AWS_API_NOTES.md`, el sondeo distingue en curso, fallo y tamaño excesivo, y
MMDSv2 se exige con `UpdateAgentRuntime` sólo cuando falta. También fija el
zip que `scripts/gen_stack_assets.py` empaqueta para los dos SDK y que
`testdata/agentcore-cdk/contract.json` nombra."""

from __future__ import annotations

import importlib.util
import io
import json
import re
import sys
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HANDLER = REPO_ROOT / "infra" / "agentcore-cdk-handler.py"
PYTHON_ZIP = (
    REPO_ROOT
    / "clients"
    / "python"
    / "src"
    / "rayito"
    / "_stacks"
    / "_artifacts"
    / "agentcore-cdk-handler.zip"
)
CONTRACT = json.loads(
    (REPO_ROOT / "testdata" / "agentcore-cdk" / "contract.json").read_text(encoding="utf-8")
)
CLIENT_TOKEN_PATTERN = re.compile(r"[a-zA-Z0-9](-*[a-zA-Z0-9]){0,256}")
CLIENT_TOKEN_MIN = 33
IMAGE_LIMIT = 2_000_000_000


def load_handler() -> ModuleType:
    spec = importlib.util.spec_from_file_location("agentcore_cdk_handler", HANDLER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ImageNotFound(Exception):
    pass


class FakeEcr:
    def __init__(self, size: int | None) -> None:
        self.size = size
        self.exceptions = type("Exceptions", (), {"ImageNotFoundException": ImageNotFound})

    def describe_images(self, **kwargs: Any) -> dict[str, Any]:
        if self.size is None:
            raise ImageNotFound
        return {"imageDetails": [{"imageSizeInBytes": self.size}]}


class FakeCodeBuild:
    def __init__(self, status: str = "IN_PROGRESS") -> None:
        self.status = status
        self.started: list[dict[str, Any]] = []

    def start_build(self, **kwargs: Any) -> dict[str, Any]:
        self.started.append(kwargs)
        return {"build": {"id": "project:build-1", "buildStatus": "IN_PROGRESS"}}

    def batch_get_builds(self, ids: list[str]) -> dict[str, Any]:
        return {"builds": [{"id": ids[0], "buildStatus": self.status}]}


class FakeControl:
    def __init__(self, *, mmds: bool, status: str = "READY") -> None:
        self.runtime: dict[str, Any] = {
            "agentRuntimeId": "rayito_bot-AbCdEfGhIj",
            "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": "repo:src-1"}},
            "roleArn": "arn:aws:iam::123456789012:role/runtime",
            "networkConfiguration": {"networkMode": "PUBLIC"},
            "protocolConfiguration": {"serverProtocol": "HTTP"},
            "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 900, "maxLifetime": 28800},
            "environmentVariables": {"AWS_REGION": "us-east-1"},
            "description": "Rayito agent bot (rayito.agentcore/1+cdk)",
            "status": status,
            "metadataConfiguration": {"requireMMDSV2": mmds},
        }
        self.updates: list[dict[str, Any]] = []

    def get_agent_runtime(self, agentRuntimeId: str) -> dict[str, Any]:
        return dict(self.runtime)

    def update_agent_runtime(self, **kwargs: Any) -> dict[str, Any]:
        self.updates.append(kwargs)
        return {"agentRuntimeVersion": "2", "status": "UPDATING"}


@pytest.fixture
def handler() -> ModuleType:
    return load_handler()


def install(handler: ModuleType, monkeypatch: pytest.MonkeyPatch, **clients: Any) -> None:
    names = {"codebuild": "codebuild", "ecr": "ecr", "bedrock-agentcore-control": "control"}
    monkeypatch.setattr(handler, "_client", lambda service: clients[names[service]])


def build_event(request: str = "Create", **data: Any) -> dict[str, Any]:
    return {
        "RequestType": request,
        "ResourceType": "Custom::RayitoImageBuild",
        "PhysicalResourceId": "src-0123456789abcdef",
        "ResourceProperties": {
            "ProjectName": "rayito-agentcore-bot",
            "RepositoryName": "rayito/agentcore/bot",
            "ImageTag": "src-0123456789abcdef",
            "SourceLocation": "bucket/key.zip",
            "AgentBaseImage": "public.ecr.aws/docker/library/python@sha256:00",
            "MaxImageBytes": str(IMAGE_LIMIT),
        },
        "Data": data,
    }


def mmds_event(request: str = "Create") -> dict[str, Any]:
    return {
        "RequestType": request,
        "ResourceType": "Custom::RayitoRuntimeMmds",
        "PhysicalResourceId": "rayito_bot-AbCdEfGhIj/mmds",
        "ResourceProperties": {"AgentRuntimeId": "rayito_bot-AbCdEfGhIj", "AgentRuntimeVersion": "1"},
    }


def test_existing_tag_skips_the_build(handler: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    codebuild = FakeCodeBuild()
    install(handler, monkeypatch, codebuild=codebuild, ecr=FakeEcr(10))
    result = handler.on_event(build_event())
    assert result["Data"]["BuildId"] == ""
    assert codebuild.started == []
    assert handler.is_complete({**build_event(), "Data": result["Data"]}) == {
        "IsComplete": True,
        "Data": {"ImageTag": "src-0123456789abcdef"},
    }


def test_missing_tag_starts_the_documented_build(
    handler: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    codebuild = FakeCodeBuild()
    install(handler, monkeypatch, codebuild=codebuild, ecr=FakeEcr(None))
    result = handler.on_event(build_event())
    assert result["PhysicalResourceId"] == "src-0123456789abcdef"
    assert result["Data"]["BuildId"] == "project:build-1"
    (request,) = codebuild.started
    assert set(request) == {
        "projectName",
        "sourceLocationOverride",
        "environmentVariablesOverride",
        "idempotencyToken",
    }
    assert request["idempotencyToken"] == "src-0123456789abcdef"
    assert [variable["name"] for variable in request["environmentVariablesOverride"]] == [
        "IMAGE_TAG",
        "AGENT_BASE_IMAGE",
    ]


def test_build_poll_states(handler: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    install(handler, monkeypatch, codebuild=FakeCodeBuild("IN_PROGRESS"), ecr=FakeEcr(None))
    assert handler.is_complete(build_event(BuildId="b")) == {"IsComplete": False}
    install(handler, monkeypatch, codebuild=FakeCodeBuild("FAILED"), ecr=FakeEcr(None))
    with pytest.raises(handler.HandlerError) as failed:
        handler.is_complete(build_event(BuildId="b"))
    assert failed.value.code == "build_failed"
    install(handler, monkeypatch, codebuild=FakeCodeBuild("SUCCEEDED"), ecr=FakeEcr(IMAGE_LIMIT + 1))
    with pytest.raises(handler.HandlerError) as too_large:
        handler.is_complete(build_event(BuildId="b"))
    assert too_large.value.code == "image_too_large"


def test_mmds_is_required_once(handler: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    control = FakeControl(mmds=False)
    install(handler, monkeypatch, control=control)
    assert handler.on_event(mmds_event()) == {"PhysicalResourceId": "rayito_bot-AbCdEfGhIj/mmds"}
    (update,) = control.updates
    assert update["metadataConfiguration"] == {"requireMMDSV2": True}
    assert update["roleArn"] == control.runtime["roleArn"]
    assert set(update) - {"agentRuntimeId", "metadataConfiguration", "clientToken"} == set(
        handler.RUNTIME_UPDATE_FIELDS
    )
    assert handler.is_complete(mmds_event()) == {"IsComplete": False}
    already = FakeControl(mmds=True)
    install(handler, monkeypatch, control=already)
    handler.on_event(mmds_event("Update"))
    assert already.updates == []
    assert handler.is_complete(mmds_event()) == {"IsComplete": True}


def test_failed_runtime_stops_the_wait(handler: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    install(handler, monkeypatch, control=FakeControl(mmds=False, status="UPDATE_FAILED"))
    with pytest.raises(handler.HandlerError) as failed:
        handler.is_complete(mmds_event())
    assert failed.value.code == "runtime_failed"


def test_delete_calls_nothing(handler: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    install(handler, monkeypatch)
    assert handler.on_event(build_event("Delete")) == {"PhysicalResourceId": "src-0123456789abcdef"}
    assert handler.is_complete(mmds_event("Delete")) == {"IsComplete": True}


def test_client_token_matches_the_api_pattern(handler: ModuleType) -> None:
    token = handler.client_token("rayito_bot-AbCdEfGhIj", "7")
    assert CLIENT_TOKEN_PATTERN.fullmatch(token)
    assert len(token) >= CLIENT_TOKEN_MIN
    assert token == handler.client_token("rayito_bot-AbCdEfGhIj", "7")
    assert token != handler.client_token("rayito_bot-AbCdEfGhIj", "8")


def test_contract_names_the_handler_entry_points(handler: ModuleType) -> None:
    constants = CONTRACT["constants"]
    assert constants["handler"]["onEvent"] == f"index.{handler.on_event.__name__}"
    assert constants["handler"]["isComplete"] == f"index.{handler.is_complete.__name__}"
    assert constants["customResourceTypes"] == {
        "imageBuild": handler.IMAGE_BUILD_TYPE,
        "runtimeMmds": handler.RUNTIME_MMDS_TYPE,
    }


def test_packaged_zip_holds_the_handler_and_the_model() -> None:
    with zipfile.ZipFile(io.BytesIO(PYTHON_ZIP.read_bytes())) as archive:
        names = archive.namelist()
        assert names == sorted(names)
        assert archive.read("index.py") == HANDLER.read_bytes()
    assert "models/bedrock-agentcore-control/2023-06-05/service-2.json.gz" in names
