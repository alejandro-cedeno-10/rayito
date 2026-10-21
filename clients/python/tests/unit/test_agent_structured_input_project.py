"""Selección de grafo (ADR-033 D3): `select_graph`, `--graph` en la
detección, `[runtime] graph` en el fichero de proyecto (resuelto al
`entrypoint` de `langgraph.json` antes de construir el runtime), el ejemplo
`langgraph-structured`; las banderas `--graph`, `--input` y `--input-file`
se prueban en `cli/test_agent_structured_input_cli.py`. Nada ejecuta el
código del proyecto ni llama a AWS."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import jsonschema
import pytest

from rayito import AgentProject, DeepAgents, InvalidArgumentException
from rayito._agent._detect_domain import detect
from rayito._agent._detect_entry import EntryChoice, graph_entrypoint, select_graph
from rayito._agent._detect_report import DetectOverrides
from rayito._agent._detection_rules import DETECTION_RULES
from rayito._agent._project_domain import AgentProjectException
from rayito._agent._project_file import read_project_document, with_file_graph
from tests.unit.fake_agent_project import project as fake_project
from tests.unit.test_agent_detect import MemoryTree, _overrides_of, _tree_of

TESTDATA = Path(__file__).resolve().parents[4] / "testdata"
CASES = TESTDATA / "agent-detection" / "cases"
PROJECTS = TESTDATA / "agent-project"
GRAPH_PROJECT = PROJECTS / "valid" / "langgraph-graph"
SAMPLES = TESTDATA / "agent-structured-samples"
SAMPLE = SAMPLES / "python" / "langgraph-structured"
CONTRACT = json.loads((TESTDATA / "agnostic-deploy" / "contract.json").read_text("utf-8"))
SCHEMA = json.loads((PROJECTS / "schema.json").read_text("utf-8"))
REGION = "us-east-1"
TICKET = {"ticket_id": "T-1"}


def _choice(name: str) -> EntryChoice:
    return EntryChoice(f"src/{name}.py", "graph", False, "test", "python", f"src.{name}:graph")


def _graphs(*names: str) -> dict[str, EntryChoice]:
    return {name: _choice(name) for name in names}


def test_select_graph_follows_the_precedence() -> None:
    assert select_graph({}, None, DETECTION_RULES) is None
    assert select_graph(_graphs("echo"), None, DETECTION_RULES) == ("echo", _choice("echo"))
    assert select_graph(_graphs("agent", "echo"), None, DETECTION_RULES)[0] == "agent"  # type: ignore[index]
    assert select_graph(_graphs("agent", "echo"), "echo", DETECTION_RULES)[0] == "echo"  # type: ignore[index]


def test_select_graph_names_the_graphs_when_it_cannot_choose() -> None:
    with pytest.raises(AgentProjectException) as ambiguous:
        select_graph(_graphs("echo", "triage"), None, DETECTION_RULES)
    assert (ambiguous.value.code, ambiguous.value.path) == ("ambiguous", "runtime.entrypoint")
    assert "echo, triage" in str(ambiguous.value) and "--graph" in str(ambiguous.value)
    with pytest.raises(AgentProjectException) as missing:
        select_graph(_graphs("echo", "triage"), "billing", DETECTION_RULES)
    assert (missing.value.code, missing.value.path) == ("invalid_value", "runtime.graph")
    assert "billing" in str(missing.value) and "echo, triage" in str(missing.value)


def test_the_listing_is_capped() -> None:
    names = [f"g{index}" for index in range(DETECTION_RULES["maxCandidatesListed"] + 2)]
    with pytest.raises(AgentProjectException) as raised:
        select_graph(_graphs(*names), None, DETECTION_RULES)
    assert names[-1] not in str(raised.value)


@pytest.mark.parametrize(
    ("case", "fragments"),
    [
        ("error-langgraph-json-ambiguous-graphs", ("echo, triage", "--graph")),
        ("error-langgraph-json-graph-not-found", ("billing", "echo, triage")),
        ("override-graph-and-entrypoint-conflict", ("--graph", "--entry")),
    ],
)
def test_detection_errors_name_what_fixes_them(case: str, fragments: tuple[str, ...]) -> None:
    with pytest.raises(AgentProjectException) as raised:
        detect(MemoryTree(_tree_of(CASES / case)), _overrides_of(CASES / case))
    assert all(fragment in str(raised.value) for fragment in fragments)


def test_graph_on_a_framework_without_langgraph_json_is_not_found() -> None:
    with pytest.raises(AgentProjectException) as raised:
        detect(MemoryTree(_tree_of(CASES / "python-strands")), DetectOverrides(graph="triage"))
    assert (raised.value.code, raised.value.path) == ("invalid_value", "runtime.graph")


def test_the_contract_messages_are_the_rules_messages() -> None:
    rules = {
        item["key"]: item["text"]
        for item in CONTRACT["agentDetectionRules"]
        if item["feature"] == "B"
    }
    assert {key: DETECTION_RULES["messages"][key] for key in rules} == rules


def test_a_graph_project_resolves_its_entrypoint_and_keeps_the_name() -> None:
    expected = json.loads((GRAPH_PROJECT / "expected.json").read_text("utf-8"))
    project = AgentProject.load(GRAPH_PROJECT)
    assert project.graph == "triage"
    assert isinstance(project.runtime, DeepAgents)
    assert project.runtime.entrypoint == "src.triage:graph"
    assert project.to_dict() == expected
    assert "entrypoint" not in expected["runtime"]


def test_a_graph_project_round_trips_with_the_file_reader(tmp_path: Path) -> None:
    document = read_project_document(GRAPH_PROJECT)
    project = AgentProject.from_dict(
        document.data, base_dir=document.base_dir, read_text=document.read_text
    )
    again = AgentProject.from_dict(
        project.to_dict(), base_dir=document.base_dir, read_text=document.read_text
    )
    assert again.to_dict() == project.to_dict()
    with pytest.raises(AgentProjectException) as raised:
        AgentProject.from_dict(project.to_dict(), base_dir=document.base_dir)
    assert (raised.value.code, raised.value.path) == ("invalid_value", "runtime.graph")


def test_the_schema_accepts_the_graph_project() -> None:
    document = tomllib.loads((GRAPH_PROJECT / "rayito-agent.toml").read_text("utf-8"))
    jsonschema.Draft202012Validator(SCHEMA).validate(document)


def test_a_project_without_langgraph_json_cannot_resolve_its_graph(tmp_path: Path) -> None:
    (tmp_path / "rayito-agent.toml").write_text(
        (GRAPH_PROJECT / "rayito-agent.toml").read_text("utf-8"), encoding="utf-8"
    )
    with pytest.raises(AgentProjectException) as raised:
        AgentProject.load(tmp_path)
    assert (raised.value.code, raised.value.path) == ("invalid_value", "runtime.graph")


@pytest.mark.parametrize("graph", ["", "-bad", "a" * 65, "con espacio"])
def test_an_invalid_graph_name_is_refused(graph: str) -> None:
    runtime = DeepAgents(entrypoint="src.triage:graph", require_entrypoint=True)
    with pytest.raises(InvalidArgumentException):
        fake_project(runtime=runtime, graph=graph)


def test_graph_only_goes_with_the_langgraph_runtime() -> None:
    with pytest.raises(InvalidArgumentException):
        fake_project(graph="triage")
    runtime = DeepAgents(entrypoint="src.triage:graph", require_entrypoint=True)
    assert fake_project(runtime=runtime, graph="triage").graph == "triage"


def test_graph_entrypoint_of_a_document_without_graphs_is_not_found() -> None:
    with pytest.raises(AgentProjectException) as raised:
        graph_entrypoint(None, "triage", DETECTION_RULES)
    assert raised.value.path == "runtime.graph"


def test_the_file_graph_reaches_the_detection_unless_a_flag_or_an_entrypoint_wins() -> None:
    document: dict[str, object] = {"runtime": {"kind": "langgraph", "graph": "triage"}}
    assert with_file_graph(None, document) == DetectOverrides(graph="triage")
    flagged = DetectOverrides(graph="echo")
    assert with_file_graph(flagged, document) is flagged
    both: dict[str, object] = {"runtime": {"graph": "triage", "entrypoint": "x:y"}}
    assert with_file_graph(None, both) is None
    assert with_file_graph(None, {}) is None


def test_the_sample_needs_a_graph_and_deploys_the_chosen_one() -> None:
    with pytest.raises(AgentProjectException) as raised:
        AgentProject.detect(SAMPLE, region=REGION)
    assert "echo, triage" in str(raised.value)
    expected = json.loads((SAMPLES / "expected" / "python-langgraph-structured.json").read_text())
    project, report = AgentProject.detect(SAMPLE, DetectOverrides(graph="triage"), region=REGION)
    assert project.to_dict() == expected
    assert report is not None and report.options == {"graph": "triage"}
    assert isinstance(project.runtime, DeepAgents)
    assert project.runtime.entrypoint == "triage:graph"


def test_a_file_graph_is_used_by_the_detection(tmp_path: Path) -> None:
    for name in ("pyproject.toml", "uv.lock", "langgraph.json", "triage.py", "echo.py"):
        (tmp_path / name).write_text((SAMPLE / name).read_text("utf-8"), encoding="utf-8")
    document = (SAMPLE / "rayito-agent.toml").read_text("utf-8")
    runtime = '\n[runtime]\nkind = "langgraph"\ngraph = "echo"\n'
    (tmp_path / "rayito-agent.toml").write_text(document + runtime, encoding="utf-8")
    project, _ = AgentProject.detect(tmp_path, region=REGION)
    assert project.graph == "echo"
    assert isinstance(project.runtime, DeepAgents)
    assert project.runtime.entrypoint == "echo:graph"
    assert "entrypoint" not in project.to_dict()["runtime"]  # type: ignore[operator]
