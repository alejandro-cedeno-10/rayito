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
import { validateEventsOption } from "./lifecycle-events/options.js";
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

/** Por qué `events` sigue sin aceptarse aunque `LifecycleEvents` ya
 * funcione por su cuenta (ver `planFeatures`). */
export const EVENTS_PENDING_REASON =
  `${EVENTS_CHANGE}: falta enviar su sección de ConfigureSandbox tras run-microvm ` +
  "(FeaturePlan.configureSections); LifecycleEvents (deploy, registerWebhook, getEvents) " +
  "ya funciona fuera de create()";

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
 * `requireCapsFor`. `logging` es el `logging` de `create()`: `events` exige
 * que mande los logs a CloudWatch. No hace ninguna llamada a AWS ni
 * construye ningún cliente: `requireCapsFor` es una comprobación puramente
 * sobre el nombre de la imagen.
 *
 * `events` se valida (tipo y `logging`) y después sigue lanzando
 * `UnimplementedError`: su sección de `ConfigureSandbox` necesita
 * `sandboxId`/`imageArn`/`imageVersion`, que sólo existen tras
 * `run-microvm`, y todavía no entra en `FeaturePlan.configureSections`
 * (ADR-020, "Hueco de integración conocido"). Aceptarlo sin enviarla
 * dejaría al usuario pagando la pila sin recibir ningún evento.
 */
export function planFeatures(
  options: FeatureOptions,
  imageVariant?: string,
  logging?: unknown,
): FeaturePlan {
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
    validateEventsOption(options.events, logging);
    throw new UnimplementedError("events", EVENTS_PENDING_REASON);
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
