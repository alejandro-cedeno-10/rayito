/**
 * Puertos del panel de gestión (ADR-026, decisión D1). `SandboxInventory` y
 * `Actions` son el subconjunto de `ControlPlane` que necesitan (el plano de
 * control ya los cumple, sin adaptador nuevo); `ImageCatalog`, `QuotaSource`
 * y `CostSource` tienen un adaptador por API de AWS (`aws.ts`) y
 * `EventsHistory` lo cumple `LifecycleEvents.getEvents`. Espejo de
 * `rayito._dashboard._ports`.
 */

import type { ListMicrovmsOptions } from "../aws/control-plane.js";
import type { EventRecord } from "../lifecycle-events/domain.js";
import type { SandboxListItem } from "../models.js";
import type { ImageVersionReader } from "../sizing/catalog.js";
import type {
  AppliedQuota,
  CostQueryResult,
  Granularity,
  ImageSummary,
  ImageVersionSummary,
} from "./domain.js";

/** Listar los MicroVMs de la región (`ListMicrovms`). */
export interface SandboxInventory {
  listMicrovms(options?: ListMicrovmsOptions): AsyncIterable<SandboxListItem>;
}

/**
 * Matar, suspender y reanudar un MicroVM; los tres son idempotentes en AWS
 * y devuelven `false` cuando no había nada que hacer.
 */
export interface Actions {
  terminateMicrovm(sandboxId: string): Promise<boolean>;
  suspendMicrovm(sandboxId: string): Promise<boolean>;
  resumeMicrovm(sandboxId: string): Promise<boolean>;
}

/**
 * Lo que `Fleet` pide al plano de control: el inventario, las acciones, la
 * resolución del nombre de imagen y la memoria de una versión.
 */
export interface ManagementPlane extends SandboxInventory, Actions, ImageVersionReader {
  readonly region: string;
  resolveTemplateArn(template: string): Promise<string>;
}

/**
 * Imágenes de la cuenta y sus versiones (`ListMicrovmImages`,
 * `ListMicrovmImageVersions`); la versión más nueva va primero.
 */
export interface ImageCatalog {
  listImages(options?: { readonly nameFilter?: string | undefined }): Promise<ImageSummary[]>;
  listVersions(imageArn: string): Promise<ImageVersionSummary[]>;
}

/** Las cuotas de Lambda MicroVMs aplicadas en la cuenta y región. */
export interface QuotaSource {
  listAppliedQuotas(): Promise<AppliedQuota[]>;
}

export interface CostQuery {
  readonly start: string;
  readonly end: string;
  readonly granularity: Granularity;
  readonly region: string | undefined;
}

/**
 * Coste de Lambda agrupado por usage type (Cost Explorer). Cada página de la
 * consulta se factura: el resultado cuenta las peticiones.
 */
export interface CostSource {
  getCostAndUsage(query: CostQuery): Promise<CostQueryResult>;
}

export interface EventsQuery {
  readonly sandboxId?: string;
  readonly types?: readonly string[];
  readonly limit?: number;
  readonly order?: "asc" | "desc";
}

/** El historial de eventos de ciclo de vida (pila `events-webhooks`). */
export interface EventsHistory {
  getEvents(options?: EventsQuery): Promise<EventRecord[]>;
}
