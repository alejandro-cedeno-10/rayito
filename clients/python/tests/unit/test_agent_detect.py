"""Detección de un proyecto de agente (ADR-029): los vectores compartidos
con TypeScript (`testdata/agent-detection/`), las cotas del análisis, que no
se lea nada excluido, la precedencia de las banderas y que detectar no llame
a AWS ni ejecute el código del proyecto."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from rayito._agent._detect_domain import detect, fill_runtime
from rayito._agent._detect_fs import LocalProjectTree
from rayito._agent._detect_report import DetectOverrides
from rayito._agent._detect_scan import (
    glob_match,
    pyproject_dependencies,
    requirement_names,
    requirements_dependencies,
)
from rayito._agent._detect_versions import resolve_build, satisfied
from rayito._agent._detection_rules import DETECTION_RULES
from rayito._agent._project_domain import AGENT_PROJECT_ERROR_CODES, AgentProjectException
from rayito._limits import (
    AGENT_DETECTION_MAX_DEPTH,
    AGENT_DETECTION_MAX_FILE_BYTES,
    AGENT_DETECTION_MAX_FILES,
    AGENT_DETECTION_RULES_SCHEMA,
    AGENT_DETECTION_SCHEMA,
)

ROOT = Path(__file__).resolve().parents[4] / "testdata" / "agent-detection"
CASES = sorted(path for path in (ROOT / "cases").iterdir() if path.is_dir())
VERSIONS = json.loads((ROOT / "versions.json").read_text(encoding="utf-8"))


class MemoryTree:
    """Un `ProjectTree` en memoria que apunta lo que se lee."""

    def __init__(self, files: Mapping[str, str]) -> None:
        self.files = dict(files)
        self.reads: list[str] = []

    def list_files(self, max_depth: int, max_files: int) -> list[str]:
        return list(self.files)

    def read_text(self, path: str, max_bytes: int) -> str | None:
        self.reads.append(path)
        text = self.files.get(path)
        return None if text is None else text[:max_bytes]


def _tree_of(case: Path) -> dict[str, str]:
    tree = case / "tree"
    return {
        path.relative_to(tree).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(tree.rglob("*"))
        if path.is_file()
    }


def _overrides_of(case: Path) -> DetectOverrides:
    file = case / "overrides.json"
    return (
        DetectOverrides(**json.loads(file.read_text(encoding="utf-8")))
        if file.exists()
        else DetectOverrides()
    )


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_detection_matches_the_shared_vector(case: Path) -> None:
    expected = json.loads((case / "expected.json").read_text(encoding="utf-8"))
    tree = MemoryTree(_tree_of(case))
    if "error" in expected:
        with pytest.raises(AgentProjectException) as raised:
            detect(tree, _overrides_of(case))
        assert {"code": raised.value.code, "path": raised.value.path} == expected["error"]
        assert raised.value.code in AGENT_PROJECT_ERROR_CODES
        assert raised.value.path in str(raised.value)
    else:
        assert detect(tree, _overrides_of(case)).to_dict() == expected


def test_every_framework_of_the_matrix_has_a_vector() -> None:
    frameworks = {
        json.loads((case / "expected.json").read_text(encoding="utf-8")).get("framework")
        for case in CASES
    }
    wanted = {
        "langgraph",
        "deepagents",
        "strands",
        "strands-harness",
        "hermes",
        "flask",
        "agentcore",
        "vercel-ai",
        "express",
        "hono",
        "fastapi",
        "opencode",
    }
    assert wanted <= frameworks


@pytest.mark.parametrize(
    "entry", VERSIONS["satisfied"], ids=lambda e: f"{e['kind']}:{e['constraint']}:{e['version']}"
)
def test_version_constraints_match_the_shared_vectors(entry: dict[str, Any]) -> None:
    assert satisfied(entry["kind"], entry["constraint"], entry["version"]) is entry["expect"]


@pytest.mark.parametrize(
    "entry", VERSIONS["resolve"], ids=lambda e: f"{e['kind']}:{e['constraint']}"
)
def test_build_resolution_matches_the_shared_vectors(entry: dict[str, Any]) -> None:
    resolution = resolve_build(
        entry["kind"], entry["constraint"], entry["builds"], entry["default"]
    )
    assert (resolution.key, resolution.understood) == (entry["key"], entry["understood"])


def _all_patterns(node: object, template: bool = False) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            under = template or key in {
                "requestFieldTemplates",
                "responseFieldTemplates",
                "portEnvTemplates",
                "portLiterals",
                "models",
            }
            if key in {"pattern", "requires"} and isinstance(value, str):
                found.append(value)
            else:
                found.extend(_all_patterns(value, under))
    elif isinstance(node, list):
        for item in node:
            if template and isinstance(item, str):
                found.append(re.sub(r"\{(?:field|env)\}", "word", item))
            else:
                found.extend(_all_patterns(item, template))
    return found


def test_every_rule_pattern_is_in_the_subset_both_languages_share() -> None:
    patterns = _all_patterns(DETECTION_RULES)
    assert len(patterns) > 50
    for pattern in patterns:
        re.compile(pattern, re.MULTILINE | re.ASCII)
        assert not re.search(r"\(\?[<=!P]|\(\?[aiLmsux]|\\[AZ]", pattern), pattern


def test_the_rules_and_the_report_name_their_schemas() -> None:
    assert DETECTION_RULES["schema"] == AGENT_DETECTION_RULES_SCHEMA
    report = detect(MemoryTree(_tree_of(CASES[0].parent / "python-strands")))
    assert report.to_dict()["schema"] == AGENT_DETECTION_SCHEMA


def test_the_scan_never_reads_what_the_default_excludes_drop() -> None:
    files = {
        "requirements.txt": "strands-agents\n",
        "agent.py": "from strands import Agent\n\nagent = Agent()\n",
        ".env": "OPENAI_API_KEY=value\n",
        ".env.local": "ANTHROPIC_API_KEY=value\n",
        ".git/config": "[core]\n",
        ".venv/lib/strands.py": "from strands import Agent\n\nhidden = Agent()\n",
        "node_modules/pkg/index.js": "module.exports = {}\n",
        "pkg/__pycache__/agent.cpython-312.pyc": "binary",
        ".rayito/agent-token": "token\n",
    }
    tree = MemoryTree(files)
    report = detect(tree)
    assert report.entrypoint == "agent:agent"
    forbidden = {".env", ".env.local", ".git/config", ".rayito/agent-token"}
    assert not forbidden.intersection(tree.reads)
    assert not [path for path in tree.reads if path.startswith((".venv/", "node_modules/"))]
    assert report.providers[0].preset == "bedrock"


def test_an_env_example_is_read_for_names_only_and_a_real_env_never() -> None:
    files = {
        "requirements.txt": "strands-agents\n",
        "agent.py": "from strands import Agent\n\nagent = Agent()\n",
        ".env": "OPENAI_API_KEY=secret-value\n",
        ".env.example": "GROQ_API_KEY=\n",
    }
    tree = MemoryTree(files)
    report = detect(tree)
    assert ".env" not in tree.reads
    assert [provider.preset for provider in report.providers] == ["groq"]
    assert "secret-value" not in json.dumps(report.to_dict())


def test_the_report_never_carries_file_contents() -> None:
    marker = "contenido-que-no-debe-salir"
    files = {
        "requirements.txt": "strands-agents\n",
        "agent.py": f"# {marker}\nfrom strands import Agent\n\nagent = Agent(p='{marker}')\n",
    }
    assert marker not in json.dumps(detect(MemoryTree(files)).to_dict())


def test_a_project_with_too_many_files_is_truncated_with_a_warning() -> None:
    files = {f"data/part{index:05d}.txt": "x" for index in range(AGENT_DETECTION_MAX_FILES + 5)}
    files["requirements.txt"] = "strands-agents\n"
    files["agent.py"] = "from strands import Agent\n\nagent = Agent()\n"
    report = detect(MemoryTree(files))
    assert report.entrypoint == "agent:agent"
    assert "scan_truncated" in {warning.code for warning in report.warnings}


def test_files_deeper_than_the_bound_are_ignored() -> None:
    deep = "/".join(["d"] * (AGENT_DETECTION_MAX_DEPTH + 1)) + "/deep.py"
    files = {
        "requirements.txt": "strands-agents\n",
        "agent.py": "from strands import Agent\n\nagent = Agent()\n",
        deep: "from strands import Agent\n\nother = Agent()\n",
    }
    assert detect(MemoryTree(files)).entrypoint == "agent:agent"


def test_a_file_is_read_up_to_the_byte_bound() -> None:
    padding = "# " + "x" * AGENT_DETECTION_MAX_FILE_BYTES + "\n"
    files = {
        "requirements.txt": "strands-agents\n",
        "agent.py": padding + "from strands import Agent\n\nlate = Agent()\n",
        "other.py": "from strands import Agent\n\nearly = Agent()\n",
    }
    assert detect(MemoryTree(files)).entrypoint == "other:early"


def test_detecting_never_runs_the_code_of_the_project(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("strands-agents\n", encoding="utf-8")
    (tmp_path / "agent.py").write_text(
        "from strands import Agent\n\nraise SystemExit('se ejecutó')\nagent = Agent()\n",
        encoding="utf-8",
    )
    assert detect(LocalProjectTree(tmp_path)).entrypoint == "agent:agent"


def test_detecting_makes_no_aws_call_and_loads_no_framework(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    import boto3

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("detect() no debe crear clientes de AWS")

    monkeypatch.setattr(boto3, "client", refuse)
    monkeypatch.setattr(boto3, "Session", refuse)
    before = set(sys.modules)
    detect(MemoryTree(_tree_of(ROOT / "cases" / "python-langgraph-demo")))
    loaded = {name.split(".")[0] for name in set(sys.modules) - before}
    assert not loaded & {"langgraph", "langchain", "strands", "fastapi", "flask"}


@pytest.mark.parametrize(
    "overrides",
    [
        DetectOverrides(language="go"),
        DetectOverrides(framework="crewai"),
        DetectOverrides(protocol="grpc"),
    ],
    ids=str,
)
def test_an_invalid_override_is_an_invalid_value(overrides: DetectOverrides) -> None:
    files = {"requirements.txt": "strands-agents\n", "agent.py": "agent = Agent()\n"}
    with pytest.raises(AgentProjectException) as raised:
        detect(MemoryTree(files), overrides)
    assert raised.value.code == "invalid_value"


def test_a_forced_framework_without_its_dependency_still_looks_for_its_entrypoint() -> None:
    files = {
        "requirements.txt": "requests\n",
        "agent.py": "from strands import Agent\n\nagent = Agent()\n",
    }
    report = detect(MemoryTree(files), DetectOverrides(framework="strands"))
    assert (report.framework, report.entrypoint) == ("strands", "agent:agent")
    assert report.evidence[0].rule == "override:framework"


def test_an_explicit_entrypoint_is_used_as_given() -> None:
    files = {"requirements.txt": "strands-agents\n", "agent.py": "agent = None\n"}
    report = detect(MemoryTree(files), DetectOverrides(entrypoint="pkg.mod:build"))
    assert report.entrypoint == "pkg.mod:build"


@pytest.mark.parametrize(
    ("pattern", "name", "expected"),
    [
        ("requirements*.txt", "requirements-dev.txt", True),
        ("requirements*.txt", "requirements.txt", True),
        ("requirements*.txt", "sub/requirements.txt", False),
        ("*.py", "agent.py", True),
        ("*.py", "agent.pyc", False),
        (".env.*", ".env.local", True),
        (".env", ".env", True),
        ("*.d.ts", "types.d.ts", True),
        ("*dev*", "requirements-dev.txt", True),
    ],
)
def test_globs_only_know_the_star(pattern: str, name: str, expected: bool) -> None:
    assert glob_match(pattern, name) is expected


def test_requirement_names_include_extras_and_normalise() -> None:
    assert requirement_names("Strands_Agents[OpenAI, Gemini]>=1.0") == [
        "strands-agents",
        "strands-agents[openai]",
        "strands-agents[gemini]",
    ]
    assert requirements_dependencies(
        "# nota\n-r base.txt\n\nlanggraph==1.0 ; python_version>'3.10'\n"
    ) == ["langgraph"]


def test_pyproject_dependencies_are_read_without_a_toml_parser() -> None:
    text = (
        '[project]\nname = "x"\ndependencies = [\n    "strands-agents[openai]>=1.0",\n'
        '    "requests",\n]\n\n[project.optional-dependencies]\nextra = ["flask>=3"]\n\n'
        '[tool.poetry.dependencies]\npython = "^3.11"\nfastapi = "^0.1"\n'
    )
    assert pyproject_dependencies(text) == [
        "strands-agents",
        "strands-agents[openai]",
        "requests",
        "flask",
        "fastapi",
    ]


def test_the_runtime_table_of_an_app_carries_its_command_and_not_an_entrypoint() -> None:
    report = detect(MemoryTree(_tree_of(ROOT / "cases" / "python-flask-custom")))
    table = report.runtime_table()
    assert table["kind"] == "custom"
    assert "entrypoint" not in table
    assert table["app"]["command"][:3] == ["python", "-m", "flask"]  # type: ignore[index]
    assert table["app"]["protocol"] == "json"  # type: ignore[index]


def test_filling_a_document_without_runtime_uses_the_detected_one() -> None:
    report = detect(MemoryTree(_tree_of(ROOT / "cases" / "python-strands")))
    filled = fill_runtime({"schema": "x", "name": "demo"}, report)
    assert filled["runtime"] == report.runtime_table()
    assert filled["name"] == "demo"


def test_what_the_file_declares_wins_over_the_detection() -> None:
    report = detect(MemoryTree(_tree_of(ROOT / "cases" / "python-strands")))
    document = {"runtime": {"kind": "strands", "entrypoint": "mi.modulo:otro"}}
    runtime = fill_runtime(document, report)["runtime"]
    assert runtime["entrypoint"] == "mi.modulo:otro"  # type: ignore[index]
    assert runtime["framework"] == "strands"  # type: ignore[index]


def test_a_file_with_another_kind_is_left_untouched() -> None:
    report = detect(MemoryTree(_tree_of(ROOT / "cases" / "python-strands")))
    document = {"runtime": {"kind": "opencode"}}
    assert fill_runtime(document, report)["runtime"] == {"kind": "opencode"}


def test_the_app_table_merges_key_by_key() -> None:
    report = detect(MemoryTree(_tree_of(ROOT / "cases" / "python-flask-custom")))
    document = {"runtime": {"kind": "custom", "app": {"port": 9001}}}
    app = fill_runtime(document, report)["runtime"]["app"]  # type: ignore[index]
    assert app["port"] == 9001
    assert app["health_path"] == "/healthz"


def test_the_local_tree_lists_by_breadth_skips_excluded_folders_and_links(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.js").write_text("x\n", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "tool.py").write_text("x\n", encoding="utf-8")
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("fuera\n", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(outside)
    tree = LocalProjectTree(tmp_path)
    assert tree.list_files(AGENT_DETECTION_MAX_DEPTH, 100) == ["pyproject.toml", "pkg/mod.py"]
    assert tree.read_text("link.txt", 100) is None
    assert tree.read_text("../" + outside.name, 100) is None
    assert tree.read_text("pkg/mod.py", 100) == "x = 1\n"
    assert tree.read_text("pkg/mod.py", 3) == "x ="
    assert tree.read_text("missing.txt", 100) is None


def test_the_local_tree_keeps_the_root_files_when_it_must_truncate(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("x\n", encoding="utf-8")
    (tmp_path / "a").mkdir()
    for index in range(5):
        (tmp_path / "a" / f"f{index}.txt").write_text("x\n", encoding="utf-8")
    assert tree_paths(tmp_path, 3)[0] == "requirements.txt"
    assert len(tree_paths(tmp_path, 3)) == 3


def tree_paths(root: Path, limit: int) -> list[str]:
    return LocalProjectTree(root).list_files(AGENT_DETECTION_MAX_DEPTH, limit)


def test_a_binary_file_is_not_text(tmp_path: Path) -> None:
    (tmp_path / "blob.bin").write_bytes(b"ab\0cd")
    assert LocalProjectTree(tmp_path).read_text("blob.bin", 100) is None
