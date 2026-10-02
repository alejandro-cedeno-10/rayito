/**
 * Seam de las siete opciones 0.6 de `Sandbox.create()` (M15 foundations).
 * Espejo de `rayito._feature_options`. Mientras una función siga siendo un
 * stub, `planFeatures` lanza `UnimplementedError` nombrando el cambio
 * OpenSpec que la trae, antes de `run-microvm`. Con las siete en
 * `undefined` no hace nada: ni `ConfigureSandbox`, ni un cliente nuevo.
 */

import { UnimplementedError } from "./errors.js";
import { validateEventsOption } from "./lifecycle-events/options.js";

export const MOUNTS_CHANGE = "m15-s3-mounts";
export const VOLUMES_CHANGE = "m15-efs-volumes";
export const SIZE_CHANGE = "m15-sizes-catalog";
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
  readonly mounts?: Readonly<Record<string, unknown>> | undefined;
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
  readonly configureSections: readonly unknown[];
}

const EMPTY_PLAN: FeaturePlan = Object.freeze({ configureSections: [] });

/**
 * Punto único por el que `create()` pasa las siete opciones 0.6.
 * `imageVariant` (de `resolveImageVariant`) queda para cuando una función
 * real lo necesite. `logging` es el `logging` de `create()`: `events` exige
 * que mande los logs a CloudWatch. No hace ninguna llamada a AWS ni
 * construye ningún cliente.
 *
 * `events` se valida (tipo y `logging`) y después sigue lanzando
 * `UnimplementedError`: su sección de `ConfigureSandbox` necesita
 * `sandboxId`/`imageArn`/`imageVersion`, que sólo existen tras
 * `run-microvm`, y el envío de `FeaturePlan.configureSections` tras el
 * primer `Health` todavía no existe en `create()` (ADR-020, "Hueco de
 * integración conocido"). Aceptarlo sin enviarla dejaría al usuario
 * pagando la pila sin recibir ningún evento.
 */
export function planFeatures(
  options: FeatureOptions,
  imageVariant?: string,
  logging?: unknown,
): FeaturePlan {
  void imageVariant;
  if (options.mounts !== undefined) {
    throw new UnimplementedError("mounts", `llega en 0.6 (${MOUNTS_CHANGE})`);
  }
  if (options.volumes !== undefined) {
    throw new UnimplementedError("volumes", `llega en 0.6 (${VOLUMES_CHANGE})`);
  }
  if (options.size !== undefined) {
    throw new UnimplementedError("size", `llega en 0.6 (${SIZE_CHANGE})`);
  }
  if (options.events !== undefined) {
    validateEventsOption(options.events, logging);
    throw new UnimplementedError("events", EVENTS_PENDING_REASON);
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
  return EMPTY_PLAN;
}
