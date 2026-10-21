"""`rayito agent ...`: el agente de IA como proyecto.

- `agent template build` (`ai-agent-fast-start`): construye la imagen de
  `AgentTemplate` con los runtimes del agente. Mismas banderas que
  `rayito template build` más `--no-deepagents`, `--no-prefetch` y
  `--kernel-warmup`; sin fichero de spec, porque la receta es la del SDK.
- `agent init | build | run | deploy | export | invoke | status | stop`
  (`agent-runtimes-byo`): conchas finas sobre `AgentProject` y
  `AgentDeployment`. `init` y `export` no llaman a AWS. Códigos de salida: 1
  si la operación falla, 2 para uso (fichero de proyecto inválido, ficheros
  que `init` no sobrescribe sin `--force`, destino de `export` ocupado).
- `agent detect` y `agent deploy [RUTA]` (ADR-029): detectan el proyecto sin
  ejecutar su código y lo despliegan como una flota con `deploy_agent()`
  (`--dry-run` sólo calcula el plan y el coste). `invoke`, `status` y `stop`
  aceptan el nombre de la flota además del id de un sandbox.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Annotated, Any

import typer

from rayito import (
    AgentCoreFleet,
    AgentDeployment,
    AgentDeployOptions,
    AgentDeployStep,
    AgentFleet,
    AgentProject,
    AgentResult,
    DetectOverrides,
    ScalingOptions,
    deploy_agent,
    plan_deploy,
)
from rayito._agent._deploy_plan import project_for, with_excluded_file
from rayito._agent._detect_report import DetectionReport
from rayito._agent._events import AgentEvent, AgentFailed, Done, Text, TextDelta, ToolCall
from rayito._agent._fleet_base import fleet_settings_of
from rayito._agent._project_base import app_hiding_own_routes
from rayito._agent._project_file import project_name_of
from rayito._agent._scaling_domain import SESSION_MODES
from rayito._agent._secret_env_deploy import prepare_secret_env
from rayito._agent._secret_env_project import (
    parse_secret_env_flags,
    parse_secret_env_from_flags,
)
from rayito._agent._template import (
    AGENT_TEMPLATE_RUNTIMES,
    DEFAULT_AGENT_TEMPLATE_BASE,
    DEFAULT_AGENT_TEMPLATE_NAME,
    AgentTemplate,
)
from rayito._agentcore._targets import TARGET_AGENTCORE, TARGET_MICROVM
from rayito._aws_region import resolve_region
from rayito._images import DEFAULT_BUILD_TIMEOUT_SECONDS
from rayito._limits import AGENT_MIN_MEMORY_MIB
from rayito.cli._agent_deploy import (
    AGENT_TOKEN_FILE_DEFAULT,
    CompactBuildLog,
    apply_project_flags,
    apply_serve_flags,
    deploy_token,
    describe_plan,
    detection_lines,
    parse_pairs,
    pool_backend_for,
)
from rayito.cli._agent_scaffold import (
    DEFAULT_PROVIDER,
    LANGUAGES,
    PROVIDER_PRESETS,
    PYTHON_LANGUAGE,
    SCAFFOLD_FALLBACK_REGION,
    SCAFFOLD_RUNTIMES,
    TYPESCRIPT_LANGUAGE,
    ScaffoldConflictError,
    ScaffoldRequest,
    scaffold_files,
    write_scaffold,
)
from rayito.cli._agent_secret_env import (
    CREATE_SECRETS_NEEDS_CONFIRMATION,
    confirmed,
    env_file_secret_line,
    env_file_secret_of,
    env_warning_line,
)
from rayito.cli._agentcore import (
    BEDROCK_MODEL_FLAG,
    BEDROCK_MODEL_HELP,
    DELETE_HELP,
    DELETE_NAME_HELP,
    DELETE_TARGET_HELP,
    DELETE_YES_HELP,
    DEPLOY_TARGET_CHECK,
    EXTERNAL_MODEL_FLAG,
    EXTERNAL_MODEL_HELP,
    IDLE_SESSION_TIMEOUT_FLAG,
    IDLE_SESSION_TIMEOUT_HELP,
    LIST_HELP,
    LIST_TARGET_HELP,
    MAX_SESSION_LIFETIME_FLAG,
    MAX_SESSION_LIFETIME_HELP,
    READABLE_SECRET_FLAG,
    READABLE_SECRET_HELP,
    SESSION_ID_FLAG,
    STOP_SESSION_ID_HELP,
    TAG_FLAG,
    TAG_HELP,
    TARGET_HELP,
    AgentCoreDeployRequest,
    agentcore_errors,
    connect_agentcore_fleet,
    delete_command_body,
    list_command_body,
    reject_token_file,
    require_agentcore_target,
    run_agentcore_deploy,
    show_status,
    stop_session,
)
from rayito.cli._console import EXIT_USAGE, echo, emit_json, fail, visible_controls
from rayito.cli._session import clients_of, json_mode
from rayito.cli._tokens import TOKEN_ENV_VAR, TokenFileError
from rayito.cli.sandbox import TokenFileOption, kill_one, token_or_exit
from rayito.exceptions import (
    AgentException,
    BuildException,
    CapacityException,
    InvalidArgumentException,
    QuotaExceededException,
    SandboxException,
)

EXPORT_TARGETS = ("docker", "agentcore")

agent_app = typer.Typer(no_args_is_help=True, help="Agente de IA dentro del sandbox.")
agent_template_app = typer.Typer(
    no_args_is_help=True, help="Plantilla de imagen con los runtimes del agente (AgentTemplate)."
)
agent_app.add_typer(agent_template_app, name="template")


def agent_template_of(
    *,
    name: str,
    base: str,
    memory_mb: int,
    deepagents: bool,
    prefetch: bool,
    kernel_warmup: bool,
) -> AgentTemplate:
    """La `AgentTemplate` que piden las banderas de la CLI."""
    runtimes = tuple(r for r in AGENT_TEMPLATE_RUNTIMES if deepagents or r != "deepagents")
    return AgentTemplate(
        name=name,
        base=base,
        runtimes=runtimes,
        prefetch=prefetch,
        kernel_warmup=kernel_warmup,
        memory_mib=memory_mb,
    )


@agent_template_app.command("build")
def build_command(
    ctx: typer.Context,
    bucket: Annotated[str, typer.Option("--bucket", help="Bucket S3 del artefacto de build.")],
    name: Annotated[
        str, typer.Option("--name", help="Nombre de la imagen a crear/actualizar.")
    ] = DEFAULT_AGENT_TEMPLATE_NAME,
    base: Annotated[
        str, typer.Option("--base", help="Imagen base (publicada con rayito image publish).")
    ] = DEFAULT_AGENT_TEMPLATE_BASE,
    memory_mb: Annotated[int, typer.Option("--memory-mb")] = AGENT_MIN_MEMORY_MIB,
    deepagents: Annotated[
        bool, typer.Option("--deepagents/--no-deepagents", help="Instala el venv de deepagents.")
    ] = True,
    prefetch: Annotated[
        bool,
        typer.Option("--prefetch/--no-prefetch", help="Hornea el demonio de precarga."),
    ] = True,
    kernel_warmup: Annotated[
        bool,
        typer.Option(
            "--kernel-warmup/--no-kernel-warmup",
            help=(
                "Mantiene el calentamiento del kernel de run_code (numpy, pandas...). "
                "Apagado por defecto: create() vuelve antes y el snapshot pesa menos."
            ),
        ),
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Reconstruye aunque nada cambiara.")
    ] = False,
    timeout: Annotated[
        float, typer.Option("--timeout", help="Segundos de espera del build.")
    ] = DEFAULT_BUILD_TIMEOUT_SECONDS,
) -> None:
    """Construye la imagen --name con OpenCode, ripgrep y (salvo
    --no-deepagents) deepagents, todo fijado por hash.

    Cada versión nueva cuesta almacenamiento de snapshot (≈ 3,0 GB, mínimo una
    semana: ≈ $0,056/semana); las versiones se borran con `rayito image`.
    """
    clients = clients_of(ctx)
    emit_json_mode = json_mode(ctx)
    try:
        template = agent_template_of(
            name=name,
            base=base,
            memory_mb=memory_mb,
            deepagents=deepagents,
            prefetch=prefetch,
            kernel_warmup=kernel_warmup,
        )
        info = template.build(
            bucket=bucket,
            force=force,
            timeout=timeout,
            on_build_logs=None if emit_json_mode else echo,
            region=clients.region,
            session=clients.session,
        )
    except InvalidArgumentException as exc:
        fail(str(exc))
    except BuildException as exc:
        fail(f"build fallido: {exc}")
    if emit_json_mode:
        emit_json({"templateId": info.template_id, "buildId": info.build_id, "alias": info.alias})
    else:
        echo(f"template_id={info.template_id}")
        echo(f"build_id={info.build_id}")


CUSTOM_INIT_HINT = (
    'la app usa protocol = "raw": el despliegue reenvía tus rutas (/health, /invoke) con '
    'deployment.url y deployment.headers; deployment.invoke() es para protocol = "agentcore"'
)
USAGE_UNKNOWN_LABEL = "desconocido"
RAW_ROUTES_HINT = (
    'aviso: con protocol = "{protocol}" el despliegue sólo expone /ping y /invocations; '
    'tus rutas ({health} y {invoke}) no se alcanzan desde fuera. Usa protocol = "raw" '
    "en [runtime.app] para exponerlas."
)
PROJECT_FILE_HELP = "Fichero o directorio del proyecto (rayito-agent.toml o .json)."
PROJECT_FILE_DEFAULT = "."
JSON_LINE_SEPARATORS = (",", ":")
STREAM_HELP = "Imprime los eventos a medida que llegan (en --json, una línea JSON por evento)."


def camel_case(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part.capitalize() for part in tail)


def camel_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {camel_case(str(key)): camel_keys(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [camel_keys(item) for item in value]
    return value


def usage_document(result: AgentResult) -> dict[str, int]:
    usage = result.usage
    return {
        "input": usage.input,
        "output": usage.output,
        "reasoning": usage.reasoning,
        "cacheRead": usage.cache_read,
        "cacheWrite": usage.cache_write,
        "total": usage.total,
    }


def result_document(result: AgentResult) -> dict[str, Any]:
    """El resultado en `--json`; `output` (la salida de un grafo invocado con
    `--input`) va tal cual, sin pasar sus claves a camelCase, y sólo si la
    hay."""
    document: dict[str, Any] = {
        "sessionId": result.session_id,
        "text": result.text,
        "steps": result.steps,
        "usage": usage_document(result),
        "usageReported": result.usage_reported,
        "exitCode": result.exit_code,
    }
    output = result_output(result)
    if output is not None:
        document["output"] = dict(output)
    return document


def result_output(result: AgentResult) -> Mapping[str, Any] | None:
    """La salida estructurada del resultado; un resultado que no la tiene
    (el de un cliente anterior o el de un doble) cuenta como sin salida."""
    output: Mapping[str, Any] | None = getattr(result, "output", None)
    return output


def output_text(output: Mapping[str, Any]) -> str:
    """La salida estructurada como JSON legible."""
    return json.dumps(output, ensure_ascii=False, indent=2)


def print_result(result: AgentResult, *, as_json: bool) -> None:
    if as_json:
        emit_json(result_document(result))
        return
    output = result_output(result)
    if output is None:
        echo(result.text)
    else:
        if result.text:
            echo(result.text, err=True)
        echo(output_text(output))
    echo(f"session_id={result.session_id}", err=True)
    tokens = result.usage.total if result.usage_reported else USAGE_UNKNOWN_LABEL
    echo(f"steps={result.steps} tokens={tokens}", err=True)


class EventPrinter:
    """Escribe los eventos de un stream: en `--json`, un objeto por línea; en
    texto, los `TextDelta` tal cual (el `Text` entero sólo si no hubo deltas)
    y las herramientas en stderr. Un `AgentFailed` termina con salida 1."""

    def __init__(self, *, as_json: bool) -> None:
        self._as_json = as_json
        self._saw_delta = False
        self._wrote_text = False

    def __call__(self, event: AgentEvent) -> None:
        output = event.output if isinstance(event, Done) else None
        if self._as_json:
            document = camel_keys(dataclasses.asdict(event))
            if output is not None:
                document["output"] = dict(output)
            echo(json.dumps(document, ensure_ascii=False, separators=JSON_LINE_SEPARATORS))
        elif isinstance(event, TextDelta):
            self._saw_delta = True
            self._write(event.text)
        elif isinstance(event, Text) and not self._saw_delta:
            self._write(event.text)
        elif isinstance(event, ToolCall):
            echo(f"tool={event.name} status={event.status}", err=True)
        elif output is not None:
            self.finish()
            echo(output_text(output))
        if isinstance(event, AgentFailed):
            self.finish()
            fail(event.message)

    def finish(self) -> None:
        if self._wrote_text:
            self._wrote_text = False
            sys.stdout.write("\n")
            sys.stdout.flush()

    def _write(self, text: str) -> None:
        self._wrote_text = True
        shown = visible_controls(text) if sys.stdout.isatty() else text
        sys.stdout.write(shown)
        sys.stdout.flush()


def choice_of(allowed: tuple[str, ...], flag: str) -> Callable[[str], str]:
    """Callback de typer que acepta sólo `allowed`, como `--variant` de `image`."""

    def check(value: str) -> str:
        if value not in allowed:
            raise typer.BadParameter(f"admitidas: {', '.join(allowed)}", param_hint=flag)
        return value

    return check


def load_project(file: str) -> AgentProject:
    try:
        return AgentProject.load(file)
    except InvalidArgumentException as exc:
        fail(str(exc), code=EXIT_USAGE)


def _with_notes(exc: BaseException) -> str:
    """El mensaje de `exc` seguido de sus notas (`--keep-on-failure` deja en
    ellas qué sandbox sigue vivo y cómo conectarse)."""
    return "\n".join([str(exc), *getattr(exc, "__notes__", [])])


def run_failed(exc: AgentException) -> None:
    fail(str(exc))


@agent_app.command("init")
def init_command(
    ctx: typer.Context,
    directory: Annotated[
        Path, typer.Argument(help="Directorio del proyecto (se crea si no existe).")
    ] = Path(PROJECT_FILE_DEFAULT),
    runtime: Annotated[
        str,
        typer.Option(
            "--runtime",
            help="Runtime del agente.",
            callback=choice_of(SCAFFOLD_RUNTIMES, "--runtime"),
        ),
    ] = "strands",
    language: Annotated[
        str,
        typer.Option(
            "--language",
            help="Lenguaje del agente (typescript sólo con strands).",
            callback=choice_of(LANGUAGES, "--language"),
        ),
    ] = PYTHON_LANGUAGE,
    provider: Annotated[
        str,
        typer.Option(
            "--provider",
            help="Proveedor del modelo.",
            callback=choice_of(tuple(PROVIDER_PRESETS), "--provider"),
        ),
    ] = DEFAULT_PROVIDER,
    model: Annotated[
        str | None, typer.Option("--model", help="Id del modelo (obligatorio con openai).")
    ] = None,
    region: Annotated[
        str | None, typer.Option("--region", help="Región de Bedrock (por defecto, la de AWS).")
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Sobrescribe los ficheros que ya existan.")
    ] = False,
) -> None:
    """Escribe rayito-agent.toml y un andamiaje mínimo para el runtime.

    No llama a AWS. Sin --force no sobrescribe nada: si algún fichero existe,
    no escribe ninguno y sale con 2.
    """
    default_model = PROVIDER_PRESETS[provider][1]
    model_id = model or default_model
    if model_id is None:
        fail(f"--model es obligatorio con --provider {provider}", code=EXIT_USAGE)
    if language == TYPESCRIPT_LANGUAGE and runtime != "strands":
        fail("--language typescript sólo existe con --runtime strands", code=EXIT_USAGE)
    request = ScaffoldRequest(
        name=project_name_of(directory),
        runtime=runtime,
        language=language,
        provider=provider,
        model_id=model_id,
        region=resolve_region(region or ctx.meta.get("region")) or SCAFFOLD_FALLBACK_REGION,
    )
    try:
        written = write_scaffold(directory, scaffold_files(request), force=force)
    except ScaffoldConflictError as exc:
        fail(
            f"ya existe {', '.join(exc.paths)}; --force lo sobrescribe",
            code=EXIT_USAGE,
        )
    if json_mode(ctx):
        emit_json({"directory": str(directory), "files": written})
    else:
        for name in written:
            echo(f"escrito {name}")
        if runtime == "custom":
            echo(CUSTOM_INIT_HINT)
        echo(f"siguiente paso: rayito agent build --bucket <bucket> --file {directory}")


@agent_app.command("build")
def build_project_command(
    ctx: typer.Context,
    bucket: Annotated[str, typer.Option("--bucket", help="Bucket S3 del artefacto de build.")],
    file: Annotated[str, typer.Option("--file", help=PROJECT_FILE_HELP)] = PROJECT_FILE_DEFAULT,
    force: Annotated[
        bool, typer.Option("--force", help="Reconstruye aunque nada cambiara.")
    ] = False,
    timeout: Annotated[
        float, typer.Option("--timeout", help="Segundos de espera del build.")
    ] = DEFAULT_BUILD_TIMEOUT_SECONDS,
) -> None:
    """Construye la imagen del proyecto: el runtime y tu código, fijados por hash.

    Cada versión nueva cuesta almacenamiento de snapshot (mínimo una semana);
    las versiones se borran con `rayito image`.
    """
    project = load_project(file)
    clients = clients_of(ctx)
    as_json = json_mode(ctx)
    try:
        info = project.build(
            bucket=bucket,
            force=force,
            timeout=timeout,
            on_build_logs=None if as_json else echo,
            region=clients.region,
            session=clients.session,
        )
    except InvalidArgumentException as exc:
        fail(str(exc), code=EXIT_USAGE)
    except BuildException as exc:
        fail(f"build fallido: {exc}")
    if as_json:
        emit_json({"templateId": info.template_id, "buildId": info.build_id, "alias": info.alias})
    else:
        echo(f"template_id={info.template_id}")
        echo(f"build_id={info.build_id}")


@agent_app.command("run")
def run_project_command(
    ctx: typer.Context,
    prompt: Annotated[str, typer.Argument(help="Lo que se le pide al agente.")],
    file: Annotated[str, typer.Option("--file", help=PROJECT_FILE_HELP)] = PROJECT_FILE_DEFAULT,
    session_id: Annotated[
        str | None, typer.Option("--session-id", help="Continúa una sesión anterior.")
    ] = None,
    model: Annotated[
        str | None, typer.Option("--model", help="Otro id de modelo para esta ejecución.")
    ] = None,
    stream: Annotated[bool, typer.Option("--stream", help=STREAM_HELP)] = False,
) -> None:
    """Crea un sandbox del proyecto, ejecuta el agente una vez y lo destruye.

    Cuesta lo que dure la VM y los tokens del modelo.
    """
    project = load_project(file)
    clients = clients_of(ctx)
    as_json = json_mode(ctx)
    printer = EventPrinter(as_json=as_json) if stream else None
    try:
        result = project.run(
            prompt,
            session_id=session_id,
            model=model,
            on_event=printer,
            region=clients.region,
            session=clients.session,
        )
    except InvalidArgumentException as exc:
        fail(str(exc), code=EXIT_USAGE)
    except AgentException as exc:
        run_failed(exc)
    if printer is not None:
        printer.finish()
        return
    print_result(result, as_json=as_json)


#: `--sessions`: los modos de `ScalingOptions.sessions`.
SESSIONS_HELP = "Sesiones compartidas entre VMs (shared) o un MicroVM por sesión (dedicated)."
DETECT_FLAGS_HELP = "Fija a mano lo que la detección decidiría."
MODEL_ID_HELP = (
    "Id del modelo, si la detección no lo encuentra (tu código puede elegirlo por configuración)."
)
MODEL_PROVIDER_HELP = (
    "Pasarela del modelo (bedrock, openai, openai-compatible, litellm…), si la detección se "
    "equivoca."
)
MODEL_BASE_URL_HELP = (
    "URL https de una pasarela compatible con OpenAI (upstream de openai-compatible o litellm)."
)
GRAPH_HELP = "Nombre del grafo de langgraph.json que se despliega, si hay varios."
INPUT_HELP = "Objeto JSON de entrada para un grafo con esquema propio; excluye PROMPT."
INPUT_FILE_HELP = "Fichero con el objeto JSON de entrada (- lee stdin); excluye PROMPT y --input."
INVOKE_PROMPT_HELP = "Lo que se le pide al agente (o usa --input / --input-file)."
#: `--input-file -` lee el objeto de la entrada estándar.
STDIN_PATH = "-"
INVOKE_INPUT_REQUIRED = "pasa PROMPT, --input o --input-file, sólo uno de los tres"
INVOKE_INPUT_UNREADABLE = "no se pudo leer el fichero de --input-file"
INVOKE_INPUT_INVALID = "la entrada no es un objeto JSON"
SECRET_ENV_HELP = (
    "NOMBRE=<secreto>[#clave]: variable de entorno con el valor de un secreto de Secrets "
    "Manager (nombre completo o ARN); repetible."
)
SECRET_ENV_FROM_HELP = (
    "<secreto>[,prefix=P][,keys=A|B]: cada clave de un secreto JSON como variable de entorno; "
    "repetible."
)
CREATE_SECRETS_FROM_HELP = (
    "Crea (o actualiza) un secreto JSON con los valores de este .env y lo usa como "
    "--secret-env-from (cuesta $0,40/mes; pide --yes)."
)
CHECK_ENV_HELP = "Avisa de las variables de .env.example que no reciben valor."
MICROVM_ID_PREFIX = "microvm-"
STEP_MESSAGES = {
    "base_image": "imagen base publicada",
    "secret_created": (
        "secreto creado para la pasarela {detail} (cuesta $0,40 al mes hasta que lo borres)"
    ),
    "image": "imagen lista: versión {detail}",
    "fleet": "{detail} VM(s) listos",
    "secret_env_created": "secreto de --create-secrets-from: {detail}",
    "env_missing": "aviso: {detail}",
}


def optional_choice_of(allowed: tuple[str, ...], flag: str) -> Callable[[str | None], str | None]:
    """Como `choice_of`, para una bandera que puede faltar."""
    check = choice_of(allowed, flag)

    def verify(value: str | None) -> str | None:
        return None if value is None else check(value)

    return verify


def detect_overrides_of(
    framework: str | None,
    language: str | None,
    entry: str | None,
    protocol: str | None,
    model_id: str | None = None,
    model_provider: str | None = None,
    model_base_url: str | None = None,
    graph: str | None = None,
) -> DetectOverrides | None:
    chosen = (framework, language, entry, protocol, model_id, model_provider, model_base_url, graph)
    if all(value is None for value in chosen):
        return None
    return DetectOverrides(
        framework=framework,
        language=language,
        entrypoint=entry,
        protocol=protocol,
        model_id=model_id,
        model_provider=model_provider,
        model_base_url=model_base_url,
        graph=graph,
    )


def step_printer(deploy_json: bool) -> Callable[[AgentDeployStep], None]:
    """Un paso de `deploy_agent()` por línea en stderr (stdout queda para el
    resultado). El bucket se nombra porque quien despliega tiene que saber
    dónde fue el artefacto; nunca se nombra un secreto."""

    def show(step: AgentDeployStep) -> None:
        if step.kind == "bucket":
            origin, _, bucket = step.detail.partition(":")
            echo(f"bucket del artefacto: {bucket} ({origin})", err=True)
        elif step.kind == "warm":
            ready = step.detail == "ready"
            echo(
                "plazas calientes aparcadas"
                if ready
                else "las plazas calientes siguen creándose; el despliegue ya sirve",
                err=True,
            )
        else:
            echo(STEP_MESSAGES[step.kind].format(detail=step.detail), err=True)

    return show


@agent_app.command("detect")
def detect_command(
    ctx: typer.Context,
    path: Annotated[
        Path, typer.Argument(help="Directorio del proyecto (por defecto, el actual).")
    ] = Path(PROJECT_FILE_DEFAULT),
    framework: Annotated[str | None, typer.Option("--framework", help=DETECT_FLAGS_HELP)] = None,
    language: Annotated[str | None, typer.Option("--language", help=DETECT_FLAGS_HELP)] = None,
    entry: Annotated[str | None, typer.Option("--entry", help=DETECT_FLAGS_HELP)] = None,
    protocol: Annotated[str | None, typer.Option("--protocol", help=DETECT_FLAGS_HELP)] = None,
    model_id: Annotated[str | None, typer.Option("--model-id", help=MODEL_ID_HELP)] = None,
    model_provider: Annotated[
        str | None, typer.Option("--model-provider", help=MODEL_PROVIDER_HELP)
    ] = None,
    model_base_url: Annotated[
        str | None, typer.Option("--model-base-url", help=MODEL_BASE_URL_HELP)
    ] = None,
    graph: Annotated[str | None, typer.Option("--graph", help=GRAPH_HELP)] = None,
    write: Annotated[
        bool,
        typer.Option("--write", help="Guarda el proyecto resuelto como rayito-agent.json."),
    ] = False,
) -> None:
    """Detecta el proyecto de un agente sin ejecutar su código y lo describe.

    Lee el fichero de proyecto (si lo hay) y los manifiestos del directorio;
    lo que detecta se puede fijar con --framework, --language, --entry y
    --protocol. No llama a AWS. Con --write guarda el resultado como
    rayito-agent.json (no sobrescribe un fichero de proyecto existente).
    """
    overrides = detect_overrides_of(
        framework, language, entry, protocol, model_id, model_provider, model_base_url, graph=graph
    )
    try:
        project, report = project_for(path, None, overrides, ctx.meta.get("region"))
    except InvalidArgumentException as exc:
        fail(str(exc), code=EXIT_USAGE)
    written = write_detected_project(path, project) if write else None
    if json_mode(ctx):
        emit_json(
            {
                "detection": None if report is None else report.to_dict(),
                "project": camel_keys(project.to_dict()),
                "written": written,
            }
        )
        return
    for line in detection_lines(report) or [f"proyecto: {project.name} (fichero de proyecto)"]:
        echo(line)
    if written is not None:
        echo(f"escrito {written}")


def write_detected_project(directory: Path, project: AgentProject) -> str:
    """Escribe `rayito-agent.json` en `directory`; un fichero de proyecto que ya
    exista no se toca."""
    from rayito._limits import AGENT_PROJECT_FILE_NAMES

    for name in AGENT_PROJECT_FILE_NAMES:
        if (directory / name).exists():
            fail(f"ya existe {name}; bórralo o edítalo a mano", code=EXIT_USAGE)
    target = directory / AGENT_PROJECT_FILE_NAMES[-1]
    target.write_text(json.dumps(project.to_dict(), indent=2, ensure_ascii=False) + "\n")
    return target.name


@agent_app.command("deploy")
def deploy_command(
    ctx: typer.Context,
    path: Annotated[
        Path | None, typer.Argument(help="Directorio del proyecto (por defecto, el actual).")
    ] = None,
    file: Annotated[str, typer.Option("--file", help=PROJECT_FILE_HELP)] = PROJECT_FILE_DEFAULT,
    timeout: Annotated[
        float | None,
        typer.Option(
            "--timeout", help="Segundos de vida del despliegue (por defecto, el del fichero)."
        ),
    ] = None,
    port: Annotated[
        int | None,
        typer.Option("--port", help="Puerto del servicio (por defecto, el del fichero)."),
    ] = None,
    token_file: TokenFileOption = None,
    keep_on_failure: Annotated[
        bool,
        typer.Option(
            "--keep-on-failure",
            help="Si el arranque falla, deja el sandbox vivo para depurarlo (cuesta hasta "
            "que lo termines con `rayito agent stop`).",
        ),
    ] = False,
    framework: Annotated[str | None, typer.Option("--framework", help=DETECT_FLAGS_HELP)] = None,
    language: Annotated[str | None, typer.Option("--language", help=DETECT_FLAGS_HELP)] = None,
    entry: Annotated[str | None, typer.Option("--entry", help=DETECT_FLAGS_HELP)] = None,
    protocol: Annotated[str | None, typer.Option("--protocol", help=DETECT_FLAGS_HELP)] = None,
    model_id: Annotated[str | None, typer.Option("--model-id", help=MODEL_ID_HELP)] = None,
    model_provider: Annotated[
        str | None, typer.Option("--model-provider", help=MODEL_PROVIDER_HELP)
    ] = None,
    model_base_url: Annotated[
        str | None, typer.Option("--model-base-url", help=MODEL_BASE_URL_HELP)
    ] = None,
    graph: Annotated[str | None, typer.Option("--graph", help=GRAPH_HELP)] = None,
    memory: Annotated[
        int | None,
        typer.Option(
            "--memory",
            help="MiB de cada VM. Se fija en la imagen: cambiarla construye una versión nueva.",
        ),
    ] = None,
    max_sessions: Annotated[
        int | None, typer.Option("--max-sessions", help="Sesiones simultáneas por VM.")
    ] = None,
    sessions: Annotated[
        str | None,
        typer.Option(
            "--sessions",
            help=SESSIONS_HELP,
            callback=optional_choice_of(SESSION_MODES, "--sessions"),
        ),
    ] = None,
    min_vms: Annotated[int | None, typer.Option("--min-vms", help="VMs siempre vivos.")] = None,
    max_vms: Annotated[int | None, typer.Option("--max-vms", help="VMs como máximo.")] = None,
    warm: Annotated[
        int | None, typer.Option("--warm", help="Plazas calientes (VMs aparcados).")
    ] = None,
    idle: Annotated[
        int | None,
        typer.Option("--idle", help="Segundos sin uso tras los que un VM se suspende."),
    ] = None,
    queue: Annotated[
        int | None, typer.Option("--queue", help="Peticiones que esperan en la cola del host.")
    ] = None,
    queue_timeout: Annotated[
        float | None,
        typer.Option("--queue-timeout", help="Segundos máximos de espera en la cola."),
    ] = None,
    env: Annotated[
        list[str] | None, typer.Option("--env", help="CLAVE=VALOR no secreto; repetible.")
    ] = None,
    allow_out: Annotated[
        list[str] | None,
        typer.Option("--allow-out", help="Destino de salida permitido; repetible."),
    ] = None,
    bucket: Annotated[
        str | None,
        typer.Option(
            "--bucket",
            help=(
                "Bucket del artefacto (por defecto, RAYITO_BUCKET, el de CDK o el de rayito setup)."
            ),
        ),
    ] = None,
    secret: Annotated[
        list[str] | None,
        typer.Option("--secret", help="proveedor=nombre de un secreto que ya existe; repetible."),
    ] = None,
    create_secret: Annotated[
        bool,
        typer.Option(
            "--create-secret",
            help="Crea el secreto que falte desde la variable del proveedor (cuesta $0,40/mes).",
        ),
    ] = False,
    secret_env: Annotated[list[str], typer.Option("--secret-env", help=SECRET_ENV_HELP)] = [],  # noqa: B006
    secret_env_from: Annotated[
        list[str], typer.Option("--secret-env-from", help=SECRET_ENV_FROM_HELP)
    ] = [],  # noqa: B006
    create_secrets_from: Annotated[
        Path | None, typer.Option("--create-secrets-from", help=CREATE_SECRETS_FROM_HELP)
    ] = None,
    check_env: Annotated[bool, typer.Option("--check-env", help=CHECK_ENV_HELP)] = False,
    base_artifact: Annotated[
        Path | None,
        typer.Option(
            "--base-artifact", help="Zip de la imagen base: se publica si no existe (pide --yes)."
        ),
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", help="Confirma lo que cuesta sin preguntar.")
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Sólo detecta y calcula el plan y el coste, sin AWS."),
    ] = False,
    build: Annotated[
        bool | None,
        typer.Option(
            "--build/--no-build",
            help="Construye la imagen (por defecto, si hay bucket) o reutiliza la existente.",
        ),
    ] = None,
    verbose: Annotated[
        bool, typer.Option("--verbose", help="Imprime todos los logs del build.")
    ] = False,
    target: Annotated[
        str,
        typer.Option("--target", help=TARGET_HELP, callback=DEPLOY_TARGET_CHECK),
    ] = TARGET_MICROVM,
    tag: Annotated[list[str], typer.Option("--tag", help=TAG_HELP)] = [],  # noqa: B006
    bedrock_model: Annotated[
        list[str], typer.Option("--bedrock-model", help=BEDROCK_MODEL_HELP)
    ] = [],  # noqa: B006
    readable_secret: Annotated[
        list[str], typer.Option("--readable-secret", help=READABLE_SECRET_HELP)
    ] = [],  # noqa: B006
    idle_session_timeout: Annotated[
        int | None, typer.Option("--idle-session-timeout", help=IDLE_SESSION_TIMEOUT_HELP)
    ] = None,
    max_session_lifetime: Annotated[
        int | None, typer.Option("--max-session-lifetime", help=MAX_SESSION_LIFETIME_HELP)
    ] = None,
    external_model: Annotated[
        bool, typer.Option("--external-model", help=EXTERNAL_MODEL_HELP)
    ] = False,
) -> None:
    """Despliega el agente de un directorio como una flota de MicroVMs.

    Detecta el lenguaje, el framework y la entrada de tu proyecto sin ejecutar
    su código (o usa su rayito-agent.toml), construye la imagen sólo si
    cambió, resuelve la región, la imagen base, el secreto del modelo y el
    bucket, y deja `--min-vms` VMs vivos y `--warm` plazas aparcadas. Cada
    valor sale de la bandera, del fichero, del entorno o del descubrimiento,
    en ese orden; nada se crea en silencio (ni buckets, ni secretos sin
    --create-secret, ni la imagen base sin --base-artifact y --yes).

    Imprime el nombre de la flota, el sandbox y la URL; las cabeceras de
    acceso no se imprimen nunca. El access token se guarda en --token-file (o
    es el de RAYITO_ACCESS_TOKEN; si no hay ninguno, en .rayito/agent-token con
    modo 0600, que se reutiliza al volver a desplegar). Vive como mucho 8 h:
    `rayito agent stop <nombre>` lo termina. Con --dry-run no llama a AWS.

    Con --target agentcore el agente va a Amazon Bedrock AgentCore Runtime sin
    Docker local: imprime el plan (región, recursos, seguridad y coste), pide
    confirmación (--yes la salta) y crea la pila rayito-agentcore-<nombre>, la
    imagen arm64 (CodeBuild, sólo si cambió) y el runtime. Sin pasarela de
    secretos: el agente lee las credenciales del rol del runtime.
    """
    with agentcore_errors(json_output=json_mode(ctx)):
        require_agentcore_target(
            target,
            {
                TAG_FLAG: tag,
                BEDROCK_MODEL_FLAG: bedrock_model,
                READABLE_SECRET_FLAG: readable_secret,
                IDLE_SESSION_TIMEOUT_FLAG: idle_session_timeout,
                MAX_SESSION_LIFETIME_FLAG: max_session_lifetime,
                EXTERNAL_MODEL_FLAG: external_model or None,
            },
        )
    if target == TARGET_AGENTCORE:
        run_agentcore_deploy(
            ctx,
            AgentCoreDeployRequest(
                project_path=deploy_target(path, file),
                overrides=detect_overrides_of(
                    framework,
                    language,
                    entry,
                    protocol,
                    model_id,
                    model_provider,
                    model_base_url,
                    graph=graph,
                ),
                max_sessions=max_sessions,
                queue=queue,
                queue_timeout=queue_timeout,
                env=env,
                allow_out=allow_out,
                bucket=bucket,
                build=build,
                verbose=verbose,
                yes=yes,
                dry_run=dry_run,
                tags=tag,
                bedrock_models=bedrock_model,
                readable_secrets=readable_secret,
                idle_session_timeout=idle_session_timeout,
                max_session_lifetime=max_session_lifetime,
                secret_env=secret_env,
                secret_env_from=secret_env_from,
                create_secrets_from=create_secrets_from,
                check_env=check_env,
                microvm_flags=frozenset(
                    flag
                    for flag, passed in {
                        "--min-vms": min_vms is not None,
                        "--max-vms": max_vms is not None,
                        "--warm": warm is not None,
                        "--memory": memory is not None,
                        "--port": port is not None,
                        "--timeout": timeout is not None,
                        "--idle": idle is not None,
                        "--secret": bool(secret),
                        "--create-secret": create_secret,
                        "--token-file": token_file is not None,
                        "--keep-on-failure": keep_on_failure,
                        "--base-artifact": base_artifact is not None,
                    }.items()
                    if passed
                ),
                external_model=external_model,
            ),
        )
        return
    project_path = deploy_target(path, file)
    overrides = detect_overrides_of(
        framework, language, entry, protocol, model_id, model_provider, model_base_url, graph=graph
    )
    try:
        project, report = project_for(project_path, None, overrides, ctx.meta.get("region"))
        project = apply_serve_flags(
            project,
            max_sessions=max_sessions,
            idle_seconds=idle,
            queue=queue,
            queue_timeout=queue_timeout,
            timeout=timeout,
            port=port,
        )
        project = apply_project_flags(
            project,
            memory=memory,
            env=parse_pairs(env, "--env"),
            allow_out=allow_out or [],
            secret_env=parse_secret_env_flags(secret_env),
            secret_env_from=parse_secret_env_from_flags(secret_env_from),
        )
        scaling = scaling_of(project, min_vms, max_vms, warm, sessions)
        secret_names = parse_pairs(secret, "--secret")
        env_file_secret = env_file_secret_of(project, create_secrets_from)
    except InvalidArgumentException as exc:
        fail(str(exc), code=EXIT_USAGE)
    as_json = json_mode(ctx)
    compact = None if verbose else CompactBuildLog(lambda line: echo(line, err=True))

    def options_with(
        token: str | None, publisher: Callable[[], None] | None, pool_backend: Any = None
    ) -> AgentDeployOptions:
        return AgentDeployOptions(
            bucket=bucket,
            secret_names=secret_names,
            create_secret_from_env=create_secret,
            base_publisher=publisher,
            access_token=token,
            keep_on_failure=keep_on_failure,
            build_image=build,
            scaling=scaling,
            pool_backend=pool_backend,
            on_build_logs=(lambda line: echo(line, err=True)) if verbose else compact,
            on_step=step_printer(as_json),
            create_secrets_from=None if create_secrets_from is None else str(create_secrets_from),
            check_env=check_env,
        )

    if dry_run:
        print_plan(
            plan_deploy(
                project=project,
                options=options_with(None, None),
                region=ctx.meta.get("region"),
            ),
            report,
            as_json=as_json,
            notices=dry_run_notices(project, create_secrets_from, check_env=check_env),
        )
        return
    if env_file_secret is not None:
        echo(env_file_secret_line(env_file_secret), err=True)
        if not confirmed(yes=yes, json_output=as_json):
            fail(CREATE_SECRETS_NEEDS_CONFIRMATION, code=EXIT_USAGE)
    try:
        token, written_to = deploy_token(token_file)
    except TokenFileError as exc:
        fail(str(exc), code=EXIT_USAGE)
    # El fichero del token se escribe antes del build: si queda dentro del
    # código del agente, se excluye para que no acabe en la imagen ni cambie
    # su hash (que un nuevo despliegue la reutilice).
    project = with_excluded_file(project, written_to)
    clients = clients_of(ctx)
    publisher = base_publisher_of(clients, base_artifact, bucket, yes=yes)
    for line in detection_lines(report):
        echo(line, err=True)
    try:
        fleet = deploy_agent(
            project=project,
            options=options_with(
                token,
                publisher,
                pool_backend_for(fleet_settings_of(project, scaling).scaling.warm_vms),
            ),
            region=clients.region,
            session=clients.session,
        )
    except InvalidArgumentException as exc:
        fail(_with_notes(exc), code=EXIT_USAGE)
    except BuildException as exc:
        fail(f"build fallido: {exc}")
    except SandboxException as exc:
        fail(_with_notes(exc))
    except Exception as exc:
        for note in getattr(exc, "__notes__", []):
            echo(note, err=True)
        raise
    finally:
        if compact is not None:
            compact.finish()
    fleet.detach()
    hidden = app_hiding_own_routes(project)
    if hidden is not None:
        echo(
            RAW_ROUTES_HINT.format(
                protocol=hidden.protocol, health=hidden.health_path, invoke=hidden.invoke_path
            ),
            err=True,
        )
    print_deployed(fleet, project.serve.port, written_to, as_json=as_json)


def deploy_target(path: Path | None, file: str) -> Path:
    """El directorio o fichero del proyecto: el argumento o `--file` (el
    nombre de 0.13); los dos a la vez, con valores distintos, son ambiguos."""
    if path is None:
        return Path(file)
    if file != PROJECT_FILE_DEFAULT and Path(file) != path:
        fail("pasa la ruta del proyecto como argumento o con --file, no las dos", code=EXIT_USAGE)
    return path


def scaling_of(
    project: AgentProject,
    min_vms: int | None,
    max_vms: int | None,
    warm: int | None,
    sessions: str | None,
) -> ScalingOptions | None:
    """El `ScalingOptions` de las banderas sobre el del proyecto; `None` si
    no se pasó ninguna (el proyecto decide)."""
    if all(value is None for value in (min_vms, max_vms, warm, sessions)):
        return None
    current = fleet_settings_of(project, None).scaling
    return ScalingOptions(
        min_vms=current.min_vms if min_vms is None else min_vms,
        max_vms=current.max_vms if max_vms is None else max_vms,
        warm_vms=current.warm_vms if warm is None else warm,
        sessions=current.sessions if sessions is None else sessions,  # type: ignore[arg-type]
        scale_in_after_seconds=current.scale_in_after_seconds,
    )


def dry_run_notices(
    project: AgentProject, create_secrets_from: Path | None, *, check_env: bool
) -> list[str]:
    """Los avisos `env_missing` del plan, con el secreto de
    `--create-secrets-from` ya conectado (sin crearlo)."""
    path = None if create_secrets_from is None else str(create_secrets_from)
    prepared = prepare_secret_env(project, path, check_env=check_env)
    return [env_warning_line(warning) for warning in prepared.warnings]


def print_plan(
    plan: Any, report: DetectionReport | None, *, as_json: bool, notices: Sequence[str] = ()
) -> None:
    if as_json:
        emit_json(
            {
                "dryRun": True,
                "detection": None if report is None else report.to_dict(),
                "plan": camel_keys(dataclasses.asdict(plan) | {"detection": None}),
            }
        )
        return
    for line in [*detection_lines(report), *notices, *describe_plan(plan)]:
        echo(line)
    echo("no se llamó a AWS (--dry-run)")


def print_deployed(fleet: AgentFleet, port: int, token_file: Path | None, *, as_json: bool) -> None:
    """El resultado de un despliegue: la flota, el primer VM con las claves de
    0.13 (`sandbox_id`, `url`, `port`) y dónde quedó el token. Nunca las
    cabeceras ni el token."""
    first = fleet.deployments[0]
    ids = list(fleet.sandbox_ids)
    if as_json:
        emit_json(
            {
                "name": fleet.name,
                "sandboxId": first.sandbox_id,
                "sandboxIds": ids,
                "url": first.url,
                "port": port,
                "tokenFile": None if token_file is None else str(token_file),
            }
        )
        return
    echo(f"name={fleet.name}")
    echo(f"sandbox_id={first.sandbox_id}")
    echo(f"url={first.url}")
    echo(f"port={port}")
    echo(f"members={len(ids)}")
    if token_file is not None:
        echo(f"token_file={token_file}")


def base_publisher_of(
    clients: Any, artifact: Path | None, bucket: str | None, *, yes: bool
) -> Callable[[], None] | None:
    """Quién publica la imagen base si falta: sólo con `--base-artifact`, y
    sólo tras confirmar el coste (`--yes`, o una respuesta en una terminal)."""
    if artifact is None:
        return None

    def publish_base() -> None:
        from rayito.cli.image import PublishRequest, resolve_bucket, run_publish

        if not yes and not (sys.stdin.isatty() and typer.confirm(BASE_COST_QUESTION)):
            fail(BASE_NEEDS_CONFIRMATION, code=EXIT_USAGE)
        code = run_publish(
            clients,
            PublishRequest(
                artifact=artifact,
                base_image_version="latest",
                bucket=resolve_bucket(bucket),
                image_name=DEFAULT_AGENT_TEMPLATE_BASE,
                os_capabilities="ALL",
            ),
            json_output=False,
        )
        if code:
            fail("la publicación de la imagen base falló")

    return publish_base


BASE_COST_QUESTION = (
    "La imagen base no existe: publicarla cuesta un build y almacenamiento de snapshot "
    "(mínimo una semana). ¿Publicarla?"
)
BASE_NEEDS_CONFIRMATION = (
    "publicar la imagen base cuesta dinero: confírmalo con --yes (o desde una terminal)"
)


@agent_app.command("export")
def export_command(
    ctx: typer.Context,
    out: Annotated[Path, typer.Option("--out", help="Directorio de salida.")],
    target: Annotated[
        str,
        typer.Option(
            "--target",
            help="docker o agentcore (contexto de build del contenedor).",
            callback=choice_of(EXPORT_TARGETS, "--target"),
        ),
    ],
    file: Annotated[str, typer.Option("--file", help=PROJECT_FILE_HELP)] = PROJECT_FILE_DEFAULT,
    overwrite: Annotated[
        bool, typer.Option("--overwrite", help="Escribe aunque el directorio no esté vacío.")
    ] = False,
) -> None:
    """Escribe un contexto de build de contenedor con el agente. No llama a AWS.

    En el contenedor la credencial del modelo la pone la plataforma: la
    garantía de la pasarela de secretos no aplica fuera de Rayito.
    """
    project = load_project(file)
    try:
        exported = project.export(target=target, out_dir=str(out), overwrite=overwrite)
    except InvalidArgumentException as exc:
        fail(str(exc), code=EXIT_USAGE)
    if json_mode(ctx):
        emit_json(
            {
                "path": str(exported.path),
                "files": list(exported.files),
                "port": exported.port,
                "platform": exported.platform,
            }
        )
    else:
        echo(f"path={exported.path}")
        echo(f"files={len(exported.files)} port={exported.port} platform={exported.platform}")


def connect_deployment(
    ctx: typer.Context, sandbox_id: str, token_file: Path | None
) -> AgentDeployment:
    clients = clients_of(ctx)
    return AgentDeployment.connect(
        sandbox_id,
        access_token=token_or_exit(token_file),
        region=clients.region,
        session=clients.session,
    )


def is_fleet_name(target: str) -> bool:
    """Un id de sandbox empieza por `microvm-`; cualquier otra cosa es el
    nombre de una flota (el de su imagen)."""
    return not target.startswith(MICROVM_ID_PREFIX)


def fleet_token(token_file: Path | None) -> str:
    """El token de una flota: `--token-file`, `RAYITO_ACCESS_TOKEN` o el
    fichero por defecto de `deploy`."""
    if (
        token_file is None
        and not os.environ.get(TOKEN_ENV_VAR)
        and AGENT_TOKEN_FILE_DEFAULT.is_file()
    ):
        return token_or_exit(AGENT_TOKEN_FILE_DEFAULT)
    return token_or_exit(token_file)


def connect_fleet(ctx: typer.Context, name: str, token_file: Path | None) -> AgentFleet:
    clients = clients_of(ctx)
    return AgentFleet.connect(
        name,
        access_token=fleet_token(token_file),
        region=clients.region,
        session=clients.session,
    )


@agent_app.command("invoke")
def invoke_command(
    ctx: typer.Context,
    sandbox_id: Annotated[
        str, typer.Argument(help="Id del sandbox desplegado o nombre de la flota.")
    ],
    prompt: Annotated[str | None, typer.Argument(help=INVOKE_PROMPT_HELP)] = None,
    session_id: Annotated[
        str | None, typer.Option("--session-id", help="Continúa una sesión anterior.")
    ] = None,
    input_json: Annotated[str | None, typer.Option("--input", help=INPUT_HELP)] = None,
    input_file: Annotated[Path | None, typer.Option("--input-file", help=INPUT_FILE_HELP)] = None,
    stream: Annotated[bool, typer.Option("--stream", help=STREAM_HELP)] = False,
    token_file: TokenFileOption = None,
    target: Annotated[
        str, typer.Option("--target", help=TARGET_HELP, callback=DEPLOY_TARGET_CHECK)
    ] = TARGET_MICROVM,
) -> None:
    """Llama a un agente desplegado con `rayito agent deploy`.

    Con el id de un sandbox habla con ese VM; con el nombre de la flota
    reparte la sesión entre sus VMs (el `session_id` que devuelve identifica
    el VM: pásalo con --session-id para seguir la conversación). Con --target
    agentcore, `sandbox_id` es el nombre del agente: se invoca con SigV4 y el
    `session_id` que devuelve continúa la conversación. Un grafo con esquema
    de entrada propio se llama con --input (un objeto JSON) o --input-file en
    lugar de PROMPT, y su salida se imprime como JSON."""
    as_json = json_mode(ctx)
    data = invoke_input(prompt, input_json, input_file)
    with agentcore_errors(json_output=as_json):
        fleet: AgentDeployment | AgentFleet | AgentCoreFleet
        if target == TARGET_AGENTCORE:
            reject_token_file(token_file)
            fleet = connect_agentcore_fleet(ctx, sandbox_id)
        else:
            fleet = (
                connect_fleet(ctx, sandbox_id, token_file)
                if is_fleet_name(sandbox_id)
                else connect_deployment(ctx, sandbox_id, token_file)
            )
        try:
            if stream:
                printer = EventPrinter(as_json=as_json)
                with fleet.stream(prompt, **invocation_options(session_id, data)) as events:
                    for event in events:
                        printer(event)
                printer.finish()
                return
            result = fleet.invoke(prompt, **invocation_options(session_id, data))
        except AgentException as exc:
            run_failed(exc)
        except InvalidArgumentException as exc:
            fail(str(exc), code=EXIT_USAGE)
        except (CapacityException, QuotaExceededException, SandboxException) as exc:
            fail(str(exc))
    print_result(result, as_json=as_json)


def invocation_options(session_id: str | None, data: Mapping[str, Any] | None) -> dict[str, Any]:
    """Los argumentos con nombre de `invoke()`/`stream()`: `input` sólo si la
    orden lo trae, así que una llamada con PROMPT es la de siempre."""
    options: dict[str, Any] = {"session_id": session_id}
    if data is not None:
        options["input"] = data
    return options


def invoke_input(
    prompt: str | None, input_json: str | None, input_file: Path | None
) -> Mapping[str, Any] | None:
    """El objeto de `--input` o de `--input-file` (`-` lee stdin), o `None`
    con PROMPT. Exactamente uno de los tres; si no, o si la entrada no es un
    objeto JSON, sale con el código de uso antes de llamar a nadie. El error
    nunca lleva el contenido."""
    if sum(value is not None for value in (prompt, input_json, input_file)) != 1:
        fail(INVOKE_INPUT_REQUIRED, code=EXIT_USAGE)
    if input_file is not None:
        input_json = read_input_file(input_file)
    if input_json is None:
        return None
    try:
        data = json.loads(input_json)
    except ValueError:
        fail(INVOKE_INPUT_INVALID, code=EXIT_USAGE)
    if not isinstance(data, dict):
        fail(INVOKE_INPUT_INVALID, code=EXIT_USAGE)
    return data


def read_input_file(path: Path) -> str:
    if str(path) == STDIN_PATH:
        return sys.stdin.read()
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        fail(INVOKE_INPUT_UNREADABLE, code=EXIT_USAGE)


@agent_app.command("status")
def status_command(
    ctx: typer.Context,
    sandbox_id: Annotated[
        str, typer.Argument(help="Id del sandbox desplegado o nombre de la flota.")
    ],
    token_file: TokenFileOption = None,
    target: Annotated[
        str, typer.Option("--target", help=TARGET_HELP, callback=DEPLOY_TARGET_CHECK)
    ] = TARGET_MICROVM,
) -> None:
    """Estado de un agente desplegado: si está listo y si está ocupado. Con el
    nombre de una flota, el de cada uno de sus VMs (uno que este proceso no
    ha abierto figura como `unknown`: preguntarle lo reanudaría). Con --target
    agentcore, `sandbox_id` es el nombre del agente y se lee su runtime."""
    if target == TARGET_AGENTCORE:
        show_status(ctx, sandbox_id, token_file)
        return
    if is_fleet_name(sandbox_id):
        print_fleet_status(connect_fleet(ctx, sandbox_id, token_file), as_json=json_mode(ctx))
        return
    status = connect_deployment(ctx, sandbox_id, token_file).status()
    document = {
        "sandboxId": sandbox_id,
        "state": status.state,
        "ready": status.ready,
        "busy": status.busy,
        "port": status.port,
        "project": status.project,
    }
    if json_mode(ctx):
        emit_json(document)
    else:
        for key, value in document.items():
            echo(f"{key}: {value}")


def print_fleet_status(fleet: AgentFleet, *, as_json: bool) -> None:
    status = fleet.status()
    if as_json:
        emit_json(camel_keys(dataclasses.asdict(status)))
        return
    echo(f"name: {status.name}")
    echo(f"members: {len(status.members)} (min {status.min_vms}, max {status.max_vms})")
    for member in status.members:
        echo(f"sandbox_id={member.sandbox_id} state={member.state}")


@agent_app.command("stop")
def stop_command(
    ctx: typer.Context,
    sandbox_id: Annotated[
        str, typer.Argument(help="Id del sandbox desplegado o nombre de la flota.")
    ],
    target: Annotated[
        str, typer.Option("--target", help=TARGET_HELP, callback=DEPLOY_TARGET_CHECK)
    ] = TARGET_MICROVM,
    session_id: Annotated[
        str | None, typer.Option("--session-id", help=STOP_SESSION_ID_HELP)
    ] = None,
) -> None:
    """Para un agente desplegado: destruye su sandbox, o todos los de la flota
    si le pasas su nombre. No necesita el access token: `terminate-microvm`
    basta. Con --target agentcore para la microVM de una sesión (--session-id,
    obligatorio): AgentCore no cobra sin sesiones; para borrar el agente usa
    `rayito agent delete`."""
    with agentcore_errors(json_output=json_mode(ctx)):
        require_agentcore_target(target, {SESSION_ID_FLAG: session_id})
    if target == TARGET_AGENTCORE:
        stop_session(ctx, sandbox_id, session_id)
        return
    clients = clients_of(ctx)
    if is_fleet_name(sandbox_id):
        fleet = AgentFleet.connect(sandbox_id, region=clients.region, session=clients.session)
        count = len(fleet.sandbox_ids)
        fleet.stop()
        if json_mode(ctx):
            emit_json({"name": sandbox_id, "stopped": count})
        else:
            echo(f"parados {count} VM(s) de la flota name={sandbox_id}")
        return
    stopped = kill_one(clients, sandbox_id)
    if json_mode(ctx):
        emit_json({"sandboxId": sandbox_id, "stopped": stopped})
    else:
        echo(f"{'parado' if stopped else 'ya no existe'} sandbox_id={sandbox_id}")


@agent_app.command("delete", help=DELETE_HELP)
def delete_command(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help=DELETE_NAME_HELP)],
    target: Annotated[
        str, typer.Option("--target", help=DELETE_TARGET_HELP, callback=DEPLOY_TARGET_CHECK)
    ],
    yes: Annotated[bool, typer.Option("--yes", help=DELETE_YES_HELP)] = False,
) -> None:
    delete_command_body(ctx, name, target, yes=yes)


@agent_app.command("list", help=LIST_HELP)
def list_command(
    ctx: typer.Context,
    target: Annotated[
        str, typer.Option("--target", help=LIST_TARGET_HELP, callback=DEPLOY_TARGET_CHECK)
    ],
) -> None:
    list_command_body(ctx, target)
