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
} from "./gen/rayito/v1/configure_pb.js";
import type { AgentFeatures } from "./gen/rayito/v1/features_pb.js";
import type { HealthResponse } from "./gen/rayito/v1/health_pb.js";

export const CONFIGURE_DOC = "docs/site/docs/funciones-opcionales/pilas-opcionales.md";

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
 * `ConfigureRequest` compartido y cómo traduce el `SectionResult` que le
 * corresponde en su propio error.
 */
export interface ConfigureSection {
  readonly section: string;
  readonly requiredFlag: keyof AgentFeatures;
  fill(request: ConfigureRequest): void;
  checkResult(code: number, errorClass: string): void;
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

/**
 * Traduce cada `SectionResult` de `response` al error de su propia sección
 * (`ConfigureSection.checkResult`); un resultado para una sección que
 * `sections` no contiene (no debería ocurrir: el agente sólo responde por
 * lo que `ConfigureRequest` llevaba) se ignora en vez de fallar de forma
 * opaca.
 */
export function checkConfigureResponse(
  response: ConfigureResponse,
  sections: readonly ConfigureSection[],
): void {
  const byName = new Map(sections.map((entry) => [entry.section, entry] as const));
  for (const result of response.results) {
    const name = WIRE_SECTION_NAMES.get(result.section);
    const entry = name === undefined ? undefined : byName.get(name);
    if (entry === undefined) {
      continue;
    }
    entry.checkResult(result.code, result.errorClass);
  }
}
