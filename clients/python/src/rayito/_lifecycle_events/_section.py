"""`ConfigureSection` de `m15-events-webhooks`: ya con `k_sbx` derivado y la
metadata del sandbox resueltas (`LifecycleEvents._build_section`, llamado
después de `run-microvm`, cuando se conocen `sandbox_id`/`image_arn`/
`image_version` — nunca antes, por eso `_feature_options.plan_features` sólo
guarda el `LifecycleEvents` ya validado y `planned_sections` añade un
`LifecycleEventsSectionFactory` en cuanto `create()` conoce esos hechos).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from rayito._configure_base import ImmediateSection

if TYPE_CHECKING:
    from rayito._lifecycle_events._service import LifecycleEvents
    from rayito._secrets import SecretCache
    from rayito.v1 import configure_pb2

SECTION_NAME: Final = "lifecycle_events"


@dataclass(frozen=True)
class LifecycleEventsSection(ImmediateSection):
    """`rayito._configure_base.ConfigureSection`: `rayd` la aplica en el acto
    (nunca `PENDING`); un `INVALID` (clave o `sandbox_id` vacíos) lanza por
    `raise_section_error`, como `gateways=`/`telemetry=`."""

    #: `k_sbx`, nunca en `repr` (tracebacks con locales, Sentry, depuradores).
    sandbox_key: bytes = field(repr=False)
    sandbox_id: str
    image_arn: str
    image_version: str

    @property
    def section(self) -> str:
        return SECTION_NAME

    @property
    def required_flag(self) -> str:
        return SECTION_NAME

    def fill(self, request: configure_pb2.ConfigureRequest) -> None:
        from rayito.v1 import lifecycle_events_pb2

        request.lifecycle_events.CopyFrom(
            lifecycle_events_pb2.LifecycleEventsConfig(
                sandbox_key=self.sandbox_key,
                sandbox_id=self.sandbox_id,
                image_arn=self.image_arn,
                image_version=self.image_version,
            )
        )


@dataclass(frozen=True)
class LifecycleEventsSectionFactory:
    """`_configure_base.SectionFactory` de `events=`: los hechos de
    `run-microvm` ya fijados; `resolve_sections` lo invoca justo antes del
    `Configure` y es entonces cuando `LifecycleEvents` lee la clave del
    stack (un `GetSecretValue` por instancia, con su propia sesión: la
    clave del stack no es un secreto de `secrets=`, así que la `SecretCache`
    del handle no se usa). `create()` asíncrono ya llama a
    `resolve_sections` en un hilo, así que esta lectura bloqueante nunca
    corre en el bucle de eventos."""

    events: LifecycleEvents
    sandbox_id: str
    image_arn: str
    image_version: str

    def __call__(self, cache: SecretCache) -> LifecycleEventsSection:
        del cache
        return self.events._build_section(
            sandbox_id=self.sandbox_id,
            image_arn=self.image_arn,
            image_version=self.image_version,
        )
