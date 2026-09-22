"""Reglas puras de `Checkpoint`/`Restore` compartidas por los árboles sync y
async (`m7-s3-persistence`, diseño D9): construcción de requests, validación
de `exclude` en cliente, traducción de eventos a resultados y de status o
`StreamError` a excepciones, y las reglas de `create(persist=)` y
`reincarnate()` que no tocan la red.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any, Final

import grpc

from rayito._limits import DEFAULT_PERSIST_TIMEOUT_SECONDS, PERSIST_EXCLUDE_MAX
from rayito._models import (
    CheckpointProgress,
    CheckpointResult,
    LaunchOptions,
    RestoreProgress,
    RestoreResult,
    S3Prefix,
)
from rayito._transport import (
    is_proxy_forbidden,
    is_stream_reset,
    rpc_details,
    rpc_status,
)
from rayito.exceptions import (
    AuthenticationException,
    InvalidArgumentException,
    NotFoundException,
    PersistenceException,
    SandboxException,
    TimeoutException,
)
from rayito.v1 import common_pb2, filesystem_pb2

logger = logging.getLogger("rayito.persistence")

CheckpointProgressCallback = Callable[[CheckpointProgress], None]
RestoreProgressCallback = Callable[[RestoreProgress], None]

EXCLUDE_MAX_BYTES: Final = 4096
UNIMPLEMENTED_MESSAGE: Final = (
    "la imagen corre un rayd sin Checkpoint/Restore: republica la imagen con un agente "
    "que implemente FilesystemService.Checkpoint (rayd >= 0.2.0)"
)
INTERRUPTED_MESSAGE: Final = (
    "la operación de persistencia se interrumpió a mitad ({reason}); el SDK no la reanuda: "
    "vuelve a ejecutarla"
)


# ------------------------------------------------------------------ requests


def validate_exclude(exclude: Sequence[str]) -> tuple[str, ...]:
    """Las reglas de `RequestPath` sobre rutas relativas al HOME: no vacía, sin
    NUL, sin `..`, no absoluta, <= 4096 bytes; como mucho 64 entradas."""
    entries = tuple(exclude)
    if len(entries) > PERSIST_EXCLUDE_MAX:
        raise InvalidArgumentException(
            f"exclude admite como mucho {PERSIST_EXCLUDE_MAX} entradas, recibidas {len(entries)}"
        )
    for entry in entries:
        validate_exclude_entry(entry)
    return entries


def validate_exclude_entry(entry: object) -> str:
    if not isinstance(entry, str) or not entry:
        raise InvalidArgumentException("cada entrada de exclude debe ser una cadena no vacía")
    if "\0" in entry:
        raise InvalidArgumentException("una entrada de exclude contiene NUL")
    if entry.startswith("/"):
        raise InvalidArgumentException(
            f"exclude admite rutas relativas al HOME, no absolutas: {entry!r}"
        )
    if len(entry.encode("utf-8")) > EXCLUDE_MAX_BYTES:
        raise InvalidArgumentException("una entrada de exclude supera 4096 bytes")
    components = [component for component in entry.split("/") if component not in ("", ".")]
    if ".." in components:
        raise InvalidArgumentException(f"exclude no admite `..`: {entry!r}")
    if not components:
        raise InvalidArgumentException(f"exclude no admite una ruta vacía: {entry!r}")
    return entry


def s3_location(target: S3Prefix) -> filesystem_pb2.S3Location:
    location = filesystem_pb2.S3Location(bucket=target.bucket, key_prefix=target.key_prefix)
    if target.region is not None:
        location.region = target.region
    return location


def checkpoint_request(
    target: S3Prefix, exclude: Sequence[str], user: str | None
) -> filesystem_pb2.CheckpointRequest:
    request = filesystem_pb2.CheckpointRequest(
        target=s3_location(target), exclude=list(validate_exclude(exclude))
    )
    if user is not None:
        request.user.CopyFrom(common_pb2.User(username=user))
    return request


def restore_request(source: S3Prefix, user: str | None) -> filesystem_pb2.RestoreRequest:
    request = filesystem_pb2.RestoreRequest(source=s3_location(source))
    if user is not None:
        request.user.CopyFrom(common_pb2.User(username=user))
    return request


def resolve_target(explicit: S3Prefix | None, bound: S3Prefix | None) -> S3Prefix:
    """`target=`/`source=` explícito, si no el `persist` del sandbox."""
    target = explicit if explicit is not None else bound
    if target is None:
        raise InvalidArgumentException(
            "no hay destino: pasa target=S3Prefix(...) o crea el sandbox con create(persist=)"
        )
    if target.name is None:
        raise InvalidArgumentException(
            "S3Prefix sin name: pásalo (S3Prefix(..., name=...)) o usa el persist de "
            "create(persist=), que lo fija al sandbox_id"
        )
    return target


def validate_persist_timeout(timeout: float | None) -> float:
    value = DEFAULT_PERSIST_TIMEOUT_SECONDS if timeout is None else timeout
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise InvalidArgumentException(f"timeout debe ser un número > 0, recibido {timeout!r}")
    return float(value)


# ---------------------------------------------------------- create rules


def require_role_for_persist(persist: S3Prefix | None, execution_role_arn: str | None) -> None:
    """Regla 1 de D9: sin execution role no hay S3, y el SDK no adivina uno."""
    if persist is not None and execution_role_arn is None:
        raise InvalidArgumentException(
            "create(persist=) requiere execution_role_arn: rayd lee S3 con las credenciales "
            "del execution role (política `persistence` de spike/m0/iam.yaml)"
        )


def bind_persist(persist: S3Prefix, sandbox_id: str) -> S3Prefix:
    """Regla 2 de D9: el `name` por defecto es el `sandbox_id`."""
    return persist if persist.name is not None else persist.with_name(sandbox_id)


def require_named_persist(persist: S3Prefix) -> S3Prefix:
    """Regla 4 de D9: `connect(persist=)` sólo enlaza y necesita el `name`."""
    if persist.name is None:
        raise InvalidArgumentException(
            "connect(persist=) requiere S3Prefix con name: es el home que checkpoint_files() "
            "y reincarnate() usarán"
        )
    return persist


def should_auto_restore(persist: S3Prefix) -> bool:
    """Regla 3 de D9: sólo un `name` dado por el caller puede tener un checkpoint."""
    return persist.name is not None


def reincarnate_requires_create_error() -> InvalidArgumentException:
    return InvalidArgumentException(
        "reincarnate() necesita un sandbox creado con Sandbox.create(persist=...): un handle "
        "de connect() no conoce las opciones de lanzamiento"
    )


def reincarnate_requires_persist_error() -> InvalidArgumentException:
    return InvalidArgumentException(
        "reincarnate() necesita un sandbox con persist (create(persist=...) o "
        "connect(..., persist=S3Prefix(..., name=...)))"
    )


def launch_kwargs(options: LaunchOptions) -> dict[str, Any]:
    """Los kwargs de `create()` que reproducen el lanzamiento original."""
    return {
        "template": options.template,
        "template_version": options.template_version,
        "timeout": options.timeout,
        "idle": options.idle,
        "envs": options.envs,
        "metadata": options.metadata,
        "cpu_time_limit": options.cpu_time_limit,
        "execution_role_arn": options.execution_role_arn,
        "allowed_ports": options.allowed_ports,
        "ingress": options.ingress,
        "egress": options.egress,
        "logging": options.logging,
        "access_token": options.access_token,
        "ready_timeout": options.ready_timeout,
        "request_timeout": options.request_timeout,
        "reconnect_timeout": options.reconnect_timeout,
        "keep_on_failure": options.keep_on_failure,
        "control_plane": options.control_plane,
        "transport": options.transport,
    }


def add_reincarnate_note(exc: BaseException, uri: str) -> None:
    exc.add_note(
        f"reincarnate(): el checkpoint en {uri} está completo; el sandbox original sigue "
        "vivo y sin cambios"
    )


# ------------------------------------------------------------------ events


def checkpoint_progress_from_proto(event: filesystem_pb2.CheckpointProgress) -> CheckpointProgress:
    return CheckpointProgress(
        files_done=int(event.files_done),
        bytes_read=int(event.bytes_read),
        bytes_uploaded=int(event.bytes_uploaded),
    )


def checkpoint_result_from_proto(
    target: S3Prefix, done: filesystem_pb2.CheckpointDone
) -> CheckpointResult:
    return CheckpointResult(
        bucket=target.bucket,
        key_prefix=target.key_prefix,
        files=int(done.files),
        bytes_read=int(done.bytes_read),
        archive_bytes=int(done.archive_bytes),
        sha256=done.sha256,
        skipped=int(done.skipped),
        duration=int(done.duration_ms) / 1000.0,
    )


def restore_progress_from_proto(event: filesystem_pb2.RestoreProgress) -> RestoreProgress:
    return RestoreProgress(
        files_done=int(event.files_done), bytes_downloaded=int(event.bytes_downloaded)
    )


def restore_result_from_proto(source: S3Prefix, done: filesystem_pb2.RestoreDone) -> RestoreResult:
    return RestoreResult(
        bucket=source.bucket,
        key_prefix=source.key_prefix,
        files=int(done.files),
        bytes_written=int(done.bytes_written),
        archive_bytes=int(done.archive_bytes),
        sha256=done.sha256,
        skipped=int(done.skipped),
        duration=int(done.duration_ms) / 1000.0,
    )


def require_started(first: Any, *, rpc: str) -> None:
    """El primer mensaje de ambos streams es `started`; cualquier otra cosa es
    un agente que no cumple el contrato (o un `error` in-stream temprano)."""
    if first is None:
        raise SandboxException(f"{rpc} terminó sin mensajes")
    kind = first.WhichOneof("event")
    if kind == "error":
        raise stream_error_exception(first.error)
    if kind != "started":
        raise SandboxException(f"{rpc} empezó con {kind!r} en vez de started")


def handle_checkpoint_event(
    event: filesystem_pb2.CheckpointEvent,
    target: S3Prefix,
    on_progress: CheckpointProgressCallback | None,
) -> CheckpointResult | None:
    """`None` mientras el stream siga; el resultado en `done`; excepción en `error`."""
    kind = event.WhichOneof("event")
    if kind == "progress":
        if on_progress is not None:
            on_progress(checkpoint_progress_from_proto(event.progress))
        return None
    if kind == "done":
        return checkpoint_result_from_proto(target, event.done)
    if kind == "error":
        raise stream_error_exception(event.error)
    return None


def handle_restore_event(
    event: filesystem_pb2.RestoreEvent,
    source: S3Prefix,
    on_progress: RestoreProgressCallback | None,
) -> RestoreResult | None:
    kind = event.WhichOneof("event")
    if kind == "progress":
        if on_progress is not None:
            on_progress(restore_progress_from_proto(event.progress))
        return None
    if kind == "done":
        return restore_result_from_proto(source, event.done)
    if kind == "error":
        raise stream_error_exception(event.error)
    return None


def stream_ended_early(rpc: str) -> PersistenceException:
    return PersistenceException(
        INTERRUPTED_MESSAGE.format(reason=f"{rpc} terminó sin done"), code="interrupted"
    )


# ------------------------------------------------------------------ errors


def stream_error_exception(error: common_pb2.StreamError) -> Exception:
    """`StreamError.code` tras `started` (tabla D8)."""
    code = error.code
    message = error.message
    if code == "not_found":
        return NotFoundException(message)
    if code == "invalid_argument":
        return InvalidArgumentException(message)
    if code == "deadline_exceeded":
        return TimeoutException(message)
    if code == "suspending":
        return PersistenceException(
            INTERRUPTED_MESSAGE.format(reason="el sandbox se está suspendiendo"),
            code="interrupted",
        )
    if code == "unimplemented":
        return PersistenceException(UNIMPLEMENTED_MESSAGE, code="unimplemented")
    return PersistenceException(message, code=code or "internal")


def status_exception(exc: grpc.RpcError) -> Exception:
    """Status gRPC antes del primer mensaje (tabla D8) y cortes a mitad."""
    code = rpc_status(exc)
    message = rpc_details(exc)
    if code is grpc.StatusCode.NOT_FOUND:
        return NotFoundException(message, grpc_code=code)
    if code is grpc.StatusCode.INVALID_ARGUMENT:
        return InvalidArgumentException(message, grpc_code=code)
    if code is grpc.StatusCode.FAILED_PRECONDITION:
        return PersistenceException(message, code="failed_precondition", grpc_code=code)
    if code is grpc.StatusCode.PERMISSION_DENIED:
        if is_proxy_forbidden(exc):
            return AuthenticationException(message, grpc_code=code, proxy_rejected=True)
        return PersistenceException(message, code="permission_denied", grpc_code=code)
    if code is grpc.StatusCode.UNAUTHENTICATED:
        return AuthenticationException(message, grpc_code=code)
    if code is grpc.StatusCode.UNIMPLEMENTED:
        return PersistenceException(UNIMPLEMENTED_MESSAGE, code="unimplemented", grpc_code=code)
    if code is grpc.StatusCode.DEADLINE_EXCEEDED:
        return TimeoutException(message, grpc_code=code)
    if code is grpc.StatusCode.RESOURCE_EXHAUSTED:
        return PersistenceException(message, code="resource_exhausted", grpc_code=code)
    if code is grpc.StatusCode.UNAVAILABLE or is_stream_reset(exc):
        return PersistenceException(
            INTERRUPTED_MESSAGE.format(reason=message or status_name(code)),
            code="interrupted",
            grpc_code=code,
        )
    if code is grpc.StatusCode.CANCELLED:
        return SandboxException(f"llamada cancelada por el cliente: {message}", grpc_code=code)
    return PersistenceException(message, code="internal", grpc_code=code)


def mid_stream_exception(exc: grpc.RpcError) -> Exception:
    """Un `RpcError` después de `started`: nunca se reconecta."""
    code = rpc_status(exc)
    if code is grpc.StatusCode.DEADLINE_EXCEEDED:
        return TimeoutException(rpc_details(exc), grpc_code=code)
    if code is grpc.StatusCode.CANCELLED:
        return SandboxException(
            f"llamada cancelada por el cliente: {rpc_details(exc)}", grpc_code=code
        )
    return PersistenceException(
        INTERRUPTED_MESSAGE.format(reason=rpc_details(exc) or status_name(code)),
        code="interrupted",
        grpc_code=code,
    )


def status_name(code: grpc.StatusCode | None) -> str:
    return "unknown" if code is None else code.name.lower()


__all__ = [
    "EXCLUDE_MAX_BYTES",
    "INTERRUPTED_MESSAGE",
    "UNIMPLEMENTED_MESSAGE",
    "CheckpointProgressCallback",
    "RestoreProgressCallback",
    "add_reincarnate_note",
    "bind_persist",
    "checkpoint_progress_from_proto",
    "checkpoint_request",
    "checkpoint_result_from_proto",
    "handle_checkpoint_event",
    "handle_restore_event",
    "launch_kwargs",
    "mid_stream_exception",
    "reincarnate_requires_create_error",
    "reincarnate_requires_persist_error",
    "require_named_persist",
    "require_role_for_persist",
    "require_started",
    "resolve_target",
    "restore_progress_from_proto",
    "restore_request",
    "restore_result_from_proto",
    "s3_location",
    "should_auto_restore",
    "status_exception",
    "stream_ended_early",
    "stream_error_exception",
    "validate_exclude",
    "validate_exclude_entry",
    "validate_persist_timeout",
]
