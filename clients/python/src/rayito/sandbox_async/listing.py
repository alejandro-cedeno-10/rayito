"""Listado paginado del `AsyncSandbox`: `AsyncSandboxListPaginator`
(`AsyncSandbox.paginate`) y el recorrido que comparte con
`AsyncSandbox.list`. La misma semántica que `sandbox_sync.listing`, con el
plano de control (boto3) en un hilo y las sondas de metadatos por
`grpc.aio`."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping

from rayito._aws import ControlPlane
from rayito._listing_base import EXHAUSTED_MESSAGE, ListingRequest, ListingSession, fetch_page
from rayito._models import SandboxInfo, SandboxListItem
from rayito._sandbox_base import metadata_matches
from rayito._transport import TransportSettings
from rayito.exceptions import SandboxException, SandboxNotFoundException

AsyncMetadataProbe = Callable[
    [ControlPlane, SandboxInfo, TransportSettings, float], Awaitable[dict[str, str] | None]
]


@dataclasses.dataclass(frozen=True)
class AsyncListingIo:
    """`sandbox_sync.listing.ListingIo` con la sonda de `Health` asíncrona."""

    plane: ControlPlane
    probe: AsyncMetadataProbe
    transport: TransportSettings
    request_timeout: float

    async def matches(self, session: ListingSession) -> AsyncGenerator[SandboxListItem, None]:
        wanted = session.wanted_metadata
        while True:
            page_request = session.walk.page_to_fetch()
            if page_request is not None:
                page = await asyncio.to_thread(
                    fetch_page, self.plane, session.filters, page_request
                )
                session.walk.accept_page(page)
                continue
            item = session.walk.next_raw()
            if item is None:
                return
            if not session.filters.accepts(item):
                continue
            if wanted is None:
                yield item
                continue
            matched = await self.metadata_match(item, wanted)
            if matched is not None:
                yield matched

    async def metadata_match(
        self, item: SandboxListItem, wanted: Mapping[str, str]
    ) -> SandboxListItem | None:
        try:
            info = await asyncio.to_thread(self.plane.get_microvm, item.sandbox_id)
        except SandboxNotFoundException:
            return None
        read = await self.probe(self.plane, info, self.transport, self.request_timeout)
        if not metadata_matches(read, wanted):
            return None
        return dataclasses.replace(item, metadata=read)

    async def take(self, session: ListingSession, limit: int | None) -> list[SandboxListItem]:
        """Hasta `limit` coincidencias sin pedir la siguiente: con `metadata`
        nunca sondea un item que no va a servir."""
        served: list[SandboxListItem] = []
        async with contextlib.aclosing(self.matches(session)) as matches:
            async for item in matches:
                served.append(item)
                if limit is not None and len(served) >= limit:
                    break
        return served


async def collect_listing(
    io: AsyncListingIo, request: ListingRequest, image_arn: str | None
) -> list[SandboxListItem]:
    """El recorrido completo de `AsyncSandbox.list()`, ordenado con `order`."""
    session = ListingSession(request, image_arn)
    matches = await io.take(session, None)
    order = request.order
    if order is None:
        return matches
    return session.start_ordered(order, matches).take(None)


class AsyncSandboxListPaginator:
    """`sandbox_sync.listing.SandboxListPaginator` con `next_items()`
    asíncrono. Construirlo no hace E/S."""

    def __init__(
        self,
        *,
        io: AsyncListingIo,
        request: ListingRequest,
        resolve_template_arn: Callable[[str], str],
    ) -> None:
        self._io = io
        self._request = request
        self._resolve_template_arn = resolve_template_arn
        self._session: ListingSession | None = None
        self._has_next = True
        self._next_token = request.next_token

    @property
    def has_next(self) -> bool:
        return self._has_next

    @property
    def next_token(self) -> str | None:
        """Antes de la primera llamada, el token recibido; después, el cursor
        mientras `has_next`, si no `None`."""
        return self._next_token

    async def next_items(self) -> list[SandboxListItem]:
        if not self._has_next:
            raise SandboxException(EXHAUSTED_MESSAGE)
        session = self._session if self._session is not None else await self._open_session()
        served = await self._serve(session)
        self._has_next = session.has_more
        self._next_token = session.next_token()
        return served

    async def _open_session(self) -> ListingSession:
        template = self._request.template
        image_arn = (
            await asyncio.to_thread(self._resolve_template_arn, template) if template else None
        )
        self._session = ListingSession(self._request, image_arn)
        return self._session

    async def _serve(self, session: ListingSession) -> list[SandboxListItem]:
        order = self._request.order
        if order is None:
            return await self._io.take(session, self._request.limit)
        if session.ordered is None:
            session.start_ordered(order, await self._io.take(session, None))
        return session.ordered_take(self._request.limit)
