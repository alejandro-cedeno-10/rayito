/**
 * Los `Host` y `Origin` que acepta el listener de `rayito sandbox proxy`
 * (dominio puro, sin E/S) y las autoridades HTTP (`host:puerto`) tal y como
 * las escribe un cliente. Espejo de `ProxyAccess` y `build_access` de
 * `rayito.cli._proxy` y de `rayito._authority`; los dos ejecutan los
 * vectores de `testdata/cli/proxy-vectors.json`.
 *
 * El proxy añade el JWE del operador a todo lo que reenvía y el guest sólo
 * ve `Host: <endpoint>`, así que no puede distinguir una petición del
 * operador de una que haga una web cualquiera abierta en su navegador. Por
 * eso el proxy exige un `Host` de esta lista (`421` si falta o no está) y un
 * `Origin`, si viene, de `http://` más una de esas autoridades o de
 * `--allow-origin` (`403`, incluido `Origin: null`): así se cortan el DNS
 * rebinding, el POST entre sitios y el WebSocket entre orígenes.
 */

import { isIPv6 } from "node:net";
import { httpAuthority, isWildcard } from "../authority.js";
import { InvalidArgumentError } from "../errors.js";
import { stripTrailing } from "../strings.js";
import { trimOws } from "./http-framing.js";

export const ALLOWED_HOST_OPTION = "--allowed-host";
export const ALLOW_ORIGIN_OPTION = "--allow-origin";

/** Los nombres de loopback que se aceptan siempre en `Host`, con el puerto local. */
export const LOOPBACK_AUTHORITY_HOSTS: readonly string[] = ["127.0.0.1", "localhost", "::1"];
const LOOPBACK_ADDRESSES: ReadonlySet<string> = new Set(LOOPBACK_AUTHORITY_HOSTS);
/** Un `Host` sin puerto sólo equivale a `<nombre>:<puerto>` cuando el puerto es el de HTTP (RFC 9110 §4.2.1). */
const HTTP_DEFAULT_PORT = 80;
const HTTP_SCHEME = "http";
const ORIGIN_SCHEMES: readonly string[] = ["http://", "https://"];
/** Los navegadores resuelven `*.localhost` a loopback (RFC 6761 §6.3) y le dan un tarro de cookies propio. */
const LOCALHOST_SUFFIX = ".localhost";
/** Una etiqueta DNS (RFC 1035 §2.3.4): `<id>.localhost` sólo se ofrece si el id del sandbox cabe en una. */
const DNS_LABEL = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;
/** Lo que nunca aparece en un `host[:puerto]`: esquema, ruta, userinfo, espacios. */
const NOT_IN_AUTHORITY = /[/@\s?#]/;
const PORT_DIGITS = /^[0-9]+$/;
const PORT_SEPARATOR = ":";
const IPV6_PORT_SEPARATOR = "]:";
const IPV6_OPEN_BRACKET = "[";
const SLASH = "/";

export function isIpv6Literal(host: string): boolean {
  return isIPv6(host);
}

export function isLoopbackBind(bind: string): boolean {
  return LOOPBACK_ADDRESSES.has(bind);
}

function bracketed(host: string): string {
  return isIpv6Literal(host) ? `[${host}]` : host;
}

/** `nombre:puerto` y, sólo si el puerto es el 80, también `nombre` a secas (un navegador omite el puerto por defecto). */
function nameAuthorities(name: string, localPort: number): Set<string> {
  const authorities = new Set([httpAuthority(name, localPort).toLowerCase()]);
  if (localPort === HTTP_DEFAULT_PORT) {
    authorities.add(bracketed(name).toLowerCase());
  }
  return authorities;
}

function hasExplicitPort(value: string): boolean {
  if (value.startsWith(IPV6_OPEN_BRACKET)) {
    return value.includes(IPV6_PORT_SEPARATOR);
  }
  return !isIpv6Literal(value) && value.includes(PORT_SEPARATOR);
}

/**
 * Un `--allowed-host`: con puerto se compara tal cual; sin puerto admite el
 * nombre a secas (un proxy inverso delante, en el 80/443) y con el puerto
 * local.
 */
function allowedHostAuthorities(value: string, localPort: number): Set<string> {
  const host = value.trim().toLowerCase();
  if (host === "" || NOT_IN_AUTHORITY.test(host)) {
    throw new InvalidArgumentError(
      `${ALLOWED_HOST_OPTION} espera host o host:puerto, sin esquema ni ruta`,
    );
  }
  if (!hasExplicitPort(host)) {
    return new Set([bracketed(host), httpAuthority(host, localPort).toLowerCase()]);
  }
  const port = host.slice(host.lastIndexOf(PORT_SEPARATOR) + 1);
  if (!PORT_DIGITS.test(port)) {
    throw new InvalidArgumentError(`${ALLOWED_HOST_OPTION}: puerto inválido`);
  }
  return new Set([host]);
}

function addAll(target: Set<string>, source: Iterable<string>): void {
  for (const item of source) {
    target.add(item);
  }
}

function allowedOrigin(value: string): string {
  const origin = stripTrailing(value.trim().toLowerCase(), SLASH);
  const scheme = ORIGIN_SCHEMES.find((prefix) => origin.startsWith(prefix));
  const authority = scheme === undefined ? "" : origin.slice(scheme.length);
  if (authority === "" || NOT_IN_AUTHORITY.test(authority)) {
    throw new InvalidArgumentError(
      `${ALLOW_ORIGIN_OPTION} espera un origen http(s)://host[:puerto], sin ruta`,
    );
  }
  return origin;
}

/** `<id>.localhost`, o `undefined` si no hay id o no cabe en una etiqueta DNS. */
export function sandboxLocalhostName(sandboxId: string | undefined): string | undefined {
  if (sandboxId === undefined) {
    return undefined;
  }
  const label = sandboxId.toLowerCase();
  return DNS_LABEL.test(label) ? `${label}${LOCALHOST_SUFFIX}` : undefined;
}

/** Los `Host` y `Origin` que acepta el listener, ya normalizados (minúsculas, sin `/` final). */
export class ProxyAccess {
  readonly hosts: ReadonlySet<string>;
  readonly origins: ReadonlySet<string>;
  readonly sandboxHost: string | undefined;

  constructor(
    hosts: ReadonlySet<string>,
    origins: ReadonlySet<string>,
    sandboxHost: string | undefined,
  ) {
    this.hosts = hosts;
    this.origins = origins;
    this.sandboxHost = sandboxHost;
  }

  admitsHost(host: string | undefined): boolean {
    return host !== undefined && this.hosts.has(trimOws(host).toLowerCase());
  }

  /** Sin `Origin` (curl, un navegador en una petición del mismo origen que no lo manda) pasa; `null` u otro origen, no. */
  admitsOrigin(origin: string | undefined): boolean {
    return (
      origin === undefined || this.origins.has(stripTrailing(trimOws(origin).toLowerCase(), SLASH))
    );
  }
}

export interface AccessInput {
  readonly bind: string;
  readonly localPort: number;
  readonly sandboxId?: string | undefined;
  readonly allowedHosts?: readonly string[];
  readonly allowedOrigins?: readonly string[];
}

/**
 * La lista de `Host`/`Origin` del listener: los nombres de loopback y
 * `--bind` (si no es comodín) con el puerto local, `<id>.localhost` con un
 * `--bind` de loopback, cada `--allowed-host` y cada `--allow-origin`. Un
 * valor mal formado es `InvalidArgumentError`, antes de tocar AWS.
 */
export function buildAccess(input: AccessInput): ProxyAccess {
  const names = [...LOOPBACK_AUTHORITY_HOSTS];
  if (!isWildcard(input.bind)) {
    names.push(input.bind);
  }
  const sandboxHost = isLoopbackBind(input.bind)
    ? sandboxLocalhostName(input.sandboxId)
    : undefined;
  if (sandboxHost !== undefined) {
    names.push(sandboxHost);
  }
  const hosts = new Set<string>();
  for (const name of names) {
    addAll(hosts, nameAuthorities(name, input.localPort));
  }
  for (const value of input.allowedHosts ?? []) {
    addAll(hosts, allowedHostAuthorities(value, input.localPort));
  }
  const origins = new Set([...hosts].map((host) => `${HTTP_SCHEME}://${host}`));
  for (const value of input.allowedOrigins ?? []) {
    origins.add(allowedOrigin(value));
  }
  return new ProxyAccess(hosts, origins, sandboxHost);
}
