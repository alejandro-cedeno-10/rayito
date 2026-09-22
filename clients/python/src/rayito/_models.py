"""Modelos públicos del SDK: dataclasses sin I/O."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import IO, Any, Final, Literal

from rayito._charts import Chart
from rayito._limits import (
    IDLE_MAX_IDLE_MIN_SECONDS,
    IDLE_SUSPENDED_MIN_SECONDS,
    PERSIST_KEY_PREFIX_MAX_BYTES,
    PORT_MAX,
    PORT_MIN,
    S3_BUCKET_NAME_MAX,
    S3_BUCKET_NAME_MIN,
)
from rayito.exceptions import InvalidArgumentException

REDACTED: Final = "<redacted>"
PROXY_AUTH_HEADER = "x-aws-proxy-auth"
PROXY_PORT_HEADER = "x-aws-proxy-port"


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


@dataclass(frozen=True)
class SandboxInfo:
    """Lo que devuelve `get-microvm` (y `run-microvm`), normalizado.

    `endpoint` es siempre el hostname pelado; `endpoint_url` le antepone
    `https://`. `template` es el ARN de la imagen. `metadata` es `None`
    cuando no se leyó del agente (`get-microvm` no lo conoce) y `{}` cuando
    se leyó y estaba vacío. `ingress`/`egress` son los ARNs de conectores
    que `run-microvm`/`get-microvm` reportan (`ingressNetworkConnectors` /
    `egressNetworkConnectors`), vacíos si la respuesta no los trae.
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

    @property
    def endpoint_url(self) -> str:
        return f"https://{self.endpoint}"

    @property
    def expires_at(self) -> datetime:
        return self.started_at + timedelta(seconds=self.maximum_duration_seconds)

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


@dataclass(frozen=True)
class SandboxMetrics:
    """`HealthService.Metrics`: instantánea procfs del MicroVM."""

    cpu_used_pct: float
    mem_used_bytes: int
    mem_total_bytes: int
    disk_used_bytes: int
    disk_total_bytes: int
    cpu_count: int
    timestamp: datetime


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
        las claves de `extra`."""
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
        return json.dumps(
            {
                "results": [
                    {"is_main_result": result.is_main_result, **result.raw}
                    for result in self.results
                ],
                "logs": {"stdout": list(self.logs.stdout), "stderr": list(self.logs.stderr)},
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
    execution role, `spike/m0/iam.yaml`); `name` identifica un home concreto y
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

    def __repr__(self) -> str:
        token = None if self.access_token is None else REDACTED
        env_count = 0 if self.envs is None else len(self.envs)
        return (
            f"LaunchOptions(template={self.template!r}, "
            f"template_version={self.template_version!r}, timeout={self.timeout!r}, "
            f"access_token={token!r}, envs=<{env_count} keys>)"
        )
