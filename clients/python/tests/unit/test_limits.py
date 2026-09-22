"""`rayito._limits` es una vista generada de `limits.json` (raíz del repo):
cada constante debe valer lo que dice el JSON. El test se salta cuando el
JSON no está (paquete instalado desde una wheel)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import cast

import pytest

from rayito import _limits

LIMITS_JSON = Path(__file__).resolve().parents[4] / "limits.json"
SET_KEYS = frozenset({"terminalStates", "suspendedStates", "managedNetworkConnectors"})
CONSTANT_NAME_OVERRIDES = {
    "s3BucketNameMin": "S3_BUCKET_NAME_MIN",
    "s3BucketNameMax": "S3_BUCKET_NAME_MAX",
}


def constant_name(key: str) -> str:
    """Mirrors ``scripts/gen_limits.py``: a service name with its own digit (``s3``)
    keeps the digit attached instead of becoming ``S_3``."""
    if key in CONSTANT_NAME_OVERRIDES:
        return CONSTANT_NAME_OVERRIDES[key]
    with_digits = re.sub(r"([a-zA-Z])(\d)", r"\1_\2", key)
    with_words = re.sub(r"(\d)([a-zA-Z])", r"\1_\2", with_digits)
    return re.sub(r"([a-z])([A-Z])", r"\1_\2", with_words).upper()


def expected_value(key: str, value: object) -> object:
    if isinstance(value, list):
        return frozenset(value) if key in SET_KEYS else tuple(value)
    return value


@pytest.fixture(scope="module")
def limits() -> dict[str, object]:
    if not LIMITS_JSON.exists():
        pytest.skip("limits.json no está en el árbol (instalación desde wheel)")
    with LIMITS_JSON.open(encoding="utf-8") as handle:
        return cast("dict[str, object]", json.load(handle))


def test_every_json_limit_matches_the_rendered_constant(limits: dict[str, object]) -> None:
    mismatches = {
        constant_name(key): (getattr(_limits, constant_name(key), None), expected_value(key, value))
        for key, value in limits.items()
        if getattr(_limits, constant_name(key), None) != expected_value(key, value)
    }
    assert mismatches == {}, f"constantes que difieren de limits.json: {mismatches}"


def test_every_rendered_constant_exists_in_the_json(limits: dict[str, object]) -> None:
    rendered = {name for name in dir(_limits) if name.isupper()}
    assert rendered == {constant_name(key) for key in limits}


def test_values_are_the_ones_of_rayito_0_0_5() -> None:
    assert _limits.MAX_DURATION_SECONDS == 28800
    assert _limits.API_TPS["SuspendMicrovm"] == 2
    assert frozenset({"TERMINATING", "TERMINATED"}) == _limits.TERMINAL_STATES
    assert _limits.DEFAULT_PORT == 8080
    assert _limits.TOKEN_REFRESH_AFTER_MINUTES == 45
