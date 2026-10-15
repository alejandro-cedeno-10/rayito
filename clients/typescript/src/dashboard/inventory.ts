/**
 * Inventario de sandboxes con la memoria de su imagen: lo comparten `Fleet`
 * (la lista del panel), `Quotas` (la memoria asignada) y `UsageCost` (la
 * estimación). Reutiliza `ConventionCatalog` de `sizing/catalog`: una
 * `GetMicrovmImageVersion` gratuita por versión de imagen y proceso,
 * cacheada. Espejo de `rayito._dashboard._inventory`.
 */

import { AuthenticationError, SandboxError } from "../errors.js";
import { defaultSizeCatalog, type SizeCatalog } from "../sizing/catalog.js";
import type { SandboxView } from "./domain.js";
import type { ManagementPlane } from "./ports.js";

export interface ViewsOptions {
  readonly states?: Iterable<string> | undefined;
  readonly imageArn?: string | undefined;
}

/**
 * Lista los MicroVMs y resuelve la memoria de cada versión de imagen una
 * sola vez por lectura. Una versión ilegible (sin permiso, borrada) deja
 * `memoryMib: undefined` en vez de romper el inventario.
 */
export class InventoryReader {
  readonly #plane: ManagementPlane;
  readonly #catalog: SizeCatalog;

  constructor(plane: ManagementPlane, catalog: SizeCatalog = defaultSizeCatalog) {
    this.#plane = plane;
    this.#catalog = catalog;
  }

  async views(options: ViewsOptions = {}): Promise<SandboxView[]> {
    const resolved = new Map<string, number | undefined>();
    const views: SandboxView[] = [];
    const listOptions = {
      ...(options.states === undefined ? {} : { states: [...options.states] }),
      ...(options.imageArn === undefined ? {} : { imageArn: options.imageArn }),
    };
    for await (const item of this.#plane.listMicrovms(listOptions)) {
      views.push({
        sandboxId: item.sandboxId,
        state: item.state,
        template: item.template,
        templateName: item.templateName,
        templateVersion: item.templateVersion,
        startedAt: item.startedAt,
        memoryMib: await this.#memoryMib(resolved, item.template, item.templateVersion),
      });
    }
    return views;
  }

  async #memoryMib(
    resolved: Map<string, number | undefined>,
    arn: string,
    version: string,
  ): Promise<number | undefined> {
    const key = `${arn}@${version}`;
    if (!resolved.has(key)) {
      resolved.set(key, await this.#lookup(arn, version));
    }
    return resolved.get(key);
  }

  async #lookup(arn: string, version: string): Promise<number | undefined> {
    try {
      return await this.#catalog.minimumMemoryMib(this.#plane, arn, version);
    } catch (error) {
      if (error instanceof SandboxError || error instanceof AuthenticationError) {
        return undefined;
      }
      throw error;
    }
  }
}
