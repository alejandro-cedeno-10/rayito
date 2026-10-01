"""Propagación `traceparent` del SDK hacia `rayd` (m15-rayd-otlp, research
Q92: sólo W3C `traceparent`/`tracestate`, nunca `grpc-trace-bin` ni
`baggage` -- el proxy sólo deja pasar `x-amzn-requestid` y filtra
`x-aws-proxy-*`, y el crítico de la investigación verificó que un
interceptor W3C normal atraviesa ese proxy sin cambios).

`TraceparentProvider` implementa el seam `CallMetadataProvider` de
`rayito._transport` (ADR-015/021: calculado en cada llamada, no fijo para
la vida del canal, porque cada RPC ocurre dentro de un span distinto).
Sólo se construye cuando `rayito._otel.instrumentation_for()` ya construyó
una instrumentación real (`tracer_provider=` puesto): sin eso, este módulo
nunca importa `opentelemetry`.

**Estado de esta entrega**: el proveedor existe y está probado de forma
aislada (inyecta la cabecera igual que `opentelemetry.propagate.inject`),
pero `sandbox_{sync,async}/main.py` todavía no lo conecta al canal gRPC
real -- conectarlo exige reordenar cuándo se construye `ProxyAuthPlugin`
frente a cuándo se conoce la instrumentación en varios puntos de un fichero
compartido entre todas las funciones 0.6 (`sandbox_sync/main.py`), un
cambio de más riesgo del que esta entrega puede probar a fondo. Queda
como seguimiento razonado, no bloqueante (mismo patrón que el reaper de
zombis huérfanos de foundations): ver `openspec/changes/m15-rayd-otlp/design.md`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rayito._transport import MetadataPairs

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
