"""Configuración cero de OpenCode (ADR-034 D8): un proyecto con sólo
`opencode.json(c)` y `AGENTS.md` despliega sus instrucciones y sus permisos;
lo que escribe `rayito-agent.toml` gana, lo que no se puede aplicar se avisa
y el informe nunca lleva el texto de los ficheros."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rayito import AgentProject
from rayito._agent._detect_opencode import strip_jsonc
from rayito._agent._detect_report import DetectionEvidence
from rayito._agent._opencode import OPENCODE_INSTRUCTIONS_PATH, OpenCodeRuntime

REGION = "us-east-1"
MODEL = "amazon-bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0"
INSTRUCTIONS = "# Reglas\n\nResponde en español y cita el fichero que lees.\n"
GATEWAY_URL = "http://127.0.0.1:18001"


def _project(
    tmp_path: Path, config: dict[str, object] | str, *, agents_md: str | None = INSTRUCTIONS
) -> Path:
    text = config if isinstance(config, str) else json.dumps({"model": MODEL, **config})
    name = "opencode.jsonc" if isinstance(config, str) else "opencode.json"
    (tmp_path / name).write_text(text, encoding="utf-8")
    if agents_md is not None:
        (tmp_path / "AGENTS.md").write_text(agents_md, encoding="utf-8")
    return tmp_path


def _served(project: AgentProject) -> dict[str, bytes]:
    gateways = dict.fromkeys(project.gateways, GATEWAY_URL)
    files = OpenCodeRuntime().build_config(project.spec, gateway_urls=gateways, workdir="/w")
    return {item.path: item.data for item in files.files}


def _served_permission(project: AgentProject) -> dict[str, object]:
    config = json.loads(next(iter(_served(project).values())))
    return dict(config["agent"]["build"]["permission"])


def test_agents_md_becomes_the_served_agents_md(tmp_path: Path) -> None:
    project, report = AgentProject.detect(_project(tmp_path, {}), region=REGION)
    assert project.spec.instructions == INSTRUCTIONS
    assert _served(project)[OPENCODE_INSTRUCTIONS_PATH] == INSTRUCTIONS.encode()
    assert report is not None
    assert DetectionEvidence("AGENTS.md", "file:AGENTS.md") in report.evidence


def test_permission_rules_round_trip_into_the_runtime_config(tmp_path: Path) -> None:
    permission = {"edit": "deny", "bash": {"*": "deny", "git status": "allow"}}
    project, report = AgentProject.detect(
        _project(tmp_path, {"permission": permission}), region=REGION
    )
    served = _served_permission(project)
    assert served["edit"] == "deny"
    assert served["bash"] == {"*": "deny", "git status": "allow"}
    assert report is not None
    assert DetectionEvidence("opencode.json", "config:permission") in report.evidence
    assert report.warnings == ()


def test_ask_is_served_as_deny_with_a_warning(tmp_path: Path) -> None:
    project, report = AgentProject.detect(
        _project(tmp_path, {"permission": {"bash": {"*": "ask", "ls": "allow"}}}), region=REGION
    )
    assert _served_permission(project)["bash"] == {"*": "deny", "ls": "allow"}
    assert report is not None
    assert [warning.code for warning in report.warnings] == ["opencode_permission_ignored"]


def test_the_project_file_wins_over_agents_md_and_the_permissions(tmp_path: Path) -> None:
    directory = _project(tmp_path, {"permission": {"edit": "deny"}})
    (directory / "rayito-agent.toml").write_text(
        '[agent]\ninstructions = "Del fichero de proyecto."\n\n'
        '[agent.permissions]\ndefault = "deny"\n',
        encoding="utf-8",
    )
    project, _ = AgentProject.detect(directory, region=REGION)
    assert project.spec.instructions == "Del fichero de proyecto."
    assert project.spec.permissions.default == "deny"
    assert "edit" not in project.spec.permissions.tools


def test_an_instructions_file_of_the_project_file_wins_over_agents_md(tmp_path: Path) -> None:
    directory = _project(tmp_path, {"permission": {"edit": "deny"}})
    (directory / "PROMPT.md").write_text("Otro prompt.\n", encoding="utf-8")
    (directory / "rayito-agent.toml").write_text(
        '[agent]\ninstructions_file = "PROMPT.md"\n', encoding="utf-8"
    )
    project, _ = AgentProject.detect(directory, region=REGION)
    assert project.spec.instructions == "Otro prompt.\n"
    assert project.spec.permissions.tools["edit"] == "deny"


def test_an_unknown_permission_key_warns_and_is_ignored(tmp_path: Path) -> None:
    project, report = AgentProject.detect(
        _project(tmp_path, {"permission": {"edit": "deny", "Remote Tool": "allow"}}),
        region=REGION,
    )
    assert dict(project.spec.permissions.tools) == {"edit": "deny"}
    assert report is not None
    assert [(warning.code, warning.detail) for warning in report.warnings] == [
        (
            "opencode_permission_ignored",
            "opencode.json: permission.<clave> no es una herramienta con allow, ask o deny "
            "(o patrones de ellas) y se ignora",
        )
    ]


def test_a_jsonc_config_with_comments_and_trailing_commas_is_read(tmp_path: Path) -> None:
    config = (
        "{\n  // modelo\n"
        f'  "model": "{MODEL}",\n'
        '  /* permisos */ "permission": {"edit": "deny",},\n}\n'
    )
    project, _ = AgentProject.detect(_project(tmp_path, config), region=REGION)
    assert project.spec.permissions.tools["edit"] == "deny"


def test_an_empty_agents_md_gives_no_instructions(tmp_path: Path) -> None:
    project, report = AgentProject.detect(_project(tmp_path, {}, agents_md=" \n"), region=REGION)
    assert project.spec.instructions is None
    assert report is not None
    assert all(item.rule != "file:AGENTS.md" for item in report.evidence)


def test_without_agents_md_nor_permission_the_project_is_unchanged(tmp_path: Path) -> None:
    project, _ = AgentProject.detect(_project(tmp_path, {}, agents_md=None), region=REGION)
    assert project.spec.instructions is None
    assert project.to_dict().get("agent") is None


def test_the_report_never_carries_agents_md_nor_the_permission_text(tmp_path: Path) -> None:
    marker = "contenido-que-no-debe-salir"
    _, report = AgentProject.detect(
        _project(
            tmp_path,
            {"permission": {"bash": {f"echo {marker}": "allow"}, marker: "allow"}},
            agents_md=f"{marker}\n",
        ),
        region=REGION,
    )
    assert report is not None
    assert marker not in json.dumps(report.to_dict())


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"a": "x // y"} // fin', '{"a": "x // y"} '),
        ('{"a": "/* no */", /* sí */ "b": 1}', '{"a": "/* no */",   "b": 1}'),
        ('{"a": [1, 2,], "b": "\\",",}', '{"a": [1, 2], "b": "\\","}'),
        ('{"a": 1, // c\n}', '{"a": 1 \n}'),
    ],
)
def test_strip_jsonc_only_touches_comments_and_trailing_commas(text: str, expected: str) -> None:
    assert strip_jsonc(text) == expected
    json.loads(strip_jsonc(text))
