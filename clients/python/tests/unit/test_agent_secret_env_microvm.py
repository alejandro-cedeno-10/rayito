"""`secret_env` en MicroVMs (ADR-033 D4) y lo que el despliegue hace con él:
el SDK lee cada secreto una vez con las credenciales de quien despliega y el
host lo recibe sólo en el entorno de su proceso (nunca en un fichero); sin la
función no se lee nada ni se crea ningún cliente. `--create-secrets-from` crea
o actualiza el secreto JSON de un `.env` y `--check-env` avisa de las variables
sin valor. Sobre dobles: nada toca AWS."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import boto3
import pytest

from rayito import SecretEnvRef, SecretEnvSource
from rayito._agent._deploy_facade import plan_deploy
from rayito._agent._deploy_plan import AgentDeployOptions
from rayito._agent._export import render_export
from rayito._agent._project_domain import AgentProjectException
from rayito._agent._project_service import ProjectService
from rayito._agent._project_service_async import AsyncProjectService
from rayito._agent._secret_env_deploy import (
    CREATED,
    UPDATED,
    EnvFileSecret,
    env_file_secret_name,
    env_missing_of,
    prepare_secret_env,
    publish_env_file_secret,
    read_env_file_secret,
)
from rayito.exceptions import AgentException, InvalidArgumentException
from tests.unit.fake_agent_project import project
from tests.unit.test_agent_project_service import Flavor

DATABASE_URL = "postgres://db.internal.example/app"
#: Una variable que el host de deepagents ya fija (no reservada por prefijo).
HOST_VARIABLE = "PYTHONUNBUFFERED"
SECRET_ENV = {
    "DATABASE_URL": "app/db-url",
    "TICKETS_TOKEN": SecretEnvRef("app/tickets", key="token", version_stage="AWSPREVIOUS"),
}
SECRETS = {
    ("app/db-url", None): DATABASE_URL,
    ("app/tickets", "AWSPREVIOUS"): json.dumps({"token": "tk-value"}),
    ("app/config", None): json.dumps({"MODE": "demo"}),
}


class _Reader:
    def __init__(self, values: dict[tuple[str, str | None], str] | None = None) -> None:
        self.values = SECRETS if values is None else values
        self.reads: list[tuple[str, str | None]] = []

    def __call__(self, secret: str, stage: str | None) -> str:
        self.reads.append((secret, stage))
        return self.values[(secret, stage)]


class SecretFlavor(Flavor):
    def __init__(self, name: str, reader: _Reader) -> None:
        super().__init__(name)
        self.reader = reader

    def service(self, the_project: Any) -> Any:
        if self.asynchronous:

            async def read(secret: str, stage: str | None) -> str:
                return self.reader(secret, stage)

            return AsyncProjectService(
                the_project,
                sandbox_class=self.klass,
                sleep=self._asleep,
                clock=self._clock,
                secret_reader=read,
            )
        return ProjectService(
            the_project,
            sandbox_class=self.klass,
            sleep=self._sleep,
            clock=self._clock,
            secret_reader=self.reader,
        )


@pytest.fixture(params=["sync", "async"])
def secret_flavor(request: pytest.FixtureRequest) -> Callable[..., SecretFlavor]:
    return lambda reader=None: SecretFlavor(request.param, reader or _Reader())


def secret_project() -> Any:
    return project(
        secret_env=SECRET_ENV,
        secret_env_from=(SecretEnvSource("app/config", prefix="APP_"),),
    )


def _serve_envs(flav: SecretFlavor) -> dict[str, str]:
    _, (_, options) = flav.sandbox.commands.calls
    return dict(options["envs"])


def test_the_host_process_receives_the_values_and_no_file_holds_them(
    secret_flavor: Callable[..., SecretFlavor],
) -> None:
    flav = secret_flavor()
    flav.call(flav.service(secret_project()), "deploy")
    envs = _serve_envs(flav)
    assert envs["DATABASE_URL"] == DATABASE_URL
    assert envs["TICKETS_TOKEN"] == "tk-value"
    assert envs["APP_MODE"] == "demo"
    assert "RAYITO_SECRET_ENV" not in envs
    assert envs["RAYITO_SECRET_ENV_NAMES"] == "APP_MODE,DATABASE_URL,TICKETS_TOKEN"
    assert flav.reader.reads == [
        ("app/db-url", None),
        ("app/tickets", "AWSPREVIOUS"),
        ("app/config", None),
    ]
    for entry in flav.sandbox.files.written:
        data = entry.data if isinstance(entry.data, bytes) else str(entry.data).encode()
        assert DATABASE_URL.encode() not in data
        assert b"tk-value" not in data
    ((_, created),) = flav.klass.created
    assert DATABASE_URL not in json.dumps(created, default=str)


def test_without_secret_env_nothing_is_read(secret_flavor: Callable[..., SecretFlavor]) -> None:
    flav = secret_flavor(_Reader({}))
    flav.call(flav.service(project()), "deploy")
    assert flav.reader.reads == []
    assert "DATABASE_URL" not in _serve_envs(flav)


def test_a_bad_secret_fails_before_the_host_starts_and_names_nothing(
    secret_flavor: Callable[..., SecretFlavor],
) -> None:
    flav = secret_flavor(_Reader({**SECRETS, ("app/config", None): "plain"}))
    with pytest.raises(AgentException) as raised:
        flav.call(flav.service(secret_project()), "deploy")
    assert (raised.value.reason, raised.value.detail_code) == (
        "runtime_error",
        "secret_env_invalid",
    )
    assert "app/config" not in str(raised.value)
    assert len(flav.sandbox.commands.calls) == 0
    assert flav.sandbox.kills == 1


def test_a_variable_the_host_already_sets_is_refused(
    secret_flavor: Callable[..., SecretFlavor],
) -> None:
    flav = secret_flavor()
    colliding = project(secret_env={HOST_VARIABLE: "app/x"})
    with pytest.raises(AgentProjectException) as raised:
        flav.call(flav.service(colliding), "deploy")
    assert (raised.value.code, raised.value.path) == (
        "invalid_value",
        f"secret_env.{HOST_VARIABLE}",
    )
    assert flav.reader.reads == []


def test_the_default_reader_is_not_built_without_secret_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[str] = []

    def spy(self: Any, service: str, *args: Any, **kwargs: Any) -> Any:
        created.append(service)
        raise AssertionError("ningún cliente sin secret_env")

    monkeypatch.setattr(boto3.session.Session, "client", spy)
    flav = Flavor("sync")
    flav.call(flav.service(project()), "deploy")
    asynchronous = Flavor("async")
    asyncio.run(asynchronous.service(project()).deploy())
    assert created == []


def test_the_export_is_identical_without_secret_env() -> None:
    plain = project()
    explicit = dataclasses.replace(plain, secret_env={}, secret_env_from=())
    for target in ("docker", "agentcore"):
        assert render_export(plain, target=target) == render_export(explicit, target=target)


# ---------------------------------------------------------- --create-secrets-from


class _Store:
    def __init__(self, existing: bool) -> None:
        self.existing = existing
        self.calls: list[tuple[str, str, str]] = []

    def exists(self, name: str) -> bool:
        return self.existing

    def create(self, name: str, value: str) -> None:
        self.calls.append(("create", name, value))

    def update(self, name: str, value: str) -> None:
        self.calls.append(("update", name, value))


def _env_file(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    path.write_text("TICKETS_API_URL=https://tickets.example.com\nTICKETS_TOKEN=tk-value\n")
    return path


def test_the_env_file_secret_holds_names_in_the_clear_and_hides_the_value(
    tmp_path: Path,
) -> None:
    secret = read_env_file_secret("support-bot", _env_file(tmp_path))
    assert secret.name == env_file_secret_name("support-bot") == "rayito/agents/support-bot/env"
    assert secret.variables == ("TICKETS_API_URL", "TICKETS_TOKEN")
    assert json.loads(secret.value)["TICKETS_TOKEN"] == "tk-value"
    assert "tk-value" not in repr(secret)


@pytest.mark.parametrize(
    ("existing", "outcome", "call"), [(False, CREATED, "create"), (True, UPDATED, "update")]
)
def test_publishing_creates_or_writes_a_new_version(
    tmp_path: Path, existing: bool, outcome: str, call: str
) -> None:
    store = _Store(existing)
    secret = read_env_file_secret("support-bot", _env_file(tmp_path))
    assert publish_env_file_secret(store, secret) == outcome
    assert store.calls == [(call, secret.name, secret.value)]


def test_preparing_wires_the_secret_as_an_allow_listed_source(tmp_path: Path) -> None:
    prepared = prepare_secret_env(project(), str(_env_file(tmp_path)), check_env=False)
    secret = prepared.secret
    assert isinstance(secret, EnvFileSecret)
    assert prepared.warnings == ()
    assert prepared.project.secret_env_from == (
        SecretEnvSource(secret.name, keys=("TICKETS_API_URL", "TICKETS_TOKEN")),
    )
    again = prepare_secret_env(prepared.project, str(_env_file(tmp_path)), check_env=False)
    assert again.project.secret_env_from == prepared.project.secret_env_from


@pytest.mark.parametrize("text", ["", "# sólo comentarios\n"])
def test_an_empty_env_file_is_refused(tmp_path: Path, text: str) -> None:
    path = tmp_path / ".env"
    path.write_text(text)
    with pytest.raises(InvalidArgumentException):
        read_env_file_secret("support-bot", path)
    with pytest.raises(InvalidArgumentException):
        read_env_file_secret("support-bot", tmp_path / "missing.env")


def test_a_reserved_name_in_the_env_file_is_named_without_its_value(tmp_path: Path) -> None:
    path = tmp_path / "local.env"
    path.write_text("TICKETS_TOKEN=tk-value\nAWS_PROFILE=dev-profile-value\n")
    with pytest.raises(InvalidArgumentException) as raised:
        read_env_file_secret("support-bot", path)
    assert "AWS_PROFILE" in str(raised.value)
    assert "dev-profile-value" not in str(raised.value)


def test_an_env_file_inside_the_source_is_excluded_from_it(tmp_path: Path) -> None:
    source = tmp_path / "agent"
    (source / "deploy").mkdir(parents=True)
    inside = source / "deploy" / "prod.env"
    outside = tmp_path / "values.env"
    for path in (inside, outside):
        path.write_text("TICKETS_TOKEN=tk-value\n")
    the_project = project(source=str(source), exclude=("tests",))
    prepared = prepare_secret_env(the_project, str(inside), check_env=False)
    assert prepared.project.exclude == ("tests", "deploy/prod.env")
    kept = prepare_secret_env(the_project, str(outside), check_env=False)
    assert kept.project.exclude == ("tests",)


# ------------------------------------------------------------------ --check-env


def _source_with_example(tmp_path: Path) -> Path:
    (tmp_path / ".env.example").write_text(
        "DATABASE_URL=\nTICKETS_API_URL=\nOPENAI_API_KEY=\nAWS_REGION=\nLOG_LEVEL=\n"
    )
    return tmp_path


def test_check_env_warns_only_for_names_that_receive_no_value(tmp_path: Path) -> None:
    source = _source_with_example(tmp_path)
    the_project = project(
        source=str(source), env={"LOG_LEVEL": "info"}, secret_env={"DATABASE_URL": "app/db"}
    )
    warnings = env_missing_of(the_project, check_env=False)
    assert len(warnings) == 1
    assert warnings[0].startswith("TICKETS_API_URL ")
    assert env_missing_of(project(source=str(source)), check_env=False) == ()
    assert len(env_missing_of(project(source=str(source)), check_env=True)) == 3


CHECK_ENV = AgentDeployOptions(check_env=True)


def test_plan_deploy_logs_the_missing_names(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    the_project = project(source=str(_source_with_example(tmp_path)))
    with caplog.at_level(logging.WARNING, logger="rayito.agent"):
        plan_deploy(
            project=the_project, options=AgentDeployOptions(check_env=True), region="us-east-1"
        )
    assert "TICKETS_API_URL" in caplog.text
    assert "LOG_LEVEL" in caplog.text


@pytest.mark.parametrize(
    "fields", [{"create_secrets_from": ""}, {"check_env": "yes"}], ids=["empty-path", "not-bool"]
)
def test_the_new_deploy_options_are_validated(fields: dict[str, Any]) -> None:
    with pytest.raises(InvalidArgumentException):
        AgentDeployOptions(**fields)
