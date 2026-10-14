/**
 * `Fleet`: lo que el panel necesita para ver y operar la flota, sobre los
 * puertos de `ports.ts` y reutilizando el SDK (`ControlPlane`,
 * `LifecycleEvents`): inventario de sandboxes, imágenes y versiones,
 * historial de eventos y acciones masivas con una cola que respeta los TPS.
 * Es de sólo lectura salvo que se construya con `allowActions: true`.
 * Construirlo no hace ninguna llamada a AWS. Espejo de
 * `rayito._dashboard._fleet`.
 */

import {
  type AwsClientSettings,
  type MonotonicClock,
  type Sleeper,
  TokenBucket,
} from "../aws/control-plane.js";
import {
  AuthenticationError,
  CapacityError,
  InvalidArgumentError,
  QuotaExceededError,
  SandboxError,
} from "../errors.js";
import type { EventRecord } from "../lifecycle-events/domain.js";
import { DEFAULT_GET_EVENTS_LIMIT } from "../lifecycle-events/domain.js";
import { MICROVM_ID_MAX_LENGTH, MICROVM_ID_MIN_LENGTH } from "../limits.js";
import { defaultSizeCatalog, type SizeCatalog } from "../sizing/catalog.js";
import { lambdaMicrovmsImageCatalog } from "./aws.js";
import {
  BULK_OPERATIONS,
  type BulkAction,
  type BulkItemResult,
  type BulkPlan,
  type BulkProgress,
  BulkResult,
  CAPACITY_CODE,
  type ImageSummary,
  type ImageVersionSummary,
  planBulk,
  QUOTA_EXCEEDED_CODE,
  type SandboxView,
  UNKNOWN_FAILURE_CODE,
} from "./domain.js";
import { InventoryReader } from "./inventory.js";
import { LazyPlane } from "./plane.js";
import type { EventsHistory, ImageCatalog, ManagementPlane } from "./ports.js";

type Credentials = AwsClientSettings["credentials"];

export interface FleetOptions {
  readonly region?: string | undefined;
  readonly credentials?: Credentials;
  readonly controlPlane?: ManagementPlane | undefined;
  readonly images?: ImageCatalog | undefined;
  readonly events?: EventsHistory | undefined;
  /**
   * Las acciones masivas (`runBulk`) exigen `true`; por defecto, sólo
   * lectura. La llamada no factura, pero el efecto sí: `kill` deja de
   * facturar cómputo; `suspend` escribe un snapshot ($0,0038/GB) y paga
   * almacenamiento ($0,08/GB-mes); `resume` lee el snapshot ($0,00155/GB) y
   * vuelve a facturar cómputo al precio de su tamaño.
   */
  readonly allowActions?: boolean | undefined;
  readonly catalog?: SizeCatalog | undefined;
  /** Reloj monótono en segundos y espera de la cola (inyectables en tests). */
  readonly monotonic?: MonotonicClock | undefined;
  readonly sleep?: Sleeper | undefined;
}

export interface SandboxesOptions {
  readonly states?: Iterable<string> | undefined;
  /** Nombre o ARN de imagen. */
  readonly image?: string | undefined;
}

export interface EventsOptions {
  readonly sandboxId?: string | undefined;
  readonly types?: readonly string[] | undefined;
  readonly limit?: number | undefined;
  readonly order?: "asc" | "desc" | undefined;
}

export interface RunBulkOptions {
  /** La cuota de TPS aplicada (`QuotaReport.appliedRate`). */
  readonly appliedRate?: number | undefined;
  /** Se llama tras cada sandbox. */
  readonly onProgress?: ((progress: BulkProgress) => void) | undefined;
}

export type BulkProgressCallback = (progress: BulkProgress) => void;

/**
 * Inventario, imágenes, eventos y acciones sobre los MicroVMs de la cuenta.
 *
 * Coste y activación
 * -------------------
 * Activa: llamar a sus métodos; construir `Fleet` no llama a AWS. Las
 *   acciones masivas exigen además `allowActions: true`.
 * Recursos y llamadas AWS: lecturas (`ListMicrovms`, `ListMicrovmImages`,
 *   `ListMicrovmImageVersions`, `GetMicrovmImageVersion`) y, con
 *   `allowActions: true`, `TerminateMicrovm`, `SuspendMicrovm` y
 *   `ResumeMicrovm` sobre los sandboxes que se indiquen; `events()` lee la
 *   tabla de la pila `events-webhooks` si se pasó `events`.
 * Coste aproximado: $0 las lecturas de sandboxes, imágenes y versiones.
 *   `events()` es de pago, aunque mínimo: lee la tabla DynamoDB de
 *   `events-webhooks` (lecturas bajo demanda,
 *   https://aws.amazon.com/dynamodb/pricing/on-demand/), unas pocas por
 *   consulta y sin sondeo. Las acciones (sólo con `allowActions: true`) no
 *   facturan por la llamada, pero cambian lo que se factura (us-east-1,
 *   `AWS_API_NOTES.md` §12, consultado 2026-10-13): `kill` deja de facturar
 *   cómputo; `suspend` escribe un snapshot ($0,0038/GB) y, mientras dure,
 *   paga almacenamiento ($0,08/GB-mes); `resume` lee el snapshot
 *   ($0,00155/GB) y vuelve a facturar cómputo al precio de su tamaño (vCPU-s
 *   y GB-s de §12, p. ej. $0,1261/h con 2 GB y 1 vCPU), así que reanudar
 *   muchos sandboxes a la vez sube la factura.
 * IAM: `lambda:ListMicrovms`, `lambda:ListMicrovmImages`,
 *   `lambda:ListMicrovmImageVersions`, `lambda:GetMicrovmImageVersion`; con
 *   acciones, `lambda:TerminateMicrovm`, `lambda:SuspendMicrovm` y
 *   `lambda:ResumeMicrovm`.
 * Cómo apagarla: no llames a sus métodos; las acciones están apagadas salvo
 *   `allowActions: true` y no dejan recursos que borrar. `events()` no
 *   consulta nada sin `events`.
 * Ejemplo:
 *   const fleet = new Fleet({ region: "us-east-1" });
 *   const running = await fleet.sandboxes({ states: ["RUNNING"] });
 */
export class Fleet {
  readonly #region: string | undefined;
  readonly #credentials: Credentials;
  readonly #plane: LazyPlane;
  readonly #events: EventsHistory | undefined;
  readonly #allowActions: boolean;
  readonly #catalog: SizeCatalog;
  readonly #monotonic: MonotonicClock | undefined;
  readonly #sleep: Sleeper | undefined;
  #images: ImageCatalog | undefined;

  constructor(options: FleetOptions = {}) {
    this.#region = options.region;
    this.#credentials = options.credentials;
    this.#plane = new LazyPlane(options.region, options.credentials, options.controlPlane);
    this.#images = options.images;
    this.#events = options.events;
    this.#allowActions = options.allowActions ?? false;
    this.#catalog = options.catalog ?? defaultSizeCatalog;
    this.#monotonic = options.monotonic;
    this.#sleep = options.sleep;
  }

  get allowActions(): boolean {
    return this.#allowActions;
  }

  /**
   * Los sandboxes con la memoria de su imagen. Sin `states`, los que no
   * están terminados; `image` filtra por nombre o ARN de imagen.
   *
   * Gratuita: `ListMicrovms` y `GetMicrovmImageVersion` no facturan.
   */
  async sandboxes(options: SandboxesOptions = {}): Promise<SandboxView[]> {
    const plane = this.#plane.get();
    const imageArn =
      options.image === undefined ? undefined : await plane.resolveTemplateArn(options.image);
    return new InventoryReader(plane, this.#catalog).views({
      states: options.states,
      imageArn,
    });
  }

  /** Las imágenes de la cuenta. Gratuita: `ListMicrovmImages` no factura. */
  images(options: { readonly nameFilter?: string | undefined } = {}): Promise<ImageSummary[]> {
    return this.#imageCatalog().listImages(options);
  }

  /**
   * Las versiones de `image` (nombre o ARN), la más nueva primero.
   *
   * Gratuita: `ListMicrovmImageVersions` no factura.
   */
  async imageVersions(image: string): Promise<ImageVersionSummary[]> {
    const arn = await this.#plane.get().resolveTemplateArn(image);
    return this.#imageCatalog().listVersions(arn);
  }

  /**
   * El historial de eventos de ciclo de vida.
   *
   * De pago, aunque mínimo: lee la tabla DynamoDB de `events-webhooks`
   * (lecturas bajo demanda, `AWS_API_NOTES.md` §25; un puñado por consulta,
   * sin sondeo). Sin `events` no llama a nada y falla.
   */
  async events(options: EventsOptions = {}): Promise<EventRecord[]> {
    if (this.#events === undefined) {
      throw new InvalidArgumentError(
        "Fleet.events: no hay historial de eventos; pasa events: new LifecycleEvents(...) " +
          "con la pila events-webhooks desplegada",
      );
    }
    return this.#events.getEvents({
      ...(options.sandboxId === undefined ? {} : { sandboxId: options.sandboxId }),
      ...(options.types === undefined ? {} : { types: options.types }),
      limit: options.limit ?? DEFAULT_GET_EVENTS_LIMIT,
      order: options.order ?? "desc",
    });
  }

  /**
   * Cuánto tardaría una acción masiva; `appliedRate` es la cuota de TPS
   * aplicada (`QuotaReport.appliedRate`).
   *
   * Gratuita: es un cálculo local, no llama a AWS.
   */
  planBulk(
    action: BulkAction,
    count: number,
    options: { readonly appliedRate?: number | undefined } = {},
  ): BulkPlan {
    validateAction(action);
    return planBulk(action, count, options.appliedRate);
  }

  /**
   * Aplica `action` a cada sandbox, de uno en uno y al ritmo de la cuota de
   * TPS de su operación (suspender, 2 TPS). Un fallo de un sandbox no
   * detiene a los demás: queda `failed` con el código de AWS. Un id
   * repetido se aplica una sola vez. `onProgress` se llama tras cada
   * sandbox.
   *
   * Exige `allowActions: true`. La llamada no factura, pero el efecto sí
   * (`AWS_API_NOTES.md` §12, us-east-1): `kill` deja de facturar cómputo;
   * `suspend` escribe un snapshot ($0,0038/GB) y paga almacenamiento
   * ($0,08/GB-mes) mientras esté suspendido; `resume` lee el snapshot
   * ($0,00155/GB) y vuelve a facturar cómputo al precio de su tamaño.
   */
  async runBulk(
    action: BulkAction,
    sandboxIds: Iterable<string>,
    options: RunBulkOptions = {},
  ): Promise<BulkResult> {
    if (!this.#allowActions) {
      throw new InvalidArgumentError(
        "Fleet.runBulk: las acciones están desactivadas; crea new Fleet({ allowActions: true })",
      );
    }
    validateAction(action);
    const ids = validatedIds(sandboxIds);
    const plan = planBulk(action, ids.length, options.appliedRate);
    const bucket = new TokenBucket(plan.tps, {
      now: this.#monotonic,
      sleep: this.#sleep,
    });
    const results: BulkItemResult[] = [];
    for (const sandboxId of ids) {
      await bucket.acquire();
      const result = await this.#apply(action, sandboxId);
      results.push(result);
      options.onProgress?.({ done: results.length, total: ids.length, last: result });
    }
    return new BulkResult(plan, results);
  }

  async #apply(action: BulkAction, sandboxId: string): Promise<BulkItemResult> {
    const plane = this.#plane.get();
    try {
      const changed = await {
        kill: () => plane.terminateMicrovm(sandboxId),
        suspend: () => plane.suspendMicrovm(sandboxId),
        resume: () => plane.resumeMicrovm(sandboxId),
      }[action]();
      return { sandboxId, outcome: changed ? "done" : "unchanged" };
    } catch (error) {
      if (error instanceof QuotaExceededError) {
        return { sandboxId, outcome: "failed", code: QUOTA_EXCEEDED_CODE };
      }
      if (error instanceof CapacityError) {
        return { sandboxId, outcome: "failed", code: CAPACITY_CODE };
      }
      if (error instanceof SandboxError || error instanceof AuthenticationError) {
        return { sandboxId, outcome: "failed", code: error.awsCode ?? UNKNOWN_FAILURE_CODE };
      }
      throw error;
    }
  }

  #imageCatalog(): ImageCatalog {
    this.#images ??= lambdaMicrovmsImageCatalog(
      this.#region ?? this.#plane.get().region,
      this.#credentials,
    );
    return this.#images;
  }
}

function validateAction(action: string): void {
  if (!(action in BULK_OPERATIONS)) {
    const admitted = Object.keys(BULK_OPERATIONS).join(", ");
    throw new InvalidArgumentError(
      `action: ${JSON.stringify(action)} no es válida (admitidas: ${admitted})`,
    );
  }
}

function validatedIds(sandboxIds: Iterable<string>): string[] {
  const ids = [...new Set(sandboxIds)];
  if (ids.length === 0) {
    throw new InvalidArgumentError("sandboxIds no puede estar vacío");
  }
  for (const id of ids) {
    if (id.length < MICROVM_ID_MIN_LENGTH || id.length > MICROVM_ID_MAX_LENGTH) {
      throw new InvalidArgumentError(
        `sandboxIds: cada id mide entre ${MICROVM_ID_MIN_LENGTH} y ${MICROVM_ID_MAX_LENGTH} caracteres`,
      );
    }
  }
  return ids;
}
