"""Modelos con la forma de E2B 2.51 que difieren de los nativos:
`SandboxInfo`, `SandboxMetrics`, `PtySize`, `SandboxState`, `SandboxQuery`
y los paginadores. Los que ya coinciden (`Execution`, `Result`,
`EntryInfo`...) se re-exportan desde `rayito.e2b` sin copiarlos."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from rayito._models import (
    DEFAULT_PTY_COLS,
    DEFAULT_PTY_ROWS,
    SandboxListItem,
    validate_pty_dimension,
)

if TYPE_CHECKING:
    from rayito.sandbox_async.listing import AsyncSandboxListPaginator
    from rayito.sandbox_sync.listing import SandboxListPaginator


class SandboxState(StrEnum):
    """Los dos estados que E2B expone; el de AWS queda en `SandboxInfo.raw_state`."""

    RUNNING = "running"
    PAUSED = "paused"


@dataclass(frozen=True)
class SandboxQuery:
    """Filtro de `Sandbox.list(query=...)` en el orden de campos de E2B.

    `metadata` es un subconjunto exacto (sólo sobre sandboxes `RUNNING`:
    leerlo despertaría uno pausado); `state` los estados de E2B;
    `started_after` "en o después"; `template` el nombre o ARN de la imagen.
    """

    metadata: dict[str, str] | None = None
    state: list[SandboxState] | None = None
    started_after: datetime | None = None
    template: str | None = None


@dataclass(frozen=True)
class SandboxInfo:
    """`SandboxInfo`/`ListedSandbox` de E2B 2.x, con `raw_state` al final.

    `sandbox_domain` es el endpoint (`None` en los items de `list()`);
    `template_id` es el ARN de la imagen y `name` su nombre; `metadata` es
    `None` cuando no se leyó del agente; `end_at` es el plazo lógico vigente
    (`None` en los items de `list()`); `cpu_count`, `memory_mb` y
    `envd_version` (la versión de `rayd`) son `None` si no se leyeron del
    agente; `lifecycle` es `{"on_timeout", "auto_resume"}` o `None` sin plazo
    gestionado; `network` es `{"allow_out", "deny_out"}` o `None` si no se
    leyó ninguna política; `allow_internet_access` es `False` sólo cuando la
    política leída lo deniega todo; `volume_mounts` siempre está vacío.
    `state` colapsa `PENDING|RUNNING` en `RUNNING` y `SUSPENDING|SUSPENDED`
    en `PAUSED`; `raw_state` conserva el de AWS.
    """

    sandbox_id: str
    sandbox_domain: str | None
    template_id: str
    name: str | None
    metadata: dict[str, str] | None
    started_at: datetime
    end_at: datetime | None
    state: SandboxState
    cpu_count: int | None
    memory_mb: int | None
    envd_version: str | None
    allow_internet_access: bool | None = None
    network: dict[str, list[str]] | None = None
    lifecycle: dict[str, Any] | None = None
    volume_mounts: list[dict[str, str]] = field(default_factory=list)
    raw_state: str = ""


ListedSandbox = SandboxInfo


@dataclass(frozen=True)
class SandboxMetrics:
    """`SandboxMetrics` de E2B: una muestra en bytes. `mem_cache` es la page
    cache (`Cached` de `/proc/meminfo`), 0 en un agente anterior a M9."""

    timestamp: datetime
    cpu_used_pct: float
    cpu_count: int
    mem_used: int
    mem_total: int
    disk_used: int
    disk_total: int
    mem_cache: int = 0


@dataclass(frozen=True)
class PtySize:
    """`PtySize(rows, cols)` en el orden de campos de E2B; el nativo es `(cols, rows)`."""

    rows: int = DEFAULT_PTY_ROWS
    cols: int = DEFAULT_PTY_COLS

    def __post_init__(self) -> None:
        validate_pty_dimension(self.rows, field="rows")
        validate_pty_dimension(self.cols, field="cols")


ItemMapper = Callable[[SandboxListItem], SandboxInfo]


class SandboxPaginator:
    """`SandboxPaginator` de E2B sobre el paginador nativo de
    `Sandbox.paginate`: `has_next` y `next_token` (opaco, reanudable con
    `Sandbox.list(next_token=...)`) son los nativos y `next_items()` devuelve
    `SandboxInfo` de E2B. Las sondas de metadatos de `list(query=...)`
    ocurren dentro de `next_items()`."""

    def __init__(self, native: SandboxListPaginator, *, mapper: ItemMapper) -> None:
        self._native = native
        self._mapper = mapper

    @property
    def has_next(self) -> bool:
        return self._native.has_next

    @property
    def next_token(self) -> str | None:
        return self._native.next_token

    def next_items(self) -> list[SandboxInfo]:
        return [self._mapper(item) for item in self._native.next_items()]


class AsyncSandboxPaginator:
    """`AsyncSandboxPaginator`: el mismo contrato que `SandboxPaginator`
    sobre el paginador nativo asíncrono."""

    def __init__(self, native: AsyncSandboxListPaginator, *, mapper: ItemMapper) -> None:
        self._native = native
        self._mapper = mapper

    @property
    def has_next(self) -> bool:
        return self._native.has_next

    @property
    def next_token(self) -> str | None:
        return self._native.next_token

    async def next_items(self) -> list[SandboxInfo]:
        return [self._mapper(item) for item in await self._native.next_items()]
