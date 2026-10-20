"""Los ARNs que el rol del runtime recibe para Bedrock y Secrets Manager
(`testdata/agentcore-deploy/model-arns.json`), en orden y sin duplicados."""

from __future__ import annotations

import json
from typing import Any

import pytest

from rayito._agentcore._domain import bedrock_resource_arns, secret_resource_arns
from rayito.exceptions import AgentCoreException
from tests.unit.fake_agentcore import DATA

VECTORS: dict[str, Any] = json.loads((DATA / "model-arns.json").read_text("utf-8"))


@pytest.mark.parametrize("case", VECTORS["bedrock"], ids=lambda case: case["name"])
def test_bedrock_arns(case: dict[str, Any]) -> None:
    arns = bedrock_resource_arns(
        case["models"], case["modelRegion"], case["account"], case["partition"]
    )
    assert list(arns) == case["expected"]


@pytest.mark.parametrize("case", VECTORS["secrets"], ids=lambda case: case["name"])
def test_secret_arns(case: dict[str, Any]) -> None:
    arns = secret_resource_arns(case["secrets"], case["region"], case["account"], case["partition"])
    assert list(arns) == case["expected"]


@pytest.mark.parametrize("case", VECTORS["rejected"], ids=lambda case: case["name"])
def test_rejected_inputs_are_iam_resource_invalid(case: dict[str, Any]) -> None:
    resolve = secret_resource_arns if case["kind"] == "secret" else bedrock_resource_arns
    with pytest.raises(AgentCoreException) as raised:
        resolve([case["value"]], case["region"], case["account"], case["partition"])
    assert raised.value.code == "iam_resource_invalid"
