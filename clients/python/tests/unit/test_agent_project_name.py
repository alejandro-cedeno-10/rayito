"""El nombre de proyecto que sale de un directorio: vectores de
`testdata/agent-project/project-names.json`, los mismos que lee TypeScript."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

import pytest

from rayito._agent._project_file import (
    DEFAULT_PROJECT_NAME,
    MAX_PROJECT_NAME_CHARS,
    project_name_of,
)

REPO_ROOT_DEPTH: Final = 4
VECTORS: Final[dict[str, Any]] = json.loads(
    (
        Path(__file__).resolve().parents[REPO_ROOT_DEPTH]
        / "testdata"
        / "agent-project"
        / "project-names.json"
    ).read_text(encoding="utf-8")
)
CASES: Final[list[dict[str, str]]] = VECTORS["cases"]


def test_the_vectors_use_the_named_constants() -> None:
    assert VECTORS["maxLength"] == MAX_PROJECT_NAME_CHARS
    assert VECTORS["defaultName"] == DEFAULT_PROJECT_NAME


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_project_name_of_follows_the_shared_vectors(case: dict[str, str], tmp_path: Path) -> None:
    assert project_name_of(tmp_path / case["directory"]) == case["name"]
