"""Lo común a `test_agentcore_deploy.py`: los agentes de ejemplo de Python
(`testdata/agent-samples/`) preparados para desplegarlos en AgentCore Runtime
con un nombre propio de la ejecución, y el inventario que comprueba que la
limpieza no deja nada de la ejecución.

Variables de entorno, además de `RAYITO_E2E=1`:

- `RAYITO_E2E_AGENTCORE=1`: sin ella todo se salta (despliega runtimes, pilas
  e imágenes de verdad y cuesta dinero).
- `AWS_REGION`: una región con AgentCore Runtime y builds arm64 de CodeBuild
  (`us-east-1` es la de la aceptación: el modelo de los ejemplos es el perfil de
  inferencia `us.` de Claude Haiku 4.5 y lo invoca el rol del runtime).
- `RAYITO_BUCKET`: el bucket de artefactos de esa región (el de `rayito setup`).
- `RAYITO_E2E_RUN_ID`: id de la ejecución, 8 minúsculas o dígitos; va en el
  nombre de cada agente y la limpieza borra sólo lo que lo lleva.
- `RAYITO_E2E_TAGS` (opcional): etiquetas `K=V` separadas por comas para la
  pila y el runtime, si una SCP de la organización las exige al crear recursos.
  Nunca van en el código.
- `RAYITO_E2E_AGENTCORE_SAMPLES` (opcional): ids del manifiesto separados por
  comas.

Cada agente se llama `<ejemplo>-<ejecución>` (como mucho 41 caracteres) mediante
`[image].template`. Coste de la aceptación completa: unos 0,80 USD (CodeBuild,
ECR, sesiones de AgentCore y el modelo); el tope, 5 USD. Guest y host se prueban
una vez, aquí; el espejo de TypeScript despliega los ejemplos de TypeScript.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import boto3
import pytest
from botocore.exceptions import ClientError

from rayito._agentcore._aws import AgentCoreClients, AgentCoreRuntimes, gateways_for
from rayito._agentcore._domain import (
    AGENT_NAME_MAX_LENGTH,
    repository_name_for,
    runtime_name_for,
    stack_name_for,
)
from rayito._agentcore._domain import is_owned as runtime_is_owned
from rayito._agentcore._models import RuntimeListing
from rayito._agentcore._service import AgentCoreDeleter, list_agents

REPO_ROOT: Final = Path(__file__).resolve().parents[4]
SAMPLES_DIR: Final = REPO_ROOT / "testdata" / "agent-samples"
MANIFEST: Final[dict[str, Any]] = json.loads((SAMPLES_DIR / "manifest.json").read_text("utf-8"))
AGENTCORE_FLAG_VAR: Final = "RAYITO_E2E_AGENTCORE"
RUN_ID_VAR: Final = "RAYITO_E2E_RUN_ID"
BUCKET_VAR: Final = "RAYITO_BUCKET"
TAGS_VAR: Final = "RAYITO_E2E_TAGS"
SAMPLES_VAR: Final = "RAYITO_E2E_AGENTCORE_SAMPLES"
REGION_VAR: Final = "AWS_REGION"
RUN_ID_PATTERN: Final = re.compile(r"[a-z0-9]{8}")
IGNORED_DIRS: Final = (".venv", "node_modules", "__pycache__", ".git")
#: Los ejemplos de Python y OpenCode: el espejo de TypeScript despliega el resto.
PYTHON_SAMPLES: Final = tuple(
    sample["id"]
    for sample in MANIFEST["samples"]
    if sample["language"] == "python" or sample["id"] == "opencode"
)
ENV_CHANGE_SAMPLE: Final = "python-strands"
CONFLICT_SAMPLE: Final = "python-flask-custom"
CLI: Final = ("python", "-m", "rayito.cli")
#: Un runtime tarda unos minutos en crearse y otros tantos el build; el resto
#: de esperas las acota el propio SDK.
CLI_DEPLOY_BUDGET_SECONDS: Final = 3000.0
CLI_READ_BUDGET_SECONDS: Final = 180.0
OTHER_MODEL_ID: Final = "amazon.nova-lite-v1:0"
UNLISTED_SECRET_NAME: Final = "rayito-e2e-unlisted-secret"


def require_enabled() -> None:
    if os.environ.get(AGENTCORE_FLAG_VAR) != "1":
        pytest.skip(f"exporta {AGENTCORE_FLAG_VAR}=1 (cabecera de agentcore_deploy_support.py)")


def require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"exporta {name} (cabecera de agentcore_deploy_support.py)")
    return value


def run_id() -> str:
    value = require(RUN_ID_VAR)
    if not RUN_ID_PATTERN.fullmatch(value):
        pytest.fail(f"{RUN_ID_VAR} debe ser de 8 minúsculas o dígitos")
    return value


def region() -> str:
    return require(REGION_VAR)


def tags() -> dict[str, str]:
    pairs = (pair.partition("=") for pair in os.environ.get(TAGS_VAR, "").split(",") if pair)
    return {key.strip(): value.strip() for key, _, value in pairs}


def selected_samples() -> list[str]:
    wanted = {name for name in os.environ.get(SAMPLES_VAR, "").split(",") if name}
    return [sample for sample in PYTHON_SAMPLES if not wanted or sample in wanted]


def agent_name(sample_id: str, suffix: str = "") -> str:
    name = f"{sample_id}-{run_id()}{suffix}"
    assert len(name) <= AGENT_NAME_MAX_LENGTH, name
    return name


def sample_of(sample_id: str) -> dict[str, Any]:
    return next(sample for sample in MANIFEST["samples"] if sample["id"] == sample_id)


def staged_sample(
    sample_id: str, tmp_path: Path, *, name: str, env: dict[str, str] | None = None
) -> Path:
    """Una copia del ejemplo (sin entornos instalados) con un `rayito-agent.toml`
    mínimo: sólo el nombre del agente y, si se pide, su entorno. Todo lo demás
    lo decide la detección."""
    target = tmp_path / name
    shutil.copytree(
        SAMPLES_DIR / sample_of(sample_id)["dir"],
        target,
        ignore=shutil.ignore_patterns(*IGNORED_DIRS),
    )
    lines = ['schema = "rayito.agent-project/1"', f'name = "{name}"', "", "[image]"]
    lines.append(f'template = "{name}"')
    if env:
        lines += ["", "[env]", *(f'{key} = "{value}"' for key, value in env.items())]
    (target / "rayito-agent.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


@dataclass
class Account:
    """Las lecturas de sólo lectura de AWS que comprueban la limpieza y la
    simulación de IAM del rol del runtime."""

    region: str

    def __post_init__(self) -> None:
        session = boto3.session.Session(region_name=self.region)
        self.control = session.client("bedrock-agentcore-control")
        self.cloudformation = session.client("cloudformation")
        self.ecr = session.client("ecr")
        self.codebuild = session.client("codebuild")
        self.logs = session.client("logs")
        self.iam = session.client("iam")
        self.account_id = str(session.client("sts").get_caller_identity()["Account"])

    def runtimes(self) -> list[RuntimeListing]:
        return list(AgentCoreRuntimes(AgentCoreClients(self.region, None)).list_runtimes())

    def owned_names(self) -> list[str]:
        runtimes = AgentCoreRuntimes(AgentCoreClients(self.region, None))
        return [item.name for item in list_agents(runtimes)]

    def stack_exists(self, name: str) -> bool:
        try:
            self.cloudformation.describe_stacks(StackName=stack_name_for(name))
        except ClientError as exc:
            if "does not exist" in str(exc):
                return False
            raise
        return True

    def repository_exists(self, name: str) -> bool:
        try:
            self.ecr.describe_repositories(repositoryNames=[repository_name_for(name)])
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "RepositoryNotFoundException":
                return False
            raise
        return True

    def runtime_role_arn(self, runtime_id: str) -> str:
        return str(self.control.get_agent_runtime(agentRuntimeId=runtime_id)["roleArn"])

    def decision(self, role_arn: str, action: str, resource: str) -> str:
        result = self.iam.simulate_principal_policy(
            PolicySourceArn=role_arn, ActionNames=[action], ResourceArns=[resource]
        )
        return str(result["EvaluationResults"][0]["EvalDecision"])

    def leftovers(self, run: str) -> dict[str, list[str]]:
        """Todo lo de la ejecución que sigue en la cuenta: runtimes, pilas y
        repositorios cuyo nombre lleva el id de la ejecución."""
        runtimes = [
            item.name for item in self.runtimes() if runtime_is_owned(item) and run in item.name
        ]
        stacks = [
            summary["StackName"]
            for page in self.cloudformation.get_paginator("list_stacks").paginate()
            for summary in page["StackSummaries"]
            if summary["StackStatus"] != "DELETE_COMPLETE" and run in summary["StackName"]
        ]
        repositories = [
            repo["repositoryName"]
            for page in self.ecr.get_paginator("describe_repositories").paginate()
            for repo in page["repositories"]
            if run in repo["repositoryName"]
        ]
        projects = [
            project for project in self.codebuild.list_projects()["projects"] if run in project
        ]
        return {
            "runtimes": runtimes,
            "stacks": stacks,
            "repositories": repositories,
            "projects": projects,
        }

    def sweep(self, run: str) -> None:
        """Borra sólo lo que lleva el id de la ejecución y quedó vivo."""

        for runtime_name in self.leftovers(run)["runtimes"]:
            name = runtime_name.removeprefix("rayito_").replace("_", "-")
            gateways = gateways_for(name, self.region)
            deleter = AgentCoreDeleter(gateways)
            deleter.delete(deleter.plan(name, runtime_name_for(name), stack_name_for(name)))
