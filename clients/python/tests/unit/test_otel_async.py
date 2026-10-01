"""Spans OpenTelemetry opt-in (M13b) en `AsyncSandbox`: mismo contrato que
`test_otel_sync.py` sobre `grpc.aio` contra el mismo `rayd` falso. No repite
cada caso de la versión síncrona; cubre lo propio de la superficie async
(creación, comando en segundo plano, kill, código y ficheros) y que el
anidado bajo un span padre del llamante también funciona en una corrutina."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from rayito import AsyncSandbox
from rayito._limits import DEFAULT_PORT
from rayito._otel import ALLOWED_SPAN_ATTRIBUTES

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
async def instrumented_sandbox(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, provider: TracerProvider
) -> AsyncIterator[AsyncSandbox]:
    stub_launch(control_plane, fake_rayd)
    created = await AsyncSandbox.create(
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
        await created.kill()


def span_names(exporter: InMemorySpanExporter) -> list[str]:
    return [span.name for span in exporter.get_finished_spans()]


async def test_create_emits_a_single_span_with_region_and_sandbox_id(
    instrumented_sandbox: AsyncSandbox, exporter: InMemorySpanExporter
) -> None:
    (span,) = exporter.get_finished_spans()
    assert span.name == "rayito.sandbox.create"
    assert span.kind == SpanKind.CLIENT
    assert span.attributes is not None
    assert span.attributes["rayito.sandbox.id"] == SANDBOX_ID
    assert set(span.attributes) <= ALLOWED_SPAN_ATTRIBUTES


async def test_kill_emits_its_own_span(
    instrumented_sandbox: AsyncSandbox,
    exporter: InMemorySpanExporter,
    control_plane: StubbedControlPlane,
) -> None:
    control_plane.microvms.add_response(
        "terminate_microvm", {}, expected_params={"microvmIdentifier": SANDBOX_ID}
    )
    await instrumented_sandbox.kill()
    assert span_names(exporter) == ["rayito.sandbox.create", "rayito.sandbox.kill"]


async def test_background_command_span_closes_when_start_returns(
    instrumented_sandbox: AsyncSandbox, exporter: InMemorySpanExporter
) -> None:
    handle = await instrumented_sandbox.commands.run("sleep 5", background=True)
    spans = [s for s in exporter.get_finished_spans() if s.name == "rayito.commands.run"]
    (span,) = spans
    assert span.end_time is not None
    assert span.attributes is not None
    assert span.attributes["rayito.commands.background"] is True
    assert "rayito.commands.exit_code" not in span.attributes
    await handle.kill()


async def test_foreground_command_span_has_exit_code(
    instrumented_sandbox: AsyncSandbox, exporter: InMemorySpanExporter
) -> None:
    await instrumented_sandbox.commands.run("echo hola")
    spans = [s for s in exporter.get_finished_spans() if s.name == "rayito.commands.run"]
    (span,) = spans
    assert span.attributes is not None
    assert span.attributes["rayito.commands.exit_code"] == 0


async def test_run_code_span_has_the_language_attribute(
    instrumented_sandbox: AsyncSandbox, exporter: InMemorySpanExporter
) -> None:
    await instrumented_sandbox.run_code("1 + 1", language="python")
    spans = [s for s in exporter.get_finished_spans() if s.name == "rayito.code.run"]
    (span,) = spans
    assert span.attributes is not None
    assert span.attributes["rayito.code.language"] == "python"


async def test_files_write_then_read_emit_their_own_spans_with_no_path_leaked(
    instrumented_sandbox: AsyncSandbox, exporter: InMemorySpanExporter
) -> None:
    secret_path = f"/home/user/{SENTINEL}.txt"
    await instrumented_sandbox.files.write(secret_path, "hola mundo")
    await instrumented_sandbox.files.read(secret_path)
    names = span_names(exporter)
    assert "rayito.files.write" in names
    assert "rayito.files.read" in names
    for span in exporter.get_finished_spans():
        if not span.name.startswith("rayito.files."):
            continue
        assert span.attributes is not None
        assert set(span.attributes) <= ALLOWED_SPAN_ATTRIBUTES
        for value in span.attributes.values():
            assert SENTINEL not in str(value)


async def test_a_user_parent_span_is_the_parent_of_every_rayito_span(
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
    provider: TracerProvider,
    exporter: InMemorySpanExporter,
) -> None:
    stub_launch(control_plane, fake_rayd)
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("user.workflow") as parent:
        sandbox = await AsyncSandbox.create(
            IMAGE_ARN,
            idle=None,
            access_token=ACCESS_TOKEN,
            control_plane=control_plane.plane,
            transport=fake_rayd.transport,
            tracer_provider=provider,
        )
        await sandbox.commands.run("echo hola")
        control_plane.microvms.add_response(
            "terminate_microvm", {}, expected_params={"microvmIdentifier": SANDBOX_ID}
        )
        await sandbox.kill()
    parent_span_id = parent.get_span_context().span_id
    rayito_spans = [s for s in exporter.get_finished_spans() if s.name.startswith("rayito.")]
    assert len(rayito_spans) == 3
    for span in rayito_spans:
        assert span.parent is not None
        assert span.parent.span_id == parent_span_id
