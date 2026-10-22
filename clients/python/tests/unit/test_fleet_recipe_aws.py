"""`SsmRecipeStore` (ADR-034, `AWS_API_NOTES.md` «Receta de la flota y
claves de corta duración de Bedrock») con `Stubber` contra el modelo de
botocore: sólo las tres operaciones y los parámetros del contrato, y los
errores mapeados por código."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.config import Config
from botocore.stub import Stubber

from rayito._agent._fleet_recipe import RecipeWarning
from rayito._agent._fleet_recipe_aws import SsmRecipeStore
from rayito._limits import AGENT_RECIPE_DESCRIPTION
from rayito.exceptions import SandboxException

REGION = "us-east-1"
PARAMETER = "/rayito/agents/my-agent/recipe"
VALUE = '{"schema":"rayito.agent-recipe/1"}'
CONTRACT: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[4] / "testdata" / "fleet-recipe" / "contract.json").read_text(
        encoding="utf-8"
    )
)


@dataclass
class SpySession:
    """Sesión boto3 falsa: `client("ssm")` devuelve `api`; cualquier otro
    servicio es un error del test."""

    api: Any
    region_name: str | None = REGION
    built: list[str] = field(default_factory=list)

    def client(self, service: str, **kwargs: Any) -> Any:
        self.built.append(service)
        assert service == "ssm", service
        return self.api


def ssm_client() -> Any:
    session = boto3.session.Session(
        region_name=REGION, aws_access_key_id="testing", aws_secret_access_key="testing"
    )
    return session.client("ssm", config=Config(retries={"total_max_attempts": 1}))


def store_with(client: Any) -> tuple[SsmRecipeStore, SpySession]:
    session = SpySession(client)
    return SsmRecipeStore(region=REGION, session=session), session


def test_construction_builds_no_client() -> None:
    _, session = store_with(ssm_client())
    assert session.built == []


def test_save_sends_exactly_the_contract_parameters() -> None:
    client = ssm_client()
    store, _ = store_with(client)
    expected = {
        "Name": PARAMETER,
        "Value": VALUE,
        "Type": "SecureString",
        "Tier": "Standard",
        "Overwrite": True,
        "Description": AGENT_RECIPE_DESCRIPTION,
    }
    assert sorted(expected) == sorted(CONTRACT["awsOperations"]["ssm"]["PutParameter"])
    with Stubber(client) as stub:
        stub.add_response(
            "put_parameter", {"Version": 1, "Tier": "Standard"}, expected_params=expected
        )
        store.save(PARAMETER, VALUE)
        stub.assert_no_pending_responses()


def test_load_decrypts_and_returns_the_value() -> None:
    client = ssm_client()
    store, _ = store_with(client)
    expected = {"Name": PARAMETER, "WithDecryption": True}
    assert sorted(expected) == sorted(CONTRACT["awsOperations"]["ssm"]["GetParameter"])
    with Stubber(client) as stub:
        stub.add_response(
            "get_parameter",
            {"Parameter": {"Name": PARAMETER, "Type": "SecureString", "Value": VALUE}},
            expected_params=expected,
        )
        assert store.load(PARAMETER) == VALUE


def test_a_missing_parameter_is_no_recipe() -> None:
    client = ssm_client()
    store, _ = store_with(client)
    with Stubber(client) as stub:
        stub.add_client_error("get_parameter", service_error_code="ParameterNotFound")
        stub.add_client_error("delete_parameter", service_error_code="ParameterNotFound")
        assert store.load(PARAMETER) is None
        store.delete(PARAMETER)


def test_delete_sends_only_the_name() -> None:
    client = ssm_client()
    store, _ = store_with(client)
    assert CONTRACT["awsOperations"]["ssm"]["DeleteParameter"] == ["Name"]
    with Stubber(client) as stub:
        stub.add_response("delete_parameter", {}, expected_params={"Name": PARAMETER})
        store.delete(PARAMETER)
        stub.assert_no_pending_responses()


@pytest.mark.parametrize("operation", ["put_parameter", "get_parameter", "delete_parameter"])
def test_access_denied_is_the_access_warning(operation: str) -> None:
    client = ssm_client()
    store, _ = store_with(client)
    calls: dict[str, Callable[[], object]] = {
        "put_parameter": lambda: store.save(PARAMETER, VALUE),
        "get_parameter": lambda: store.load(PARAMETER),
        "delete_parameter": lambda: store.delete(PARAMETER),
    }
    with Stubber(client) as stub:
        stub.add_client_error(operation, service_error_code="AccessDeniedException")
        with pytest.raises(RecipeWarning) as caught:
            calls[operation]()
    assert caught.value.code == "recipe_access_denied"


@pytest.mark.parametrize(
    "code",
    [
        "ParameterLimitExceeded",
        "ParameterPatternMismatchException",
        "HierarchyLevelLimitExceededException",
        "TooManyUpdates",
        "ThrottlingException",
    ],
)
def test_a_save_that_cannot_happen_is_not_saved(code: str) -> None:
    client = ssm_client()
    store, _ = store_with(client)
    with Stubber(client) as stub:
        stub.add_client_error("put_parameter", service_error_code=code)
        with pytest.raises(RecipeWarning) as caught:
            store.save(PARAMETER, VALUE)
    assert caught.value.code == "recipe_not_saved"


def test_any_other_read_error_propagates_without_the_aws_message() -> None:
    client = ssm_client()
    store, _ = store_with(client)
    with Stubber(client) as stub:
        stub.add_client_error(
            "get_parameter",
            service_error_code="InternalServerError",
            service_message=f"fallo en {PARAMETER}",
        )
        with pytest.raises(SandboxException) as caught:
            store.load(PARAMETER)
    assert caught.value.aws_code == "InternalServerError"
    assert PARAMETER not in str(caught.value)
    assert "ssm:GetParameter" in str(caught.value)


def test_repr_names_nothing() -> None:
    store, _ = store_with(ssm_client())
    assert repr(store) == "SsmRecipeStore()"
