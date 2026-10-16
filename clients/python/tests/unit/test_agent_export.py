"""`AgentProject.export()` (`_agent/_export.py`): el árbol de ficheros y el
`Dockerfile` de cada caso de `testdata/agent/export/` (los dos SDK leen los
mismos), el rechazo de un directorio no vacío y que no hay ninguna llamada
de red ni cliente de AWS. `RAYITO_UPDATE_SNAPSHOTS=1` regenera los
esperados."""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any

import pytest

from rayito._agent._export import EXPORT_PLATFORM, AgentExport, export_project, render_export
from rayito._agent._project_domain import AgentProject
from rayito._limits import AGENT_EXPORT_BASE_IMAGE_DIGEST, AGENT_SERVICE_EXPORT_PORT
from rayito.exceptions import InvalidArgumentException
from tests.unit.fake_agent import FakeAgentRuntime
from tests.unit.fake_agent_project import project

EXPORT_DIR = Path(__file__).resolve().parents[4] / "testdata" / "agent" / "export"
CASES = sorted(path.name for path in EXPORT_DIR.iterdir() if (path / "case.json").is_file())
HOST_PREFIX = "agent_host/"
SNAPSHOT_FILES = ("Dockerfile", "host.json", ".dockerignore", "README.md")
UPDATE = os.environ.get("RAYITO_UPDATE_SNAPSHOTS") == "1"


def _load_case(name: str) -> tuple[AgentProject, str]:
    case_dir = EXPORT_DIR / name
    case = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
    return AgentProject.from_dict(case["project"], base_dir=str(case_dir)), str(case["target"])


def _tree(name: str) -> dict[str, bytes]:
    the_project, target = _load_case(name)
    return render_export(the_project, target=target)


def _names(tree: dict[str, bytes]) -> list[str]:
    """Los ficheros del export sin los del host, que cambian con cada driver
    nuevo (sólo se comprueba que viajan)."""
    return sorted(path for path in tree if not path.startswith(HOST_PREFIX))


@pytest.mark.parametrize("name", CASES)
def test_export_matches_the_snapshot(name: str) -> None:
    tree = _tree(name)
    case_dir = EXPORT_DIR / name
    if UPDATE:
        for file in SNAPSHOT_FILES:
            (case_dir / "expected" / file).parent.mkdir(exist_ok=True)
            (case_dir / "expected" / file).write_bytes(tree[file])
        (case_dir / "expected" / "files.json").write_text(
            json.dumps(_names(tree), indent=2) + "\n", encoding="utf-8"
        )
    for file in SNAPSHOT_FILES:
        assert tree[file].decode() == (case_dir / "expected" / file).read_text(encoding="utf-8"), (
            file
        )
    expected = json.loads((case_dir / "expected" / "files.json").read_text(encoding="utf-8"))
    assert _names(tree) == expected
    assert f"{HOST_PREFIX}__main__.py" in tree and f"{HOST_PREFIX}serve.py" in tree


@pytest.mark.parametrize("name", CASES)
def test_the_dockerfile_is_arm64_pinned_and_serves_on_8080(name: str) -> None:
    dockerfile = _tree(name)["Dockerfile"].decode()
    assert f"--platform={EXPORT_PLATFORM}" in dockerfile
    assert f"@{AGENT_EXPORT_BASE_IMAGE_DIGEST}" in dockerfile
    assert f"EXPOSE {AGENT_SERVICE_EXPORT_PORT}" in dockerfile
    assert f'"--port", "{AGENT_SERVICE_EXPORT_PORT}"' in dockerfile
    assert "USER user" in dockerfile
    assert "RAYITO_AGENT_SERVICE_TOKEN" not in dockerfile


@pytest.mark.parametrize("name", CASES)
def test_the_exported_host_config_has_no_ready_marker_and_no_secret(name: str) -> None:
    host = json.loads(_tree(name)["host.json"])
    assert "ready_marker" not in host
    assert "bedrock-key" not in json.dumps(host)


def test_export_writes_the_tree_and_describes_it(tmp_path: Path) -> None:
    the_project, target = _load_case(CASES[0])
    out = tmp_path / "build"
    export = export_project(the_project, target=target, out_dir=out)
    assert isinstance(export, AgentExport)
    assert (export.port, export.platform, export.path) == (
        AGENT_SERVICE_EXPORT_PORT,
        EXPORT_PLATFORM,
        str(out),
    )
    assert list(export.files) == sorted(export.files)
    assert {str(p.relative_to(out)) for p in out.rglob("*") if p.is_file()} == set(export.files)
    assert (out / "Dockerfile").read_bytes() == _tree(CASES[0])["Dockerfile"]


def test_export_refuses_a_non_empty_directory_unless_overwrite(tmp_path: Path) -> None:
    the_project, target = _load_case(CASES[0])
    (tmp_path / "ya-estaba.txt").write_text("mío")
    with pytest.raises(InvalidArgumentException, match="no está vacío"):
        export_project(the_project, target=target, out_dir=tmp_path)
    assert [p.name for p in tmp_path.iterdir()] == ["ya-estaba.txt"]
    export_project(the_project, target=target, out_dir=tmp_path, overwrite=True)
    assert (tmp_path / "ya-estaba.txt").read_text() == "mío"
    assert (tmp_path / "Dockerfile").is_file()


def test_export_accepts_an_existing_empty_directory(tmp_path: Path) -> None:
    the_project, target = _load_case(CASES[0])
    export_project(the_project, target=target, out_dir=tmp_path)
    assert (tmp_path / "Dockerfile").is_file()


def test_unknown_target_is_rejected_before_writing(tmp_path: Path) -> None:
    with pytest.raises(InvalidArgumentException, match="target"):
        export_project(project(), target="ecs", out_dir=tmp_path / "x")
    assert not (tmp_path / "x").exists()


def test_a_runtime_that_cannot_be_installed_or_served_fails_before_writing(tmp_path: Path) -> None:
    with pytest.raises(InvalidArgumentException, match="Runtime"):
        export_project(project(runtime=FakeAgentRuntime()), out_dir=tmp_path / "x")
    assert not (tmp_path / "x").exists()


def test_the_readme_states_that_the_gateway_guarantee_does_not_apply() -> None:
    readme = _tree(CASES[0])["README.md"].decode()
    assert "no hay pasarela" in readme
    assert "sólo vale dentro de Rayito" in readme


def test_agentcore_adds_its_instructions() -> None:
    names = {name: _tree(name)["README.md"].decode() for name in CASES}
    agentcore = [text for text in names.values() if "Bedrock AgentCore Runtime" in text]
    docker = [text for text in names.values() if "Bedrock AgentCore Runtime" not in text]
    assert agentcore and docker


def test_export_makes_no_network_call_and_creates_no_aws_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []

    def forbidden(*args: Any, **kwargs: Any) -> None:
        calls.append("llamada")
        raise AssertionError("export() no debe usar la red")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr("boto3.session.Session.client", forbidden)
    monkeypatch.setattr("boto3.client", forbidden)
    for name in CASES:
        the_project, target = _load_case(name)
        export_project(the_project, target=target, out_dir=tmp_path / name)
    assert calls == []
