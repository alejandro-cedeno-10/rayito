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
 * `telemetry` (m15-rayd-otlp) y `events` (m15-events-webhooks) se validan
 * aquí y su sección se planea tras `run-microvm` (`plannedSections`),
 * porque necesitan hechos que sólo existen entonces.
 */

import type { PlannedSection } from "./configure/base.js";
import { UnimplementedError } from "./errors.js";
import { validateEventsOption } from "./lifecycle-events/options.js";
import { LifecycleEventsSectionFactory } from "./lifecycle-events/section.js";
import type { LifecycleEvents } from "./lifecycle-events/service.js";
import { requireCapsFor } from "./role-policy.js";
import { planS3Mounts } from "./s3-mounts/section.js";
import type { SandboxCreateOptions } from "./sandbox/sandbox.js";
import { validateGateways } from "./secret-gateway/domain.js";
import { GatewaySectionFactory } from "./secret-gateway/section.js";
import type { TelemetryExport } from "./telemetry-export/domain.js";
import { planTelemetry } from "./telemetry-export/domain.js";
import { type TelemetryLaunchFacts, TelemetrySectionFactory } from "./telemetry-export/section.js";

export const VOLUMES_CHANGE = "m15-efs-volumes";
export const EVENTS_CHANGE = "m15-events-webhooks";
export const TELEMETRY_CHANGE = "m15-rayd-otlp";
export const GATEWAYS_CHANGE = "m15-secrets-gateway";
export const DOMAIN_CHANGE = "m15-custom-domain";

/**
 * Los siete kwargs 0.6 de `Sandbox.create()`, agrupados: los mismos tipos (y
 * el mismo TSDoc, con su bloque "Coste y activación") que
 * `SandboxCreateOptions`, la única fuente de cada uno.
 */
export type FeatureOptions = Pick<
  SandboxCreateOptions,
  "mounts" | "volumes" | "size" | "events" | "telemetry" | "gateways" | "domain"
>;

/**
 * Lo que `create()` guarda en `LaunchOptions.features` para que
 * `reincarnate()` lance el sucesor con las mismas secciones de
 * ConfigureSandbox: las opciones tal cual (de solo lectura, se guardan por
 * referencia) salvo `size`, que ya va dentro del ARN de plantilla resuelto
 * (`LaunchOptions.template`); repetirlo sobre un ARN es
 * `InvalidArgumentError` (`applySizeSuffix`).
 */
export function relaunchFeatures<T extends FeatureOptions>(options: T): Omit<T, "size"> {
  const { size: _resolvedIntoTemplate, ...features } = options;
  return features;
}

/** Lo que `create()` hace con las opciones 0.6 una vez validadas:
 * `configureSections`, las secciones de `ConfigureSandbox` que ya pueden
 * planearse antes de `run-microvm` (`mounts`; `gateways`, un
 * `ConfigureSectionFactory` a la espera de la `SecretCache`), y
 * `telemetry`, el `TelemetryExport` ya validado cuya sección necesita los
 * hechos de imagen, y `events`, el `LifecycleEvents` cuya sección necesita
 * además el `sandboxId` para derivar `k_sbx`: `plannedSections` las junta
 * en cuanto `create()` conoce esos hechos. */
export interface FeaturePlan {
  readonly configureSections: readonly PlannedSection[];
  readonly telemetry?: TelemetryExport | undefined;
  readonly events?: LifecycleEvents | undefined;
}

/** Lo que sólo `run-microvm` y el primer `Health` saben. */
export interface LaunchFacts extends TelemetryLaunchFacts {
  readonly sandboxId: string;
}

const EMPTY_PLAN: FeaturePlan = Object.freeze({
  configureSections: [],
  telemetry: undefined,
  events: undefined,
});

/**
 * Punto único por el que `create()` pasa las siete opciones 0.6.
 * `imageVariant` (de `resolveImageVariant`) es la variante de imagen,
 * cuando el nombre ya permite decidirla; `mounts` y `telemetry` (con
 * `OtlpAuth.executionRole()`) lo usan para exigir la variante caps antes
 * de lanzar (`requireCapsFor`, una comprobación puramente sobre el nombre
 * de la imagen). `logging` es el `logging` de `create()`: `events` exige
 * que mande los logs a CloudWatch. No hace ninguna llamada a AWS ni
 * construye ningún cliente.
 *
 * `events` se valida aquí (tipo y `logging`) y viaja en
 * `FeaturePlan.events`: su sección necesita `sandboxId`/`imageArn`/
 * `imageVersion`, que sólo existen tras `run-microvm`, así que
 * `plannedSections` la añade entonces (ADR-020). La clave del stack se lee
 * justo antes del `Configure`, nunca aquí.
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
  const events =
    options.events === undefined ? undefined : validateEventsOption(options.events, logging);
  const telemetry =
    options.telemetry === undefined ? undefined : planTelemetry(options.telemetry, imageVariant);
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
  return sections.length === 0 && telemetry === undefined && events === undefined
    ? EMPTY_PLAN
    : { configureSections: sections, telemetry, events };
}

/**
 * Todas las secciones que `create()` manda en su único `Configure`: las de
 * `plan.configureSections` más, con `telemetry`, un
 * `TelemetrySectionFactory` y, con `events`, un
 * `LifecycleEventsSectionFactory`, ambos sobre `facts` (lo que sólo
 * `run-microvm` y el primer `Health` saben). `sandbox.ts` nunca nombra una
 * función concreta.
 */
export function plannedSections(plan: FeaturePlan, facts: LaunchFacts): readonly PlannedSection[] {
  const sections: PlannedSection[] = [...plan.configureSections];
  if (plan.telemetry !== undefined) {
    sections.push(new TelemetrySectionFactory(plan.telemetry, facts));
  }
  if (plan.events !== undefined) {
    sections.push(
      new LifecycleEventsSectionFactory(plan.events, {
        sandboxId: facts.sandboxId,
        imageArn: facts.imageArn,
        imageVersion: facts.imageVersion,
      }),
    );
  }
  return sections;
}
