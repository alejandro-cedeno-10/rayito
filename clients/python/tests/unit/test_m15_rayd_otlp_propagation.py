"""`rayito._telemetry_export._propagation`: `TraceparentProvider` injects a
real W3C `traceparent` over whatever span is active (research Q92), and
`baggage` never reaches the carrier even when the active context has one.
Closes a gap this change's own `proposal.md` claimed was already covered
("unit-tested in isolation") but was not: before this file, nothing in the
test suite imported `_propagation` at all.
"""

from __future__ import annotations

from opentelemetry import baggage
from opentelemetry import context as otel_context
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from rayito import AsyncSandbox, Sandbox
from rayito._otel import NOOP, instrumentation_for
from rayito._telemetry_export._propagation import TraceparentProvider, call_metadata_providers

from .conftest import (
    ACCESS_TOKEN,
    IMAGE_ARN,
    RaydEndpoint,
    StubbedControlPlane,
    auth_token_response,
    microvm_response,
)


def test_metadata_is_empty_with_no_active_span_and_no_baggage() -> None:
    assert TraceparentProvider().metadata() == ()


def test_metadata_carries_a_real_w3c_traceparent_for_the_active_span() -> None:
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(InMemorySpanExporter()))
    tracer = tracer_provider.get_tracer("test")
    provider = TraceparentProvider()
    with tracer.start_as_current_span("rpc"):
        metadata = dict(provider.metadata())
    assert "traceparent" in metadata
    # `version-trace_id-parent_id-flags`: four hyphen-separated fields,
    # version "00" (W3C Trace Context §3.2), the only version this research
    # (Q92) cares about.
    parts = metadata["traceparent"].split("-")
    assert len(parts) == 4
    assert parts[0] == "00"


def test_metadata_never_carries_baggage_even_when_the_active_context_has_one() -> None:
    provider = TraceparentProvider()
    ctx = baggage.set_baggage("secret", "value")
    token = otel_context.attach(ctx)
    try:
        metadata = dict(provider.metadata())
    finally:
        otel_context.detach(token)
    assert "baggage" not in metadata


def test_two_calls_in_different_spans_carry_different_trace_ids() -> None:
    """`metadata()` is computed fresh per call, never fixed at construction
    (the provider itself is stateless): two different spans must never
    share a trace id."""
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(InMemorySpanExporter()))
    tracer = tracer_provider.get_tracer("test")
    provider = TraceparentProvider()
    with tracer.start_as_current_span("first"):
        first = dict(provider.metadata())["traceparent"]
    with tracer.start_as_current_span("second"):
        second = dict(provider.metadata())["traceparent"]
    assert first.split("-")[1] != second.split("-")[1]


# ------------------------------------------------- wiring onto the live channel


def test_without_tracer_provider_no_provider_is_installed() -> None:
    assert call_metadata_providers(NOOP) == ()


def test_with_tracer_provider_one_traceparent_provider_is_installed() -> None:
    (provider,) = call_metadata_providers(instrumentation_for(TracerProvider()))
    assert isinstance(provider, TraceparentProvider)


def _stub_launch_and_kill(control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint) -> None:
    control_plane.microvms.add_response("run_microvm", microvm_response(endpoint=fake_rayd.host))
    control_plane.microvms.add_response("create_microvm_auth_token", auth_token_response())
    control_plane.microvms.add_response("terminate_microvm", {})


def _assert_one_start_with_a_w3c_traceparent(fake_rayd: RaydEndpoint) -> None:
    (metadata,) = fake_rayd.process.start_metadata
    version, trace_id, span_id, _flags = metadata["traceparent"].split("-")
    assert version == "00"
    assert len(trace_id) == 32
    assert len(span_id) == 16
    assert "baggage" not in metadata


def test_a_sync_handle_with_tracer_provider_sends_traceparent_to_rayd(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    _stub_launch_and_kill(control_plane, fake_rayd)
    sbx = Sandbox.create(
        IMAGE_ARN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
        access_token=ACCESS_TOKEN,
        tracer_provider=TracerProvider(),
    )
    sbx.commands.run("echo hola")
    sbx.kill()
    _assert_one_start_with_a_w3c_traceparent(fake_rayd)


async def test_an_async_handle_with_tracer_provider_sends_traceparent_to_rayd(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    _stub_launch_and_kill(control_plane, fake_rayd)
    sbx = await AsyncSandbox.create(
        IMAGE_ARN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
        access_token=ACCESS_TOKEN,
        tracer_provider=TracerProvider(),
    )
    await sbx.commands.run("echo hola")
    await sbx.kill()
    _assert_one_start_with_a_w3c_traceparent(fake_rayd)
