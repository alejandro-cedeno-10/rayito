"""Validación de `events=` en `Sandbox.create()` (`_feature_options.plan_features`):
el tipo de la opción y que `logging=` mande los logs del sandbox a
CloudWatch, que es de donde el forwarder lee las líneas de evento. Pura:
ninguna llamada a AWS.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from rayito._sandbox_base import LoggingOption, logging_config
from rayito.exceptions import InvalidArgumentException

if TYPE_CHECKING:
    from rayito._lifecycle_events._service import LifecycleEvents

#: `logging_config` sólo usa el nombre de la plantilla para el log group de
#: `"cloudwatch"`; aquí sólo importa qué clave resuelve (`cloudWatch` o
#: `disabled`), así que cualquier nombre sirve.
_ANY_TEMPLATE: Final = "events-validation"
_CLOUDWATCH_KEY: Final = "cloudWatch"


def sends_logs_to_cloudwatch(logging: LoggingOption) -> bool:
    """`True` para `"cloudwatch"` y para un `{"cloudWatch": {...}}` propio,
    vía el mismo resolver que usa `run-microvm`; un valor inválido lanza
    su `InvalidArgumentException`."""
    return _CLOUDWATCH_KEY in logging_config(logging, template_name=_ANY_TEMPLATE)


def validate_events_option(events: object, logging: LoggingOption) -> LifecycleEvents:
    """`events=` debe ser un `LifecycleEvents`/`AsyncLifecycleEvents` y
    `logging=` debe enviar a CloudWatch; si no, `InvalidArgumentException`
    antes de cualquier llamada a AWS. Devuelve el `LifecycleEvents` síncrono
    que construirá la sección (el propio, o el que envuelve un
    `AsyncLifecycleEvents`): `resolve_sections` corre en un hilo también en
    `AsyncSandbox.create()`, así que una sola implementación sirve a los dos."""
    from rayito._lifecycle_events._service import LifecycleEvents
    from rayito._lifecycle_events._service_async import AsyncLifecycleEvents

    if not isinstance(events, LifecycleEvents | AsyncLifecycleEvents):
        raise InvalidArgumentException("events= debe ser un LifecycleEvents o AsyncLifecycleEvents")
    if not sends_logs_to_cloudwatch(logging):
        raise InvalidArgumentException(
            'events= necesita logging="cloudwatch" (o {"cloudWatch": {...}}): el forwarder '
            "lee las líneas de evento de los logs del sandbox"
        )
    return events if isinstance(events, LifecycleEvents) else events._sync_events
