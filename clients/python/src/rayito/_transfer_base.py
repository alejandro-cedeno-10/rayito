"""Núcleo puro de las transferencias por S3 (ADR-010), compartido por
`Sandbox` y `AsyncSandbox`: resolución y validación de `S3Staging`, claves de
staging, vidas de las URLs prefirmadas, plan multiparte, enrutado de ficheros
grandes, construcción de los cinco RPCs de transferencia, conversión de
`TransferState` y la tabla de errores de D13. Sin I/O de red: las URLs las
firma `rayito._s3.S3Gateway` y los RPCs los hacen los árboles sync y async.

Nada aquí registra ni devuelve en un mensaje una URL, un bucket, una clave
o una ruta: una URL prefirmada es una credencial al portador.
"""

from __future__ import annotations

import hashlib
import io
import math
import secrets
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import IO, Any, Final, Literal

import grpc
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

from rayito._filesystem_base import (
    PreparedWrite,
    coerce_data,
    file_request_deadline,
    validate_mode,
    validate_path,
)
from rayito._limits import (
    TRANSFER_DEFAULT_EXPIRES_IN_SECONDS,
    TRANSFER_INTERNAL_EXPIRES_IN_SECONDS,
    TRANSFER_PART_SIZE_MIN_BYTES,
    TRANSFER_PRESIGN_MAX_SECONDS,
    TRANSFER_SINGLE_PUT_MAX_BYTES,
)
from rayito._models import (
    S3Prefix,
    S3Staging,
    TransferDirectionName,
    TransferPhaseName,
    TransferStatus,
    UploadMethod,
    WriteEntry,
    prefixes_overlap,
)
from rayito._transport import rpc_status, translate_rpc_error
from rayito.exceptions import (
    AuthenticationException,
    DiskFullException,
    FileNotFoundException,
    FileUploadException,
    InvalidArgumentException,
    SandboxException,
    TimeoutException,
    TransferException,
    UnimplementedError,
)
from rayito.v1 import filesystem_pb2

OCTET_STREAM: Final = "application/octet-stream"
CONTENT_TYPE_HEADER: Final = "Content-Type"
UPLOAD_KEY_DIRECTION: Final = "up"
DOWNLOAD_KEY_DIRECTION: Final = "down"
STAGING_TOKEN_BYTES: Final = 16
EXPORT_BUDGET_BASE_SECONDS: Final = 900
EXPORT_BUDGET_BYTES_PER_SECOND: Final = 1_000_000
DELETE_GRACE_SECONDS: Final = 3600
MIB: Final = 1_048_576
PART_SIZE_DIVISOR: Final = 1000
UPLOAD_CHUNK_BYTES: Final = 8 * MIB
UPLOAD_MAX_CONCURRENCY: Final = 8
OBJECT_READ_CHUNK_BYTES: Final = 262_144
CAPABILITY_PROBE_ID: Final = ""
MISSING_STAGING_REASON: Final = "configura transfer=S3Staging(...) o RAYITO_TRANSFER_BUCKET"
OUTDATED_IMAGE_REASON: Final = "actualiza la imagen: este rayd no tiene transferencias"
S3_CREDENTIAL_ERRORS: Final = frozenset(
    {"AccessDenied", "ExpiredToken", "InvalidAccessKeyId", "SignatureDoesNotMatch", "InvalidToken"}
)
S3_REGION_ERRORS: Final = frozenset(
    {"PermanentRedirect", "AuthorizationHeaderMalformed", "IllegalLocationConstraintException"}
)

KeyDirection = Literal["up", "down"]

PHASE_NAMES: Final[dict[int, TransferPhaseName]] = {
    filesystem_pb2.TRANSFER_PHASE_WAITING: "waiting",
    filesystem_pb2.TRANSFER_PHASE_RUNNING: "running",
    filesystem_pb2.TRANSFER_PHASE_DONE: "done",
    filesystem_pb2.TRANSFER_PHASE_FAILED: "failed",
    filesystem_pb2.TRANSFER_PHASE_CANCELLED: "cancelled",
}
DIRECTION_NAMES: Final[dict[int, TransferDirectionName]] = {
    filesystem_pb2.TRANSFER_DIRECTION_IMPORT: "import",
    filesystem_pb2.TRANSFER_DIRECTION_EXPORT: "export",
}
TERMINAL_PHASES: Final = frozenset(
    {
        filesystem_pb2.TRANSFER_PHASE_DONE,
        filesystem_pb2.TRANSFER_PHASE_FAILED,
        filesystem_pb2.TRANSFER_PHASE_CANCELLED,
    }
)


# ------------------------------------------------------------ configuration


def resolve_staging(
    transfer: S3Staging | None, environ: Mapping[str, str] | None = None
) -> S3Staging | None:
    """`transfer=` explícito o, si es `None`, `S3Staging.from_env()`."""
    if transfer is None:
        return S3Staging.from_env(environ)
    if not isinstance(transfer, S3Staging):
        raise InvalidArgumentException(
            f"transfer debe ser S3Staging o None, recibido {type(transfer).__name__}"
        )
    return transfer


def validate_staging_against_persist(transfer: S3Staging | None, persist: S3Prefix | None) -> None:
    """En el mismo bucket, el prefijo de transferencia y el de persistencia
    deben ser disjuntos por componentes: la regla de ciclo de vida de un día
    del primero borraría los checkpoints del segundo."""
    if transfer is None or persist is None or transfer.bucket != persist.bucket:
        return
    if prefixes_overlap(transfer.prefix, persist.prefix):
        raise InvalidArgumentException(
            "transfer.prefix y persist.prefix comparten bucket y se solapan: la regla de ciclo "
            "de vida del prefijo de transferencias borraría los checkpoints; usa prefijos "
            "disjuntos"
        )


def presign_client_config() -> Config:
    """SigV4, host virtual y endpoint regional también en us-east-1: el
    botocore por defecto firma SigV2 contra `s3.amazonaws.com` allí
    (AWS_API_NOTES.md Q62) y `rayd` sólo acepta el host regional."""
    return Config(
        signature_version="s3v4",
        s3={"addressing_style": "virtual", "us_east_1_regional_endpoint": "regional"},
    )


def staging_region(staging: S3Staging, sandbox_region: str) -> str:
    return staging.region or sandbox_region


def missing_staging_error(feature: str) -> UnimplementedError:
    return UnimplementedError(feature, MISSING_STAGING_REASON)


def outdated_image_error(feature: str) -> UnimplementedError:
    return UnimplementedError(feature, OUTDATED_IMAGE_REASON)


def require_staging(staging: S3Staging | None, feature: str) -> S3Staging:
    if staging is None:
        raise missing_staging_error(feature)
    return staging


# -------------------------------------------------------------- staging keys


@dataclass(frozen=True)
class StagingObject:
    """Un objeto de staging: `key` es `<prefix>/<sandbox_id>/<up|down>/<hex>`
    y nunca lleva la ruta del usuario. Se nombra así en cada request a `rayd`
    (`S3Object`) y en cada llamada a S3."""

    bucket: str
    key: str
    region: str

    def to_proto(self) -> filesystem_pb2.S3Object:
        return filesystem_pb2.S3Object(bucket=self.bucket, key=self.key, region=self.region)


@dataclass(frozen=True)
class ImportUrls:
    """Las dos URLs que `rayd` usa en una importación: nunca se registran."""

    get_url: str
    delete_url: str


@dataclass(frozen=True)
class ExportResult:
    """Una exportación terminada: dónde quedó y qué bytes se subieron."""

    target: StagingObject
    transfer_id: str
    sha256: str
    size: int


@dataclass(frozen=True)
class CompletedExport:
    """Una exportación en `DONE` más los ETags de sus partes (vacíos en un
    PUT único), que el SDK necesita para completar la subida multiparte."""

    export: ExportResult
    part_etags: tuple[str, ...]


def completed_export(
    target: StagingObject, transfer_id: str, state: filesystem_pb2.TransferState
) -> CompletedExport:
    return CompletedExport(
        export=ExportResult(
            target=target,
            transfer_id=transfer_id,
            sha256=str(state.sha256),
            size=int(state.bytes_done),
        ),
        part_etags=tuple(str(etag) for etag in state.part_etags),
    )


def new_staging_token() -> str:
    return secrets.token_hex(STAGING_TOKEN_BYTES)


def staging_key(prefix: str, sandbox_id: str, direction: KeyDirection, token: str) -> str:
    return f"{prefix}/{sandbox_id}/{direction}/{token}"


def new_staging_object(
    staging: S3Staging, sandbox_id: str, sandbox_region: str, direction: KeyDirection
) -> StagingObject:
    return StagingObject(
        bucket=staging.bucket,
        key=staging_key(staging.prefix, sandbox_id, direction, new_staging_token()),
        region=staging_region(staging, sandbox_region),
    )


# ------------------------------------------------------------------ lifetimes


def validate_expires_in(expires_in: object, *, field: str = "expires_in") -> int:
    if isinstance(expires_in, bool) or not isinstance(expires_in, int):
        raise InvalidArgumentException(f"{field} debe ser un entero de segundos")
    if expires_in <= 0:
        raise InvalidArgumentException(f"{field} debe ser > 0")
    return expires_in


def effective_expires_in(expires_in: object, staging: S3Staging) -> int:
    """`min(expires_in, max_expires_in, 604800)`: S3 rechaza una firma de más
    de 7 días y el techo real es también la caducidad de las credenciales."""
    return min(
        validate_expires_in(expires_in), staging.max_expires_in, TRANSFER_PRESIGN_MAX_SECONDS
    )


def expires_in_from_signature_expiration(use_signature_expiration: object) -> int:
    """`Sandbox.upload_url/download_url(use_signature_expiration=)` de E2B:
    `None` son 3600 s y `<= 0` no tiene sentido con URLs que siempre caducan."""
    if use_signature_expiration is None:
        return TRANSFER_DEFAULT_EXPIRES_IN_SECONDS
    return validate_expires_in(use_signature_expiration, field="use_signature_expiration")


def capped_lifetime(seconds: int) -> int:
    return min(seconds, TRANSFER_PRESIGN_MAX_SECONDS)


def delete_lifetime(user_lifetime: int) -> int:
    """El DELETE de limpieza vive una hora más que el ticket: la importación
    puede terminar justo al vencer el GET."""
    return capped_lifetime(user_lifetime + DELETE_GRACE_SECONDS)


def internal_get_lifetime(staging: S3Staging) -> int:
    return min(TRANSFER_INTERNAL_EXPIRES_IN_SECONDS, staging.max_expires_in)


def export_budget_seconds(size: int) -> int:
    """900 s más 1 s por MB: el presupuesto de una exportación a 1 MB/s."""
    return EXPORT_BUDGET_BASE_SECONDS + math.ceil(size / EXPORT_BUDGET_BYTES_PER_SECOND)


def export_lifetime(size: int) -> int:
    return capped_lifetime(export_budget_seconds(size))


def expires_at(signed_at: datetime, lifetime: int) -> datetime:
    return signed_at + timedelta(seconds=lifetime)


def unix_ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def validate_max_bytes(max_bytes: object) -> int | None:
    if max_bytes is None:
        return None
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise InvalidArgumentException("max_bytes debe ser None o un entero >= 1")
    return max_bytes


# ---------------------------------------------------------------- user urls


def upload_ticket_headers(form: bool) -> dict[str, str]:
    """El PUT crudo recomienda `Content-Type: application/octet-stream` (sin
    firmarlo: un `requests.put(url, data=f)` sin cabeceras también vale); el
    formulario POST lleva sus propios campos."""
    return {} if form else {CONTENT_TYPE_HEADER: OCTET_STREAM}


def upload_method(form: bool) -> UploadMethod:
    return "POST" if form else "PUT"


def post_conditions(max_bytes: int | None) -> list[list[object]]:
    return [["content-length-range", 0, max_bytes or TRANSFER_SINGLE_PUT_MAX_BYTES]]


def download_filename(path: str, filename: str | None) -> str:
    if filename is None:
        return path.rstrip("/").rsplit("/", 1)[-1] or "download"
    if not isinstance(filename, str) or not filename or "\0" in filename:
        raise InvalidArgumentException("filename debe ser una cadena no vacía y sin NUL")
    return filename


def content_disposition(filename: str) -> str:
    """`attachment; filename="<ascii>"; filename*=UTF-8''<pct>`: en `<ascii>`
    cada byte UTF-8 fuera de 0x20-0x7E y cada `"` o `\\` pasa a `_`."""
    ascii_name = "".join(
        chr(byte) if 0x20 <= byte <= 0x7E and chr(byte) not in '"\\' else "_"
        for byte in filename.encode("utf-8")
    )
    encoded = urllib.parse.quote(filename, safe="")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded}"


def object_params(target: StagingObject) -> dict[str, Any]:
    return {"Bucket": target.bucket, "Key": target.key}


def download_params(target: StagingObject, disposition: str) -> dict[str, Any]:
    return {**object_params(target), "ResponseContentDisposition": disposition}


def upload_part_params(target: StagingObject, upload_id: str, part_number: int) -> dict[str, Any]:
    return {**object_params(target), "UploadId": upload_id, "PartNumber": part_number}


def completed_parts(etags: Sequence[str]) -> dict[str, Any]:
    return {"Parts": [{"ETag": etag, "PartNumber": number} for number, etag in enumerate(etags, 1)]}


# -------------------------------------------------------------- multipart


@dataclass(frozen=True)
class PartPlan:
    part_size: int
    part_count: int


def ceil_to_mib(size: int) -> int:
    return math.ceil(size / MIB) * MIB


def part_plan(size: int, staging: S3Staging) -> PartPlan | None:
    """`None` por debajo de `multipart_threshold_bytes` (un solo PUT); si no,
    partes de `max(8 MiB, ceil_to_MiB(ceil(size / 1000)))`: como mucho 1000
    URLs, que caben en un `StartExportRequest`."""
    if size < staging.multipart_threshold_bytes:
        return None
    part_size = max(TRANSFER_PART_SIZE_MIN_BYTES, ceil_to_mib(math.ceil(size / PART_SIZE_DIVISOR)))
    return PartPlan(part_size=part_size, part_count=max(1, math.ceil(size / part_size)))


# ------------------------------------------------------------------ routing


def should_route(size: int, staging: S3Staging | None) -> bool:
    return staging is not None and size >= staging.threshold_bytes


@dataclass(frozen=True)
class RoutedWrite:
    """Una entrada de `write_files` que va por S3: `source` es el fichero
    tal cual (nunca se materializa) o los bytes ya codificados; `size` es
    `None` para un stream no buscable."""

    index: int
    path: str
    source: IO[bytes] | IO[str] | bytes
    mode: int | None
    size: int | None


@dataclass
class WritePlan:
    """`grpc` comparte un stream `Write`; `routed` va por S3 una a una. Los
    índices permiten devolver los resultados en el orden pedido."""

    grpc: list[tuple[int, PreparedWrite]] = field(default_factory=list)
    routed: list[RoutedWrite] = field(default_factory=list)
    count: int = 0

    @property
    def grpc_entries(self) -> list[PreparedWrite]:
        return [entry for _, entry in self.grpc]

    def without_routing(self) -> WritePlan:
        """Todo por gRPC: el agente no tiene transferencias. Las entradas
        enrutadas aún no se leyeron, así que materializarlas ahora es seguro."""
        demoted = [
            (routed.index, (routed.path, coerce_data(routed.source), routed.mode))
            for routed in self.routed
        ]
        merged = sorted([*self.grpc, *demoted], key=lambda item: item[0])
        return WritePlan(grpc=merged, routed=[], count=self.count)


def plan_writes(files: Sequence[WriteEntry], staging: S3Staging | None) -> WritePlan:
    """Sin staging todo va por gRPC materializado (el camino de siempre). Con
    staging va por S3 lo que mide `>= threshold_bytes` y todo stream binario
    no buscable (tamaño desconocido: se sube en streaming)."""
    if not files:
        raise InvalidArgumentException("write_files necesita al menos un fichero")
    plan = WritePlan(count=len(files))
    for index, entry in enumerate(files):
        if not isinstance(entry, WriteEntry):
            raise InvalidArgumentException(
                f"write_files acepta WriteEntry, recibido {type(entry).__name__}"
            )
        path = validate_path(entry.path)
        mode = validate_mode(entry.mode)
        routed = route_entry(index, path, entry.data, mode, staging)
        if routed is None:
            plan.grpc.append((index, (path, coerce_data(entry.data), mode)))
        else:
            plan.routed.append(routed)
    return plan


def route_entry(
    index: int, path: str, data: object, mode: int | None, staging: S3Staging | None
) -> RoutedWrite | None:
    if staging is None:
        return None
    if isinstance(data, str | bytes | bytearray | memoryview):
        payload = coerce_data(data)
        if not should_route(len(payload), staging):
            return None
        return RoutedWrite(index=index, path=path, source=payload, mode=mode, size=len(payload))
    if isinstance(data, io.TextIOBase) or not callable(getattr(data, "read", None)):
        return None
    size = remaining_size(data)
    if size is not None and not should_route(size, staging):
        return None
    return RoutedWrite(index=index, path=path, source=data, mode=mode, size=size)  # type: ignore[arg-type]


def remaining_size(stream: object) -> int | None:
    """Bytes que quedan desde la posición actual de un fichero buscable
    (`seek`/`tell`, sin leer); `None` si no se puede buscar."""
    seekable = getattr(stream, "seekable", None)
    if not callable(seekable) or not seekable():
        return None
    position = stream.tell()  # type: ignore[attr-defined]
    end = stream.seek(0, io.SEEK_END)  # type: ignore[attr-defined]
    stream.seek(position)  # type: ignore[attr-defined]
    return int(end) - int(position)


class HashingReader:
    """Lo que `upload_fileobj` lee de una escritura enrutada: el sha256 y el
    tamaño se calculan al vuelo, en orden, sin materializar el fichero. No
    expone `seek` a propósito: s3transfer lo trata como no buscable y lo lee
    secuencialmente, así el hash nunca ve una parte dos veces."""

    def __init__(self, source: IO[bytes] | IO[str] | bytes) -> None:
        self._source: Any = io.BytesIO(source) if isinstance(source, bytes) else source
        self._digest = hashlib.sha256()
        self._size = 0
        self._aborted = False

    def read(self, size: int = -1) -> bytes:
        if self._aborted:
            raise transfer_timeout_error()
        chunk = self._source.read(size)
        data = chunk.encode("utf-8") if isinstance(chunk, str) else bytes(chunk)
        self._digest.update(data)
        self._size += len(data)
        return data

    def abort(self) -> None:
        """Corta una subida que agotó su plazo: la siguiente lectura de
        s3transfer falla y la subida en segundo plano termina."""
        self._aborted = True

    @property
    def sha256(self) -> str:
        return self._digest.hexdigest()

    @property
    def size(self) -> int:
        return self._size


@dataclass(frozen=True)
class OperationDeadline:
    """El plazo de una operación enrutada: `request_timeout` cubre la
    operación entera; sin él, `60 s + 1 s por MB` del tamaño, contados desde
    que empezó."""

    started_at: float
    request_timeout: float | None

    def remaining(self, size: int, now: float) -> float:
        left = file_request_deadline(size, self.request_timeout) - (now - self.started_at)
        if left <= 0:
            raise transfer_timeout_error()
        return left

    def budget(self, size: int | None, now: float) -> float | None:
        """Lo que le queda a la pata S3: sin `request_timeout` y con un stream
        de tamaño desconocido no hay presupuesto por MB que calcular, así que
        la subida no se acota (el plazo se aplica al import que sigue)."""
        if size is None and self.request_timeout is None:
            return None
        return self.remaining(size or 0, now)


def transfer_timeout_error() -> TimeoutException:
    return TimeoutException("la transferencia agotó su plazo antes de terminar")


# ------------------------------------------------------------------ requests


def presigned_request(url: str) -> filesystem_pb2.PresignedRequest:
    return filesystem_pb2.PresignedRequest(url=url)


def start_import_request(
    *,
    path: str,
    user: str | None,
    mode: int | None,
    target: StagingObject,
    get_url: str,
    delete_url: str,
    wait_for_object: bool,
    expires_at: datetime,
    max_bytes: int | None,
    expected_sha256: str = "",
    metadata: Mapping[str, str] | None = None,
) -> filesystem_pb2.StartImportRequest:
    request = filesystem_pb2.StartImportRequest(
        path=path,
        object=target.to_proto(),
        get=presigned_request(get_url),
        wait_for_object=wait_for_object,
        expires_at_unix_ms=unix_ms(expires_at),
        max_bytes=max_bytes or 0,
        expected_sha256=expected_sha256,
    )
    request.delete.CopyFrom(presigned_request(delete_url))
    if mode is not None:
        request.mode = mode
    if metadata:
        request.metadata.update(metadata)
    if user:
        request.user.username = user
    return request


def start_export_request(
    *,
    path: str,
    user: str | None,
    target: StagingObject,
    expires_at: datetime,
    put_url: str | None = None,
    part_urls: Sequence[str] = (),
    part_size: int = 0,
) -> filesystem_pb2.StartExportRequest:
    request = filesystem_pb2.StartExportRequest(
        path=path, object=target.to_proto(), expires_at_unix_ms=unix_ms(expires_at)
    )
    if put_url is not None:
        request.put.CopyFrom(presigned_request(put_url))
    else:
        request.multipart.part_size = part_size
        request.multipart.parts.extend(presigned_request(url) for url in part_urls)
    if user:
        request.user.username = user
    return request


def get_transfer_request(transfer_id: str) -> filesystem_pb2.GetTransferRequest:
    return filesystem_pb2.GetTransferRequest(transfer_id=transfer_id)


def watch_transfer_request(transfer_id: str) -> filesystem_pb2.WatchTransferRequest:
    return filesystem_pb2.WatchTransferRequest(transfer_id=transfer_id)


def watch_starter(
    request: filesystem_pb2.WatchTransferRequest, timeout: float | None
) -> Callable[[Any], Any]:
    """El arranque de `WatchTransfer` con su deadline ya calculado, para
    reabrirlo tras un corte sin capturar la variable de un bucle."""
    return lambda stub: stub.WatchTransfer(request, timeout=timeout)


def cancel_transfer_request(transfer_id: str) -> filesystem_pb2.CancelTransferRequest:
    return filesystem_pb2.CancelTransferRequest(transfer_id=transfer_id)


# ------------------------------------------------------------- conversions


def state_from_proto(state: filesystem_pb2.TransferState) -> TransferStatus:
    error_code, error_reason = state_error(state) if state.HasField("error") else (None, None)
    return TransferStatus(
        transfer_id=str(state.transfer_id),
        direction=DIRECTION_NAMES.get(int(state.direction), "import"),
        phase=PHASE_NAMES.get(int(state.phase), "waiting"),
        bytes_done=int(state.bytes_done),
        bytes_total=int(state.bytes_total),
        probes=int(state.probes),
        error_code=error_code,
        error_reason=error_reason,
    )


def is_terminal(state: filesystem_pb2.TransferState) -> bool:
    return int(state.phase) in TERMINAL_PHASES


def is_done(state: filesystem_pb2.TransferState) -> bool:
    return int(state.phase) == filesystem_pb2.TRANSFER_PHASE_DONE


def terminal_state(event: Any) -> filesystem_pb2.TransferState | None:
    """El estado final de un `TransferEvent`; `None` para `keepalive` y para
    las fotos intermedias."""
    if event.WhichOneof("event") != "state":
        return None
    state: filesystem_pb2.TransferState = event.state
    return state if is_terminal(state) else None


def state_error(state: filesystem_pb2.TransferState) -> tuple[str, str]:
    """`(code, reason)` de `TransferState.error`: el mensaje de `rayd` es
    `"<reason>: <frase>"`; un `CANCELLED` sin error es `cancelled`."""
    if not state.HasField("error"):
        fallback = (
            "cancelled"
            if int(state.phase) == filesystem_pb2.TRANSFER_PHASE_CANCELLED
            else "internal"
        )
        return fallback, fallback
    code = str(state.error.code) or "internal"
    message = str(state.error.message)
    reason = message.split(":", 1)[0].strip() if ":" in message else code
    return code, reason or code


def failure_message(state: filesystem_pb2.TransferState) -> str:
    code, reason = state_error(state)
    message = str(state.error.message) if state.HasField("error") else ""
    if message.startswith(f"{reason}:"):
        return message
    return f"{reason}: {message or code}"


def failure_from_state(
    state: filesystem_pb2.TransferState, direction: TransferDirectionName
) -> Exception:
    """Tabla D13: cada código con excepción propia la usa; el resto es
    `FileUploadException` en una importación y `TransferException` en una
    exportación. Todo mensaje empieza por `"<reason>: "`."""
    code, reason = state_error(state)
    message = failure_message(state)
    if code == "deadline_exceeded":
        return TimeoutException(message)
    if code == "invalid_argument":
        return InvalidArgumentException(message)
    if code == "resource_exhausted":
        return DiskFullException(message)
    if code == "permission_denied":
        return AuthenticationException(message, proxy_rejected=False)
    if code == "not_found":
        return FileNotFoundException(message)
    if direction == "import":
        return FileUploadException(message, code=code, reason=reason)
    return TransferException(message, code=code, reason=reason)


def checksum_mismatch_error() -> TransferException:
    return TransferException(
        "checksum_mismatch: el sha256 de lo descargado no coincide con el de la exportación",
        code="failed_precondition",
        reason="checksum_mismatch",
    )


def translate_transfer_error(
    exc: grpc.RpcError, feature: str, *, filesystem: bool = False
) -> Exception:
    """La tabla unaria salvo `UNIMPLEMENTED`, que en un RPC de transferencia
    sólo significa un `rayd` anterior a M9."""
    if rpc_status(exc) is grpc.StatusCode.UNIMPLEMENTED:
        return outdated_image_error(feature)
    return translate_rpc_error(exc, filesystem=filesystem)


def probe_supports_transfers(exc: grpc.RpcError) -> bool:
    """`GetTransfer("")` es la sonda de capacidad: `NOT_FOUND` es un agente
    M9, `UNIMPLEMENTED` uno anterior; cualquier otro status se propaga."""
    code = rpc_status(exc)
    if code is grpc.StatusCode.NOT_FOUND:
        return True
    if code is grpc.StatusCode.UNIMPLEMENTED:
        return False
    raise translate_rpc_error(exc) from exc


def transfer_deadline_error(transfer_id: str) -> TimeoutException:
    return TimeoutException(
        f"la transferencia {transfer_id} no terminó en el plazo pedido; sigue en curso en el "
        "sandbox"
    )


def translate_s3_error(exc: Exception) -> Exception:
    """Errores de las llamadas del SDK a S3 con las credenciales del llamante,
    por nombre de código de botocore; el mensaje nunca lleva bucket ni clave."""
    if isinstance(exc, NoCredentialsError):
        return AuthenticationException("no hay credenciales de AWS para firmar la transferencia")
    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code", "Unknown"))
        if code in S3_CREDENTIAL_ERRORS:
            return AuthenticationException(
                f"S3 rechazó las credenciales del llamante ({code})", aws_code=code
            )
        if code == "NoSuchBucket":
            return InvalidArgumentException(
                "el bucket de transferencias no existe (NoSuchBucket)", aws_code=code
            )
        if code in S3_REGION_ERRORS:
            return InvalidArgumentException(
                f"S3Staging.region no es la región del bucket ({code})", aws_code=code
            )
        return SandboxException(f"S3 respondió {code} a una transferencia", aws_code=code)
    if isinstance(exc, BotoCoreError):
        return SandboxException(f"fallo de botocore en una transferencia: {type(exc).__name__}")
    return exc


# ------------------------------------------------------------- capability


class CapabilityProbe:
    """Resultado cacheado de la sonda `GetTransfer("")` durante la vida del
    sandbox: `None` sin sondear, `True` agente M9, `False` anterior."""

    def __init__(self) -> None:
        self.supported: bool | None = None

    def require(self, feature: str) -> None:
        if self.supported is False:
            raise outdated_image_error(feature)
