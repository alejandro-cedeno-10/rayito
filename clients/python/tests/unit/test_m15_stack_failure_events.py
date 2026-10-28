"""`first_failed_resource` / `describe_failed_resource` (dominio puro) contra
los vectores compartidos con TypeScript (`testdata/stacks/failure-events.json`):
el recurso más antiguo que falló en la última operación, nunca uno de un
despliegue anterior."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rayito._stacks._model import StackEvent, describe_failed_resource, first_failed_resource

VECTORS = json.loads(
    (Path(__file__).resolve().parents[4] / "testdata" / "stacks" / "failure-events.json").read_text(
        encoding="utf-8"
    )
)["cases"]


def _event(raw: dict[str, Any]) -> StackEvent:
    return StackEvent(
        logical_resource_id=raw["logicalResourceId"],
        resource_status=raw["resourceStatus"],
        resource_status_reason=raw["resourceStatusReason"],
    )


@pytest.mark.parametrize("case", VECTORS, ids=[case["name"] for case in VECTORS])
def test_the_shared_vectors(case: dict[str, Any]) -> None:
    found = first_failed_resource([_event(raw) for raw in case["events"]], case["stackName"])
    if case["expected"] is None:
        assert found is None
        assert case["expectedText"] is None
        return
    assert found == _event(case["expected"])
    assert describe_failed_resource(found) == case["expectedText"]
