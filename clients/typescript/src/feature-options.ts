/**
 * Seam de las siete opciones 0.6 de `Sandbox.create()` (M15 foundations).
 * Espejo de `rayito._feature_options`. Mientras una función siga siendo un
 * stub, `planFeatures` lanza `UnimplementedError` nombrando el cambio
 * OpenSpec que la trae, antes de `run-microvm`. Con las siete en
 * `undefined` no hace nada: ni `ConfigureSandbox`, ni un cliente nuevo.
 *
 * `mounts` (`m15-s3-mounts`) es la primera en dejar de ser un stub:
 * `requireCapsFor` corre aquí, antes de `run-microvm`, cuando `imageVariant`
 * ya permite decidirlo; sobre cualquier otro nombre la decisión se difiere
 * al agente (`Health.features`, comprobado tras `/run` por `Sandbox.#open`
 * — ver `configure-base.ts`'s `requireCapabilities`).
 */

import type { ConfigureSection } from "./configure-base.js";
import { UnimplementedError } from "./errors.js";
import { requireCapsFor } from "./role-policy.js";
import type { S3MountsOption } from "./s3-mounts/domain.js";
import { planS3Mounts } from "./s3-mounts/section.js";

export const VOLUMES_CHANGE = "m15-efs-volumes";
export const SIZE_CHANGE = "m15-sizes-catalog";
export const EVENTS_CHANGE = "m15-events-webhooks";
export const TELEMETRY_CHANGE = "m15-rayd-otlp";
export const GATEWAYS_CHANGE = "m15-secrets-gateway";
export const DOMAIN_CHANGE = "m15-custom-domain";

/** Los siete kwargs 0.6 de `Sandbox.create()`, agrupados. */
export interface FeatureOptions {
  readonly mounts?: S3MountsOption | undefined;
  readonly volumes?: Readonly<Record<string, unknown>> | undefined;
  readonly size?: unknown;
  readonly events?: unknown;
  readonly telemetry?: unknown;
  readonly gateways?: Readonly<Record<string, unknown>> | undefined;
  readonly domain?: unknown;
}

/** Vacío en 0.6 foundations a propósito: `planFeatures` lanza antes de
 * construir uno si alguna opción estaba puesta. */
export interface FeaturePlan {
  readonly configureSections: readonly ConfigureSection[];
}

const EMPTY_PLAN: FeaturePlan = Object.freeze({ configureSections: [] });

/**
 * Punto único por el que `create()` pasa las siete opciones 0.6.
 * `imageVariant` (de `resolveImageVariant`) es la variante de imagen,
 * cuando el nombre ya permite decidirla; `mounts` lo usa para
 * `requireCapsFor`. No hace ninguna llamada a AWS ni construye ningún
 * cliente: `requireCapsFor` es una comprobación puramente sobre el nombre
 * de la imagen.
 */
export function planFeatures(options: FeatureOptions, imageVariant?: string): FeaturePlan {
  const sections: ConfigureSection[] = [];
  if (options.mounts !== undefined) {
    requireCapsFor("mounts", imageVariant);
    const section = planS3Mounts(options.mounts);
    if (section !== undefined) {
      sections.push(section);
    }
  }
  if (options.volumes !== undefined) {
    throw new UnimplementedError("volumes", `llega en 0.6 (${VOLUMES_CHANGE})`);
  }
  if (options.size !== undefined) {
    throw new UnimplementedError("size", `llega en 0.6 (${SIZE_CHANGE})`);
  }
  if (options.events !== undefined) {
    throw new UnimplementedError("events", `llega en 0.6 (${EVENTS_CHANGE})`);
  }
  if (options.telemetry !== undefined) {
    throw new UnimplementedError("telemetry", `llega en 0.6 (${TELEMETRY_CHANGE})`);
  }
  if (options.gateways !== undefined) {
    throw new UnimplementedError("gateways", `llega en 0.6 (${GATEWAYS_CHANGE})`);
  }
  if (options.domain !== undefined) {
    throw new UnimplementedError("domain", `llega en 0.6 (${DOMAIN_CHANGE})`);
  }
  return sections.length === 0 ? EMPTY_PLAN : { configureSections: sections };
}
