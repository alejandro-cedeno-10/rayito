/**
 * Seam de las siete opciones 0.6 de `Sandbox.create()` (M15 foundations).
 * Espejo de `rayito._feature_options`. Mientras una función siga siendo un
 * stub, `planFeatures` lanza `UnimplementedError` nombrando el cambio
 * OpenSpec que la trae, antes de `run-microvm`. Con las siete en
 * `undefined` no hace nada: ni `ConfigureSandbox`, ni un cliente nuevo.
 *
 * `mounts` (`m15-s3-mounts`) y `gateways` (m15-secrets-gateway) ya no son
 * stubs. `mounts`: `requireCapsFor` corre aquí, antes de `run-microvm`,
 * cuando `imageVariant` ya permite decidirlo; sobre cualquier otro nombre
 * la decisión se difiere al agente (`Health.features`, comprobado por
 * `requireCapabilities` de `configure/base.ts` antes del `Configure`).
 * `gateways`: sólo se valida su forma; la sección de verdad la construye
 * `create()`/`take()` con su `SecretCache` (`GatewaySectionFactory`).
 */

import type { PlannedSection } from "./configure/base.js";
import { UnimplementedError } from "./errors.js";
import { requireCapsFor } from "./role-policy.js";
import type { S3MountsOption } from "./s3-mounts/domain.js";
import { planS3Mounts } from "./s3-mounts/section.js";
import { type SecretGateway, validateGateways } from "./secret-gateway/domain.js";
import { GatewaySectionFactory } from "./secret-gateway/section.js";

export const VOLUMES_CHANGE = "m15-efs-volumes";
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
  readonly gateways?: Readonly<Record<string, SecretGateway>> | undefined;
  readonly domain?: unknown;
}

/** Las secciones de `ConfigureSandbox` que `create()`/`take()` mandan tras
 * el primer `Health`: listas (`mounts`) o a la espera de la `SecretCache`
 * (`gateways`, un `ConfigureSectionFactory`). */
export interface FeaturePlan {
  readonly configureSections: readonly PlannedSection[];
}

const EMPTY_PLAN: FeaturePlan = Object.freeze({ configureSections: [] });

/**
 * Punto único por el que `create()` pasa las siete opciones 0.6.
 * `imageVariant` (de `resolveImageVariant`) es la variante de imagen,
 * cuando el nombre ya permite decidirla; `mounts` lo usa para
 * `requireCapsFor`. `logging` (el `logging` de `create()`, tal cual: una
 * función que lee los logs del sandbox, como `events`, exige que lleguen a
 * CloudWatch) queda para cuando una función real lo necesite; ninguna rama
 * de hoy lo usa. No hace ninguna llamada a AWS ni construye ningún
 * cliente: `requireCapsFor` es una comprobación puramente sobre el nombre
 * de la imagen.
 */
export function planFeatures(
  options: FeatureOptions,
  imageVariant?: string,
  logging?: unknown,
): FeaturePlan {
  void logging;
  const sections: PlannedSection[] = [];
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
  // `size` (m15-sizes-catalog) ya no es un stub: no produce ninguna
  // sección de ConfigureSandbox (decide qué imagen lanzar, no un ajuste
  // del guest en marcha), así que `create()` la resuelve por su cuenta con
  // `sizing/sizing.ts` antes de pedir el ARN de la plantilla.
  if (options.events !== undefined) {
    throw new UnimplementedError("events", `llega en 0.6 (${EVENTS_CHANGE})`);
  }
  if (options.telemetry !== undefined) {
    throw new UnimplementedError("telemetry", `llega en 0.6 (${TELEMETRY_CHANGE})`);
  }
  if (options.gateways !== undefined) {
    // Sólo valida la forma (ninguna llamada a AWS: `validateGateways` es
    // pura). La `SecretCache` que de verdad resuelve cada cabecera llega
    // después, cuando `create()` ya la calculó para `secrets` — ver
    // `GatewaySectionFactory`.
    sections.push(new GatewaySectionFactory(validateGateways(options.gateways)));
  }
  if (options.domain !== undefined) {
    throw new UnimplementedError("domain", `llega en 0.6 (${DOMAIN_CHANGE})`);
  }
  return sections.length === 0 ? EMPTY_PLAN : { configureSections: sections };
}
