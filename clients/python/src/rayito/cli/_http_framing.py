"""Delimitación de mensajes HTTP/1.1 para `rayito sandbox proxy` (dominio
puro, sin E/S): dónde acaba el cuerpo de una petición o de una respuesta
(RFC 9112 §6.3), para que el proxy reenvíe exactamente un mensaje y nada de
lo que venga detrás.

- Petición: `Transfer-Encoding` cuya última codificación es `chunked` →
  troceado; `Content-Length` válido → esa longitud; ninguno → sin cuerpo.
  Ambigüedades que un intermediario no debe resolver por su cuenta
  (`Transfer-Encoding` junto a `Content-Length`, un `Transfer-Encoding` que
  no acaba en `chunked`, varios `Content-Length` distintos o no numéricos)
  son `MalformedFramingError`: el proxy responde `400` sin reenviar nada.
- Respuesta: `1xx`, `204`, `304` o la respuesta a un `HEAD` no llevan
  cuerpo; si no, `chunked`, `Content-Length` o, sin ninguno de los dos,
  hasta que el upstream cierre.
"""

from __future__ import annotations

import enum
import re
import string
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

TRANSFER_ENCODING_HEADER_NAME: Final = "Transfer-Encoding"
CONTENT_LENGTH_HEADER_NAME: Final = "Content-Length"
CHUNKED_CODING: Final = "chunked"
HEAD_METHOD: Final = "HEAD"
LIST_SEPARATOR: Final = ","
#: RFC 9110 §15.2: `1xx` nunca lleva cuerpo.
INFORMATIONAL_MIN: Final = 100
INFORMATIONAL_MAX: Final = 199
SWITCHING_PROTOCOLS: Final = 101
NO_CONTENT: Final = 204
NOT_MODIFIED: Final = 304
_STATUS_LINE = re.compile(r"^HTTP/1\.[01] (\d{3})(?: .*)?$")
_DIGITS = re.compile(r"^[0-9]+$")


class MalformedFramingError(ValueError):
    """La delimitación del cuerpo es ambigua o inválida. Nunca lleva el
    contenido de la petición en el mensaje."""


class BodyKind(enum.Enum):
    NONE = "none"
    LENGTH = "length"
    CHUNKED = "chunked"
    UNTIL_CLOSE = "until-close"


@dataclass(frozen=True)
class BodyFraming:
    kind: BodyKind
    length: int = 0


NO_BODY: Final = BodyFraming(BodyKind.NONE)


def _list_values(values: Iterable[str]) -> list[str]:
    return [item.strip().lower() for value in values for item in value.split(LIST_SEPARATOR)]


def _content_length(values: list[str]) -> int | None:
    """La longitud común de todos los `Content-Length` (RFC 9110 §8.6
    admite repeticiones idénticas), `None` sin ninguno."""
    if not values:
        return None
    items = set(_list_values(values))
    if len(items) != 1:
        raise MalformedFramingError("varios Content-Length distintos")
    (item,) = items
    if not _DIGITS.match(item):
        raise MalformedFramingError("Content-Length no numérico")
    return int(item)


def request_body_framing(transfer_encodings: list[str], content_lengths: list[str]) -> BodyFraming:
    """El cuerpo de una petición a partir de todos sus `Transfer-Encoding` y
    `Content-Length`."""
    if transfer_encodings:
        if content_lengths:
            raise MalformedFramingError("Transfer-Encoding junto a Content-Length")
        codings = [coding for coding in _list_values(transfer_encodings) if coding]
        if not codings or codings[-1] != CHUNKED_CODING:
            raise MalformedFramingError("Transfer-Encoding que no acaba en chunked")
        return BodyFraming(BodyKind.CHUNKED)
    length = _content_length(content_lengths)
    if not length:
        return NO_BODY
    return BodyFraming(BodyKind.LENGTH, length)


def response_status(status_line: str) -> int:
    """El código de una línea de estado HTTP/1.x; `MalformedFramingError`
    si no lo es."""
    match = _STATUS_LINE.match(status_line)
    if match is None:
        raise MalformedFramingError("línea de estado inválida")
    return int(match.group(1))


def is_informational(status: int) -> bool:
    return INFORMATIONAL_MIN <= status <= INFORMATIONAL_MAX


def response_body_framing(
    status: int,
    *,
    request_method: str,
    transfer_encodings: list[str],
    content_lengths: list[str],
) -> BodyFraming:
    """El cuerpo de una respuesta (RFC 9112 §6.3). Un `Content-Length`
    inválido del upstream se trata como "hasta que cierre", que nunca lee
    de más."""
    if is_informational(status) or status in (NO_CONTENT, NOT_MODIFIED):
        return NO_BODY
    if request_method.upper() == HEAD_METHOD:
        return NO_BODY
    if transfer_encodings:
        codings = _list_values(transfer_encodings)
        if codings and codings[-1] == CHUNKED_CODING:
            return BodyFraming(BodyKind.CHUNKED)
        return BodyFraming(BodyKind.UNTIL_CLOSE)
    try:
        length = _content_length(content_lengths)
    except MalformedFramingError:
        return BodyFraming(BodyKind.UNTIL_CLOSE)
    if length is None:
        return BodyFraming(BodyKind.UNTIL_CLOSE)
    return BodyFraming(BodyKind.LENGTH, length) if length else NO_BODY


def chunk_size(line: bytes) -> int:
    """El tamaño de una línea `chunk-size [; ext] CRLF` (RFC 9112 §7.1)."""
    size, _, _ = line.rstrip(b"\r\n").partition(b";")
    text = size.strip().decode("latin-1")
    if not text or any(char not in string.hexdigits for char in text):
        raise MalformedFramingError("chunk-size inválido")
    return int(text, 16)
