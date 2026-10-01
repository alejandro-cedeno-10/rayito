"""Seam de las siete opciones 0.6 de `Sandbox.create()` (M15 foundations):
`FeatureOptions` las agrupa y `plan_features()` decide, antes de cualquier
llamada a AWS, qué hacer con cada una. Mientras una función siga siendo un
stub (`features::slot::Unsupported` del lado de `rayd`, y del lado del SDK
cualquier opción puesta en algo distinto de `None`), `plan_features` lanza
`UnimplementedError` nombrando el cambio OpenSpec que la trae — antes de
`run-microvm`, así que un sandbox nunca llega a lanzarse por una opción que
el SDK aún no sabe cumplir. Con las siete en `None` (el valor por defecto)
`plan_features` no hace nada: ni un `ConfigureSandbox`, ni un cliente AWS
nuevo, el comportamiento exacto de 0.5.x.

`gateways=` (m15-secrets-gateway) es la primera en dejar de ser un stub:
`plan_features` sólo valida su forma (pura, cero AWS) y devuelve un
`GatewaySectionFactory` en `FeaturePlan.configure_sections` — el
`ConfigureSandbox` de verdad, con cada cabecera ya resuelta, lo manda
`create()`/`take()` una vez conocen la `SecretCache` y el agente confirmó
el flag en `Health.features`. Cada función sustituye su propia rama por
una implementación real en su propio cambio OpenSpec; ni esta firma ni
`FeatureOptions`/`FeaturePlan` cambian para eso.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from rayito._secret_gateway import GatewaySectionFactory, validate_gateways
from rayito.exceptions import UnimplementedError

MOUNTS_CHANGE: Final = "m15-s3-mounts"
VOLUMES_CHANGE: Final = "m15-efs-volumes"
SIZE_CHANGE: Final = "m15-sizes-catalog"
EVENTS_CHANGE: Final = "m15-events-webhooks"
TELEMETRY_CHANGE: Final = "m15-rayd-otlp"
GATEWAYS_CHANGE: Final = "m15-secrets-gateway"
DOMAIN_CHANGE: Final = "m15-custom-domain"


@dataclass(frozen=True)
class FeatureOptions:
    """Los siete kwargs 0.6 de `Sandbox.create()`/`AsyncSandbox.create()`,
    agrupados; cada campo es `None` salvo que el llamante lo ponga. Los
    tipos son deliberadamente laxos (`Any`/`Mapping[str, Any]`): las clases
    concretas (`S3Mount`, `EfsVolume`, `SizeRequest`, ...) las define cada
    función en su propio módulo, nunca aquí.
    """

    mounts: Mapping[str, Any] | None = None
    volumes: Mapping[str, Any] | None = None
    size: Any | None = None
    events: Any | None = None
    telemetry: Any | None = None
    gateways: Mapping[str, Any] | None = None
    domain: Any | None = None


@dataclass(frozen=True)
class FeaturePlan:
    """Lo que `create()` hace con las opciones una vez alguna función deja
    de ser un stub: ajustes al plan de lanzamiento, secciones de
    `ConfigureSandbox` a enviar tras `Health`, hooks a correr después de
    que el agente esté listo y antes de matar el sandbox. Vacío en 0.6
    foundations a propósito: `plan_features` lanza antes de llegar a
    construir uno si alguna opción estaba puesta.
    """

    configure_sections: tuple[Any, ...] = ()


def plan_features(options: FeatureOptions, *, image_variant: str | None = None) -> FeaturePlan:
    """Punto único por el que `create()`/`take()` pasan las siete opciones
    0.6. `image_variant` (de `_role_policy.resolve_image_variant`) queda
    para cuando una función real lo necesite (s3-mounts, efs-volumes,
    rayd-otlp con rol exigen la variante caps); ninguna rama de hoy lo usa.
    No hace ninguna llamada a AWS ni construye ningún cliente.
    """
    del image_variant
    if options.mounts is not None:
        raise UnimplementedError("mounts=", f"llega en 0.6 ({MOUNTS_CHANGE})")
    if options.volumes is not None:
        raise UnimplementedError("volumes=", f"llega en 0.6 ({VOLUMES_CHANGE})")
    if options.size is not None:
        raise UnimplementedError("size=", f"llega en 0.6 ({SIZE_CHANGE})")
    if options.events is not None:
        raise UnimplementedError("events=", f"llega en 0.6 ({EVENTS_CHANGE})")
    if options.telemetry is not None:
        raise UnimplementedError("telemetry=", f"llega en 0.6 ({TELEMETRY_CHANGE})")
    sections: tuple[Any, ...] = ()
    if options.gateways is not None:
        # Sólo valida la forma (ninguna llamada a AWS: `validate_gateways`
        # es pura). La `SecretCache` que de verdad resuelve cada cabecera
        # llega después, cuando `create()`/`take()` ya la calcularon para
        # `secrets=` — ver `GatewaySectionFactory`.
        sections = (GatewaySectionFactory(validate_gateways(options.gateways)),)
    if options.domain is not None:
        raise UnimplementedError("domain=", f"llega en 0.6 ({DOMAIN_CHANGE})")
    return FeaturePlan(configure_sections=sections)
