"""Listado paginado del `Sandbox` síncrono: `SandboxListPaginator`
(`Sandbox.paginate`) y el generador que comparte con `Sandbox.list`.

Sin `order` el recorrido es perezoso: cada `next_items()` pide páginas de
`list-microvms` sólo hasta llenar `limit` y la sonda de metadatos sólo toca
los items que consume. Con `order` la primera llamada recorre todas las
páginas (AWS no ordena): O(páginas), más la sonda O(n) con `metadata`.
"""

from __future__ import annotations

import dataclasses
import itertools
from collections.abc import Callable, Iterator, Mapping

from rayito._aws import ControlPlane
from rayito._listing_base import EXHAUSTED_MESSAGE, ListingRequest, ListingSession, fetch_page
from rayito._models import SandboxInfo, SandboxListItem
from rayito._sandbox_base import metadata_matches
from rayito._transport import TransportSettings
from rayito.exceptions import SandboxException, SandboxNotFoundException

MetadataProbe = Callable[
    [ControlPlane, SandboxInfo, TransportSettings, float], dict[str, str] | None
]


@dataclasses.dataclass(frozen=True)
class ListingIo:
    """La E/S de un listado: el plano de control y la sonda de metadatos
    (un `Health` por un canal dedicado) con su transporte y su plazo."""

    plane: ControlPlane
    probe: MetadataProbe
    transport: TransportSettings
    request_timeout: float

    def matches(self, session: ListingSession) -> Iterator[SandboxListItem]:
        """Los items que pasan los filtros, en el orden de `list-microvms`,
        pidiendo cada página sólo cuando la anterior se agotó."""
        wanted = session.wanted_metadata
        while True:
            page_request = session.walk.page_to_fetch()
            if page_request is not None:
                session.walk.accept_page(fetch_page(self.plane, session.filters, page_request))
                continue
            item = session.walk.next_raw()
            if item is None:
                return
            if not session.filters.accepts(item):
                continue
            if wanted is None:
                yield item
                continue
            matched = self.metadata_match(item, wanted)
            if matched is not None:
                yield matched

    def metadata_match(
        self, item: SandboxListItem, wanted: Mapping[str, str]
    ) -> SandboxListItem | None:
        """La sonda de M6: `get-microvm` (omitido si ya no existe o no está
        `RUNNING`) y un `Health`; devuelve el item con `metadata` relleno."""
        try:
            info = self.plane.get_microvm(item.sandbox_id)
        except SandboxNotFoundException:
            return None
        read = self.probe(self.plane, info, self.transport, self.request_timeout)
        if not metadata_matches(read, wanted):
            return None
        return dataclasses.replace(item, metadata=read)


def stream_listing(
    io: ListingIo, request: ListingRequest, image_arn: str | None
) -> Iterator[SandboxListItem]:
    """El recorrido completo de `Sandbox.list()`: perezoso sin `order`; con
    `order` recoge todas las páginas y luego sirve en orden."""
    session = ListingSession(request, image_arn)
    order = request.order
    if order is None:
        yield from io.matches(session)
        return
    yield from session.start_ordered(order, io.matches(session)).take(None)


class SandboxListPaginator:
    """Un listado reanudable (`Sandbox.paginate`), la forma del
    `SandboxPaginator` de E2B.

    `next_token` es opaco: guárdalo y pásalo a un `Sandbox.paginate()`
    nuevo con los mismos filtros para seguir donde se quedó. No lo loguees:
    lleva el ARN de la imagen y la huella de los filtros.
    """

    def __init__(
        self,
        *,
        io: ListingIo,
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

    def next_items(self) -> list[SandboxListItem]:
        """La siguiente página de hasta `limit` items (todos sin `limit`).
        Puede venir vacía mientras `has_next` siga siendo `True`."""
        if not self._has_next:
            raise SandboxException(EXHAUSTED_MESSAGE)
        session = self._session if self._session is not None else self._open_session()
        served = self._serve(session)
        self._has_next = session.has_more
        self._next_token = session.next_token()
        return served

    def _open_session(self) -> ListingSession:
        template = self._request.template
        image_arn = self._resolve_template_arn(template) if template else None
        self._session = ListingSession(self._request, image_arn)
        return self._session

    def _serve(self, session: ListingSession) -> list[SandboxListItem]:
        order = self._request.order
        if order is None:
            return list(itertools.islice(self._io.matches(session), self._request.limit))
        if session.ordered is None:
            session.start_ordered(order, self._io.matches(session))
        return session.ordered_take(self._request.limit)
