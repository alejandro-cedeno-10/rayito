/**
 * Seam de las siete opciones 0.6 de `Sandbox.create()` (M15 foundations).
 * Espejo de `rayito._feature_options`. Mientras una función siga siendo un
 * stub, `planFeatures` lanza `UnimplementedError` nombrando el cambio
 * OpenSpec que la trae, antes de `run-microvm`. Con las siete en
 * `undefined` no hace nada: ni `ConfigureSandbox`, ni un cliente nuevo.
 */

import { UnimplementedError } from "./errors.js";
import type { TelemetryExport } from "./telemetry-export/domain.js";
import { planTelemetry } from "./telemetry-export/domain.js";

export const MOUNTS_CHANGE = "m15-s3-mounts";
export const VOLUMES_CHANGE = "m15-efs-volumes";
export const SIZE_CHANGE = "m15-sizes-catalog";
export const EVENTS_CHANGE = "m15-events-webhooks";
export const TELEMETRY_CHANGE = "m15-rayd-otlp";
export const GATEWAYS_CHANGE = "m15-secrets-gateway";
export const DOMAIN_CHANGE = "m15-custom-domain";

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

/** `telemetry` (m15-rayd-otlp) is the first real function: an already
 * validated `TelemetryExport`, ready for `create()` to build its
 * `TelemetryExportSection` once it knows the image facts (the ARN and
 * version from `run-microvm`, the memory from `Health`). The rest keep
 * throwing `UnimplementedError` and never populate this field. */
export interface FeaturePlan {
  readonly configureSections: readonly unknown[];
  readonly telemetry: TelemetryExport | undefined;
}

const EMPTY_PLAN: FeaturePlan = Object.freeze({ configureSections: [], telemetry: undefined });

/**
 * Punto único por el que `create()` pasa las siete opciones 0.6.
 * `imageVariant` (de `resolveImageVariant`) es lo que `telemetry` (con
 * `OtlpAuth.executionRole()`) usa para exigir la variante caps antes de
 * lanzar, cuando el nombre de imagen ya lo permite saber; las demás ramas
 * siguen sin usarlo. No hace ninguna llamada a AWS ni construye ningún
 * cliente.
 */
export function planFeatures(options: FeatureOptions, imageVariant?: string): FeaturePlan {
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
    throw new UnimplementedError("events", `llega en 0.6 (${EVENTS_CHANGE})`);
  }
  const telemetry =
    options.telemetry === undefined ? undefined : planTelemetry(options.telemetry, imageVariant);
  if (options.gateways !== undefined) {
    throw new UnimplementedError("gateways", `llega en 0.6 (${GATEWAYS_CHANGE})`);
  }
  if (options.domain !== undefined) {
    throw new UnimplementedError("domain", `llega en 0.6 (${DOMAIN_CHANGE})`);
  }
  return telemetry === undefined ? EMPTY_PLAN : { configureSections: [], telemetry };
}
