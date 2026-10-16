/**
 * `DashboardApi`: el núcleo de `/api/v1`, sin transporte ni autenticación
 * (eso es de cada cáscara: la local con sesión, `Host`/`Origin` y CSRF).
 * Traduce una `ApiRequest` en llamadas a `Fleet`, `Quotas` y `UsageCost` y
 * devuelve un documento JSON. Espejo de `rayito._dashboard._api._router`.
 *
 * - Sólo lectura por defecto: las acciones exigen `allowActions: true` y un
 *   `"confirm": true` en el cuerpo, y se encolan en `BulkJobs` al ritmo del
 *   TPS.
 * - El coste facturado (Cost Explorer, de pago) exige `costExplorer: true` y
 *   `?billed=true`; sin ello la API sólo da la estimación, que es gratuita.
 * - Las respuestas nunca llevan el mensaje de AWS: sólo códigos.
 */

import {
  AuthenticationError,
  InvalidArgumentError,
  NotFoundError,
  QuotaExceededError,
  RateLimitError,
  SandboxError,
} from "../../errors.js";
import { HTTP_ACCEPTED, HTTP_OK } from "../../http-status.js";
import { DEFAULT_GET_EVENTS_LIMIT } from "../../lifecycle-events/domain.js";
import {
  COST_CACHE_TTL_SECONDS,
  COST_EXPLORER_REQUEST_PRICE_USD,
  DASHBOARD_BULK_MAX_SANDBOXES,
  DASHBOARD_EVENTS_MAX_LIMIT,
  DASHBOARD_JOB_POLL_INTERVAL_SECONDS,
  DASHBOARD_LOGS_MAX_LINES,
  DASHBOARD_POLL_INTERVAL_SECONDS,
  DASHBOARD_READ_CACHE_SECONDS,
} from "../../limits.js";
import type { Logger } from "../../logger.js";
import { MS_PER_SECOND } from "../../units.js";
import { VERSION } from "../../version.js";
import type { UsageCost } from "../cost.js";
import {
  BULK_OPERATIONS,
  type BulkAction,
  type BulkPlan,
  type QuotaReport,
  TtlCache,
} from "../domain.js";
import { type Fleet, validatedSandboxIds } from "../fleet.js";
import type { Quotas } from "../quotas.js";
import { type Branding, DEFAULT_BRANDING } from "../theme.js";
import {
  ACTION_NAMES,
  API_PREFIX,
  API_VERSION,
  type ApiRequest,
  type ApiResponse,
  DEPLOYMENT_LOCAL,
  ERROR_CODES,
  ERROR_MESSAGES,
  type ErrorCode,
  type JsonObject,
  type LogsSource,
} from "./contract.js";
import { BulkJobs, type JobQueue, JobsBusyError } from "./jobs.js";
import {
  billedJson,
  estimateJson,
  eventJson,
  imageJson,
  imageVersionJson,
  planJson,
  quotasJson,
  sandboxJson,
} from "./serialize.js";

const SEGMENT = "([^/]+)";
const SANDBOX_ACTION_PATTERN = new RegExp(`^${API_PREFIX}/sandboxes/${SEGMENT}/${SEGMENT}$`);
const BULK_ACTION_PATTERN = new RegExp(`^${API_PREFIX}/actions$`);
const GET = "GET";
const POST = "POST";
const COST_SUFFIX = "/cost";
const COST_PATH = `${API_PREFIX}${COST_SUFFIX}`;
const TRUTHY: ReadonlySet<string> = new Set(["1", "true", "yes"]);
const EVENT_ORDERS = ["asc", "desc"] as const;
const DEFAULT_EVENT_ORDER = "desc";
const MIN_QUERY_LIMIT = 1;
const INTEGER_TEXT = /^\s*[+-]?\d+\s*$/;
const DECIMAL_RADIX = 10;
const EMPTY_BODY = "{}";
const SANDBOX_STATE_PARAM = "state";

type Query = Readonly<Record<string, readonly string[]>>;
type Match = RegExpExecArray;
type Handler = (request: ApiRequest, match: Match) => Promise<ApiResponse>;

/** Un error que se devuelve tal cual: `code` de `ERROR_CODES` y, si la hay, la causa de AWS por su código. */
export class ApiError extends Error {
  readonly code: ErrorCode;
  readonly awsCode: string | undefined;

  constructor(code: ErrorCode, message?: string, options: { readonly awsCode?: string } = {}) {
    super(message || ERROR_MESSAGES[code]);
    this.name = "ApiError";
    this.code = code;
    this.awsCode = options.awsCode;
  }
}

export function errorResponse(error: ApiError): ApiResponse {
  return {
    status: ERROR_CODES[error.code],
    body: {
      error: {
        code: error.code,
        message: error.message,
        ...(error.awsCode === undefined ? {} : { awsCode: error.awsCode }),
      },
    },
  };
}

function awsCodeOf(error: unknown): string | undefined {
  const code = (error as { awsCode?: unknown } | null)?.awsCode;
  return typeof code === "string" ? code : undefined;
}

/**
 * Una excepción del SDK en un `ApiError`, por clase y código de AWS, nunca
 * por el texto del mensaje (que no se repite).
 */
export function translate(error: unknown): ApiError {
  const awsCode = awsCodeOf(error);
  const options = awsCode === undefined ? {} : { awsCode };
  if (error instanceof InvalidArgumentError) {
    return awsCode === undefined
      ? new ApiError("invalid_argument", error.message)
      : new ApiError("invalid_argument", undefined, options);
  }
  if (error instanceof NotFoundError) {
    return new ApiError("not_found");
  }
  if (error instanceof AuthenticationError) {
    return new ApiError("aws_access_denied", undefined, options);
  }
  if (error instanceof RateLimitError || error instanceof QuotaExceededError) {
    return new ApiError("aws_throttled", undefined, options);
  }
  if (error instanceof SandboxError) {
    return new ApiError("aws_error", undefined, options);
  }
  return new ApiError("internal_error");
}

/** Qué pide una petición de acción ya validada: la acción y los ids (únicos y con su longitud comprobada). */
export interface ActionTargets {
  readonly action: BulkAction;
  readonly sandboxIds: readonly string[];
}

/** `true` para `POST /sandboxes/<id>/<acción>` y `POST /actions`: las dos rutas que ejecutan algo sobre un sandbox. */
export function isActionRoute(method: string, path: string): boolean {
  return method === POST && (SANDBOX_ACTION_PATTERN.test(path) || BULK_ACTION_PATTERN.test(path));
}

/**
 * Los objetivos de `POST /sandboxes/<id>/<acción>` y de `POST /actions`, o
 * `undefined` si la petición no es una de esas dos rutas. Lanza el mismo
 * `ApiError` que la ruta.
 */
export function actionTargets(request: ApiRequest): ActionTargets | undefined {
  if (!isActionRoute(request.method, request.path)) {
    return undefined;
  }
  const single = SANDBOX_ACTION_PATTERN.exec(request.path);
  if (single !== null) {
    return targets(actionOf(single[2]), [single[1] as string], request);
  }
  const body = jsonBody(request);
  const ids = body.sandboxIds;
  if (!Array.isArray(ids) || !ids.every((item) => typeof item === "string")) {
    throw new ApiError("invalid_argument", "sandboxIds: se esperaba una lista de ids");
  }
  return targets(actionOf(body.action), ids as string[], request);
}

function targets(action: BulkAction, ids: string[], request: ApiRequest): ActionTargets {
  if (jsonBody(request).confirm !== true) {
    throw new ApiError("confirmation_required");
  }
  if (ids.length > DASHBOARD_BULK_MAX_SANDBOXES) {
    throw new ApiError(
      "invalid_argument",
      `sandboxIds: como mucho ${DASHBOARD_BULK_MAX_SANDBOXES} ids por acción`,
    );
  }
  return { action, sandboxIds: validatedSandboxIds(ids) };
}

interface Route {
  readonly method: string;
  readonly pattern: RegExp;
  readonly handler: Handler;
}

export interface DashboardApiOptions {
  readonly fleet: Fleet;
  readonly quotas: Quotas;
  readonly cost: UsageCost;
  readonly region: string;
  readonly jobs?: JobQueue | undefined;
  readonly logs?: LogsSource | undefined;
  readonly eventsConfigured?: boolean | undefined;
  readonly costExplorer?: boolean | undefined;
  readonly branding?: Branding | undefined;
  /** Reloj monótono en milisegundos de la caché de lecturas (inyectable en tests). */
  readonly monotonicMs?: (() => number) | undefined;
  readonly logger?: Logger | undefined;
}

/** El contrato `/api/v1` sobre los servicios del SDK. */
export class DashboardApi {
  readonly #fleet: Fleet;
  readonly #quotas: Quotas;
  readonly #cost: UsageCost;
  readonly #region: string;
  readonly #logs: LogsSource | undefined;
  readonly #eventsConfigured: boolean;
  readonly #costExplorer: boolean;
  readonly #branding: Branding;
  readonly #logger: Logger | undefined;
  readonly #cache: TtlCache<string, unknown>;
  readonly #jobs: JobQueue;
  readonly #routes: readonly Route[];
  #generation = 0;

  constructor(options: DashboardApiOptions) {
    this.#fleet = options.fleet;
    this.#quotas = options.quotas;
    this.#cost = options.cost;
    this.#region = options.region;
    this.#logs = options.logs;
    this.#eventsConfigured = options.eventsConfigured ?? false;
    this.#costExplorer = options.costExplorer ?? false;
    this.#branding = options.branding ?? DEFAULT_BRANDING;
    this.#logger = options.logger;
    this.#cache = new TtlCache(
      DASHBOARD_READ_CACHE_SECONDS * MS_PER_SECOND,
      options.monotonicMs ?? (() => performance.now()),
    );
    this.#jobs =
      options.jobs ?? new BulkJobs(options.fleet, { onFinished: () => this.invalidate() });
    this.#routes = this.#buildRoutes();
  }

  get jobs(): JobQueue {
    return this.#jobs;
  }

  /**
   * Descarta las lecturas cacheadas (tras una acción). La generación impide
   * que una lectura que empezó antes vuelva a guardar su dato viejo.
   */
  invalidate(): void {
    this.#generation += 1;
    this.#cache.clear();
  }

  close(): Promise<void> {
    return this.#jobs.close();
  }

  /**
   * Si la petición cambia algo o gasta dinero. Las cáscaras le exigen
   * entonces su defensa CSRF: toda escritura y la consulta facturada de Cost
   * Explorer, que aunque sea un `GET` cuesta cada vez.
   */
  requiresCsrf(request: ApiRequest): boolean {
    if (request.method !== GET) {
      return true;
    }
    return request.path === COST_PATH && flag(request.query, "billed");
  }

  async handle(request: ApiRequest): Promise<ApiResponse> {
    try {
      return await this.#dispatch(request);
    } catch (error) {
      if (error instanceof ApiError) {
        return errorResponse(error);
      }
      this.#logger?.warn?.("dashboard: petición fallida", {
        method: request.method,
        error: error instanceof Error ? error.name : typeof error,
      });
      return errorResponse(translate(error));
    }
  }

  /**
   * Cuánto tardaría una acción masiva sobre `count` sandboxes con la cuota de
   * TPS aplicada. Es un cálculo local más la lectura de cuotas (gratuita).
   */
  async planFor(action: BulkAction, count: number): Promise<BulkPlan> {
    return this.#fleet.planBulk(action, count, { appliedRate: await this.#appliedRate(action) });
  }

  async #dispatch(request: ApiRequest): Promise<ApiResponse> {
    let matchedPath = false;
    for (const route of this.#routes) {
      const match = route.pattern.exec(request.path);
      if (match === null) {
        continue;
      }
      matchedPath = true;
      if (route.method === request.method) {
        return route.handler(request, match);
      }
    }
    throw new ApiError(matchedPath ? "method_not_allowed" : "not_found");
  }

  #buildRoutes(): Route[] {
    const route = (method: string, suffix: string, handler: Handler): Route => ({
      method,
      pattern: new RegExp(`^${API_PREFIX}${suffix}$`),
      handler,
    });
    return [
      route(GET, "/meta", (request) => this.#meta(request)),
      route(GET, "/accounts", () => this.#accounts()),
      route(GET, "/sandboxes", (request) => this.#sandboxes(request)),
      route(POST, `/sandboxes/${SEGMENT}/${SEGMENT}`, (request) => this.#submit(request)),
      route(GET, "/images", (request) => this.#images(request)),
      route(GET, "/images/versions", (request) => this.#imageVersions(request)),
      route(GET, "/quotas", () => this.#quotasReport()),
      route(GET, COST_SUFFIX, (request) => this.#costReport(request)),
      route(GET, "/events", (request) => this.#events(request)),
      route(GET, "/logs", (request) => this.#logLines(request)),
      route(GET, "/actions", () => this.#listJobs()),
      route(GET, "/actions/plan", (request) => this.#plan(request)),
      route(GET, `/actions/${SEGMENT}`, (_request, match) => this.#job(match)),
      route(POST, "/actions", (request) => this.#submit(request)),
    ];
  }

  async #cached<T>(key: string, load: () => Promise<T>): Promise<T> {
    const found = this.#cache.get(key);
    if (found !== undefined) {
      return found as T;
    }
    const generation = this.#generation;
    const value = await load();
    if (generation === this.#generation) {
      this.#cache.put(key, value);
    }
    return value;
  }

  #ok(body: JsonObject, status: number = HTTP_OK): ApiResponse {
    return { status, body };
  }

  async #meta(_request: ApiRequest): Promise<ApiResponse> {
    return this.#ok({
      apiVersion: API_VERSION,
      rayitoVersion: VERSION,
      region: this.#region,
      deployment: DEPLOYMENT_LOCAL,
      allowActions: this.#fleet.allowActions,
      costExplorer: this.#costExplorerJson(),
      eventsConfigured: this.#eventsConfigured,
      logsAvailable: this.#logs !== undefined,
      pollIntervalSeconds: DASHBOARD_POLL_INTERVAL_SECONDS,
      jobPollIntervalSeconds: DASHBOARD_JOB_POLL_INTERVAL_SECONDS,
      bulkMaxSandboxes: DASHBOARD_BULK_MAX_SANDBOXES,
      theme: { name: this.#branding.name, logo: this.#branding.logo },
    });
  }

  /** Sin vista multicuenta, ninguna cuenta adicional; `AccountsApi` responde por encima con las que haya. */
  async #accounts(): Promise<ApiResponse> {
    return this.#ok({ accounts: [] });
  }

  #costExplorerJson(): JsonObject {
    return {
      enabled: this.#costExplorer,
      requestPriceUsd: COST_EXPLORER_REQUEST_PRICE_USD,
      cacheTtlSeconds: COST_CACHE_TTL_SECONDS,
    };
  }

  async #sandboxes(request: ApiRequest): Promise<ApiResponse> {
    const stateFilter = request.query[SANDBOX_STATE_PARAM] ?? [];
    const states = stateFilter.length > 0 ? stateFilter : undefined;
    const image = single(request.query, "image");
    const views = await this.#cached(
      JSON.stringify(["sandboxes", states ?? null, image ?? null]),
      () => this.#fleet.sandboxes({ states, image }),
    );
    return this.#ok({ sandboxes: views.map(sandboxJson) });
  }

  async #images(request: ApiRequest): Promise<ApiResponse> {
    const nameFilter = single(request.query, "filter");
    const images = await this.#cached(JSON.stringify(["images", nameFilter ?? null]), () =>
      this.#fleet.images({ nameFilter }),
    );
    return this.#ok({ images: images.map(imageJson) });
  }

  async #imageVersions(request: ApiRequest): Promise<ApiResponse> {
    const image = required(request.query, "image");
    const versions = await this.#cached(JSON.stringify(["versions", image]), () =>
      this.#fleet.imageVersions(image),
    );
    return this.#ok({ image, versions: versions.map(imageVersionJson) });
  }

  #quotaReport(): Promise<QuotaReport> {
    return this.#cached("quotas", () => this.#quotas.report());
  }

  async #quotasReport(): Promise<ApiResponse> {
    return this.#ok(quotasJson(await this.#quotaReport()));
  }

  async #costReport(request: ApiRequest): Promise<ApiResponse> {
    const estimate = await this.#cached("estimate", () => this.#cost.estimate());
    let billed: JsonObject | null = null;
    if (flag(request.query, "billed")) {
      if (!this.#costExplorer) {
        throw new ApiError("cost_explorer_disabled");
      }
      billed = billedJson(
        await this.#cost.billed({
          start: single(request.query, "start"),
          end: single(request.query, "end"),
          region: single(request.query, "region"),
          refresh: flag(request.query, "refresh"),
        }),
      );
    }
    return this.#ok({
      estimated: estimateJson(estimate),
      billed,
      costExplorer: this.#costExplorerJson(),
    });
  }

  async #events(request: ApiRequest): Promise<ApiResponse> {
    if (!this.#eventsConfigured) {
      return this.#ok({ configured: false, events: [] });
    }
    const limit = boundedInt(
      request.query,
      "limit",
      DEFAULT_GET_EVENTS_LIMIT,
      DASHBOARD_EVENTS_MAX_LIMIT,
    );
    const order = single(request.query, "order") || DEFAULT_EVENT_ORDER;
    if (!isEventOrder(order)) {
      throw new ApiError("invalid_argument", "order debe ser 'asc' o 'desc'");
    }
    const types = request.query.type ?? [];
    const events = await this.#fleet.events({
      sandboxId: single(request.query, "sandboxId"),
      types: types.length > 0 ? types : undefined,
      limit,
      order,
    });
    return this.#ok({ configured: true, events: events.map(eventJson) });
  }

  async #logLines(request: ApiRequest): Promise<ApiResponse> {
    if (this.#logs === undefined) {
      throw new ApiError("not_found", "los logs no están disponibles en este panel");
    }
    const [sandboxId] = validatedSandboxIds([required(request.query, "sandboxId")]) as [string];
    const limit = boundedInt(
      request.query,
      "limit",
      DASHBOARD_LOGS_MAX_LINES,
      DASHBOARD_LOGS_MAX_LINES,
    );
    const page = await this.#logs.read(sandboxId, { limit });
    return this.#ok({
      sandboxId,
      truncated: page.truncated,
      fallback: page.fallback,
      lines: page.lines.map((line) => ({
        timestamp: line.timestamp,
        message: line.message,
        stream: line.stream,
      })),
    });
  }

  async #listJobs(): Promise<ApiResponse> {
    return this.#ok({ jobs: this.#jobs.snapshots() });
  }

  async #job(match: Match): Promise<ApiResponse> {
    const job = this.#jobs.get(match[1] as string);
    if (job === undefined) {
      throw new ApiError("not_found");
    }
    return this.#ok({ job });
  }

  async #plan(request: ApiRequest): Promise<ApiResponse> {
    const action = actionOf(required(request.query, "action"));
    const count = boundedInt(request.query, "count", MIN_QUERY_LIMIT, DASHBOARD_BULK_MAX_SANDBOXES);
    return this.#ok({ plan: planJson(await this.planFor(action, count)) });
  }

  async #submit(request: ApiRequest): Promise<ApiResponse> {
    this.#requireActions();
    const found = actionTargets(request);
    if (found === undefined) {
      throw new ApiError("not_found");
    }
    const appliedRate = await this.#appliedRate(found.action);
    const plan = this.#fleet.planBulk(found.action, found.sandboxIds.length, { appliedRate });
    try {
      const job = this.#jobs.submit(found.action, found.sandboxIds, { plan, appliedRate });
      return this.#ok({ job }, HTTP_ACCEPTED);
    } catch (error) {
      if (error instanceof JobsBusyError) {
        throw new ApiError("jobs_busy");
      }
      throw error;
    }
  }

  #requireActions(): void {
    if (!this.#fleet.allowActions) {
      throw new ApiError("actions_disabled");
    }
  }

  /**
   * La cuota de TPS aplicada de la operación, o `undefined` (el ritmo por
   * defecto) si Service Quotas no se puede leer: una acción no debe
   * bloquearse por un permiso de lectura que falte.
   */
  async #appliedRate(action: BulkAction): Promise<number | undefined> {
    try {
      return (await this.#quotaReport()).appliedRate(BULK_OPERATIONS[action]);
    } catch (error) {
      if (error instanceof SandboxError || error instanceof AuthenticationError) {
        return undefined;
      }
      throw error;
    }
  }
}

function single(query: Query, name: string): string | undefined {
  return query[name]?.[0];
}

function required(query: Query, name: string): string {
  const value = single(query, name);
  if (!value) {
    throw new ApiError("invalid_argument", `falta el parámetro ${name}`);
  }
  return value;
}

function flag(query: Query, name: string): boolean {
  const value = single(query, name);
  return value !== undefined && TRUTHY.has(value.toLowerCase());
}

function boundedInt(query: Query, name: string, fallback: number, maximum: number): number {
  const raw = single(query, name);
  if (raw === undefined) {
    return fallback;
  }
  if (!INTEGER_TEXT.test(raw)) {
    throw new ApiError("invalid_argument", `${name}: se esperaba un entero`);
  }
  const value = Number.parseInt(raw, DECIMAL_RADIX);
  if (value < MIN_QUERY_LIMIT || value > maximum) {
    throw new ApiError("invalid_argument", `${name}: entre ${MIN_QUERY_LIMIT} y ${maximum}`);
  }
  return value;
}

function isEventOrder(value: string): value is (typeof EVENT_ORDERS)[number] {
  return (EVENT_ORDERS as readonly string[]).includes(value);
}

function actionOf(value: unknown): BulkAction {
  if (!(ACTION_NAMES as readonly unknown[]).includes(value)) {
    throw new ApiError(
      "invalid_argument",
      `action: no es válida (admitidas: ${ACTION_NAMES.join(", ")})`,
    );
  }
  return value as BulkAction;
}

const UTF8 = new TextDecoder("utf-8", { fatal: true });

function jsonBody(request: ApiRequest): Record<string, unknown> {
  let parsed: unknown;
  try {
    const text = UTF8.decode(request.body);
    parsed = JSON.parse(text === "" ? EMPTY_BODY : text);
  } catch {
    throw new ApiError("invalid_json");
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new ApiError("invalid_json");
  }
  return parsed as Record<string, unknown>;
}
