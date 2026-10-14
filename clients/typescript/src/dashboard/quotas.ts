/**
 * `Quotas`: las cuotas aplicadas de Lambda MicroVMs (Service Quotas), la
 * memoria asignada contra la cuota de la región (RUNNING más suspendidos,
 * de `ListMicrovms` por la memoria de cada imagen) y los avisos cerca del
 * límite. Construirlo no hace ninguna llamada a AWS. Espejo de
 * `rayito._dashboard._quotas`.
 */

import type { AwsClientSettings } from "../aws/control-plane.js";
import { defaultSizeCatalog, type SizeCatalog } from "../sizing/catalog.js";
import { sdkServiceQuotasSource } from "./aws.js";
import { buildQuotaReport, MEMORY_COUNTED_STATES, type QuotaReport } from "./domain.js";
import { InventoryReader } from "./inventory.js";
import { LazyPlane } from "./plane.js";
import type { ManagementPlane, QuotaSource } from "./ports.js";

type Credentials = AwsClientSettings["credentials"];

export interface QuotasOptions {
  readonly region?: string | undefined;
  readonly credentials?: Credentials;
  readonly controlPlane?: ManagementPlane | undefined;
  readonly source?: QuotaSource | undefined;
  readonly catalog?: SizeCatalog | undefined;
}

/**
 * Cuotas y uso de memoria de una región.
 *
 * Coste y activación
 * -------------------
 * Activa: una llamada explícita a `report()`; construir `Quotas` no llama
 *   a AWS.
 * Recursos y llamadas AWS: sólo lecturas, ninguna crea recursos:
 *   `ListServiceQuotas` (código `lambda`, con el peer opcional
 *   `@aws-sdk/client-service-quotas`), `ListMicrovms` y, como mucho una vez
 *   por versión de imagen y proceso, `GetMicrovmImageVersion`.
 * Coste aproximado: $0, sin ninguna llamada de pago. `GetMicrovmImageVersion`
 *   no factura ni tiene cuota propia (`AWS_API_NOTES.md` §24 y §30); Service
 *   Quotas no publica ninguna tarifa por consulta (guía de Service Quotas,
 *   https://docs.aws.amazon.com/servicequotas/latest/userguide/intro.html,
 *   consultada 2026-10-13); `ListMicrovms` cuenta contra un TPS no
 *   documentado (a confirmar en la aceptación, tarea 1.1 del cambio
 *   `dashboard`).
 * IAM: `servicequotas:ListServiceQuotas`, `lambda:ListMicrovms` y
 *   `lambda:GetMicrovmImageVersion`.
 * Cómo apagarla: no llames a `report()`; no deja recursos que borrar.
 * Ejemplo:
 *   const report = await new Quotas({ region: "us-east-1" }).report();
 *   console.log(report.memory.level);
 */
export class Quotas {
  readonly #region: string | undefined;
  readonly #credentials: Credentials;
  readonly #plane: LazyPlane;
  readonly #catalog: SizeCatalog;
  #source: QuotaSource | undefined;

  constructor(options: QuotasOptions = {}) {
    this.#region = options.region;
    this.#credentials = options.credentials;
    this.#plane = new LazyPlane(options.region, options.credentials, options.controlPlane);
    this.#source = options.source;
    this.#catalog = options.catalog ?? defaultSizeCatalog;
  }

  /**
   * Gratuita: sólo lecturas de Service Quotas y de Lambda MicroVMs, sin
   * ninguna llamada de pago ni recursos nuevos.
   */
  async report(): Promise<QuotaReport> {
    const plane = this.#plane.get();
    const applied = await this.#quotaSource(plane.region).listAppliedQuotas();
    const views = await new InventoryReader(plane, this.#catalog).views({
      states: MEMORY_COUNTED_STATES,
    });
    return buildQuotaReport(
      plane.region,
      applied,
      views.map((view) => view.memoryMib),
    );
  }

  #quotaSource(region: string): QuotaSource {
    this.#source ??= sdkServiceQuotasSource(this.#region ?? region, this.#credentials);
    return this.#source;
  }
}
