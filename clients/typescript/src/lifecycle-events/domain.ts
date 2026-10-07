/**
 * Dominio puro de `m15-events-webhooks`: espejo de
 * `rayito._lifecycle_events._domain` (Python) y de
 * `infra/lambdas/events_webhooks/domain/event.py`.
 */

import { isIPv4, isIPv6 } from "node:net";

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
  "url debe ser una URL https:// válida hacia una dirección pública: host DNS " +
  "(no localhost) o IP pública (no de loopback, privada, link-local ni de metadatos), " +
  "puerto entre 1 y 65535";
/**
 * RFC 6761 §6.3: `localhost` y todo nombre bajo `.localhost` resuelven a
 * loopback; el deliverer los rechazaría tras resolverlos.
 */
export const LOCALHOST_NAME = "localhost";

/**
 * Las redes que bloquea `is_blocked` del guard SSRF del deliverer
 * (`infra/lambdas/events_webhooks/domain/ssrf.py`, Python 3.12 como su
 * runtime): `is_private` (`IPV4_PRIVATE_NETWORKS` salvo
 * `IPV4_PRIVATE_EXCEPTIONS`), y loopback, link-local, multicast, reservada,
 * sin especificar y CGNAT (100.64.0.0/10, RFC 6598). Son las tablas de
 * `ipaddress._IPv4Constants`/`_IPv6Constants` de CPython 3.12;
 * `testdata/lifecycle-events/ssrf-address-vectors.json` comprueba que los
 * tres lados dan el mismo veredicto.
 */
const IPV4_PRIVATE_NETWORKS = [
  "0.0.0.0/8",
  "10.0.0.0/8",
  "127.0.0.0/8",
  "169.254.0.0/16",
  "172.16.0.0/12",
  "192.0.0.0/24",
  "192.0.0.170/31",
  "192.0.2.0/24",
  "192.168.0.0/16",
  "198.18.0.0/15",
  "198.51.100.0/24",
  "203.0.113.0/24",
  "240.0.0.0/4",
  "255.255.255.255/32",
] as const;
const IPV4_PRIVATE_EXCEPTIONS = ["192.0.0.9/32", "192.0.0.10/32"] as const;
const IPV4_OTHER_BLOCKED_NETWORKS = [
  "127.0.0.0/8",
  "169.254.0.0/16",
  "224.0.0.0/4",
  "240.0.0.0/4",
  "0.0.0.0/32",
  "100.64.0.0/10",
] as const;
const IPV6_PRIVATE_NETWORKS = [
  "::1/128",
  "::/128",
  "::ffff:0:0/96",
  "64:ff9b:1::/48",
  "100::/64",
  "2001::/23",
  "2001:db8::/32",
  "2002::/16",
  "3fff::/20",
  "fc00::/7",
  "fe80::/10",
] as const;
const IPV6_PRIVATE_EXCEPTIONS = [
  "2001:1::1/128",
  "2001:1::2/128",
  "2001:3::/32",
  "2001:4:112::/48",
  "2001:20::/28",
  "2001:30::/28",
] as const;
const IPV6_OTHER_BLOCKED_NETWORKS = [
  "::1/128",
  "fe80::/10",
  "ff00::/8",
  "::/128",
  "::/8",
  "100::/8",
  "200::/7",
  "400::/6",
  "800::/5",
  "1000::/4",
  "4000::/3",
  "6000::/3",
  "8000::/3",
  "a000::/3",
  "c000::/3",
  "e000::/4",
  "f000::/5",
  "f800::/6",
  "fe00::/9",
] as const;
const IPV4_BITS = 32;
const IPV6_BITS = 128;
const IPV6_GROUPS = 8;
const IPV6_GROUP_BITS = 16n;
/** `::ffff:0:0/96`: los 80 bits altos a cero y luego `ffff`. */
const IPV4_MAPPED_PREFIX = 0xffffn << 32n;
const IPV4_MAPPED_MASK = ~((1n << 32n) - 1n) & ((1n << 128n) - 1n);
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
 * 253 en total en su forma punycode, que no sea `localhost` ni acabe en
 * `.localhost`, o una IP que `isBlockedWebhookAddress` no bloquee) y, si lo
 * lleva, puerto entre `MIN_PORT` y `MAX_PORT`. El mismo criterio que
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
  const address = literalAddress(parsed.hostname);
  if (address === "invalid") {
    return false;
  }
  if (address !== undefined) {
    return !isBlockedWebhookAddress(address);
  }
  return isValidDnsName(parsed.hostname) && !isLocalhost(parsed.hostname);
}

/** Una IP ya parseada: la familia y su valor como entero sin signo. */
export interface IpAddress {
  readonly family: 4 | 6;
  readonly value: bigint;
}

/**
 * `true` para una dirección a la que el deliverer nunca entrega: la misma
 * regla que `is_blocked` de su guard SSRF y que
 * `is_blocked_webhook_address` (`_domain.py`) —loopback, privada,
 * link-local (IMDS incluida), multicast, reservada, sin especificar o
 * CGNAT—, y una IPv6 con una IPv4 dentro (`::ffff:a.b.c.d`) se juzga como
 * esa IPv4. Aquí sólo se miran IPs literales: un nombre DNS que resuelve a
 * una de ellas (o que cambia de respuesta, DNS rebinding) lo sigue parando
 * el deliverer al resolver.
 */
export function isBlockedWebhookAddress(address: IpAddress): boolean {
  if (address.family === 6 && (address.value & IPV4_MAPPED_MASK) === IPV4_MAPPED_PREFIX) {
    return isBlockedWebhookAddress({ family: 4, value: address.value & 0xffffffffn });
  }
  const [privateNetworks, exceptions, others] =
    address.family === 4
      ? [IPV4_PRIVATE_NETWORKS, IPV4_PRIVATE_EXCEPTIONS, IPV4_OTHER_BLOCKED_NETWORKS]
      : [IPV6_PRIVATE_NETWORKS, IPV6_PRIVATE_EXCEPTIONS, IPV6_OTHER_BLOCKED_NETWORKS];
  const isPrivate = inAnyNetwork(address, privateNetworks) && !inAnyNetwork(address, exceptions);
  return isPrivate || inAnyNetwork(address, others);
}

/** Parsea una IP literal (`a.b.c.d`, o IPv6 con o sin corchetes). */
export function parseIpAddress(text: string): IpAddress | undefined {
  const bare = text.startsWith("[") && text.endsWith("]") ? text.slice(1, -1) : text;
  if (isIPv4(bare)) {
    return { family: 4, value: ipv4Value(bare) };
  }
  if (isIPv6(bare)) {
    return { family: 6, value: ipv6Value(bare) };
  }
  return undefined;
}

/**
 * La IP que escribe un `hostname` ya normalizado por el WHATWG `URL` (que
 * convierte `0x7f.1` o `2130706433` en `127.0.0.1`), `undefined` si es un
 * nombre DNS, o `"invalid"` si parece una IP pero no lo es.
 */
function literalAddress(hostname: string): IpAddress | "invalid" | undefined {
  if (hostname.startsWith("[") || /^[0-9.]+$/.test(hostname)) {
    return parseIpAddress(hostname) ?? "invalid";
  }
  return undefined;
}

function isLocalhost(hostname: string): boolean {
  const name = (hostname.endsWith(".") ? hostname.slice(0, -1) : hostname).toLowerCase();
  return name === LOCALHOST_NAME || name.endsWith(`.${LOCALHOST_NAME}`);
}

function inAnyNetwork(address: IpAddress, networks: readonly string[]): boolean {
  const bits = BigInt(address.family === 4 ? IPV4_BITS : IPV6_BITS);
  return networks.some((network) => {
    const [base = "", prefixText = ""] = network.split("/");
    const parsed = parseIpAddress(base);
    if (parsed === undefined || parsed.family !== address.family) {
      return false;
    }
    const hostBits = bits - BigInt(prefixText);
    return address.value >> hostBits === parsed.value >> hostBits;
  });
}

function ipv4Value(text: string): bigint {
  return text.split(".").reduce((value, octet) => (value << 8n) | BigInt(octet), 0n);
}

/** Expande `::` y una IPv4 final embebida (`::ffff:1.2.3.4`). */
function ipv6Value(text: string): bigint {
  const [head = "", tail] = text.split("::");
  const groups = (part: string): bigint[] =>
    part === ""
      ? []
      : part.split(":").flatMap((group) => {
          if (group.includes(".")) {
            const v4 = ipv4Value(group);
            return [v4 >> IPV6_GROUP_BITS, v4 & 0xffffn];
          }
          return [BigInt(`0x${group}`)];
        });
  const leading = groups(head);
  const trailing = tail === undefined ? [] : groups(tail);
  const zeros = Array<bigint>(IPV6_GROUPS - leading.length - trailing.length).fill(0n);
  return [...leading, ...zeros, ...trailing].reduce(
    (value, group) => (value << IPV6_GROUP_BITS) | group,
    0n,
  );
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
