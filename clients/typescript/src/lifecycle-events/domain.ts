/**
 * Dominio puro de `m15-events-webhooks`: espejo de
 * `rayito._lifecycle_events._domain` (Python) y de
 * `infra/lambdas/events_webhooks/domain/event.py`.
 */

export const EVENT_TYPE_PREFIX = "sandbox.lifecycle.";

export const EVENT_KINDS = ["created", "paused", "resumed", "killed"] as const;
export type EventKind = (typeof EVENT_KINDS)[number];

export const KILL_REASONS = ["request", "timeout", "unknown"] as const;
export type KillReason = (typeof KILL_REASONS)[number];

export const DEFAULT_STACK_NAME = "rayito-events-webhooks";

/**
 * `deploy({ reconcilerIntervalMinutes })`'s default, mirrored by
 * `infra/events-webhooks.yaml`'s own `ReconcilerIntervalMinutes` parameter
 * default — kept as one constant so the Python sync API, the async API,
 * the CLI and this TypeScript mirror can never drift from each other.
 */
export const DEFAULT_RECONCILER_INTERVAL_MINUTES = 5;

/**
 * `infra/events-webhooks.yaml`'s `ReconcilerIntervalMinutes` `MinValue`:
 * EventBridge Scheduler only accepts `rate(1 minute)` (singular) for 1, and
 * the template always renders `rate(N minutes)`, so 1 is excluded rather
 * than special-cased.
 */
export const MIN_RECONCILER_INTERVAL_MINUTES = 2;

/**
 * `getEvents({ limit })`'s default, also `MAX_GET_EVENTS_LIMIT` in
 * `service.ts` (a DynamoDB `Query`'s own practical page size for this
 * table, AWS_API_NOTES.md §25).
 */
export const DEFAULT_GET_EVENTS_LIMIT = 100;

/** El único esquema que el deliverer acepta (`adapters/http_client.py`). */
export const WEBHOOK_URL_SCHEME = "https";
/**
 * RFC 1035 §2.3.4: 63 octetos por etiqueta y 253 caracteres por nombre (la
 * forma ASCII, ya en punycode). Lo mismo que el codec `idna` del deliverer
 * rechaza al resolver: validarlo aquí evita registrar un webhook que nunca
 * podría entregarse. Espejo de `_domain.py`.
 */
export const MAX_HOSTNAME_LABEL_CHARS = 63;
export const MAX_HOSTNAME_CHARS = 253;
export const MIN_PORT = 1;
export const MAX_PORT = 65_535;
/** Mismo texto que `INVALID_WEBHOOK_URL` de `_domain.py`; nunca repite la URL. */
export const INVALID_WEBHOOK_URL =
  "url debe ser una URL https:// válida: host DNS o IP, puerto entre 1 y 65535";
const DNS_LABEL_PATTERN = new RegExp(`^[A-Za-z0-9_-]{1,${MAX_HOSTNAME_LABEL_CHARS}}$`);
/**
 * `esquema://` seguido de una autoridad no vacía, sin espacios, controles ni
 * barras invertidas en toda la URL: el WHATWG `URL` y el `urlsplit` de Python
 * discrepan en esos casos, así que ninguno de los dos SDKs los acepta.
 */
// biome-ignore lint/suspicious/noControlCharactersInRegex: rechazarlos es justo el objetivo
const URL_SHAPE_PATTERN = /^[A-Za-z][A-Za-z0-9+.-]*:\/\/[^/?#\\\x00-\x20\x7f][^\\\x00-\x20\x7f]*$/;

/**
 * `true` si el deliverer puede entregar a `url`: esquema `https`, host no
 * vacío (un nombre DNS con etiquetas de 1-63 caracteres `[A-Za-z0-9_-]` y
 * 253 en total en su forma punycode, o una IP) y, si lo lleva, puerto entre
 * `MIN_PORT` y `MAX_PORT`. El mismo criterio que
 * `is_deliverable_webhook_url` (`_domain.py`).
 */
export function isDeliverableWebhookUrl(url: string): boolean {
  if (!URL_SHAPE_PATTERN.test(url)) {
    return false;
  }
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return false;
  }
  if (parsed.protocol !== `${WEBHOOK_URL_SCHEME}:` || parsed.hostname === "") {
    return false;
  }
  if (parsed.port !== "") {
    const port = Number(parsed.port);
    if (!(port >= MIN_PORT && port <= MAX_PORT)) {
      return false;
    }
  }
  return parsed.hostname.startsWith("[") || isValidDnsName(parsed.hostname);
}

function isValidDnsName(asciiHost: string): boolean {
  const name = asciiHost.endsWith(".") ? asciiHost.slice(0, -1) : asciiHost;
  if (name.length === 0 || name.length > MAX_HOSTNAME_CHARS) {
    return false;
  }
  return name.split(".").every((label) => DNS_LABEL_PATTERN.test(label));
}

export function eventType(kind: EventKind): string {
  return `${EVENT_TYPE_PREFIX}${kind}`;
}

/** Una fila de `getEvents()`. */
export interface EventRecord {
  readonly eventId: string;
  readonly sandboxId: string;
  readonly kind: EventKind;
  readonly killReason: KillReason | undefined;
  readonly generation: number;
  readonly occurredAtMs: number;
  readonly imageArn: string;
  readonly imageVersion: string;
}

export function eventRecordType(record: EventRecord): string {
  return eventType(record.kind);
}

export function sandboxExecutionId(record: EventRecord): string {
  return `${record.sandboxId}#${record.generation}`;
}

/** Lo que `registerWebhook`/`listWebhooks` exponen; nunca el secreto. */
export interface WebhookInfo {
  readonly webhookId: string;
  readonly url: string;
  readonly types: readonly string[];
}
