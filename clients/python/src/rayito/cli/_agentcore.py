"""Lo que `rayito agent deploy|invoke|status|stop|list|delete --target
agentcore` hacen además de lo de MicroVMs (ADR-031): leer las banderas, planificar
sin AWS, imprimir el plan, pedir confirmación, aplicar con el progreso en stderr
y escribir el resultado (texto o `--json`). Delgado: las decisiones son del
dominio (`_agentcore._domain`) y la ejecución de `AgentCoreDeployer`.

Códigos de salida: 0 éxito, `--dry-run` o nada que borrar; 1 fallo de AWS o un
"no" en la confirmación; 2 uso o entorno (`AgentCoreException.exit_code`).
Con `--json` un error es un único objeto `{"error": {"code", "message"},
"exitCode"}` por stdout y la línea `rayito: <mensaje>` por stderr.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import typer

from rayito._agent._deploy_plan import AgentDeployOptions
from rayito._agent._detect_report import DetectionReport
from rayito._agent._project_domain import AgentProject
from rayito._agent._secret_env_project import (
    parse_secret_env_flags,
    parse_secret_env_from_flags,
)
from rayito._agentcore._deploy import (
    AgentCorePorts,
    PlannedDeploy,
    agentcore_gateways,
    apply_agentcore,
    plan_agentcore,
    publish_planned_secret,
)
from rayito._agentcore._domain import runtime_name_for, stack_name_for
from rayito._agentcore._errors import AgentCoreErrorCode
from rayito._agentcore._messages import (
    CONFIRM_QUESTION,
    DELETE_CONFIRM_QUESTION,
    DRY_RUN_FOOTER,
    ERROR_PREFIX,
)
from rayito._agentcore._models import AgentCoreOptions
from rayito._agentcore._ports import AgentCoreGateways, RuntimeControl
from rayito._agentcore._service import AgentCoreDeleter, describe_agent, list_agents
from rayito._agentcore._targets import TARGET_AGENTCORE, TARGET_MICROVM, TARGETS
from rayito.cli import _agentcore_output as output
from rayito.cli._agent_deploy import (
    apply_project_flags,
    apply_serve_flags,
    detection_lines,
    parse_pairs,
)
from rayito.cli._agent_secret_env import notices_of
from rayito.cli._console import EXIT_FAILURE, EXIT_USAGE, echo, emit_json, fail
from rayito.cli._session import Clients, clients_of, json_mode
from rayito.cli.stack import parse_pairs as parse_tag_pairs
from rayito.exceptions import AgentCoreException, InvalidArgumentException

if TYPE_CHECKING:
    from rayito._agent._detect_report import DetectOverrides
    from rayito._agentcore._fleet import AgentCoreFleet

TARGET_HELP: Final = (
    "Dónde se despliega: microvm (por defecto, una flota de Lambda MicroVMs) o agentcore "
    "(Amazon Bedrock AgentCore Runtime)."
)
TAG_HELP: Final = (
    "Etiqueta K=V; repetible. Sólo con --target agentcore: se aplica a la pila "
    "rayito-agentcore-<nombre> (CloudFormation la propaga) y al runtime; útil si la "
    "organización exige etiquetas."
)
BEDROCK_MODEL_HELP: Final = (
    "Id o ARN de un modelo o perfil de inferencia de Bedrock que el rol del runtime puede "
    "invocar; repetible. Sólo con --target agentcore; por defecto, el modelo del proyecto."
)
READABLE_SECRET_HELP: Final = (
    "Nombre de un secreto de Secrets Manager que el rol del runtime puede leer; repetible. "
    "Sólo con --target agentcore; por defecto, ninguno."
)
IDLE_SESSION_TIMEOUT_HELP: Final = (
    "Segundos sin uso tras los que AgentCore termina la microVM de una sesión (60-28800; "
    "por defecto, 900). Sólo con --target agentcore."
)
MAX_SESSION_LIFETIME_HELP: Final = (
    "Segundos de vida máxima de la microVM de una sesión (60-28800; por defecto, 28800). "
    "Sólo con --target agentcore."
)
STOP_SESSION_ID_HELP: Final = (
    "Con --target agentcore: la sesión cuya microVM se para (StopRuntimeSession)."
)
DELETE_HELP: Final = (
    "Borra un agente desplegado con --target agentcore: el runtime, la pila "
    "rayito-agentcore-<nombre> (repositorio ECR con sus imágenes, proyecto y logs de "
    "CodeBuild y los dos roles) y el grupo de logs del runtime. Sólo borra lo que creó Rayito "
    "para ese nombre. Pide confirmación (--yes la salta). Gratis; deja de costar el "
    "almacenamiento de las imágenes."
)
LIST_HELP: Final = (
    "Lista los agentes desplegados con --target agentcore en la región (los runtimes que creó "
    "Rayito). Gratis: sólo lee."
)
DELETE_TARGET_HELP: Final = "Sólo agentcore: para una flota de MicroVMs usa rayito agent stop."
LIST_TARGET_HELP: Final = "Sólo agentcore."
DELETE_NAME_HELP: Final = "Nombre del agente (el de rayito agent deploy)."
DELETE_YES_HELP: Final = "No pedir confirmación (hace falta sin terminal o con --json)."
#: Nombres de las banderas que sólo valen con `--target agentcore`
#: (`option_requires_agentcore`).
TAG_FLAG: Final = "--tag"
BEDROCK_MODEL_FLAG: Final = "--bedrock-model"
READABLE_SECRET_FLAG: Final = "--readable-secret"
IDLE_SESSION_TIMEOUT_FLAG: Final = "--idle-session-timeout"
MAX_SESSION_LIFETIME_FLAG: Final = "--max-session-lifetime"
SESSION_ID_FLAG: Final = "--session-id"
TOKEN_FILE_FLAG: Final = "--token-file"
TOKEN_FILE_REASON: Final = "AgentCore autentica con IAM (SigV4), no con un access token"


def target_of(allowed: tuple[str, ...], flag: str) -> Callable[[str], str]:
    """Callback de typer que acepta sólo `allowed` como destino."""

    def check(value: str) -> str:
        if value not in allowed:
            raise typer.BadParameter(f"admitidas: {', '.join(allowed)}", param_hint=flag)
        return value

    return check


DEPLOY_TARGET_CHECK: Final = target_of(TARGETS, "--target")


@contextmanager
def agentcore_errors(*, json_output: bool) -> Iterator[None]:
    """Un `AgentCoreException` es `rayito: <mensaje>` por stderr, su documento
    por stdout con `--json` y su código de salida (2 uso, 1 AWS)."""
    try:
        yield
    except AgentCoreException as exc:
        echo(f"{ERROR_PREFIX}{exc}", err=True)
        if json_output:
            emit_json(output.error_document(exc))
        raise typer.Exit(exc.exit_code) from None


def require_agentcore_target(target: str, flags: Mapping[str, object]) -> None:
    """Las banderas de AgentCore con el destino MicroVM son
    `option_requires_agentcore`, antes de cualquier llamada a AWS."""
    if target == TARGET_AGENTCORE:
        return
    for flag, value in flags.items():
        if value is not None and value != []:
            raise AgentCoreException("option_requires_agentcore", option=flag)


def confirm(question: str, *, yes: bool, json_output: bool, refusal: AgentCoreErrorCode) -> None:
    """Con `--yes` sigue; en una terminal pregunta (un "no" sale con 1); sin
    terminal o con `--json` y sin `--yes`, el error `refusal`."""
    if yes:
        return
    if json_output or not sys.stdin.isatty():
        raise AgentCoreException(refusal)
    if not typer.confirm(question):
        raise typer.Exit(EXIT_FAILURE)


@dataclass(frozen=True)
class AgentCoreDeployRequest:
    """Lo que `rayito agent deploy --target agentcore` lee de sus banderas
    (tal cual, sin interpretar) y cuáles de las banderas de MicroVMs se
    pasaron (`microvm_flags`, para `option_not_supported`)."""

    project_path: Path
    overrides: DetectOverrides | None
    max_sessions: int | None
    queue: int | None
    queue_timeout: float | None
    env: list[str] | None
    allow_out: list[str] | None
    bucket: str | None
    build: bool | None
    verbose: bool
    yes: bool
    dry_run: bool
    tags: list[str]
    bedrock_models: list[str]
    readable_secrets: list[str]
    idle_session_timeout: int | None
    max_session_lifetime: int | None
    microvm_flags: Collection[str]
    secret_env: Sequence[str] = ()
    secret_env_from: Sequence[str] = ()
    create_secrets_from: Path | None = None
    check_env: bool = False


def adjust_with(request: AgentCoreDeployRequest) -> Callable[[AgentProject], AgentProject]:
    """Las banderas que aplican a AgentCore sobre el proyecto detectado: la
    concurrencia y la cola del host, el entorno y, para que sea un error, la
    política de salida. `--memory`, `--port`, `--timeout` y `--idle` no se
    aplican: la llamada los rechaza."""
    env = parse_pairs(request.env, "--env")
    secret_env = parse_secret_env_flags(request.secret_env)
    secret_env_from = parse_secret_env_from_flags(request.secret_env_from)

    def adjust(project: AgentProject) -> AgentProject:
        served = apply_serve_flags(
            project,
            max_sessions=request.max_sessions,
            idle_seconds=None,
            queue=request.queue,
            queue_timeout=request.queue_timeout,
            timeout=None,
            port=None,
        )
        return apply_project_flags(
            served,
            memory=None,
            env=env,
            allow_out=request.allow_out or [],
            secret_env=secret_env,
            secret_env_from=secret_env_from,
        )

    return adjust


def secret_env_options(request: AgentCoreDeployRequest) -> dict[str, Any]:
    """`create_secrets_from` y `check_env` de las banderas, sólo si se pasaron."""
    options: dict[str, Any] = {}
    if request.create_secrets_from is not None:
        options["create_secrets_from"] = str(request.create_secrets_from)
    if request.check_env:
        options["check_env"] = True
    return options


def run_agentcore_deploy(ctx: typer.Context, request: AgentCoreDeployRequest) -> None:
    json_output = json_mode(ctx)
    try:
        adjust = adjust_with(request)
        agentcore = AgentCoreOptions(
            bedrock_models=tuple(request.bedrock_models),
            readable_secrets=tuple(request.readable_secrets),
            idle_session_timeout_seconds=request.idle_session_timeout,
            max_session_lifetime_seconds=request.max_session_lifetime,
            tags=parse_tag_pairs(request.tags, option="--tag"),
        )
    except InvalidArgumentException as exc:
        fail(str(exc), code=EXIT_USAGE)
    options = AgentDeployOptions(
        bucket=request.bucket,
        build_image=request.build,
        on_build_logs=(lambda line: echo(line, err=True)) if request.verbose else None,
        **secret_env_options(request),
    )
    with agentcore_errors(json_output=json_output):
        clients = None if request.dry_run else clients_of(ctx)
        planned = plan_agentcore(
            request.project_path,
            options=options,
            overrides=request.overrides,
            region=ctx.meta.get("region") if clients is None else clients.region,
            session=None if clients is None else clients.session,
            agentcore=agentcore,
            cli_flags=request.microvm_flags,
            adjust=adjust,
        )
        print_plan(planned, json_output=json_output, dry_run=request.dry_run)
        if not json_output:
            for line in notices_of(planned.env_warnings, planned.env_file_secret):
                echo(line, err=True)
        if request.dry_run or clients is None:
            return
        confirm(
            CONFIRM_QUESTION, yes=request.yes, json_output=json_output, refusal="needs_confirmation"
        )
        publish_planned_secret(planned, clients.agentcore_override, clients.session)
        deployment = apply_agentcore(
            planned,
            agentcore_gateways(planned, clients.agentcore_override, clients.session),
            lambda _key, line: echo(line, err=True),
        )
    if json_output:
        emit_json(output.deploy_document(deployment))
        return
    for line in output.deployed_lines(deployment):
        echo(line)


def print_plan(planned: PlannedDeploy, *, json_output: bool, dry_run: bool) -> None:
    plan = planned.plan
    report = plan.detection if isinstance(plan.detection, DetectionReport) else None
    detection = None if report is None else report.to_dict()
    if json_output:
        if dry_run:
            emit_json(output.dry_run_document(plan, detection))
        return
    for line in detection_lines(report):
        echo(line, err=not dry_run)
    for line in output.plan_lines(plan):
        echo(line)
    if dry_run:
        echo(DRY_RUN_FOOTER)


def region_of(ctx: typer.Context) -> tuple[Clients, str]:
    clients = clients_of(ctx)
    return clients, clients.region


def runtimes_of(clients: Clients) -> RuntimeControl:
    """El control de AgentCore del llamante: el de los tests o el de boto3."""
    ports = clients.agentcore_override
    if ports is not None and ports.gateways is not None:
        return ports.gateways.runtimes
    from rayito._agentcore._aws import AgentCoreClients, AgentCoreRuntimes

    return AgentCoreRuntimes(AgentCoreClients(clients.region, clients.session))


def gateways_of(clients: Clients, name: str) -> AgentCoreGateways:
    ports = clients.agentcore_override
    if ports is not None and ports.gateways is not None:
        return ports.gateways
    from rayito._agentcore._aws import gateways_for

    return gateways_for(name, clients.region, session=clients.session)


def connect_agentcore_fleet(ctx: typer.Context, name: str) -> AgentCoreFleet:
    """La flota del agente `name`: con los puertos de los tests o conectando
    con boto3."""
    from rayito._agentcore._fleet import AgentCoreFleet

    clients = clients_of(ctx)
    ports: AgentCorePorts | None = clients.agentcore_override
    if ports is not None and ports.gateways is not None and ports.invoker is not None:
        _, status = describe_agent(
            ports.gateways.runtimes, name, clients.region, runtime_name_for(name)
        )
        return AgentCoreFleet(status, gateways=ports.gateways, invoker=ports.invoker)
    return AgentCoreFleet.connect(name, region=clients.region, session=clients.session)


def reject_token_file(token_file: object) -> None:
    """`--token-file` no existe con AgentCore (SigV4)."""
    if token_file is not None:
        raise AgentCoreException(
            "option_not_supported", option=TOKEN_FILE_FLAG, reason=TOKEN_FILE_REASON
        )


def show_status(ctx: typer.Context, name: str, token_file: object) -> None:
    json_output = json_mode(ctx)
    with agentcore_errors(json_output=json_output):
        reject_token_file(token_file)
        clients, region = region_of(ctx)
        _, status = describe_agent(runtimes_of(clients), name, region, runtime_name_for(name))
    if json_output:
        emit_json(output.status_document(status))
        return
    for line in output.status_lines(status):
        echo(line)


def stop_session(ctx: typer.Context, name: str, session_id: str | None) -> None:
    json_output = json_mode(ctx)
    with agentcore_errors(json_output=json_output):
        if session_id is None:
            raise AgentCoreException("session_required")
        stopped = connect_agentcore_fleet(ctx, name).stop_session(session_id)
    if json_output:
        emit_json(output.stop_document(name, session_id, stopped=stopped))
    elif stopped:
        echo(output.stopped_line(name, session_id))


def require_agentcore_listing_target(target: str, command: str) -> None:
    """`list` y `delete` sólo existen con AgentCore: el destino MicroVM es
    `target_unsupported`, con el mismo documento de error que los demás."""
    if target != TARGET_AGENTCORE:
        raise AgentCoreException("target_unsupported", command=command)


def list_command_body(ctx: typer.Context, target: str) -> None:
    json_output = json_mode(ctx)
    with agentcore_errors(json_output=json_output):
        require_agentcore_listing_target(target, "list")
        clients, region = region_of(ctx)
        items = list_agents(runtimes_of(clients))
    if json_output:
        emit_json(output.list_document(items, region))
        return
    for line in output.list_lines(items, region):
        echo(line)


def delete_command_body(ctx: typer.Context, name: str, target: str, *, yes: bool) -> None:
    json_output = json_mode(ctx)
    with agentcore_errors(json_output=json_output):
        require_agentcore_listing_target(target, "delete")
        clients, _ = region_of(ctx)
        deleter = AgentCoreDeleter(
            gateways_of(clients, name), on_progress=lambda line: echo(line, err=True)
        )
        plan = deleter.plan(name, runtime_name_for(name), stack_name_for(name))
        if not json_output:
            for line in output.delete_plan_lines(plan):
                echo(line)
        if plan.nothing:
            result = deleter.delete(plan)
        else:
            confirm(
                DELETE_CONFIRM_QUESTION,
                yes=yes,
                json_output=json_output,
                refusal="delete_needs_confirmation",
            )
            result = deleter.delete(plan)
    if json_output:
        emit_json(output.delete_document(result))
    elif not plan.nothing:
        echo(output.deleted_line(result))


__all__ = [
    "DEPLOY_TARGET_CHECK",
    "TARGET_MICROVM",
    "AgentCoreDeployRequest",
    "agentcore_errors",
    "connect_agentcore_fleet",
    "delete_command_body",
    "list_command_body",
    "require_agentcore_target",
    "run_agentcore_deploy",
    "show_status",
    "stop_session",
]
