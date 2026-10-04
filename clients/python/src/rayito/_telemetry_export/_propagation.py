"""Propagación `traceparent` del SDK hacia `rayd` (m15-rayd-otlp, research
Q92: sólo W3C `traceparent`/`tracestate`, nunca `grpc-trace-bin` ni
`baggage` -- el proxy sólo deja pasar `x-amzn-requestid` y filtra
`x-aws-proxy-*`, y el crítico de la investigación verificó que un
interceptor W3C normal atraviesa ese proxy sin cambios).

`TraceparentProvider` implementa el seam `CallMetadataProvider` de
`rayito._transport` (ADR-015/021: calculado en cada llamada, no fijo para
la vida del canal, porque cada RPC ocurre dentro de un span distinto).
`call_metadata_providers` es lo que instalan `Sandbox._use_instrumentation`
y `AsyncSandbox._use_instrumentation` en el `ProxyAuthPlugin` del handle:
`()` sin `tracer_provider=` (este módulo nunca importa `opentelemetry` y el
canal manda exactamente las cabeceras de 0.5.x), un `TraceparentProvider`
con él. Los canales de un solo uso (`Sandbox.kill`/`pause`/`resume`
estáticos y el sondeo de `get_info`) no lo llevan: no hay handle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rayito._otel import Instrumentation
    from rayito._transport import CallMetadataProvider, MetadataPairs

#: `baggage` nunca viaja hacia `rayd`, ni siquiera si el contexto activo
#: tiene una (Q92): el proxy filtra claves `grpc-*` de forma inconsistente,
#: así que W3C `traceparent`/`tracestate` es la única vía fiable, y
#: `baggage` no aporta nada que `rayd` necesite.
_DROPPED_CARRIER_KEYS = ("baggage",)


class TraceparentProvider:
    """`CallMetadataProvider`: una cabecera `traceparent` (y `tracestate`,
    si el propagador activo la usa) por cada llamada, calculada sobre el
    span activo en ese momento. Construirlo importa `opentelemetry.propagate`
    una vez; no hace ninguna llamada de red."""

    __slots__ = ("_inject",)

    def __init__(self) -> None:
        from opentelemetry import propagate

        self._inject = propagate.inject

    def metadata(self) -> MetadataPairs:
        carrier: dict[str, str] = {}
        self._inject(carrier)
        for key in _DROPPED_CARRIER_KEYS:
            carrier.pop(key, None)
        return tuple(carrier.items())


def call_metadata_providers(
    instrumentation: Instrumentation,
) -> tuple[CallMetadataProvider, ...]:
    """Los `CallMetadataProvider` del canal de un handle con esta
    instrumentación: ninguno sin `tracer_provider=`, un
    `TraceparentProvider` con él."""
    return (TraceparentProvider(),) if instrumentation.propagates else ()
