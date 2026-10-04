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

`mounts=` (`m15-s3-mounts`) y `gateways=` (m15-secrets-gateway) ya no son
stubs. `mounts=`: `require_caps_for` corre aquí, antes de `run-microvm`,
cuando `image_variant` ya permite decidirlo (un nombre `rayito-<variant>`);
sobre cualquier otro nombre la decisión se difiere al agente
(`Health.features`, comprobado después de `/run` por `create()`/`take()` —
ver `_configure_base.require_capabilities`). `gateways=`: `plan_features`
sólo valida su forma (pura, cero AWS) y devuelve un `GatewaySectionFactory`
(`_configure_base.SectionFactory`) en `FeaturePlan.configure_sections` — el
`ConfigureSandbox` de verdad, con cada cabecera ya resuelta, lo manda
`create()`/`take()` una vez conocen la `SecretCache`. `telemetry=`
(m15-rayd-otlp) y `events=` (m15-events-webhooks) se validan aquí y su
sección se planea tras `run-microvm` (`planned_sections`), porque necesitan
hechos que sólo existen entonces. `volumes=` (m15-efs-volumes) se valida
aquí y `prepare_features` resuelve con EFS, antes de `run-microvm`, la IP de
mount target de cada volumen; su sección viaja en el mismo `Configure`.
Cada función sustituye
su propia rama por una implementación real en su propio cambio OpenSpec; ni
esta firma ni `FeatureOptions`/`FeaturePlan` cambian para eso.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, replace
from typing import TYPE_CHECKING, Any, Final

from rayito._aws import LazyClient
from rayito._configure_base import PlannedSection
from rayito._lifecycle_events._options import validate_events_option
from rayito._lifecycle_events._section import LifecycleEventsSectionFactory
from rayito._role_policy import require_caps_for
from rayito._s3_mounts import S3Mount, plan_s3_mounts
from rayito._secret_gateway import GatewaySectionFactory, validate_gateways
from rayito._telemetry_export import TelemetrySectionFactory
from rayito._telemetry_export import plan as plan_telemetry
from rayito._volumes._mount_targets import (
    MountTargetResolver,
    needs_mount_targets,
    resolve_mount_targets,
)
from rayito._volumes._section import EfsVolumesSection, VolumesRequest, plan_volumes
from rayito.exceptions import UnimplementedError

if TYPE_CHECKING:
    import boto3

    from rayito._lifecycle_events._service import LifecycleEvents
    from rayito._telemetry_export import TelemetryExport

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

    mounts: Mapping[str, S3Mount] | None = None
    volumes: Mapping[str, Any] | None = None
    size: Any | None = None
    events: Any | None = None
    telemetry: Any | None = None
    gateways: Mapping[str, Any] | None = None
    domain: Any | None = None


def relaunch_features(options: FeatureOptions) -> FeatureOptions:
    """Lo que `create()` guarda en `LaunchOptions.features` para que
    `reincarnate()` lance el sucesor con las mismas secciones de
    `ConfigureSandbox`: las siete opciones tal cual (copiando los
    `Mapping` para que una mutación posterior del llamante no cambie el
    relanzamiento) salvo `size`, que ya va dentro del ARN de plantilla
    resuelto (`LaunchOptions.template`); repetirlo sobre un ARN es
    `InvalidArgumentException` (`_sizing.apply_size_suffix`)."""
    return replace(
        options,
        mounts=None if options.mounts is None else dict(options.mounts),
        volumes=None if options.volumes is None else dict(options.volumes),
        gateways=None if options.gateways is None else dict(options.gateways),
        size=None,
    )


def feature_kwargs(options: FeatureOptions | None) -> dict[str, Any]:
    """Los kwargs de `create()` que reproducen `options` campo a campo
    (vacío sin opciones): una opción 0.6 nueva en `FeatureOptions` se
    reenvía sola en `reincarnate()`, sin tocar `launch_kwargs`."""
    if options is None:
        return {}
    return {option.name: getattr(options, option.name) for option in fields(options)}


@dataclass(frozen=True)
class FeaturePlan:
    """Lo que `create()` hace con las opciones 0.6 una vez validadas:
    `configure_sections`, las secciones de `ConfigureSandbox` que ya pueden
    planearse antes de `run-microvm` (`mounts=`, `gateways=`);
    `telemetry`, el `TelemetryExport` ya validado cuya sección necesita los
    hechos de imagen (el ARN y la versión de `run-microvm`, la memoria del
    primer `Health`); y `events`, el `LifecycleEvents` (síncrono, también
    para `AsyncLifecycleEvents`) cuya sección necesita además el
    `sandbox_id` para derivar `k_sbx`. `planned_sections` las junta en
    cuanto `create()` conoce esos hechos. `volumes`, el `volumes=` ya
    validado al que `prepare_features` añade las IPs de mount target antes de
    `run-microvm`, convirtiéndolo en una sección más de
    `configure_sections`.
    """

    configure_sections: tuple[PlannedSection, ...] = ()
    telemetry: TelemetryExport | None = None
    events: LifecycleEvents | None = None
    volumes: VolumesRequest | None = None


@dataclass(frozen=True)
class LaunchFacts:
    """Lo que sólo se sabe tras `run-microvm` y el primer `Health`."""

    sandbox_id: str
    image_arn: str
    image_version: str
    guest_memory_bytes: int | None


def planned_sections(plan: FeaturePlan, facts: LaunchFacts) -> tuple[PlannedSection, ...]:
    """Todas las secciones que `create()` manda en su único `Configure`:
    las de `plan.configure_sections` más, con `telemetry=`, un
    `TelemetrySectionFactory` y, con `events=`, un
    `LifecycleEventsSectionFactory`, ambos sobre `facts`. `main.py` nunca
    nombra una función concreta."""
    sections: list[PlannedSection] = list(plan.configure_sections)
    if plan.telemetry is not None:
        sections.append(
            TelemetrySectionFactory(
                plan.telemetry,
                image_arn=facts.image_arn,
                image_version=facts.image_version,
                guest_memory_bytes=facts.guest_memory_bytes,
            )
        )
    if plan.events is not None:
        sections.append(
            LifecycleEventsSectionFactory(
                plan.events,
                sandbox_id=facts.sandbox_id,
                image_arn=facts.image_arn,
                image_version=facts.image_version,
            )
        )
    return tuple(sections)


def plan_features(
    options: FeatureOptions,
    *,
    image_variant: str | None = None,
    logging: object = None,
    egress: Sequence[str] | None = None,
    execution_role_arn: str | None = None,
) -> FeaturePlan:
    """Punto único por el que `create()`/`take()` pasan las siete opciones
    0.6. `image_variant` (de `_role_policy.resolve_image_variant`) es la
    variante de imagen, cuando el nombre ya permite decidirla; `mounts=`,
    `volumes=` y `telemetry=` (con `OtlpAuth.execution_role()`) lo usan
    para exigir la variante caps antes de lanzar (`require_caps_for`, una
    comprobación puramente sobre el nombre de la imagen). `logging` es el `logging=` de
    `create()`: `events=` exige que mande los logs a CloudWatch. `egress` y
    `execution_role_arn` son los de `create()`: `volumes=` exige un único
    conector propio y un rol (`_volumes._section.plan_volumes`). No hace
    ninguna llamada a AWS ni construye ningún cliente.

    `events=` se valida aquí (tipo y `logging`) y viaja en
    `FeaturePlan.events`: su sección necesita `sandbox_id`/`image_arn`/
    `image_version`, que sólo existen tras `run-microvm`, así que
    `planned_sections` la añade entonces (ADR-020). La clave del stack se
    lee justo antes del `Configure`, nunca aquí.
    """
    sections: list[PlannedSection] = []
    if options.mounts is not None:
        require_caps_for("mounts=", image_variant)
        section = plan_s3_mounts(options.mounts)
        if section is not None:
            sections.append(section)
    volumes = (
        plan_volumes(
            options.volumes,
            image_variant=image_variant,
            egress=egress,
            execution_role_arn=execution_role_arn,
        )
        if options.volumes is not None
        else None
    )
    # `size=` (m15-sizes-catalog) ya no es un stub: no produce ninguna
    # sección de `ConfigureSandbox` (no es un ajuste del guest en marcha,
    # es qué imagen lanzar), así que `create()` la resuelve por su cuenta
    # con `_sizing.resolve_size`/`apply_size_suffix` antes de pedir el ARN
    # de la plantilla, y aquí no hay nada que comprobar ni que lanzar.
    events = (
        validate_events_option(options.events, logging)  # type: ignore[arg-type]
        if options.events is not None
        else None
    )
    telemetry = (
        plan_telemetry(options.telemetry, image_variant=image_variant)
        if options.telemetry is not None
        else None
    )
    if options.gateways is not None:
        # Sólo valida la forma (ninguna llamada a AWS: `validate_gateways`
        # es pura). La `SecretCache` que de verdad resuelve cada cabecera
        # llega después, cuando `create()`/`take()` ya la calcularon para
        # `secrets=` — ver `GatewaySectionFactory`.
        sections.append(GatewaySectionFactory(validate_gateways(options.gateways)))
    if options.domain is not None:
        raise UnimplementedError(
            "domain=",
            f"{DOMAIN_CHANGE} aún no se cablea a Sandbox.create(): usa rayito.CustomDomain "
            "(experimental) directamente",
        )
    return FeaturePlan(
        configure_sections=tuple(sections), telemetry=telemetry, events=events, volumes=volumes
    )


def prepare_features(
    plan: FeaturePlan, *, region: str | None, session: boto3.session.Session | None
) -> FeaturePlan:
    """Lo que `create()` resuelve con AWS después de `plan_features` y antes
    de `run-microvm`, para que un fallo no llegue a lanzar (ni a pagar) un
    MicroVM: hoy, la IP de mount target de cada volumen de `volumes=` que no
    la trae (`DescribeMountTargets`, credenciales del llamante, una llamada
    por sistema de ficheros). Sin `volumes=` devuelve `plan` tal cual, sin
    construir ningún cliente. `main.py` nunca nombra la función concreta."""
    if plan.volumes is None:
        return plan
    volumes = plan.volumes.volumes
    if needs_mount_targets(volumes):
        client = LazyClient("efs", region=region, session=session).get()
        volumes = resolve_mount_targets(volumes, MountTargetResolver(client))
    return replace(
        plan,
        configure_sections=(*plan.configure_sections, EfsVolumesSection(volumes)),
        volumes=None,
    )


async def aprepare_features(
    plan: FeaturePlan, *, region: str | None, session: boto3.session.Session | None
) -> FeaturePlan:
    """`prepare_features` para `AsyncSandbox.create()`: sin nada que
    resolver vuelve en el acto; si no, en un hilo (boto3 es bloqueante)."""
    if plan.volumes is None:
        return plan
    return await asyncio.to_thread(prepare_features, plan, region=region, session=session)
