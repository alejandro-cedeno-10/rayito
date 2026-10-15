"""Fakes en memoria de los puertos del panel de gestión: un plano de control
(inventario, acciones, memoria por versión de imagen), una fuente de cuotas,
una de coste y un historial de eventos. Cada fake anota sus llamadas, así
los tests comprueban qué se llamó y, sobre todo, qué no."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from datetime import UTC, date, datetime
from typing import Any

from rayito._dashboard._domain import (
    AppliedQuota,
    CostQueryResult,
    Granularity,
    ImageSummary,
    ImageVersionSummary,
)
from rayito._lifecycle_events._domain import EventRecord
from rayito._models import ImageVersionInfo, SandboxListItem
from rayito.exceptions import SandboxException, SandboxNotFoundException

from .conftest import ACCOUNT_ID, IMAGE_ARN, REGION

NOW = datetime(2026, 10, 13, 12, 0, 0, tzinfo=UTC)
OTHER_IMAGE_ARN = f"arn:aws:lambda:{REGION}:{ACCOUNT_ID}:microvm-image:rayito-base-8gb"


def sandbox_id(number: int) -> str:
    return f"microvm-00000000-0000-0000-0000-{number:012d}"


def list_item(
    number: int,
    *,
    state: str = "RUNNING",
    image_arn: str = IMAGE_ARN,
    version: str = "1.0",
    started_at: datetime = NOW,
) -> SandboxListItem:
    return SandboxListItem(
        sandbox_id=sandbox_id(number),
        state=state,
        template=image_arn,
        template_version=version,
        started_at=started_at,
    )


class FakePlane:
    """`ManagementPlane` falso. `memory` mapea `(imagen, versión)` a MiB; una
    versión ausente lanza `SandboxNotFoundException`. `fail` programa la
    excepción de una acción sobre un sandbox."""

    def __init__(
        self,
        items: Sequence[SandboxListItem] = (),
        memory: dict[tuple[str, str], int] | None = None,
        region: str = REGION,
    ) -> None:
        self.items = list(items)
        self.memory = memory if memory is not None else {(IMAGE_ARN, "1.0"): 2048}
        self._region = region
        self.calls: list[tuple[str, Any]] = []
        self.fail: dict[tuple[str, str], Exception] = {}
        self.unchanged: set[tuple[str, str]] = set()

    @property
    def region(self) -> str:
        return self._region

    def resolve_template_arn(self, template: str) -> str:
        self.calls.append(("resolve_template_arn", template))
        return template if template.startswith("arn:") else IMAGE_ARN

    def list_microvms(
        self,
        *,
        image_arn: str | None = None,
        image_version: str | None = None,
        states: Iterable[str] | None = None,
    ) -> Iterator[SandboxListItem]:
        wanted = None if states is None else frozenset(states)
        self.calls.append(("list_microvms", None if wanted is None else sorted(wanted)))
        for item in self.items:
            if image_arn is not None and item.template != image_arn:
                continue
            if wanted is None and item.state in {"TERMINATING", "TERMINATED"}:
                continue
            if wanted is not None and item.state not in wanted:
                continue
            yield item

    def get_microvm_image_version(self, image_arn: str, image_version: str) -> ImageVersionInfo:
        self.calls.append(("get_microvm_image_version", (image_arn, image_version)))
        try:
            return ImageVersionInfo(minimum_memory_mib=self.memory[(image_arn, image_version)])
        except KeyError:
            raise SandboxNotFoundException("versión de imagen desconocida") from None

    def terminate_microvm(self, sandbox_id: str) -> bool:
        return self._act("terminate_microvm", sandbox_id)

    def suspend_microvm(self, sandbox_id: str) -> bool:
        return self._act("suspend_microvm", sandbox_id)

    def resume_microvm(self, sandbox_id: str) -> bool:
        return self._act("resume_microvm", sandbox_id)

    def _act(self, operation: str, sandbox_id: str) -> bool:
        self.calls.append((operation, sandbox_id))
        failure = self.fail.get((operation, sandbox_id))
        if failure is not None:
            raise failure
        return (operation, sandbox_id) not in self.unchanged

    def operations(self) -> list[str]:
        return [name for name, _ in self.calls]


class FakeQuotaSource:
    def __init__(self, applied: Sequence[AppliedQuota]) -> None:
        self.applied = list(applied)
        self.calls = 0

    def list_applied_quotas(self) -> list[AppliedQuota]:
        self.calls += 1
        return list(self.applied)


class FakeCostSource:
    def __init__(self, result: CostQueryResult) -> None:
        self.result = result
        self.queries: list[tuple[date, date, str, str | None]] = []

    def get_cost_and_usage(
        self, *, start: date, end: date, granularity: Granularity, region: str | None
    ) -> CostQueryResult:
        self.queries.append((start, end, granularity, region))
        return self.result


class FakeImageCatalog:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def list_images(self, *, name_filter: str | None = None) -> list[ImageSummary]:
        self.calls.append(("list_images", name_filter))
        return [
            ImageSummary("rayito-base", IMAGE_ARN, "CREATED", "2.0", None, NOW),
        ]

    def list_versions(self, image_arn: str) -> list[ImageVersionSummary]:
        self.calls.append(("list_versions", image_arn))
        return [ImageVersionSummary("2.0", "SUCCESSFUL", "ACTIVE", "1", "artifact.zip", 2048, NOW)]


class FakeEvents:
    def __init__(self, records: Sequence[EventRecord] = ()) -> None:
        self.records = list(records)
        self.calls: list[dict[str, Any]] = []

    def get_events(
        self,
        *,
        sandbox_id: str | None = None,
        types: Sequence[str] | None = None,
        limit: int = 100,
        order: str = "desc",
    ) -> list[EventRecord]:
        self.calls.append(
            {"sandbox_id": sandbox_id, "types": types, "limit": limit, "order": order}
        )
        return list(self.records)


def applied_defaults(region: str = REGION, *, memory_gb: float | None = None) -> list[AppliedQuota]:
    """Todas las cuotas del catálogo en su valor por defecto de `region`."""
    from rayito._dashboard._domain import quota_default
    from rayito._limits import QUOTA_NAMES

    applied: list[AppliedQuota] = []
    for code, name in QUOTA_NAMES.items():
        value = float(quota_default(code, region))
        if memory_gb is not None and code == "L-CD1C0CC4":
            value = memory_gb
        applied.append(AppliedQuota(code, name, value, adjustable=True))
    return applied


class RaisingError(SandboxException):
    """Una excepción de AWS con código, para las acciones que fallan."""

    def __init__(self, code: str) -> None:
        super().__init__("mensaje que no debe salir", aws_code=code)
