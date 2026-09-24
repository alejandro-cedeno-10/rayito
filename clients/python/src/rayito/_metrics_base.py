"""Núcleo puro del historial de métricas (`HealthService.MetricsHistory`),
compartido por `Sandbox` y `AsyncSandbox`: conversión de `datetime` a
milisegundos Unix, validación previa al RPC, el request, el mapeo de la
respuesta y la traducción de un `rayd` anterior a M9 (sin el RPC) a
`UnimplementedError`, como las transferencias y el plazo del servidor."""

from __future__ import annotations

from datetime import datetime
from typing import Final

import grpc

from rayito._limits import TERMINAL_STATES
from rayito._models import SandboxInfo, SandboxMetrics
from rayito._process_base import metrics_from_proto
from rayito._sandbox_base import terminal_state_error
from rayito.exceptions import (
    InvalidArgumentException,
    SandboxException,
    SandboxStateException,
    UnimplementedError,
)
from rayito.v1 import health_pb2

HISTORY_UNIMPLEMENTED_REASON: Final = (
    "la imagen es anterior a M9 (rayd sin MetricsHistory): publica una imagen M9"
)
HISTORY_FEATURE: Final = "get_metrics_history"
CLASS_HISTORY_FEATURE: Final = "Sandbox.get_metrics_history(sandbox_id)"
MAX_POINTS_WIRE_LIMIT: Final = 2**32 - 1
MAX_POINTS_ERROR: Final = "max_points debe ser un entero >= 1"
INVERTED_RANGE_ERROR: Final = "start es posterior a end"


class MetricsHistoryUnavailable(UnimplementedError):
    """El historial de métricas contra un `rayd` anterior a M9. Es un
    `UnimplementedError` como cualquier otro para el usuario; el tipo existe
    para que el shim de E2B lo distinga sin comparar el texto de `reason`,
    que sólo es para mostrar."""


def unix_ms_or_zero(moment: datetime | None, *, field: str) -> int:
    """`None` es 0 en el cable ("sin límite"). Un `datetime` naive es hora
    local, la misma regla que documenta E2B; uno anterior a 1970 no cabe en
    el request (0 ya significa "sin límite") y se rechaza nombrando `field`."""
    if moment is None:
        return 0
    if not isinstance(moment, datetime):
        raise InvalidArgumentException(f"{field} debe ser un datetime")
    millis = round(moment.timestamp() * 1000)
    if millis < 0:
        raise InvalidArgumentException(f"{field} no puede ser anterior a 1970-01-01T00:00:00Z")
    return millis


def validate_max_points(max_points: int | None) -> int:
    """`None` es 0 en el cable ("sin reducción"); un valor por encima del
    `uint32` del proto se recorta, porque pedir más puntos que muestras es
    un no-op en `rayd`."""
    if max_points is None:
        return 0
    if isinstance(max_points, bool) or not isinstance(max_points, int) or max_points < 1:
        raise InvalidArgumentException(MAX_POINTS_ERROR)
    return min(max_points, MAX_POINTS_WIRE_LIMIT)


def metrics_history_request(
    start: datetime | None, end: datetime | None, max_points: int | None
) -> health_pb2.MetricsHistoryRequest:
    start_ms = unix_ms_or_zero(start, field="start")
    end_ms = unix_ms_or_zero(end, field="end")
    points = validate_max_points(max_points)
    if start is not None and end is not None and start_ms > end_ms:
        raise InvalidArgumentException(INVERTED_RANGE_ERROR)
    return health_pb2.MetricsHistoryRequest(
        start_unix_ms=start_ms, end_unix_ms=end_ms, max_points=points
    )


def metrics_history_from_proto(
    response: health_pb2.MetricsHistoryResponse,
) -> list[SandboxMetrics]:
    """Las muestras en el orden en que las mandó el agente (ascendente)."""
    return [metrics_from_proto(sample) for sample in response.samples]


def is_history_unimplemented(exc: BaseException) -> bool:
    """La traducción unaria convierte `UNIMPLEMENTED` (un `rayd` sin el
    método) en `InvalidArgumentException` conservando `grpc_code`."""
    return isinstance(exc, SandboxException) and exc.grpc_code is grpc.StatusCode.UNIMPLEMENTED


def history_unimplemented_error(cause: SandboxException, feature: str) -> UnimplementedError:
    """El historial contra un `rayd` anterior a M9 es una feature ausente
    (`UnimplementedError`, no `SandboxException`), con la causa gRPC
    encadenada."""
    error = MetricsHistoryUnavailable(feature, HISTORY_UNIMPLEMENTED_REASON)
    error.__cause__ = cause
    return error


def is_history_unavailable(exc: BaseException) -> bool:
    """El `MetricsHistoryUnavailable` de `history_unimplemented_error`: el
    shim de E2B lo distingue por tipo para caer en la instantánea."""
    return isinstance(exc, MetricsHistoryUnavailable)


def ensure_history_readable(info: SandboxInfo) -> None:
    """La variante de clase sólo lee el historial de un sandbox `RUNNING`:
    uno terminado ya no existe y cualquier otro estado se despertaría con la
    llamada, así que falla antes de acuñar ningún JWE."""
    if info.state == "RUNNING":
        return
    if info.state in TERMINAL_STATES:
        raise terminal_state_error(info)
    raise SandboxStateException(
        f"el sandbox {info.sandbox_id} está {info.state}: leer su historial lo despertaría; "
        "reanúdalo con connect()"
    )
