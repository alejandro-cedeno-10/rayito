"""`secret_env` en AgentCore (ADR-033 D4) sobre dobles de los puertos: el
plan (`RAYITO_SECRET_ENV`, los secretos unidos a los legibles, la línea de
seguridad, `secretEnvNames` sólo con la función), el export (la herramienta y
la clave de `host.json` sólo con la función; con Docker, un aviso), el mínimo
privilegio del rol con los parámetros que manda el apply (un test que falla si
se amplía), y que ningún valor de un secreto aparece en el plan, el export ni
los parámetros. Nada toca AWS."""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import logging
import sys
from pathlib import Path
from typing import Any

import pytest

from rayito import (
    AgentCoreOptions,
    AgentDeployOptions,
    AgentDeployStep,
    SecretEnvRef,
    SecretEnvSource,
)
from rayito._agent._export import (
    DOCKER_SECRET_ENV_WARNING,
    SECRET_ENV_SCRIPT_PATH,
    render_export,
)
from rayito._agent._project_domain import AgentProject, AgentProjectException
from rayito._agent._secret_env_domain import SCRIPT_NAME
from rayito._agent._secret_env_project import check_runtime_collisions
from rayito._agentcore._deploy import AgentCorePorts, deploy_agentcore, plan_agentcore
from rayito._agentcore._domain import (
    SECURITY_SECRET_ENV,
    readable_secrets_of,
    secret_env_environment,
    secret_resource_arns,
)
from rayito._agentcore._messages import SECURITY_TEXTS
from rayito._limits import AGENT_SECRET_ENV_VARIABLE
from rayito.cli._agentcore_output import plan_document, plan_lines
from tests.unit import fake_agentcore as fake
from tests.unit.fake_agentcore import DATA
from tests.unit.test_agentcore_invoke import json_body
from tests.unit.test_agentcore_zero_cost import project as base_project

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[4]
RUNTIME_TEMPLATE = REPO / "infra" / "agentcore-runtime.yaml"
PLAN_CASES: list[dict[str, Any]] = json.loads((DATA / "plan-vectors.json").read_text("utf-8"))[
    "cases"
]
SECRET_ENV = {
    "DATABASE_URL": "app/db-url",
    "LLM_GATEWAY_KEY": SecretEnvRef(
        "arn:aws:secretsmanager:us-east-1:123456789012:secret:llm-AbCdEf", key="api_key"
    ),
}
SOURCES = (SecretEnvSource("app/config", prefix="APP_", keys=("DB_HOST",)),)
EXPECTED_ARNS = [
    "arn:aws:secretsmanager:us-east-1:123456789012:secret:app/db-url-??????",
    "arn:aws:secretsmanager:us-east-1:123456789012:secret:llm-AbCdEf",
    "arn:aws:secretsmanager:us-east-1:123456789012:secret:app/config-??????",
]
SECRET_ARN_SUFFIX = "-??????"
GET_SECRET_VALUE = "secretsmanager:GetSecretValue"
ENVIRON = {"RAYITO_BUCKET": fake.BUCKET}


def project(**fields: Any) -> AgentProject:
    return dataclasses.replace(base_project(), **fields)


def secret_project() -> AgentProject:
    return project(secret_env=SECRET_ENV, secret_env_from=SOURCES)


def ports(cloud: fake.FakeCloud) -> AgentCorePorts:
    return AgentCorePorts(
        gateways=cloud.gateways,
        invoker=fake.FakeInvoker(json_body()),
        environ=ENVIRON,
        clock=cloud.clock,
        sleep=cloud.clock.sleep,
    )


def plan_of(the_project: Any, agentcore: AgentCoreOptions | None = None) -> Any:
    return plan_agentcore(
        Path(DATA),
        project=the_project,
        region=fake.REGION,
        agentcore=agentcore,
        environ=ENVIRON,
    )


# ------------------------------------------------------------------------ plan


@pytest.mark.parametrize("case", PLAN_CASES, ids=[case["name"] for case in PLAN_CASES])
def test_secret_env_names_appear_only_when_the_plan_uses_them(
    case: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = case["expected"]
    if "error" in expected:
        return
    the_project = fake_project(case["project"])
    options = case["call"].get("agentcore", {})
    agentcore = AgentCoreOptions(
        bedrock_models=tuple(options.get("bedrockModels", ())),
        readable_secrets=tuple(options.get("readableSecrets", ())),
        external_model=bool(options.get("externalModel", False)),
    )
    monkeypatch.setenv("RAYITO_BUCKET", fake.BUCKET)
    plan = plan_of(the_project, agentcore).plan
    document = plan_document(plan)
    if "secretEnvNames" in expected:
        assert document["secretEnvNames"] == expected["secretEnvNames"]
        assert list(plan.secret_env_names) == expected["secretEnvNames"]
    else:
        assert "secretEnvNames" not in document
        assert plan.secret_env_names == ()


def fake_project(document: dict[str, Any]) -> AgentProject:
    return AgentProject.from_dict(document, base_dir=str(DATA))


def test_the_plan_carries_the_spec_and_unions_the_secrets() -> None:
    planned = plan_of(secret_project(), AgentCoreOptions(readable_secrets=("app/db-url",)))
    plan = planned.plan
    assert json.loads(plan.environment[AGENT_SECRET_ENV_VARIABLE]) == {
        "env": {
            "DATABASE_URL": {"secret": "app/db-url"},
            "LLM_GATEWAY_KEY": {
                "key": "api_key",
                "secret": "arn:aws:secretsmanager:us-east-1:123456789012:secret:llm-AbCdEf",
            },
        },
        "from": [{"keys": ["DB_HOST"], "prefix": "APP_", "secret": "app/config"}],
        "v": 1,
    }
    assert plan.readable_secrets == (
        "app/db-url",
        "arn:aws:secretsmanager:us-east-1:123456789012:secret:llm-AbCdEf",
        "app/config",
    )
    assert list(secret_resource_arns(plan.readable_secrets, fake.REGION, fake.ACCOUNT)) == (
        EXPECTED_ARNS
    )
    assert SECURITY_SECRET_ENV in plan.security
    assert plan.secret_env_names == ("APP_DB_HOST", "DATABASE_URL", "LLM_GATEWAY_KEY")


def test_the_security_line_names_the_variables_and_says_the_trade_off() -> None:
    plan = plan_of(secret_project()).plan
    lines = "\n".join(plan_lines(plan))
    assert "APP_DB_HOST, DATABASE_URL, LLM_GATEWAY_KEY" in lines
    assert SECURITY_TEXTS["secretEnv"].split("{names}")[1] in lines


def test_without_secret_env_the_plan_is_the_same_as_before() -> None:
    plain = project()
    assert secret_env_environment(plain) == {}
    assert readable_secrets_of(plain, AgentCoreOptions()) == ()
    plan = plan_of(plain).plan
    assert AGENT_SECRET_ENV_VARIABLE not in plan.environment
    assert SECURITY_SECRET_ENV not in plan.security
    assert "secretEnvNames" not in plan_document(plan)


# ---------------------------------------------------------------------- export


def test_the_agentcore_export_adds_the_helper_and_the_host_key_only_with_secret_env() -> None:
    tree = render_export(secret_project(), target="agentcore")
    plain = render_export(project(), target="agentcore")
    assert SCRIPT_NAME in tree and SCRIPT_NAME not in plain
    host = json.loads(tree["host.json"])
    assert sorted(host["secret_env"]) == ["python", "reserved", "script"]
    assert host["secret_env"]["script"] == SECRET_ENV_SCRIPT_PATH
    assert "secret_env" not in json.loads(plain["host.json"])
    dockerfile = tree["Dockerfile"].decode()
    assert f"COPY {SCRIPT_NAME} {SECRET_ENV_SCRIPT_PATH}" in dockerfile
    assert "requirements-aws-credentials.txt" in dockerfile
    assert "aws_env_credentials.py" not in dockerfile
    assert "Secretos como variables de entorno" in tree["README.md"].decode()


def test_the_docker_export_ignores_secret_env_with_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="rayito.agent"):
        tree = render_export(secret_project(), target="docker")
    assert DOCKER_SECRET_ENV_WARNING in caplog.text
    assert SCRIPT_NAME not in tree
    assert "secret_env" not in json.loads(tree["host.json"])
    assert DOCKER_SECRET_ENV_WARNING in tree["README.md"].decode()


def test_a_variable_the_runtime_already_sets_is_refused() -> None:
    with pytest.raises(AgentProjectException) as raised:
        check_runtime_collisions(secret_project(), {"DATABASE_URL": "x"})
    assert (raised.value.code, raised.value.path) == ("invalid_value", "secret_env.DATABASE_URL")
    check_runtime_collisions(secret_project(), {"OTHER": "x"})


# --------------------------------------------------- least privilege, applied


class _Loader(yaml.SafeLoader):  # type: ignore[name-defined,misc]
    pass


def _short_tag(loader: Any, suffix: str, node: Any) -> Any:
    name = suffix if suffix in {"Ref", "Condition"} else f"Fn::{suffix}"
    if isinstance(node, yaml.ScalarNode):
        return {name: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {name: loader.construct_sequence(node, deep=True)}
    return {name: loader.construct_mapping(node, deep=True)}


_Loader.add_multi_constructor("!", _short_tag)


def _runtime_statements() -> list[dict[str, Any]]:
    template = yaml.load(RUNTIME_TEMPLATE.read_text(encoding="utf-8"), Loader=_Loader)
    statements = template["Resources"]["RuntimeRole"]["Properties"]["Policies"][0][
        "PolicyDocument"
    ]["Statement"]
    return [item["Fn::If"][1] if "Fn::If" in item else item for item in statements]


def _actions(statement: dict[str, Any]) -> list[str]:
    action = statement["Action"]
    return [action] if isinstance(action, str) else list(action)


def test_the_runtime_role_reads_exactly_the_referenced_secrets_and_nothing_more() -> None:
    cloud = fake.cloud()
    deploy_agentcore(ports(cloud), Path(DATA), project=secret_project(), region=fake.REGION)
    parameters = cloud.stack.parameters_seen[0]
    arns = parameters["ReadableSecretArns"].split(",")
    assert arns == EXPECTED_ARNS
    for arn in arns:
        assert "*" not in arn.removesuffix(SECRET_ARN_SUFFIX)
    statements = _runtime_statements()
    (read,) = [item for item in statements if item.get("Sid") == "ReadSecrets"]
    assert read["Action"] == GET_SECRET_VALUE
    assert read["Resource"] == {"Ref": "ReadableSecretArns"}
    every_action = [action for item in statements for action in _actions(item)]
    assert "secretsmanager:*" not in every_action
    assert not any(action.startswith("kms:") for action in every_action)
    assert not any(action.endswith("ListSecrets") for action in every_action)
    secret_actions = [action for action in every_action if action.startswith("secretsmanager:")]
    assert secret_actions == [GET_SECRET_VALUE]


def test_no_value_ever_reaches_the_plan_the_export_or_the_runtime() -> None:
    cloud = fake.cloud()
    marker = "valor-que-nunca-sale"
    the_project = secret_project()
    deploy_agentcore(ports(cloud), Path(DATA), project=the_project, region=fake.REGION)
    planned = plan_of(the_project)
    surfaces = [
        json.dumps(plan_document(planned.plan)),
        "\n".join(plan_lines(planned.plan)),
        repr(planned.plan),
        json.dumps(dict(cloud.stack.parameters_seen[0])),
        json.dumps(dict(cloud.runtimes.specs[0].environment_variables)),
        *(data.decode("utf-8", "replace") for data in planned.tree.values()),
    ]
    assert not any(marker in surface for surface in surfaces)
    assert (
        cloud.runtimes.specs[0].environment_variables[AGENT_SECRET_ENV_VARIABLE]
        == (planned.plan.environment[AGENT_SECRET_ENV_VARIABLE])
    )


def test_the_options_of_secret_env_apply_to_agentcore() -> None:
    options = AgentDeployOptions(check_env=True)
    planned = plan_agentcore(
        Path(DATA), project=project(), options=options, region=fake.REGION, environ=ENVIRON
    )
    assert planned.env_file_secret is None


# ------------------------------------------------------- sample secret-env-echo

SAMPLE = REPO / "testdata" / "agent-secret-env" / "secret-env-echo"


def test_the_sample_project_and_its_agentcore_host_config_are_the_expected_ones() -> None:
    sample = AgentProject.load(SAMPLE)
    expected = json.loads((SAMPLE / "expected" / "project.json").read_text("utf-8"))
    assert sample.to_dict() == expected
    tree = render_export(sample, target="agentcore")
    assert tree["host.json"] == (SAMPLE / "expected" / "agentcore-host.json").read_bytes()


def test_the_sample_echoes_names_and_digests_never_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgres://db.internal.example/app")
    monkeypatch.setenv("TICKETS_TOKEN", "tk-value")
    monkeypatch.delenv("APP_DB_HOST", raising=False)
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    spec = importlib.util.spec_from_file_location("secret_env_echo", SAMPLE / "agent.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    reply = json.loads(module.echo({"prompt": "hola"})["text"])
    assert reply["present"] == ["DATABASE_URL", "TICKETS_TOKEN"]
    assert set(reply["sha256_8"]) == {"DATABASE_URL", "TICKETS_TOKEN"}
    assert "tk-value" not in json.dumps(reply)


# ------------------------------------------------ --create-secrets-from, --check-env


SECRET_STEP_KINDS = ("env_missing", "secret_env_created")


class _Store:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def exists(self, name: str) -> bool:
        return False

    def create(self, name: str, value: str) -> None:
        self.calls.append(("create", name))

    def update(self, name: str, value: str) -> None:
        self.calls.append(("update", name))


def test_deploying_creates_the_env_file_secret_and_reports_names_only(tmp_path: Path) -> None:
    source = tmp_path / "agent"
    source.mkdir()
    (source / ".env.example").write_text("TICKETS_API_URL=\nTICKETS_TOKEN=\nLOG_LEVEL=\n")
    env_file = tmp_path / "values.env"
    env_file.write_text("TICKETS_API_URL=https://tickets.example.com\nTICKETS_TOKEN=tk-value\n")
    cloud = fake.cloud()
    store = _Store()
    steps: list[AgentDeployStep] = []
    the_ports = dataclasses.replace(ports(cloud), secret_env_store=store)
    deploy_agentcore(
        the_ports,
        Path(DATA),
        project=project(source=str(source)),
        options=AgentDeployOptions(
            on_step=steps.append, create_secrets_from=str(env_file), check_env=True
        ),
        region=fake.REGION,
    )
    assert store.calls == [("create", "rayito/agents/support-bot/env")]
    secret_steps = [(step.kind, step.detail) for step in steps if step.kind in SECRET_STEP_KINDS]
    assert secret_steps[0][0] == "env_missing" and secret_steps[0][1].startswith("LOG_LEVEL ")
    assert secret_steps[-1] == ("secret_env_created", "created")
    parameters = cloud.stack.parameters_seen[0]
    assert "secret:rayito/agents/support-bot/env-??????" in parameters["ReadableSecretArns"]
    assert "tk-value" not in json.dumps([step.detail for step in steps])
