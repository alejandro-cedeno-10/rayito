"""El plan de instalación de un agente que ya existe (`_install_plan.py`, 0.14
D1 y D3) contra los vectores que comparte con TypeScript
(`testdata/agent/install-plans/`): los pasos de Node (con `npm`, `pnpm` y
`yarn`) y de CPython fijado, `uv`, y las dependencias de Python con `pip` y
`uv`. Los pines son los de `limits.json`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rayito._agent import _install_plan as plan
from rayito._agent._hermes import HERMES_PYTHON_MINOR, HermesRuntime
from rayito._agent._runtime import InstallTarget
from rayito._limits import (
    AGENT_DEFAULT_NODE_MAJOR,
    AGENT_DEFAULT_PYTHON_MINOR,
    AGENT_NODE_SHA256S,
    AGENT_NODE_VERSIONS,
    AGENT_PYTHON_BUILD_SHA256S,
    AGENT_PYTHON_BUILD_VERSIONS,
)
from rayito.exceptions import InvalidArgumentException

VECTORS = json.loads(
    (
        Path(__file__).resolve().parents[4]
        / "testdata"
        / "agent"
        / "install-plans"
        / "install-plans.json"
    ).read_text("utf-8")
)
CASES: list[dict[str, Any]] = VECTORS["cases"]


def _call(case: dict[str, Any]) -> Any:
    call = case["call"]
    target = InstallTarget(**call["target"])
    args = call["args"]
    match call["fn"]:
        case "node_runtime_steps":
            return plan.node_runtime_steps(target, args.get("node_version"))
        case "node_dependency_steps":
            return plan.node_dependency_steps(target, args["manager"])
        case "python_runtime_steps":
            return plan.python_runtime_steps(target, args.get("python_version"))
        case "python_interpreter":
            return plan.python_interpreter(target, args.get("python_version"))
        case "python_download_command":
            return plan.python_download_command(
                args["minor"], args["parent_dir"], args["tarball_path"]
            )
        case "uv_steps":
            return plan.uv_steps(target)
        case "python_dependency_steps":
            return plan.python_dependency_steps(target, **args)
    raise AssertionError(call["fn"])


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_the_plan_matches_the_shared_vectors(case: dict[str, Any]) -> None:
    expected = case["expected"]
    if "error" in expected:
        with pytest.raises(InvalidArgumentException):
            _call(case)
        return
    value = _call(case)
    if "value" in expected:
        assert value == expected["value"]
        return
    assert [{"op": step.op, "args": dict(step.args)} for step in value] == expected["steps"]


def test_every_pinned_build_has_a_version_and_a_sha256() -> None:
    assert set(AGENT_PYTHON_BUILD_VERSIONS) == set(AGENT_PYTHON_BUILD_SHA256S)
    assert set(AGENT_NODE_VERSIONS) == set(AGENT_NODE_SHA256S)
    assert AGENT_DEFAULT_NODE_MAJOR in AGENT_NODE_VERSIONS
    assert AGENT_DEFAULT_PYTHON_MINOR in AGENT_PYTHON_BUILD_VERSIONS
    for minor, version in AGENT_PYTHON_BUILD_VERSIONS.items():
        assert version.startswith(f"{minor}.")
    for major, version in AGENT_NODE_VERSIONS.items():
        assert version.startswith(f"{major}.")
    for digest in (*AGENT_PYTHON_BUILD_SHA256S.values(), *AGENT_NODE_SHA256S.values()):
        assert len(digest) == 64


def test_hermes_uses_the_shared_pinned_python() -> None:
    steps = HermesRuntime().install_steps(InstallTarget())
    command = str(steps[0].args["cmd"])
    assert AGENT_PYTHON_BUILD_SHA256S[HERMES_PYTHON_MINOR] in command
    assert f"cpython-{AGENT_PYTHON_BUILD_VERSIONS[HERMES_PYTHON_MINOR]}%2B" in command


@pytest.mark.parametrize(
    ("manager", "kind"),
    [("pip", "python"), ("uv", "python"), ("npm", "node"), ("pnpm", "node"), ("yarn", "node")],
)
def test_the_ecosystem_of_a_package_manager(manager: str, kind: str) -> None:
    assert plan.ecosystem(manager) == kind


def test_an_unknown_package_manager_is_rejected() -> None:
    with pytest.raises(InvalidArgumentException, match="package_manager"):
        plan.ecosystem("bun")


@pytest.mark.parametrize("manager", plan.NODE_PACKAGE_MANAGERS)
def test_every_node_manager_has_a_lockfile_and_a_frozen_install(manager: str) -> None:
    manifest, lockfile = plan.node_package_files(manager)
    assert (manifest, lockfile) == ("package.json", plan.NODE_LOCKFILES[manager])
    steps = plan.node_dependency_steps(InstallTarget(), manager)
    assert [step.args["src"] for step in steps[:2]] == [f"app/{manifest}", f"app/{lockfile}"]
    install = str(steps[-1].args["cmd"])
    assert "--ignore-scripts" in install
    assert "frozen" in install or " ci " in install
