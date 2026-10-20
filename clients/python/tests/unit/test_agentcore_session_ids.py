"""El id de sesión de AgentCore (`testdata/agentcore-deploy/session-ids.json`):
uno nuevo sin id, el del llamante si ya tiene la forma, y uno derivado
determinista en cualquier otro caso."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

import pytest

from rayito._agentcore._domain import agentcore_session_id
from rayito._limits import AGENTCORE_SESSION_ID_MAX_LENGTH, AGENTCORE_SESSION_ID_MIN_LENGTH
from rayito.exceptions import InvalidArgumentException
from tests.unit.fake_agentcore import DATA

VECTORS: dict[str, Any] = json.loads((DATA / "session-ids.json").read_text("utf-8"))


@pytest.mark.parametrize(
    "case", VECTORS["cases"], ids=lambda case: case["kind"] + case["input"][:12]
)
def test_session_id_vectors(case: dict[str, Any]) -> None:
    assert agentcore_session_id(case["input"]) == case["expected"]


def test_a_call_without_a_session_gets_a_new_one_of_the_right_shape() -> None:
    first = agentcore_session_id(None)
    assert re.fullmatch(VECTORS["newPattern"], first)
    assert first != agentcore_session_id(None)
    assert AGENTCORE_SESSION_ID_MIN_LENGTH <= len(first) <= AGENTCORE_SESSION_ID_MAX_LENGTH


def test_the_new_id_comes_from_the_injected_uuid() -> None:
    fixed = uuid.UUID(int=1)
    assert agentcore_session_id(None, lambda: fixed) == f"rayito-{fixed.hex}"


def test_an_empty_id_is_an_invalid_argument() -> None:
    assert VECTORS["emptyIsError"] is True
    with pytest.raises(InvalidArgumentException, match="session_id"):
        agentcore_session_id("")
