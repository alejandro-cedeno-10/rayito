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
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer

from rayito import AgentDeployment, AgentProject, AgentResult
from rayito._agent._events import AgentEvent, AgentFailed, Text, TextDelta, ToolCall
from rayito._agent._template import (
    AGENT_TEMPLATE_RUNTIMES,
    DEFAULT_AGENT_TEMPLATE_BASE,
    DEFAULT_AGENT_TEMPLATE_NAME,
    AgentTemplate,
)
from rayito._aws_region import resolve_region
from rayito._images import DEFAULT_BUILD_TIMEOUT_SECONDS
from rayito._limits import AGENT_MIN_MEMORY_MIB
from rayito._payload import generate_access_token
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
    project_name_of,
    scaffold_files,
    write_scaffold,
)
from rayito.cli._console import EXIT_USAGE, echo, emit_json, fail, visible_controls
from rayito.cli._session import clients_of, json_mode
from rayito.cli._tokens import TOKEN_ENV_VAR, TokenFileError, write_token_file
from rayito.cli.sandbox import TokenFileOption, kill_one, token_or_exit
from rayito.exceptions import AgentException, BuildException, InvalidArgumentException

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


DEPLOY_NEEDS_TOKEN_FILE_MESSAGE = (
    f"sin {TOKEN_ENV_VAR}, deploy necesita --token-file para poder volver a conectarte "
    "(invoke y status)"
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
    return {
        "sessionId": result.session_id,
        "text": result.text,
        "steps": result.steps,
        "usage": usage_document(result),
        "exitCode": result.exit_code,
    }


def print_result(result: AgentResult, *, as_json: bool) -> None:
    if as_json:
        emit_json(result_document(result))
        return
    echo(result.text)
    echo(f"session_id={result.session_id}", err=True)
    echo(f"steps={result.steps} tokens={result.usage.total}", err=True)


class EventPrinter:
    """Escribe los eventos de un stream: en `--json`, un objeto por línea; en
    texto, los `TextDelta` tal cual (el `Text` entero sólo si no hubo deltas)
    y las herramientas en stderr. Un `AgentFailed` termina con salida 1."""

    def __init__(self, *, as_json: bool) -> None:
        self._as_json = as_json
        self._saw_delta = False
        self._wrote_text = False

    def __call__(self, event: AgentEvent) -> None:
        if self._as_json:
            echo(
                json.dumps(
                    camel_keys(dataclasses.asdict(event)),
                    ensure_ascii=False,
                    separators=JSON_LINE_SEPARATORS,
                )
            )
        elif isinstance(event, TextDelta):
            self._saw_delta = True
            self._write(event.text)
        elif isinstance(event, Text) and not self._saw_delta:
            self._write(event.text)
        elif isinstance(event, ToolCall):
            echo(f"tool={event.name} status={event.status}", err=True)
        if isinstance(event, AgentFailed):
            self.finish()
            fail(event.message)

    def finish(self) -> None:
        if self._wrote_text:
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


@agent_app.command("deploy")
def deploy_command(
    ctx: typer.Context,
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
) -> None:
    """Despliega el agente como servicio HTTP en un sandbox y deja la VM viva.

    Imprime el id del sandbox y la URL; las cabeceras de acceso no se
    imprimen nunca (se piden con AgentDeployment.headers). El access token
    del sandbox se guarda en --token-file (o es el de RAYITO_ACCESS_TOKEN):
    `invoke` y `status` lo necesitan. Vive como mucho 8 h: para pararlo,
    `rayito agent stop`.
    """
    token = deploy_token(token_file)
    project = load_project(file)
    serve = project.serve
    if timeout is not None:
        serve = dataclasses.replace(serve, timeout_seconds=timeout)
    if port is not None:
        serve = dataclasses.replace(serve, port=port)
    clients = clients_of(ctx)
    try:
        deployment = dataclasses.replace(project, serve=serve).deploy(
            region=clients.region, session=clients.session, access_token=token
        )
    except InvalidArgumentException as exc:
        fail(str(exc), code=EXIT_USAGE)
    if json_mode(ctx):
        emit_json({"sandboxId": deployment.sandbox_id, "url": deployment.url, "port": serve.port})
    else:
        echo(f"sandbox_id={deployment.sandbox_id}")
        echo(f"url={deployment.url}")
        echo(f"port={serve.port}")


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


def deploy_token(token_file: Path | None) -> str:
    """El token del despliegue: `RAYITO_ACCESS_TOKEN` o uno nuevo. Se guarda
    en `--token-file` antes de crear el sandbox, así un fallo al escribirlo
    nunca deja un VM huérfano. Sin ninguno de los dos no habría forma de
    volver a conectarse, y `deploy` no empieza."""
    existing = os.environ.get(TOKEN_ENV_VAR) or None
    if existing is None and token_file is None:
        fail(DEPLOY_NEEDS_TOKEN_FILE_MESSAGE, code=EXIT_USAGE)
    token = existing or generate_access_token()
    if token_file is not None:
        try:
            write_token_file(token_file, token)
        except TokenFileError as exc:
            fail(str(exc), code=EXIT_USAGE)
    return token


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


@agent_app.command("invoke")
def invoke_command(
    ctx: typer.Context,
    sandbox_id: Annotated[str, typer.Argument(help="Id del sandbox desplegado.")],
    prompt: Annotated[str, typer.Argument(help="Lo que se le pide al agente.")],
    session_id: Annotated[
        str | None, typer.Option("--session-id", help="Continúa una sesión anterior.")
    ] = None,
    stream: Annotated[bool, typer.Option("--stream", help=STREAM_HELP)] = False,
    token_file: TokenFileOption = None,
) -> None:
    """Llama a un agente desplegado con `rayito agent deploy`."""
    deployment = connect_deployment(ctx, sandbox_id, token_file)
    as_json = json_mode(ctx)
    try:
        if stream:
            printer = EventPrinter(as_json=as_json)
            with deployment.stream(prompt, session_id=session_id) as events:
                for event in events:
                    printer(event)
            printer.finish()
            return
        result = deployment.invoke(prompt, session_id=session_id)
    except AgentException as exc:
        run_failed(exc)
    print_result(result, as_json=as_json)


@agent_app.command("status")
def status_command(
    ctx: typer.Context,
    sandbox_id: Annotated[str, typer.Argument(help="Id del sandbox desplegado.")],
    token_file: TokenFileOption = None,
) -> None:
    """Estado de un agente desplegado: si está listo y si está ocupado."""
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


@agent_app.command("stop")
def stop_command(
    ctx: typer.Context,
    sandbox_id: Annotated[str, typer.Argument(help="Id del sandbox desplegado.")],
) -> None:
    """Para un agente desplegado: destruye su sandbox. No necesita el access
    token: `terminate-microvm` basta."""
    stopped = kill_one(clients_of(ctx), sandbox_id)
    if json_mode(ctx):
        emit_json({"sandboxId": sandbox_id, "stopped": stopped})
    else:
        echo(f"{'parado' if stopped else 'ya no existe'} sandbox_id={sandbox_id}")
