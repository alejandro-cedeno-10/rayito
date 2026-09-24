"""Modelos públicos del SDK: dataclasses sin I/O."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import IO, Any, Final, Literal, NoReturn, Protocol, Self, TypeAlias, TypedDict

from rayito._charts import Chart
from rayito._limits import (
    IDLE_MAX_IDLE_MIN_SECONDS,
    IDLE_SUSPENDED_MIN_SECONDS,
    LIFECYCLE_TIMEOUT_EXIT_CODE,
    PERSIST_KEY_PREFIX_MAX_BYTES,
    PORT_MAX,
    PORT_MIN,
    S3_BUCKET_NAME_MAX,
    S3_BUCKET_NAME_MIN,
    TRANSFER_DEFAULT_MAX_EXPIRES_IN_SECONDS,
    TRANSFER_DEFAULT_MULTIPART_THRESHOLD_BYTES,
    TRANSFER_DEFAULT_PREFIX,
    TRANSFER_DEFAULT_THRESHOLD_BYTES,
    TRANSFER_MULTIPART_THRESHOLD_MIN_BYTES,
    TRANSFER_PRESIGN_MAX_SECONDS,
    TRANSFER_SINGLE_PUT_MAX_BYTES,
    TRANSFER_THRESHOLD_MIN_BYTES,
)
from rayito.exceptions import InvalidArgumentException

REDACTED: Final = "<redacted>"
PROXY_AUTH_HEADER = "x-aws-proxy-auth"
PROXY_PORT_HEADER = "x-aws-proxy-port"
TIMEOUT_STATE_REASON: Final = f"Container Stopped with Exit Code: {LIFECYCLE_TIMEOUT_EXIT_CODE}"


@dataclass(frozen=True)
class IdlePolicy:
    """Espejo de `idlePolicy` de `run-microvm`.

    `suspended_duration_seconds=None` se resuelve en `create()` como
    `timeout - max_idle_seconds`; `0` significa terminar al suspender.
    Un ciclo suspend/resume cuesta ≈ $0,0049 a 2 GB (0,92 GB de snapshot
    escritos y leídos, SPEC.md §7) ≈ 140 s de cómputo, así que un
    `max_idle_seconds` menor que ≈ 150 s no ahorra dinero; el mínimo que
    acepta la API es 60.
    """

    max_idle_seconds: int = 300
    suspended_duration_seconds: int | None = None
    auto_resume: bool = True

    def __post_init__(self) -> None:
        if self.max_idle_seconds < IDLE_MAX_IDLE_MIN_SECONDS:
            raise InvalidArgumentException(
                f"max_idle_seconds debe ser >= {IDLE_MAX_IDLE_MIN_SECONDS}, "
                f"recibido {self.max_idle_seconds}"
            )
        if (
            self.suspended_duration_seconds is not None
            and self.suspended_duration_seconds < IDLE_SUSPENDED_MIN_SECONDS
        ):
            raise InvalidArgumentException(
                f"suspended_duration_seconds debe ser >= {IDLE_SUSPENDED_MIN_SECONDS}, "
                f"recibido {self.suspended_duration_seconds}"
            )


LifecyclePhaseName = Literal["unmanaged", "active", "resume_grace", "expired"]
TimeoutActionName = Literal["kill", "pause"]


@dataclass(frozen=True)
class SandboxLifecycle:
    """El plazo lógico que impone `rayd` (ADR-011), tal como lo reporta `Health`.

    `phase` es `unmanaged` cuando el sandbox se creó sin `max_lifetime` ni
    `on_timeout` (su vida es la de la plataforma, ADR-007), `active` con el
    plazo corriendo, `resume_grace` durante los 30 s tras reanudarse pasado
    el plazo (esperando a `connect()`) y `expired` con el plazo vencido.
    `deadline` y `cap` son instantes de pared (`None` en `unmanaged`); `cap`
    es `max_lifetime - 60 s` desde el arranque y el plazo nunca lo pasa.
    `extensions` cuenta los `SetTimeout` que movieron el plazo en este
    arranque del agente.
    """

    phase: LifecyclePhaseName
    deadline: datetime | None
    cap: datetime | None
    timeout_seconds: float
    on_timeout: TimeoutActionName | None
    auto_resume: bool
    extensions: int

    @property
    def managed(self) -> bool:
        return self.phase != "unmanaged"


@dataclass(frozen=True)
class SandboxInfo:
    """Lo que devuelve `get-microvm` (y `run-microvm`), normalizado.

    `endpoint` es siempre el hostname pelado; `endpoint_url` le antepone
    `https://`. `template` es el ARN de la imagen. `metadata` es `None`
    cuando no se leyó del agente (`get-microvm` no lo conoce) y `{}` cuando
    se leyó y estaba vacío. `ingress`/`egress` son los ARNs de conectores
    que `run-microvm`/`get-microvm` reportan (`ingressNetworkConnectors` /
    `egressNetworkConnectors`), vacíos si la respuesta no los trae.
    `lifecycle` es el último plazo lógico leído del agente (`None` si no se
    leyó o si la imagen es anterior a M9): `expires_at` es ese plazo cuando
    el sandbox lo tiene y `platform_expires_at` siempre el tope de la
    plataforma (`started_at + maximum_duration_seconds`).
    `agent_version`, `cpu_count` y `memory_mb` son la vista del guest que
    devuelve `Health` (`None` si no se leyó del agente o no se conoce):
    `cpu_count` son las CPUs que `rayd` puede usar y `memory_mb` el
    `MemTotal` del guest en MiB, no el `minimumMemoryInMiB` de la imagen.
    """

    sandbox_id: str
    state: str
    endpoint: str
    template: str
    template_version: str
    started_at: datetime
    maximum_duration_seconds: int
    terminated_at: datetime | None = None
    state_reason: str | None = None
    idle: IdlePolicy | None = None
    execution_role_arn: str | None = None
    metadata: dict[str, str] | None = None
    ingress: tuple[str, ...] = ()
    egress: tuple[str, ...] = ()
    lifecycle: SandboxLifecycle | None = None
    agent_version: str | None = None
    cpu_count: int | None = None
    memory_mb: int | None = None

    @property
    def endpoint_url(self) -> str:
        return f"https://{self.endpoint}"

    @property
    def expires_at(self) -> datetime:
        lifecycle = self.lifecycle
        if lifecycle is not None and lifecycle.managed and lifecycle.deadline is not None:
            return lifecycle.deadline
        return self.platform_expires_at

    @property
    def platform_expires_at(self) -> datetime:
        return self.started_at + timedelta(seconds=self.maximum_duration_seconds)

    @property
    def timed_out(self) -> bool:
        """`rayd` salió por el plazo lógico en modo `kill` (código 124 en el
        `stateReason` de `get-microvm`, `AWS_API_NOTES.md` Q63)."""
        return self.state_reason == TIMEOUT_STATE_REASON

    @property
    def template_name(self) -> str:
        return template_name_from_arn(self.template)

    def remaining_seconds(self, now: datetime | None = None) -> float:
        current = now or datetime.now(UTC)
        return max(0.0, (self.expires_at - current).total_seconds())


@dataclass(frozen=True)
class SandboxListItem:
    """Un item de `list-microvms`: no trae `endpoint` ni `stateReason`.

    `metadata` sólo viene relleno en `Sandbox.list(metadata=...)`, que lo lee
    del agente sandbox a sandbox; `None` significa que no se leyó.
    """

    sandbox_id: str
    state: str
    template: str
    template_version: str
    started_at: datetime
    metadata: dict[str, str] | None = None

    @property
    def template_name(self) -> str:
        return template_name_from_arn(self.template)


@dataclass(frozen=True)
class MicrovmListPage:
    """Una página cruda de `list-microvms`: todos sus items, sin filtrar por
    estado, y el `nextToken` de AWS (`None` en la última)."""

    items: tuple[SandboxListItem, ...]
    next_token: str | None = None


class HostAccess(str):
    """Resultado de `get_host(port)`.

    Es un `str` con el hostname del MicroVM para que `f"https://{host}"` siga
    funcionando como en E2B, y además expone `url`, `port` y `headers`. Las
    cabeceras se leen en cada acceso para reflejar el JWE renovado por el
    refresher; nunca incluyen `x-aws-proxy-force-h2` (eso es sólo para gRPC).
    """

    __slots__ = ("_port", "_token_provider")

    _port: int
    _token_provider: Callable[[], str]

    def __new__(cls, host: str, *, port: int, token_provider: Callable[[], str]) -> HostAccess:
        instance = super().__new__(cls, host)
        instance._port = port
        instance._token_provider = token_provider
        return instance

    @property
    def host(self) -> str:
        return str.__str__(self)

    @property
    def url(self) -> str:
        return f"https://{self.host}"

    @property
    def port(self) -> int:
        return self._port

    @property
    def headers(self) -> dict[str, str]:
        return {PROXY_AUTH_HEADER: self._token_provider(), PROXY_PORT_HEADER: str(self._port)}

    def __repr__(self) -> str:
        return f"HostAccess({self.host!r}, port={self._port})"


ProcessKindName = Literal["process", "pty"]

MAX_PTY_DIMENSION: Final = 4096
DEFAULT_PTY_COLS: Final = 80
DEFAULT_PTY_ROWS: Final = 24


@dataclass(frozen=True)
class PtySize:
    """Tamaño de una terminal (`PtyService` `PtySize`): `1 <= cols, rows <= 4096`."""

    cols: int = DEFAULT_PTY_COLS
    rows: int = DEFAULT_PTY_ROWS

    def __post_init__(self) -> None:
        validate_pty_dimension(self.cols, field="cols")
        validate_pty_dimension(self.rows, field="rows")


def validate_pty_dimension(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_PTY_DIMENSION:
        raise InvalidArgumentException(
            f"{field} debe ser un entero entre 1 y {MAX_PTY_DIMENSION}, recibido {value!r}"
        )
    return value


@dataclass(frozen=True)
class CommandResult:
    """Salida completa de un comando que terminó con exit code 0.

    Un exit code distinto de cero llega como `CommandExitException`, que lleva
    los mismos campos; `error` queda reservado para futuros estados sin
    excepción y es `None` en M2.
    """

    stdout: str
    stderr: str
    exit_code: int
    error: str | None = None


@dataclass(frozen=True)
class ProcessInfo:
    """Un item de `ProcessService.List`: `cmd`/`args` son los del wrapper
    (`/bin/bash -l -c <cmd>`), no el comando que escribió el usuario."""

    pid: int
    cmd: str
    args: tuple[str, ...] = ()
    envs: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    tag: str | None = None
    kind: ProcessKindName = "process"


ALL_TRAFFIC: Final = "0.0.0.0/0"


class EgressEnforcement(StrEnum):
    """Cómo aplica `rayd` la política de egress en el guest (ADR-012), tal
    como lo publican `Health.egress_enforcement` y `NetworkState.enforcement`.
    `UNSPECIFIED` es un agente anterior a M9 y cuenta como `NONE`: ninguna
    política en el guest, sólo el conector de la plataforma."""

    UNSPECIFIED = "unspecified"
    NONE = "none"
    GUEST_ROUTES = "guest_routes"
    GUEST_ROUTES_AND_PROXY = "guest_routes_and_proxy"


@dataclass(frozen=True)
class EgressProxy:
    """Proxy SOCKS5 del operador (`egress_proxy` de E2B): `address` es
    `host:puerto` o `[IPv6]:puerto`; las credenciales RFC 1929 sólo viajan
    en `UpdateNetwork` y el `repr` nunca muestra la contraseña."""

    address: str
    username: str | None = None
    password: str | None = field(default=None, repr=False)


def empty_rules() -> Mapping[str, tuple[object, ...]]:
    return MappingProxyType({})


@dataclass(frozen=True)
class NetworkSelectorContext:
    """Lo que recibe un selector invocable de `allow_out`/`deny_out`, con los
    nombres de atributo de E2B (`ctx.all_traffic`, `ctx.rules`). `rules`
    siempre está vacío: Rayito no tiene transformaciones de peticiones."""

    all_traffic: str = ALL_TRAFFIC
    rules: Mapping[str, tuple[object, ...]] = field(default_factory=empty_rules)


NetworkSelector: TypeAlias = Sequence[str] | Callable[[NetworkSelectorContext], Sequence[str]]


class NetworkOptions(TypedDict, total=False):
    """`network=` como dict, con las claves de E2B."""

    allow_out: NetworkSelector
    deny_out: NetworkSelector
    egress_proxy: EgressProxy | Mapping[str, str]


@dataclass(frozen=True)
class NetworkPolicy:
    """Política de egress resuelta (selectores ya evaluados). Una entrada
    permitida gana siempre a una denegada; sin `deny_out` ni `egress_proxy`
    no restringe nada."""

    allow_out: tuple[str, ...] = ()
    deny_out: tuple[str, ...] = ()
    egress_proxy: EgressProxy | None = None


@dataclass(frozen=True)
class NetworkState:
    """`NetworkService.GetNetwork`/`UpdateNetwork`: las listas tal como se
    enviaron y cómo se aplican. Nunca incluye la dirección ni las
    credenciales del proxy; `local_proxy_port` es `None` cuando el proxy
    local de `rayd` no está corriendo."""

    allow_out: tuple[str, ...]
    deny_out: tuple[str, ...]
    egress_proxy_configured: bool
    enforcement: EgressEnforcement
    local_proxy_port: int | None


@dataclass(frozen=True)
class SandboxHealth:
    """`HealthService.Health` tal como lo expone `get_health()`.

    `resume_generation` cuenta los `/resume` aceptados desde el arranque del
    agente; `clock_offset_ms` es `wall_delta - monotonic_delta` entre el
    último `/suspend` y su `/resume` (0 si no hubo); `kernel_state_lost` es
    `True` cuando algún kernel no respondió a la sonda de `/resume` y fue
    reiniciado (su estado se perdió). `uptime_ms` incluye el tiempo suspendido.
    `metadata` es el mapa del `runHookPayload` tal cual lo guardó `rayd`
    (vacío antes de `/run`, si el payload no lo traía o si la imagen es
    anterior a M6). `imds_blocked` es `True` sólo cuando `rayd` instaló y
    verificó la regla de `iptables` que bloquea IMDS para uid 1000 (imagen
    `rayito-base-caps`; `False` en la imagen por defecto, fail-open).
    `hook_anomalies` cuenta las llamadas anómalas a los hooks en este
    arranque (`/run` repetido, `/suspend`/`/resume` rechazados por el
    limitador, recuperaciones de un `/suspend` que nunca congeló la VM):
    distinto de 0 significa que alguien posee un token `allPorts` de la VM.
    `lifecycle` es el plazo lógico (ADR-011); `None` en un agente anterior
    a M9, que no impone ningún timeout. `egress_enforcement` es la política
    de egress que `rayd` verificó en el guest (ADR-012); `UNSPECIFIED` en un
    agente anterior a M9. `cpu_count` y `memory_total_bytes` son la vista del
    guest (CPUs que `rayd` puede usar y `MemTotal` en bytes), 0 en un agente
    anterior a M9.
    """

    agent_ready: bool
    kernel_ready: bool
    agent_version: str
    uptime_ms: int
    sandbox_id: str
    resume_generation: int
    clock_offset_ms: int
    kernel_state_lost: bool
    metadata: dict[str, str] = field(default_factory=dict)
    imds_blocked: bool = False
    hook_anomalies: int = 0
    lifecycle: SandboxLifecycle | None = None
    egress_enforcement: EgressEnforcement = EgressEnforcement.UNSPECIFIED
    cpu_count: int = 0
    memory_total_bytes: int = 0


@dataclass(frozen=True)
class SandboxMetrics:
    """Una muestra procfs del MicroVM: la instantánea de `get_metrics()` o un
    punto de `get_metrics_history()`. `mem_cache_bytes` es el `Cached` de
    `/proc/meminfo` (page cache), 0 en un agente anterior a M9."""

    cpu_used_pct: float
    mem_used_bytes: int
    mem_total_bytes: int
    disk_used_bytes: int
    disk_total_bytes: int
    cpu_count: int
    timestamp: datetime
    mem_cache_bytes: int = 0


class FileType(StrEnum):
    """Tipo de una entrada del sistema de ficheros del sandbox."""

    FILE = "file"
    DIR = "dir"
    SYMLINK = "symlink"


@dataclass(frozen=True)
class EntryInfo:
    """`EntryInfo` de `FilesystemService`, sin seguir enlaces simbólicos.

    `type` es `None` para lo que no es fichero regular, directorio ni symlink
    (FIFO, socket, dispositivo). `path` es la ruta pedida normalizada, nunca
    la canónica. `permissions` tiene la forma de `ls -l` (`-rw-r--r--`) y
    `mode` son los bits `st_mode & 0o7777`. `symlink_target` sólo en symlinks.
    `metadata` son los metadatos de `files.write(metadata=)` (xattrs
    `user.rayito.*`, claves en minúsculas), de sólo lectura y vacíos si no
    hay o si la imagen es anterior a M9.
    """

    name: str
    type: FileType | None
    path: str
    size: int
    mode: int
    permissions: str
    owner: str
    group: str
    modified_time: datetime
    symlink_target: str | None = None
    metadata: Mapping[str, str] = field(default_factory=lambda: frozen_mapping({}), hash=False)


class FilesystemEventType(StrEnum):
    """Tipos de evento de `watch_dir` (misma tabla que E2B)."""

    CREATE = "create"
    WRITE = "write"
    REMOVE = "remove"
    RENAME = "rename"
    CHMOD = "chmod"


@dataclass(frozen=True)
class FilesystemEvent:
    """Un evento de `watch_dir`: `name` es relativo al directorio observado
    (`sub/b.txt` en modo recursivo) y `entry` sólo llega con `include_entry`
    en `CREATE`/`WRITE`/`CHMOD`."""

    name: str
    type: FilesystemEventType
    entry: EntryInfo | None = None


@dataclass(frozen=True)
class WriteEntry:
    """Un fichero de `files.write_files`: `data` se materializa en memoria."""

    path: str
    data: str | bytes | IO[bytes] | IO[str]
    mode: int | None = None


@dataclass(frozen=True)
class CodeContext:
    """Un contexto de `CodeService`: un kernel con su propio scope y cwd."""

    id: str
    language: str
    cwd: str

    @classmethod
    def from_json(cls, data: Mapping[str, str]) -> CodeContext:
        """El inverso del diccionario de E2B (`id`, `language`, `cwd`); una
        clave ausente es `InvalidArgumentException` que la nombra."""
        return cls(
            id=required_json_key(data, "id"),
            language=required_json_key(data, "language"),
            cwd=required_json_key(data, "cwd"),
        )


def required_json_key(data: Mapping[str, str], key: str) -> str:
    if key not in data:
        raise InvalidArgumentException(f"falta la clave '{key}' en el JSON del contexto")
    return str(data[key])


@dataclass(frozen=True)
class OutputMessage:
    """Un `OutputChunk` de `Execute`: `timestamp` en ns Unix del sidecar y
    `error=True` cuando vino por stderr. Un chunk no es una línea."""

    line: str
    timestamp: int
    error: bool = False

    def __str__(self) -> str:
        return self.line


@dataclass
class Logs:
    """Salida de una ejecución, un elemento por `OutputChunk` recibido."""

    stdout: list[str] = field(default_factory=list)
    stderr: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps({"stdout": list(self.stdout), "stderr": list(self.stderr)})


@dataclass(frozen=True)
class ExecutionError:
    """Error del kernel (`ZeroDivisionError`...) o sintético del agente
        (`ExecutionTimeout`, `KernelDied`, `KernelRestarted`, `ContextDestroyed`,
        `ExecutionAborted`, `OutputTruncated`); `traceback` son las líneas de
        Jupyter unidas con `"
    "`, vacío en los sintéticos."""

    name: str
    value: str
    traceback: str = ""

    def to_json(self) -> str:
        return json.dumps(execution_error_to_dict(self))


RESULT_FORMAT_ORDER: Final = (
    "text",
    "html",
    "markdown",
    "svg",
    "png",
    "jpeg",
    "pdf",
    "latex",
    "javascript",
    "json",
    "data",
    "chart",
)


@dataclass(frozen=True, kw_only=True, repr=False)
class Result:
    """Un mime bundle de Jupyter (`display_data` o `execute_result`).

    Las imágenes (`png`, `jpeg`, `pdf`) son base64 tal cual llegan; `json` y
    `data` son el documento parseado (o la cadena original si no era JSON
    válido); `chart` es el `Chart` de `e2b/chart`. `raw` conserva cada mime
    como cadena y `extra` los mime types sin campo propio (incluido
    `rayito/omitted` cuando el sidecar descartó un valor de más de 8 MiB).
    """

    text: str | None = None
    html: str | None = None
    markdown: str | None = None
    svg: str | None = None
    png: str | None = None
    jpeg: str | None = None
    pdf: str | None = None
    latex: str | None = None
    javascript: str | None = None
    json: Any = None
    data: Any = None
    chart: Chart | None = None
    is_main_result: bool = False
    extra: dict[str, str] = field(default_factory=dict)
    raw: dict[str, str] = field(default_factory=dict)

    def formats(self) -> list[str]:
        """Campos presentes, en el orden de `RESULT_FORMAT_ORDER`, y después
        las claves de `extra` (ordenadas alfabéticamente al leerlas del proto,
        porque el orden de un mapa protobuf no está garantizado)."""
        present = [name for name in RESULT_FORMAT_ORDER if getattr(self, name) is not None]
        return [*present, *self.extra]

    def __str__(self) -> str:
        return self.text or ""

    def __repr__(self) -> str:
        return f"Result(formats={self.formats()!r}, is_main_result={self.is_main_result!r})"

    def _repr_html_(self) -> str | None:
        return self.html

    def _repr_markdown_(self) -> str | None:
        return self.markdown

    def _repr_svg_(self) -> str | None:
        return self.svg

    def _repr_png_(self) -> str | None:
        return self.png

    def _repr_jpeg_(self) -> str | None:
        return self.jpeg

    def _repr_pdf_(self) -> str | None:
        return self.pdf

    def _repr_latex_(self) -> str | None:
        return self.latex

    def _repr_json_(self) -> Any:
        return self.json

    def _repr_javascript_(self) -> str | None:
        return self.javascript


@dataclass
class Execution:
    """Resultado de `run_code`. Un error del kernel es dato (`error`), nunca
    excepción; `execution_count` es el de Jupyter (`None` si el kernel no
    llegó a empezar la celda)."""

    results: list[Result] = field(default_factory=list)
    logs: Logs = field(default_factory=Logs)
    error: ExecutionError | None = None
    execution_count: int | None = None

    @property
    def text(self) -> str | None:
        """`text/plain` del `execute_result` (la última expresión de la celda)."""
        for result in self.results:
            if result.is_main_result:
                return result.text
        return None

    def to_json(self) -> str:
        """El documento de E2B: `logs` va anidado como la cadena JSON de
        `Logs.to_json()`, no como objeto."""
        return json.dumps(
            {
                "results": [
                    {"is_main_result": result.is_main_result, **result.raw}
                    for result in self.results
                ],
                "logs": self.logs.to_json(),
                "error": None if self.error is None else execution_error_to_dict(self.error),
                "execution_count": self.execution_count,
            }
        )


def execution_error_to_dict(error: ExecutionError) -> dict[str, str]:
    return {"name": error.name, "value": error.value, "traceback": error.traceback}


def template_name_from_arn(image_arn: str) -> str:
    return image_arn.rsplit(":", 1)[-1]


def validate_port(port: object, *, field: str = "port") -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not PORT_MIN <= port <= PORT_MAX:
        raise InvalidArgumentException(
            f"{field} debe ser un entero entre {PORT_MIN} y {PORT_MAX}, recibido {port!r}"
        )
    return port


# ------------------------------------------------------------- persistence

S3_KEY_SAFE_CHARS: Final = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!_.*'()-/"
)
S3_BUCKET_CHARS: Final = frozenset("abcdefghijklmnopqrstuvwxyz0123456789.-")
PERSIST_ARCHIVE_KEY: Final = "home.tar.gz"
PERSIST_MANIFEST_KEY: Final = "manifest.json"


def validate_bucket_name(bucket: object) -> str:
    """Reglas de D2 (las mismas que aplica `rayd`): 3-63 caracteres, `[a-z0-9.-]`,
    empieza y termina en letra o dígito, sin `..`, no una IPv4 literal."""
    if not isinstance(bucket, str):
        raise InvalidArgumentException(f"bucket debe ser str, recibido {bucket!r}")
    if not S3_BUCKET_NAME_MIN <= len(bucket) <= S3_BUCKET_NAME_MAX:
        raise InvalidArgumentException(
            f"bucket debe tener entre {S3_BUCKET_NAME_MIN} y {S3_BUCKET_NAME_MAX} caracteres"
        )
    if any(char not in S3_BUCKET_CHARS for char in bucket):
        raise InvalidArgumentException("bucket sólo admite minúsculas, dígitos, `.` y `-`")
    if not (bucket[0].isalnum() and bucket[-1].isalnum()):
        raise InvalidArgumentException("bucket debe empezar y terminar en letra o dígito")
    if ".." in bucket:
        raise InvalidArgumentException("bucket no puede contener `..`")
    octets = bucket.split(".")
    if len(octets) == 4 and all(octet.isdigit() for octet in octets):
        raise InvalidArgumentException("bucket no puede parecer una dirección IPv4")
    return bucket


def validate_key_prefix(key_prefix: object, *, field: str = "prefix") -> str:
    """Reglas de D2: no vacío, <= 900 bytes UTF-8, sin `/` inicial ni final, sin
    componentes vacíos, `.` o `..`, sólo el conjunto seguro de S3."""
    if not isinstance(key_prefix, str) or not key_prefix:
        raise InvalidArgumentException(f"{field} debe ser una cadena no vacía")
    if len(key_prefix.encode("utf-8")) > PERSIST_KEY_PREFIX_MAX_BYTES:
        raise InvalidArgumentException(
            f"{field} no puede superar {PERSIST_KEY_PREFIX_MAX_BYTES} bytes"
        )
    if key_prefix.startswith("/") or key_prefix.endswith("/"):
        raise InvalidArgumentException(f"{field} no puede empezar ni terminar en `/`")
    for component in key_prefix.split("/"):
        if component == "":
            raise InvalidArgumentException(f"{field} no puede contener `//`")
        if component in (".", ".."):
            raise InvalidArgumentException(f"{field} no puede contener componentes `.` o `..`")
    if any(char not in S3_KEY_SAFE_CHARS for char in key_prefix):
        raise InvalidArgumentException(
            f"{field} sólo admite letras, dígitos y `!_.*'()-/` (conjunto seguro de S3)"
        )
    return key_prefix


@dataclass(frozen=True)
class S3Prefix:
    """Dónde vive un `HOME` persistido: `s3://bucket/prefix/name/`.

    `prefix` es la base del operador (la que acota la política IAM del
    execution role, `infra/iam.yaml`); `name` identifica un home concreto y
    `Sandbox.create(persist=)` lo rellena con el `sandbox_id` si viene vacío.
    `region` sólo si el bucket no está en la región del sandbox. Se valida en
    cliente con las mismas reglas que aplica `rayd`.
    """

    bucket: str
    prefix: str = "rayito"
    name: str | None = None
    region: str | None = None

    def __post_init__(self) -> None:
        validate_bucket_name(self.bucket)
        validate_key_prefix(self.prefix)
        if self.name is not None:
            validate_key_prefix(f"{self.prefix}/{self.name}", field="name")
        if self.region is not None and not self.region:
            raise InvalidArgumentException("region no puede ser una cadena vacía")

    @property
    def key_prefix(self) -> str:
        """`prefix/name`; `InvalidArgumentException` mientras `name` sea `None`."""
        if self.name is None:
            raise InvalidArgumentException(
                "S3Prefix sin name: create(persist=) lo fija al sandbox_id; para "
                "connect()/restore explícito pásalo (S3Prefix(..., name=...))"
            )
        return f"{self.prefix}/{self.name}"

    @property
    def archive_key(self) -> str:
        return f"{self.key_prefix}/{PERSIST_ARCHIVE_KEY}"

    @property
    def manifest_key(self) -> str:
        return f"{self.key_prefix}/{PERSIST_MANIFEST_KEY}"

    @property
    def uri(self) -> str:
        return f"s3://{self.bucket}/{self.key_prefix}"

    def with_name(self, name: str) -> S3Prefix:
        return S3Prefix(bucket=self.bucket, prefix=self.prefix, name=name, region=self.region)


@dataclass(frozen=True)
class CheckpointProgress:
    """Un `CheckpointProgress` del agente (como mucho uno por segundo)."""

    files_done: int
    bytes_read: int
    bytes_uploaded: int


@dataclass(frozen=True)
class CheckpointResult:
    """`CheckpointDone`: qué se archivó y dónde. `sha256` es el del `home.tar.gz`."""

    bucket: str
    key_prefix: str
    files: int
    bytes_read: int
    archive_bytes: int
    sha256: str
    skipped: int
    duration: float

    @property
    def uri(self) -> str:
        return f"s3://{self.bucket}/{self.key_prefix}"


@dataclass(frozen=True)
class RestoreProgress:
    files_done: int
    bytes_downloaded: int


@dataclass(frozen=True)
class RestoreResult:
    """`RestoreDone`: qué se extrajo en el `HOME` del sandbox."""

    bucket: str
    key_prefix: str
    files: int
    bytes_written: int
    archive_bytes: int
    sha256: str
    skipped: int
    duration: float

    @property
    def uri(self) -> str:
        return f"s3://{self.bucket}/{self.key_prefix}"


@dataclass(frozen=True, repr=False)
class LaunchOptions:
    """Lo que `create()` recibió, guardado en la instancia para que
    `reincarnate()` lance el sandbox siguiente igual (menos el token de acceso
    salvo que fuera explícito). `template` es el ARN ya resuelto.

    `access_token` y los valores de `envs` son secretos y se redactan en
    `repr`/`str`: sólo se muestra si hay token y cuántas variables hay.
    """

    template: str
    template_version: str | None
    timeout: int
    idle: IdlePolicy | None
    envs: dict[str, str] | None
    metadata: dict[str, str] | None
    cpu_time_limit: int | None
    execution_role_arn: str | None
    allowed_ports: tuple[Any, ...] | None
    ingress: tuple[str, ...] | None
    egress: tuple[str, ...] | None
    logging: Any
    access_token: str | None
    ready_timeout: float
    request_timeout: float
    reconnect_timeout: float
    keep_on_failure: bool
    control_plane: Any
    transport: Any
    max_lifetime: int | None = None
    on_timeout: TimeoutActionName | None = None
    network: NetworkPolicy | None = None

    def __repr__(self) -> str:
        token = None if self.access_token is None else REDACTED
        env_count = 0 if self.envs is None else len(self.envs)
        return (
            f"LaunchOptions(template={self.template!r}, "
            f"template_version={self.template_version!r}, timeout={self.timeout!r}, "
            f"access_token={token!r}, envs=<{env_count} keys>)"
        )


# ------------------------------------------------------------------ transfers

TRANSFER_BUCKET_ENV_VAR: Final = "RAYITO_TRANSFER_BUCKET"
TRANSFER_PREFIX_ENV_VAR: Final = "RAYITO_TRANSFER_PREFIX"
TRANSFER_REGION_ENV_VAR: Final = "RAYITO_TRANSFER_REGION"
TRANSFER_PREFIX_MAX_BYTES: Final = 256
ARTIFACT_NAMESPACE: Final = "rayito"
DNS_BUCKET_PATTERN: Final = re.compile(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]")
PREFIX_SEGMENT_PATTERN: Final = re.compile(r"[A-Za-z0-9_.-]+")
REGION_PATTERN: Final = re.compile(r"[a-z]{2}(-gov)?-[a-z]+-[0-9]")

TransferDirectionName = Literal["import", "export"]
TransferPhaseName = Literal["waiting", "running", "done", "failed", "cancelled"]
UploadMethod = Literal["PUT", "POST"]


def frozen_mapping(values: Mapping[str, str]) -> Mapping[str, str]:
    """Copia de sólo lectura: un `EntryInfo` congelado no expone un dict mutable."""
    return MappingProxyType(dict(values))


def prefixes_overlap(first: str, second: str) -> bool:
    """Dos prefijos de clave se pisan cuando uno es prefijo del otro por
    componentes (`data` y `data/tmp`, pero no `data` y `database`)."""
    first_parts = first.split("/")
    second_parts = second.split("/")
    shared = min(len(first_parts), len(second_parts))
    return first_parts[:shared] == second_parts[:shared]


def validate_transfer_bucket(bucket: object) -> str:
    """Nombre DNS sin puntos: los certificados comodín del host virtual de S3
    nunca cubren un bucket con puntos. El mensaje nunca repite el valor."""
    if not isinstance(bucket, str) or DNS_BUCKET_PATTERN.fullmatch(bucket) is None:
        raise InvalidArgumentException(
            "S3Staging.bucket debe ser un nombre DNS sin puntos: 3-63 minúsculas, dígitos o "
            "`-`, empezando y terminando en letra o dígito"
        )
    if bucket.startswith("xn--") or bucket.endswith("-s3alias"):
        raise InvalidArgumentException(
            "S3Staging.bucket no puede empezar por `xn--` ni terminar en `-s3alias`"
        )
    return bucket


def validate_transfer_prefix(prefix: object) -> str:
    """Segmentos `[A-Za-z0-9_.-]` unidos por `/`, 1-256 bytes, disjunto por
    componentes del espacio de artefactos `rayito` (la regla de ciclo de vida
    de un día del prefijo de transferencia los borraría)."""
    if not isinstance(prefix, str) or not prefix:
        raise InvalidArgumentException("S3Staging.prefix debe ser una cadena no vacía")
    if len(prefix.encode("utf-8")) > TRANSFER_PREFIX_MAX_BYTES:
        raise InvalidArgumentException(
            f"S3Staging.prefix no puede superar {TRANSFER_PREFIX_MAX_BYTES} bytes"
        )
    segments = prefix.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise InvalidArgumentException(
            "S3Staging.prefix no admite `/` al principio o al final, `//` ni segmentos `.` o `..`"
        )
    if any(PREFIX_SEGMENT_PATTERN.fullmatch(segment) is None for segment in segments):
        raise InvalidArgumentException(
            "S3Staging.prefix sólo admite letras, dígitos, `_`, `.` y `-` entre `/`"
        )
    if prefixes_overlap(prefix, ARTIFACT_NAMESPACE):
        raise InvalidArgumentException(
            "S3Staging.prefix no puede solaparse con el espacio de artefactos `rayito`"
        )
    return prefix


def validate_transfer_region(region: object) -> str | None:
    if region is None:
        return None
    if not isinstance(region, str) or REGION_PATTERN.fullmatch(region) is None:
        raise InvalidArgumentException("S3Staging.region no es un código de región de AWS válido")
    return region


def validate_int_between(value: object, *, field: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise InvalidArgumentException(f"{field} debe ser un entero entre {low} y {high}")
    return value


@dataclass(frozen=True)
class S3Staging:
    """El bucket de transferencias de `upload_url`/`download_url` y de los
    ficheros grandes (ADR-010). El SDK firma cada URL con las credenciales
    del llamante; `rayd` nunca guarda ninguna.

    `region=None` usa la región del sandbox y debe ser la del bucket.
    `max_expires_in` topa la vida de las URLs de usuario (el techo real es
    también la caducidad de tus credenciales). `threshold_bytes` es el
    tamaño a partir del cual `files.write`/`files.read` van por S3 y
    `multipart_threshold_bytes` el de una exportación multiparte. No hay
    bucket por defecto: `from_env()` lee `RAYITO_TRANSFER_BUCKET`,
    `RAYITO_TRANSFER_PREFIX` y `RAYITO_TRANSFER_REGION`.
    """

    bucket: str
    prefix: str = TRANSFER_DEFAULT_PREFIX
    region: str | None = None
    max_expires_in: int = TRANSFER_DEFAULT_MAX_EXPIRES_IN_SECONDS
    threshold_bytes: int = TRANSFER_DEFAULT_THRESHOLD_BYTES
    multipart_threshold_bytes: int = TRANSFER_DEFAULT_MULTIPART_THRESHOLD_BYTES

    def __post_init__(self) -> None:
        validate_transfer_bucket(self.bucket)
        validate_transfer_prefix(self.prefix)
        validate_transfer_region(self.region)
        validate_int_between(
            self.max_expires_in,
            field="S3Staging.max_expires_in",
            low=1,
            high=TRANSFER_PRESIGN_MAX_SECONDS,
        )
        validate_int_between(
            self.threshold_bytes,
            field="S3Staging.threshold_bytes",
            low=TRANSFER_THRESHOLD_MIN_BYTES,
            high=TRANSFER_SINGLE_PUT_MAX_BYTES,
        )
        validate_int_between(
            self.multipart_threshold_bytes,
            field="S3Staging.multipart_threshold_bytes",
            low=TRANSFER_MULTIPART_THRESHOLD_MIN_BYTES,
            high=TRANSFER_SINGLE_PUT_MAX_BYTES,
        )

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> S3Staging | None:
        """`None` cuando `RAYITO_TRANSFER_BUCKET` falta o está vacía; los
        campos que no vienen del entorno conservan su valor por defecto."""
        source = os.environ if environ is None else environ
        bucket = source.get(TRANSFER_BUCKET_ENV_VAR) or None
        if bucket is None:
            return None
        prefix = source.get(TRANSFER_PREFIX_ENV_VAR) or TRANSFER_DEFAULT_PREFIX
        region = source.get(TRANSFER_REGION_ENV_VAR) or None
        return cls(bucket=bucket, prefix=prefix, region=region)


@dataclass(frozen=True)
class TransferStatus:
    """Una foto de `GetTransfer`: `bytes_total` es 0 hasta que la importación
    ve el objeto; `probes` cuenta los sondeos del GET; `error_code` y
    `error_reason` sólo en `failed`/`cancelled`."""

    transfer_id: str
    direction: TransferDirectionName
    phase: TransferPhaseName
    bytes_done: int
    bytes_total: int
    probes: int
    error_code: str | None = None
    error_reason: str | None = None


class TicketOperations(Protocol):
    """Lo que un `UploadTicket` delega en el sandbox que lo emitió."""

    def wait_transfer(self, transfer_id: str, timeout: float | None) -> EntryInfo: ...

    def transfer_status(self, transfer_id: str) -> TransferStatus: ...

    def cancel_transfer(self, transfer_id: str) -> None: ...


class AsyncTicketOperations(Protocol):
    """Lo que un `AsyncUploadTicket` delega en el `AsyncSandbox` que lo emitió."""

    async def wait_transfer(self, transfer_id: str, timeout: float | None) -> EntryInfo: ...

    async def transfer_status(self, transfer_id: str) -> TransferStatus: ...

    async def cancel_transfer(self, transfer_id: str) -> None: ...


class SignedUrl(str):
    """Un `str` cuyo valor es una URL prefirmada de S3, para que
    `requests.put(url, ...)` y `urlopen(url)` funcionen como en E2B.

    La URL es una credencial al portador hasta `expires_at`: `repr` nunca la
    muestra y no se puede serializar con `pickle` (está ligada a un sandbox
    vivo y a una firma). `str(url)` y `url.url` sí la devuelven: no la
    registres en logs.
    """

    __slots__ = ("_expires_at", "_path", "_transfer_id")

    _expires_at: datetime
    _path: str
    _transfer_id: str

    def __new__(cls, url: str, *, path: str, expires_at: datetime, transfer_id: str) -> Self:
        instance = super().__new__(cls, url)
        instance._path = path
        instance._expires_at = expires_at
        instance._transfer_id = transfer_id
        return instance

    @property
    def url(self) -> str:
        return str.__str__(self)

    @property
    def path(self) -> str:
        return self._path

    @property
    def expires_at(self) -> datetime:
        return self._expires_at

    @property
    def transfer_id(self) -> str:
        return self._transfer_id

    def __reduce__(self) -> NoReturn:
        raise TypeError(f"{type(self).__name__} no es serializable: es una credencial viva")


class UploadTicketBase(SignedUrl):
    """Campos comunes de `UploadTicket` y `AsyncUploadTicket`: `method` es
    `PUT` (cuerpo crudo, con `headers`) o `POST` (formulario multiparte con
    `fields` y el fichero en el campo `file`)."""

    __slots__ = ("_fields", "_headers", "_method")

    _method: UploadMethod
    _headers: dict[str, str]
    _fields: dict[str, str]

    def __new__(
        cls,
        url: str,
        *,
        method: UploadMethod,
        headers: Mapping[str, str],
        fields: Mapping[str, str],
        path: str,
        expires_at: datetime,
        transfer_id: str,
    ) -> Self:
        instance = super().__new__(
            cls, url, path=path, expires_at=expires_at, transfer_id=transfer_id
        )
        instance._method = method
        instance._headers = dict(headers)
        instance._fields = dict(fields)
        return instance

    @property
    def method(self) -> UploadMethod:
        return self._method

    @property
    def headers(self) -> dict[str, str]:
        return dict(self._headers)

    @property
    def fields(self) -> dict[str, str]:
        return dict(self._fields)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(path={self.path!r}, method={self.method!r}, "
            f"expires_at={self.expires_at.isoformat()!r})"
        )


class UploadTicket(UploadTicketBase):
    """Resultado de `files.upload_url`: la importación ya está armada en
    `rayd` antes de que la URL exista para el caller, y es de un solo uso.

    `wait()` sigue la transferencia hasta su final y devuelve el `EntryInfo`
    escrito (o levanta la excepción mapeada); con `timeout` agotado levanta
    `TimeoutException` y el ticket sigue armado. `status()` es una foto y
    `cancel()` la cancela (idempotente).
    """

    __slots__ = ("_operations",)

    _operations: TicketOperations

    def __new__(
        cls,
        url: str,
        *,
        method: UploadMethod,
        headers: Mapping[str, str],
        fields: Mapping[str, str],
        path: str,
        expires_at: datetime,
        transfer_id: str,
        operations: TicketOperations,
    ) -> Self:
        instance = super().__new__(
            cls,
            url,
            method=method,
            headers=headers,
            fields=fields,
            path=path,
            expires_at=expires_at,
            transfer_id=transfer_id,
        )
        instance._operations = operations
        return instance

    def wait(self, timeout: float | None = None) -> EntryInfo:
        return self._operations.wait_transfer(self.transfer_id, timeout)

    def status(self) -> TransferStatus:
        return self._operations.transfer_status(self.transfer_id)

    def cancel(self) -> None:
        self._operations.cancel_transfer(self.transfer_id)


class AsyncUploadTicket(UploadTicketBase):
    """`UploadTicket` de `AsyncSandbox`: `wait()`, `status()` y `cancel()`
    son corrutinas."""

    __slots__ = ("_operations",)

    _operations: AsyncTicketOperations

    def __new__(
        cls,
        url: str,
        *,
        method: UploadMethod,
        headers: Mapping[str, str],
        fields: Mapping[str, str],
        path: str,
        expires_at: datetime,
        transfer_id: str,
        operations: AsyncTicketOperations,
    ) -> Self:
        instance = super().__new__(
            cls,
            url,
            method=method,
            headers=headers,
            fields=fields,
            path=path,
            expires_at=expires_at,
            transfer_id=transfer_id,
        )
        instance._operations = operations
        return instance

    async def wait(self, timeout: float | None = None) -> EntryInfo:
        return await self._operations.wait_transfer(self.transfer_id, timeout)

    async def status(self) -> TransferStatus:
        return await self._operations.transfer_status(self.transfer_id)

    async def cancel(self) -> None:
        await self._operations.cancel_transfer(self.transfer_id)


class DownloadLink(SignedUrl):
    """Resultado de `files.download_url`: una foto del fichero tomada al
    llamar (`size` y `sha256` de los bytes exportados), servida por S3 hasta
    `expires_at` con `Content-Disposition: attachment`."""

    __slots__ = ("_sha256", "_size")

    _size: int
    _sha256: str

    def __new__(
        cls,
        url: str,
        *,
        path: str,
        expires_at: datetime,
        transfer_id: str,
        size: int,
        sha256: str,
    ) -> Self:
        instance = super().__new__(
            cls, url, path=path, expires_at=expires_at, transfer_id=transfer_id
        )
        instance._size = size
        instance._sha256 = sha256
        return instance

    @property
    def size(self) -> int:
        return self._size

    @property
    def sha256(self) -> str:
        return self._sha256

    def __repr__(self) -> str:
        return (
            f"DownloadLink(path={self.path!r}, size={self.size}, "
            f"expires_at={self.expires_at.isoformat()!r})"
        )
