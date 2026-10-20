"""`deploy_agent(target="agentcore")` contra AWS real (ADR-031, openspec
`agentcore-deploy`): los ejemplos de Python y OpenCode de
`testdata/agent-samples/` desplegados en Amazon Bedrock AgentCore Runtime sin
tocar su código y sin Docker local.

Para cada ejemplo: despliegue, runtime `READY` con MMDSv2, `invoke` y `stream`
contestan 42 (los que tienen herramienta), el id de sesión devuelto continúa la
sesión, un redespliegue sin cambios es tres `reuse` (ni build ni versión nueva
del runtime), `stop_session`, el rol de mínimo privilegio (la simulación de IAM
deniega otro modelo y un secreto que no se dio) y, al final, `delete` sin dejar
runtime, pila, repositorio ni proyecto de la ejecución. Además: un cambio de
entorno actualiza el mismo runtime (uno por nombre), la CLI hace el ciclo
completo con `--json`, y un runtime sin la marca de Rayito es `name_conflict`.

Guest y host se prueban una vez, aquí (`parity-checklist.md`): el espejo de
TypeScript (`agentcore-deploy.e2e.test.ts`) despliega los ejemplos de
TypeScript. Variables, coste y tope: cabecera de `agentcore_deploy_support.py`.
Cada despliegue se borra en `finally`; el inventario de la cuenta se compara
antes y después.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import pytest

from rayito import (
    AgentCoreFleet,
    AgentCoreOptions,
    AgentDeployOptions,
    AgentDeployStep,
    deploy_agent,
)
from rayito._agent._events import Done
from rayito._agentcore._domain import runtime_name_for
from rayito.exceptions import AgentCoreException

from .agentcore_deploy_support import (
    BUCKET_VAR,
    CLI,
    CLI_DEPLOY_BUDGET_SECONDS,
    CLI_READ_BUDGET_SECONDS,
    CONFLICT_SAMPLE,
    ENV_CHANGE_SAMPLE,
    MANIFEST,
    OTHER_MODEL_ID,
    UNLISTED_SECRET_NAME,
    Account,
    agent_name,
    region,
    require,
    require_enabled,
    run_id,
    sample_of,
    selected_samples,
    staged_sample,
    tags,
)

pytestmark = pytest.mark.e2e

PROMPT: Final = MANIFEST["prompt"]
EXPECTED_ANSWER: Final = MANIFEST["expectedAnswer"]
REUSE_KEYS: Final = {"stackNoChanges", "imageReused", "runtimeReuse"}
BUILD_KEYS: Final = {"buildStart", "sourceUpload"}


@pytest.fixture(scope="module")
def account() -> Iterator[Account]:
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


def options(steps: list[AgentDeployStep] | None = None) -> AgentDeployOptions:
    return AgentDeployOptions(
        bucket=require(BUCKET_VAR), on_step=None if steps is None else steps.append
    )


def agentcore_options() -> AgentCoreOptions:
    return AgentCoreOptions(tags=tags())


def deploy(directory: Path, steps: list[AgentDeployStep] | None = None) -> AgentCoreFleet:
    return deploy_agent(
        directory,
        target="agentcore",
        options=options(steps),
        agentcore=agentcore_options(),
        region=region(),
    )


@pytest.fixture
def fleets() -> Iterator[list[AgentCoreFleet]]:
    """Todo lo que un test despliega se borra al terminar, pase lo que pase."""
    created: list[AgentCoreFleet] = []
    yield created
    for fleet in created:
        with contextlib.suppress(AgentCoreException):
            fleet.delete()


def check_answer(sample: dict[str, Any], text: str) -> None:
    assert text.strip()
    if sample["toolUse"]:
        assert EXPECTED_ANSWER in text


@pytest.mark.parametrize("sample_id", selected_samples())
def test_a_sample_deploys_answers_and_is_deleted_without_leftovers(
    sample_id: str, account: Account, fleets: list[AgentCoreFleet], tmp_path: Path
) -> None:
    sample = sample_of(sample_id)
    name = agent_name(sample_id)
    directory = staged_sample(sample_id, tmp_path, name=name)
    steps: list[AgentDeployStep] = []

    fleet = deploy(directory, steps)
    fleets.append(fleet)

    status = fleet.status()
    assert status.status == "READY" and fleet.runtime_name.startswith("rayito_")
    assert fleet.name == name and fleet.image_uri.endswith(tuple("0123456789abcdef"))

    first = fleet.invoke(PROMPT)
    check_answer(sample, first.text)
    continued = fleet.invoke(PROMPT, session_id=first.session_id)
    assert continued.session_id == first.session_id

    with fleet.stream(PROMPT) as events:
        collected = list(events)
        streamed = events.session_id
    assert isinstance(collected[-1], Done) and streamed

    unchanged: list[AgentDeployStep] = []
    again = deploy(directory, unchanged)
    assert again.runtime_id == fleet.runtime_id
    assert {step.kind for step in unchanged} >= REUSE_KEYS
    assert not {step.kind for step in unchanged} & BUILD_KEYS
    assert again.runtime_version == fleet.runtime_version

    assert fleet.stop_session(first.session_id) is True

    role = account.runtime_role_arn(fleet.runtime_id)
    other = f"arn:aws:bedrock:{account.region}::foundation-model/{OTHER_MODEL_ID}"
    secret = (
        f"arn:aws:secretsmanager:{account.region}:{account.account_id}"
        f":secret:{UNLISTED_SECRET_NAME}-AbCdEf"
    )
    assert account.decision(role, "bedrock:InvokeModel", other) != "allowed"
    assert account.decision(role, "secretsmanager:GetSecretValue", secret) != "allowed"

    result = fleet.delete()
    assert (result.runtime_deleted, result.stack_deleted) == (True, True)
    assert not account.stack_exists(name) and not account.repository_exists(name)
    assert name not in account.owned_names()


def test_a_changed_environment_updates_the_same_runtime_without_a_duplicate(
    account: Account, fleets: list[AgentCoreFleet], tmp_path: Path
) -> None:
    name = agent_name(ENV_CHANGE_SAMPLE, "-env")
    fleet = deploy(staged_sample(ENV_CHANGE_SAMPLE, tmp_path, name=name, env={"MODE": "one"}))
    fleets.append(fleet)
    steps: list[AgentDeployStep] = []
    changed = deploy(
        staged_sample(ENV_CHANGE_SAMPLE, tmp_path / "second", name=name, env={"MODE": "two"}),
        steps,
    )
    assert changed.runtime_id == fleet.runtime_id
    assert int(changed.runtime_version) > int(fleet.runtime_version)
    assert "runtimeUpdate" in {step.kind for step in steps}
    assert account.owned_names().count(name) == 1
    assert fleet.status().status == "READY"


def test_the_cli_runs_the_whole_cycle_with_json(account: Account, tmp_path: Path) -> None:
    sample_id = CONFLICT_SAMPLE
    name = agent_name(sample_id, "-cli")
    directory = staged_sample(sample_id, tmp_path, name=name)
    tag_flags = [flag for key, value in tags().items() for flag in ("--tag", f"{key}={value}")]
    base = [*CLI, "--region", region(), "--json"]

    def run(*arguments: str, budget: float) -> dict[str, Any]:
        completed = subprocess.run(
            [*base, *arguments], capture_output=True, text=True, timeout=budget, check=False
        )
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout)  # type: ignore[no-any-return]

    plan = run(
        "agent",
        "deploy",
        str(directory),
        "--target",
        "agentcore",
        "--dry-run",
        *tag_flags,
        budget=CLI_READ_BUDGET_SECONDS,
    )
    assert plan["dryRun"] is True and plan["plan"]["name"] == name
    try:
        deployed = run(
            "agent",
            "deploy",
            str(directory),
            "--target",
            "agentcore",
            "--yes",
            "--bucket",
            require(BUCKET_VAR),
            *tag_flags,
            budget=CLI_DEPLOY_BUDGET_SECONDS,
        )
        assert deployed["status"] == "READY"
        listed = run("agent", "list", "--target", "agentcore", budget=CLI_READ_BUDGET_SECONDS)
        assert name in [agent["name"] for agent in listed["agents"]]
        status = run(
            "agent", "status", name, "--target", "agentcore", budget=CLI_READ_BUDGET_SECONDS
        )
        assert status["status"] == "READY"
        answer = run(
            "agent", "invoke", name, PROMPT, "--target", "agentcore", budget=CLI_READ_BUDGET_SECONDS
        )
        assert answer["text"].strip() and answer["sessionId"]
        stopped = run(
            "agent",
            "stop",
            name,
            "--target",
            "agentcore",
            "--session-id",
            answer["sessionId"],
            budget=CLI_READ_BUDGET_SECONDS,
        )
        assert stopped["stopped"] is True
    finally:
        removed = run(
            "agent",
            "delete",
            name,
            "--target",
            "agentcore",
            "--yes",
            budget=CLI_DEPLOY_BUDGET_SECONDS,
        )
    assert removed["runtimeDeleted"] is True and removed["stackDeleted"] is True
    assert name not in account.owned_names()


def test_a_runtime_without_the_marker_is_name_conflict_and_is_never_touched(
    account: Account, fleets: list[AgentCoreFleet], tmp_path: Path
) -> None:
    base_name = agent_name(CONFLICT_SAMPLE, "-base")
    fleet = deploy(staged_sample(CONFLICT_SAMPLE, tmp_path, name=base_name))
    fleets.append(fleet)
    clashing = agent_name(CONFLICT_SAMPLE, "-nc")
    created = account.control.create_agent_runtime(
        agentRuntimeName=runtime_name_for(clashing),
        agentRuntimeArtifact={"containerConfiguration": {"containerUri": fleet.image_uri}},
        roleArn=account.runtime_role_arn(fleet.runtime_id),
        networkConfiguration={"networkMode": "PUBLIC"},
        protocolConfiguration={"serverProtocol": "HTTP"},
        description="creado a mano por el e2e",
        tags=tags(),
    )
    try:
        with pytest.raises(AgentCoreException) as raised:
            deploy(staged_sample(CONFLICT_SAMPLE, tmp_path / "nc", name=clashing))
        assert raised.value.code == "name_conflict"
        assert not account.stack_exists(clashing)
    finally:
        account.control.delete_agent_runtime(agentRuntimeId=created["agentRuntimeId"])
