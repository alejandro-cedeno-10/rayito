"""Cada caso de `testdata/cli/command-cases.json` con la CLI de Python: el
código de salida, stdout y los efectos son los que la CLI de TypeScript
tiene que reproducir (ADR-027). Si cambia la salida de un comando, el caso se
regenera a propósito y TypeScript lo sigue."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

import pytest

from .command_cases_runner import run_case

REPO_ROOT_DEPTH: Final = 5
CASES_FILE: Final = (
    Path(__file__).resolve().parents[REPO_ROOT_DEPTH] / "testdata" / "cli" / "command-cases.json"
)
CASES: Final[list[dict[str, Any]]] = json.loads(CASES_FILE.read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_command_case(case: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    got = run_case(case, monkeypatch)
    expected = case["expected"]
    assert got["exitCode"] == expected["exitCode"], got["stderr"]
    assert got["stdout"] == expected["stdout"]
    assert got["effects"] == expected["effects"]
    for needle in case.get("stderrIncludes", []):
        assert needle in got["stderr"] + got["stdout"]


def test_cases_cover_every_phase_one_command() -> None:
    commands = {
        " ".join(word for word in case["args"] if not word.startswith("-")) for case in CASES
    }
    for command in (
        "sandbox list",
        "sandbox kill",
        "sandbox logs",
        "stack list",
        "stack status",
        "stack deploy",
        "stack destroy",
        "image list",
        "image sizes",
    ):
        assert any(entry.startswith(command) for entry in commands), command
    assert any(case["args"][:3] == ["--json", "sandbox", "info"] for case in CASES)
