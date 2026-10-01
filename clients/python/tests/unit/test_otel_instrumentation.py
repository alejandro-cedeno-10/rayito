"""`rayito._otel`: sin `tracer_provider=`, `NOOP` es el mismo singleton en
cada `span()` y nunca importa `opentelemetry`; con un `TracerProvider` del
SDK y un `InMemorySpanExporter`, cada `span()` abre un span `SpanKind.CLIENT`
cuyos atributos están contenidos en `ALLOWED_SPAN_ATTRIBUTES`, una excepción
pone el estado en `ERROR` con el nombre de su clase (nunca su mensaje ni su
traza) y un atributo fuera de la lista es `InvalidArgumentException` antes de
abrir el span."""

from __future__ import annotations

import subprocess
import sys

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from rayito._otel import ALLOWED_SPAN_ATTRIBUTES, NOOP, instrumentation_for
from rayito.exceptions import InvalidArgumentException

SENTINEL = "s3cr3t-token-abc123-/home/user/secret.txt-SELECT * FROM users"


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def provider(exporter: InMemorySpanExporter) -> TracerProvider:
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    return tracer_provider


def test_instrumentation_for_none_is_the_shared_noop() -> None:
    assert instrumentation_for(None) is NOOP


def test_noop_span_is_the_same_singleton_regardless_of_arguments() -> None:
    first = NOOP.span("rayito.sandbox.create")
    second = NOOP.span("rayito.commands.run", {"rayito.sandbox.id": "sbx-1"})
    assert first is second


def test_noop_span_is_a_working_context_manager_with_a_no_op_span() -> None:
    with NOOP.span("rayito.sandbox.create") as handle:
        # Las llamadas que el código instrumentado hace sobre el span (p. ej.
        # `rayito.sandbox.id`, sólo conocido tras `run-microvm`) no deben
        # fallar ni hacer nada cuando no hay `tracer_provider=`.
        handle.set_attribute("rayito.sandbox.id", "sbx-1")
        handle.record_exception(ValueError("x"))
        handle.set_status("ERROR")


def test_importing_otel_module_never_imports_opentelemetry_without_a_provider() -> None:
    probe = (
        "import sys; from rayito._otel import instrumentation_for; "
        "instrumentation_for(None).span('x').__enter__(); "
        "print('opentelemetry' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "False"


def test_otel_span_has_client_kind_and_the_rayito_tracer(
    provider: TracerProvider, exporter: InMemorySpanExporter
) -> None:
    instrumentation = instrumentation_for(provider)
    with instrumentation.span("rayito.sandbox.create", {"rayito.sandbox.id": "sbx-1"}):
        pass
    (span,) = exporter.get_finished_spans()
    assert span.name == "rayito.sandbox.create"
    assert span.kind == SpanKind.CLIENT
    assert span.attributes is not None
    assert span.attributes["rayito.sandbox.id"] == "sbx-1"
    assert span.instrumentation_scope is not None
    assert span.instrumentation_scope.name == "rayito"


def test_otel_span_attributes_are_a_subset_of_the_allowlist(
    provider: TracerProvider, exporter: InMemorySpanExporter
) -> None:
    instrumentation = instrumentation_for(provider)
    with instrumentation.span(
        "rayito.commands.run",
        {"rayito.commands.exit_code": 0, "rayito.commands.background": False},
    ):
        pass
    (span,) = exporter.get_finished_spans()
    assert set(span.attributes or {}) <= ALLOWED_SPAN_ATTRIBUTES


def test_unknown_attribute_is_rejected_before_opening_the_span(
    provider: TracerProvider, exporter: InMemorySpanExporter
) -> None:
    instrumentation = instrumentation_for(provider)
    with (
        pytest.raises(InvalidArgumentException) as excinfo,
        instrumentation.span("rayito.commands.run", {"rayito.commands.cmd": "ls"}),
    ):
        pass
    assert "rayito.commands.cmd" in str(excinfo.value)
    assert exporter.get_finished_spans() == ()


def test_exception_sets_error_status_with_only_the_class_name(
    provider: TracerProvider, exporter: InMemorySpanExporter
) -> None:
    instrumentation = instrumentation_for(provider)
    with pytest.raises(ValueError), instrumentation.span("rayito.commands.run"):
        raise ValueError(SENTINEL)
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code == StatusCode.ERROR
    assert span.status.description == "ValueError"
    assert span.status.description is not None
    assert SENTINEL not in span.status.description


def test_sentinel_never_appears_in_any_attribute_or_event(
    provider: TracerProvider, exporter: InMemorySpanExporter
) -> None:
    instrumentation = instrumentation_for(provider)
    with (
        pytest.raises(RuntimeError),
        instrumentation.span("rayito.code.run", {"rayito.sandbox.id": "sbx-1"}),
    ):
        raise RuntimeError(SENTINEL)
    (span,) = exporter.get_finished_spans()
    assert span.attributes is not None
    for value in span.attributes.values():
        assert SENTINEL not in str(value)
    for event in span.events:
        assert event.attributes is not None
        for value in event.attributes.values():
            assert SENTINEL not in str(value)
    assert SENTINEL not in str(span.status.description)
