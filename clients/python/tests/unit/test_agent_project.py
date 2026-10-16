"""`AgentProject` (ADR-028, `ai-agent-project`): la descripción tipada,
el fichero `rayito-agent.toml` / `.json` y sus vectores compartidos con
TypeScript (`testdata/agent-project/`). Todo es puro: ninguna prueba crea un
cliente de AWS."""

from __future__ import annotations

import inspect
import json
import tomllib
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from rayito import (
    AgentLimits,
    AgentModel,
    AgentProject,
    AgentSpec,
    AsyncAgentProject,
    InvalidArgumentException,
    SecretGateway,
    ServeOptions,
    UnimplementedError,
    anthropic_gateway,
)
from rayito._agent._project_domain import AGENT_PROJECT_ERROR_CODES, AgentProjectException
from rayito._limits import AGENT_PROJECT_FILE_NAMES, MAX_DURATION_SECONDS

ROOT = Path(__file__).parents[4] / "testdata" / "agent-project"
VALID = sorted((ROOT / "valid").glob("*.toml"))
INVALID = json.loads((ROOT / "invalid" / "cases.json").read_text(encoding="utf-8"))["cases"]
SCHEMA = json.loads((ROOT / "schema.json").read_text(encoding="utf-8"))


def _spec(gateway: str = "anthropic") -> AgentSpec:
    return AgentSpec(model=AgentModel(provider="anthropic", id="claude-haiku-4-5", gateway=gateway))


def _project(**overrides: Any) -> AgentProject:
    fields: dict[str, Any] = {
        "name": "demo",
        "runtime": "opencode",
        "spec": _spec(),
        "gateways": {"anthropic": anthropic_gateway("rayito/anthropic-key")},
    }
    fields.update(overrides)
    return AgentProject(**fields)


@pytest.mark.parametrize("path", VALID, ids=[p.stem for p in VALID])
def test_valid_vectors_have_the_expected_canonical_form(path: Path) -> None:
    expected = json.loads(path.with_suffix(".expected.json").read_text(encoding="utf-8"))
    assert AgentProject.load(path).to_dict() == expected


@pytest.mark.parametrize("path", VALID, ids=[p.stem for p in VALID])
def test_canonical_form_round_trips_through_from_dict_and_json(path: Path, tmp_path: Path) -> None:
    project = AgentProject.load(path)
    canonical = project.to_dict()
    again = AgentProject.from_dict(canonical, base_dir=project.base_dir)
    assert again.to_dict() == canonical
    (tmp_path / "rayito-agent.json").write_text(json.dumps(canonical), encoding="utf-8")
    assert AgentProject.load(tmp_path).to_dict() == canonical


@pytest.mark.parametrize("path", VALID, ids=[p.stem for p in VALID])
def test_schema_accepts_every_valid_vector(path: Path) -> None:
    jsonschema.Draft202012Validator(SCHEMA).validate(tomllib.loads(path.read_text("utf-8")))


@pytest.mark.parametrize("case", INVALID, ids=[c["case"] for c in INVALID])
def test_invalid_vectors_fail_with_their_path_and_code(case: dict[str, Any]) -> None:
    with pytest.raises(AgentProjectException) as raised:
        AgentProject.load(ROOT / "invalid" / case["file"])
    assert raised.value.path == case["path"]
    assert raised.value.code == case["code"]
    assert case["path"] in str(raised.value)
    assert isinstance(raised.value, InvalidArgumentException)


@pytest.mark.parametrize("case", INVALID, ids=[c["case"] for c in INVALID])
def test_schema_rejects_the_structural_invalid_vectors(case: dict[str, Any]) -> None:
    document = tomllib.loads((ROOT / "invalid" / case["file"]).read_text("utf-8"))
    errors = list(jsonschema.Draft202012Validator(SCHEMA).iter_errors(document))
    assert bool(errors) is (case["schema"] == "rejects")


def test_error_codes_are_a_closed_list() -> None:
    assert {case["code"] for case in INVALID} <= set(AGENT_PROJECT_ERROR_CODES)


def test_load_prefers_toml_over_json_in_a_directory(tmp_path: Path) -> None:
    assert AGENT_PROJECT_FILE_NAMES == ("rayito-agent.toml", "rayito-agent.json")
    (tmp_path / "rayito-agent.toml").write_text((ROOT / "valid" / "minimal.toml").read_text())
    (tmp_path / "rayito-agent.json").write_text("{}")
    assert AgentProject.load(tmp_path).name == "hello-agent"


def test_load_accepts_a_file_path_and_reports_a_missing_one(tmp_path: Path) -> None:
    assert AgentProject.load(ROOT / "valid" / "minimal.toml").name == "hello-agent"
    with pytest.raises(AgentProjectException) as raised:
        AgentProject.load(tmp_path)
    assert raised.value.code == "file_not_found"


@pytest.mark.parametrize(("name", "text"), [("a.toml", "name = ["), ("b.json", "[1, 2]")])
def test_an_unreadable_document_is_an_invalid_value(tmp_path: Path, name: str, text: str) -> None:
    (tmp_path / name).write_text(text)
    with pytest.raises(AgentProjectException) as raised:
        AgentProject.load(tmp_path / name)
    assert raised.value.code == "invalid_value"


def test_the_error_of_a_bad_document_never_carries_its_values(tmp_path: Path) -> None:
    (tmp_path / "rayito-agent.toml").write_text('name = "hunter2-secret"\nnope = [')
    with pytest.raises(AgentProjectException) as raised:
        AgentProject.load(tmp_path)
    assert "hunter2" not in str(raised.value)


def test_a_project_without_a_file_reader_cannot_use_instructions_file() -> None:
    document = {
        "schema": "rayito.agent-project/1",
        "name": "demo",
        "runtime": {"kind": "opencode"},
        "model": {"provider": "anthropic", "id": "claude-haiku-4-5", "gateway": "a"},
        "gateways": {"a": {"preset": "anthropic", "secret": "rayito/key"}},
        "agent": {"instructions_file": "AGENTS.md"},
    }
    with pytest.raises(AgentProjectException) as raised:
        AgentProject.from_dict(document, base_dir="/proj")
    assert raised.value.path == "agent.instructions_file"


def test_from_dict_resolves_paths_without_touching_the_disk() -> None:
    document = {
        "schema": "rayito.agent-project/1",
        "name": "demo",
        "runtime": {"kind": "deepagents"},
        "source": {"path": "code", "requirements": "code/requirements.txt"},
        "model": {"provider": "anthropic", "id": "claude-haiku-4-5", "gateway": "a"},
        "gateways": {"a": {"preset": "anthropic", "secret": "rayito/key"}},
    }
    project = AgentProject.from_dict(document, base_dir="/nonexistent/project")
    assert project.source == "/nonexistent/project/code"
    assert project.requirements == "/nonexistent/project/code/requirements.txt"
    assert project.to_dict()["source"] == {"path": "code", "requirements": "code/requirements.txt"}


def test_construction_validates_before_calling_anyone() -> None:
    with pytest.raises(InvalidArgumentException):
        _project(name="Not A Name")
    with pytest.raises(InvalidArgumentException, match="pasarela"):
        _project(gateways={})
    with pytest.raises(InvalidArgumentException):
        _project(memory_mib=1024)
    with pytest.raises(InvalidArgumentException):
        _project(spec="no es un AgentSpec")
    with pytest.raises(InvalidArgumentException):
        _project(allow_internet_access="si")
    with pytest.raises(InvalidArgumentException):
        _project(gateways={"anthropic": "no es una pasarela"})
    with pytest.raises(UnimplementedError):
        _project(runtime="crewai")
    with pytest.raises(InvalidArgumentException, match="CustomRuntime"):
        _project(runtime="custom")


def test_defaults_come_from_the_platform_constants() -> None:
    project = _project()
    assert project.serve == ServeOptions()
    assert project.limits == AgentLimits()
    assert project.allow_internet_access is False
    assert project.egress is None
    assert project.to_dict()["runtime"] == {"kind": "opencode"}
    assert set(project.to_dict()) == {"schema", "name", "runtime", "model", "gateways"}


@pytest.mark.parametrize("port", [8080, 9000, 0, 70000, True, "8088"])
def test_serve_port_is_validated(port: Any) -> None:
    with pytest.raises(InvalidArgumentException):
        ServeOptions(port=port)


@pytest.mark.parametrize("timeout", [0, -1, MAX_DURATION_SECONDS + 1, True])
def test_serve_timeout_is_capped_at_the_platform_maximum(timeout: Any) -> None:
    with pytest.raises(InvalidArgumentException):
        ServeOptions(timeout_seconds=timeout)


def test_serve_options_are_frozen_and_copy_their_metadata() -> None:
    metadata = {"team": "support"}
    serve = ServeOptions(metadata=metadata)
    metadata["team"] = "other"
    assert serve.metadata == {"team": "support"}
    with pytest.raises(TypeError):
        serve.metadata["x"] = "y"  # type: ignore[index]


def test_gateways_are_copied_and_secret_free() -> None:
    gateway = anthropic_gateway("rayito/anthropic-key")
    project = _project(gateways={"anthropic": gateway})
    assert isinstance(project.gateways["anthropic"], SecretGateway)
    assert "rayito/anthropic-key" in json.dumps(project.to_dict())
    assert "Bearer" not in json.dumps(project.to_dict())


def test_async_project_has_the_same_constructor_and_load() -> None:
    assert inspect.signature(AsyncAgentProject) == inspect.signature(AgentProject)
    assert isinstance(AsyncAgentProject.load(ROOT / "valid" / "minimal.toml"), AsyncAgentProject)


def test_public_methods_have_equal_signatures_in_sync_and_async() -> None:
    names = sorted(
        name
        for name, member in inspect.getmembers(AgentProject)
        if not name.startswith("_") and callable(member)
    )
    for name in names:
        assert list(inspect.signature(getattr(AsyncAgentProject, name)).parameters.items()) == list(
            inspect.signature(getattr(AgentProject, name)).parameters.items()
        ), name


def test_loading_and_writing_make_no_aws_call(monkeypatch: pytest.MonkeyPatch) -> None:
    import boto3
    import boto3.session

    calls: list[str] = []

    def record(name: str) -> Any:
        def factory(*args: Any, **kwargs: Any) -> Any:
            calls.append(name)
            raise AssertionError(name)

        return factory

    monkeypatch.setattr(boto3, "client", record("boto3.client"))
    monkeypatch.setattr(boto3, "Session", record("boto3.Session"))
    monkeypatch.setattr(boto3.session.Session, "client", record("Session.client"))
    for path in VALID:
        AgentProject.load(path).to_dict()
    _project().to_dict()
    assert calls == []
