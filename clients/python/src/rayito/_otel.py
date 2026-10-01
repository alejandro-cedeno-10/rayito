"""Spans OpenTelemetry del lado del SDK, opt-in (M13b, ADR-014).

Una función opcional más (apagada por defecto): `tracer_provider=` en
`Sandbox.create()`, `connect()` (ambas formas) y en las variantes de clase
`kill`/`pause`/`resume` guarda en el handle una `Instrumentation` que envuelve
cada operación de ciclo de vida, comandos, código y ficheros en un span
`rayito.*` de `SpanKind.CLIENT`. Sin `tracer_provider=` (el valor por
defecto, `None`), `instrumentation_for()` devuelve `NOOP`: ni una asignación
por llamada, ni un import de `opentelemetry`.

`NOOP.span()` siempre es el mismo `contextlib.nullcontext()` compartido
(nunca se construye uno por llamada); con un proveedor, el import perezoso de
`opentelemetry.trace` pasa por `require_module()` de `rayito._optional`
(extra `otel`), y sólo ocurre una vez, en el momento de activar la opción
(`create()`/`connect()`), nunca a nivel de módulo.

Lista cerrada de atributos (`ALLOWED_SPAN_ATTRIBUTES`): el constructor del
span rechaza cualquier otra clave con `InvalidArgumentException`, para que
nunca se cuele por descuido el texto de un comando, código, una ruta, un
valor de `envs`, un nombre o valor de secreto, un valor de `metadata`, un
access token, un JWE o una URL prefirmada. Un error dentro de un span llama a
`record_exception` y pone el estado en `ERROR`, pero el nombre de clase de la
excepción es el único dato que entra: ni el mensaje ni la traza se graban
(se sobrescriben con ese mismo nombre), porque el mensaje de una excepción
del SDK puede incluir el propio dato sensible que el span nunca debe ver.

La propagación de `traceparent`/`tracestate` hacia `rayd`, la exportación de
métricas o logs del sandbox y la instrumentación del shim de E2B quedan
fuera de alcance (M13b); la exportación de los spans (y su coste) la
configura el proveedor que pasa el llamante.

Coste y activación
-------------------
Activa: `tracer_provider=` (un `opentelemetry.trace.TracerProvider`, o
    cualquier objeto con `get_tracer(name, version)`) en `create()`,
    `connect()` o en `Sandbox.kill`/`pause`/`resume` de clase; mismo nombre
    de opción en TypeScript (`tracerProvider`).
Recursos y llamadas AWS: ninguno. Rayito sólo crea spans en el proveedor que
    ya tiene el llamante; no hace ninguna llamada a AWS ni crea ningún
    cliente nuevo por esta opción.
Coste aproximado: $0 de AWS; el coste (si lo hay) es el del backend de
    exportación que configure el llamante (Collector, Jaeger, X-Ray, etc.),
    fuera de rayito.
IAM: ninguno adicional.
Cómo apagarla: no pases `tracer_provider=` (por defecto `None`).
Ejemplo:
    from opentelemetry import trace
    from rayito import Sandbox

    sbx = Sandbox.create(tracer_provider=trace.get_tracer_provider())
    sbx.commands.run("echo hola")  # span "rayito.commands.run"
    sbx.kill()
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator, Mapping
from typing import TYPE_CHECKING, Any, Protocol

from rayito._optional import require_module
from rayito._version import __version__
from rayito.exceptions import InvalidArgumentException

if TYPE_CHECKING:
    from opentelemetry.trace import Span

#: Única fuente de verdad de los nombres de atributo que un span de rayito
#: puede llevar; cualquier otra clave es `InvalidArgumentException`. Nunca
#: texto libre (comando, código, ruta, env, secreto, metadata, token, URL).
ALLOWED_SPAN_ATTRIBUTES = frozenset(
    {
        "rayito.sandbox.id",
        "rayito.region",
        "rayito.template.name",
        "rayito.resume_generation",
        "rayito.operation",
        "rayito.commands.exit_code",
        "rayito.commands.background",
        "rayito.code.language",
        "rayito.files.operation",
        "rayito.files.count",
        "rayito.files.bytes",
        "rayito.error.type",
    }
)


class TracerProviderLike(Protocol):
    """Lo mínimo de `opentelemetry.trace.TracerProvider` que `rayito`
    necesita: cualquier objeto con `get_tracer(name, version)` sirve (el SDK
    real, su `NoOpTracerProvider` o un doble de test), sin tener que importar
    `opentelemetry` sólo para comprobar el tipo."""

    def get_tracer(self, name: str, version: str | None = None) -> Any: ...


class Instrumentation:
    """Fachada de spans que guarda un `Sandbox`. `span()` es un gestor de
    contexto: entra al empezar la operación y sale al terminarla (falle o
    no); sin excepción no toca el estado del span."""

    def span(
        self, name: str, attributes: Mapping[str, Any] | None = None
    ) -> contextlib.AbstractContextManager[Any]:
        raise NotImplementedError


class _NoopSpan:
    """Lo que entrega `NOOP.span()`: duck-types lo mínimo de
    `opentelemetry.trace.Span` que usan las llamadas instrumentadas
    (`set_attribute`, `record_exception`, `set_status`), todo sin efecto, así
    el código de `Sandbox` no necesita un `if span is not None` en cada sitio
    donde añade un atributo tardío (como `rayito.sandbox.id`, sólo conocido
    tras `run-microvm`)."""

    __slots__ = ()

    def set_attribute(self, key: str, value: Any) -> None:
        return None

    def record_exception(self, *args: Any, **kwargs: Any) -> None:
        return None

    def set_status(self, *args: Any, **kwargs: Any) -> None:
        return None


_NOOP_SPAN = _NoopSpan()


class _NoopInstrumentation(Instrumentation):
    """Sin `tracer_provider=`: `span()` siempre devuelve el mismo
    `contextlib.nullcontext(_NOOP_SPAN)` ya construido, nunca uno nuevo por
    llamada, y no valida ni copia `attributes` (no hay a dónde escribirlos)."""

    __slots__ = ()

    _CONTEXT: contextlib.AbstractContextManager[_NoopSpan] = contextlib.nullcontext(_NOOP_SPAN)

    def span(
        self, name: str, attributes: Mapping[str, Any] | None = None
    ) -> contextlib.AbstractContextManager[Any]:
        return self._CONTEXT


#: Instancia compartida; `instrumentation_for(None)` siempre devuelve ésta.
NOOP = _NoopInstrumentation()


class _OtelInstrumentation(Instrumentation):
    """Con `tracer_provider=`: un tracer `rayito` (creado una sola vez, al
    activar la opción) y, por llamada a `span()`, un span `SpanKind.CLIENT`
    con los atributos ya validados contra `ALLOWED_SPAN_ATTRIBUTES`."""

    __slots__ = ("_trace", "_tracer")

    def __init__(self, tracer_provider: TracerProviderLike) -> None:
        trace = require_module("opentelemetry.trace", extra="otel", feature="tracer_provider")
        self._trace = trace
        self._tracer = tracer_provider.get_tracer("rayito", __version__)

    @contextlib.contextmanager
    def span(self, name: str, attributes: Mapping[str, Any] | None = None) -> Iterator[Span]:
        validated = _validated_attributes(attributes)
        with self._tracer.start_as_current_span(
            name,
            kind=self._trace.SpanKind.CLIENT,
            attributes=validated,
            # El registro de la excepción y el estado de error los hace este
            # método a mano (sólo el nombre de la clase, nunca su mensaje ni
            # su traza): el comportamiento automático de
            # `start_as_current_span` grabaría el `str(exception)` completo,
            # que puede llevar el dato sensible que el span nunca debe ver.
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                yield span
            except Exception as exc:
                error_type = type(exc).__name__
                span.record_exception(
                    exc,
                    attributes={
                        "exception.type": error_type,
                        "exception.message": error_type,
                        "exception.stacktrace": error_type,
                    },
                )
                span.set_status(self._trace.Status(self._trace.StatusCode.ERROR, error_type))
                raise


def _validated_attributes(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    if not attributes:
        return {}
    unknown = set(attributes) - ALLOWED_SPAN_ATTRIBUTES
    if unknown:
        raise InvalidArgumentException(
            "atributo de span de rayito no permitido: " + ", ".join(sorted(unknown))
        )
    return dict(attributes)


def instrumentation_for(tracer_provider: TracerProviderLike | None) -> Instrumentation:
    """`None` (por defecto) → `NOOP`: cero overhead y ningún import de
    `opentelemetry`. Con un proveedor, construye el tracer `rayito` ya mismo
    (una vez, en `create()`/`connect()`) para que cada `span()` posterior
    sólo abra y cierre un span de ese tracer."""
    if tracer_provider is None:
        return NOOP
    return _OtelInstrumentation(tracer_provider)
