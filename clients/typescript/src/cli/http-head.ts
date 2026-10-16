/**
 * La cabecera de una petición o respuesta HTTP/1.1 y su reescritura hacia el
 * endpoint del sandbox (dominio puro, sin E/S). Espejo de `HttpHead`,
 * `parse_http_head` y `rewrite_head` de `rayito.cli._proxy`; los dos
 * ejecutan los vectores de `testdata/cli/proxy-vectors.json`.
 *
 * **Contrabando de cabeceras (`request smuggling`)**: `parseHttpHead`
 * rechaza cualquier cabecera con un CR o LF suelto fuera de un `\r\n`, una
 * continuación `obs-fold` (RFC 7230 la quitó), un nombre con espacios o fuera
 * de los `tchar` de RFC 9110 §5.6.2, o una línea sin `:`. Un `\n` suelto en
 * el valor de una cabecera podía colar una línea `x-aws-proxy-port` falsa
 * delante de la real: el nombre exterior no empieza por `x-aws-proxy-`, así
 * que el filtro por nombre no la veía.
 */

import {
  type BodyFraming,
  CONTENT_LENGTH_HEADER_NAME,
  requestBodyFraming,
  TRANSFER_ENCODING_HEADER_NAME,
  trimOws,
} from "./http-framing.js";

export const HEADER_TERMINATOR = "\r\n\r\n";
const CRLF = "\r\n";
const HEADER_NAME_SEPARATOR = ":";
const CONTINUATION_LINE_STARTS: ReadonlySet<string> = new Set([" ", "\t"]);
const REQUEST_LINE_SEPARATOR = " ";
const LIST_SEPARATOR = ",";
/** RFC 9110 §5.6.2 `tchar`: los nombres de cabecera que no cumplan esto se rechazan. */
const TOKEN_CHARACTERS = /^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$/;
const BARE_LINE_BREAK = /[\r\n]/;
export const PROXY_HEADER_PREFIX = "x-aws-proxy-";
export const AUTH_HEADER_NAME = "X-aws-proxy-auth";
export const PORT_HEADER_NAME = "X-aws-proxy-port";
export const HOST_HEADER_NAME = "Host";
export const ORIGIN_HEADER_NAME = "Origin";
const COOKIE_HEADER_NAME = "Cookie";
const COOKIE_SEPARATOR = "; ";
const COOKIE_PAIR_DELIMITER = ";";
/** Prefijo de la cookie de sesión de `rayito dashboard` (`<prefijo><puerto>`). */
export const DASHBOARD_COOKIE_PREFIX = "rayito_dashboard_";
const CONNECTION_HEADER_NAME = "Connection";
const UPGRADE_HEADER_NAME = "Upgrade";
const UPGRADE_TOKEN = "upgrade";
const CLOSE_TOKEN = "close";

/** La cabecera del cliente no es HTTP/1.1 estricto; nunca lleva el contenido de la petición. */
export class MalformedHttpHeadError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "MalformedHttpHeadError";
  }
}

export type HeaderPair = readonly [name: string, value: string];

/** Línea inicial y cabeceras de un mensaje HTTP/1.1 ya separadas. */
export class HttpHead {
  readonly requestLine: string;
  readonly headers: readonly HeaderPair[];

  constructor(requestLine: string, headers: readonly HeaderPair[]) {
    this.requestLine = requestLine;
    this.headers = headers;
  }

  header(name: string): string | undefined {
    return this.headerValues(name)[0];
  }

  /** Todos los valores de `name`, en orden (las cabeceras que pueden repetirse). */
  headerValues(name: string): string[] {
    const target = name.toLowerCase();
    return this.headers.filter(([key]) => key.toLowerCase() === target).map(([, value]) => value);
  }

  get method(): string {
    return this.requestLine.split(REQUEST_LINE_SEPARATOR, 1)[0] ?? "";
  }

  /** El cuerpo de esta petición; `MalformedFramingError` si es ambiguo. */
  bodyFraming(): BodyFraming {
    return requestBodyFraming(
      this.headerValues(TRANSFER_ENCODING_HEADER_NAME),
      this.headerValues(CONTENT_LENGTH_HEADER_NAME),
    );
  }

  /**
   * RFC 9110 §7.8: hace falta `Upgrade` Y el token `upgrade` en `Connection`;
   * un `Upgrade` suelto (con `Connection: keep-alive`) no es un upgrade y se
   * trata como cualquier petición.
   */
  isUpgrade(): boolean {
    const tokens = (this.header(CONNECTION_HEADER_NAME) ?? "")
      .split(LIST_SEPARATOR)
      .map((token) => trimOws(token).toLowerCase());
    return tokens.includes(UPGRADE_TOKEN) && this.header(UPGRADE_HEADER_NAME) !== undefined;
  }
}

function parseHeaderLine(line: string): HeaderPair {
  if (CONTINUATION_LINE_STARTS.has(line.charAt(0))) {
    throw new MalformedHttpHeadError("continuación obs-fold no soportada");
  }
  const separator = line.indexOf(HEADER_NAME_SEPARATOR);
  if (separator < 0) {
    throw new MalformedHttpHeadError("línea de cabecera sin ':'");
  }
  const name = line.slice(0, separator);
  if (!TOKEN_CHARACTERS.test(name)) {
    throw new MalformedHttpHeadError("nombre de cabecera fuera de los tchar de RFC 9110");
  }
  return [name, trimOws(line.slice(separator + 1))];
}

/**
 * `raw` es la cabecera decodificada como latin-1 e incluye el `\r\n\r\n`
 * final. HTTP/1.1 estricto: cada línea termina en `\r\n` (ni CR ni LF
 * sueltos), sin `obs-fold`, con nombre hecho sólo de `tchar` y con `:`;
 * cualquier otra cosa es `MalformedHttpHeadError`.
 */
export function parseHttpHead(raw: string): HttpHead {
  if (!raw.endsWith(HEADER_TERMINATOR)) {
    throw new MalformedHttpHeadError("cabecera sin terminador CRLF CRLF");
  }
  const lines = raw.slice(0, -HEADER_TERMINATOR.length).split(CRLF);
  if (lines.some((line) => BARE_LINE_BREAK.test(line))) {
    throw new MalformedHttpHeadError("CR o LF suelto fuera de un CRLF");
  }
  const [requestLine = "", ...headerLines] = lines;
  if (requestLine === "") {
    throw new MalformedHttpHeadError("línea de petición vacía");
  }
  const headers = headerLines.filter((line) => line !== "").map(parseHeaderLine);
  return new HttpHead(requestLine, headers);
}

/** El valor de `Cookie` sin las cookies de sesión del panel local; `undefined` si no queda ninguna. */
export function withoutDashboardCookies(cookieHeader: string): string | undefined {
  const kept = cookieHeader
    .split(COOKIE_PAIR_DELIMITER)
    .map(trimOws)
    .filter((cookie) => cookie !== "" && !cookie.startsWith(DASHBOARD_COOKIE_PREFIX));
  return kept.length === 0 ? undefined : kept.join(COOKIE_SEPARATOR);
}

function withCookiesFiltered(headers: readonly HeaderPair[]): HeaderPair[] {
  const filtered: HeaderPair[] = [];
  for (const [name, value] of headers) {
    if (name.toLowerCase() !== COOKIE_HEADER_NAME.toLowerCase()) {
      filtered.push([name, value]);
      continue;
    }
    const remaining = withoutDashboardCookies(value);
    if (remaining !== undefined) {
      filtered.push([name, remaining]);
    }
  }
  return filtered;
}

function serialise(startLine: string, headers: readonly HeaderPair[]): string {
  return [startLine, ...headers.map(([name, value]) => `${name}: ${value}`), "", ""].join(CRLF);
}

export interface RewriteTarget {
  readonly endpoint: string;
  readonly jwe: string;
  readonly port: number;
}

/**
 * Cabecera reescrita hacia `<endpoint>:443` (AWS_API_NOTES.md §7): quita
 * cualquier `x-aws-proxy-*` y `Host` que traiga el cliente, añade `Host`,
 * `X-aws-proxy-auth` y `X-aws-proxy-port`, y fuerza `Connection: close` salvo
 * en una petición `Upgrade`, donde `Connection`/`Upgrade` se dejan intactas
 * para que el handshake de WebSocket llegue igual. Quita también las cookies
 * de sesión de `rayito dashboard`.
 */
export function rewriteHead(head: HttpHead, target: RewriteTarget): string {
  const upgrade = head.isUpgrade();
  const kept = withCookiesFiltered(head.headers).filter(([name]) => {
    const lowered = name.toLowerCase();
    return (
      !lowered.startsWith(PROXY_HEADER_PREFIX) &&
      lowered !== HOST_HEADER_NAME.toLowerCase() &&
      (upgrade || lowered !== CONNECTION_HEADER_NAME.toLowerCase())
    );
  });
  kept.push([HOST_HEADER_NAME, target.endpoint]);
  kept.push([AUTH_HEADER_NAME, target.jwe]);
  kept.push([PORT_HEADER_NAME, String(target.port)]);
  if (!upgrade) {
    kept.push([CONNECTION_HEADER_NAME, CLOSE_TOKEN]);
  }
  return serialise(head.requestLine, kept);
}

/** La cabecera de una respuesta con su `Connection` sustituido por `Connection: close`. */
export function closingResponseHead(response: HttpHead): string {
  const kept = response.headers.filter(
    ([name]) => name.toLowerCase() !== CONNECTION_HEADER_NAME.toLowerCase(),
  );
  kept.push([CONNECTION_HEADER_NAME, CLOSE_TOKEN]);
  return serialise(response.requestLine, kept);
}
