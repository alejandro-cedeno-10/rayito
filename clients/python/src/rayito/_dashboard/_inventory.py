"""Inventario de sandboxes con la memoria de su imagen: lo comparten `Fleet`
(la lista del panel), `Quotas` (la memoria asignada) y `UsageCost` (la
estimación). Reutiliza `ConventionCatalog` de `_size_catalog`: una
`GetMicrovmImageVersion` gratuita por versión de imagen y proceso, cacheada.
"""

from __future__ import annotations

from collections.abc import Iterable

from rayito._dashboard._domain import SandboxView
from rayito._dashboard._ports import ManagementPlane
from rayito._size_catalog import DEFAULT_SIZE_CATALOG, SizeCatalog
from rayito.exceptions import AuthenticationException, SandboxException

MemoryKey = tuple[str, str]


class InventoryReader:
    """Lista los MicroVMs y resuelve la memoria de cada versión de imagen una
    sola vez por lectura. Una versión ilegible (sin permiso, borrada) deja
    `memory_mib=None` en vez de romper el inventario."""

    def __init__(self, plane: ManagementPlane, catalog: SizeCatalog = DEFAULT_SIZE_CATALOG) -> None:
        self._plane = plane
        self._catalog = catalog

    def views(
        self, *, states: Iterable[str] | None = None, image_arn: str | None = None
    ) -> list[SandboxView]:
        resolved: dict[MemoryKey, int | None] = {}
        return [
            SandboxView(
                sandbox_id=item.sandbox_id,
                state=item.state,
                template=item.template,
                template_name=item.template_name,
                template_version=item.template_version,
                started_at=item.started_at,
                memory_mib=self._memory_mib(resolved, item.template, item.template_version),
            )
            for item in self._plane.list_microvms(image_arn=image_arn, states=states)
        ]

    def _memory_mib(
        self, resolved: dict[MemoryKey, int | None], arn: str, version: str
    ) -> int | None:
        key = (arn, version)
        if key not in resolved:
            resolved[key] = self._lookup(arn, version)
        return resolved[key]

    def _lookup(self, arn: str, version: str) -> int | None:
        try:
            return self._catalog.minimum_memory_mib(self._plane, arn, version)
        except (SandboxException, AuthenticationException):
            return None
