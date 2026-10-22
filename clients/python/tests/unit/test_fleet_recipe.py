"""La receta de la flota (ADR-034 D1-D2) contra los vectores compartidos
`testdata/fleet-recipe/recipes.json` y el contrato `contract.json`: el
documento, los bytes exactos del JSON, el sobre `gzip+base64`, lo que se
rechaza y el nombre del parámetro."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from rayito import _limits
from rayito._agent._fleet_recipe import (
    RECIPE_ENCODING_GZIP,
    RECIPE_UNREADABLE,
    RECIPE_WARNING_CODES,
    RecipeWarning,
    decode_recipe,
    encode_recipe,
    recipe_document,
    recipe_from_document,
    recipe_of,
    recipe_parameter_name,
    recipe_project,
)
from rayito._agent._fleet_recipe_aws import (
    RECIPE_SAVED,
    load_recipe,
    store_recipe,
)
from rayito._agent._scaling_domain import ScalingOptions
from tests.unit.fake_fleet_recipe import MemoryRecipeStore, denied

TESTDATA = Path(__file__).resolve().parents[4] / "testdata" / "fleet-recipe"
VECTORS: dict[str, Any] = json.loads((TESTDATA / "recipes.json").read_text(encoding="utf-8"))
CONTRACT: dict[str, Any] = json.loads((TESTDATA / "contract.json").read_text(encoding="utf-8"))
CASES = {case["id"]: case for case in VECTORS["cases"]}
SMALL: dict[str, Any] = CASES["small-bedrock"]["document"]


def document_of(case: dict[str, Any]) -> dict[str, Any]:
    """El documento de un caso: el suyo o `small-bedrock` con las
    instrucciones que describe."""
    if "document" in case:
        return dict(case["document"])
    document = copy.deepcopy(SMALL)
    spec = case["instructions"]
    if "repeat" in spec:
        text = spec["repeat"] * spec["times"]
    else:
        text = sha256_hex(spec["sha256HexChars"])
    document["project"].setdefault("agent", {})["instructions"] = text
    return document


def sha256_hex(chars: int) -> str:
    text, index = "", 0
    while len(text) < chars:
        text += hashlib.sha256(str(index).encode()).hexdigest()
        index += 1
    return text[:chars]


def test_the_limits_are_the_contract_ones() -> None:
    limits = CONTRACT["limits"]
    assert limits["agentRecipeParameterTemplate"] == _limits.AGENT_RECIPE_PARAMETER_TEMPLATE
    assert limits["agentRecipeSchema"] == _limits.AGENT_RECIPE_SCHEMA
    assert limits["agentRecipeMaxBytes"] == _limits.AGENT_RECIPE_MAX_BYTES
    assert limits["agentRecipeDescription"] == _limits.AGENT_RECIPE_DESCRIPTION
    assert limits["gatewayKeyRenewAfterSeconds"] == _limits.GATEWAY_KEY_RENEW_AFTER_SECONDS
    assert limits["gatewayKeyExpirySeconds"] == _limits.GATEWAY_KEY_EXPIRY_SECONDS


def test_the_warning_codes_are_the_contract_ones() -> None:
    recipe_codes = [code for code in CONTRACT["warningCodes"] if code.startswith("recipe_")]
    assert list(RECIPE_WARNING_CODES) == recipe_codes


def test_a_small_recipe_is_the_exact_json_of_the_vector() -> None:
    case = CASES["small-bedrock"]
    assert case["encoding"] == "json"
    assert encode_recipe(case["document"]) == case["value"]


def test_a_large_recipe_becomes_the_gzip_envelope() -> None:
    case = CASES["large-instructions"]
    document = document_of(case)

    value = encode_recipe(document)

    assert value is not None
    envelope = json.loads(value)
    assert envelope["schema"] == _limits.AGENT_RECIPE_SCHEMA
    assert envelope["encoding"] == RECIPE_ENCODING_GZIP == case["encoding"]
    assert len(value.encode("utf-8")) <= _limits.AGENT_RECIPE_MAX_BYTES
    assert recipe_document(decode_recipe(value)) == document


@pytest.mark.parametrize("case_id", ["small-bedrock", "large-instructions"])
def test_the_value_of_each_vector_reads_back_as_its_document(case_id: str) -> None:
    case = CASES[case_id]
    recipe = decode_recipe(case["value"])
    assert recipe_document(recipe) == document_of(case)
    assert recipe_project(recipe).gateway_presets == {"model": "bedrock"}


def test_a_recipe_that_does_not_fit_even_compressed_is_not_encoded() -> None:
    case = CASES["too-large"]
    assert case["warning"] == "recipe_too_large"
    assert encode_recipe(document_of(case)) is None


@pytest.mark.parametrize("case", VECTORS["rejected"], ids=lambda case: case["id"])
def test_an_unknown_schema_or_encoding_is_unreadable(case: dict[str, Any]) -> None:
    with pytest.raises(RecipeWarning) as caught:
        decode_recipe(case["value"])
    assert caught.value.code == case["warning"] == RECIPE_UNREADABLE


@pytest.mark.parametrize("case", VECTORS["names"], ids=lambda case: case["fleet"])
def test_the_parameter_name_follows_the_template_and_the_ssm_rule(case: dict[str, Any]) -> None:
    assert recipe_parameter_name(case["fleet"]) == case["parameter"]


def test_a_recipe_from_a_project_keeps_names_and_never_secret_values() -> None:
    recipe = recipe_from_document(SMALL)
    project = recipe_project(recipe)

    rebuilt = recipe_of(
        project,
        name="my-agent",
        image_version="3",
        scaling=ScalingOptions(min_vms=1, max_vms=3, warm_vms=1),
        renew_gateway_keys=False,
        saved_by=SMALL["savedBy"],
    )

    assert recipe_document(rebuilt) == SMALL
    value = encode_recipe(recipe_document(rebuilt)) or ""
    assert "app/config" in value
    assert "Bearer" not in value


def test_a_project_the_reader_rejects_is_unreadable() -> None:
    document = copy.deepcopy(SMALL)
    document["project"]["schema"] = "rayito.agent-project/9"
    with pytest.raises(RecipeWarning) as caught:
        recipe_project(recipe_from_document(document))
    assert caught.value.code == RECIPE_UNREADABLE


def test_store_and_load_round_trip_through_the_port() -> None:
    store = MemoryRecipeStore()
    recipe = recipe_from_document(SMALL)

    assert store_recipe(store, recipe) == RECIPE_SAVED
    loaded = load_recipe(store, "my-agent")

    assert loaded is not None
    assert recipe_document(loaded[0]) == SMALL
    assert store.calls == [
        ("save", "/rayito/agents/my-agent/recipe"),
        ("load", "/rayito/agents/my-agent/recipe"),
    ]


def test_a_too_large_recipe_deletes_the_previous_one() -> None:
    store = MemoryRecipeStore(values={"/rayito/agents/my-agent/recipe": "{}"})
    recipe = recipe_from_document(document_of(CASES["too-large"]))

    assert store_recipe(store, recipe) == "recipe_too_large"
    assert store.operations == ["delete"]
    assert store.values == {}


def test_a_save_without_permission_is_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    store = MemoryRecipeStore(failures={"save": denied()})

    outcome = store_recipe(store, recipe_from_document(SMALL))

    assert outcome == "recipe_access_denied"
    assert "recipe_access_denied" in caplog.text
    assert "/rayito/agents" not in caplog.text


def test_a_fleet_name_outside_the_ssm_rule_is_not_saved() -> None:
    store = MemoryRecipeStore()
    recipe = recipe_from_document({**SMALL, "name": "bad name"})

    assert store_recipe(store, recipe) == "recipe_not_saved"
    assert store.calls == []


def test_an_unreadable_value_loads_as_no_recipe(caplog: pytest.LogCaptureFixture) -> None:
    store = MemoryRecipeStore(values={"/rayito/agents/my-agent/recipe": "not json"})

    assert load_recipe(store, "my-agent") is None
    assert "recipe_unreadable" in caplog.text
