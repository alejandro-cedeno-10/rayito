"""Traza de oro de 0.5.x "coste cero" (M15 foundations, §1(g) /
§9.1 de la arquitectura de 0.6): con las siete opciones 0.6 ausentes,
`create → commands.run → files.write → pause → resume → commands.run →
kill → list` debe producir exactamente la misma secuencia de operaciones
boto3 (con el `runHookPayload` que viaja en `RunMicrovm`) y de métodos gRPC
que en 0.5.x. La traza se capturó en esta rama con las opciones 0.6 ausentes
(equivalente a 0.5.1: ningún comportamiento de 0.5.x cambió, confirmado por
el resto de la suite de unit tests sin tocar) y queda congelada en
`fixtures/zero_cost_0_5_trace.json`; cualquier cambio futuro que la rompa
debe justificar por qué 0.6, sin ninguna opción activada, deja de ser
byte a byte 0.5.x.

Simplificación deliberada frente a la arquitectura: en vez de "connect" como
RPC explícito, el paso de reconexión se comprueba con un segundo
`commands.run` tras el resume (reabre el canal unario sobre el mismo
`access_token`), que es lo que de verdad hace la reconexión automática del
SDK; no hay ningún RPC "Connect" propio en una llamada en foreground.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import boto3
import grpc
import pytest

from rayito import Sandbox
from rayito._transport import ProxyAuthPlugin, TransportSettings

from .conftest import (
    ACCESS_TOKEN,
    IMAGE_ARN,
    RaydEndpoint,
    StubbedControlPlane,
    auth_token_response,
    microvm_response,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "zero_cost_0_5_trace.json"


class _RecordingInterceptor(
    grpc.UnaryUnaryClientInterceptor,  # type: ignore[type-arg]
    grpc.UnaryStreamClientInterceptor,  # type: ignore[type-arg]
    grpc.StreamUnaryClientInterceptor,  # type: ignore[type-arg]
    grpc.StreamStreamClientInterceptor,  # type: ignore[type-arg]
):
    """Registra el método de cada RPC saliente, en orden; no toca la
    petición ni la respuesta."""

    def __init__(self) -> None:
        self.methods: list[str] = []

    def intercept_unary_unary(self, continuation, client_call_details, request):  # type: ignore[no-untyped-def]
        self.methods.append(client_call_details.method)
        return continuation(client_call_details, request)

    def intercept_unary_stream(self, continuation, client_call_details, request):  # type: ignore[no-untyped-def]
        self.methods.append(client_call_details.method)
        return continuation(client_call_details, request)

    def intercept_stream_unary(self, continuation, client_call_details, request_iterator):  # type: ignore[no-untyped-def]
        self.methods.append(client_call_details.method)
        return continuation(client_call_details, request_iterator)

    def intercept_stream_stream(self, continuation, client_call_details, request_iterator):  # type: ignore[no-untyped-def]
        self.methods.append(client_call_details.method)
        return continuation(client_call_details, request_iterator)


class _RecordingTransport(TransportSettings):
    """Como `TransportSettings` de loopback, pero cada canal pasa por un
    `_RecordingInterceptor` compartido."""

    def __init__(self, endpoint: RaydEndpoint, interceptor: _RecordingInterceptor) -> None:
        super().__init__(
            channel_credentials=grpc.local_channel_credentials(grpc.LocalConnectionType.LOCAL_TCP),
            port=endpoint.port,
            options=(),
        )
        self._interceptor = interceptor

    def open_channel(self, host: str, plugin: ProxyAuthPlugin) -> grpc.Channel:
        channel = super().open_channel(host, plugin)
        return grpc.intercept_channel(channel, self._interceptor)


#: The W3C trace headers `tracer_provider=` adds (research Q92); never sent
#: without it.
TRACE_HEADERS = frozenset({"traceparent", "tracestate"})

#: `clientToken` (idempotency) is random per call; masked before comparing.
MASKED_PARAMS = ("clientToken",)
MASK = "<masked>"


def _mask(params: dict[str, Any]) -> dict[str, Any]:
    return {key: (MASK if key in MASKED_PARAMS else value) for key, value in params.items()}


def _boto3_recorder(events: Any, calls: list[dict[str, Any]]) -> Any:
    """`before-parameter-build` gives the high-level kwargs the SDK itself
    built, before serialization — the right altitude for "did 0.6 add a
    field or a call", unlike `before-call` (wire bytes) or `before-sign`."""

    def record(model: Any, params: dict[str, Any], **_kwargs: Any) -> None:
        calls.append({"operation": model.name, "params": _mask(params)})

    events.register_first("before-parameter-build.*.*", record)
    return record


def test_the_default_0_6_path_matches_the_0_5_x_golden_trace(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    boto_calls: list[dict[str, Any]] = []
    _boto3_recorder(control_plane.plane._client.meta.events, boto_calls)

    control_plane.microvms.add_response("run_microvm", microvm_response(endpoint=fake_rayd.host))
    control_plane.microvms.add_response("create_microvm_auth_token", auth_token_response())
    control_plane.microvms.add_response("get_microvm", microvm_response(endpoint=fake_rayd.host))
    control_plane.microvms.add_response("suspend_microvm", {})
    control_plane.microvms.add_response(
        "get_microvm", microvm_response(endpoint=fake_rayd.host, state="SUSPENDED")
    )
    control_plane.microvms.add_response("resume_microvm", {})
    control_plane.microvms.add_response("create_microvm_auth_token", auth_token_response())
    control_plane.microvms.add_response("terminate_microvm", {})
    control_plane.microvms.add_response("list_microvms", {"items": []})

    interceptor = _RecordingInterceptor()
    sbx = Sandbox.create(
        IMAGE_ARN,
        control_plane=control_plane.plane,
        transport=_RecordingTransport(fake_rayd, interceptor),
        access_token=ACCESS_TOKEN,
    )
    sbx.commands.run("echo hola")
    sbx.files.write("/home/user/f.txt", "contenido")
    sbx.pause()
    fake_rayd.suspend()
    fake_rayd.resume()
    sbx.resume()
    sbx.commands.run("echo de nuevo")
    sbx.kill()
    list(Sandbox.list(control_plane=control_plane.plane))

    trace = {
        "boto3_operations": [call["operation"] for call in boto_calls],
        "run_microvm_payload": json.loads(boto_calls[0]["params"]["runHookPayload"]),
        "grpc_methods": interceptor.methods,
    }
    expected = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert trace == expected
    # m15-rayd-otlp: without `tracer_provider=` no RPC carries a W3C trace
    # header toward `rayd` (call-credential metadata never reaches a client
    # interceptor, so this reads what the fake `rayd` actually received).
    received = [*fake_rayd.process.start_metadata, *fake_rayd.servicer.health_calls]
    assert received
    for metadata in received:
        assert TRACE_HEADERS.isdisjoint(metadata)


def test_building_optionalstacks_creates_no_aws_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Construir `OptionalStacks()` (el servicio que respalda `rayito
    stack` y cada fachada de feature) no llama a `boto3.session.Session.client`
    en absoluto: `CloudFormationProvisioner` sólo construye sus
    `LazyClient`, que difieren hasta el primer uso real (`deploy`,
    `status`, `destroy`)."""
    from rayito import OptionalStacks

    calls: list[str] = []
    original = boto3.session.Session.client

    def recording_client(self: Any, service_name: str, *args: Any, **kwargs: Any) -> Any:
        calls.append(service_name)
        return original(self, service_name, *args, **kwargs)

    monkeypatch.setattr(boto3.session.Session, "client", recording_client)
    OptionalStacks()
    assert calls == []
