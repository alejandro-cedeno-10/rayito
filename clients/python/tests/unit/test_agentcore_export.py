"""Lo que el export `agentcore` añade al contexto de build de 0.14 (ADR-031,
D9): el argumento `AGENT_BASE_IMAGE` antes del `FROM` y, sólo para un adaptador
que lee las credenciales del entorno (`vercel-ai`), la herramienta que las lee
del rol del runtime. El export `docker` no cambia ni un byte."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from rayito import AgentProject
from rayito._agent._export import RawAppNotExportable, render_export
from rayito._agent._recipes import asset_bytes
from rayito._agentcore._domain import base_image_reference
from rayito._limits import (
    AGENT_AWS_CREDENTIALS_REQUIREMENTS_SHA256,
    AGENT_EXPORT_BASE_IMAGE_DIGEST,
    AGENTCORE_BASE_IMAGE_MIRROR,
    AGENTCORE_ENV_CREDENTIAL_ADAPTERS,
)
from tests.unit.fake_agentcore import CONTRACT

EXPORT_DIR = Path(__file__).resolve().parents[4] / "testdata" / "agent" / "export"
EXPORT: dict[str, Any] = CONTRACT["constants"]["export"]
CREDENTIALS: dict[str, Any] = EXPORT["awsCredentials"]


def tree(case: str, target: str | None = None) -> dict[str, bytes]:
    directory = EXPORT_DIR / case
    document = json.loads((directory / "case.json").read_text(encoding="utf-8"))
    project = AgentProject.from_dict(document["project"], base_dir=str(directory))
    return render_export(project, target=target or document["target"])


def dockerfile_lines(case: str, target: str | None = None) -> list[str]:
    return tree(case, target)["Dockerfile"].decode().splitlines()


def test_the_requirements_asset_has_the_sha256_of_limits() -> None:
    digest = hashlib.sha256(asset_bytes(CREDENTIALS["requirementsAsset"])).hexdigest()
    assert digest == AGENT_AWS_CREDENTIALS_REQUIREMENTS_SHA256


def test_the_only_adapter_that_needs_the_helper_is_vercel_ai() -> None:
    assert AGENTCORE_ENV_CREDENTIAL_ADAPTERS == ("vercel-ai",)


def test_the_agentcore_dockerfile_takes_its_base_from_a_build_argument() -> None:
    lines = dockerfile_lines("deepagents-agentcore")
    arg = EXPORT["baseImageArgLine"].format(
        dockerfileBaseImageArg=CONTRACT["constants"]["dockerfileBaseImageArg"],
        baseImageReference=f"python:3.12-slim-bookworm@{AGENT_EXPORT_BASE_IMAGE_DIGEST}",
    )
    from_line = (
        EXPORT["fromLine"]
        .format(
            platform="linux/arm64",
            dockerfileBaseImageArg=CONTRACT["constants"]["dockerfileBaseImageArg"],
        )
        .replace("{{", "{")
        .replace("}}", "}")
    )
    assert arg in lines
    assert from_line in lines
    assert lines.index(arg) + 1 == lines.index(from_line)


def test_the_docker_target_keeps_the_pinned_from_and_has_no_credentials_helper() -> None:
    lines = dockerfile_lines("vercel-ai-agentcore", "docker")
    assert any(
        line.startswith("FROM --platform=linux/arm64 python:3.12-slim-bookworm@sha256:")
        for line in lines
    )
    assert not any(line.startswith("ARG ") for line in lines)
    files = tree("vercel-ai-agentcore", "docker")
    assert CREDENTIALS["requirementsAsset"] not in files
    assert CREDENTIALS["scriptAsset"] not in files
    assert CREDENTIALS["hostKey"] not in json.loads(files["host.json"])


def test_the_build_argument_mirror_has_the_same_digest_as_the_export_base() -> None:
    reference = base_image_reference(AGENT_EXPORT_BASE_IMAGE_DIGEST)
    assert reference == f"{AGENTCORE_BASE_IMAGE_MIRROR}@{AGENT_EXPORT_BASE_IMAGE_DIGEST}"
    assert AGENTCORE_BASE_IMAGE_MIRROR.removeprefix("public.ecr.aws/docker/library/") == (
        "python:3.12-slim-bookworm"
    )


def test_a_framework_with_its_own_credential_chain_gets_no_helper() -> None:
    files = tree("deepagents-agentcore")
    assert CREDENTIALS["requirementsAsset"] not in files
    assert CREDENTIALS["scriptAsset"] not in files
    assert CREDENTIALS["hostKey"] not in json.loads(files["host.json"])


def test_vercel_ai_gets_the_helper_the_steps_the_host_key_and_the_files() -> None:
    files = tree("vercel-ai-agentcore")
    assert files[CREDENTIALS["requirementsAsset"]] == asset_bytes(CREDENTIALS["requirementsAsset"])
    assert files[CREDENTIALS["scriptAsset"]] == asset_bytes(CREDENTIALS["scriptAsset"])
    host = json.loads(files["host.json"])
    assert host[CREDENTIALS["hostKey"]] == {
        "python": CREDENTIALS["hostValue"]["python"].format(venvDir=CREDENTIALS["venvDir"]),
        "script": CREDENTIALS["scriptPath"],
    }
    lines = files["Dockerfile"].decode().splitlines()
    install = CREDENTIALS["installCommandTemplate"].format(
        sha256=AGENT_AWS_CREDENTIALS_REQUIREMENTS_SHA256,
        requirementsPath=CREDENTIALS["requirementsPath"],
        venvDir=CREDENTIALS["venvDir"],
    )
    first = lines.index(
        f"COPY {CREDENTIALS['requirementsAsset']} {CREDENTIALS['requirementsPath']}"
    )
    assert lines[first + 1] == f"RUN {install}"
    assert lines[first + 2] == f"COPY {CREDENTIALS['scriptAsset']} {CREDENTIALS['scriptPath']}"
    assert lines.index("COPY agent_host /opt/agents/rayito/agent_host") == first + 3
    node_bridge = next(index for index, line in enumerate(lines) if "node_bridge.mjs" in line)
    assert node_bridge < first


def test_the_helper_never_ships_a_credential() -> None:
    script = asset_bytes(CREDENTIALS["scriptAsset"]).decode()
    assert "169.254" not in script and "AKIA" not in script


def test_an_app_that_only_exposes_its_routes_cannot_be_exported_for_agentcore() -> None:
    project = AgentProject.from_dict(
        {
            "schema": "rayito.agent-project/1",
            "name": "mcp-server",
            "runtime": {
                "kind": "custom",
                "app": {"command": ["python", "server.py"], "port": 9100, "protocol": "raw"},
            },
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
        },
        base_dir=str(EXPORT_DIR),
    )
    with pytest.raises(RawAppNotExportable):
        render_export(project, target="agentcore")
