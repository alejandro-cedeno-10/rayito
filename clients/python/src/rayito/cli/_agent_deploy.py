"""Lo que `rayito agent deploy` y `rayito agent detect` hacen antes y después de
llamar al SDK (ADR-029): leer las banderas, aplicarlas al proyecto detectado,
resolver el access token y su fichero, acortar los logs del build y escribir el
informe de la detección y del plan. Sin AWS: cada función es pura o toca sólo
ficheros locales.
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from rayito._agent._deploy_plan import AgentDeployPlan
from rayito._agent._project_domain import AgentProject
from rayito._models import IdlePolicy, NetworkPolicy
from rayito._payload import generate_access_token
from rayito._pool_backends import JsonFilePoolBackend
from rayito.cli._tokens import TOKEN_ENV_VAR, TokenFileError, read_token_file, write_token_file
from rayito.exceptions import InvalidArgumentException

#: Dónde guarda `rayito agent deploy` el access token de la flota si no se le
#: pasa `--token-file` ni `RAYITO_ACCESS_TOKEN`: un fichero `0600` dentro del
#: proyecto que el despliegue excluye del código que sube.
AGENT_TOKEN_FILE_DEFAULT: Final = Path(".rayito/agent-token")
#: Dónde guarda `rayito agent deploy` las plazas calientes para que sobrevivan
#: al comando (`JsonFilePoolBackend`, modo 0600, con los tokens de las plazas
#: aparcadas): junto al token, dentro del proyecto y fuera del código que sube.
AGENT_POOL_FILE_DEFAULT: Final = Path(".rayito/agent-pool.json")
TOKEN_DIRECTORY_MODE: Final = 0o700
#: Cada cuántas líneas de log del build se imprime una de progreso.
COMPACT_LOG_PROGRESS_LINES: Final = 200
#: Una línea de log con alguno de estos textos siempre se imprime.
COMPACT_LOG_ALERT_MARKERS: Final[tuple[str, ...]] = ("error", "failed", "fatal")
SECRET_FLAG_SEPARATOR: Final = "="


class CompactBuildLog:
    """El `on_build_logs` por defecto de `rayito agent deploy`: un build de
    imagen escribe cientos de líneas de log, así que sólo se imprimen las que
    avisan de un error, una de progreso cada `COMPACT_LOG_PROGRESS_LINES` y,
    al acabar, un resumen. `--verbose` imprime todas."""

    def __init__(self, emit: Callable[[str], None]) -> None:
        self._emit = emit
        self._lines = 0

    def __call__(self, line: str) -> None:
        self._lines += 1
        lowered = line.lower()
        if any(marker in lowered for marker in COMPACT_LOG_ALERT_MARKERS):
            self._emit(line)
        elif self._lines % COMPACT_LOG_PROGRESS_LINES == 0:
            self._emit(f"construyendo la imagen: {self._lines} líneas de log")

    def finish(self) -> None:
        if self._lines:
            self._emit(f"build: {self._lines} líneas de log (--verbose las imprime todas)")


def parse_pairs(values: Sequence[str] | None, flag: str) -> dict[str, str]:
    """`K=V` repetibles de una bandera como diccionario. El mensaje de un par
    mal formado no repite el valor: podría ser un secreto mal pegado."""
    pairs: dict[str, str] = {}
    for item in values or ():
        key, separator, value = item.partition(SECRET_FLAG_SEPARATOR)
        if not separator or not key or not value:
            raise InvalidArgumentException(f"{flag}: se esperaba CLAVE=VALOR")
        pairs[key] = value
    return pairs


def idle_policy_of(seconds: int) -> IdlePolicy:
    return IdlePolicy(max_idle_seconds=seconds)


def apply_serve_flags(
    project: AgentProject,
    *,
    max_sessions: int | None,
    idle_seconds: int | None,
    queue: int | None,
    queue_timeout: float | None,
    timeout: float | None,
    port: int | None,
) -> AgentProject:
    """El proyecto con lo que las banderas fijan en `ServeOptions`: sólo lo que
    se pasó (un valor `None` deja el del fichero o el de la detección)."""
    passed: dict[str, Any] = {
        "max_concurrency": max_sessions,
        "idle": None if idle_seconds is None else idle_policy_of(idle_seconds),
        "queue_max": queue,
        "queue_timeout_seconds": queue_timeout,
        "timeout_seconds": timeout,
        "port": port,
    }
    changes = {name: value for name, value in passed.items() if value is not None}
    if not changes:
        return project
    return dataclasses.replace(project, serve=dataclasses.replace(project.serve, **changes))


def apply_project_flags(
    project: AgentProject,
    *,
    memory: int | None,
    env: Mapping[str, str],
    allow_out: Sequence[str],
) -> AgentProject:
    """El proyecto con la memoria, el entorno y las salidas de red de las
    banderas: `--env` se suma al `env` del proyecto y `--allow-out` fija la
    política de salida (conserva el `deny_out` que ya tuviera)."""
    result = project
    if memory is not None:
        result = dataclasses.replace(result, memory_mib=memory)
    if env:
        result = dataclasses.replace(result, env={**result.env, **env})
    if allow_out:
        deny_out = () if result.network is None else tuple(result.network.deny_out)
        network = NetworkPolicy(allow_out=tuple(allow_out), deny_out=deny_out)
        result = dataclasses.replace(result, network=network)
    return result


def deploy_token(
    token_file: Path | None, environ: Mapping[str, str] = os.environ
) -> tuple[str | None, Path | None]:
    """El token que `deploy` pasa al SDK y el fichero donde queda.

    Con `--token-file`: `RAYITO_ACCESS_TOKEN` o uno nuevo, guardado en el
    fichero antes de crear nada (uno que ya exista es un error y no se toca:
    así un fallo al escribirlo nunca deja un VM huérfano). Sin él, la variable
    de entorno gana y no se escribe nada; si tampoco hay variable, se usa
    `AGENT_TOKEN_FILE_DEFAULT`: se reutiliza si existe (volver a desplegar
    reconecta con la misma flota) y, si no, se crea con modo `0600`."""
    from_environment = environ.get(TOKEN_ENV_VAR) or None
    if token_file is not None:
        token = from_environment or generate_access_token()
        write_token_file(token_file, token)
        return token, token_file
    if from_environment is not None:
        return from_environment, None
    return default_token(AGENT_TOKEN_FILE_DEFAULT), AGENT_TOKEN_FILE_DEFAULT


def pool_backend_for(
    warm_vms: int, path: Path = AGENT_POOL_FILE_DEFAULT
) -> JsonFilePoolBackend | None:
    """El respaldo en fichero de las plazas calientes si hay que dejar alguna
    aparcada (`warm_vms > 0`); sin plazas no se crea ni el directorio."""
    if warm_vms == 0:
        return None
    path.parent.mkdir(parents=True, exist_ok=True, mode=TOKEN_DIRECTORY_MODE)
    return JsonFilePoolBackend(path)


def default_token(path: Path) -> str:
    if path.exists():
        existing = read_token_file(path)
        if existing:
            return existing
        raise TokenFileError(f"el fichero de token está vacío: {path}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=TOKEN_DIRECTORY_MODE)
    token = generate_access_token()
    write_token_file(path, token)
    return token


def format_usd(value: float) -> str:
    return f"${value:.4f}".replace(".", ",")


def describe_plan(plan: AgentDeployPlan) -> list[str]:
    """El plan en líneas de texto (sin secretos, sin AWS)."""
    scaling = plan.scaling
    cost = plan.cost
    lines = [
        f"agente: {plan.name} (imagen {plan.template}, base {plan.base}, {plan.memory_mib} MiB)",
        f"región: {plan.region or 'la de la cadena de boto3'}",
        (
            f"VMs: mínimo {scaling.min_vms}, máximo {scaling.max_vms}, calientes "
            f"{scaling.warm_vms}, sesiones {scaling.sessions}"
        ),
        f"bucket del artefacto: {_source_label(plan.bucket_source)}",
        f"access token: {_source_label(plan.token_source)}",
    ]
    lines.extend(
        f"secreto de la pasarela {item.gateway}: {_source_label(item.source)}"
        for item in plan.secrets
    )
    lines.append(
        f"coste de lista: {format_usd(cost.running_usd_per_hour)}/h por los VMs mínimos; "
        f"plazas aparcadas {format_usd(cost.parked_usd_per_day)}/día; imagen nueva "
        f"{format_usd(cost.image_build_usd)} y {format_usd(cost.image_storage_usd_per_week)}/semana"
    )
    lines.extend(f"nota: {note}" for note in plan.notes)
    return lines


_SOURCE_LABELS: Final[Mapping[str, str]] = {
    "argument": "el argumento",
    "project_file": "el fichero del proyecto",
    "environment": "la variable de entorno",
    "discovered": "se descubrirá el bucket de CDK de la cuenta",
    "generated": "se generará uno nuevo",
    "override": "el de --secret",
    "project": "el del proyecto",
}


def _source_label(source: str) -> str:
    return _SOURCE_LABELS.get(source, source)


def detection_lines(report: object) -> list[str]:
    """Los campos escalares de un informe de detección como `clave: valor`
    (no la lista de evidencias ni ningún contenido de ficheros)."""
    if not dataclasses.is_dataclass(report) or isinstance(report, type):
        return []
    return [
        f"detectado {key}: {value}"
        for key, value in dataclasses.asdict(report).items()
        if isinstance(value, str | int | bool) and value not in ("", None)
    ]
