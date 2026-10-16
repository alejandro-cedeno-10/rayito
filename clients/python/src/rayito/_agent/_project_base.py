"""Lo que comparten `_project_service.py` y `_project_service_async.py`
(ADR-028, D6), sin E/S: la plantilla de `build()`, los argumentos de
`Sandbox.create()` de `run()` y `deploy()`, el fichero de configuración del
host (`host.json`) y el comando que lo arranca en modo servicio.
"""

from __future__ import annotations

import json
import math
import posixpath
from collections.abc import Mapping
from typing import Any, Final

from rayito._agent._deployment_base import labels_for
from rayito._agent._opencode import shell_quote
from rayito._agent._project_domain import AgentProject
from rayito._agent._recipes import AGENT_HOST_DIR
from rayito._agent._runtime import (
    GatewayWiring,
    HostConfig,
    InstallableRuntime,
    ServableRuntime,
    require_installable,
    require_servable,
)
from rayito._agent._runtimes import resolve_runtime
from rayito._agent._stream_base import gateway_urls_for
from rayito._agent._template import AgentTemplate, AsyncAgentTemplate
from rayito._limits import (
    AGENT_RUN_SANDBOX_MARGIN_SECONDS,
    AGENT_SERVICE_READY_MARKER_PATH,
    AGENT_SERVICE_SESSION_HEADER,
    DEFAULT_AGENT_WORKDIR,
)
from rayito._models import WriteEntry

#: Esquema del `host.json` que lee `agent_host` (`rayito.agent-host/1`).
AGENT_HOST_SCHEMA: Final = "rayito.agent-host/1"
#: El directorio del host dentro de `AGENT_STATE_DIR` y su configuración.
AGENT_HOST_STATE_DIR: Final = posixpath.dirname(AGENT_SERVICE_READY_MARKER_PATH)
AGENT_HOST_CONFIG_PATH: Final = f"{AGENT_HOST_STATE_DIR}/host.json"
#: Sufijo del marcador de arranque fallido (`agent_host/serve.py`).
AGENT_HOST_FAILED_SUFFIX: Final = ".failed"
#: Etiqueta del comando del host en `commands.list()`.
AGENT_SERVE_TAG: Final = "rayito-agent-serve"
#: Cada cuánto `deploy()` mira el marcador de listo.
AGENT_SERVICE_READY_POLL_SECONDS: Final = 0.5
#: URL de relleno para calcular la forma del `host_config()` sin sandbox.
PLACEHOLDER_GATEWAY_URL: Final = "http://127.0.0.1:0"
HOST_DOC_MODE: Final = 0o644


def template_name(project: AgentProject) -> str:
    """La imagen del proyecto: `template` o, por defecto, `name`."""
    return project.template or project.name


def project_template(project: AgentProject, *, asynchronous: bool = False) -> AgentTemplate:
    """El `AgentTemplate` de `project`: su runtime (ya comprobado como
    instalable), su código y la memoria pedida."""
    installable_runtime(project)
    template_class = AsyncAgentTemplate if asynchronous else AgentTemplate
    return template_class(
        name=template_name(project),
        base=project.base,
        runtimes=(project.runtime,),
        memory_mib=project.memory_mib,
        app=project.source,
        app_exclude=tuple(project.exclude),
    )


def installable_runtime(project: AgentProject) -> InstallableRuntime:
    return require_installable(resolve_runtime(project.runtime))


def servable_runtime(project: AgentProject) -> ServableRuntime:
    return require_servable(resolve_runtime(project.runtime))


def seconds_ceiling(value: float) -> int:
    """`Sandbox.create(timeout=)` son segundos enteros."""
    return math.ceil(value)


def run_timeout(project: AgentProject) -> int:
    """Vida del sandbox de `run()`: el plazo de la ejecución más un margen
    (`AGENT_RUN_SANDBOX_MARGIN_SECONDS`)."""
    return seconds_ceiling(project.limits.timeout_seconds + AGENT_RUN_SANDBOX_MARGIN_SECONDS)


def request_timeout(project: AgentProject) -> float:
    """Plazo de una petición a un despliegue: el de una ejecución más el
    margen del sandbox."""
    return project.limits.timeout_seconds + AGENT_RUN_SANDBOX_MARGIN_SECONDS


def run_create_args(project: AgentProject) -> dict[str, Any]:
    """Los argumentos de `Sandbox.create()` de `run()`: la imagen del
    proyecto, sus pasarelas y su red. Sin `idle` ni etiquetas: el sandbox
    vive lo que dura la ejecución."""
    return {"timeout": run_timeout(project), **_common_create_args(project)}


def deploy_create_args(
    project: AgentProject, *, raw: bool = False, access_token: str | None = None
) -> dict[str, Any]:
    """Los de `deploy()`: la vida y la política de `idle` de
    `project.serve`, las etiquetas con las que `connect()` reconoce el
    despliegue y, si el llamador lo da, el access token con el que otro
    proceso puede reconectarse."""
    serve = project.serve
    metadata = {**serve.metadata, **labels_for(project.name, serve.port, raw=raw)}
    args: dict[str, Any] = {
        "timeout": seconds_ceiling(serve.timeout_seconds),
        "idle": serve.idle,
        "metadata": metadata,
        **_common_create_args(project),
    }
    if access_token is not None:
        args["access_token"] = access_token
    return args


def _common_create_args(project: AgentProject) -> dict[str, Any]:
    args: dict[str, Any] = {
        "gateways": dict(project.gateways),
        "allow_internet_access": project.allow_internet_access,
    }
    if project.egress is not None:
        args["egress"] = list(project.egress)
    return args


def is_raw_app(host: HostConfig) -> bool:
    """Una app `raw` (`HttpApp(protocol="raw")`): sólo se expone."""
    return host.driver == "http-app" and host.options.get("protocol") == "raw"


def is_raw_project(project: AgentProject) -> bool:
    """Si el runtime del proyecto es una app `raw`, antes de crear el
    sandbox (las etiquetas del despliegue lo dicen para `connect()`): el
    `host_config()` es puro, y con URLs de pasarela de relleno devuelve el
    mismo driver y las mismas opciones."""
    urls = dict.fromkeys(project.spec.gateway_names(), PLACEHOLDER_GATEWAY_URL)
    host = servable_runtime(project).host_config(
        project.spec, wiring=GatewayWiring(urls=urls), workdir=DEFAULT_AGENT_WORKDIR
    )
    return is_raw_app(host)


def host_config_for(project: AgentProject, sandbox_gateways: Mapping[str, Any]) -> HostConfig:
    """La `HostConfig` del runtime con las URLs de las pasarelas del sandbox
    recién creado; falla con `InvalidArgumentException` si falta una."""
    wiring = GatewayWiring(urls=gateway_urls_for(project.spec, sandbox_gateways))
    return servable_runtime(project).host_config(
        project.spec, wiring=wiring, workdir=DEFAULT_AGENT_WORKDIR
    )


def host_document(project: AgentProject, host: HostConfig) -> dict[str, object]:
    """El `host.json`: el driver, sus opciones, los topes de `AgentLimits`,
    la concurrencia y dónde dejar el marcador de listo."""
    limits = project.limits
    return {
        "schema": AGENT_HOST_SCHEMA,
        "v": 1,
        "driver": host.driver,
        "options": dict(host.options),
        "limits": {
            "max_steps": limits.max_steps,
            "timeout_seconds": limits.timeout_seconds,
            "max_output_bytes": limits.max_output_bytes,
            "max_total_tokens": limits.max_total_tokens,
        },
        "max_concurrency": project.serve.max_concurrency,
        "session_header": AGENT_SERVICE_SESSION_HEADER,
        "ready_marker": AGENT_SERVICE_READY_MARKER_PATH,
    }


def host_entries(project: AgentProject, host: HostConfig) -> list[WriteEntry]:
    """Los ficheros que `deploy()` escribe antes de arrancar el host: los
    del runtime (con sus modos) y el `host.json`."""
    entries = [WriteEntry(f.path, f.data, f.mode) for f in host.files.files]
    document = json.dumps(host_document(project, host), indent=2, sort_keys=True) + "\n"
    entries.append(WriteEntry(AGENT_HOST_CONFIG_PATH, document.encode("utf-8"), HOST_DOC_MODE))
    return entries


def serve_command(host: HostConfig, port: int) -> str:
    """El comando que arranca el host en modo servicio con el intérprete del
    runtime."""
    return (
        f"exec {shell_quote(host.python)} {shell_quote(AGENT_HOST_DIR)} serve "
        f"{shell_quote(AGENT_HOST_CONFIG_PATH)} --port {port}"
    )


def failed_marker_path() -> str:
    return AGENT_SERVICE_READY_MARKER_PATH + AGENT_HOST_FAILED_SUFFIX


def clear_markers_command() -> str:
    """Borra los marcadores de un host anterior antes de arrancar el nuevo:
    un `ready` que sobreviva (una imagen o un guest reutilizado) haría que
    `deploy()` diera por listo un host que aún no escucha."""
    return (
        f"rm -f {shell_quote(AGENT_SERVICE_READY_MARKER_PATH)} {shell_quote(failed_marker_path())}"
    )
