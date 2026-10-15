/**
 * Dominio puro del panel de gestión (ADR-026, cambio OpenSpec `dashboard`):
 * cuotas de Lambda MicroVMs con sus avisos, coste facturado y estimado,
 * vistas de sandboxes e imágenes y planificación de acciones masivas. Nada
 * aquí importa `@aws-sdk/*`, una librería HTTP ni el código generado del
 * proto: los adaptadores traducen a estos modelos (`aws.ts`) y los
 * servicios los componen (`quotas.ts`, `cost.ts`, `fleet.ts`). Espejo de
 * `rayito._dashboard._domain` (Python): ambos pasan los vectores de
 * `testdata/dashboard/`.
 */

import { InvalidArgumentError } from "../errors.js";
import {
  API_TPS,
  BASELINE_MIB_PER_VCPU,
  COMPUTE_PRICE_USD_PER_GB_SECOND,
  COMPUTE_PRICE_USD_PER_VCPU_SECOND,
  COST_CACHE_MAX_ENTRIES,
  LARGE_QUOTA_REGIONS,
  QUOTA_CRITICAL_FRACTION,
  QUOTA_DEFAULTS,
  QUOTA_KINDS,
  QUOTA_LARGE_REGION_DEFAULTS,
  QUOTA_NAMES,
  QUOTA_OPERATIONS,
  QUOTA_SERVICE_CODE,
  QUOTA_WARNING_FRACTION,
  SUSPENDED_STATES,
} from "../limits.js";

export type QuotaLevel = "ok" | "warning" | "critical";
export type WarningKind = "memory_high" | "memory_critical" | "below_default" | "not_listed";
export type BulkAction = "kill" | "suspend" | "resume";
export type BulkOutcome = "done" | "unchanged" | "failed";
export type Granularity = "DAILY" | "MONTHLY";

export const RUNNING_STATE = "RUNNING";
/**
 * Código de un fallo masivo que no trae código de AWS; igual en Python y
 * TypeScript para que la API JSON dé el mismo valor en las dos fases.
 */
export const UNKNOWN_FAILURE_CODE = "UnknownError";
export const QUOTA_EXCEEDED_CODE = "ServiceQuotaExceededException";
export const CAPACITY_CODE = "InsufficientCapacityException";
/**
 * Estados cuya memoria cuenta contra la cuota por región: `RUNNING` más los
 * suspendidos (`SUSPENDING` y `SUSPENDED`), `AWS_API_NOTES.md` §11.
 */
export const MEMORY_COUNTED_STATES: readonly string[] = [RUNNING_STATE, ...SUSPENDED_STATES];
export const MIB_PER_GB = 1024;
/** Milisegundos en un segundo, para convertir marcas de `Date`. */
export const MS_PER_SECOND = 1000;
export const PERCENT = 100;

export const MEMORY_KIND = "memory-gb";
export const RATE_KIND = "rate";
export const BURST_KIND = "burst";

/**
 * Formato de la URL de la consola de Service Quotas para pedir un aumento
 * (no es un parámetro de la API; sólo la partición `aws`). Pendiente de
 * confirmar en la aceptación de AWS (tarea 1.1 del cambio `dashboard`).
 */
const INCREASE_URL_TEMPLATE =
  "https://{region}.console.aws.amazon.com/servicequotas/home/services/{service}/quotas/{code}";

/**
 * Cada `BulkAction` y la operación de AWS cuya cuota de TPS la limita
 * (`AWS_API_NOTES.md` §5, `limits.json` `apiTps`).
 */
export const BULK_OPERATIONS: Readonly<Record<BulkAction, string>> = {
  kill: "TerminateMicrovm",
  suspend: "SuspendMicrovm",
  resume: "ResumeMicrovm",
};

export const BILLED_LABEL = "billed";
export const ESTIMATED_LABEL = "estimated";
export const ESTIMATE_BASIS =
  "estimación: tiempo transcurrido desde el arranque de cada sandbox RUNNING por el precio de cómputo baseline; no incluye snapshots ni consumo sobre el baseline y no sustituye a la factura";
/** Región de los precios documentados que usa la estimación (`AWS_API_NOTES.md` §12). */
export const PRICING_REGION = "us-east-1";

/**
 * Sufijo del usage type (`Lambda-MicroVM-<sufijo>`) de Cost Explorer y el
 * componente que factura, verificado el 2026-09-16 (`AWS_API_NOTES.md` §12).
 */
export const COMPONENT_BY_USAGE_SUFFIX: Readonly<Record<string, string>> = {
  "Memory-GB-Second-ARM": "memory",
  "vCPU-Second-ARM": "vcpu",
  "Snapshot-Read-GB": "snapshot-read",
  "Snapshot-Write-GB": "snapshot-write",
  "Snapshot-Storage-GB-Hour": "snapshot-storage",
};
export const OTHER_COMPONENT = "other";
/**
 * Los usage types de otras regiones llevan un prefijo (`EUW1-Lambda-…`); sin
 * medir todavía fuera de us-east-1 (tarea 1.1).
 */
const MICROVM_USAGE_TYPE_PATTERN = /^(?:[A-Z0-9]+-)?Lambda-MicroVM-(.+)$/;

function catalogValue<T>(record: Readonly<Record<string, T>>, code: string): T {
  const value = record[code];
  if (value === undefined) {
    throw new RangeError(`el catálogo de cuotas no define ${code}`);
  }
  return value;
}

// --------------------------------------------------------------------- quotas

/** Una cuota tal como la devuelve Service Quotas (`ListServiceQuotas`). */
export interface AppliedQuota {
  readonly code: string;
  readonly name: string;
  readonly value: number;
  readonly adjustable: boolean;
}

/**
 * El valor por defecto de la cuota `code` en `region` (las cuatro regiones
 * grandes parten de más memoria y builds, `AWS_API_NOTES.md` §11).
 */
export function quotaDefault(code: string, region: string | undefined): number {
  const large = QUOTA_LARGE_REGION_DEFAULTS[code];
  if (region !== undefined && LARGE_QUOTA_REGIONS.has(region) && large !== undefined) {
    return large;
  }
  return catalogValue(QUOTA_DEFAULTS, code);
}

/** El enlace de la consola para pedir un aumento de la cuota `code`. */
export function increaseUrl(region: string, code: string): string {
  return INCREASE_URL_TEMPLATE.replace("{region}", region)
    .replace("{service}", QUOTA_SERVICE_CODE)
    .replace("{code}", code);
}

/**
 * Una cuota del catálogo con su valor aplicado (`undefined` si Service
 * Quotas no la lista), el valor por defecto de la región y el enlace de
 * aumento.
 */
export class QuotaEntry {
  constructor(
    readonly code: string,
    readonly name: string,
    readonly kind: string,
    readonly operation: string | undefined,
    readonly defaultValue: number,
    readonly value: number | undefined,
    readonly adjustable: boolean,
    readonly increaseUrl: string,
  ) {}

  get belowDefault(): boolean {
    return this.value !== undefined && this.value < this.defaultValue;
  }
}

/** Una entrada por cuota del catálogo, en el orden del catálogo. */
export function buildQuotaEntries(
  applied: Iterable<AppliedQuota>,
  region: string,
): readonly QuotaEntry[] {
  const byCode = new Map([...applied].map((quota) => [quota.code, quota]));
  return Object.keys(QUOTA_NAMES).map((code) => {
    const quota = byCode.get(code);
    return new QuotaEntry(
      code,
      catalogValue(QUOTA_NAMES, code),
      catalogValue(QUOTA_KINDS, code),
      QUOTA_OPERATIONS[code],
      quotaDefault(code, region),
      quota?.value,
      quota?.adjustable ?? false,
      increaseUrl(region, code),
    );
  });
}

/** La tasa y la ráfaga aplicadas de una operación (`undefined` si no se listan). */
export interface TpsLimit {
  readonly operation: string;
  readonly rate: number | undefined;
  readonly burst: number | undefined;
}

/** Las cuotas de TPS agrupadas por operación, en el orden del catálogo. */
export function tpsLimits(entries: Iterable<QuotaEntry>): readonly TpsLimit[] {
  const rates = new Map<string, number | undefined>();
  const bursts = new Map<string, number | undefined>();
  for (const entry of entries) {
    if (entry.operation === undefined) {
      continue;
    }
    if (entry.kind === RATE_KIND) {
      rates.set(entry.operation, entry.value);
    } else if (entry.kind === BURST_KIND) {
      bursts.set(entry.operation, entry.value);
    }
  }
  const operations = [...new Set([...rates.keys(), ...bursts.keys()])];
  return operations.map((operation) => ({
    operation,
    rate: rates.get(operation),
    burst: bursts.get(operation),
  }));
}

/**
 * `critical` y `warning` desde los umbrales de `limits.json`; sin cuota
 * conocida no hay nivel que avisar.
 */
export function classifyFraction(fraction: number | undefined): QuotaLevel {
  if (fraction === undefined) {
    return "ok";
  }
  if (fraction >= QUOTA_CRITICAL_FRACTION) {
    return "critical";
  }
  if (fraction >= QUOTA_WARNING_FRACTION) {
    return "warning";
  }
  return "ok";
}

/**
 * Memoria asignada (RUNNING más suspendidos) contra la cuota de la región.
 * `unresolved` cuenta los sandboxes cuya memoria de imagen no se pudo leer:
 * el uso real puede ser mayor.
 */
export interface MemoryUsage {
  readonly allocatedGb: number;
  readonly quotaGb: number | undefined;
  readonly fraction: number | undefined;
  readonly level: QuotaLevel;
  readonly sandboxes: number;
  readonly unresolved: number;
}

/**
 * `memoryMib` trae un elemento por sandbox contado (`undefined` si no se
 * resolvió su memoria).
 */
export function memoryUsage(
  memoryMib: Iterable<number | undefined>,
  quotaGb: number | undefined,
): MemoryUsage {
  const samples = [...memoryMib];
  const resolved = samples.filter((mib): mib is number => mib !== undefined);
  const allocatedGb = resolved.reduce((total, mib) => total + mib, 0) / MIB_PER_GB;
  const fraction = quotaGb === undefined || quotaGb === 0 ? undefined : allocatedGb / quotaGb;
  return {
    allocatedGb,
    quotaGb,
    fraction,
    level: classifyFraction(fraction),
    sandboxes: samples.length,
    unresolved: samples.length - resolved.length,
  };
}

/** Un aviso con el enlace para pedir el aumento de `code`. */
export interface QuotaWarning {
  readonly kind: WarningKind;
  readonly code: string;
  readonly message: string;
  readonly increaseUrl: string;
}

function memoryWarning(entry: QuotaEntry, memory: MemoryUsage): QuotaWarning {
  const kind: WarningKind = memory.level === "critical" ? "memory_critical" : "memory_high";
  const percent = (memory.fraction ?? 0) * PERCENT;
  return {
    kind,
    code: entry.code,
    message:
      `Memoria asignada al ${percent.toFixed(0)} % de la cuota ` +
      `(${memory.allocatedGb} de ${memory.quotaGb} GB): pide un aumento antes de ` +
      "lanzar más sandboxes",
    increaseUrl: entry.increaseUrl,
  };
}

/**
 * Avisos de memoria cerca de la cuota, de cuotas por debajo del valor por
 * defecto (las cuentas nuevas parten de cuotas reducidas) y de cuotas que
 * Service Quotas no lista.
 */
export function quotaWarnings(
  entries: Iterable<QuotaEntry>,
  memory: MemoryUsage,
): readonly QuotaWarning[] {
  const warnings: QuotaWarning[] = [];
  for (const entry of entries) {
    if (entry.kind === MEMORY_KIND && memory.level !== "ok" && memory.fraction !== undefined) {
      warnings.push(memoryWarning(entry, memory));
    }
    if (entry.value === undefined) {
      warnings.push({
        kind: "not_listed",
        code: entry.code,
        message: `${entry.name}: Service Quotas no la lista`,
        increaseUrl: entry.increaseUrl,
      });
    } else if (entry.belowDefault) {
      warnings.push({
        kind: "below_default",
        code: entry.code,
        message:
          `${entry.name}: ${entry.value}, por debajo del valor por defecto ` +
          `(${entry.defaultValue}); las cuentas nuevas parten de cuotas reducidas`,
        increaseUrl: entry.increaseUrl,
      });
    }
  }
  return warnings;
}

/** Cuotas aplicadas, uso de memoria y avisos de una región. */
export class QuotaReport {
  constructor(
    readonly region: string,
    readonly entries: readonly QuotaEntry[],
    readonly memory: MemoryUsage,
    readonly warnings: readonly QuotaWarning[],
  ) {}

  get tps(): readonly TpsLimit[] {
    return tpsLimits(this.entries);
  }

  /** La tasa aplicada de `operation` (`SuspendMicrovm`…), o `undefined` si no se lista. */
  appliedRate(operation: string): number | undefined {
    return this.tps.find((limit) => limit.operation === operation)?.rate;
  }
}

export function buildQuotaReport(
  region: string,
  applied: Iterable<AppliedQuota>,
  memoryMib: Iterable<number | undefined>,
): QuotaReport {
  const entries = buildQuotaEntries(applied, region);
  const memoryQuota = entries.find((entry) => entry.kind === MEMORY_KIND)?.value;
  const memory = memoryUsage(memoryMib, memoryQuota);
  return new QuotaReport(region, entries, memory, quotaWarnings(entries, memory));
}

// ----------------------------------------------------------------------- cost

/** Un grupo `USAGE_TYPE` de un periodo de Cost Explorer. */
export interface CostGroup {
  readonly usageType: string;
  readonly costUsd: number;
  readonly quantity: number;
  readonly unit: string;
}

/**
 * Un `ResultsByTime` de Cost Explorer (fechas `AAAA-MM-DD`); `estimated` es
 * el indicador `Estimated` de AWS (el periodo aún puede cambiar).
 */
export interface CostPeriod {
  readonly start: string;
  readonly end: string;
  readonly estimated: boolean;
  readonly groups: readonly CostGroup[];
}

/**
 * Los periodos de una consulta y las peticiones que costó (cada página de
 * `GetCostAndUsage` se factura). `fetchedAt` sólo lo trae un resultado servido
 * de una caché ajena (la tabla de la pila `dashboard`): es cuándo se leyó de
 * verdad, y sin él se toma el instante de la consulta.
 */
export interface CostQueryResult {
  readonly periods: readonly CostPeriod[];
  readonly requests: number;
  readonly fetchedAt?: Date;
}

export interface BilledLine {
  readonly usageType: string;
  readonly component: string;
  readonly quantity: number;
  readonly unit: string;
  readonly costUsd: number;
}

/**
 * Coste facturado de MicroVMs según Cost Explorer, por usage type. `partial`
 * es `true` si algún periodo aún es estimado por AWS (retraso de unas 24 h,
 * `AWS_API_NOTES.md` §12); `requests` son las peticiones de pago que costó;
 * `fetchedAt` cuándo se leyó (la caché lo conserva).
 */
export interface BilledCost {
  readonly start: string;
  readonly end: string;
  readonly region: string | undefined;
  readonly lines: readonly BilledLine[];
  readonly totalUsd: number;
  readonly partial: boolean;
  readonly requests: number;
  readonly fetchedAt: Date;
  readonly label: typeof BILLED_LABEL;
}

/**
 * El componente de un usage type de MicroVMs, o `undefined` si el usage type
 * no es de MicroVMs (el servicio `AWS Lambda` factura más cosas).
 */
export function microvmComponent(usageType: string): string | undefined {
  const suffix = MICROVM_USAGE_TYPE_PATTERN.exec(usageType)?.[1];
  if (suffix === undefined) {
    return undefined;
  }
  return COMPONENT_BY_USAGE_SUFFIX[suffix] ?? OTHER_COMPONENT;
}

interface CostTotals {
  component: string;
  quantity: number;
  unit: string;
  costUsd: number;
}

/**
 * Suma por usage type de MicroVMs los periodos de `result`, sin tocar los de
 * otros usage types de Lambda.
 */
export function foldBilledCost(
  result: CostQueryResult,
  window: {
    readonly start: string;
    readonly end: string;
    readonly region: string | undefined;
    readonly fetchedAt: Date;
  },
): BilledCost {
  const totals = new Map<string, CostTotals>();
  for (const period of result.periods) {
    for (const group of period.groups) {
      const component = microvmComponent(group.usageType);
      if (component === undefined) {
        continue;
      }
      const current = totals.get(group.usageType) ?? {
        component,
        quantity: 0,
        unit: group.unit,
        costUsd: 0,
      };
      totals.set(group.usageType, {
        component,
        quantity: current.quantity + group.quantity,
        unit: group.unit,
        costUsd: current.costUsd + group.costUsd,
      });
    }
  }
  const lines: BilledLine[] = [...totals.entries()]
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(([usageType, total]) => ({ usageType, ...total }));
  return {
    start: window.start,
    end: window.end,
    region: window.region,
    lines,
    totalUsd: lines.reduce((sum, line) => sum + line.costUsd, 0),
    partial: result.periods.some((period) => period.estimated),
    requests: result.requests,
    fetchedAt: result.fetchedAt ?? window.fetchedAt,
    label: BILLED_LABEL,
  };
}

/**
 * Cómputo baseline de un sandbox de `memoryMib` activo `activeSeconds`: un
 * vCPU por cada `BASELINE_MIB_PER_VCPU` MiB más los GB-s de memoria, a los
 * precios de `limits.json` (`AWS_API_NOTES.md` §12: 2 GB cuestan $0,1261/h).
 */
export function estimateComputeUsd(memoryMib: number, activeSeconds: number): number {
  const vcpus = memoryMib / BASELINE_MIB_PER_VCPU;
  const gigabytes = memoryMib / MIB_PER_GB;
  return (
    activeSeconds *
    (vcpus * COMPUTE_PRICE_USD_PER_VCPU_SECOND + gigabytes * COMPUTE_PRICE_USD_PER_GB_SECOND)
  );
}

/**
 * Coste estimado de un sandbox. Sólo los `RUNNING` con memoria conocida
 * llevan importe: de uno suspendido no se conoce el tiempo activo.
 */
export interface SandboxCostEstimate {
  readonly sandboxId: string;
  readonly state: string;
  readonly memoryMib: number | undefined;
  readonly activeSeconds: number | undefined;
  readonly costUsd: number | undefined;
  readonly label: typeof ESTIMATED_LABEL;
}

/** La estimación por sandbox y su total, siempre rotulada `estimated`. */
export interface CostEstimate {
  readonly items: readonly SandboxCostEstimate[];
  readonly totalUsd: number;
  readonly pricingRegion: string;
  readonly basis: string;
  readonly label: typeof ESTIMATED_LABEL;
}

export function estimateSandboxCost(
  sandboxId: string,
  state: string,
  startedAt: Date,
  memoryMib: number | undefined,
  now: Date,
): SandboxCostEstimate {
  if (state !== RUNNING_STATE || memoryMib === undefined) {
    return {
      sandboxId,
      state,
      memoryMib,
      activeSeconds: undefined,
      costUsd: undefined,
      label: ESTIMATED_LABEL,
    };
  }
  const activeSeconds = Math.max(0, (now.getTime() - startedAt.getTime()) / MS_PER_SECOND);
  return {
    sandboxId,
    state,
    memoryMib,
    activeSeconds,
    costUsd: estimateComputeUsd(memoryMib, activeSeconds),
    label: ESTIMATED_LABEL,
  };
}

export function buildCostEstimate(items: Iterable<SandboxCostEstimate>): CostEstimate {
  const listed = [...items];
  return {
    items: listed,
    totalUsd: listed.reduce((sum, item) => sum + (item.costUsd ?? 0), 0),
    pricingRegion: PRICING_REGION,
    basis: ESTIMATE_BASIS,
    label: ESTIMATED_LABEL,
  };
}

/**
 * Caché en memoria con caducidad y tope de entradas, con reloj inyectable
 * (milisegundos). Cada `put` descarta las caducadas y, si aun así se pasa de
 * `maxEntries`, las más antiguas: un servidor de larga vida no crece con
 * cada ventana de consulta distinta.
 */
export class TtlCache<K, V> {
  readonly #ttlMs: number;
  readonly #now: () => number;
  readonly #maxEntries: number;
  readonly #entries = new Map<K, { readonly storedAt: number; readonly value: V }>();

  constructor(ttlMs: number, now: () => number, maxEntries: number = COST_CACHE_MAX_ENTRIES) {
    this.#ttlMs = ttlMs;
    this.#now = now;
    this.#maxEntries = maxEntries;
  }

  get size(): number {
    return this.#entries.size;
  }

  get(key: K): V | undefined {
    const entry = this.#entries.get(key);
    if (entry === undefined) {
      return undefined;
    }
    if (this.#expired(entry.storedAt)) {
      this.#entries.delete(key);
      return undefined;
    }
    return entry.value;
  }

  put(key: K, value: V): void {
    this.#entries.delete(key);
    for (const [stored, entry] of this.#entries) {
      if (this.#expired(entry.storedAt)) {
        this.#entries.delete(stored);
      }
    }
    this.#entries.set(key, { storedAt: this.#now(), value });
    for (const oldest of this.#entries.keys()) {
      if (this.#entries.size <= this.#maxEntries) {
        break;
      }
      this.#entries.delete(oldest);
    }
  }

  #expired(storedAt: number): boolean {
    return this.#now() - storedAt >= this.#ttlMs;
  }
}

// ------------------------------------------------------------- views and bulk

/**
 * Un sandbox del inventario con la memoria de su imagen (`undefined` si no
 * se pudo leer). Sin `envs`, `runHookPayload` ni tokens: no existen en el
 * modelo.
 */
export interface SandboxView {
  readonly sandboxId: string;
  readonly state: string;
  readonly template: string;
  readonly templateName: string;
  readonly templateVersion: string;
  readonly startedAt: Date;
  readonly memoryMib: number | undefined;
}

export interface ImageSummary {
  readonly name: string;
  readonly imageArn: string;
  readonly state: string;
  readonly latestActiveVersion: string | undefined;
  readonly latestFailedVersion: string | undefined;
  readonly createdAt: Date;
}

/**
 * Una versión de imagen; `artifact` es sólo el nombre del fichero del
 * artefacto (nunca el bucket ni la ruta) y `memoryMib` sólo si la lista lo
 * trae.
 */
export interface ImageVersionSummary {
  readonly imageVersion: string;
  readonly state: string;
  readonly status: string;
  readonly baseImageVersion: string | undefined;
  readonly artifact: string | undefined;
  readonly memoryMib: number | undefined;
  readonly createdAt: Date;
}

/** Lo que tardará una acción masiva con la cuota de TPS de su operación. */
export interface BulkPlan {
  readonly action: BulkAction;
  readonly operation: string;
  readonly count: number;
  readonly tps: number;
  readonly estimatedSeconds: number;
}

/**
 * El ritmo de la cola: la cuota aplicada si es menor que la de `apiTps` (una
 * cuenta nueva parte reducida); si es mayor, la de `apiTps`, que es lo que
 * respeta el plano de control del SDK.
 */
export function bulkRate(action: BulkAction, appliedRate: number | undefined): number {
  const operation = BULK_OPERATIONS[action];
  const apiRate = API_TPS[operation];
  if (apiRate === undefined) {
    throw new RangeError(`apiTps no define ${operation}`);
  }
  if (appliedRate === undefined) {
    return apiRate;
  }
  if (!(appliedRate > 0)) {
    throw new InvalidArgumentError(`appliedRate: debe ser mayor que 0 (recibido ${appliedRate})`);
  }
  return Math.min(apiRate, appliedRate);
}

export function planBulk(
  action: BulkAction,
  count: number,
  appliedRate?: number | undefined,
): BulkPlan {
  const tps = bulkRate(action, appliedRate);
  return { action, operation: BULK_OPERATIONS[action], count, tps, estimatedSeconds: count / tps };
}

/**
 * El resultado de una acción sobre un sandbox. `code` es el código de AWS de
 * un fallo, nunca su mensaje.
 */
export interface BulkItemResult {
  readonly sandboxId: string;
  readonly outcome: BulkOutcome;
  readonly code?: string | undefined;
}

export interface BulkProgress {
  readonly done: number;
  readonly total: number;
  readonly last: BulkItemResult;
}

export class BulkResult {
  constructor(
    readonly plan: BulkPlan,
    readonly items: readonly BulkItemResult[],
  ) {}

  count(outcome: BulkOutcome): number {
    return this.items.filter((item) => item.outcome === outcome).length;
  }
}
