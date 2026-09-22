"""Modelos con la forma de E2B que difieren de los nativos: `SandboxInfo`,
`SandboxMetrics`, `PtySize`, `SandboxState`, `SandboxQuery` y los
paginadores. Los que ya coinciden (`Execution`, `Result`, `EntryInfo`...)
se re-exportan desde `rayito.e2b` sin copiarlos."""

from __future__ import annotations

import itertools
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from rayito._models import DEFAULT_PTY_COLS, DEFAULT_PTY_ROWS, validate_pty_dimension


class SandboxState(StrEnum):
    """Los dos estados que E2B expone; el de AWS queda en `SandboxInfo.raw_state`."""

    RUNNING = "running"
    PAUSED = "paused"


@dataclass(frozen=True)
class SandboxQuery:
    """Filtro de `Sandbox.list(query=...)`: sólo `metadata` (subconjunto exacto)."""

    metadata: dict[str, str] | None = None


@dataclass(frozen=True)
class SandboxInfo:
    """`SandboxInfo`/`ListedSandbox` de E2B.

    `template_id` es el ARN de la imagen y `name` su nombre; `metadata` es
    `None` cuando no se leyó del agente; `end_at` es `started_at + timeout`
    (`None` en los items de `list()`, que no traen la duración); `state`
    colapsa `PENDING|RUNNING` en `RUNNING` y `SUSPENDING|SUSPENDED` en
    `PAUSED`, y `raw_state` conserva el de AWS.
    """

    sandbox_id: str
    template_id: str
    name: str
    metadata: dict[str, str] | None
    started_at: datetime
    end_at: datetime | None
    state: SandboxState
    raw_state: str


ListedSandbox = SandboxInfo


@dataclass(frozen=True)
class SandboxMetrics:
    """`SandboxMetrics` de E2B: una instantánea (bytes), nunca una serie."""

    timestamp: datetime
    cpu_used_pct: float
    cpu_count: int
    mem_used: int
    mem_total: int
    disk_used: int
    disk_total: int


@dataclass(frozen=True)
class PtySize:
    """`PtySize(rows, cols)` en el orden de campos de E2B; el nativo es `(cols, rows)`."""

    rows: int = DEFAULT_PTY_ROWS
    cols: int = DEFAULT_PTY_COLS

    def __post_init__(self) -> None:
        validate_pty_dimension(self.rows, field="rows")
        validate_pty_dimension(self.cols, field="cols")


class SandboxPaginator:
    """`SandboxPaginator` de E2B sobre el iterador nativo de `Sandbox.list`.

    `limit` es el tamaño de página; `next_token` es siempre `None` porque
    Rayito pagina `list-microvms` por dentro. La iteración es perezosa: las
    sondas de metadatos de `list(query=...)` ocurren dentro de `next_items()`,
    que mira un item más allá de la página para que `has_next` sea exacto.
    """

    def __init__(self, items: Iterator[SandboxInfo], *, limit: int | None = None) -> None:
        self._items = items
        self._limit = limit
        self._peeked: list[SandboxInfo] = []
        self._exhausted = False
        self._started = False

    @property
    def has_next(self) -> bool:
        if not self._started:
            return True
        return bool(self._peeked) or not self._exhausted

    @property
    def next_token(self) -> str | None:
        return None

    def next_items(self) -> list[SandboxInfo]:
        self._started = True
        page = self._peeked
        self._peeked = []
        if self._limit is None:
            page.extend(self._items)
            self._exhausted = True
            return page
        page.extend(itertools.islice(self._items, self._limit - len(page)))
        self._peek_one()
        return page

    def _peek_one(self) -> None:
        try:
            self._peeked = [next(self._items)]
        except StopIteration:
            self._exhausted = True


class AsyncSandboxPaginator:
    """`AsyncSandboxPaginator`: `AsyncSandbox.list` nativo devuelve la lista
    completa, así que la primera `next_items()` la recoge y las siguientes
    sirven páginas de `limit` items."""

    def __init__(
        self, collect: Callable[[], Awaitable[list[SandboxInfo]]], *, limit: int | None = None
    ) -> None:
        self._collect = collect
        self._limit = limit
        self._pending: list[SandboxInfo] | None = None

    @property
    def has_next(self) -> bool:
        return self._pending is None or bool(self._pending)

    @property
    def next_token(self) -> str | None:
        return None

    async def next_items(self) -> list[SandboxInfo]:
        if self._pending is None:
            self._pending = list(await self._collect())
        if self._limit is None:
            page, self._pending = self._pending, []
            return page
        page, self._pending = self._pending[: self._limit], self._pending[self._limit :]
        return page
