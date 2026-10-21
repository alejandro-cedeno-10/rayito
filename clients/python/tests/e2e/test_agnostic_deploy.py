"""El despliegue agnóstico de ADR-033 (openspec `agnostic-agent-deploy`)
contra AWS real, en AgentCore Runtime y en una flota de MicroVMs, con el SDK
y con la CLI:

- **Entrada estructurada y selección de grafo** (D3): `langgraph-structured`
  con `--graph triage`; `invoke` y `stream` con `input` devuelven `output`, la
  sesión continúa, una entrada que no cumple el esquema es `invalid_input` y un
  prompt es `input_required`; un redespliegue sin cambios no construye nada.
- **Secretos como variables de entorno** (D4): secretos creados por la prueba
  (uno de texto y dos JSON, por nombre, por ARN y con `version_stage`) llegan
  al agente (`secret-env-echo` devuelve un resumen de cada valor, nunca el
  valor); el valor no está en `environmentVariables` de `GetAgentRuntime`, ni
  en la imagen de ECR, ni en los logs del runtime; el rol sólo lee esos
  secretos; una sesión nueva ve el valor rotado; un secreto que no existe
  hace fallar el arranque en cerrado (`secret_env_denied`).
- **Modelo elegido por el código** (D2): `openai-compatible-env` en AgentCore
  con `external_model` (sin ningún permiso de Bedrock, la URL de la pasarela en
  el entorno y la clave sólo por `secret_env`) y en MicroVM detrás de la
  pasarela de secretos (URL de la pasarela y clave marcador).
- **Protocolo `langgraph-api`** (D5): el ejemplo servido con `langgraph dev`
  contesta un prompt y una entrada, y dos llamadas de la misma sesión van al
  mismo hilo; se mide la primera llamada.

Ningún grafo llama a un modelo. Guest y host se prueban una vez, aquí
(`parity-checklist.md`); el espejo de TypeScript
(`agnostic-deploy.e2e.test.ts`) prueba lo que sólo existe en su SDK.
Variables, coste y tope: cabecera de `agnostic_deploy_support.py`. Cada
despliegue se borra (AgentCore) o se para (MicroVM) en `finally`, y los
secretos se borran al terminar el módulo.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pytest

from rayito import (
    AgentCoreFleet,
    AgentCoreOptions,
    AgentDeployOptions,
    AgentDeployStep,
    AgentFleet,
    DetectOverrides,
    Done,
    deploy_agent,
)
from rayito.exceptions import AgentCoreException, AgentException, SandboxException

from .agent_project_support import new_access_token
from .agent_zero_config_support import BASE_VAR as MICROVM_BASE_VAR
from .agent_zero_config_support import BUCKET_VAR as MICROVM_BUCKET_VAR
from .agent_zero_config_support import SECRET_VAR as BEDROCK_SECRET_VAR
from .agentcore_deploy_support import (
    BUCKET_VAR,
    CLI,
    CLI_DEPLOY_BUDGET_SECONDS,
    CLI_READ_BUDGET_SECONDS,
    Account,
    region,
    require,
    require_enabled,
    run_id,
    tags,
)
from .agnostic_deploy_support import (
    EXAMPLE_MODEL,
    LANGGRAPH_API_SAMPLE,
    NORMAL_TICKET,
    OPENAI_COMPATIBLE_BASE_URL,
    OPENAI_COMPATIBLE_SAMPLE,
    PLACEHOLDER_GATEWAY_KEY,
    SECRET_ENV_DENIED_LINE,
    SECRET_ENV_LOADED_LINE,
    SECRET_ENV_NAMES,
    SECRET_ENV_SAMPLE,
    STRUCTURED_SAMPLE,
    TRIAGE_GRAPH,
    URGENT_TICKET,
    RunSecrets,
    digest,
    found_in,
    image_holds,
    random_value,
    require_agnostic,
    runtime_environment,
    staged_project,
    wait_for_log_line,
)

pytestmark = pytest.mark.e2e

URGENT_INPUT: Final = {"ticket_id": URGENT_TICKET}
NORMAL_INPUT: Final = {"ticket_id": NORMAL_TICKET}
URGENT_OUTPUT: Final = {"priority": "high", "summary": f"ticket {URGENT_TICKET}: prioridad high"}
NORMAL_OUTPUT: Final = {
    "priority": "normal",
    "summary": f"ticket {NORMAL_TICKET}: prioridad normal",
}
NOT_THE_SCHEMA: Final = {"ticket": URGENT_TICKET}
PROMPT: Final = "Clasifica el ticket, por favor."
LANGGRAPH_PROMPT: Final = "hola desde el e2e"
LANGGRAPH_FOLLOW_UP: Final = "segunda llamada"
LANGGRAPH_TICKET: Final = "T-7"
REUSE_KEYS: Final = {"stackNoChanges", "imageReused", "runtimeReuse"}
BUILD_KEYS: Final = {"buildStart", "sourceUpload"}
ALLOWED: Final = "allowed"
#: Las tablas del ejemplo `secret-env-echo` y los secretos que nombra; la
#: prueba los cambia por los suyos.
SECRET_ENV_TABLE: Final = "[secret_env]"
SAMPLE_DB_URL_LINE: Final = 'DATABASE_URL = "rayito-e2e/secret-env/db-url"'
SAMPLE_TICKETS_SECRET: Final = "rayito-e2e/secret-env/tickets"
SAMPLE_CONFIG_SECRET: Final = "rayito-e2e/secret-env/config"
CURRENT_STAGE: Final = "AWSCURRENT"
UNLISTED_SECRET_SUFFIX: Final = "unlisted-AbCdEf"
GATEWAY_LLM: Final = "llm"
GATEWAY_BEDROCK: Final = "bedrock"
TOKEN_FILE_NAME: Final = "agent.token"
#: Un modelo de Bedrock cualquiera: con `external_model` el rol no puede
#: invocar ninguno, tampoco el de los ejemplos.
BEDROCK_MODEL_ARN_TEMPLATE: Final = "arn:aws:bedrock:{region}::foundation-model/{model}"
INFERENCE_PROFILE_ARN_TEMPLATE: Final = (
    "arn:aws:bedrock:{region}:{account}:inference-profile/{model}"
)


# ------------------------------------------------------------------ fixtures


@pytest.fixture(scope="module")
def account() -> Iterator[Account]:
    """AgentCore habilitado; al terminar no queda nada de la ejecución."""
    require_agnostic()
    require_enabled()
    require(BUCKET_VAR)
    the_account = Account(region())
    yield the_account
    the_account.sweep(run_id())
    assert the_account.leftovers(run_id()) == {
        "runtimes": [],
        "stacks": [],
        "repositories": [],
        "projects": [],
    }


@pytest.fixture(scope="module")
def run_secrets() -> Iterator[RunSecrets]:
    require_agnostic()
    created = RunSecrets(region(), tags())
    try:
        yield created
    finally:
        created.delete_all()


@pytest.fixture
def agentcore_fleets() -> Iterator[list[AgentCoreFleet]]:
    created: list[AgentCoreFleet] = []
    yield created
    for fleet in created:
        with contextlib.suppress(AgentCoreException):
            fleet.delete()


@pytest.fixture
def microvm() -> str:
    """La imagen base de los despliegues en MicroVM; sin ella se saltan."""
    require_agnostic()
    require(MICROVM_BUCKET_VAR)
    require(BEDROCK_SECRET_VAR)
    return require(MICROVM_BASE_VAR)


@pytest.fixture
def microvm_fleets() -> Iterator[list[AgentFleet]]:
    created: list[AgentFleet] = []
    yield created
    for fleet in created:
        with contextlib.suppress(SandboxException):
            fleet.stop()


# ------------------------------------------------------------------- helpers


def name_of(kind: str) -> str:
    return f"agn-{kind}-{run_id()}"


def secret_name(kind: str) -> str:
    return f"rayito-e2e/{run_id()}/{kind}"


def deploy_agentcore(
    directory: Path,
    steps: list[AgentDeployStep] | None = None,
    *,
    overrides: DetectOverrides | None = None,
    external_model: bool = False,
) -> AgentCoreFleet:
    return deploy_agent(
        directory,
        target="agentcore",
        options=AgentDeployOptions(
            bucket=require(BUCKET_VAR), on_step=None if steps is None else steps.append
        ),
        overrides=overrides,
        agentcore=AgentCoreOptions(tags=tags(), external_model=external_model),
        region=region(),
    )


def deploy_microvm(
    directory: Path,
    secret_names: Mapping[str, str],
    *,
    overrides: DetectOverrides | None = None,
) -> AgentFleet:
    return deploy_agent(
        directory,
        options=AgentDeployOptions(
            bucket=require(MICROVM_BUCKET_VAR),
            secret_names=dict(secret_names),
            access_token=new_access_token(),
        ),
        overrides=overrides,
        region=region(),
    )


def assert_unchanged_redeploy(directory: Path, fleet: AgentCoreFleet, **kwargs: Any) -> None:
    steps: list[AgentDeployStep] = []
    again = deploy_agentcore(directory, steps, **kwargs)
    kinds = {step.kind for step in steps}
    assert again.runtime_id == fleet.runtime_id
    assert kinds >= REUSE_KEYS and not kinds & BUILD_KEYS
    assert again.runtime_version == fleet.runtime_version


def assert_deleted(account: Account, fleet: AgentCoreFleet) -> None:
    result = fleet.delete()
    assert (result.runtime_deleted, result.stack_deleted) == (True, True)
    assert fleet.name not in account.owned_names()


def assert_structured_graph(fleet: AgentFleet | AgentCoreFleet) -> None:
    """`input` con su `output`, la sesión que continúa, el `stream` y los dos
    errores de entrada, iguales en los dos destinos."""
    first = fleet.invoke(input=URGENT_INPUT)
    assert first.output == URGENT_OUTPUT and first.text == ""
    continued = fleet.invoke(input=NORMAL_INPUT, session_id=first.session_id)
    assert continued.output == NORMAL_OUTPUT
    with fleet.stream(input=URGENT_INPUT) as events:
        collected = list(events)
    done = collected[-1]
    assert isinstance(done, Done) and done.output == URGENT_OUTPUT
    for kwargs, detail_code in (
        ({"input": NOT_THE_SCHEMA}, "invalid_input"),
        ({"prompt": PROMPT}, "input_required"),
    ):
        with pytest.raises(AgentException) as raised:
            fleet.invoke(**kwargs)
        assert (raised.value.reason, raised.value.detail_code) == (
            "protocol_error",
            detail_code,
        )


def echoed(text: str) -> dict[str, Any]:
    document = json.loads(text)
    assert isinstance(document, dict)
    return document


def expected_digests(values: Mapping[str, str]) -> dict[str, str]:
    return {name: digest(value) for name, value in values.items()}


def cli(*arguments: str, budget: float, stdin: str | None = None) -> dict[str, Any]:
    completed = subprocess.run(
        [*CLI, "--region", region(), "--json", *arguments],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=budget,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    document = json.loads(completed.stdout)
    assert isinstance(document, dict)
    return document


def cli_events(*arguments: str, budget: float) -> list[dict[str, Any]]:
    completed = subprocess.run(
        [*CLI, "--region", region(), "--json", *arguments],
        capture_output=True,
        text=True,
        timeout=budget,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    return [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]


def tag_flags() -> list[str]:
    return [flag for key, value in tags().items() for flag in ("--tag", f"{key}={value}")]


@dataclass(frozen=True)
class SecretEnvSet:
    """Los tres secretos de `secret-env-echo` de un test: los valores que debe
    ver el agente (por variable) y el nombre y el ARN de cada secreto (por
    clase: `db-url`, `tickets`, `config`)."""

    values: dict[str, str]
    names: dict[str, str]
    arns: dict[str, str]


def created_secret_env(run_secrets: RunSecrets, owner: str) -> SecretEnvSet:
    """Crea los tres secretos con valores nuevos y nombres propios de `owner`
    (el test que los usa): el de texto, el JSON con `token` y el JSON de
    configuración, con una clave que no se pide."""
    values = {name: random_value() for name in SECRET_ENV_NAMES}
    names = {kind: secret_name(f"{owner}-{kind}") for kind in ("db-url", "tickets", "config")}
    contents: dict[str, str | dict[str, str]] = {
        "db-url": values["DATABASE_URL"],
        "tickets": {"token": values["TICKETS_TOKEN"]},
        "config": {"DB_HOST": values["APP_DB_HOST"], "DB_USER": random_value()},
    }
    arns = {kind: run_secrets.create(names[kind], contents[kind]) for kind in names}
    return SecretEnvSet(values, names, arns)


# ------------------------------------------------------------------ AgentCore


def test_agentcore_structured_graph_with_input_and_output(
    account: Account, agentcore_fleets: list[AgentCoreFleet], tmp_path: Path
) -> None:
    directory = staged_project(STRUCTURED_SAMPLE, tmp_path, name=name_of("graph"))
    overrides = DetectOverrides(graph=TRIAGE_GRAPH)
    fleet = deploy_agentcore(directory, overrides=overrides)
    agentcore_fleets.append(fleet)
    assert fleet.status().status == "READY"

    assert_structured_graph(fleet)
    assert_unchanged_redeploy(directory, fleet, overrides=overrides)
    assert_deleted(account, fleet)


def test_agentcore_secret_env_reaches_the_agent_and_nowhere_else(
    account: Account,
    run_secrets: RunSecrets,
    agentcore_fleets: list[AgentCoreFleet],
    tmp_path: Path,
) -> None:
    created = created_secret_env(run_secrets, "sdk")
    values, arns = created.values, created.arns
    db_url = (
        f'DATABASE_URL = {{ secret = "{created.names["db-url"]}", '
        f'version_stage = "{CURRENT_STAGE}" }}'
    )
    replace = {
        SAMPLE_DB_URL_LINE: db_url,
        SAMPLE_TICKETS_SECRET: arns["tickets"],
        SAMPLE_CONFIG_SECRET: created.names["config"],
    }
    name = name_of("senv")
    fleet = deploy_agentcore(
        staged_project(SECRET_ENV_SAMPLE, tmp_path, name=name, replace=replace)
    )
    agentcore_fleets.append(fleet)

    first = fleet.invoke(PROMPT)
    assert echoed(first.text) == {
        "present": list(SECRET_ENV_NAMES),
        "sha256_8": expected_digests(values),
    }
    environment = runtime_environment(account.control, fleet.runtime_id)
    assert "RAYITO_SECRET_ENV" in environment
    assert found_in(environment.values(), values.values()) == []
    role = account.runtime_role_arn(fleet.runtime_id)
    for arn in arns.values():
        assert account.decision(role, "secretsmanager:GetSecretValue", arn) == ALLOWED
    unlisted = arns["db-url"].rsplit(":", 1)[0] + f":{UNLISTED_SECRET_SUFFIX}"
    assert account.decision(role, "secretsmanager:GetSecretValue", unlisted) != ALLOWED

    rotated = random_value()
    run_secrets.rotate(created.names["db-url"], rotated)
    after = echoed(fleet.invoke(PROMPT).text)
    assert after["sha256_8"]["DATABASE_URL"] == digest(rotated)

    loaded = SECRET_ENV_LOADED_LINE.format(count=len(SECRET_ENV_NAMES))
    messages = wait_for_log_line(account.logs, fleet.runtime_id, loaded)
    every_value = [*values.values(), rotated]
    assert found_in(messages, every_value) == []
    assert image_holds(account.ecr, fleet.image_uri, every_value) == []

    missing = secret_name("missing")
    denied = staged_project(
        SECRET_ENV_SAMPLE,
        tmp_path / "denied",
        name=name,
        replace={
            **replace,
            SECRET_ENV_TABLE: f'{SECRET_ENV_TABLE}\nMISSING_VALUE = "{missing}"',
        },
    )
    deploy_agentcore(denied)
    with pytest.raises(AgentException) as raised:
        fleet.invoke(PROMPT)
    assert missing not in str(raised.value)
    wait_for_log_line(account.logs, fleet.runtime_id, SECRET_ENV_DENIED_LINE)
    assert_deleted(account, fleet)


def test_agentcore_external_model_has_no_bedrock_and_gets_its_key_from_secret_env(
    account: Account,
    run_secrets: RunSecrets,
    agentcore_fleets: list[AgentCoreFleet],
    tmp_path: Path,
) -> None:
    key = random_value()
    run_secrets.create(secret_name("llm-key"), key)
    directory = staged_project(
        OPENAI_COMPATIBLE_SAMPLE,
        tmp_path,
        name=name_of("extm"),
        extra=["", SECRET_ENV_TABLE, f'LLM_API_KEY = "{secret_name("llm-key")}"'],
    )
    fleet = deploy_agentcore(directory, external_model=True)
    agentcore_fleets.append(fleet)

    checks = echoed(fleet.invoke(PROMPT).text)
    assert checks == {
        "base_url_set": True,
        "base_url_is_gateway": False,
        "api_key_is_placeholder": False,
        "model_env": "LLM_MODEL",
    }
    environment = runtime_environment(account.control, fleet.runtime_id)
    assert environment["LLM_BASE_URL"] == OPENAI_COMPATIBLE_BASE_URL
    assert found_in(environment.values(), [key]) == []
    role = account.runtime_role_arn(fleet.runtime_id)
    for resource in (
        BEDROCK_MODEL_ARN_TEMPLATE.format(region=account.region, model=EXAMPLE_MODEL),
        INFERENCE_PROFILE_ARN_TEMPLATE.format(
            region=account.region, account=account.account_id, model=EXAMPLE_MODEL
        ),
    ):
        assert account.decision(role, "bedrock:InvokeModel", resource) != ALLOWED
    assert_deleted(account, fleet)


def test_agentcore_cli_structured_input_and_secret_env_from(
    account: Account, run_secrets: RunSecrets, tmp_path: Path
) -> None:
    """La CLI: `--graph` y `--input` / `--input-file -` / `--stream`, y
    `--secret-env` / `--secret-env-from` sobre un ejemplo sin tablas propias."""
    graph = name_of("cgraph")
    graph_dir = staged_project(STRUCTURED_SAMPLE, tmp_path, name=graph)
    created = created_secret_env(run_secrets, "cli")
    values = created.values
    senv = name_of("csenv")
    senv_dir = staged_project(SECRET_ENV_SAMPLE, tmp_path, name=senv, drop_from=SECRET_ENV_TABLE)
    deploy_flags = [
        "--target",
        "agentcore",
        "--yes",
        "--bucket",
        require(BUCKET_VAR),
        *tag_flags(),
    ]
    secret_flags = [
        "--secret-env",
        f"DATABASE_URL={created.names['db-url']}",
        "--secret-env",
        f"TICKETS_TOKEN={created.names['tickets']}#token",
        "--secret-env-from",
        f"{created.names['config']},prefix=APP_,keys=DB_HOST",
    ]
    target = ("--target", "agentcore")
    try:
        plan = cli(
            "agent",
            "deploy",
            str(graph_dir),
            "--graph",
            TRIAGE_GRAPH,
            "--target",
            "agentcore",
            "--dry-run",
            budget=CLI_READ_BUDGET_SECONDS,
        )
        assert plan["dryRun"] is True and plan["detection"]["options"] == {"graph": TRIAGE_GRAPH}
        deployed = cli(
            "agent",
            "deploy",
            str(graph_dir),
            "--graph",
            TRIAGE_GRAPH,
            *deploy_flags,
            budget=CLI_DEPLOY_BUDGET_SECONDS,
        )
        assert deployed["status"] == "READY"
        answer = cli(
            "agent",
            "invoke",
            graph,
            "--input",
            json.dumps(URGENT_INPUT),
            *target,
            budget=CLI_READ_BUDGET_SECONDS,
        )
        assert answer["output"] == URGENT_OUTPUT
        from_stdin = cli(
            "agent",
            "invoke",
            graph,
            "--input-file",
            "-",
            "--session-id",
            answer["sessionId"],
            *target,
            budget=CLI_READ_BUDGET_SECONDS,
            stdin=json.dumps(NORMAL_INPUT),
        )
        assert from_stdin["output"] == NORMAL_OUTPUT
        events = cli_events(
            "agent",
            "invoke",
            graph,
            "--input",
            json.dumps(URGENT_INPUT),
            "--stream",
            *target,
            budget=CLI_READ_BUDGET_SECONDS,
        )
        assert events[-1]["type"] == "done" and events[-1]["output"] == URGENT_OUTPUT

        secret_deploy = cli(
            "agent",
            "deploy",
            str(senv_dir),
            *secret_flags,
            *deploy_flags,
            budget=CLI_DEPLOY_BUDGET_SECONDS,
        )
        assert secret_deploy["status"] == "READY"
        echo = cli("agent", "invoke", senv, PROMPT, *target, budget=CLI_READ_BUDGET_SECONDS)
        assert echoed(echo["text"])["sha256_8"] == expected_digests(values)
        fleet = AgentCoreFleet.connect(senv, region=region())
        environment = runtime_environment(account.control, fleet.runtime_id)
        assert found_in(environment.values(), values.values()) == []
        assert image_holds(account.ecr, fleet.image_uri, list(values.values())) == []
    finally:
        for name in (graph, senv):
            with contextlib.suppress(AssertionError):
                cli(
                    "agent",
                    "delete",
                    name,
                    "--target",
                    "agentcore",
                    "--yes",
                    budget=CLI_DEPLOY_BUDGET_SECONDS,
                )
    assert graph not in account.owned_names() and senv not in account.owned_names()


def test_agentcore_cli_langgraph_api_protocol(account: Account, tmp_path: Path) -> None:
    name = name_of("lgapi")
    directory = staged_project(LANGGRAPH_API_SAMPLE, tmp_path, name=name)
    target = ("--target", "agentcore")
    try:
        deployed = cli(
            "agent",
            "deploy",
            str(directory),
            "--protocol",
            "langgraph-api",
            "--model-provider",
            "bedrock",
            "--model-id",
            EXAMPLE_MODEL,
            *target,
            "--yes",
            "--bucket",
            require(BUCKET_VAR),
            *tag_flags(),
            budget=CLI_DEPLOY_BUDGET_SECONDS,
        )
        assert deployed["status"] == "READY"
        started = time.monotonic()
        first = cli(
            "agent", "invoke", name, LANGGRAPH_PROMPT, *target, budget=CLI_READ_BUDGET_SECONDS
        )
        print(
            f"\n[langgraph-api] primera llamada de una sesión nueva: "
            f"{time.monotonic() - started:.1f} s",
            flush=True,
        )
        assert first["text"] == LANGGRAPH_PROMPT.upper()
        follow_up = cli(
            "agent",
            "invoke",
            name,
            LANGGRAPH_FOLLOW_UP,
            "--session-id",
            first["sessionId"],
            *target,
            budget=CLI_READ_BUDGET_SECONDS,
        )
        assert follow_up["text"] == LANGGRAPH_FOLLOW_UP.upper()
        assert follow_up["sessionId"] == first["sessionId"]
        ticket = cli(
            "agent",
            "invoke",
            name,
            "--input",
            json.dumps({"ticket_id": LANGGRAPH_TICKET}),
            *target,
            budget=CLI_READ_BUDGET_SECONDS,
        )
        assert ticket["text"] == f"ticket {LANGGRAPH_TICKET}"
    finally:
        removed = cli("agent", "delete", name, *target, "--yes", budget=CLI_DEPLOY_BUDGET_SECONDS)
    assert removed["runtimeDeleted"] is True and name not in account.owned_names()


# -------------------------------------------------------------------- MicroVM


def test_microvm_structured_graph_with_input_and_output(
    microvm: str, microvm_fleets: list[AgentFleet], tmp_path: Path
) -> None:
    directory = staged_project(
        STRUCTURED_SAMPLE, tmp_path, name=f"agn-{run_id()}-graph", base=microvm
    )
    fleet = deploy_microvm(
        directory,
        {GATEWAY_BEDROCK: require(BEDROCK_SECRET_VAR)},
        overrides=DetectOverrides(graph=TRIAGE_GRAPH),
    )
    microvm_fleets.append(fleet)
    assert fleet.status().healthy == 1
    assert_structured_graph(fleet)


def test_microvm_secret_env_reaches_the_agent(
    microvm: str,
    run_secrets: RunSecrets,
    microvm_fleets: list[AgentFleet],
    tmp_path: Path,
) -> None:
    created = created_secret_env(run_secrets, "vm")
    directory = staged_project(
        SECRET_ENV_SAMPLE,
        tmp_path,
        name=f"agn-{run_id()}-senv",
        replace={
            SAMPLE_DB_URL_LINE: f'DATABASE_URL = "{created.names["db-url"]}"',
            SAMPLE_TICKETS_SECRET: created.arns["tickets"],
            SAMPLE_CONFIG_SECRET: created.names["config"],
        },
        base=microvm,
    )
    fleet = deploy_microvm(directory, {GATEWAY_BEDROCK: require(BEDROCK_SECRET_VAR)})
    microvm_fleets.append(fleet)
    assert echoed(fleet.invoke(PROMPT).text) == {
        "present": list(SECRET_ENV_NAMES),
        "sha256_8": expected_digests(created.values),
    }


def test_microvm_model_chosen_by_code_goes_through_the_gateway(
    microvm: str,
    run_secrets: RunSecrets,
    microvm_fleets: list[AgentFleet],
    tmp_path: Path,
) -> None:
    gateway_secret = f"e2e-{run_id()}/llm"
    run_secrets.create(f"rayito/{gateway_secret}", PLACEHOLDER_GATEWAY_KEY)
    directory = staged_project(
        OPENAI_COMPATIBLE_SAMPLE, tmp_path, name=f"agn-{run_id()}-model", base=microvm
    )
    fleet = deploy_microvm(directory, {GATEWAY_LLM: gateway_secret})
    microvm_fleets.append(fleet)
    assert echoed(fleet.invoke(PROMPT).text) == {
        "base_url_set": True,
        "base_url_is_gateway": True,
        "api_key_is_placeholder": True,
        "model_env": "LLM_MODEL",
    }


def test_microvm_cli_langgraph_api_protocol(microvm: str, tmp_path: Path) -> None:
    """`deploy` escribe el access token en `--token-file` (que aún no existe)
    e `invoke` lo lee de ahí: nunca va por argv."""
    name = f"agn-{run_id()}-lgapi"
    directory = staged_project(LANGGRAPH_API_SAMPLE, tmp_path / "src", name=name, base=microvm)
    token = ("--token-file", str(tmp_path / TOKEN_FILE_NAME))
    try:
        deployed = cli(
            "agent",
            "deploy",
            str(directory),
            "--protocol",
            "langgraph-api",
            "--model-provider",
            "bedrock",
            "--model-id",
            EXAMPLE_MODEL,
            "--secret",
            f"{GATEWAY_BEDROCK}={require(BEDROCK_SECRET_VAR)}",
            "--bucket",
            require(MICROVM_BUCKET_VAR),
            "--yes",
            *token,
            budget=CLI_DEPLOY_BUDGET_SECONDS,
        )
        assert deployed["name"] == name
        started = time.monotonic()
        first = cli(
            "agent", "invoke", name, LANGGRAPH_PROMPT, *token, budget=CLI_READ_BUDGET_SECONDS
        )
        print(
            f"\n[langgraph-api] primera llamada en MicroVM: {time.monotonic() - started:.1f} s",
            flush=True,
        )
        assert first["text"] == LANGGRAPH_PROMPT.upper()
        ticket = cli(
            "agent",
            "invoke",
            name,
            "--input",
            json.dumps({"ticket_id": LANGGRAPH_TICKET}),
            "--session-id",
            first["sessionId"],
            *token,
            budget=CLI_READ_BUDGET_SECONDS,
        )
        assert ticket["text"] == f"ticket {LANGGRAPH_TICKET}"
    finally:
        cli("agent", "stop", name, budget=CLI_READ_BUDGET_SECONDS)
