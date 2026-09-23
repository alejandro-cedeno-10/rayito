/**
 * Núcleo puro del pool: el registro de plaza y su forma JSON (esquema
 * `rayito.pool/1`, intercambiable con el SDK Python), selección de la plaza
 * más vieja, reciclado, reconciliación con `list-microvms`, backoff del
 * relleno, estadísticas, el calendario `TakePoll` y el rechazo de opciones
 * de lanzamiento en `Sandbox.create({ pool })`. Sin I/O.
 */

import { InvalidArgumentError } from "../errors.js";
import { defineHidden } from "../hidden.js";
import { type IdlePolicy, type SandboxInfo, sandboxInfo } from "../models.js";
import { ReadinessPoll } from "../sandbox/readiness.js";

export const POOL_SCHEMA = "rayito.pool/1";
export const REDACTED = "<redacted>";

export type SlotState = "warming" | "ready";
export type ReconcileAction = "keep" | "repark" | "check" | "drop";

const LISTED_STATES_TO_DROP: ReadonlySet<string> = new Set(["TERMINATING", "TERMINATED"]);

/**
 * Todo lo que `take()` necesita de una plaza sin un `get-microvm`.
 * `accessToken` es el secreto de la plaza (uno por plaza, acuñado por el pool)
 * y no es enumerable: no sale al inspeccionar el registro.
 */
export interface SlotRecord {
  readonly sandboxId: string;
  readonly accessToken: string;
  readonly endpoint: string;
  readonly template: string;
  readonly templateVersion: string;
  readonly startedAt: Date;
  readonly maximumDurationSeconds: number;
  readonly region: string;
  readonly state: SlotState;
  readonly idle: IdlePolicy | undefined;
  readonly executionRoleArn: string | undefined;
  readonly ingress: readonly string[];
  readonly egress: readonly string[];
  readonly parkedAt: Date | undefined;
}

export function expiresAt(record: SlotRecord): Date {
  return new Date(record.startedAt.getTime() + record.maximumDurationSeconds * 1000);
}

export function remainingMs(record: SlotRecord, now: Date): number {
  return Math.max(0, expiresAt(record).getTime() - now.getTime());
}

/** Lo que se loguea de una plaza: nunca el token. */
export function describeRecord(record: SlotRecord): Record<string, unknown> {
  return {
    sandboxId: record.sandboxId,
    state: record.state,
    accessToken: REDACTED,
    expiresAt: expiresAt(record).toISOString(),
  };
}

export function recordFromInfo(
  info: SandboxInfo,
  fields: {
    readonly accessToken: string;
    readonly region: string;
    readonly state: SlotState;
    readonly parkedAt?: Date | undefined;
  },
): SlotRecord {
  const record: Omit<SlotRecord, "accessToken"> = {
    sandboxId: info.sandboxId,
    endpoint: info.endpoint,
    template: info.template,
    templateVersion: info.templateVersion,
    startedAt: info.startedAt,
    maximumDurationSeconds: info.maximumDurationSeconds,
    region: fields.region,
    state: fields.state,
    idle: info.idle,
    executionRoleArn: info.executionRoleArn,
    ingress: Object.freeze([]),
    egress: Object.freeze([]),
    parkedAt: fields.parkedAt,
  };
  return Object.freeze(defineHidden(record, "accessToken", fields.accessToken));
}

export function sandboxInfoFromRecord(record: SlotRecord, state = "SUSPENDED"): SandboxInfo {
  return sandboxInfo({
    sandboxId: record.sandboxId,
    state,
    endpoint: record.endpoint,
    template: record.template,
    templateVersion: record.templateVersion,
    startedAt: record.startedAt,
    maximumDurationSeconds: record.maximumDurationSeconds,
    idle: record.idle,
    executionRoleArn: record.executionRoleArn,
  });
}

/** Forma JSON de una plaza (claves en snake_case, fechas ISO-8601 UTC), la misma que escribe Python. */
export function recordToJson(record: SlotRecord): Record<string, unknown> {
  return {
    sandbox_id: record.sandboxId,
    access_token: record.accessToken,
    endpoint: record.endpoint,
    template: record.template,
    template_version: record.templateVersion,
    started_at: record.startedAt.toISOString(),
    maximum_duration_seconds: record.maximumDurationSeconds,
    region: record.region,
    state: record.state,
    idle:
      record.idle === undefined
        ? null
        : {
            max_idle_seconds: record.idle.maxIdleSeconds,
            suspended_duration_seconds: record.idle.suspendedDurationSeconds ?? null,
            auto_resume: record.idle.autoResume,
          },
    execution_role_arn: record.executionRoleArn ?? null,
    ingress: [...record.ingress],
    egress: [...record.egress],
    parked_at: record.parkedAt === undefined ? null : record.parkedAt.toISOString(),
  };
}

function field(data: Record<string, unknown>, key: string): unknown {
  const value = data[key];
  if (value === undefined) {
    throw new InvalidArgumentError(`registro de plaza inválido: falta ${key}`);
  }
  return value;
}

function stringField(data: Record<string, unknown>, key: string): string {
  return String(field(data, key));
}

function dateField(data: Record<string, unknown>, key: string): Date {
  const parsed = new Date(stringField(data, key));
  if (Number.isNaN(parsed.getTime())) {
    throw new InvalidArgumentError(`registro de plaza inválido: ${key} no es una fecha`);
  }
  return parsed;
}

function idleFromJson(value: unknown): IdlePolicy | undefined {
  if (value === null || value === undefined || typeof value !== "object") {
    return undefined;
  }
  const idle = value as Record<string, unknown>;
  const suspended = idle.suspended_duration_seconds;
  return Object.freeze({
    maxIdleSeconds: Number(idle.max_idle_seconds),
    suspendedDurationSeconds:
      suspended === null || suspended === undefined ? undefined : Number(suspended),
    autoResume: idle.auto_resume === undefined ? true : Boolean(idle.auto_resume),
  });
}

function stringList(value: unknown): readonly string[] {
  if (value === null || value === undefined) {
    return Object.freeze([]);
  }
  if (!Array.isArray(value)) {
    throw new InvalidArgumentError("registro de plaza inválido: ingress/egress no es una lista");
  }
  return Object.freeze(value.map((item) => String(item)));
}

export function recordFromJson(value: unknown): SlotRecord {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new InvalidArgumentError("registro de plaza inválido: no es un objeto");
  }
  const data = value as Record<string, unknown>;
  const state = stringField(data, "state");
  if (state !== "warming" && state !== "ready") {
    throw new InvalidArgumentError(`estado de plaza desconocido: ${JSON.stringify(state)}`);
  }
  const parked = data.parked_at;
  const role = data.execution_role_arn;
  const record: Omit<SlotRecord, "accessToken"> = {
    sandboxId: stringField(data, "sandbox_id"),
    endpoint: stringField(data, "endpoint"),
    template: stringField(data, "template"),
    templateVersion: stringField(data, "template_version"),
    startedAt: dateField(data, "started_at"),
    maximumDurationSeconds: Number(field(data, "maximum_duration_seconds")),
    region: stringField(data, "region"),
    state,
    idle: idleFromJson(data.idle),
    executionRoleArn: role === null || role === undefined ? undefined : String(role),
    ingress: stringList(data.ingress),
    egress: stringList(data.egress),
    parkedAt: parked === null || parked === undefined ? undefined : dateField(data, "parked_at"),
  };
  return Object.freeze(defineHidden(record, "accessToken", stringField(data, "access_token")));
}

/**
 * La plaza `ready` que caduca antes de entre las que aún tienen
 * `minRemainingMs` de vida (las más viejas primero); `undefined` si ninguna.
 */
export function pickReadySlot(
  records: Iterable<SlotRecord>,
  now: Date,
  minRemainingMs: number,
): SlotRecord | undefined {
  let best: SlotRecord | undefined;
  for (const record of records) {
    if (record.state !== "ready" || slotIsStale(record, now, minRemainingMs)) {
      continue;
    }
    if (best === undefined || compareByExpiry(record, best) < 0) {
      best = record;
    }
  }
  return best;
}

export function compareByExpiry(a: SlotRecord, b: SlotRecord): number {
  const delta = expiresAt(a).getTime() - expiresAt(b).getTime();
  if (delta !== 0) {
    return delta;
  }
  return a.sandboxId < b.sandboxId ? -1 : a.sandboxId > b.sandboxId ? 1 : 0;
}

export function slotIsStale(record: SlotRecord, now: Date, minRemainingMs: number): boolean {
  return remainingMs(record, now) < minRemainingMs;
}

/**
 * Qué hacer con una plaza `ready` según `list-microvms`: `keep` (suspendida
 * o arrancando), `repark` (alguien la reanudó), `check` (ausente: leer
 * `get-microvm`) o `drop` (listada terminal).
 */
export function reconcileAction(
  record: SlotRecord,
  listedState: string | undefined,
): ReconcileAction {
  if (record.state !== "ready") {
    return "keep";
  }
  if (listedState === undefined) {
    return "check";
  }
  if (listedState === "RUNNING") {
    return "repark";
  }
  if (LISTED_STATES_TO_DROP.has(listedState)) {
    return "drop";
  }
  return "keep";
}

/** Espera tras un calentamiento fallido: 1 s doblando hasta 60 s con ±25 % de jitter; un éxito la devuelve a 1 s. */
export class FillBackoff {
  static readonly initialDelayMs = 1000;
  static readonly maxDelayMs = 60_000;
  static readonly jitter = 0.25;

  readonly #random: () => number;
  #delayMs = FillBackoff.initialDelayMs;

  constructor(random: () => number = Math.random) {
    this.#random = random;
  }

  nextDelayMs(): number {
    const delay = this.#delayMs * (1 + FillBackoff.jitter * (2 * this.#random() - 1));
    this.#delayMs = Math.min(this.#delayMs * 2, FillBackoff.maxDelayMs);
    return Math.max(0, delay);
  }

  reset(): void {
    this.#delayMs = FillBackoff.initialDelayMs;
  }
}

/** Sondeo de `Health` tras un `resume-microvm` explícito: 100 ms doblando hasta 500 ms sin jitter. */
export class TakePoll extends ReadinessPoll {
  static override initialDelayMs = 100;
  static override maxDelayMs = 500;
  static override stateCheckIntervalMs = 5000;
  static override maxRpcTimeoutMs = 5000;
  static override minRpcTimeoutMs = 500;
  static override jitter = 0;

  protected override timing(): {
    initialDelayMs: number;
    maxDelayMs: number;
    stateCheckIntervalMs: number;
    maxRpcTimeoutMs: number;
    minRpcTimeoutMs: number;
    jitter: number;
  } {
    return TakePoll;
  }
}

/** Una plaza vista desde `stats()`: nunca lleva el token. */
export interface PoolSlotInfo {
  readonly sandboxId: string;
  readonly state: SlotState;
  readonly startedAt: Date;
  readonly expiresAt: Date;
  readonly parkedAt: Date | undefined;
}

/**
 * Contadores acumulados desde `start()` y las plazas actuales.
 * `takes === hits + misses`; `launched` cuenta cada `run-microvm` aceptado
 * (calentamientos y fallbacks), `recycled` los reciclados del sweeper,
 * `lost` las plazas perdidas (reconciliación, toma o recuperación) y
 * `failed` los calentamientos y tomas que lanzaron.
 */
export interface PoolStats {
  readonly size: number;
  readonly ready: number;
  readonly warming: number;
  readonly takes: number;
  readonly hits: number;
  readonly misses: number;
  readonly launched: number;
  readonly recycled: number;
  readonly lost: number;
  readonly failed: number;
  readonly slots: readonly PoolSlotInfo[];
}

export interface PoolCounters {
  takes: number;
  hits: number;
  misses: number;
  launched: number;
  recycled: number;
  lost: number;
  failed: number;
}

export function newCounters(): PoolCounters {
  return { takes: 0, hits: 0, misses: 0, launched: 0, recycled: 0, lost: 0, failed: 0 };
}

export function slotInfoFromRecord(record: SlotRecord): PoolSlotInfo {
  return Object.freeze({
    sandboxId: record.sandboxId,
    state: record.state,
    startedAt: record.startedAt,
    expiresAt: expiresAt(record),
    parkedAt: record.parkedAt,
  });
}

export function statsFromRecords(
  records: Iterable<SlotRecord>,
  fields: { readonly size: number; readonly warming: number; readonly counters: PoolCounters },
): PoolStats {
  const ordered = [...records].sort(compareByExpiry);
  return Object.freeze({
    size: fields.size,
    ready: ordered.filter((record) => record.state === "ready").length,
    warming: fields.warming,
    takes: fields.counters.takes,
    hits: fields.counters.hits,
    misses: fields.counters.misses,
    launched: fields.counters.launched,
    recycled: fields.counters.recycled,
    lost: fields.counters.lost,
    failed: fields.counters.failed,
    slots: Object.freeze(ordered.map(slotInfoFromRecord)),
  });
}

/** Las opciones de lanzamiento o de plano que `create({ pool })` rechaza, en el orden de la firma. */
export const POOL_REJECTED_OPTIONS: readonly string[] = Object.freeze([
  "template",
  "templateVersion",
  "timeoutMs",
  "maxLifetimeMs",
  "onTimeout",
  "idle",
  "envs",
  "metadata",
  "cpuTimeLimit",
  "executionRoleArn",
  "allowedPorts",
  "ingress",
  "egress",
  "logging",
  "region",
  "client",
  "accessToken",
  "keepOnFailure",
  "controlPlane",
  "transport",
]);

/** La primera opción rechazada que viene definida junto a `pool`; `undefined` si ninguna. */
export function rejectedPoolOption(options: Readonly<Record<string, unknown>>): string | undefined {
  return POOL_REJECTED_OPTIONS.find((name) => options[name] !== undefined);
}

export function rejectLaunchOptionsWithPool(options: object): void {
  const offending = rejectedPoolOption(options as Readonly<Record<string, unknown>>);
  if (offending !== undefined) {
    throw new InvalidArgumentError(
      `create({ pool }) no admite \`${offending}\`: la configuración de lanzamiento es la del ` +
        "PoolConfig del pool; sólo pasan readyTimeoutMs, requestTimeoutMs, reconnectTimeoutMs y logger",
    );
  }
}
