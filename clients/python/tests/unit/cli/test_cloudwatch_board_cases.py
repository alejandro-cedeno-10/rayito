"""Los dashboards de `testdata/dashboard/cloudwatch-board-cases.json`: la CLI
de TypeScript genera exactamente estos bytes (ADR-027), y este test fija que
los de Python no cambian sin que se regeneren a propósito."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

import pytest

from rayito._dashboard._cloudwatch_board import build_board, render_board
from rayito.exceptions import InvalidArgumentException

REPO_ROOT_DEPTH: Final = 5
CASES_FILE: Final = (
    Path(__file__).resolve().parents[REPO_ROOT_DEPTH]
    / "testdata"
    / "dashboard"
    / "cloudwatch-board-cases.json"
)
SHARED: Final[dict[str, Any]] = json.loads(CASES_FILE.read_text(encoding="utf-8"))
DEFAULT_PREFIX: Final = "/rayito/"
INDENT: Final = 2


def board_of(options: dict[str, Any]) -> dict[str, Any]:
    return build_board(
        region=options.get("region", "us-east-1"),
        log_group_prefix=options.get("logGroupPrefix", DEFAULT_PREFIX),
        log_groups=options.get("logGroups", []),
        include_otlp=options.get("includeOtlp", False),
        include_quota=options.get("includeQuota", False),
    )


@pytest.mark.parametrize("case", SHARED["cases"], ids=lambda case: case["name"])
def test_board_matches_the_shared_case(case: dict[str, Any]) -> None:
    assert render_board(board_of(case["options"]), indent=INDENT) == case["rendered"]


@pytest.mark.parametrize("case", SHARED["invalid"], ids=lambda case: case["name"])
def test_invalid_input_is_refused_with_the_shared_message(case: dict[str, Any]) -> None:
    with pytest.raises(InvalidArgumentException) as raised:
        board_of(case["options"])
    assert str(raised.value) == case["message"]
