"""`AgentFleet.connect(nombre)` con la receta de la flota (ADR-034 D2, D3)
en sus dos superficies: sin `project` lee la receta (un `GetParameter`) y
crea VMs iguales a los del despliegue; con `project=` o `use_recipe=False`
no toca Parameter Store (la traza de 0.16); `status()` dice de dónde sale
el escalado y `stop()` borra sólo la receta que leyó o guardó. Sobre dobles:
ninguna prueba llama a AWS."""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path
from typing import Any

import pytest

import rayito.sandbox_async
import rayito.sandbox_sync
from rayito._agent._deploy_facade import plan_deploy
from rayito._agent._deploy_plan import AgentDeployOptions
from rayito._agent._fleet import AgentFleet
from rayito._agent._fleet_async import AsyncAgentFleet
from rayito._agent._fleet_recipe import (
    FleetRecipe,
    decode_recipe,
    encode_recipe,
    recipe_document,
    recipe_of,
    renewal_on_connect,
)
from rayito._agent._project_domain import AgentProject
from rayito._agent._scaling_domain import ScalingOptions
from rayito.exceptions import (
    AgentCoreException,
    InvalidArgumentException,
    SandboxException,
    SecretException,
)
from tests.unit.fake_agent_fleet import FLEET_TOKEN, FleetFlavor
from tests.unit.fake_agent_project import MODEL_ID
from tests.unit.fake_fleet_recipe import MemoryRecipeStore, denied
from tests.unit.test_agent_deploy import Harness

NAME = "support-bot"
PARAMETER = f"/rayito/agents/{NAME}/recipe"
RECIPE_VECTORS: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[4] / "testdata" / "fleet-recipe" / "recipes.json").read_text(
        encoding="utf-8"
    )
)
RENEWAL_VECTORS: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[4] / "testdata" / "fleet-recipe" / "renewal.json").read_text(
        encoding="utf-8"
    )
)
RECIPE_SCALING = ScalingOptions(min_vms=2, max_vms=5, warm_vms=0, scale_in_after_seconds=300)


def recipe_project() -> AgentProject:
    """El proyecto de la receta, leído de un documento con un preset
    `bedrock` (así la receta guarda sus presets)."""
    return AgentProject.from_dict(
        {
            "schema": "rayito.agent-project/1",
            "name": NAME,
            "runtime": {"kind": "deepagents"},
            "model": {
                "provider": "bedrock",
                "id": MODEL_ID,
                "gateway": "bedrock",
                "region": "us-east-1",
            },
            "gateways": {
                "bedrock": {
                    "preset": "bedrock",
                    "secret": "bedrock-key",
                    "region": "us-east-1",
                    "models": [MODEL_ID],
                }
            },
            "env": {"LOG_LEVEL": "debug"},
            "network": {"allow_out": ["api.example.com"]},
        },
        base_dir=".",
    )


def saved_recipe(**changes: Any) -> FleetRecipe:
    options = {
        "name": NAME,
        "image_version": "7",
        "scaling": RECIPE_SCALING,
        "renew_gateway_keys": False,
        **changes,
    }
    return recipe_of(recipe_project(), **options)


def store_with(recipe: FleetRecipe | None = None) -> MemoryRecipeStore:
    store = MemoryRecipeStore()
    if recipe is not None:
        value = encode_recipe(recipe_document(recipe))
        assert value is not None
        store.values[PARAMETER] = value
    return store


@pytest.fixture(params=["sync", "async"])
def flavor(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> FleetFlavor:
    """Las dos superficies, con la clase `Sandbox` por defecto del servicio
    que la flota crea sustituida por la de doble."""
    flav = FleetFlavor(request.param)
    monkeypatch.setattr(rayito.sandbox_sync, "Sandbox", flav.classes)
    monkeypatch.setattr(rayito.sandbox_async, "AsyncSandbox", flav.classes)
    return flav


def connect(flavor: FleetFlavor, **options: Any) -> Any:
    fleet_class: Any = AsyncAgentFleet if flavor.asynchronous else AgentFleet
    factory = flavor.backend.async_factory if flavor.asynchronous else flavor.backend.factory
    return flavor.call(
        fleet_class.connect(
            NAME,
            access_token=FLEET_TOKEN,
            directory=flavor.classes_directory(),
            transport_factory=factory,
            clock=flavor.clock,
            sleep=flavor.clock.asleep if flavor.asynchronous else flavor.clock.sleep,
            **options,
        )
    )


def test_connect_without_project_creates_vms_from_the_recipe(flavor: FleetFlavor) -> None:
    store = store_with(saved_recipe())
    fleet = connect(flavor, recipe_store=store)

    result = flavor.call(fleet.invoke("hola"))

    assert result.text == "respuesta a hola"
    assert store.calls == [("load", PARAMETER)]
    template, arguments = flavor.classes.created[0]
    assert template == NAME
    assert arguments["template_version"] == "7"
    assert arguments["envs"]["LOG_LEVEL"] == "debug"
    assert arguments["network"].allow_out == ("api.example.com",)
    assert set(arguments["gateways"]) == {"bedrock"}


def test_status_reports_the_scaling_of_the_recipe(flavor: FleetFlavor) -> None:
    fleet = connect(flavor, recipe_store=store_with(saved_recipe()))

    status = flavor.call(fleet.status())

    assert (status.min_vms, status.max_vms) == (2, 5)
    assert status.scaling_source == "recipe"
    assert fleet.scaling.scale_in_after_seconds == 300


def test_an_explicit_scaling_wins_over_the_recipe(flavor: FleetFlavor) -> None:
    explicit = ScalingOptions(min_vms=1, max_vms=2)
    fleet = connect(flavor, recipe_store=store_with(saved_recipe()), scaling=explicit)

    status = flavor.call(fleet.status())

    assert (status.min_vms, status.max_vms, status.scaling_source) == (1, 2, "explicit")


def test_an_explicit_project_never_reads_parameter_store(flavor: FleetFlavor) -> None:
    store = store_with(saved_recipe())
    fleet = connect(flavor, recipe_store=store, project=recipe_project())

    status = flavor.call(fleet.status())
    flavor.call(fleet.stop())

    assert store.calls == []
    assert status.scaling_source == "project"


def test_use_recipe_false_is_the_0_16_fleet(flavor: FleetFlavor) -> None:
    store = store_with(saved_recipe())
    fleet = connect(flavor, recipe_store=store, use_recipe=False)

    status = flavor.call(fleet.status())

    assert store.calls == []
    assert status.scaling_source == "default"
    with pytest.raises(InvalidArgumentException):
        flavor.call(fleet.invoke("hola"))


def test_without_a_recipe_the_fleet_is_the_0_16_one(flavor: FleetFlavor) -> None:
    store = store_with()
    fleet = connect(flavor, recipe_store=store)

    status = flavor.call(fleet.status())
    flavor.call(fleet.stop())

    assert status.scaling_source == "default"
    assert store.operations == ["load"]


def test_a_recipe_without_permission_warns_and_is_the_0_16_fleet(
    flavor: FleetFlavor, caplog: pytest.LogCaptureFixture
) -> None:
    store = store_with(saved_recipe())
    store.failures["load"] = denied()

    fleet = connect(flavor, recipe_store=store)

    assert flavor.call(fleet.status()).scaling_source == "default"
    assert caplog.text.count("recipe_access_denied") == 1
    assert PARAMETER not in caplog.text


@pytest.mark.parametrize("case", RECIPE_VECTORS["loadFailures"], ids=lambda case: case["id"])
def test_a_parameter_store_failure_is_a_warning_and_the_0_16_fleet(
    flavor: FleetFlavor, case: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    store = store_with(saved_recipe())
    store.failures["load"] = SandboxException("Parameter Store falló", aws_code=case["awsCode"])
    flavor.classes.add()

    fleet = connect(flavor, recipe_store=store)

    assert flavor.call(fleet.status()).scaling_source == "default"
    assert caplog.text.count(case["warning"]) == 1
    flavor.call(fleet.stop())
    assert not flavor.classes.live


def test_stop_deletes_the_recipe_it_read(flavor: FleetFlavor) -> None:
    store = store_with(saved_recipe())
    fleet = connect(flavor, recipe_store=store)

    flavor.call(fleet.stop())

    assert store.operations == ["load", "delete"]
    assert store.values == {}


def test_stop_keeps_the_recipe_on_request(flavor: FleetFlavor) -> None:
    store = store_with(saved_recipe())
    fleet = connect(flavor, recipe_store=store)

    flavor.call(fleet.stop(keep_recipe=True))

    assert store.operations == ["load"]
    assert PARAMETER in store.values


def test_a_stop_without_permission_to_delete_only_warns(
    flavor: FleetFlavor, caplog: pytest.LogCaptureFixture
) -> None:
    store = store_with(saved_recipe())
    store.failures["delete"] = denied()
    fleet = connect(flavor, recipe_store=store)

    flavor.call(fleet.stop())

    assert "recipe_access_denied" in caplog.text


def test_the_renewal_option_of_the_recipe_needs_its_extra_only_when_on(
    flavor: FleetFlavor,
) -> None:
    store = store_with(saved_recipe(renew_gateway_keys=True))

    fleet = connect(flavor, recipe_store=store, renew_gateway_keys=False)

    assert not fleet._key_renewal.enabled


@pytest.mark.parametrize("case", RENEWAL_VECTORS["connect"], ids=lambda case: case["id"])
def test_only_the_caller_turns_renewal_on(case: dict[str, Any]) -> None:
    recipe = None if case["recipe"] is None else saved_recipe(renew_gateway_keys=case["recipe"])

    assert renewal_on_connect(recipe, case["requested"]) == (case["renew"], case["warning"])


def test_a_renewing_recipe_never_renews_by_itself(
    flavor: FleetFlavor, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setitem(sys.modules, "aws_bedrock_token_generator", None)
    keys = DeniedRenewer()
    store = store_with(saved_recipe(renew_gateway_keys=True))

    fleet = connect(flavor, recipe_store=store, key_renewer=keys)
    flavor.call(fleet.status())
    result = flavor.call(fleet.invoke("hola"))
    flavor.call(fleet.scale(min_vms=2, max_vms=2))
    flavor.call(fleet.stop())

    assert result.text == "respuesta a hola"
    assert not fleet._key_renewal.enabled
    assert keys.calls == 0
    assert caplog.text.count("recipe_renewal_ignored") == 1
    assert PARAMETER not in caplog.text
    assert not flavor.classes.live


def test_an_explicit_renewal_without_permission_fails(flavor: FleetFlavor) -> None:
    fleet = connect(
        flavor,
        recipe_store=store_with(saved_recipe(renew_gateway_keys=True)),
        renew_gateway_keys=True,
        key_renewer=DeniedRenewer(),
    )

    with pytest.raises(SecretException, match="sin permiso"):
        flavor.call(fleet.invoke("hola"))


class DeniedRenewer:
    """Un renovador sin `secretsmanager:PutSecretValue`."""

    def __init__(self) -> None:
        self.calls = 0

    def renew(self) -> bool:
        self.calls += 1
        raise SecretException("sin permiso", code="permission_denied")


# ------------------------------------------------------------------ deploy


@pytest.fixture(params=["sync", "async"])
def harness(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(request.param, monkeypatch)


def deploy_saving(harness: Harness, store: MemoryRecipeStore, **options: Any) -> Any:
    original = harness.ports

    def ports(the_project: AgentProject) -> Any:
        built = original(the_project)
        return dataclasses.replace(
            built, fleet_kwargs={**built.fleet_kwargs, "recipe_store": store}
        )

    harness.ports = ports  # type: ignore[method-assign]
    return harness.deploy(recipe_project(), options=AgentDeployOptions(**options))


def test_a_deploy_saves_the_recipe_once_after_the_fleet_starts(harness: Harness) -> None:
    store = MemoryRecipeStore()

    deploy_saving(harness, store, scaling=ScalingOptions(min_vms=1, max_vms=3))

    assert store.calls == [("save", PARAMETER)]
    recipe = decode_recipe(store.values[PARAMETER])
    assert recipe.image_version == "3.0"
    assert recipe.project["env"] == {"LOG_LEVEL": "debug"}
    assert recipe.scaling.max_vms == 3
    assert recipe.gateway_presets == {"bedrock": "bedrock"}
    assert [step.kind for step in harness.steps] == ["bucket", "image", "fleet"]


def test_save_recipe_false_makes_no_ssm_call(harness: Harness) -> None:
    store = MemoryRecipeStore()

    deploy_saving(harness, store, save_recipe=False)

    assert store.calls == []


def test_a_recipe_that_cannot_be_saved_never_fails_the_deploy(harness: Harness) -> None:
    store = MemoryRecipeStore(failures={"save": denied()})

    fleet = deploy_saving(harness, store)

    assert fleet.sandbox_ids
    assert (harness.steps[-1].kind, harness.steps[-1].detail) == (
        "recipe",
        "recipe_access_denied",
    )


def test_scale_in_reaches_the_fleet_and_the_recipe(harness: Harness) -> None:
    store = MemoryRecipeStore()

    fleet = deploy_saving(harness, store, scale_in_after_seconds=300)

    assert fleet.scaling.scale_in_after_seconds == 300
    assert decode_recipe(store.values[PARAMETER]).scaling.scale_in_after_seconds == 300


def test_a_scale_in_below_the_minimum_fails_before_anything() -> None:
    with pytest.raises(InvalidArgumentException):
        AgentDeployOptions(scale_in_after_seconds=10)


def test_starting_a_fleet_directly_saves_nothing(flavor: FleetFlavor) -> None:
    store = MemoryRecipeStore()
    fleet = flavor.fleet(None, the_project=recipe_project(), recipe_store=store)

    flavor.call(fleet.start())
    flavor.call(fleet.stop())

    assert store.calls == []


def test_the_plan_shows_the_recipe_step_without_calling_ssm() -> None:
    assert plan_deploy(project=recipe_project()).recipe == "save"
    off = plan_deploy(project=recipe_project(), options=AgentDeployOptions(save_recipe=False))
    assert off.recipe == "off"


@pytest.mark.parametrize(
    ("options", "option"),
    [
        ({"save_recipe": False}, "save_recipe"),
        ({"renew_gateway_keys": True}, "renew_gateway_keys"),
        ({"scale_in_after_seconds": 300}, "scale_in_after_seconds"),
    ],
)
def test_the_recipe_options_do_not_apply_to_agentcore(options: dict[str, Any], option: str) -> None:
    with pytest.raises(AgentCoreException) as caught:
        plan_deploy(
            project=recipe_project(), options=AgentDeployOptions(**options), target="agentcore"
        )
    assert caught.value.code == "option_not_supported"
    assert option in str(caught.value)
