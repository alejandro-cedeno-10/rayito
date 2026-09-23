"""Núcleo puro del historial de métricas (design D6): conversión de
`datetime` a milisegundos, validación de `max_points`, el request, el mapeo
y la traducción de un `rayd` anterior a M9."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import grpc
import pytest

from rayito._metrics_base import (
    HISTORY_FEATURE,
    HISTORY_UNIMPLEMENTED_REASON,
    MAX_POINTS_WIRE_LIMIT,
    MetricsHistoryUnavailable,
    ensure_history_readable,
    history_unimplemented_error,
    is_history_unavailable,
    is_history_unimplemented,
    metrics_history_from_proto,
    metrics_history_request,
    unix_ms_or_zero,
    validate_max_points,
)
from rayito._models import SandboxInfo
from rayito.exceptions import (
    InvalidArgumentException,
    SandboxException,
    SandboxNotFoundException,
    SandboxStateException,
    UnimplementedError,
)
from rayito.v1 import health_pb2

MOMENT = datetime(2026, 9, 22, 12, 0, 0, 123_000, tzinfo=UTC)
MOMENT_MS = 1_790_078_400_123


def info_in(state: str) -> SandboxInfo:
    return SandboxInfo(
        sandbox_id="microvm-1",
        state=state,
        endpoint="host",
        template="arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base",
        template_version="1",
        started_at=MOMENT,
        maximum_duration_seconds=900,
        state_reason="Success." if state.startswith("TERMINAT") else None,
    )


def test_unix_ms_or_zero_converts_aware_and_naive_datetimes() -> None:
    assert unix_ms_or_zero(None, field="start") == 0
    assert unix_ms_or_zero(MOMENT, field="start") == MOMENT_MS
    naive = datetime(2026, 9, 22, 12, 0)
    assert unix_ms_or_zero(naive, field="start") == round(naive.astimezone(UTC).timestamp() * 1000)


def test_unix_ms_or_zero_rejects_pre_epoch_and_non_datetimes() -> None:
    with pytest.raises(InvalidArgumentException, match="end"):
        unix_ms_or_zero(datetime(1969, 12, 31, 23, 59, tzinfo=UTC), field="end")
    with pytest.raises(InvalidArgumentException, match="start"):
        unix_ms_or_zero(1_790_000_000, field="start")  # type: ignore[arg-type]


def test_validate_max_points() -> None:
    assert validate_max_points(None) == 0
    assert validate_max_points(1) == 1
    assert validate_max_points(10**12) == MAX_POINTS_WIRE_LIMIT
    for bad in (0, -3, True, 2.0, "5"):
        with pytest.raises(InvalidArgumentException, match="max_points debe ser un entero >= 1"):
            validate_max_points(bad)  # type: ignore[arg-type]


def test_metrics_history_request_fills_the_three_fields() -> None:
    request = metrics_history_request(MOMENT, MOMENT + timedelta(minutes=5), 2)
    assert (request.start_unix_ms, request.end_unix_ms, request.max_points) == (
        MOMENT_MS,
        MOMENT_MS + 300_000,
        2,
    )
    empty = metrics_history_request(None, None, None)
    assert (empty.start_unix_ms, empty.end_unix_ms, empty.max_points) == (0, 0, 0)


def test_metrics_history_request_rejects_an_inverted_range() -> None:
    with pytest.raises(InvalidArgumentException, match="start es posterior a end"):
        metrics_history_request(MOMENT, MOMENT - timedelta(milliseconds=1), None)
    same = metrics_history_request(MOMENT, MOMENT, None)
    assert same.start_unix_ms == same.end_unix_ms == MOMENT_MS


def test_metrics_history_from_proto_keeps_the_order_and_maps_mem_cache() -> None:
    response = health_pb2.MetricsHistoryResponse(
        samples=[
            health_pb2.MetricsResponse(timestamp_unix_ms=2_000, mem_cache_bytes=7, cpu_count=2),
            health_pb2.MetricsResponse(timestamp_unix_ms=1_000, mem_cache_bytes=9, cpu_count=2),
        ],
        oldest_unix_ms=1_000,
    )
    samples = metrics_history_from_proto(response)
    assert [sample.mem_cache_bytes for sample in samples] == [7, 9]
    assert samples[0].timestamp == datetime.fromtimestamp(2, tz=UTC)
    assert metrics_history_from_proto(health_pb2.MetricsHistoryResponse()) == []


def test_unimplemented_is_translated_to_an_unimplemented_error_keeping_the_cause() -> None:
    cause = InvalidArgumentException("Method not found", grpc_code=grpc.StatusCode.UNIMPLEMENTED)
    assert is_history_unimplemented(cause)
    assert not is_history_unimplemented(SandboxException("x", grpc_code=grpc.StatusCode.INTERNAL))
    assert not is_history_unimplemented(ValueError("x"))
    error = history_unimplemented_error(cause, HISTORY_FEATURE)
    assert isinstance(error, UnimplementedError) and not isinstance(error, SandboxException)
    assert error.feature == HISTORY_FEATURE
    assert error.reason == HISTORY_UNIMPLEMENTED_REASON and "M9" in str(error)
    assert error.__cause__ is cause
    assert is_history_unavailable(error)
    assert not is_history_unavailable(UnimplementedError(HISTORY_FEATURE, "otro motivo"))
    assert not is_history_unavailable(cause)


def test_history_unavailability_is_identified_by_type_not_by_reason_text() -> None:
    cause = InvalidArgumentException("Method not found", grpc_code=grpc.StatusCode.UNIMPLEMENTED)
    error = history_unimplemented_error(cause, HISTORY_FEATURE)
    assert type(error) is MetricsHistoryUnavailable
    assert issubclass(MetricsHistoryUnavailable, UnimplementedError)
    assert not is_history_unavailable(
        UnimplementedError(HISTORY_FEATURE, HISTORY_UNIMPLEMENTED_REASON)
    )
    reworded = MetricsHistoryUnavailable(HISTORY_FEATURE, "texto reescrito")
    assert is_history_unavailable(reworded)


def test_history_is_readable_only_on_a_running_sandbox() -> None:
    ensure_history_readable(info_in("RUNNING"))
    for state in ("TERMINATING", "TERMINATED"):
        with pytest.raises(SandboxNotFoundException):
            ensure_history_readable(info_in(state))
    for state in ("PENDING", "SUSPENDING", "SUSPENDED"):
        with pytest.raises(SandboxStateException, match="despertaría"):
            ensure_history_readable(info_in(state))
