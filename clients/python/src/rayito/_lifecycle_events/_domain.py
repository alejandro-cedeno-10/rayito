"""Dominio puro de `m15-events-webhooks`: el modelo de evento tal y como lo
lee el SDK (espejo de `rayd_core::lifecycle_events::event` y de
`infra/lambdas/events_webhooks/domain/event.py`) y `WebhookInfo`, lo que
`list_webhooks()` devuelve. Nada aquí importa `boto3`.
"""

from __future__ import annotations

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
    "url debe ser una URL https:// válida: host DNS o IP, puerto entre 1 y 65535"
)


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
    caracteres `[A-Za-z0-9_-]` y 253 en total, o una IP) y, si lo lleva,
    puerto entre `MIN_PORT` y `MAX_PORT`. El mismo criterio que
    `isDeliverableWebhookUrl` (`domain.ts`)."""
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
    return ":" in hostname or _is_valid_dns_name(ascii_host)


def _is_valid_dns_name(ascii_host: str) -> bool:
    """Un nombre DNS en forma ASCII (un punto final opcional)."""
    name = ascii_host.removesuffix(".")
    if not name or len(name) > MAX_HOSTNAME_CHARS:
        return False
    return all(_DNS_LABEL_PATTERN.fullmatch(label) for label in name.split("."))
