"""Las banderas de la receta de la flota en `rayito agent deploy | status |
stop` (ADR-034, `testdata/fleet-recipe/contract.json` `cliFlags`): llegan
al SDK, `--env` rechaza lo que parece un secreto
(`testdata/fleet-recipe/env-rejection.json`), `status` no inventa un escalado
y `stop --keep-recipe` conserva la receta. Con dobles: nada toca AWS."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import pytest
from typer.testing import CliRunner

from rayito._agent._fleet_base import FleetMemberStatus, FleetStatus
from rayito._payload import generate_access_token
from rayito.cli import agent as agent_cli
from rayito.cli._agent_deploy import (
    ENV_FLAG_AUTH_COMPANIONS,
    ENV_FLAG_AUTH_SEGMENTS,
    ENV_FLAG_SECRET_SEGMENTS,
    ENV_LOOKS_SECRET_MESSAGE,
    INVOKE_RENEW_GATEWAY_KEYS_HELP,
    KEEP_RECIPE_HELP,
    RENEW_GATEWAY_KEYS_HELP,
    RENEW_NEEDS_FLEET_MESSAGE,
    SAVE_RECIPE_HELP,
    SCALE_IN_AFTER_HELP,
    STATUS_UNKNOWN_SCALING,
    env_flag_looks_secret,
)
from rayito.cli._console import EXIT_USAGE
from rayito.cli._session import Clients
from rayito.cli.app import app
from tests.unit.fake_agent_project import project as real_project

TESTDATA: Final = Path(__file__).resolve().parents[5] / "testdata" / "fleet-recipe"
CONTRACT: Final[dict[str, Any]] = json.loads(
    (TESTDATA / "contract.json").read_text(encoding="utf-8")
)
ENV_VECTORS: Final[dict[str, Any]] = json.loads(
    (TESTDATA / "env-rejection.json").read_text(encoding="utf-8")
)
SANDBOX_ID: Final = "microvm-00000000-0000-0000-0000-000000000001"
ACCESS_TOKEN: Final = generate_access_token()


@dataclass
class FakeDeployment:
    sandbox_id: str = SANDBOX_ID
    url: str = "https://abc.lambda-microvm.us-east-1.on.aws"


@dataclass
class FakeFleet:
    name: str = "support-bot"
    deployments: tuple[FakeDeployment, ...] = field(default_factory=lambda: (FakeDeployment(),))
    stops: list[dict[str, Any]] = field(default_factory=list)
    scaling_source: str = "default"
    terminated: int = 1

    @property
    def sandbox_ids(self) -> tuple[str, ...]:
        return tuple(deployment.sandbox_id for deployment in self.deployments)

    def detach(self) -> None:
        return None

    def stop(self, **kwargs: Any) -> int:
        self.stops.append(kwargs)
        return self.terminated

    def status(self) -> FleetStatus:
        return FleetStatus(
            name=self.name,
            members=(FleetMemberStatus(SANDBOX_ID, "unknown", False, 0),),
            min_vms=1,
            max_vms=1,
            warm_vms=0,
            warm_ready=0,
            scaling_source=self.scaling_source,  # type: ignore[arg-type]
        )


@pytest.fixture
def deployed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[dict[str, Any]]:
    """`deploy_agent` de doble sobre el proyecto de ejemplo; devuelve los
    argumentos con los que se llamó."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RAYITO_ACCESS_TOKEN", ACCESS_TOKEN)
    calls: list[dict[str, Any]] = []

    def detect(path: Any, found: Any, overrides: Any, region: Any = None) -> Any:
        return real_project(), None

    def deploy(**kwargs: Any) -> FakeFleet:
        calls.append(kwargs)
        options = kwargs["options"]
        if options.on_step is not None:
            from rayito._agent._deploy_plan import AgentDeployStep

            options.on_step(AgentDeployStep("recipe", "recipe_access_denied"))
        return FakeFleet()

    monkeypatch.setattr(agent_cli, "project_for", detect)
    monkeypatch.setattr(agent_cli, "deploy_agent", deploy)
    return calls


def connected_fleet(monkeypatch: pytest.MonkeyPatch, fleet: FakeFleet) -> list[dict[str, Any]]:
    connected: list[dict[str, Any]] = []

    def connect(name: str, **kwargs: Any) -> FakeFleet:
        connected.append({"name": name, **kwargs})
        return fleet

    monkeypatch.setattr(
        agent_cli, "AgentFleet", type("FakeAgentFleet", (), {"connect": staticmethod(connect)})
    )
    return connected


def test_the_rendered_help_keeps_the_square_brackets() -> None:
    """Typer pinta la ayuda con el marcado de rich, que se comía `[scaling]`
    y `[#clave]` como si fueran estilos."""
    wide = {"TERMINAL_WIDTH": "400", "COLUMNS": "400"}
    result = CliRunner().invoke(app, ["agent", "deploy", "--help"], env=wide)
    assert result.exit_code == 0
    assert "el de [scaling] del proyecto" in result.output
    assert "NOMBRE=<secreto>[#clave]" in result.output


def test_the_help_texts_are_the_contract_ones() -> None:
    helps = {(flag["command"], flag["flag"]): flag["help"] for flag in CONTRACT["cliFlags"]}
    assert helps[("agent deploy", "--scale-in-after")] == SCALE_IN_AFTER_HELP
    assert helps[("agent deploy", "--no-save-recipe")] == SAVE_RECIPE_HELP
    assert helps[("agent deploy", "--renew-gateway-keys")] == RENEW_GATEWAY_KEYS_HELP
    assert helps[("agent invoke", "--renew-gateway-keys")] == INVOKE_RENEW_GATEWAY_KEYS_HELP
    assert helps[("agent stop", "--keep-recipe")] == KEEP_RECIPE_HELP
    assert CONTRACT["messages"]["envLooksSecret"] == ENV_LOOKS_SECRET_MESSAGE
    assert CONTRACT["messages"]["statusUnknownScaling"] == STATUS_UNKNOWN_SCALING
    assert CONTRACT["messages"]["renewNeedsFleet"] == RENEW_NEEDS_FLEET_MESSAGE


def test_the_secret_segments_are_the_shared_ones() -> None:
    assert list(ENV_FLAG_SECRET_SEGMENTS) == ENV_VECTORS["segments"]
    assert list(ENV_FLAG_AUTH_SEGMENTS) == ENV_VECTORS["authSegments"]
    assert list(ENV_FLAG_AUTH_COMPANIONS) == ENV_VECTORS["authCompanions"]


@pytest.mark.parametrize("name", ENV_VECTORS["rejected"])
def test_a_secret_looking_env_name_is_rejected(name: str) -> None:
    assert env_flag_looks_secret(name)


@pytest.mark.parametrize("name", ENV_VECTORS["accepted"])
def test_an_ordinary_env_name_is_accepted(name: str) -> None:
    assert not env_flag_looks_secret(name)


def test_deploy_rejects_a_secret_env_before_any_call(
    deployed: list[dict[str, Any]], runner: CliRunner, clients: Clients
) -> None:
    result = runner.invoke(app, ["agent", "deploy", "--env", "API_TOKEN=x"], obj=clients)

    assert result.exit_code == EXIT_USAGE
    assert ENV_LOOKS_SECRET_MESSAGE.format(name="API_TOKEN") in result.output
    assert "--secret-env" in result.output
    assert deployed == []


def test_the_recipe_flags_reach_the_deploy_options(
    deployed: list[dict[str, Any]], runner: CliRunner, clients: Clients
) -> None:
    result = runner.invoke(
        app,
        [
            "agent",
            "deploy",
            "--scale-in-after",
            "300",
            "--no-save-recipe",
            "--renew-gateway-keys",
            "--env",
            "LOG_LEVEL=debug",
        ],
        obj=clients,
    )

    assert result.exit_code == 0, result.output
    options = deployed[0]["options"]
    assert options.scaling.scale_in_after_seconds == 300
    assert options.save_recipe is False
    assert options.renew_gateway_keys is True
    assert deployed[0]["project"].env["LOG_LEVEL"] == "debug"


def test_by_default_the_recipe_is_saved_and_keys_are_not_renewed(
    deployed: list[dict[str, Any]], runner: CliRunner, clients: Clients
) -> None:
    result = runner.invoke(app, ["--json", "agent", "deploy"], obj=clients)

    assert result.exit_code == 0, result.output
    options = deployed[0]["options"]
    assert options.save_recipe is True
    assert options.renew_gateway_keys is False
    assert json.loads(result.stdout)["warnings"] == ["recipe_access_denied"]


@pytest.mark.parametrize(
    "flags",
    [["--scale-in-after", "300"], ["--no-save-recipe"], ["--renew-gateway-keys"]],
)
def test_the_recipe_flags_are_microvm_only(
    flags: list[str], runner: CliRunner, clients: Clients
) -> None:
    result = runner.invoke(app, ["agent", "deploy", "--target", "agentcore", *flags], obj=clients)

    assert result.exit_code == EXIT_USAGE
    assert flags[0] in result.output
    assert "agentcore" in result.output


def test_dry_run_lists_the_recipe_step(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner, clients: Clients, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(agent_cli, "project_for", lambda *args, **kwargs: (real_project(), None))

    saved = runner.invoke(app, ["agent", "deploy", "--dry-run"], obj=clients)
    off = runner.invoke(app, ["agent", "deploy", "--dry-run", "--no-save-recipe"], obj=clients)

    assert "recipe: save" in saved.output
    assert "recipe: off" in off.output


def test_status_without_project_or_recipe_says_the_scaling_is_unknown(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner, clients: Clients
) -> None:
    monkeypatch.setenv("RAYITO_ACCESS_TOKEN", ACCESS_TOKEN)
    connected_fleet(monkeypatch, FakeFleet())

    text = runner.invoke(app, ["agent", "status", "support-bot"], obj=clients)
    as_json = runner.invoke(app, ["--json", "agent", "status", "support-bot"], obj=clients)

    assert "members: 1 (escalado desconocido: sin proyecto ni receta)" in text.output
    assert "min 1, max 1" not in text.output
    assert json.loads(as_json.output)["scalingSource"] == "default"


def test_status_from_the_recipe_prints_its_scaling(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner, clients: Clients
) -> None:
    monkeypatch.setenv("RAYITO_ACCESS_TOKEN", ACCESS_TOKEN)
    connected_fleet(monkeypatch, FakeFleet(scaling_source="recipe"))

    text = runner.invoke(app, ["agent", "status", "support-bot"], obj=clients)

    assert "members: 1 (min 1, max 1)" in text.output


@pytest.mark.parametrize(
    ("flags", "expected"), [([], {}), (["--keep-recipe"], {"keep_recipe": True})]
)
def test_stop_keeps_the_recipe_only_on_request(
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
    clients: Clients,
    flags: list[str],
    expected: dict[str, Any],
) -> None:
    fleet = FakeFleet()
    connected_fleet(monkeypatch, fleet)

    result = runner.invoke(app, ["agent", "stop", "support-bot", *flags], obj=clients)

    assert result.exit_code == 0, result.output
    assert fleet.stops == [expected]


def test_stop_reports_only_the_vms_it_terminated(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner, clients: Clients
) -> None:
    """Un VM que la flota no pudo probar que sea suyo no se termina y no
    cuenta como parado."""
    fleet = FakeFleet(terminated=0)
    connected_fleet(monkeypatch, fleet)

    result = runner.invoke(app, ["--json", "agent", "stop", "support-bot"], obj=clients)

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"name": "support-bot", "stopped": 0}


def test_a_fleet_without_members_prints_without_a_first_vm(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--min-vms 0` no deja ningún VM con servicio: el resultado no tiene
    primer VM (antes, `IndexError` tras desplegar)."""
    empty = FakeFleet(deployments=())
    agent_cli.print_deployed(empty, 8080, None, as_json=True)  # type: ignore[arg-type]
    document = json.loads(capsys.readouterr().out)
    assert (document["sandboxId"], document["url"], document["sandboxIds"]) == (None, None, [])
    agent_cli.print_deployed(empty, 8080, None, as_json=False)  # type: ignore[arg-type]
    text = capsys.readouterr().out
    assert "sandbox_id=" not in text and "url=" not in text
    assert "members=0" in text


@pytest.mark.parametrize(
    "command", [["agent", "stop", "support-bot"], ["agent", "status", "support-bot"]]
)
def test_stop_and_status_never_ask_for_the_renewal_warning(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner, clients: Clients, command: list[str]
) -> None:
    """`stop` y `status` nunca crean VMs: conectan con
    `renew_gateway_keys=False`, así una receta que pedía renovar no avisa
    `recipe_renewal_ignored` (la misma regla que la CLI de TypeScript)."""
    monkeypatch.setenv("RAYITO_ACCESS_TOKEN", "token-de-prueba-0123456789abcdefghijklmn")
    connected = connected_fleet(monkeypatch, FakeFleet())

    result = runner.invoke(app, command, obj=clients)

    assert result.exit_code == 0, result.output
    assert connected[0]["renew_gateway_keys"] is False
