"""Puertos del panel de gestión (ADR-026, decisión D1). `SandboxInventory`
y `Actions` son el subconjunto de `ControlPlane` que necesitan (el plano de
control ya los cumple, sin adaptador nuevo); `ImageCatalog`, `QuotaSource` y
`CostSource` tienen un adaptador por API de AWS (`_aws.py`) y
`EventsHistory` lo cumple `LifecycleEvents.get_events`. Todos son
`Protocol`: los tests usan fakes, nunca AWS.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from datetime import date
from typing import Protocol

from rayito._dashboard._domain import (
    AppliedQuota,
    CostQueryResult,
    Granularity,
    ImageSummary,
    ImageVersionSummary,
)
from rayito._lifecycle_events._domain import EventRecord
from rayito._models import ImageVersionInfo, SandboxListItem


class SandboxInventory(Protocol):
    """Listar los MicroVMs de la región (`ListMicrovms`)."""

    def list_microvms(
        self,
        *,
        image_arn: str | None = None,
        image_version: str | None = None,
        states: Iterable[str] | None = None,
    ) -> Iterator[SandboxListItem]: ...


class Actions(Protocol):
    """Matar, suspender y reanudar un MicroVM; los tres son idempotentes en
    AWS y devuelven `False` cuando no había nada que hacer."""

    def terminate_microvm(self, sandbox_id: str) -> bool: ...

    def suspend_microvm(self, sandbox_id: str) -> bool: ...

    def resume_microvm(self, sandbox_id: str) -> bool: ...


class ManagementPlane(SandboxInventory, Actions, Protocol):
    """Lo que `Fleet` pide al plano de control: el inventario, las acciones,
    la resolución del nombre de imagen y la memoria de una versión."""

    @property
    def region(self) -> str: ...

    def resolve_template_arn(self, template: str) -> str: ...

    def get_microvm_image_version(self, image_arn: str, image_version: str) -> ImageVersionInfo: ...


class ImageCatalog(Protocol):
    """Imágenes de la cuenta y sus versiones (`ListMicrovmImages`,
    `ListMicrovmImageVersions`); la versión más nueva va primero."""

    def list_images(self, *, name_filter: str | None = None) -> list[ImageSummary]: ...

    def list_versions(self, image_arn: str) -> list[ImageVersionSummary]: ...


class QuotaSource(Protocol):
    """Las cuotas de Lambda MicroVMs aplicadas en la cuenta y región."""

    def list_applied_quotas(self) -> list[AppliedQuota]: ...


class CostSource(Protocol):
    """Coste de Lambda agrupado por usage type (Cost Explorer). Cada página
    de la consulta se factura: el resultado cuenta las peticiones."""

    def get_cost_and_usage(
        self, *, start: date, end: date, granularity: Granularity, region: str | None
    ) -> CostQueryResult: ...


class EventsHistory(Protocol):
    """El historial de eventos de ciclo de vida (pila `events-webhooks`)."""

    def get_events(
        self,
        *,
        sandbox_id: str | None = None,
        types: Sequence[str] | None = None,
        limit: int = ...,
        order: str = "desc",
    ) -> list[EventRecord]: ...
