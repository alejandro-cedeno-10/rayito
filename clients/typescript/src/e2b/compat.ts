/**
 * El mapeo puro de `rayito/e2b` sobre el SDK nativo, espejo camelCase de
 * `rayito/e2b/_compat.py` y `_connection.py`: las opciones de E2B JS 2.51 se
 * traducen a las nativas o se rechazan antes de tocar AWS o el agente. Sin
 * I/O salvo `emitCompatWarning`, que sólo llama a `process.emitWarning`.
 */

import { Code } from "@connectrpc/connect";
import {
  InvalidArgumentError,
  SandboxError,
  SandboxNotFoundError,
  UnimplementedError,
} from "../errors.js";
import { PORT_MAX, PORT_MIN, SUSPENDED_STATES, TERMINAL_STATES } from "../limits.js";
import {
  ALL_TRAFFIC,
  type IdlePolicyInput,
  type SandboxInfo as NativeSandboxInfo,
  type SandboxMetrics as NativeSandboxMetrics,
  type NetworkPolicyInput,
  type NetworkState,
  type SandboxListItem,
} from "../models.js";
import { DEFAULT_LANGUAGE, normalizeLanguage } from "../sandbox/code.js";
import { CAP_MARGIN_MS, MAX_LIFETIME_MS } from "../sandbox/lifecycle.js";
import type { SandboxCreateOptions, SandboxPaginateOptions } from "../sandbox/sandbox.js";
import { validateExtraHeaders } from "../transport/headers.js";
import { validateProxyUrl } from "../transport/proxy-tunnel.js";
import type { TransportSettings } from "../transport/transport.js";
import { validateIntegration, validateRetries } from "../validation.js";
import { ConnectionConfig, type ConnectionOpts } from "./connection.js";
import type {
  SandboxInfo,
  SandboxLifecycle,
  SandboxListOpts,
  SandboxMetrics,
  SandboxNetworkOpts,
  SandboxNetworkUpdate,
  SandboxOnResume,
  SandboxOpts,
  SandboxState,
} from "./types.js";
import { COMPAT_DOC_PATH, unimplemented } from "./unimplemented.js";

export const COMPAT_WARNING_TYPE = "RayitoCompatWarning";
/** El `timeoutMs` de E2B cuando falta: 5 minutos. */
export const E2B_DEFAULT_TIMEOUT_MS = 300_000;
/** El tope de plataforma mínimo del shim: una hora aunque el plazo lógico sea corto. */
export const E2B_DEFAULT_MAX_LIFETIME_MS = 3_600_000;
/** `network.httpsPorts` no vacío sigue la medición QE2 (el `HTTPS_PORTS_SUPPORTED` de Python). */
export const HTTPS_PORTS_SUPPORTED = false;
export const HTTPS_PORTS_REASON =
  "el proxy de Lambda MicroVMs no reenvía TLS extremo a extremo a un puerto del guest " +
  "(medido, fila 67 QE2 de AWS_API_NOTES.md §16); getHost(puerto) sirve HTTP en claro";
/** La `idle` de la plataforma en modo `pause`, como `IdlePolicy(max_idle_seconds=300)` en Python. */
export const PAUSE_MAX_IDLE_SECONDS = 300;
export const SHIM_DEFAULT_INGRESS: readonly string[] = Object.freeze(["ALL_INGRESS"]);
const INTERNET_EGRESS = "INTERNET_EGRESS";
export const SHIM_EGRESS: readonly string[] = Object.freeze([INTERNET_EGRESS]);

export const AVAILABLE_KERNELS_REASON =
  "kernels disponibles: python en toda imagen; bash, javascript y typescript en la " +
  "variante rayito-base-poly; R y Java no (SPEC.md §4)";
export const POLY_KERNELS_REASON =
  "este kernel sólo existe en la variante rayito-base-poly (publícala con make " +
  "image-publish-poly y úsala como template)";

const AUTH_REASON = "Rayito autentica con tus credenciales de AWS y el access token del sandbox";
const ENDPOINT_REASON = "el endpoint lo da run-microvm; no hay dominio ni API de E2B";

/** Las opciones de conexión de E2B que no tienen efecto en Rayito, con el motivo del aviso. */
export const IGNORED_CONNECTION_OPTS: Readonly<Record<string, string>> = Object.freeze({
  apiKey: AUTH_REASON,
  validateApiKey: AUTH_REASON,
  apiHeaders: AUTH_REASON,
  domain: ENDPOINT_REASON,
  apiUrl: ENDPOINT_REASON,
  sandboxUrl: ENDPOINT_REASON,
  debug: "no hay un modo de depuración contra un servidor local de E2B",
});
export const SECURE_FALSE_REASON =
  "el endpoint siempre exige X-aws-proxy-auth y el access token; secure: false no lo relaja";

/** Un aviso por opción ignorada; nombra la opción y nunca su valor. */
export function emitCompatWarning(name: string, reason: string): void {
  process.emitWarning(`${name} ignorado: ${reason}`, { type: COMPAT_WARNING_TYPE });
}

/** Emite los avisos de una lista de opciones ignoradas (`IGNORED_CONNECTION_OPTS` o `secure`). */
export function emitIgnoredWarnings(ignored: readonly string[]): void {
  for (const name of ignored) {
    emitCompatWarning(name, IGNORED_CONNECTION_OPTS[name] ?? SECURE_FALSE_REASON);
  }
}

/** Las opciones nativas de conexión que salen de un `ConnectionOpts` de E2B. */
export interface NativeConnection {
  readonly region?: string | undefined;
  readonly controlPlane?: SandboxCreateOptions["controlPlane"];
  readonly accessToken?: string | undefined;
  readonly requestTimeoutMs?: number | undefined;
  readonly logger?: SandboxCreateOptions["logger"];
  readonly signal?: AbortSignal | undefined;
  readonly transport?: Partial<TransportSettings> | undefined;
  readonly retries?: number | undefined;
  readonly proxy?: string | undefined;
  readonly integration?: string | undefined;
}

export interface SplitConnection {
  readonly connection: NativeConnection;
  /** Las claves de `IGNORED_CONNECTION_OPTS` presentes, en orden alfabético. */
  readonly ignored: readonly string[];
}

function defined<T extends object>(entries: T): Partial<T> {
  return Object.fromEntries(
    Object.entries(entries).filter(([, value]) => value !== undefined),
  ) as Partial<T>;
}

/**
 * Separa lo que aplica (`requestTimeoutMs`, `retries`, `logger`, `headers`,
 * `proxy`, `signal` y los enlaces de Rayito) de lo que sólo avisa. `headers`
 * pasan a `transport.extraHeaders`; `proxy` va al transporte y al plano de
 * control; `ConnectionConfig.setIntegration` va al plano. Todo se valida aquí.
 */
export function splitConnectionOpts(opts: ConnectionOpts = {}): SplitConnection {
  const ignored = Object.keys(IGNORED_CONNECTION_OPTS)
    .filter((name) => (opts as Record<string, unknown>)[name] !== undefined)
    .sort();
  const headers = validateExtraHeaders(opts.headers);
  const proxy = validateProxyUrl(opts.proxy);
  const retries = validateRetries(opts.retries);
  const integration = validateIntegration(ConnectionConfig.integration);
  const transport =
    headers === undefined && proxy === undefined
      ? opts.transport
      : { ...(opts.transport ?? {}), ...defined({ extraHeaders: headers, proxy }) };
  return {
    connection: defined({
      region: opts.region,
      controlPlane: opts.controlPlane,
      accessToken: opts.accessToken,
      requestTimeoutMs: opts.requestTimeoutMs,
      logger: opts.logger,
      signal: opts.signal,
      transport,
      retries,
      proxy,
      integration,
    }),
    ignored,
  };
}

/** La regla de `merge_api_params` de E2B: gana lo de la llamada salvo `undefined`; `headers` no se fusiona. */
export function mergeBoundOpts<T extends ConnectionOpts>(bound: ConnectionOpts, call: T): T {
  return { ...bound, ...defined(call) } as T;
}

/** `onResume`: `"restore"` (o ausente) sigue; `"reboot"` es `UnimplementedError`. */
export function validateOnResume(onResume: SandboxOnResume | undefined): void {
  if (onResume === undefined || onResume === "restore") {
    return;
  }
  if (onResume === "reboot") {
    throw unimplemented("connect({ onResume: 'reboot' })");
  }
  throw new InvalidArgumentError(
    `onResume debe ser 'restore' o 'reboot', recibido ${JSON.stringify(onResume)}`,
  );
}

/** `keepMemory: false` es `UnimplementedError`; `true` o ausente, la pausa de siempre. */
export function validateKeepMemory(keepMemory: boolean | undefined): void {
  if (keepMemory === false) {
    throw unimplemented("pause({ keepMemory: false })");
  }
}

export interface ShimLifecycle {
  readonly onTimeout: "kill" | "pause";
  readonly autoResume: boolean;
}

const LIFECYCLE_KEYS: ReadonlySet<string> = new Set(["onTimeout", "autoResume"]);

function onTimeoutAction(onTimeout: unknown): "kill" | "pause" {
  if (onTimeout === undefined) {
    return "kill";
  }
  if (onTimeout === "kill" || onTimeout === "pause") {
    return onTimeout;
  }
  if (typeof onTimeout !== "object" || onTimeout === null || Array.isArray(onTimeout)) {
    throw new InvalidArgumentError(
      `lifecycle.onTimeout debe ser 'pause' o 'kill', recibido ${JSON.stringify(onTimeout)}`,
    );
  }
  const { action, keepMemory } = onTimeout as { action?: unknown; keepMemory?: unknown };
  if (action !== "kill" && action !== "pause") {
    throw new InvalidArgumentError(
      `lifecycle.onTimeout.action debe ser 'pause' o 'kill', recibido ${JSON.stringify(action)}`,
    );
  }
  if (keepMemory !== undefined && action === "kill") {
    throw new InvalidArgumentError("lifecycle.onTimeout.keepMemory sólo vale con action 'pause'");
  }
  if (keepMemory === false) {
    throw unimplemented("lifecycle.onTimeout.keepMemory=false");
  }
  return action;
}

/** La validación de E2B de `lifecycle`, espejo de `map_lifecycle` (m9-server-timeout). */
export function mapLifecycle(lifecycle: SandboxLifecycle | undefined): ShimLifecycle {
  if (lifecycle === undefined) {
    return { onTimeout: "kill", autoResume: false };
  }
  if (typeof lifecycle !== "object" || lifecycle === null || Array.isArray(lifecycle)) {
    throw new InvalidArgumentError("lifecycle debe ser un objeto con onTimeout y autoResume");
  }
  for (const key of Object.keys(lifecycle)) {
    if (!LIFECYCLE_KEYS.has(key)) {
      throw new InvalidArgumentError(`lifecycle: clave desconocida '${key}'`);
    }
  }
  const onTimeout = onTimeoutAction(lifecycle.onTimeout);
  const autoResume = lifecycle.autoResume ?? false;
  if (typeof autoResume !== "boolean") {
    throw new InvalidArgumentError("lifecycle.autoResume debe ser un boolean");
  }
  if (autoResume && onTimeout !== "pause") {
    throw new InvalidArgumentError("autoResume sólo puede ser true con onTimeout 'pause'");
  }
  return { onTimeout, autoResume };
}

/**
 * El `maxLifetimeMs` del shim cuando falta, en ms:
 * `max(3 600 000, min(timeoutMs + 60 000, 28 800 000))` con `timeoutMs`
 * redondeado a segundos. No es el `defaultMaxLifetimeMs(timeoutSeconds)`
 * nativo, que parte de segundos y no tiene el suelo de una hora de E2B.
 */
export function e2bDefaultMaxLifetimeMs(timeoutMs: number): number {
  const roundedMs = Math.ceil(timeoutMs / 1000) * 1000;
  return Math.max(
    E2B_DEFAULT_MAX_LIFETIME_MS,
    Math.min(roundedMs + CAP_MARGIN_MS, MAX_LIFETIME_MS),
  );
}

const CREATE_NETWORK_KEYS: ReadonlySet<string> = new Set([
  "allowOut",
  "denyOut",
  "egressProxy",
  "rules",
  "maskRequestHost",
  "allowPublicTraffic",
  "httpsPorts",
]);
const UPDATE_NETWORK_KEYS: ReadonlySet<string> = new Set([
  "allowOut",
  "denyOut",
  "egressProxy",
  "rules",
  "allowInternetAccess",
]);

function networkRecord(network: unknown, field: string): Record<string, unknown> {
  if (typeof network !== "object" || network === null || Array.isArray(network)) {
    throw new InvalidArgumentError(`${field} debe ser un objeto`);
  }
  return network as Record<string, unknown>;
}

function rejectUnknownNetworkKeys(
  network: Record<string, unknown>,
  known: ReadonlySet<string>,
): void {
  for (const key of Object.keys(network)) {
    if (!known.has(key)) {
      throw new InvalidArgumentError(`network: clave desconocida '${key}'`);
    }
  }
}

function validateHttpsPorts(ports: unknown): void {
  if (ports === undefined) {
    return;
  }
  const valid =
    Array.isArray(ports) &&
    ports.every((port) => Number.isInteger(port) && port >= PORT_MIN && port <= PORT_MAX);
  if (!valid) {
    throw new InvalidArgumentError("network.httpsPorts debe ser una lista de puertos 1–65535");
  }
  if ((ports as readonly unknown[]).length > 0 && !HTTPS_PORTS_SUPPORTED) {
    throw new UnimplementedError("network.httpsPorts", HTTPS_PORTS_REASON, COMPAT_DOC_PATH);
  }
}

function nativePolicy(network: Record<string, unknown>): NetworkPolicyInput {
  return defined({
    allowOut: network.allowOut,
    denyOut: network.denyOut,
    egressProxy: network.egressProxy,
  }) as NetworkPolicyInput;
}

/**
 * `network` de `create`: `rules`, `maskRequestHost` y `allowPublicTraffic:
 * true` son `UnimplementedError`; `allowPublicTraffic: false` es el
 * comportamiento permanente y `httpsPorts` se valida y, no vacío, sigue la
 * medición QE2 (`UnimplementedError` mientras `HTTPS_PORTS_SUPPORTED` sea
 * `false`, como en Python). Otra clave es `InvalidArgumentError`.
 */
export function rejectUnsupportedNetworkKeys(
  network: SandboxNetworkOpts | undefined,
): NetworkPolicyInput | undefined {
  if (network === undefined) {
    return undefined;
  }
  const record = networkRecord(network, "network");
  rejectUnknownNetworkKeys(record, CREATE_NETWORK_KEYS);
  if (record.rules !== undefined) {
    throw unimplemented("network.rules");
  }
  if (record.maskRequestHost !== undefined) {
    throw unimplemented("network.maskRequestHost");
  }
  if (record.allowPublicTraffic === true) {
    throw unimplemented("network.allowPublicTraffic=true");
  }
  if (record.allowPublicTraffic !== undefined && record.allowPublicTraffic !== false) {
    throw new InvalidArgumentError("network.allowPublicTraffic debe ser un boolean");
  }
  validateHttpsPorts(record.httpsPorts);
  return nativePolicy(record);
}

export interface NetworkUpdateMapping {
  readonly policy: NetworkPolicyInput;
  readonly allowInternetAccess: boolean | undefined;
}

/** `updateNetwork` de E2B: `rules` es `UnimplementedError`; lo omitido se borra, como en E2B. */
export function mapNetworkUpdate(network: SandboxNetworkUpdate | undefined): NetworkUpdateMapping {
  if (network === undefined) {
    return { policy: {}, allowInternetAccess: undefined };
  }
  const record = networkRecord(network, "network");
  rejectUnknownNetworkKeys(record, UPDATE_NETWORK_KEYS);
  if (record.rules !== undefined) {
    throw unimplemented("network.rules");
  }
  return {
    policy: nativePolicy(record),
    allowInternetAccess: network.allowInternetAccess,
  };
}

export interface CreateMapping {
  readonly native: SandboxCreateOptions;
  readonly ignored: readonly string[];
}

function rejectUnsupportedCreateOpts(opts: SandboxOpts): void {
  if (opts.mcp !== undefined) {
    throw unimplemented("mcp");
  }
  if (opts.iam !== undefined) {
    throw unimplemented("iam");
  }
  if (opts.volumeMounts !== undefined) {
    throw unimplemented("volumeMounts");
  }
}

function lifecycleIdle(lifecycle: ShimLifecycle): IdlePolicyInput | null {
  if (lifecycle.onTimeout === "kill") {
    return null;
  }
  return { maxIdleSeconds: PAUSE_MAX_IDLE_SECONDS, autoResume: lifecycle.autoResume };
}

/**
 * `Sandbox.create(opts)` y `Sandbox.create(template, opts)`: la tabla D5 en
 * camelCase. `timeoutMs` es el plazo lógico de `rayd` (300 000 si falta) y
 * toda creación del shim manda el bloque `lifecycle`, así una imagen anterior
 * a M9 falla en vez de ignorar el plazo. `ingress` es `ALL_INGRESS` salvo que
 * se pase y `egress` siempre `INTERNET_EGRESS` (la política va en el guest).
 */
export function mapCreateOptions(
  templateOrOpts?: string | SandboxOpts,
  maybeOpts?: SandboxOpts,
): CreateMapping {
  const opts: SandboxOpts =
    typeof templateOrOpts === "string" ? (maybeOpts ?? {}) : (templateOrOpts ?? {});
  const template = typeof templateOrOpts === "string" ? templateOrOpts : opts.template;
  rejectUnsupportedCreateOpts(opts);
  const network = rejectUnsupportedNetworkKeys(opts.network);
  const lifecycle = mapLifecycle(opts.lifecycle);
  const { connection, ignored } = splitConnectionOpts(opts);
  const timeoutMs = opts.timeoutMs ?? E2B_DEFAULT_TIMEOUT_MS;
  const native: SandboxCreateOptions = {
    ...connection,
    ...defined({
      template,
      templateVersion: opts.templateVersion,
      metadata: opts.metadata,
      envs: opts.envs,
      executionRoleArn: opts.executionRoleArn,
      allowedPorts: opts.allowedPorts,
      logging: opts.logging,
      readyTimeoutMs: opts.readyTimeoutMs,
      reconnectTimeoutMs: opts.reconnectTimeoutMs,
      keepOnFailure: opts.keepOnFailure,
      allowInternetAccess: opts.allowInternetAccess,
      network,
    }),
    timeoutMs,
    maxLifetimeMs: opts.maxLifetimeMs ?? e2bDefaultMaxLifetimeMs(timeoutMs),
    onTimeout: lifecycle.onTimeout,
    idle: lifecycleIdle(lifecycle),
    ingress: opts.ingress ?? SHIM_DEFAULT_INGRESS,
    egress: SHIM_EGRESS,
  };
  const secureIgnored = opts.secure === false ? ["secure"] : [];
  return { native, ignored: [...ignored, ...secureIgnored] };
}

const E2B_STATE_FILTERS: Readonly<Record<SandboxState, readonly string[]>> = Object.freeze({
  running: Object.freeze(["PENDING", "RUNNING"]),
  paused: Object.freeze(["SUSPENDING", "SUSPENDED"]),
});

export const LIST_METADATA_STATE_REASON =
  "los metadatos viven en el agente; leerlos despertaría el sandbox";
export const LIST_METADATA_STATE_FEATURE = "list(query.state=paused, query.metadata)";

/**
 * `list({ query, order, limit, nextToken })` de E2B como opciones de
 * `Sandbox.paginate()`. `query.metadata` sólo filtra sandboxes `RUNNING`: con
 * `state: ["running"]` viaja `RUNNING`, y con cualquier otro estado es
 * `UnimplementedError` antes de tocar AWS, como en Python, porque leer los
 * metadatos despertaría el sandbox.
 */
export function mapListOptions(opts: SandboxListOpts = {}): SandboxPaginateOptions {
  const { connection } = splitConnectionOpts(opts);
  const query = opts.query ?? {};
  const states =
    query.state === undefined
      ? undefined
      : query.state.flatMap((state) => E2B_STATE_FILTERS[state] ?? invalidState(state));
  const runningOnly =
    query.state !== undefined &&
    query.state.length > 0 &&
    query.state.every((s) => s === "running");
  if (query.metadata !== undefined && query.state !== undefined && !runningOnly) {
    throw new UnimplementedError(
      LIST_METADATA_STATE_FEATURE,
      LIST_METADATA_STATE_REASON,
      COMPAT_DOC_PATH,
    );
  }
  const narrowed = query.metadata !== undefined && runningOnly ? ["RUNNING"] : states;
  return {
    ...connection,
    ...defined({
      metadata: query.metadata,
      states: narrowed,
      startedAfter: query.startedAfter,
      template: query.template,
      order: opts.order,
      limit: opts.limit,
      nextToken: opts.nextToken,
    }),
  };
}

function invalidState(state: unknown): never {
  throw new InvalidArgumentError(
    `query.state admite 'running' y 'paused', recibido ${JSON.stringify(state)}`,
  );
}

/** `PENDING|RUNNING` → `running`, `SUSPENDING|SUSPENDED` → `paused`; terminado es `SandboxNotFoundError`. */
export function stateFromNative(state: string, sandboxId: string): SandboxState {
  if (TERMINAL_STATES.has(state)) {
    throw new SandboxNotFoundError(`el sandbox ${sandboxId} está ${state}`);
  }
  return SUSPENDED_STATES.has(state) ? "paused" : "running";
}

function isNativeInfo(info: NativeSandboxInfo | SandboxListItem): info is NativeSandboxInfo {
  return "endpoint" in info;
}

function hasInternetConnector(egress: readonly string[]): boolean {
  return egress.some((connector) => connector.split(":").at(-1) === INTERNET_EGRESS);
}

/**
 * `false` sólo con una política leída que lo deniega todo; `true` con una
 * leída que no, o sin política en el guest y con `INTERNET_EGRESS`;
 * `undefined` si no se leyó (el `internet_access_from` de Python).
 */
function internetAccessFrom(
  network: NetworkState | undefined,
  networkRead: boolean,
  egress: readonly string[],
): boolean | undefined {
  if (!networkRead) {
    return undefined;
  }
  if (network !== undefined) {
    return !(network.denyOut.includes(ALL_TRAFFIC) && network.allowOut.length === 0);
  }
  return hasInternetConnector(egress) ? true : undefined;
}

/**
 * Lo que `infoFromNative` no encuentra en la `SandboxInfo` nativa: la
 * política leída del guest. `networkRead` dice que se intentó `GetNetwork`
 * (`network` ausente con `networkRead` = imagen sin política en el guest).
 * Los metadatos vienen de la propia `SandboxInfo` (los del último `Health`).
 */
export interface InfoExtras {
  readonly network?: NetworkState | undefined;
  readonly networkRead?: boolean | undefined;
}

/**
 * El `SandboxInfo` de E2B 2.51 (D10): `endAt` es el plazo lógico (`expiresAt`),
 * `cpuCount`/`memoryMB`/`envdVersion` lo leído de `Health`, `lifecycle` sólo
 * si `rayd` lo gestiona, `network` con la política leída del guest (sea cual
 * sea su `enforcement`), `allowInternetAccess` según `internetAccessFrom`, y
 * `volumeMounts` siempre vacío.
 */
export function infoFromNative(
  info: NativeSandboxInfo | SandboxListItem,
  extras: InfoExtras = {},
): SandboxInfo {
  const state = stateFromNative(info.state, info.sandboxId);
  const metadata = Object.freeze({ ...(info.metadata ?? {}) });
  const network = extras.network;
  const base = {
    sandboxId: info.sandboxId,
    templateId: info.template,
    name: info.templateName,
    metadata,
    startedAt: info.startedAt,
    state,
    network:
      network === undefined
        ? undefined
        : Object.freeze({ allowOut: [...network.allowOut], denyOut: [...network.denyOut] }),
    allowInternetAccess: isNativeInfo(info)
      ? internetAccessFrom(network, extras.networkRead ?? false, info.egress)
      : undefined,
    volumeMounts: Object.freeze([]),
  };
  if (!isNativeInfo(info)) {
    return Object.freeze({
      ...base,
      endAt: undefined,
      cpuCount: undefined,
      memoryMB: undefined,
      envdVersion: undefined,
      lifecycle: undefined,
      sandboxDomain: undefined,
    });
  }
  const managed = info.lifecycle !== undefined && info.lifecycle.phase !== "unmanaged";
  return Object.freeze({
    ...base,
    endAt: info.expiresAt,
    cpuCount: info.cpuCount,
    memoryMB: info.memoryMb,
    envdVersion: info.agentVersion,
    lifecycle:
      managed && info.lifecycle !== undefined
        ? Object.freeze({
            onTimeout: info.lifecycle.onTimeout ?? "kill",
            autoResume: info.lifecycle.autoResume,
          })
        : undefined,
    sandboxDomain: info.endpoint,
  });
}

/** `SandboxMetrics` de E2B: bytes con los nombres de E2B (`memUsed`, `memCache`...). */
export function metricsFromNative(metrics: NativeSandboxMetrics): SandboxMetrics {
  return Object.freeze({
    timestamp: metrics.timestamp,
    cpuUsedPct: metrics.cpuUsedPct,
    cpuCount: metrics.cpuCount,
    memUsed: metrics.memUsedBytes,
    memTotal: metrics.memTotalBytes,
    memCache: metrics.memCacheBytes,
    diskUsed: metrics.diskUsedBytes,
    diskTotal: metrics.diskTotalBytes,
  });
}

function languageFeature(feature: string, language: unknown): string {
  return `${feature}({ language: ${JSON.stringify(language)} })`;
}

/**
 * El nombre canónico que el SDK nativo entiende (`bash`, `javascript`,
 * `typescript`; `js` y `ts` son alias); `undefined` y `python` (sin
 * distinguir mayúsculas) son el kernel por defecto y viajan como
 * `undefined`, así una celda Python no lleva `language`. Cualquier otro
 * kernel de E2B (`r`, `java`...) es `UnimplementedError` antes de tocar el
 * agente, con el `InvalidArgumentError` nativo en `cause`. Que la imagen
 * tenga el kernel lo decide el agente (`unimplementedLanguage`).
 */
export function normalizedLanguageOrUnimplemented(
  language: string | undefined,
  feature: string,
): string | undefined {
  if (language === undefined) {
    return undefined;
  }
  let canonical: string | undefined;
  try {
    canonical = normalizeLanguage(language);
  } catch (error) {
    if (!(error instanceof InvalidArgumentError)) {
      throw error;
    }
    throw new UnimplementedError(
      languageFeature(feature, language),
      AVAILABLE_KERNELS_REASON,
      COMPAT_DOC_PATH,
      { cause: error },
    );
  }
  return canonical === DEFAULT_LANGUAGE ? undefined : canonical;
}

/**
 * El `UnimplementedError` del shim para un agente que respondió
 * `Unimplemented` a un kernel que la imagen no trae, con el error nativo en
 * `cause`; `undefined` para cualquier otro error, que el shim propaga sin
 * tocar (p. ej. el `InvalidArgument` de un agente anterior a M9 que no
 * conoce `typescript`).
 */
export function unimplementedLanguage(
  error: unknown,
  feature: string,
  language: string | undefined,
): UnimplementedError | undefined {
  if (!(error instanceof SandboxError) || error.grpcCode !== Code.Unimplemented) {
    return undefined;
  }
  return new UnimplementedError(
    languageFeature(feature, language),
    POLY_KERNELS_REASON,
    COMPAT_DOC_PATH,
    { cause: error },
  );
}

/**
 * Un callback de E2B, que puede ser asíncrono, con la forma síncrona que
 * espera el SDK nativo: su rechazo va a `onRejected` en vez de quedar como
 * una promesa rechazada sin manejar, y un `throw` síncrono sigue llegando al
 * nativo, que decide como con cualquier callback.
 */
export function settleAsyncCallback<A>(
  callback: (arg: A) => void | Promise<void>,
  onRejected: (error: unknown) => void,
): (arg: A) => void {
  return (arg: A): void => {
    void Promise.resolve(callback(arg)).catch(onRejected);
  };
}
