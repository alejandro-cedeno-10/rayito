"""Las operaciones de `AgentProject` y `AsyncAgentProject` (`build`, `run`,
`deploy`, `export`): la delegación en los servicios, la paridad de firmas
entre las dos superficies, el bloque "Coste y activación" y que construir,
cargar, exportar y crear un cliente de despliegue no llaman a AWS."""

from __future__ import annotations

import asyncio
import inspect
import json
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from rayito._agent._deployment import AgentDeployment
from rayito._agent._deployment_async import AsyncAgentDeployment
from rayito._agent._project_domain import AgentProject, AsyncAgentProject
from rayito._agent._project_service import ProjectService
from rayito._agent._project_service_async import AsyncProjectService
from tests.unit.fake_agent_project import (
    AsyncFakeSandbox,
    FakeSandbox,
    project,
)

COST_ENTRIES = (
    "Activa:",
    "Recursos y llamadas AWS:",
    "Coste aproximado:",
    "IAM:",
    "Cómo apagarla:",
    "Ejemplo:",
)
OPERATIONS = ("build", "run", "deploy", "export")
DEPLOYMENT_METHODS = ("invoke", "stream", "status", "stop", "connect")


def _public_methods(cls: type, names: tuple[str, ...]) -> dict[str, inspect.Signature]:
    return {name: inspect.signature(getattr(cls, name)) for name in names}


def test_sync_and_async_projects_have_identical_signatures() -> None:
    sync = _public_methods(AgentProject, OPERATIONS)
    asynchronous = _public_methods(AsyncAgentProject, OPERATIONS)
    for name in OPERATIONS:
        assert list(sync[name].parameters.items()) == list(asynchronous[name].parameters.items())
    sync_public = {n for n in dir(AgentProject) if not n.startswith("_")}
    async_public = {n for n in dir(AsyncAgentProject) if not n.startswith("_")}
    assert sync_public == async_public
    assert inspect.iscoroutinefunction(AsyncAgentProject.deploy)
    assert not inspect.iscoroutinefunction(AgentProject.deploy)


def test_sync_and_async_constructors_are_identical() -> None:
    assert list(inspect.signature(AgentProject).parameters.items()) == list(
        inspect.signature(AsyncAgentProject).parameters.items()
    )


def test_deployments_have_identical_public_signatures() -> None:
    for name in DEPLOYMENT_METHODS:
        sync = inspect.signature(getattr(AgentDeployment, name))
        asynchronous = inspect.signature(getattr(AsyncAgentDeployment, name))
        assert list(sync.parameters.items()) == list(asynchronous.parameters.items()), name
    for name in ("url", "headers", "sandbox_id", "port"):
        assert isinstance(getattr(AgentDeployment, name), property)
        assert isinstance(getattr(AsyncAgentDeployment, name), property)
    for name in ("invoke", "status", "stop", "connect"):
        assert inspect.iscoroutinefunction(getattr(AsyncAgentDeployment, name))


def test_operations_delegate_to_the_services(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def recorder(name: str) -> Callable[..., str]:
        def record(self: Any, *args: Any, **kwargs: Any) -> str:
            calls.append((name, args, kwargs))
            return name

        return record

    def async_recorder(name: str) -> Callable[..., Any]:
        async def record(self: Any, *args: Any, **kwargs: Any) -> str:
            calls.append((name, args, kwargs))
            return name

        return record

    for name in ("build", "run", "deploy"):
        monkeypatch.setattr(ProjectService, name, recorder(name))
        monkeypatch.setattr(AsyncProjectService, name, async_recorder(name))
    the_project = project()
    assert cast(Any, the_project.build(bucket="b", force=True)) == "build"
    assert cast(Any, the_project.run("hola", session_id="s", reasoning=True)) == "run"
    assert cast(Any, the_project.deploy(region="us-east-1", access_token="dG9rZW4")) == "deploy"
    assert calls[0][2]["bucket"] == "b" and calls[0][2]["force"] is True
    assert calls[1][1] == ("hola",) and calls[1][2]["session_id"] == "s"
    assert calls[2][2] == {
        "region": "us-east-1",
        "session": None,
        "access_token": "dG9rZW4",
        "keep_on_failure": False,
    }
    async_project = AsyncAgentProject(**_fields(the_project))
    assert cast(Any, asyncio.run(async_project.build(bucket="b"))) == "build"
    assert cast(Any, asyncio.run(async_project.run("hola"))) == "run"
    assert cast(Any, asyncio.run(async_project.deploy())) == "deploy"


def _fields(the_project: AgentProject) -> dict[str, Any]:
    return {name: getattr(the_project, name) for name in inspect.signature(AgentProject).parameters}


def test_the_cost_block_is_in_the_class_and_in_both_deploy_docstrings() -> None:
    for doc in (
        AgentProject.__doc__,
        AgentProject.deploy.__doc__,
        AsyncAgentProject.deploy.__doc__,
    ):
        assert doc is not None
        assert "Coste y activación" in doc
        for entry in COST_ENTRIES:
            assert entry in doc, entry
    assert "Coste y activación" in (AgentDeployment.__doc__ or "")
    assert "Coste y activación" in (AsyncAgentDeployment.__doc__ or "")


def _no_aws(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def forbidden(name: str) -> Callable[..., Any]:
        def trip(*args: Any, **kwargs: Any) -> Any:
            calls.append(name)
            raise AssertionError(f"{name} no debe llamarse")

        return trip

    monkeypatch.setattr("boto3.session.Session.client", forbidden("Session.client"))
    monkeypatch.setattr("boto3.client", forbidden("boto3.client"))
    monkeypatch.setattr("boto3.Session", forbidden("boto3.Session"))
    monkeypatch.setattr(socket.socket, "connect", forbidden("socket.connect"))
    return calls


def test_construct_load_serialise_and_export_make_zero_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _no_aws(monkeypatch)
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
                "secret": "bedrock-key",
                "region": "us-east-1",
                "models": ["us.anthropic.claude-haiku-4-5-20251001-v1:0"],
            }
        },
    }
    (tmp_path / "rayito-agent.json").write_text(json.dumps(document))
    loaded = AgentProject.load(tmp_path)
    assert loaded.to_dict()["name"] == "support-bot"
    AsyncAgentProject.load(tmp_path)
    ProjectService(loaded)
    AsyncProjectService(loaded)
    loaded.export(target="docker", out_dir=tmp_path / "out")
    loaded.export(target="agentcore", out_dir=tmp_path / "out2")
    assert (tmp_path / "out" / "Dockerfile").is_file()
    assert calls == []


def test_creating_a_deployment_client_makes_zero_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _no_aws(monkeypatch)
    AgentDeployment(FakeSandbox(), port=8088)
    AsyncAgentDeployment(AsyncFakeSandbox(), port=8088, host_access=FakeSandbox().get_host(8088))
    assert calls == []
