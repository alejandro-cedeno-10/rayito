"""Dominio puro de `m15-events-webhooks`: el modelo de evento tal y como lo
lee el SDK (espejo de `rayd_core::lifecycle_events::event` y de
`infra/lambdas/events_webhooks/domain/event.py`) y `WebhookInfo`, lo que
`list_webhooks()` devuelve. Nada aquí importa `boto3`.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

#: Prefijo del `type` compatible con E2B que `register_webhook(types=...)` y
#: `get_events()` usan (`sandbox.lifecycle.created|paused|resumed|killed`).
EVENT_TYPE_PREFIX: Final = "sandbox.lifecycle."

EVENT_KINDS: Final = ("created", "paused", "resumed", "killed")
KILL_REASONS: Final = ("request", "timeout", "unknown")

DEFAULT_STACK_NAME: Final = "rayito-events-webhooks"

#: `deploy(reconciler_interval_minutes=...)`'s default, mirrored by
#: `infra/events-webhooks.yaml`'s own `ReconcilerIntervalMinutes` parameter
#: default — kept as one constant so the sync API, the async API, the CLI
#: and the TypeScript mirror (`DEFAULT_RECONCILER_INTERVAL_MINUTES` in
#: `domain.ts`) can never drift from each other.
DEFAULT_RECONCILER_INTERVAL_MINUTES: Final = 5

#: `infra/events-webhooks.yaml`'s `ReconcilerIntervalMinutes` `MinValue`:
#: EventBridge Scheduler only accepts `rate(1 minute)` (singular) for 1, and
#: the template always renders `rate(N minutes)`, so 1 is excluded rather
#: than special-cased.
MIN_RECONCILER_INTERVAL_MINUTES: Final = 2

#: `get_events(limit=...)`'s default, also `_MAX_GET_EVENTS_LIMIT` in
#: `_service.py`/`_service_async.py` (a `DynamoDB` `Query`'s own practical
#: page size for this table, AWS_API_NOTES.md §25).
DEFAULT_GET_EVENTS_LIMIT: Final = 100

#: El único esquema que el deliverer acepta (`adapters/http_client.py`).
WEBHOOK_URL_SCHEME: Final = "https"
#: RFC 1035 §2.3.4: 63 octetos por etiqueta y 253 caracteres por nombre (la
#: forma ASCII, ya en punycode). Lo mismo que el codec `idna` del deliverer
#: rechaza al resolver: validarlo aquí evita registrar un webhook que nunca
#: podría entregarse.
MAX_HOSTNAME_LABEL_CHARS: Final = 63
MAX_HOSTNAME_CHARS: Final = 253
_DNS_LABEL_PATTERN: Final = re.compile(rf"[A-Za-z0-9_-]{{1,{MAX_HOSTNAME_LABEL_CHARS}}}")
#: `esquema://` seguido de una autoridad no vacía, sin espacios, controles ni
#: barras invertidas en toda la URL: los dos parsers (`urlsplit` aquí, el
#: WHATWG `URL` en TypeScript) discrepan en esos casos, así que ninguno los
#: acepta.
_URL_SHAPE_PATTERN: Final = re.compile(
    r"[A-Za-z][A-Za-z0-9+.-]*://[^/?#\\\x00-\x20\x7f][^\\\x00-\x20\x7f]*"
)
MIN_PORT: Final = 1
MAX_PORT: Final = 65_535
#: Mismo texto en `domain.ts` (`INVALID_WEBHOOK_URL`); nunca repite la URL
#: (puede llevar una credencial en la ruta).
INVALID_WEBHOOK_URL: Final = (
    "url debe ser una URL https:// válida hacia una dirección pública: host DNS "
    "(no localhost) o IP pública (no de loopback, privada, link-local ni de metadatos), "
    "puerto entre 1 y 65535"
)
#: RFC 6761 §6.3: `localhost` y todo nombre bajo `.localhost` resuelven a
#: loopback; el deliverer los rechazaría tras resolverlos.
LOCALHOST_NAME: Final = "localhost"
#: 100.64.0.0/10 (RFC 6598), el `CGNAT_BLOCK` del deliverer
#: (`infra/lambdas/events_webhooks/domain/ssrf.py`): `ipaddress` no tiene
#: predicado para este bloque.
CGNAT_NETWORK: Final = ipaddress.ip_network("100.64.0.0/10")
_DECIMAL_PATTERN: Final = re.compile(r"[0-9]*")
_OCTAL_PATTERN: Final = re.compile(r"[0-7]*")
_HEX_PATTERN: Final = re.compile(r"[0-9A-Fa-f]*")
_IPV4_PARTS: Final = 4
_IPV4_OCTET_LIMIT: Final = 256


def event_type(kind: str) -> str:
    return f"{EVENT_TYPE_PREFIX}{kind}"


@dataclass(frozen=True)
class EventRecord:
    """Una fila de `get_events()`. `sandbox_execution_id` es
    ``f"{sandbox_id}#{generation}"`` (documentado para el payload del
    webhook); `sandbox_template_id` es el ARN de la imagen."""

    event_id: str
    sandbox_id: str
    kind: str
    kill_reason: str | None
    generation: int
    occurred_at_ms: int
    image_arn: str
    image_version: str

    @property
    def type(self) -> str:
        return event_type(self.kind)

    @property
    def sandbox_execution_id(self) -> str:
        return f"{self.sandbox_id}#{self.generation}"


@dataclass(frozen=True)
class WebhookInfo:
    """Lo que `register_webhook`/`list_webhooks` exponen; nunca el secreto
    (ni su nombre en Secrets Manager, ni mucho menos su valor)."""

    webhook_id: str
    url: str
    types: tuple[str, ...]


def is_deliverable_webhook_url(url: str) -> bool:
    """`True` si el deliverer puede entregar a `url`: esquema `https`, host
    no vacío (un nombre DNS codificable en IDNA, con etiquetas de 1-63
    caracteres `[A-Za-z0-9_-]` y 253 en total, que no sea `localhost` ni
    acabe en `.localhost`, o una IP que `is_blocked_webhook_address` no
    bloquee) y, si lo lleva, puerto entre `MIN_PORT` y `MAX_PORT`. El mismo
    criterio que `isDeliverableWebhookUrl` (`domain.ts`)."""
    if not _URL_SHAPE_PATTERN.fullmatch(url):
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
        hostname = parts.hostname or ""
        ascii_host = hostname.encode("idna").decode("ascii")
    except (ValueError, UnicodeError):
        return False
    if parts.scheme != WEBHOOK_URL_SCHEME or not hostname:
        return False
    if port is not None and not MIN_PORT <= port <= MAX_PORT:
        return False
    try:
        address = _literal_address(hostname)
    except ValueError:
        return False
    if address is not None:
        return not is_blocked_webhook_address(address)
    return _is_valid_dns_name(ascii_host) and not _is_localhost(ascii_host)


def is_blocked_webhook_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """`True` para una dirección a la que el deliverer nunca entrega: la
    misma regla que `is_blocked` del guard SSRF del deliverer
    (`infra/lambdas/events_webhooks/domain/ssrf.py`) —loopback, privada,
    link-local (IMDS incluida), multicast, reservada, sin especificar o
    CGNAT—, y una IPv6 con una IPv4 dentro (`::ffff:a.b.c.d`) se juzga como
    esa IPv4. Ambos lados pasan `testdata/lifecycle-events/
    ssrf-address-vectors.json`. Aquí sólo se miran IPs literales: un nombre
    DNS que resuelve a una de ellas (o que cambia de respuesta, DNS
    rebinding) lo sigue parando el deliverer al resolver."""
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if address.is_loopback or address.is_private or address.is_link_local:
        return True
    if address.is_multicast or address.is_reserved or address.is_unspecified:
        return True
    return isinstance(address, ipaddress.IPv4Address) and address in CGNAT_NETWORK


def _literal_address(
    hostname: str,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """La IP que `hostname` escribe literalmente, o `None` si es un nombre.
    Una IPv6 va entre corchetes (`urlsplit` ya los quita). Un host que acaba
    en número es una IPv4 según el parser WHATWG (`0x7f.1`, `2130706433`),
    que es el que usa el SDK de TypeScript y lo que `getaddrinfo` acepta en
    el deliverer; si no es una IPv4 válida, `ValueError`."""
    if ":" in hostname:
        return ipaddress.ip_address(hostname)
    parts = hostname.split(".")
    if len(parts) > 1 and parts[-1] == "":
        parts.pop()
    last = parts[-1]
    if not (last.isascii() and last.isdigit()) and not _is_hex_number(last):
        return None
    return _whatwg_ipv4(parts)


def _is_hex_number(part: str) -> bool:
    return part[:2].lower() == "0x" and _HEX_PATTERN.fullmatch(part[2:]) is not None


def _whatwg_ipv4(parts: list[str]) -> ipaddress.IPv4Address:
    """El IPv4 parser de WHATWG URL sobre las partes ya separadas por `.`:
    de 1 a 4 números decimales, octales (`0…`) o hexadecimales (`0x…`);
    todos menos el último caben en un octeto y el último llena el resto."""
    if len(parts) > _IPV4_PARTS or "" in parts:
        raise ValueError("IPv4 con partes vacías o de más")
    numbers = [_ipv4_number(part) for part in parts]
    *leading, last = numbers
    if any(number >= _IPV4_OCTET_LIMIT for number in leading):
        raise ValueError("IPv4 con un octeto de más de 255")
    if last >= _IPV4_OCTET_LIMIT ** (_IPV4_PARTS + 1 - len(numbers)):
        raise ValueError("IPv4 fuera de rango")
    value = last
    for index, number in enumerate(leading):
        value += number * _IPV4_OCTET_LIMIT ** (_IPV4_PARTS - 1 - index)
    return ipaddress.IPv4Address(value)


def _ipv4_number(part: str) -> int:
    if part[:2].lower() == "0x":
        digits, base, pattern = part[2:], 16, _HEX_PATTERN
    elif len(part) > 1 and part.startswith("0"):
        digits, base, pattern = part[1:], 8, _OCTAL_PATTERN
    else:
        digits, base, pattern = part, 10, _DECIMAL_PATTERN
    if pattern.fullmatch(digits) is None:
        raise ValueError("parte de IPv4 no numérica")
    return int(digits, base) if digits else 0


def _is_localhost(ascii_host: str) -> bool:
    name = ascii_host.removesuffix(".").lower()
    return name == LOCALHOST_NAME or name.endswith(f".{LOCALHOST_NAME}")


def _is_valid_dns_name(ascii_host: str) -> bool:
    """Un nombre DNS en forma ASCII (un punto final opcional)."""
    name = ascii_host.removesuffix(".")
    if not name or len(name) > MAX_HOSTNAME_CHARS:
        return False
    return all(_DNS_LABEL_PATTERN.fullmatch(label) for label in name.split("."))
