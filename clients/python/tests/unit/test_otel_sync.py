"""Spans OpenTelemetry opt-in (M13b) en `Sandbox` síncrono contra el `rayd`
falso: sin `tracer_provider=` los tests existentes no cambian (ya lo prueba
el resto de la suite); con un `TracerProvider` del SDK y un
`InMemorySpanExporter`, cada operación instrumentada emite exactamente un
span con el nombre esperado, anidado bajo un span padre del llamante, con
atributos ⊆ `ALLOWED_SPAN_ATTRIBUTES` y sin ningún centinela de datos
sensibles."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from rayito import Sandbox
from rayito._limits import DEFAULT_PORT
from rayito._otel import ALLOWED_SPAN_ATTRIBUTES
from rayito.exceptions import CommandExitException

from .conftest import (
    ACCESS_TOKEN,
    IMAGE_ARN,
    SANDBOX_ID,
    RaydEndpoint,
    StubbedControlPlane,
    auth_token_response,
    microvm_response,
)

SENTINEL = "s3cr3t-token-/home/user/.aws/credentials-SELECT*FROM-users"


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def provider(exporter: InMemorySpanExporter) -> TracerProvider:
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    return tracer_provider


def stub_launch(control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint) -> None:
    control_plane.microvms.add_response("run_microvm", microvm_response(endpoint=fake_rayd.host))
    control_plane.microvms.add_response(
        "create_microvm_auth_token",
        auth_token_response(),
        expected_params={
            "microvmIdentifier": SANDBOX_ID,
            "expirationInMinutes": 60,
            "allowedPorts": [{"port": DEFAULT_PORT}],
        },
    )


@pytest.fixture
def instrumented_sandbox(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, provider: TracerProvider
) -> Iterator[Sandbox]:
    stub_launch(control_plane, fake_rayd)
    created = Sandbox.create(
        IMAGE_ARN,
        idle=None,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
        tracer_provider=provider,
    )
    try:
        yield created
    finally:
        control_plane.microvms.add_response(
            "terminate_microvm", {}, expected_params={"microvmIdentifier": SANDBOX_ID}
        )
        created.kill()


def span_names(exporter: InMemorySpanExporter) -> list[str]:
    return [span.name for span in exporter.get_finished_spans()]


def test_create_emits_a_single_span_with_region_and_sandbox_id(
    instrumented_sandbox: Sandbox, exporter: InMemorySpanExporter
) -> None:
    (span,) = exporter.get_finished_spans()
    assert span.name == "rayito.sandbox.create"
    assert span.kind == SpanKind.CLIENT
    assert span.attributes is not None
    assert span.attributes["rayito.sandbox.id"] == SANDBOX_ID
    assert span.attributes["rayito.operation"] == "create"
    assert set(span.attributes) <= ALLOWED_SPAN_ATTRIBUTES


def test_kill_emits_its_own_span_after_create(
    instrumented_sandbox: Sandbox,
    exporter: InMemorySpanExporter,
    control_plane: StubbedControlPlane,
) -> None:
    control_plane.microvms.add_response(
        "terminate_microvm", {}, expected_params={"microvmIdentifier": SANDBOX_ID}
    )
    instrumented_sandbox.kill()
    assert span_names(exporter) == ["rayito.sandbox.create", "rayito.sandbox.kill"]
    kill_span = exporter.get_finished_spans()[1]
    assert kill_span.attributes is not None
    assert kill_span.attributes["rayito.sandbox.id"] == SANDBOX_ID


def test_foreground_command_span_closes_after_wait_with_exit_code(
    instrumented_sandbox: Sandbox, exporter: InMemorySpanExporter
) -> None:
    instrumented_sandbox.commands.run("echo hola")
    spans = [s for s in exporter.get_finished_spans() if s.name == "rayito.commands.run"]
    (span,) = spans
    assert span.attributes is not None
    assert span.attributes["rayito.commands.background"] is False
    assert span.attributes["rayito.commands.exit_code"] == 0
    assert set(span.attributes) <= ALLOWED_SPAN_ATTRIBUTES


def test_background_command_span_closes_when_start_returns(
    instrumented_sandbox: Sandbox, exporter: InMemorySpanExporter
) -> None:
    handle = instrumented_sandbox.commands.run("sleep 5", background=True)
    spans = [s for s in exporter.get_finished_spans() if s.name == "rayito.commands.run"]
    (span,) = spans
    # El span ya está cerrado (closed) en cuanto `run` devuelve el handle,
    # mucho antes de que el proceso en segundo plano termine.
    assert span.end_time is not None
    assert span.attributes is not None
    assert span.attributes["rayito.commands.background"] is True
    assert "rayito.commands.exit_code" not in span.attributes
    handle.kill()


def test_failing_command_sets_error_status_without_the_command_text(
    instrumented_sandbox: Sandbox, exporter: InMemorySpanExporter
) -> None:
    with pytest.raises(CommandExitException):
        instrumented_sandbox.commands.run("exit 7", tag=SENTINEL)
    spans = [s for s in exporter.get_finished_spans() if s.name == "rayito.commands.run"]
    (span,) = spans
    assert span.status.status_code == StatusCode.ERROR
    assert span.status.description == "CommandExitException"
    assert span.attributes is not None
    assert span.attributes["rayito.commands.exit_code"] == 7
    for value in span.attributes.values():
        assert SENTINEL not in str(value)
    for event in span.events:
        assert event.attributes is not None
        for value in event.attributes.values():
            assert SENTINEL not in str(value)


def test_run_code_span_has_the_language_attribute(
    instrumented_sandbox: Sandbox, exporter: InMemorySpanExporter
) -> None:
    instrumented_sandbox.run_code("1 + 1", language="python")
    spans = [s for s in exporter.get_finished_spans() if s.name == "rayito.code.run"]
    (span,) = spans
    assert span.attributes is not None
    assert span.attributes["rayito.code.language"] == "python"
    assert set(span.attributes) <= ALLOWED_SPAN_ATTRIBUTES


def test_files_write_then_read_emit_their_own_spans_with_no_path_leaked(
    instrumented_sandbox: Sandbox, exporter: InMemorySpanExporter
) -> None:
    secret_path = f"/home/user/{SENTINEL}.txt"
    instrumented_sandbox.files.write(secret_path, "hola mundo")
    instrumented_sandbox.files.read(secret_path)
    names = span_names(exporter)
    assert "rayito.files.write" in names
    assert "rayito.files.write_files" in names
    assert "rayito.files.read" in names
    assert "rayito.files.get_info" in names
    for span in exporter.get_finished_spans():
        if not span.name.startswith("rayito.files."):
            continue
        assert span.attributes is not None
        assert set(span.attributes) <= ALLOWED_SPAN_ATTRIBUTES
        for value in span.attributes.values():
            assert SENTINEL not in str(value)
    write_span = next(s for s in exporter.get_finished_spans() if s.name == "rayito.files.write")
    assert write_span.attributes is not None
    assert write_span.attributes["rayito.files.bytes"] == len(b"hola mundo")
    read_span = next(s for s in exporter.get_finished_spans() if s.name == "rayito.files.read")
    assert read_span.attributes is not None
    assert read_span.attributes["rayito.files.bytes"] == len(b"hola mundo")


def test_pause_and_resume_instance_methods_use_the_handle_instrumentation(
    instrumented_sandbox: Sandbox,
    exporter: InMemorySpanExporter,
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
) -> None:
    control_plane.microvms.add_response(
        "get_microvm",
        microvm_response(endpoint=fake_rayd.host, state="RUNNING"),
    )
    control_plane.microvms.add_response(
        "suspend_microvm", {}, expected_params={"microvmIdentifier": SANDBOX_ID}
    )
    instrumented_sandbox.pause(wait=False)
    names = span_names(exporter)
    assert "rayito.sandbox.pause" in names


def test_a_user_parent_span_is_the_parent_of_every_rayito_span(
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
    provider: TracerProvider,
    exporter: InMemorySpanExporter,
) -> None:
    stub_launch(control_plane, fake_rayd)
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("user.workflow") as parent:
        sandbox = Sandbox.create(
            IMAGE_ARN,
            idle=None,
            access_token=ACCESS_TOKEN,
            control_plane=control_plane.plane,
            transport=fake_rayd.transport,
            tracer_provider=provider,
        )
        sandbox.commands.run("echo hola")
        control_plane.microvms.add_response(
            "terminate_microvm", {}, expected_params={"microvmIdentifier": SANDBOX_ID}
        )
        sandbox.kill()
    parent_span_id = parent.get_span_context().span_id
    rayito_spans = [s for s in exporter.get_finished_spans() if s.name.startswith("rayito.")]
    assert len(rayito_spans) == 3
    for span in rayito_spans:
        assert span.parent is not None
        assert span.parent.span_id == parent_span_id
