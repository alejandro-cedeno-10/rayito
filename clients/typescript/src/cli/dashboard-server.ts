/**
 * La cáscara local de `rayito dashboard`: un servidor HTTP de `node:http` en
 * `127.0.0.1` que sirve la SPA y la API `/api/v1` de `DashboardApi`. No añade
 * dependencias ni servidores de Rayito: es un proceso tuyo, mientras la orden
 * corre. Espejo de `rayito.cli._dashboard_server`, con las mismas defensas y
 * la misma matriz de peticiones (`testdata/dashboard/guard-vectors.json`).
 *
 * Defensas (el panel usa tus credenciales de AWS, así que cualquier web que
 * abras en el navegador es un atacante posible):
 *
 * - **`Host` y `Origin`**: la misma lista (`ProxyAccess`) que
 *   `rayito sandbox proxy`. `Host` ajeno es `421` (DNS rebinding); un
 *   `Origin` presente y ajeno, incluido `null`, es `403`.
 * - **Token de arranque de un solo uso**: la URL impresa lleva `?token=…`; el
 *   servidor lo cambia por una cookie de sesión `HttpOnly`, `SameSite=Strict`,
 *   `Path=/`, y redirige a la URL limpia con `Referrer-Policy: no-referrer`.
 *   Un segundo uso, o uno pasado el TTL, se rechaza. Sin la cookie, nada
 *   responde.
 * - **`Sec-Fetch-Site`**: en `/api/v1`, si el navegador manda la cabecera y no
 *   es `same-origin` ni `none`, la petición es `403`. Cubre lo que `Origin` no
 *   ve: un `<img>` o una navegación de otra app local (incluida la web que
 *   sirve un sandbox por `rayito sandbox proxy` en `127.0.0.1:<otro puerto>`)
 *   es `same-site`, comparte el tarro de cookies y no manda `Origin`.
 * - **CSRF**: toda petición que escribe o gasta dinero (`POST` y el `GET` de
 *   coste facturado, `DashboardApi.requiresCsrf`) exige la cabecera
 *   `X-Rayito-CSRF` con el token de la sesión, que sólo entrega
 *   `GET /api/v1/session` a quien ya tiene la cookie; un `POST` además exige
 *   `Content-Type: application/json` (una web ajena no lo manda sin preflight).
 * - **Sólo lectura por defecto**: las acciones las gobierna `DashboardApi`.
 * - **Cabeceras**: CSP sin `unsafe-inline`, `nosniff`, `no-store`, `DENY`.
 * - **Nada se registra**: ni peticiones, ni cabeceras, ni la URL con el token.
 *
 * Todas las comparaciones de secretos usan `crypto.timingSafeEqual`.
 */

import { randomBytes, timingSafeEqual } from "node:crypto";
import {
  createServer,
  type IncomingHttpHeaders,
  type IncomingMessage,
  type Server,
  type ServerResponse,
} from "node:http";
import type { AddressInfo } from "node:net";
import {
  API_PREFIX,
  type ApiBackend,
  type ERROR_CODES,
  type JsonObject,
} from "../dashboard/api/contract.js";
import { LOGO_HEADERS, securityHeaders } from "../dashboard/api/headers.js";
import { ApiError, errorResponse } from "../dashboard/api/router.js";
import {
  INDEX_PATH,
  LOGO_PATH,
  loadStaticFiles,
  type StaticFile,
} from "../dashboard/api/static.js";
import type { Appearance } from "../dashboard/theme.js";
import { HTTP_OK, HTTP_SEE_OTHER } from "../http-status.js";
import {
  DASHBOARD_REQUEST_MAX_BODY_BYTES,
  DASHBOARD_REQUEST_TIMEOUT_SECONDS,
  DASHBOARD_SESSION_TOKEN_BYTES,
  DASHBOARD_START_TOKEN_TTL_SECONDS,
} from "../limits.js";
import { MS_PER_SECOND } from "../units.js";
import { buildAccess, type ProxyAccess } from "./access.js";
import { DASHBOARD_COOKIE_PREFIX } from "./http-head.js";

export const LOOPBACK = "127.0.0.1" as const;
export const CSRF_HEADER = "X-Rayito-CSRF" as const;
const FETCH_SITE_HEADER = "sec-fetch-site";
/** Las cabeceras que anuncian un cuerpo de petición (RFC 9112 §6). */
const BODY_HEADER_NAMES = ["content-length", "transfer-encoding"] as const;
/**
 * Los únicos valores de `Sec-Fetch-Site` que admite la API: la propia SPA, o
 * una navegación escrita por el usuario (sin origen que atacar).
 */
const ADMITTED_FETCH_SITES: ReadonlySet<string> = new Set(["same-origin", "none"]);
const SESSION_PATH = `${API_PREFIX}/session`;
const START_TOKEN_PARAM = "token";
const JSON_TYPE = "application/json";
const TEXT_TYPE = "text/plain; charset=utf-8";
const ALLOWED_METHODS: ReadonlySet<string> = new Set(["GET", "POST"]);
const LENGTH_TEXT = /^\d+$/;
const PLACEHOLDER_ORIGIN = "http://placeholder.invalid";

export const SECURITY_HEADERS: Readonly<Record<string, string>> = securityHeaders();

function safeEqual(left: string, right: string): boolean {
  const a = Buffer.from(left);
  const b = Buffer.from(right);
  return a.length === b.length && timingSafeEqual(a, b);
}

/**
 * El token de arranque (un solo uso, con TTL), la sesión y el token CSRF de
 * una ejecución. Todo se compara en tiempo constante.
 */
export class SessionGuard {
  readonly csrfToken: string;
  readonly cookieName: string;
  readonly #clock: () => number;
  readonly #tokenBytes: number;
  readonly #startTtlMs: number;
  readonly #issuedAt: number;
  #startToken: string | undefined;
  #session: string | undefined;

  constructor(
    port: number,
    options: {
      readonly clock?: (() => number) | undefined;
      readonly tokenBytes?: number;
      readonly startTtlMs?: number;
    } = {},
  ) {
    this.#clock = options.clock ?? (() => performance.now());
    this.#tokenBytes = options.tokenBytes ?? DASHBOARD_SESSION_TOKEN_BYTES;
    this.#startTtlMs = options.startTtlMs ?? DASHBOARD_START_TOKEN_TTL_SECONDS * MS_PER_SECOND;
    this.#issuedAt = this.#clock();
    this.#startToken = this.#newToken();
    this.csrfToken = this.#newToken();
    this.cookieName = `${DASHBOARD_COOKIE_PREFIX}${port}`;
  }

  /** El token que se imprime una sola vez en la URL de arranque. */
  get startToken(): string {
    if (this.#startToken === undefined) {
      throw new Error("el token de arranque ya se canjeó");
    }
    return this.#startToken;
  }

  /**
   * Cambia el token de arranque por el valor de la cookie de sesión;
   * `undefined` si no coincide, ya se usó o caducó. Se quema siempre que se
   * presenta uno válido, y sólo entonces.
   */
  redeem(presented: string | undefined): string | undefined {
    const expected = this.#startToken;
    if (presented === undefined || expected === undefined || !safeEqual(presented, expected)) {
      return undefined;
    }
    this.#startToken = undefined;
    if (this.#clock() - this.#issuedAt > this.#startTtlMs) {
      return undefined;
    }
    this.#session = this.#newToken();
    return this.#session;
  }

  hasSession(cookieHeader: string | undefined): boolean {
    if (this.#session === undefined || !cookieHeader) {
      return false;
    }
    const value = cookieValue(cookieHeader, this.cookieName);
    return value !== undefined && safeEqual(value, this.#session);
  }

  csrfMatches(presented: string | undefined): boolean {
    return presented !== undefined && safeEqual(presented, this.csrfToken);
  }

  sessionCookie(value: string): string {
    return `${this.cookieName}=${value}; HttpOnly; SameSite=Strict; Path=/`;
  }

  #newToken(): string {
    return randomBytes(this.#tokenBytes).toString("base64url");
  }
}

/** El valor de la última cookie `name` de una cabecera `Cookie`. */
function cookieValue(header: string, name: string): string | undefined {
  let found: string | undefined;
  for (const part of header.split(";")) {
    const separator = part.indexOf("=");
    if (separator >= 0 && part.slice(0, separator).trim() === name) {
      found = part.slice(separator + 1).trim();
    }
  }
  return found;
}

export interface Reply {
  readonly status: number;
  readonly body: Uint8Array;
  readonly contentType: string;
  readonly headers: Readonly<Record<string, string>>;
}

const ENCODER = new TextEncoder();

function jsonReply(
  status: number,
  body: JsonObject,
  headers: Readonly<Record<string, string>> = {},
): Reply {
  return { status, body: ENCODER.encode(JSON.stringify(body)), contentType: JSON_TYPE, headers };
}

function lowerCaseHeaders(headers: IncomingHttpHeaders): Record<string, string> {
  const flat: Record<string, string> = {};
  for (const [name, value] of Object.entries(headers)) {
    if (value !== undefined) {
      flat[name.toLowerCase()] = Array.isArray(value) ? value.join(", ") : value;
    }
  }
  return flat;
}

function decodePath(path: string): string {
  try {
    return decodeURIComponent(path);
  } catch {
    return path;
  }
}

function queryOf(url: URL): Record<string, string[]> {
  const query: Record<string, string[]> = {};
  for (const [name, value] of url.searchParams) {
    if (value !== "") {
      query[name] = [...(query[name] ?? []), value];
    }
  }
  return query;
}

/**
 * Lo que el servidor decide por petición, sin sockets: `respond`. Los tests
 * de seguridad la llaman directamente y la cáscara HTTP sólo la envuelve.
 */
export class LocalApp {
  readonly #api: ApiBackend;
  readonly #access: ProxyAccess;
  readonly #guard: SessionGuard;
  readonly #static: ReadonlyMap<string, StaticFile>;

  constructor(
    api: ApiBackend,
    access: ProxyAccess,
    guard: SessionGuard,
    staticFiles: ReadonlyMap<string, StaticFile>,
  ) {
    this.#api = api;
    this.#access = access;
    this.#guard = guard;
    this.#static = staticFiles;
  }

  /** `headers` con las claves en minúsculas; `body` ya leído (o `undefined`) y acotado por la cáscara. */
  async respond(
    method: string,
    target: string,
    headers: Readonly<Record<string, string>>,
    body: Uint8Array | undefined,
  ): Promise<Reply> {
    const rejection = this.#rejectOrigin(headers);
    if (rejection !== undefined) {
      return rejection;
    }
    const url = new URL(target, PLACEHOLDER_ORIGIN);
    const query = queryOf(url);
    if (!ALLOWED_METHODS.has(method)) {
      return this.errorReply("method_not_allowed");
    }
    const startToken = query[START_TOKEN_PARAM]?.[0];
    if (method === "GET" && url.pathname === INDEX_PATH && startToken !== undefined) {
      return this.#redeem(startToken);
    }
    if (!this.#guard.hasSession(headers.cookie)) {
      return this.errorReply("unauthorized");
    }
    const payload = body ?? new Uint8Array();
    if (url.pathname.startsWith(`${API_PREFIX}/`)) {
      const apiRequest = { method, path: decodePath(url.pathname), query, body: payload };
      const refusal = this.#rejectApiRequest(apiRequest, headers);
      if (refusal !== undefined) {
        return refusal;
      }
    }
    return this.#route(method, url.pathname, query, payload);
  }

  errorReply(code: keyof typeof ERROR_CODES): Reply {
    const response = errorResponse(new ApiError(code));
    return jsonReply(response.status, response.body);
  }

  #rejectOrigin(headers: Readonly<Record<string, string>>): Reply | undefined {
    if (!this.#access.admitsHost(headers.host)) {
      return this.errorReply("misdirected_request");
    }
    if (!this.#access.admitsOrigin(headers.origin)) {
      return this.errorReply("forbidden");
    }
    return undefined;
  }

  #rejectApiRequest(
    request: { method: string; path: string; query: Record<string, string[]>; body: Uint8Array },
    headers: Readonly<Record<string, string>>,
  ): Reply | undefined {
    const fetchSite = headers[FETCH_SITE_HEADER];
    if (fetchSite !== undefined && !ADMITTED_FETCH_SITES.has(fetchSite.toLowerCase())) {
      return this.errorReply("forbidden");
    }
    if (request.method === "POST") {
      const contentType = (headers["content-type"] ?? "").split(";")[0]?.trim().toLowerCase();
      if (contentType !== JSON_TYPE) {
        return this.errorReply("unsupported_media_type");
      }
    }
    if (
      this.#api.requiresCsrf(request) &&
      !this.#guard.csrfMatches(headers[CSRF_HEADER.toLowerCase()])
    ) {
      return this.errorReply("csrf_failed");
    }
    return undefined;
  }

  #redeem(token: string): Reply {
    const session = this.#guard.redeem(token);
    if (session === undefined) {
      return this.errorReply("unauthorized");
    }
    return {
      status: HTTP_SEE_OTHER,
      body: new Uint8Array(),
      contentType: TEXT_TYPE,
      headers: { Location: INDEX_PATH, "Set-Cookie": this.#guard.sessionCookie(session) },
    };
  }

  async #route(
    method: string,
    path: string,
    query: Record<string, string[]>,
    body: Uint8Array,
  ): Promise<Reply> {
    if (path === SESSION_PATH && method === "GET") {
      return jsonReply(HTTP_OK, { csrfToken: this.#guard.csrfToken });
    }
    if (path.startsWith(`${API_PREFIX}/`)) {
      const response = await this.#api.handle({ method, path: decodePath(path), query, body });
      return jsonReply(response.status, response.body);
    }
    const file = method === "GET" ? this.#static.get(path) : undefined;
    if (file === undefined) {
      return this.errorReply("not_found");
    }
    return {
      status: HTTP_OK,
      body: file.data,
      contentType: file.contentType,
      headers: path === LOGO_PATH ? LOGO_HEADERS : {},
    };
  }
}

function contentLength(value: string | undefined): number | undefined {
  return value === undefined || !LENGTH_TEXT.test(value) ? undefined : Number(value);
}

async function readBody(request: IncomingMessage): Promise<Uint8Array> {
  const chunks: Buffer[] = [];
  for await (const chunk of request) {
    chunks.push(chunk as Buffer);
  }
  return Buffer.concat(chunks);
}

function send(response: ServerResponse, reply: Reply, close: boolean): void {
  response.statusCode = reply.status;
  const headers: Record<string, string> = {
    "Content-Type": reply.contentType,
    "Content-Length": String(reply.body.length),
    ...SECURITY_HEADERS,
    ...reply.headers,
  };
  if (close) {
    headers.Connection = "close";
  }
  for (const [name, value] of Object.entries(headers)) {
    response.setHeader(name, value);
  }
  response.end(reply.body);
}

/**
 * Atiende una petición HTTP: sólo `POST` lleva cuerpo, acotado por
 * `DASHBOARD_REQUEST_MAX_BODY_BYTES` y por su `Content-Length`. Los demás
 * métodos no leen cuerpo: si la petición trae uno, se cierra la conexión
 * tras responder para que sus bytes no se lean como la petición siguiente
 * (desincronización de HTTP/1.1 con keep-alive).
 */
async function handle(
  app: LocalApp,
  request: IncomingMessage,
  response: ServerResponse,
): Promise<void> {
  const headers = lowerCaseHeaders(request.headers);
  const method = request.method ?? "GET";
  if (method === "POST") {
    const length = contentLength(headers["content-length"]);
    if (length === undefined) {
      return send(response, app.errorReply("invalid_argument"), true);
    }
    if (length > DASHBOARD_REQUEST_MAX_BODY_BYTES) {
      return send(response, app.errorReply("payload_too_large"), true);
    }
    const body = await readBody(request);
    return send(response, await app.respond(method, request.url ?? "/", headers, body), false);
  }
  const carriesBody = BODY_HEADER_NAMES.some((name) => name in headers);
  return send(
    response,
    await app.respond(method, request.url ?? "/", headers, undefined),
    carriesBody,
  );
}

export function startUrl(port: number, token: string): string {
  return `http://${LOOPBACK}:${port}${INDEX_PATH}?${START_TOKEN_PARAM}=${token}`;
}

export interface LocalDashboardOptions {
  /** Puerto local; 0 reserva uno libre al azar. */
  readonly port?: number;
  readonly appearance?: Appearance | undefined;
  /** El directorio de la SPA (`dist/dashboard-static/` en el paquete). */
  readonly staticDirectory: string;
  readonly clock?: (() => number) | undefined;
}

/**
 * Une una `DashboardApi` con el servidor: reserva el puerto (0, uno al
 * azar), construye la lista de `Host`/`Origin` con ese puerto y sirve.
 */
export class LocalDashboard {
  /** La dirección en la que escucha: siempre loopback. */
  readonly address: string;
  readonly port: number;
  readonly guard: SessionGuard;
  readonly app: LocalApp;
  readonly #server: Server;
  readonly #api: ApiBackend;

  private constructor(api: ApiBackend, server: Server, options: LocalDashboardOptions) {
    this.#api = api;
    this.#server = server;
    const bound = server.address() as AddressInfo;
    this.address = bound.address;
    this.port = bound.port;
    this.guard = new SessionGuard(this.port, { clock: options.clock });
    this.app = new LocalApp(
      api,
      buildAccess({ bind: LOOPBACK, localPort: this.port }),
      this.guard,
      loadStaticFiles(options.staticDirectory, options.appearance),
    );
    server.on("request", (request, response) => {
      handle(this.app, request, response).catch(() => {
        send(response, this.app.errorReply("internal_error"), true);
      });
    });
  }

  /** Abre el puerto en `127.0.0.1` y empieza a servir; rechaza si el puerto está ocupado. */
  static async start(api: ApiBackend, options: LocalDashboardOptions): Promise<LocalDashboard> {
    const server = createServer();
    server.requestTimeout = DASHBOARD_REQUEST_TIMEOUT_SECONDS * MS_PER_SECOND;
    server.headersTimeout = DASHBOARD_REQUEST_TIMEOUT_SECONDS * MS_PER_SECOND;
    await new Promise<void>((resolve, reject) => {
      server.once("error", reject);
      server.listen(options.port ?? 0, LOOPBACK, () => {
        server.off("error", reject);
        resolve();
      });
    });
    return new LocalDashboard(api, server, options);
  }

  /** La URL de arranque con el token: la única vez que se muestra. */
  get url(): string {
    return startUrl(this.port, this.guard.startToken);
  }

  /** Cierra el socket y la cola de acciones. */
  async shutdown(): Promise<void> {
    const closed = new Promise<void>((resolve) => this.#server.close(() => resolve()));
    this.#server.closeAllConnections();
    await closed;
    await this.#api.close();
  }
}
