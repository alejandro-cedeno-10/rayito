/**
 * `UsageCost`: el coste facturado de Lambda MicroVMs (Cost Explorer, por
 * usage type, con caché local de `COST_CACHE_TTL_SECONDS`) y la estimación
 * por sandbox (tiempo activo por los precios documentados). Son dos cosas
 * distintas y cada resultado lleva su rótulo (`billed`, `estimated`): la
 * estimación nunca se presenta como factura. Construirlo no hace ninguna
 * llamada a AWS. Espejo de `rayito._dashboard._cost`.
 */

import type { AwsClientSettings } from "../aws/control-plane.js";
import { InvalidArgumentError } from "../errors.js";
import { COST_CACHE_TTL_SECONDS } from "../limits.js";
import { defaultSizeCatalog, type SizeCatalog } from "../sizing/catalog.js";
import { sdkCostExplorerSource } from "./aws.js";
import {
  type BilledCost,
  buildCostEstimate,
  type CostEstimate,
  estimateSandboxCost,
  foldBilledCost,
  type Granularity,
  MEMORY_COUNTED_STATES,
  MS_PER_SECOND,
  TtlCache,
} from "./domain.js";
import { InventoryReader } from "./inventory.js";
import { LazyPlane } from "./plane.js";
import type { CostSource, ManagementPlane } from "./ports.js";

type Credentials = AwsClientSettings["credentials"];

const MS_PER_DAY = 86_400_000;
/** Longitud de `AAAA-MM-DD` y de su prefijo `AAAA-MM-` en una fecha ISO 8601. */
const ISO_DATE_LENGTH = "YYYY-MM-DD".length;
const ISO_MONTH_PREFIX_LENGTH = "YYYY-MM-".length;
const FIRST_DAY_OF_MONTH = "01";
const ISO_DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;

export interface UsageCostOptions {
  readonly region?: string | undefined;
  readonly credentials?: Credentials;
  readonly controlPlane?: ManagementPlane | undefined;
  readonly source?: CostSource | undefined;
  readonly catalog?: SizeCatalog | undefined;
  /** Caducidad de la caché de `billed()`, en milisegundos (por defecto 24 h). */
  readonly cacheTtlMs?: number | undefined;
  /** Reloj de pared (para fechas y `fetchedAt`); el de la caché es `monotonicMs`. */
  readonly now?: (() => Date) | undefined;
  readonly monotonicMs?: (() => number) | undefined;
}

export interface BilledOptions {
  /** `AAAA-MM-DD` (incluido); por defecto el día 1 del mes en curso (UTC). */
  readonly start?: Date | string | undefined;
  /** `AAAA-MM-DD` (excluido); por defecto mañana (UTC). */
  readonly end?: Date | string | undefined;
  readonly granularity?: Granularity | undefined;
  /** Filtra por región de AWS; sin filtro, toda la cuenta. */
  readonly region?: string | undefined;
  /** Ignora la caché local. */
  readonly refresh?: boolean | undefined;
}

function isoDate(value: Date): string {
  return value.toISOString().slice(0, ISO_DATE_LENGTH);
}

function asDate(value: Date | string, name: string): string {
  if (value instanceof Date) {
    return isoDate(value);
  }
  if (!ISO_DATE_PATTERN.test(value) || Number.isNaN(Date.parse(value))) {
    throw new InvalidArgumentError(`${name}: se esperaba una fecha AAAA-MM-DD`);
  }
  return value;
}

/**
 * Coste facturado y estimado de Lambda MicroVMs.
 *
 * Coste y activación
 * -------------------
 * Activa: una llamada explícita a `billed()` (la estimación, `estimate()`,
 *   no usa Cost Explorer); construir `UsageCost` no llama a AWS.
 * Recursos y llamadas AWS: sólo lecturas. `billed()` hace
 *   `ce:GetCostAndUsage` (una petición por página, normalmente una) en
 *   us-east-1, donde vive Cost Explorer, con el peer opcional
 *   `@aws-sdk/client-cost-explorer`; `estimate()` hace `ListMicrovms` y
 *   `GetMicrovmImageVersion` (una por versión de imagen y proceso).
 * Coste aproximado: $0,01 por petición a Cost Explorer (us-east-1,
 *   consultado 2026-10-13,
 *   https://aws.amazon.com/aws-cost-management/aws-cost-explorer/pricing/);
 *   la caché local conserva cada ventana `COST_CACHE_TTL_SECONDS` (24 h),
 *   unas 30 peticiones al mes ≈ $0,30 con el panel abierto todo el mes.
 *   `estimate()` es gratuita.
 * IAM: `ce:GetCostAndUsage` para `billed()`; `lambda:ListMicrovms` y
 *   `lambda:GetMicrovmImageVersion` para `estimate()`.
 * Cómo apagarla: no llames a `billed()`; no deja recursos que borrar.
 * Ejemplo:
 *   const cost = await new UsageCost().billed();
 *   console.log(cost.totalUsd, cost.partial);
 */
export class UsageCost {
  readonly #credentials: Credentials;
  readonly #plane: LazyPlane;
  readonly #catalog: SizeCatalog;
  readonly #now: () => Date;
  readonly #cache: TtlCache<string, BilledCost>;
  #source: CostSource | undefined;

  constructor(options: UsageCostOptions = {}) {
    this.#credentials = options.credentials;
    this.#plane = new LazyPlane(options.region, options.credentials, options.controlPlane);
    this.#source = options.source;
    this.#catalog = options.catalog ?? defaultSizeCatalog;
    this.#now = options.now ?? (() => new Date());
    this.#cache = new TtlCache(
      options.cacheTtlMs ?? COST_CACHE_TTL_SECONDS * MS_PER_SECOND,
      options.monotonicMs ?? (() => performance.now()),
    );
  }

  /**
   * El coste facturado de `start` (incluido) a `end` (excluido). Cost
   * Explorer tiene unas 24 h de retraso, y `partial` avisa de que aún hay
   * periodos estimados por AWS. Una ventana ya leída se sirve de la caché
   * local hasta que caduca.
   */
  async billed(options: BilledOptions = {}): Promise<BilledCost> {
    const [start, end] = this.#window(options.start, options.end);
    const granularity = options.granularity ?? "MONTHLY";
    const key = JSON.stringify([start, end, granularity, options.region ?? null]);
    const cached = options.refresh === true ? undefined : this.#cache.get(key);
    if (cached !== undefined) {
      return cached;
    }
    const result = await this.#costSource().getCostAndUsage({
      start,
      end,
      granularity,
      region: options.region,
    });
    const cost = foldBilledCost(result, {
      start,
      end,
      region: options.region,
      fetchedAt: this.#now(),
    });
    this.#cache.put(key, cost);
    return cost;
  }

  /**
   * La estimación por sandbox: el tiempo transcurrido desde el arranque de
   * cada `RUNNING` por el precio de cómputo baseline de su tamaño. Siempre
   * `estimated`: no incluye snapshots ni el consumo sobre el baseline.
   */
  async estimate(): Promise<CostEstimate> {
    const plane = this.#plane.get();
    const now = this.#now();
    const views = await new InventoryReader(plane, this.#catalog).views({
      states: MEMORY_COUNTED_STATES,
    });
    return buildCostEstimate(
      views.map((view) =>
        estimateSandboxCost(view.sandboxId, view.state, view.startedAt, view.memoryMib, now),
      ),
    );
  }

  #window(start: Date | string | undefined, end: Date | string | undefined): [string, string] {
    const today = this.#now();
    const windowStart =
      start === undefined
        ? `${isoDate(today).slice(0, ISO_MONTH_PREFIX_LENGTH)}${FIRST_DAY_OF_MONTH}`
        : asDate(start, "start");
    const windowEnd =
      end === undefined ? isoDate(new Date(today.getTime() + MS_PER_DAY)) : asDate(end, "end");
    if (windowStart >= windowEnd) {
      throw new InvalidArgumentError("billed: start tiene que ser anterior a end");
    }
    return [windowStart, windowEnd];
  }

  #costSource(): CostSource {
    this.#source ??= sdkCostExplorerSource(this.#credentials);
    return this.#source;
  }
}
