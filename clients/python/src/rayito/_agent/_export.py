"""`AgentProject.export()`: un contexto de build de contenedor `linux/arm64`
para llevar el agente fuera de Rayito (ADR-028, D6), sin ninguna llamada de
red. Escribe el `Dockerfile` (base fijada por digest, los pasos de instalación
del runtime para contenedor, el host del agente, su configuración con el
modelo cableado directo, el código del usuario), un `.dockerignore` y un
`README.md` con las órdenes de build y de ejecución. El contenedor sirve
`rayito.agent-service/1` en `AGENT_SERVICE_EXPORT_PORT` (8080, el puerto que
AgentCore espera).

Fuera de Rayito no hay pasarela de secretos: la credencial del modelo la pone
la plataforma (variables de entorno, rol de IAM) y el agente puede leerla. La
garantía de que el agente nunca ve la credencial sólo vale dentro de un
sandbox de Rayito; el README generado lo dice.
"""

from __future__ import annotations

import json
import logging
import os
import posixpath
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

from rayito._agent._project_base import (
    AGENT_HOST_SCHEMA,
    host_document,
    installable_runtime,
    is_raw_app,
    servable_runtime,
)
from rayito._agent._recipes import (
    AGENT_APP_CONTEXT_DIR,
    AGENT_APP_DIR,
    AGENT_HOST_CONTEXT_DIR,
    AGENT_HOST_DIR,
    AGENT_INSTALL_DIR,
    AGENT_RAYITO_DIR,
    agent_host_files,
    asset_bytes,
    hashed_venv_install,
)
from rayito._agent._runtime import (
    STEP_COPY,
    STEP_RUN,
    DirectWiring,
    HostConfig,
    InstallTarget,
    TemplateStep,
)
from rayito._agent._secret_env_domain import (
    HOST_KEY as SECRET_ENV_HOST_KEY,
)
from rayito._agent._secret_env_domain import (
    SCRIPT_NAME as SECRET_ENV_SCRIPT_NAME,
)
from rayito._agent._secret_env_domain import uses_secret_env
from rayito._agent._secret_env_project import check_runtime_collisions
from rayito._agent._template import _app_files
from rayito._agentcore import _env_credentials
from rayito._limits import (
    AGENT_AWS_CREDENTIALS_REQUIREMENTS_SHA256,
    AGENT_EXPORT_BASE_IMAGE,
    AGENT_EXPORT_BASE_IMAGE_DIGEST,
    AGENT_SERVICE_EXPORT_PORT,
    AGENT_STATE_DIR,
    DEFAULT_AGENT_WORKDIR,
)
from rayito.exceptions import InvalidArgumentException

if TYPE_CHECKING:
    from rayito._agent._project_domain import AgentProject

logger = logging.getLogger("rayito.agent")

ExportTarget = Literal["docker", "agentcore"]
EXPORT_TARGETS: Final[tuple[str, ...]] = ("docker", "agentcore")
#: La plataforma de la imagen exportada: la de AgentCore y la de las
#: imágenes de Rayito.
EXPORT_PLATFORM: Final = "linux/arm64"
#: La imagen base con su digest: lo que se construye hoy es lo que se
#: construirá mañana.
BASE_IMAGE_REFERENCE: Final = f"{AGENT_EXPORT_BASE_IMAGE}@{AGENT_EXPORT_BASE_IMAGE_DIGEST}"
#: El argumento de build con el que el `FROM` de AgentCore toma la imagen base
#: de otro registro con el mismo digest (`contract.json`, `dockerfileBaseImageArg`).
BASE_IMAGE_ARG: Final = "AGENT_BASE_IMAGE"
#: Dónde deja el export la herramienta que lee las credenciales del rol de
#: AgentCore para los adaptadores que sólo usan variables de entorno.
AWS_CREDENTIALS_REQUIREMENTS_PATH: Final = (
    f"{AGENT_INSTALL_DIR}/{_env_credentials.REQUIREMENTS_NAME}"
)
AWS_CREDENTIALS_VENV_DIR: Final = f"{AGENT_RAYITO_DIR}/aws-credentials"
AWS_CREDENTIALS_SCRIPT_PATH: Final = f"{AGENT_RAYITO_DIR}/{_env_credentials.SCRIPT_NAME}"
AWS_CONFIG_PATH: Final = f"{AGENT_RAYITO_DIR}/{_env_credentials.CONFIG_NAME}"
#: La herramienta que lee los secretos de `secret_env` al arrancar en AgentCore
#: (ADR-033 D4), en el mismo venv de botocore.
SECRET_ENV_SCRIPT_PATH: Final = f"{AGENT_RAYITO_DIR}/{SECRET_ENV_SCRIPT_NAME}"
AGENT_USER: Final = "user"
#: `~/.rayito`: lo que `user` debe poder escribir (sesiones del agente).
RAYITO_HOME_DIR: Final = f"{DEFAULT_AGENT_WORKDIR}/.rayito"
AGENT_UID: Final = 1000
HOST_CONFIG_NAME: Final = "host.json"
HOST_CONFIG_PATH: Final = f"{AGENT_RAYITO_DIR}/{HOST_CONFIG_NAME}"
RUNTIME_FILES_CONTEXT_DIR: Final = "config"
DOCKERFILE_NAME: Final = "Dockerfile"
DOCKERIGNORE_NAME: Final = ".dockerignore"
README_NAME: Final = "README.md"
DOCKERIGNORE_CONTENT: Final = f"{README_NAME}\n{DOCKERIGNORE_NAME}\n{DOCKERFILE_NAME}\n"
#: Variable con la que un contenedor exige `Authorization: Bearer` en
#: `/invocations` (`agent_host/serve.py`); no se fija en la imagen.
SERVICE_TOKEN_ENV: Final = "RAYITO_AGENT_SERVICE_TOKEN"
HEALTHCHECK_INTERVAL: Final = "30s"
HEALTHCHECK_COMMAND: Final = (
    'python3 -c "import urllib.request; '
    f"urllib.request.urlopen('http://127.0.0.1:{AGENT_SERVICE_EXPORT_PORT}/ping')\""
)
_PERMISSIONS_CMD: Final = (
    f"chown -R root:root {AGENT_INSTALL_DIR} && chmod -R a+rX,go-w {AGENT_INSTALL_DIR}"
)


class RawAppNotExportable(InvalidArgumentException):
    """Una app con `protocol="raw"` no cumple el contrato de AgentCore
    (`/ping` y `/invocations`) y no se puede exportar para ese destino."""


@dataclass(frozen=True)
class AgentExport:
    """Lo que `export()` escribió: el directorio, los ficheros (rutas
    relativas, ordenadas), el puerto en el que sirve el contenedor y su
    plataforma."""

    path: str
    files: tuple[str, ...]
    port: int
    platform: str = EXPORT_PLATFORM


def export_project(
    project: AgentProject,
    *,
    target: str = "docker",
    out_dir: str | os.PathLike[str],
    overwrite: bool = False,
) -> AgentExport:
    """Renderiza y escribe el contexto de build de `project` en `out_dir`.
    Falla con `InvalidArgumentException`, antes de escribir nada, si `target`
    no existe, el runtime no se puede instalar ni servir, el proyecto es una
    app `raw` para `agentcore` o `out_dir` no está vacío (sin `overwrite`)."""
    if target not in EXPORT_TARGETS:
        raise InvalidArgumentException(f"target debe ser uno de {list(EXPORT_TARGETS)}")
    tree = render_export(project, target=target)
    destination = Path(out_dir)
    if not overwrite and destination.exists() and any(destination.iterdir()):
        raise InvalidArgumentException(
            "out_dir no está vacío; usa otro directorio u overwrite=True"
        )
    for relative, data in tree.items():
        file_path = destination / relative
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(data)
    return AgentExport(
        path=str(destination), files=tuple(sorted(tree)), port=AGENT_SERVICE_EXPORT_PORT
    )


def render_export(project: AgentProject, *, target: str = "docker") -> dict[str, bytes]:
    """El árbol de ficheros del export (ruta relativa -> bytes). Puro salvo
    la lectura del código del usuario."""
    runtime = installable_runtime(project)
    servable = servable_runtime(project)
    install = InstallTarget(platform="container")
    host = servable.host_config(project.spec, wiring=DirectWiring(), workdir=DEFAULT_AGENT_WORKDIR)
    agentcore = target == "agentcore"
    credential_process = agentcore and _env_credentials.uses_credential_process(project.runtime)
    if credential_process:
        host = replace(host, envs={**host.envs, **_env_credentials.profile_envs(AWS_CONFIG_PATH)})
    if target == "agentcore" and is_raw_app(host):
        raise RawAppNotExportable(
            "una app con protocol='raw' no cumple el contrato de AgentCore (/ping + /invocations)"
        )
    tree: dict[str, bytes] = {}
    for name, data in runtime.context_files().items():
        tree[name] = data
    for relative, data in agent_host_files().items():
        tree[f"{AGENT_HOST_CONTEXT_DIR}/{relative}"] = data
    for index, runtime_file in enumerate(host.files.files):
        tree[f"{RUNTIME_FILES_CONTEXT_DIR}/{index}"] = runtime_file.data
    env_credentials = agentcore and _env_credentials.uses_env_credentials(project.runtime)
    secret_env = _secret_env_target(project, host, target)
    tree[HOST_CONFIG_NAME] = _host_config_bytes(
        project, host, env_credentials=env_credentials, secret_env=secret_env
    )
    if env_credentials or credential_process or secret_env:
        tree[_env_credentials.REQUIREMENTS_NAME] = asset_bytes(_env_credentials.REQUIREMENTS_NAME)
    if env_credentials or credential_process:
        tree[_env_credentials.SCRIPT_NAME] = asset_bytes(_env_credentials.SCRIPT_NAME)
    if secret_env:
        tree[SECRET_ENV_SCRIPT_NAME] = asset_bytes(SECRET_ENV_SCRIPT_NAME)
    if credential_process:
        tree[_env_credentials.CONFIG_NAME] = _env_credentials.config_text(
            f"{AWS_CREDENTIALS_VENV_DIR}/bin/python", AWS_CREDENTIALS_SCRIPT_PATH
        ).encode()
    if project.source is not None:
        for relative, data in _app_files(project.source, project.exclude):
            tree[f"{AGENT_APP_CONTEXT_DIR}/{relative}"] = data
    steps = runtime.install_steps(install)
    tree[DOCKERFILE_NAME] = _dockerfile(
        project,
        host,
        steps,
        target=target,
        helper=env_credentials or credential_process,
        config=credential_process,
        secret_env=secret_env,
    ).encode()
    tree[DOCKERIGNORE_NAME] = DOCKERIGNORE_CONTENT.encode()
    tree[README_NAME] = _readme(project, target).encode()
    return dict(sorted(tree.items()))


def _secret_env_target(project: AgentProject, host: HostConfig, target: str) -> bool:
    """Si el export lleva la herramienta de `secret_env`: sólo con AgentCore.
    Con Docker se ignora con un aviso (fase 2, ADR-033 D4). Un nombre que el
    runtime ya fija es `invalid_value` antes de escribir nada."""
    if not uses_secret_env(project.secret_env, project.secret_env_from):
        return False
    if target != "agentcore":
        logger.warning(DOCKER_SECRET_ENV_WARNING)
        return False
    check_runtime_collisions(project, host.envs)
    return True


def _host_config_bytes(
    project: AgentProject, host: HostConfig, *, env_credentials: bool, secret_env: bool = False
) -> bytes:
    document = host_document(project, host)
    del document["ready_marker"]
    document["schema"] = AGENT_HOST_SCHEMA
    if env_credentials:
        document[_env_credentials.HOST_KEY] = {
            "python": f"{AWS_CREDENTIALS_VENV_DIR}/bin/python",
            "script": AWS_CREDENTIALS_SCRIPT_PATH,
        }
    if secret_env:
        document[SECRET_ENV_HOST_KEY] = {
            "python": f"{AWS_CREDENTIALS_VENV_DIR}/bin/python",
            "reserved": sorted(host.envs),
            "script": SECRET_ENV_SCRIPT_PATH,
        }
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()


def _step_lines(steps: Sequence[TemplateStep]) -> tuple[list[str], list[str]]:
    lines: list[str] = []
    executables: list[str] = []
    for step in steps:
        if step.op == STEP_RUN:
            lines.append(f"RUN {step.args['cmd']}")
        elif step.op == STEP_COPY:
            lines.append(f"COPY {step.args['src']} {step.args['dst']}")
            if step.args.get("executable") is True:
                executables.append(str(step.args["dst"]))
    return lines, executables


def _env_lines(envs: Mapping[str, str]) -> list[str]:
    return [f"ENV {name}={json.dumps(value)}" for name, value in sorted(envs.items())]


def _credentials_lines(target: InstallTarget, *, credentials: bool, secret_env: bool) -> list[str]:
    """Los pasos que instalan las herramientas de AgentCore (un venv con
    botocore fijado por hash): la que lee las credenciales del rol para un
    adaptador que sólo las lee de variables de entorno y, con `secret_env`, la
    que lee los secretos al arrancar."""
    install = hashed_venv_install(
        target,
        venv_dir=AWS_CREDENTIALS_VENV_DIR,
        requirements_path=AWS_CREDENTIALS_REQUIREMENTS_PATH,
        requirements_sha256=AGENT_AWS_CREDENTIALS_REQUIREMENTS_SHA256,
    )
    lines = [
        f"COPY {_env_credentials.REQUIREMENTS_NAME} {AWS_CREDENTIALS_REQUIREMENTS_PATH}",
        f"RUN {install}",
    ]
    if credentials:
        lines.append(f"COPY {_env_credentials.SCRIPT_NAME} {AWS_CREDENTIALS_SCRIPT_PATH}")
    if secret_env:
        lines.append(f"COPY {SECRET_ENV_SCRIPT_NAME} {SECRET_ENV_SCRIPT_PATH}")
    return lines


def _from_lines(target: str) -> list[str]:
    """Con AgentCore, la base llega por un argumento de build para que CodeBuild
    use el espejo de ECR Public con el mismo digest; con Docker, la referencia
    fija de siempre."""
    if target != "agentcore":
        return [f"FROM --platform={EXPORT_PLATFORM} {BASE_IMAGE_REFERENCE}"]
    return [
        f"ARG {BASE_IMAGE_ARG}={BASE_IMAGE_REFERENCE}",
        f"FROM --platform={EXPORT_PLATFORM} ${{{BASE_IMAGE_ARG}}}",
    ]


def _dockerfile(
    project: AgentProject,
    host: HostConfig,
    steps: Sequence[TemplateStep],
    *,
    target: str,
    helper: bool,
    config: bool,
    secret_env: bool = False,
) -> str:
    install_lines, executables = _step_lines(steps)
    config_dirs = sorted({posixpath.dirname(file.path) for file in host.files.files})
    copy_files = [
        f"COPY --chown={AGENT_USER}:{AGENT_USER} --chmod={file.mode:04o} "
        f"{RUNTIME_FILES_CONTEXT_DIR}/{index} {file.path}"
        for index, file in enumerate(host.files.files)
    ]
    permissions = _PERMISSIONS_CMD + (
        f" && chmod 0755 {' '.join(executables)}" if executables else ""
    )
    command = json.dumps(
        [
            host.python,
            AGENT_HOST_DIR,
            "serve",
            HOST_CONFIG_PATH,
            "--port",
            str(AGENT_SERVICE_EXPORT_PORT),
        ]
    )
    lines = [
        "# syntax=docker/dockerfile:1",
        f"# Agente `{project.name}` exportado por Rayito. Construye con:",
        f"#   docker buildx build --platform {EXPORT_PLATFORM} -t {project.name} .",
        *_from_lines(target),
        f"RUN useradd --create-home --uid {AGENT_UID} --shell /bin/bash {AGENT_USER}"
        f" && mkdir -p {AGENT_STATE_DIR} {' '.join(config_dirs)}"
        f" && chown -R {AGENT_USER}:{AGENT_USER} {RAYITO_HOME_DIR}",
        *install_lines,
        *(
            _credentials_lines(
                InstallTarget(platform="container"), credentials=helper, secret_env=secret_env
            )
            if helper or secret_env
            else []
        ),
        *([f"COPY {_env_credentials.CONFIG_NAME} {AWS_CONFIG_PATH}"] if config else []),
        f"COPY {AGENT_HOST_CONTEXT_DIR} {AGENT_HOST_DIR}",
        f"COPY {HOST_CONFIG_NAME} {HOST_CONFIG_PATH}",
        *([f"COPY {AGENT_APP_CONTEXT_DIR} {AGENT_APP_DIR}"] if project.source is not None else []),
        f"RUN {permissions}",
        *copy_files,
        *_env_lines(host.envs),
        f"EXPOSE {AGENT_SERVICE_EXPORT_PORT}",
        f"USER {AGENT_USER}",
        f"WORKDIR {DEFAULT_AGENT_WORKDIR}",
        f"HEALTHCHECK --interval={HEALTHCHECK_INTERVAL} CMD {HEALTHCHECK_COMMAND}",
        f"CMD {command}",
    ]
    return "\n".join(lines) + "\n"


def _readme(project: AgentProject, target: str) -> str:
    port = AGENT_SERVICE_EXPORT_PORT
    lines = [
        f"# {project.name}",
        "",
        "Contexto de build exportado por Rayito (`AgentProject.export()`). El contenedor",
        f"sirve el contrato `rayito.agent-service/1` en el puerto {port}: `GET /ping` y",
        "`POST /invocations`.",
        "",
        "## Construir y probar en local",
        "",
        "```bash",
        f"docker buildx build --platform {EXPORT_PLATFORM} -t {project.name} .",
        f"docker run --rm --platform {EXPORT_PLATFORM} -p {port}:{port} \\",
        f"  -e {SERVICE_TOKEN_ENV}=<token> {project.name}",
        f"curl http://127.0.0.1:{port}/ping",
        f"curl -X POST http://127.0.0.1:{port}/invocations \\",
        "  -H 'Authorization: Bearer <token>' -H 'Content-Type: application/json' \\",
        '  -d \'{"prompt": "hola"}\'',
        "```",
        "",
        "Con `Accept: text/event-stream` la respuesta llega como un mensaje SSE por evento.",
        f"`{SERVICE_TOKEN_ENV}` exige `Authorization: Bearer <token>` en `/invocations`.",
        "",
        "## Credenciales",
        "",
        "Fuera de un sandbox de Rayito no hay pasarela de secretos: la credencial del",
        "modelo la pone la plataforma donde corras el contenedor (variables de entorno o",
        "rol de IAM) y el agente puede leerla. La garantía de que el agente nunca ve la",
        "credencial sólo vale dentro de Rayito.",
    ]
    if uses_secret_env(project.secret_env, project.secret_env_from):
        lines += _secret_env_readme(target)
    if target == "agentcore":
        lines += _agentcore_readme(port)
    return "\n".join(lines) + "\n"


def _secret_env_readme(target: str) -> list[str]:
    if target != "agentcore":
        return ["", DOCKER_SECRET_ENV_WARNING]
    return [
        "",
        "### Secretos como variables de entorno",
        "",
        "El host lee los secretos de `secret_env` al arrancar, con el rol del runtime, y",
        "los pone en el entorno del agente antes de cargar su código. El agente y todo lo",
        "que lance pueden leer su entorno: da al rol sólo los secretos que necesita.",
    ]


def _agentcore_readme(port: int) -> list[str]:
    return [
        "",
        "## Amazon Bedrock AgentCore Runtime",
        "",
        f"AgentCore espera una imagen `{EXPORT_PLATFORM}` que sirva `/ping` y `/invocations`",
        f"en el puerto {port}, como esta. La forma más corta de desplegarla es",
        "`rayito agent deploy <directorio> --target agentcore`: construye la imagen con",
        "CodeBuild (sin Docker local), la sube a Amazon ECR, crea el rol del runtime con",
        "el mínimo privilegio y crea o actualiza el runtime. A mano: sube la imagen a ECR,",
        "crea el runtime en la consola o con la CLI de AgentCore y activa MMDSv2",
        "(`requireMMDSV2` en `UpdateAgentRuntime`), sin el que AgentCore rechaza las",
        "invocaciones. AgentCore autentica las invocaciones por sí mismo (SigV4): no",
        f"fijes `{SERVICE_TOKEN_ENV}`.",
        "",
        f"El argumento de build `{BASE_IMAGE_ARG}` toma la misma imagen base, con el mismo",
        "digest, de otro registro (por ejemplo `public.ecr.aws/docker/library/python`).",
        "",
        "### Seguridad: sin pasarela de secretos",
        "",
        "En AgentCore no hay pasarela de secretos de Rayito: el agente obtiene las",
        "credenciales del rol del runtime por el servicio de metadatos de la microVM, y",
        "cualquier código que corra en la sesión puede leerlas. Da al rol sólo lo que el",
        "agente necesita: los modelos de Bedrock que usa y, si hace falta, los secretos",
        "que debe leer.",
    ]


DOCKER_SECRET_ENV_WARNING: Final = (
    "secret_env y secret_env_from no se exportan a Docker: pon esas variables con la "
    "plataforma donde corras el contenedor"
)

__all__ = ["EXPORT_TARGETS", "AgentExport", "export_project", "render_export"]
