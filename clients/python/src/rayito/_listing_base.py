"""Núcleo puro del listado paginado, compartido por `Sandbox.paginate`,
`AsyncSandbox.paginate` y los dos `list()`: filtros canónicos, el token
opaco (intercambiable con el SDK de TypeScript), el `PageWalk` que recorre
las páginas de `list-microvms` y el `OrderedWalk` de `order=`.

`list-microvms` no ordena ni filtra por estado ni por fecha
(`AWS_API_NOTES.md` §6): el estado, `started_after` y `order` se aplican en
el cliente. La E/S (páginas, sondas de metadatos) la hacen los llamadores
síncrono y asíncrono.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

from rayito._aws import ControlPlane
from rayito._limits import LIST_MAX_RESULTS, TERMINAL_STATES
from rayito._metrics_base import unix_ms_or_zero
from rayito._models import MicrovmListPage, SandboxListItem
from rayito._payload import validated_metadata
from rayito._sandbox_base import METADATA_LIST_STATES, list_states_for_metadata
from rayito.exceptions import InvalidArgumentException

ListOrder = Literal["asc", "desc"]

TOKEN_VERSION: Final = 1
TOKEN_MAX_CHARS: Final = 8192
FINGERPRINT_HEX_CHARS: Final = 16
DIGEST_HEX_CHARS: Final = 12
INVALID_TOKEN_MESSAGE: Final = "next_token inválido"
FOREIGN_TOKEN_MESSAGE: Final = "next_token no corresponde a estos filtros"
EXHAUSTED_MESSAGE: Final = "no quedan páginas: has_next es False"
PAGE_TOKEN_KEYS: Final = frozenset({"v", "f", "a", "s"})
KEY_TOKEN_KEYS: Final = frozenset({"v", "f", "k"})
BASE64URL_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]+")
FINGERPRINT_PATTERN: Final = re.compile(r"[0-9a-f]{16}")
DIGEST_PATTERN: Final = re.compile(r"[0-9a-f]{12}")


def canonical_json(value: object) -> bytes:
    """Claves ordenadas, sin espacios y UTF-8 sin escapes: los mismos bytes
    que `canonicalJson` del SDK de TypeScript."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def item_digest(sandbox_id: str) -> str:
    return hashlib.sha256(sandbox_id.encode()).hexdigest()[:DIGEST_HEX_CHARS]


def started_at_ms(item: SandboxListItem) -> int:
    """La misma conversión que `unix_ms_or_zero` aplica a `started_after`, así
    un `started_after` igual al `started_at` de un item lo incluye."""
    return round(item.started_at.timestamp() * 1000)


def validate_order(order: str | None) -> ListOrder | None:
    if order is None:
        return None
    if order == "asc":
        return "asc"
    if order == "desc":
        return "desc"
    raise InvalidArgumentException(f"order inválido {order!r}: se esperaba 'asc' o 'desc'")


def validate_limit(limit: int | None) -> int | None:
    if limit is None:
        return None
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise InvalidArgumentException("limit debe ser un entero >= 1")
    return limit


@dataclass(frozen=True)
class ListFilters:
    """Los filtros de un listado, canónicos (tuplas ordenadas) para que la
    huella no dependa del orden en que el caller los pasó. `states` `None`
    es el defecto: sin `metadata` omite `TERMINATING|TERMINATED`, con
    `metadata` sólo `RUNNING` (los metadatos viven en el agente)."""

    image_arn: str | None
    image_version: str | None
    states: tuple[str, ...] | None
    started_after_ms: int | None
    metadata: tuple[tuple[str, str], ...] | None
    order: ListOrder | None

    def fingerprint(self) -> str:
        """El token la lleva para rechazar su uso con otros filtros; es un
        hash, así que nunca expone los valores de `metadata`."""
        document = {
            "image": self.image_arn,
            "version": self.image_version,
            "states": None if self.states is None else list(self.states),
            "started_after_ms": self.started_after_ms,
            "metadata": None if self.metadata is None else dict(self.metadata),
            "order": self.order,
        }
        return hashlib.sha256(canonical_json(document)).hexdigest()[:FINGERPRINT_HEX_CHARS]

    def accepts(self, item: SandboxListItem) -> bool:
        """Estado y `started_after` ("en o después", como E2B). Los metadatos
        se comprueban aparte porque exigen una sonda al agente."""
        return self._state_wanted(item.state) and (
            self.started_after_ms is None or started_at_ms(item) >= self.started_after_ms
        )

    def _state_wanted(self, state: str) -> bool:
        if self.states is not None:
            return state in self.states
        if self.metadata is not None:
            return state in METADATA_LIST_STATES
        return state not in TERMINAL_STATES


@dataclass(frozen=True)
class PageCursor:
    """Dónde reanudar un recorrido sin `order`: el `nextToken` de AWS que
    trae la página actual (`None` = la primera) y el digest de los items
    crudos ya consumidos de esa página."""

    aws_token: str | None
    consumed: frozenset[str]


@dataclass(frozen=True)
class KeyCursor:
    """Dónde reanudar un recorrido con `order`: el último item servido."""

    started_at_ms: int
    sandbox_id: str


Cursor = PageCursor | KeyCursor

FIRST_PAGE: Final = PageCursor(aws_token=None, consumed=frozenset())


@dataclass(frozen=True)
class DecodedToken:
    fingerprint: str
    cursor: Cursor


@dataclass(frozen=True)
class PageRequest:
    aws_token: str | None


def encode_next_token(fingerprint: str, cursor: Cursor) -> str:
    document: dict[str, object] = {"v": TOKEN_VERSION, "f": fingerprint}
    if isinstance(cursor, PageCursor):
        document["a"] = cursor.aws_token
        document["s"] = sorted(cursor.consumed)
    else:
        document["k"] = [cursor.started_at_ms, cursor.sandbox_id]
    return base64.urlsafe_b64encode(canonical_json(document)).rstrip(b"=").decode("ascii")


def decode_next_token(token: str) -> DecodedToken:
    """Nunca repite el token en el error: lleva el ARN de la imagen y la
    huella de los filtros."""
    document = _token_document(token)
    if document is None:
        raise InvalidArgumentException(INVALID_TOKEN_MESSAGE) from None
    decoded = _decoded_from_document(document)
    if decoded is None:
        raise InvalidArgumentException(INVALID_TOKEN_MESSAGE) from None
    return decoded


def next_token_for(fingerprint: str, cursor: Cursor | None) -> str | None:
    return None if cursor is None else encode_next_token(fingerprint, cursor)


def _token_document(token: object) -> dict[str, object] | None:
    if not isinstance(token, str) or len(token) > TOKEN_MAX_CHARS:
        return None
    if not BASE64URL_PATTERN.fullmatch(token):
        return None
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        document = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def _decoded_from_document(document: dict[str, object]) -> DecodedToken | None:
    version = document.get("v")
    fingerprint = document.get("f")
    if type(version) is not int or version != TOKEN_VERSION:
        return None
    if not isinstance(fingerprint, str) or not FINGERPRINT_PATTERN.fullmatch(fingerprint):
        return None
    keys = frozenset(document)
    cursor: Cursor | None = None
    if keys == PAGE_TOKEN_KEYS:
        cursor = _page_cursor(document["a"], document["s"])
    elif keys == KEY_TOKEN_KEYS:
        cursor = _key_cursor(document["k"])
    return None if cursor is None else DecodedToken(fingerprint, cursor)


def _page_cursor(aws_token: object, digests: object) -> PageCursor | None:
    if aws_token is not None and not isinstance(aws_token, str):
        return None
    if not isinstance(digests, list) or len(digests) > LIST_MAX_RESULTS:
        return None
    if not all(isinstance(digest, str) and DIGEST_PATTERN.fullmatch(digest) for digest in digests):
        return None
    return PageCursor(aws_token=aws_token, consumed=frozenset(digests))


def _key_cursor(key: object) -> KeyCursor | None:
    if not isinstance(key, list) or len(key) != 2:
        return None
    millis, sandbox_id = key
    if type(millis) is not int or millis < 0:
        return None
    if not isinstance(sandbox_id, str) or not sandbox_id:
        return None
    return KeyCursor(started_at_ms=millis, sandbox_id=sandbox_id)


@dataclass(frozen=True)
class ListingRequest:
    """Los argumentos de un listado ya validados, antes de resolver el
    template (lo único que puede necesitar una llamada a AWS)."""

    template: str | None
    template_version: str | None
    states: tuple[str, ...] | None
    metadata: tuple[tuple[str, str], ...] | None
    started_after_ms: int | None
    order: ListOrder | None
    limit: int | None
    next_token: str | None
    decoded: DecodedToken | None

    def filters(self, image_arn: str | None) -> ListFilters:
        return ListFilters(
            image_arn=image_arn,
            image_version=self.template_version,
            states=self.states,
            started_after_ms=self.started_after_ms,
            metadata=self.metadata,
            order=self.order,
        )


def listing_request(
    *,
    template: str | None,
    template_version: str | None,
    states: Iterable[str] | None,
    metadata: Mapping[str, str] | None,
    started_after: datetime | None,
    order: str | None,
    limit: int | None,
    next_token: str | None,
) -> ListingRequest:
    """Todo lo que se puede validar sin AWS: `limit`, `order`, `metadata`
    (sólo con `RUNNING`), `started_after` y la forma del token, que tiene que
    corresponder a `order` (cursor de clave con `order`, de página sin él)."""
    validated_limit = validate_limit(limit)
    validated_order = validate_order(order)
    requested_states = None if states is None else tuple(sorted(states))
    canonical_metadata = None
    if metadata is not None:
        canonical_metadata = tuple(sorted(validated_metadata(metadata).items()))
        list_states_for_metadata(requested_states)
    started_after_ms = (
        None if started_after is None else unix_ms_or_zero(started_after, field="started_after")
    )
    decoded = None if next_token is None else decode_next_token(next_token)
    if decoded is not None and isinstance(decoded.cursor, KeyCursor) != (
        validated_order is not None
    ):
        raise InvalidArgumentException(INVALID_TOKEN_MESSAGE)
    return ListingRequest(
        template=template,
        template_version=template_version,
        states=requested_states,
        metadata=canonical_metadata,
        started_after_ms=started_after_ms,
        order=validated_order,
        limit=validated_limit,
        next_token=next_token,
        decoded=decoded,
    )


def resume_cursors(
    decoded: DecodedToken | None, filters: ListFilters
) -> tuple[PageCursor, KeyCursor | None]:
    """El `PageCursor` desde el que recorrer las páginas y, con `order`, la
    clave tras la que servir. Un token de otros filtros se rechaza antes de
    pedir ninguna página."""
    if decoded is None:
        return FIRST_PAGE, None
    if decoded.fingerprint != filters.fingerprint():
        raise InvalidArgumentException(FOREIGN_TOKEN_MESSAGE)
    if isinstance(decoded.cursor, KeyCursor):
        return FIRST_PAGE, decoded.cursor
    return decoded.cursor, None


class PageWalk:
    """Recorre las páginas de `list-microvms` desde un `PageCursor`.

    Al reanudar vuelve a pedir la página a la que apunta el cursor y salta
    por identidad (digest del id) los items ya consumidos de ella: un
    sandbox creado o desaparecido entre medias dentro de esa página ni se
    duplica ni se salta. El movimiento entre páginas entre dos llamadas no
    está protegido, como con cualquier cursor de AWS."""

    def __init__(self, cursor: PageCursor) -> None:
        self._aws_token = cursor.aws_token
        self._consumed = set(cursor.consumed)
        self._buffer: deque[SandboxListItem] = deque()
        self._fetched = False
        self._next_token: str | None = None

    def page_to_fetch(self) -> PageRequest | None:
        if self._buffer:
            return None
        if not self._fetched:
            return PageRequest(self._aws_token)
        if self._next_token is None:
            return None
        self._aws_token = self._next_token
        self._consumed = set()
        self._fetched = False
        return PageRequest(self._aws_token)

    def accept_page(self, page: MicrovmListPage) -> None:
        self._buffer.extend(
            item for item in page.items if item_digest(item.sandbox_id) not in self._consumed
        )
        self._next_token = page.next_token
        self._fetched = True

    def next_raw(self) -> SandboxListItem | None:
        if not self._buffer:
            return None
        item = self._buffer.popleft()
        self._consumed.add(item_digest(item.sandbox_id))
        return item

    def cursor(self) -> PageCursor:
        return PageCursor(aws_token=self._aws_token, consumed=frozenset(self._consumed))

    @property
    def has_more(self) -> bool:
        return bool(self._buffer) or not self._fetched or self._next_token is not None


def fetch_page(plane: ControlPlane, filters: ListFilters, request: PageRequest) -> MicrovmListPage:
    """Siempre `maxResults` 50: los límites de página son los mismos en un
    recorrido nuevo y en uno reanudado."""
    return plane.list_microvms_page(
        image_arn=filters.image_arn,
        image_version=filters.image_version,
        max_results=LIST_MAX_RESULTS,
        next_token=request.aws_token,
    )


def ordering_key(item: SandboxListItem) -> tuple[int, str]:
    return started_at_ms(item), item.sandbox_id


class OrderedWalk:
    """Sirve por `startedAt` (desempate por id) los items ya recogidos de
    todas las páginas. La reanudación es por clave (keyset): tolera
    sandboxes creados entre dos llamadas."""

    def __init__(
        self, items: Iterable[SandboxListItem], order: ListOrder, after: KeyCursor | None
    ) -> None:
        descending = order == "desc"
        ordered = sorted(items, key=ordering_key, reverse=descending)
        if after is not None:
            boundary = (after.started_at_ms, after.sandbox_id)
            ordered = [
                item
                for item in ordered
                if (ordering_key(item) < boundary if descending else ordering_key(item) > boundary)
            ]
        self._remaining: deque[SandboxListItem] = deque(ordered)
        self._last: KeyCursor | None = after

    def take(self, limit: int | None) -> list[SandboxListItem]:
        count = len(self._remaining) if limit is None else min(limit, len(self._remaining))
        served = [self._remaining.popleft() for _ in range(count)]
        if served:
            millis, sandbox_id = ordering_key(served[-1])
            self._last = KeyCursor(started_at_ms=millis, sandbox_id=sandbox_id)
        return served

    def cursor(self) -> KeyCursor | None:
        return self._last

    @property
    def has_more(self) -> bool:
        return bool(self._remaining)


class ListingSession:
    """El estado de un listado tras resolver el template: filtros, huella,
    el `PageWalk` y, con `order`, el `OrderedWalk` que se llena en la
    primera llamada. Los paginadores síncrono y asíncrono sólo añaden la
    E/S (páginas y sondas de metadatos)."""

    def __init__(self, request: ListingRequest, image_arn: str | None) -> None:
        self.request = request
        self.filters = request.filters(image_arn)
        self.fingerprint = self.filters.fingerprint()
        page_cursor, self.after = resume_cursors(request.decoded, self.filters)
        self.walk = PageWalk(page_cursor)
        self.ordered: OrderedWalk | None = None

    @property
    def wanted_metadata(self) -> dict[str, str] | None:
        metadata = self.filters.metadata
        return None if metadata is None else dict(metadata)

    def start_ordered(self, order: ListOrder, matches: Iterable[SandboxListItem]) -> OrderedWalk:
        """Con `order` la primera llamada recorre todas las páginas: AWS no
        ordena, así que el coste es O(páginas) (más la sonda O(n) con
        `metadata`)."""
        self.ordered = OrderedWalk(matches, order, self.after)
        return self.ordered

    def ordered_take(self, limit: int | None) -> list[SandboxListItem]:
        return [] if self.ordered is None else self.ordered.take(limit)

    @property
    def has_more(self) -> bool:
        if self.ordered is not None:
            return self.ordered.has_more
        return self.walk.has_more

    def next_token(self) -> str | None:
        if not self.has_more:
            return None
        cursor: Cursor | None = (
            self.ordered.cursor() if self.ordered is not None else self.walk.cursor()
        )
        return next_token_for(self.fingerprint, cursor)
