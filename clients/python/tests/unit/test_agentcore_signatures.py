"""La superficie pública de `--target agentcore` (`contract.json`, `sdk.python`):
las firmas síncronas y asíncronas coinciden, `invoke` y `stream` tienen los
parámetros de `AgentFleet` y las clases exportadas son las del contrato."""

from __future__ import annotations

import inspect
from typing import Any

import pytest

import rayito
from rayito import (
    AgentCoreFleet,
    AgentCoreFleetStream,
    AgentFleet,
    AsyncAgentCoreFleet,
    AsyncAgentCoreFleetStream,
    AsyncAgentFleet,
    deploy_agent,
    deploy_agent_async,
    plan_deploy,
)
from tests.unit.fake_agentcore import CONTRACT

PYTHON: dict[str, Any] = CONTRACT["sdk"]["python"]


def parameters(function: Any) -> list[tuple[str, Any, Any]]:
    return [
        (parameter.name, parameter.default, parameter.kind)
        for parameter in inspect.signature(function).parameters.values()
    ]


@pytest.mark.parametrize("method", ["invoke", "stream"])
def test_invoke_and_stream_have_the_parameters_of_agent_fleet(method: str) -> None:
    expected = parameters(getattr(AgentFleet, method))
    assert parameters(getattr(AgentCoreFleet, method)) == expected
    assert parameters(getattr(AsyncAgentFleet, method)) == expected
    assert parameters(getattr(AsyncAgentCoreFleet, method)) == expected


@pytest.mark.parametrize("method", ["invoke", "stream", "status", "stop_session", "delete"])
def test_the_sync_and_async_fleets_have_the_same_parameters(method: str) -> None:
    assert parameters(getattr(AgentCoreFleet, method)) == parameters(
        getattr(AsyncAgentCoreFleet, method)
    )


def test_the_async_fleet_methods_are_coroutines_and_the_sync_ones_are_not() -> None:
    for method in ("invoke", "stream", "status", "stop_session", "delete"):
        assert inspect.iscoroutinefunction(getattr(AsyncAgentCoreFleet, method))
        assert not inspect.iscoroutinefunction(getattr(AgentCoreFleet, method))
    assert inspect.iscoroutinefunction(AsyncAgentCoreFleet.connect)


def test_connect_has_the_contracts_signature() -> None:
    assert [name for name, _, _ in parameters(AgentCoreFleet.connect)] == [
        "name",
        "region",
        "session",
    ]
    assert parameters(AgentCoreFleet.connect) == parameters(AsyncAgentCoreFleet.connect)


def test_the_fleet_properties_are_the_contracts() -> None:
    for name in PYTHON["fleet"]["properties"]:
        assert isinstance(getattr(AgentCoreFleet, name), property)
        assert isinstance(getattr(AsyncAgentCoreFleet, name), property)


def test_the_stream_members_are_the_contracts() -> None:
    for stream in (AgentCoreFleetStream, AsyncAgentCoreFleetStream):
        assert hasattr(stream, "session_id") and hasattr(stream, "result")
        assert hasattr(stream, "close")
    assert hasattr(AgentCoreFleetStream, "__iter__")
    assert hasattr(AsyncAgentCoreFleetStream, "__aiter__")


@pytest.mark.parametrize("function", [deploy_agent, deploy_agent_async, plan_deploy])
def test_target_and_agentcore_are_keyword_only_after_session(function: Any) -> None:
    names = [name for name, _, _ in parameters(function)]
    assert names[-3:] == ["session", "target", "agentcore"]
    by_name = {name: (default, kind) for name, default, kind in parameters(function)}
    assert by_name["target"] == ("microvm", inspect.Parameter.KEYWORD_ONLY)
    assert by_name["agentcore"] == (None, inspect.Parameter.KEYWORD_ONLY)


def test_deploy_agent_and_deploy_agent_async_share_a_signature() -> None:
    assert parameters(deploy_agent) == parameters(deploy_agent_async)


def test_the_value_classes_have_the_contracts_fields() -> None:
    options = inspect.signature(rayito.AgentCoreOptions).parameters
    assert list(options) == [field.split(":")[0] for field in PYTHON["options"]["fields"]]
    status = list(inspect.signature(rayito.AgentCoreStatus).parameters)
    assert status == PYTHON["status"]["fields"]
    deleted = list(inspect.signature(rayito.AgentCoreDeleteResult).parameters)
    assert deleted == PYTHON["deleteResult"]["fields"]
    assert rayito.AgentCoreException.__bases__ == (Exception,)


def test_the_public_names_are_exported() -> None:
    for name in (
        "AgentCoreFleet",
        "AsyncAgentCoreFleet",
        "AgentCoreFleetStream",
        "AsyncAgentCoreFleetStream",
        "AgentCoreOptions",
        "AgentCoreStatus",
        "AgentCoreDeleteResult",
        "AgentCoreDeployPlan",
        "AgentCoreException",
    ):
        assert name in rayito.__all__
        assert getattr(rayito, name) is not None
