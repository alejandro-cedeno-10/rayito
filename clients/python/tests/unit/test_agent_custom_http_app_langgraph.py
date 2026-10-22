"""`HttpApp(protocol="langgraph-api")` (ADR-033 D5): validación de
`assistant_id`, la tabla `[runtime.app]` canónica, lo que el SDK manda al
host y el camino desde la detección con `--protocol langgraph-api` hasta la
configuración del host. Comparte `testdata/agent-langgraph-api/host-config.json`
con `agent-custom-langgraph-api.test.ts`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rayito import AgentModel, AgentSpec
from rayito._agent._custom import HTTP_APP_PROTOCOLS, CustomRuntime, HttpApp
from rayito._agent._detect_domain import detect
from rayito._agent._runtime import DirectWiring
from rayito._limits import AGENT_LANGGRAPH_HEALTH_PATH, AGENT_LANGGRAPH_INVOKE_PATH
from rayito.exceptions import InvalidArgumentException
from tests.unit.test_agent_detect import MemoryTree, _overrides_of, _tree_of

REPO = Path(__file__).resolve().parents[4]
VECTORS = json.loads(
    (REPO / "testdata" / "agent-langgraph-api" / "host-config.json").read_text("utf-8")
)
DETECTION_CASES = REPO / "testdata" / "agent-detection" / "cases"
DETECTION_CASE = DETECTION_CASES / "python-langgraph-json-langgraph-api-override"
HOST_ONLY_KEYS = ("workdir", "model", "app_dir", "path_prepend")


def _spec() -> AgentSpec:
    return AgentSpec(model=AgentModel(provider="bedrock", id="m", gateway="gw", region="us-east-1"))


def _host_options(runtime: CustomRuntime) -> dict[str, Any]:
    host = runtime.host_config(_spec(), wiring=DirectWiring(), workdir="/home/user")
    return {key: value for key, value in host.options.items() if key not in HOST_ONLY_KEYS}


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda case: case["name"])
def test_the_app_table_and_the_host_options_match_the_vector(case: dict[str, Any]) -> None:
    app = HttpApp.from_mapping(case["app"])
    assert app.to_mapping() == case["canonical"]
    assert HttpApp.from_mapping(app.to_mapping()) == app
    assert _host_options(CustomRuntime(app=app)) == case["hostOptions"]


@pytest.mark.parametrize("case", VECTORS["invalid"], ids=lambda case: case["name"])
def test_an_invalid_app_table_is_refused(case: dict[str, Any]) -> None:
    with pytest.raises(InvalidArgumentException):
        HttpApp.from_mapping(case["app"])


def test_langgraph_api_is_one_more_protocol_at_the_end() -> None:
    assert HTTP_APP_PROTOCOLS == ("agentcore", "json", "openai-chat", "raw", "langgraph-api")


@pytest.mark.parametrize("protocol", ["agentcore", "json", "openai-chat", "raw"])
def test_other_protocols_keep_their_paths_and_table(protocol: str) -> None:
    app = HttpApp(command=["x"], port=9100, protocol=protocol)  # type: ignore[arg-type]
    assert app.assistant_id is None
    assert (app.served_health_path, app.served_invoke_path) == (app.health_path, app.invoke_path)
    assert "assistant_id" not in app.to_mapping()
    assert "assistant_id" not in _host_options(CustomRuntime(app=app))


def test_the_served_paths_of_langgraph_api() -> None:
    app = HttpApp(
        command=["langgraph", "dev"], port=8000, protocol="langgraph-api", assistant_id="a"
    )
    assert app.served_health_path == AGENT_LANGGRAPH_HEALTH_PATH
    assert app.served_invoke_path == AGENT_LANGGRAPH_INVOKE_PATH
    assert app.hides_own_routes is False


@pytest.mark.parametrize(
    ("health_path", "hidden"), [(AGENT_LANGGRAPH_HEALTH_PATH, False), ("/salud", True)]
)
def test_the_detected_langgraph_health_path_is_not_an_own_route(
    health_path: str, hidden: bool
) -> None:
    app = HttpApp(
        command=["langgraph", "dev"],
        port=8000,
        protocol="langgraph-api",
        assistant_id="a",
        health_path=health_path,
    )
    assert app.hides_own_routes is hidden
    other = HttpApp(command=["x"], port=8000, protocol="agentcore", health_path=health_path)
    assert other.hides_own_routes is True


def test_a_detected_project_becomes_a_langgraph_api_host_config() -> None:
    report = detect(MemoryTree(_tree_of(DETECTION_CASE)), _overrides_of(DETECTION_CASE))
    table = report.runtime_table()
    assert table["kind"] == "custom"
    runtime = CustomRuntime.from_options(table)
    options = _host_options(runtime)
    assert options["protocol"] == "langgraph-api"
    assert options["assistant_id"] == "agent"
    assert options["health_path"] == AGENT_LANGGRAPH_HEALTH_PATH
    assert options["invoke_path"] == AGENT_LANGGRAPH_INVOKE_PATH
    assert options["command"][:2] == ["langgraph", "dev"]
