"""Detección del modelo elegido por el código (ADR-033 D2): el proyecto que
sale de cada caso nuevo (`bridge.json`), que nunca se lee un valor, las
banderas `--model-id`, `--model-provider` y `--model-base-url`, y que las
banderas de la CLI son las de `testdata/agnostic-deploy/contract.json`."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import click
import pytest
import typer
from typer.testing import CliRunner

from rayito._agent._detect_domain import detect
from rayito._agent._detect_project import detected_document
from rayito._agent._detect_report import DetectOverrides
from rayito._agent._project_domain import AgentProject, AgentProjectException
from rayito.cli.app import app

ROOT = Path(__file__).resolve().parents[4] / "testdata"
CASES_DIR = ROOT / "agent-detection" / "cases"
BRIDGE_CASES = sorted(path.parent for path in CASES_DIR.glob("*/bridge.json"))
CONTRACT = json.loads((ROOT / "agnostic-deploy" / "contract.json").read_text("utf-8"))
SURFACE = json.loads((ROOT / "cli" / "surface.json").read_text("utf-8"))["commands"]
FEATURE_FLAGS = [flag for flag in CONTRACT["cliFlags"] if flag["feature"] == "A"]
PROJECT_NAME = "ticket-agent"
REGION = "us-east-1"
SECRET_VALUES = ("not-a-real-key-value", "https://private-llm.internal", "real-model-name")


class MemoryTree:
    """Un `ProjectTree` en memoria."""

    def __init__(self, files: Mapping[str, str]) -> None:
        self.files = dict(files)

    def list_files(self, max_depth: int, max_files: int) -> list[str]:
        return list(self.files)

    def read_text(self, path: str, max_bytes: int) -> str | None:
        text = self.files.get(path)
        return None if text is None else text[:max_bytes]


def _tree_of(case: Path) -> dict[str, str]:
    tree = case / "tree"
    return {
        path.relative_to(tree).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(tree.rglob("*"))
        if path.is_file()
    }


def _bridge_of(case: Path) -> dict[str, Any]:
    return dict(json.loads((case / "bridge.json").read_text(encoding="utf-8")))


def _overrides_of(case: Path) -> DetectOverrides:
    return DetectOverrides(**_bridge_of(case).get("overrides", {}))


@pytest.mark.parametrize("case", BRIDGE_CASES, ids=lambda case: case.name)
def test_the_detected_project_matches_the_bridge_vector(case: Path) -> None:
    expected = _bridge_of(case)
    overrides = _overrides_of(case)
    report = detect(MemoryTree(_tree_of(case)), overrides)

    def bridge() -> dict[str, object]:
        return detected_document({}, report, name=PROJECT_NAME, region=REGION, overrides=overrides)

    if "error" in expected:
        with pytest.raises(AgentProjectException) as raised:
            bridge()
        assert {"code": raised.value.code, "path": raised.value.path} == expected["error"]
        return
    document = bridge()
    assert document["model"] == expected["model"]
    assert document["gateways"] == expected["gateways"]
    project = AgentProject.from_dict(document, base_dir=".")
    assert project.spec.model.id == expected["model"].get("id")


def _leaky_tree() -> dict[str, str]:
    return {
        "pyproject.toml": (
            '[project]\nname = "x"\nrequires-python = ">=3.11"\n'
            'dependencies = ["langgraph>=1.0", "langchain-openai>=1.0"]\n'
        ),
        "uv.lock": 'version = 1\nrequires-python = ">=3.11"\n',
        "agent.py": (
            "import os\nfrom langchain_openai import ChatOpenAI\n"
            "from langgraph.prebuilt import create_react_agent\n\n\n"
            "def build_graph():\n"
            '    model = ChatOpenAI(model=os.getenv("LLM_MODEL"), '
            'base_url=os.getenv("LLM_BASE_URL"))\n'
            "    return create_react_agent(model, [])\n"
        ),
        ".env.example": (
            f"LLM_MODEL={SECRET_VALUES[2]}\nLLM_BASE_URL={SECRET_VALUES[1]}\n"
            f"export LLM_API_KEY='{SECRET_VALUES[0]}'\n"
        ),
        "langgraph.json": json.dumps(
            {
                "graphs": {"agent": "./agent.py:build_graph"},
                "env": {"LLM_API_KEY": SECRET_VALUES[0]},
            }
        ),
    }


def test_the_report_and_the_project_hold_names_never_values() -> None:
    overrides = DetectOverrides(model_base_url="https://llm-gateway.example.com")
    report = detect(MemoryTree(_leaky_tree()), overrides)
    assert report.model_envs == {
        "id_env": "LLM_MODEL",
        "base_url_env": "LLM_BASE_URL",
        "api_key_env": "LLM_API_KEY",
    }
    document = detected_document({}, report, name=PROJECT_NAME, region=REGION, overrides=overrides)
    text = json.dumps([report.to_dict(), document])
    assert not [value for value in SECRET_VALUES if value in text]


def test_an_unknown_model_provider_names_the_known_presets() -> None:
    with pytest.raises(AgentProjectException) as raised:
        detect(MemoryTree(_leaky_tree()), DetectOverrides(model_provider="acme"))
    assert (raised.value.code, raised.value.path) == ("invalid_value", "model.provider")
    assert "--model-provider acme" in str(raised.value) and "litellm" in str(raised.value)


def test_an_invalid_model_id_is_an_invalid_value() -> None:
    with pytest.raises(AgentProjectException) as raised:
        detect(MemoryTree(_leaky_tree()), DetectOverrides(model_id="no spaces allowed"))
    assert (raised.value.code, raised.value.path) == ("invalid_value", "model.id")


def test_a_base_url_needs_a_preset_with_an_upstream() -> None:
    overrides = DetectOverrides(model_provider="anthropic", model_base_url="https://x.example.com")
    report = detect(MemoryTree(_leaky_tree()), overrides)
    with pytest.raises(AgentProjectException) as raised:
        detected_document({}, report, name=PROJECT_NAME, region=REGION, overrides=overrides)
    assert (raised.value.code, raised.value.path) == (
        "invalid_value",
        "gateways.anthropic.upstream",
    )


def test_without_a_base_url_the_missing_upstream_names_the_flag() -> None:
    report = detect(MemoryTree(_leaky_tree()))
    with pytest.raises(AgentProjectException) as raised:
        detected_document({}, report, name=PROJECT_NAME, region=REGION)
    assert raised.value.path == "gateways.openai-compatible.upstream"
    assert "--model-base-url" in str(raised.value)


def test_the_override_provider_replaces_the_detected_ones_without_warnings() -> None:
    report = detect(MemoryTree(_leaky_tree()), DetectOverrides(model_provider="openai"))
    assert [provider.preset for provider in report.providers] == ["openai"]
    assert [warning.code for warning in report.warnings] == ["model_id_from_code"]


def test_the_cli_detect_takes_the_model_flags(tmp_path: Path) -> None:
    for name, text in _tree_of(CASES_DIR / "override-model-id-and-provider").items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    overrides = _bridge_of(CASES_DIR / "override-model-id-and-provider")["overrides"]
    result = CliRunner().invoke(
        app,
        [
            "--json",
            "--region",
            REGION,
            "agent",
            "detect",
            str(tmp_path),
            "--model-id",
            overrides["model_id"],
            "--model-provider",
            overrides["model_provider"],
            "--model-base-url",
            overrides["model_base_url"],
        ],
    )
    assert result.exit_code == 0, result.output
    project = json.loads(result.stdout)["project"]
    assert project["model"]["id"] == overrides["model_id"]
    assert project["model"]["gateway"] == overrides["model_provider"]


def _surface_parameters(command: list[str]) -> list[dict[str, Any]]:
    (entry,) = [item for item in SURFACE if item["path"] == command]
    return list(entry["parameters"])


def _click_help(command: list[str], name: str) -> str | None:
    group: Any = typer.main.get_command(app)
    context = click.Context(group)
    for part in command:
        group = group.get_command(context, part)
    (param,) = [item for item in group.params if item.name == name]
    return getattr(param, "help", None)


@pytest.mark.parametrize(
    "flag",
    [(flag, command) for flag in FEATURE_FLAGS for command in flag["commands"]],
    ids=lambda item: f"{' '.join(item[1])} {item[0]['flags'][0]}",
)
def test_the_cli_flags_are_the_contracts(flag: tuple[dict[str, Any], list[str]]) -> None:
    spec, command = flag
    parameters = _surface_parameters(command)
    names = [parameter["flags"][0] for parameter in parameters]
    (found,) = [parameter for parameter in parameters if parameter["name"] == spec["name"]]
    assert found["flags"] == spec["flags"]
    assert (found["type"], found["multiple"], found["default"], found["is_flag"]) == (
        spec["type"],
        spec["multiple"],
        spec["default"],
        spec["isFlag"],
    )
    assert names.index(spec["flags"][0]) == names.index(spec["after"]) + 1
    assert _click_help(command, spec["name"]) == spec["help"]
