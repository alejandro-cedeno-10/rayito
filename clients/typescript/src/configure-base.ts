/**
 * Parte pura de `ConfigureSandbox` (M15 foundations, ADR-015): qué dice
 * `Health.features` sobre un agente, y cómo se traduce el resultado de una
 * sección de `Configure` a un error. Espejo de `rayito._configure_base`.
 * Ninguna llamada de red vive aquí; el RPC en sí lo hace `sandbox/core.ts`
 * (`core.clients.configure`).
 */

import { randomUUID } from "node:crypto";
import { create } from "@bufbuild/protobuf";

import { UnimplementedError } from "./errors.js";
import {
  ConfigSection,
  type ConfigureRequest,
  ConfigureRequestSchema,
  type ConfigureResponse,
  type ConfigureStatusResponse,
  SectionCode,
  type SectionResult,
} from "./gen/rayito/v1/configure_pb.js";
import type { AgentFeatures } from "./gen/rayito/v1/features_pb.js";
import type { HealthResponse } from "./gen/rayito/v1/health_pb.js";

export const CONFIGURE_DOC = "docs/site/docs/funciones-opcionales/pilas-opcionales.md";

/** Cadencia del sondeo de `ConfigureStatus` mientras alguna sección sigue
 * `PENDING` tras `Configure`: un montaje local responde en decenas de
 * milisegundos una vez listo, así que un cuarto de segundo no añade
 * latencia apreciable a `create()` ni martillea al agente. Espejo de
 * `CONFIGURE_SETTLE_POLL_S` de Python. */
export const CONFIGURE_SETTLE_POLL_MS = 250;

/**
 * `undefined` cuando el agente es anterior a 0.6.0 (el campo `features`
 * está ausente, presencia de mensaje de proto3: no basta con mirar los
 * flags, que todos valdrían `false` igual que en un 0.6.0 sin ninguna
 * función real todavía). `requireConfigureSupport` exige 0.6.0 antes de
 * usar cualquier flag.
 */
export function agentFeaturesFromHealth(response: HealthResponse): AgentFeatures | undefined {
  return response.features;
}

/**
 * Lanza `UnimplementedError` nombrando la imagen 0.6.0 si `features` es
 * `undefined` (agente anterior a `ConfigureService`); devuelve `features`
 * tal cual en otro caso, para que el llamante siga comprobando su propio
 * flag.
 */
export function requireConfigureSupport(
  features: AgentFeatures | undefined,
  feature: string,
): AgentFeatures {
  if (features === undefined) {
    throw new UnimplementedError(
      feature,
      "necesita una imagen 0.6.0 o posterior (ConfigureSandbox)",
      CONFIGURE_DOC,
    );
  }
  return features;
}

/**
 * Lo que una función 0.6 implementa para participar en una sola llamada a
 * `Configure`: el nombre de su sección (para los logs y el orden), el flag
 * de `AgentFeatures` que debe estar activo, cómo rellena su campo del
 * `ConfigureRequest` compartido, cómo traduce el `SectionResult` que le
 * corresponde en su propio error y, si el agente la deja en `PENDING`,
 * cuánto esperar (`settleTimeoutMs`) y cómo leer su estado en
 * `ConfigureStatus` (`checkStatus`: `true` si ya se asentó; lanza su
 * propio error si falló, o si `final` y todavía no se ha asentado).
 */
export interface ConfigureSection {
  readonly section: string;
  readonly requiredFlag: keyof AgentFeatures;
  readonly settleTimeoutMs: number;
  fill(request: ConfigureRequest): void;
  checkResult(code: number, errorClass: string): void;
  checkStatus(status: ConfigureStatusResponse, final: boolean): boolean;
}

const WIRE_SECTION_NAMES: ReadonlyMap<ConfigSection, string> = new Map([
  [ConfigSection.S3_MOUNTS, "s3_mounts"],
  [ConfigSection.EFS_VOLUMES, "efs_volumes"],
  [ConfigSection.LIFECYCLE_EVENTS, "lifecycle_events"],
  [ConfigSection.TELEMETRY_EXPORT, "telemetry_export"],
  [ConfigSection.SECRET_GATEWAY, "secret_gateway"],
]);

/**
 * Puerta de capacidad previa al envío: la primera sección cuyo propio
 * `requiredFlag` esté en `false` en `features` lanza `UnimplementedError`
 * nombrándola, antes de construir un solo `ConfigureRequest`. Se llama
 * justo tras el primer `Health`, dentro del mismo `try`/`catch` que
 * `Sandbox.#open` ya usa para terminar el sandbox ante cualquier fallo
 * anterior a `agentReady` (salvo `keepOnFailure`): esta puerta reutiliza
 * esa terminación, no implementa la suya propia.
 */
export function requireCapabilities(
  sections: readonly ConfigureSection[],
  features: AgentFeatures,
): void {
  for (const entry of sections) {
    if (!features[entry.requiredFlag]) {
      throw new UnimplementedError(
        entry.section,
        "esta imagen no tiene un adaptador real para esta función " +
          "(agente anterior a 0.6.0, o variante de imagen sin el caps que necesita)",
        CONFIGURE_DOC,
      );
    }
  }
}

/**
 * Un único `ConfigureRequest` con todas las secciones de `sections`
 * rellenas; `requestId` es nuevo en cada llamada (idempotencia nunca
 * pedida por `create()`, que sólo llama una vez por sandbox).
 */
export function buildConfigureRequest(sections: readonly ConfigureSection[]): ConfigureRequest {
  const request = create(ConfigureRequestSchema, { requestId: randomUUID() });
  for (const entry of sections) {
    entry.fill(request);
  }
  return request;
}

/** Cada `SectionResult` de `response` junto a su sección; un resultado para
 * una sección que `sections` no contiene (no debería ocurrir: el agente
 * sólo responde por lo que `ConfigureRequest` llevaba) se ignora en vez de
 * fallar de forma opaca. */
function sectionsByResult(
  response: ConfigureResponse,
  sections: readonly ConfigureSection[],
): [ConfigureSection, SectionResult][] {
  const byName = new Map(sections.map((entry) => [entry.section, entry] as const));
  const paired: [ConfigureSection, SectionResult][] = [];
  for (const result of response.results) {
    const name = WIRE_SECTION_NAMES.get(result.section);
    const entry = name === undefined ? undefined : byName.get(name);
    if (entry !== undefined) {
      paired.push([entry, result]);
    }
  }
  return paired;
}

/**
 * Traduce cada `SectionResult` de `response` al error de su propia sección
 * (`ConfigureSection.checkResult`) y devuelve las que quedaron en
 * `PENDING`: `create()` no vuelve hasta verlas asentarse (`stillPending`).
 */
export function checkConfigureResponse(
  response: ConfigureResponse,
  sections: readonly ConfigureSection[],
): ConfigureSection[] {
  const pending: ConfigureSection[] = [];
  for (const [entry, result] of sectionsByResult(response, sections)) {
    entry.checkResult(result.code, result.errorClass);
    if (result.code === SectionCode.PENDING) {
      pending.push(entry);
    }
  }
  return pending;
}

/** El mayor `settleTimeoutMs` de las secciones pendientes: todas se
 * sondean juntas en la misma `ConfigureStatus`. */
export function settleTimeoutMs(pending: readonly ConfigureSection[]): number {
  return pending.reduce((longest, entry) => Math.max(longest, entry.settleTimeoutMs), 0);
}

/** Las secciones de `pending` que `status` todavía no da por asentadas;
 * cada una lanza su propio error si falló, o si `final` y sigue sin
 * asentarse. */
export function stillPending(
  status: ConfigureStatusResponse,
  pending: readonly ConfigureSection[],
  final: boolean,
): ConfigureSection[] {
  return pending.filter((entry) => !entry.checkStatus(status, final));
}
