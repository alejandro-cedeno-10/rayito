"""`rayito agent deploy` con `--secret-env`, `--secret-env-from`,
`--create-secrets-from` y `--check-env` (ADR-033 D4) sobre dobles: el plan de
AgentCore lleva `RAYITO_SECRET_ENV` y `secretEnvNames`, una bandera mal
formada es un error de uso que no repite el valor, y crear el secreto de un
`.env` enseña sólo nombres, pide confirmación y lo crea antes de aplicar.
Nada toca AWS."""

from __future__ import annotations

import json
from pathlib import Path

import boto3
import pytest
from typer.testing import CliRunner, Result

from rayito._agentcore._deploy import AgentCorePorts
from rayito._limits import AGENT_SECRET_ENV_VARIABLE
from rayito.cli._agent_secret_env import CREATE_SECRETS_NEEDS_CONFIRMATION
from rayito.cli._session import Clients
from rayito.cli.app import app
from tests.unit import fake_agentcore as fake
from tests.unit.cli.test_agentcore_cli import World, world
from tests.unit.fake_agent_project import project

EXIT_USAGE = 2
TOKEN_VALUE = "tk-value"


@pytest.fixture(autouse=True)
def known_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAYITO_BUCKET", fake.BUCKET)
    monkeypatch.setenv("AWS_REGION", fake.REGION)


class _Store:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def exists(self, name: str) -> bool:
        return False

    def create(self, name: str, value: str) -> None:
        self.calls.append(("create", name))

    def update(self, name: str, value: str) -> None:
        self.calls.append(("update", name))


def _with_store(w: World, store: _Store) -> World:
    ports = w.clients.agentcore_override
    assert ports is not None
    clients = Clients(
        session=w.clients.session,
        region=w.clients.region,
        agentcore_override=AgentCorePorts(
            gateways=ports.gateways, invoker=ports.invoker, secret_env_store=store
        ),
    )
    return World(clients, w.cloud, w.invoker, w.directory)


def _run(w: World, *args: str) -> Result:
    return CliRunner().invoke(app, list(args), obj=w.clients)


def _env_file(directory: Path) -> Path:
    path = directory.parent / f"{directory.name}.env"
    path.write_text(f"TICKETS_API_URL=https://tickets.example.com\nTICKETS_TOKEN={TOKEN_VALUE}\n")
    return path


def test_the_agentcore_dry_run_carries_the_spec_and_the_names(tmp_path: Path) -> None:
    w = world(tmp_path)
    result = _run(
        w,
        "--json",
        "agent",
        "deploy",
        str(w.directory),
        "--target",
        "agentcore",
        "--dry-run",
        "--secret-env",
        "DATABASE_URL=app/db-url",
        "--secret-env",
        "LLM_GATEWAY_KEY=app/llm#api_key",
        "--secret-env-from",
        "app/config,prefix=APP_,keys=DB_HOST|DB_USER",
    )
    assert result.exit_code == 0, result.stderr
    plan = json.loads(result.stdout)["plan"]
    assert plan["secretEnvNames"] == [
        "APP_DB_HOST",
        "APP_DB_USER",
        "DATABASE_URL",
        "LLM_GATEWAY_KEY",
    ]
    assert json.loads(plan["environment"][AGENT_SECRET_ENV_VARIABLE])["v"] == 1
    assert plan["readableSecrets"] == ["app/db-url", "app/llm", "app/config"]
    assert "secretEnv" in plan["security"]
    assert w.cloud.recorder.calls == []


def test_a_bad_secret_env_flag_is_a_usage_error_that_hides_the_value(tmp_path: Path) -> None:
    w = world(tmp_path)
    for target in ("microvm", "agentcore"):
        result = _run(
            w,
            "agent",
            "deploy",
            str(w.directory),
            "--target",
            target,
            "--dry-run",
            "--secret-env",
            "LLM_GATEWAY_KEY=sk-not-a-name",
        )
        assert result.exit_code == EXIT_USAGE, result.stdout
        assert "secret_env.LLM_GATEWAY_KEY" in result.stderr
        assert "sk-not-a-name" not in result.stderr + result.stdout


def test_the_microvm_dry_run_applies_the_flags_without_confirmation(tmp_path: Path) -> None:
    w = world(tmp_path)
    result = _run(
        w,
        "agent",
        "deploy",
        str(w.directory),
        "--dry-run",
        "--secret-env",
        "DATABASE_URL=app/db-url",
        "--create-secrets-from",
        str(_env_file(w.directory)),
        "--check-env",
    )
    assert result.exit_code == 0, result.stderr
    assert TOKEN_VALUE not in result.stdout + result.stderr


def test_creating_a_secret_from_a_microvm_deploy_needs_confirmation(tmp_path: Path) -> None:
    w = world(tmp_path)
    result = _run(
        w, "agent", "deploy", str(w.directory), "--create-secrets-from", str(_env_file(w.directory))
    )
    assert result.exit_code == EXIT_USAGE
    assert CREATE_SECRETS_NEEDS_CONFIRMATION in result.stderr
    assert "TICKETS_API_URL, TICKETS_TOKEN" in result.stderr
    assert TOKEN_VALUE not in result.stdout + result.stderr


def test_an_agentcore_deploy_creates_the_secret_before_applying(tmp_path: Path) -> None:
    store = _Store()
    w = _with_store(world(tmp_path), store)
    result = _run(
        w,
        "agent",
        "deploy",
        str(w.directory),
        "--target",
        "agentcore",
        "--yes",
        "--create-secrets-from",
        str(_env_file(w.directory)),
    )
    assert result.exit_code == 0, result.stderr
    assert store.calls == [("create", "rayito/agents/support-bot/env")]
    assert "TICKETS_API_URL, TICKETS_TOKEN" in result.stderr
    parameters = w.cloud.stack.parameters_seen[0]
    assert "secret:rayito/agents/support-bot/env-??????" in parameters["ReadableSecretArns"]
    assert TOKEN_VALUE not in result.stdout + result.stderr


def test_without_the_new_flags_the_plan_is_the_same(tmp_path: Path) -> None:
    w = world(tmp_path)
    (w.directory / "rayito-agent.json").write_text(json.dumps(project().to_dict()))
    args = ["--json", "agent", "deploy", str(w.directory), "--target", "agentcore", "--dry-run"]
    plan = json.loads(_run(w, *args).stdout)["plan"]
    assert "secretEnvNames" not in plan
    assert AGENT_SECRET_ENV_VARIABLE not in plan["environment"]
    assert isinstance(w.clients.session, boto3.session.Session)
    assert w.clients.region == fake.REGION
