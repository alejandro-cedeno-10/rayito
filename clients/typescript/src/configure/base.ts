/**
 * Parte pura de `ConfigureSandbox` (ADR-015): qué dice `Health.features`
 * sobre un agente, cómo se resuelve, se rellena y se aplica cada sección
 * de `Configure` (capacidad, una única llamada, resultado por sección y la
 * espera acotada a las secciones `PENDING`) y qué recibe una sección tras
 * aplicarse (`PostApplySection`). Espejo de `rayito._configure_base`. Las
 * llamadas gRPC viven en `sandbox/sandbox.ts` (vía los clientes Connect-ES
 * de `ConfigureService`); este módulo no importa `@connectrpc/connect`.
 */

import { randomUUID } from "node:crypto";
import { create } from "@bufbuild/protobuf";

import { SandboxError, UnimplementedError } from "../errors.js";
import {
  ConfigSection,
  type ConfigureRequest,
  ConfigureRequestSchema,
  type ConfigureResponse,
  type ConfigureStatusResponse,
  SectionCode,
  type SectionResult,
} from "../gen/rayito/v1/configure_pb.js";
import type { AgentFeatures as WireAgentFeatures } from "../gen/rayito/v1/features_pb.js";
import type { HealthResponse } from "../gen/rayito/v1/health_pb.js";
import type { SecretCache } from "../secrets/cache.js";

export const CONFIGURE_DOC = "docs/site/docs/funciones-opcionales/pilas-opcionales.md";
export const CONFIGURE_FEATURE = "ConfigureSandbox";

/** Cadencia del sondeo de `ConfigureStatus` mientras alguna sección sigue
 * `PENDING` tras `Configure`: un montaje local responde en decenas de
 * milisegundos una vez listo, así que un cuarto de segundo no añade
 * latencia apreciable a `create()` ni martillea al agente. Espejo de
 * `CONFIGURE_SETTLE_POLL_S` de Python. */
export const CONFIGURE_SETTLE_POLL_MS = 250;

/** Espejo de `AgentFeatures` (`features.proto`): qué funciones 0.6 soporta
 * el agente en ejecución. Ningún flag es secreto. */
export interface AgentFeatures {
  readonly configure: boolean;
  readonly s3Mounts: boolean;
  readonly efsVolumes: boolean;
  readonly lifecycleEvents: boolean;
  readonly telemetryExport: boolean;
  readonly secretGateway: boolean;
  readonly templateStart: boolean;
}

function agentFeaturesFromWire(features: WireAgentFeatures): AgentFeatures {
  return {
    configure: features.configure,
    s3Mounts: features.s3Mounts,
    efsVolumes: features.efsVolumes,
    lifecycleEvents: features.lifecycleEvents,
    telemetryExport: features.telemetryExport,
    secretGateway: features.secretGateway,
    templateStart: features.templateStart,
  };
}

/**
 * `undefined` cuando el agente es anterior a 0.6.0 (el campo `features` está
 * ausente de verdad, presencia de mensaje de proto3: no basta con mirar los
 * flags, que todos valdrían `false` igual que en un 0.6.0 sin ninguna función
 * real todavía). Quien llama exige 0.6.0 con `requireConfigureSupport` antes
 * de usar cualquier flag.
 */
export function agentFeaturesFromHealth(response: HealthResponse): AgentFeatures | undefined {
  return response.features === undefined ? undefined : agentFeaturesFromWire(response.features);
}

/**
 * `UnimplementedError` nombrando la imagen 0.6.0 si `features` es
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
  fill(request: ConfigureRequest): void | Promise<void>;
  checkResult(code: number, errorClass: string): void;
  checkStatus(status: ConfigureStatusResponse, final: boolean): boolean;
}

/**
 * Una entrada de `FeaturePlan.configureSections` que todavía no es un
 * `ConfigureSection`: necesita la `SecretCache` que `create()`/`take()` ya
 * calculan para `secrets` (la misma, nunca una segunda). Hoy
 * `GatewaySectionFactory`; `resolveSections` la construye justo antes de
 * la llamada a `Configure`.
 */
export interface ConfigureSectionFactory {
  build(cache: SecretCache): ConfigureSection;
}

/** Lo que `planFeatures` pone en `FeaturePlan.configureSections`: una
 * sección ya lista (`mounts`) o una que espera la `SecretCache`
 * (`gateways`). */
export type PlannedSection = ConfigureSection | ConfigureSectionFactory;

function isSectionFactory(entry: PlannedSection): entry is ConfigureSectionFactory {
  return typeof (entry as Partial<ConfigureSectionFactory>).build === "function";
}

/** Cada entrada de `planned` como `ConfigureSection`: las que ya lo son tal
 * cual, cada `ConfigureSectionFactory` construida con `cache()` — que sólo
 * se llama si alguna entrada la necesita. */
export function resolveSections(
  planned: readonly PlannedSection[],
  cache: () => SecretCache,
): ConfigureSection[] {
  return planned.map((entry) => (isSectionFactory(entry) ? entry.build(cache()) : entry));
}

/**
 * Lo que `PostApplySection.afterApply` recibe una vez su `Configure` se
 * aplicó: el `ConfigureStatus` de ese momento y cómo volver a mandar esa
 * misma sección más tarde (`fill` otra vez, `Configure`,
 * `checkConfigureResponse` y el `ConfigureStatus` nuevo). `sandbox.ts` lo
 * construye igual para cualquier función, sin saber cuál es.
 */
export interface SectionApplied {
  readonly status: ConfigureStatusResponse;
  readonly reapply: () => Promise<ConfigureStatusResponse>;
}

/**
 * Un `ConfigureSection` que además necesita algo después de aplicarse (hoy
 * `gateways`: leer los puertos de `ConfigureStatus` y poder rotar).
 * `create()`/`take()` piden `ConfigureStatus` una sola vez si alguna
 * sección lo implementa y guardan lo que devuelve `afterApply` bajo su
 * `section`; la propiedad pública de esa función (`sbx.gateways`, ...) lo
 * lee de ahí. Así `sandbox.ts` nunca nombra una función concreta.
 */
export interface PostApplySection extends ConfigureSection {
  afterApply(applied: SectionApplied): unknown;
}

export function isPostApplySection(section: ConfigureSection): section is PostApplySection {
  return typeof (section as Partial<PostApplySection>).afterApply === "function";
}

/**
 * `undefined` cuando la sección se aplicó o sigue asentándose (`PENDING`);
 * en otro caso el error que describe por qué no.
 */
export function sectionError(
  section: string,
  code: SectionCode,
  errorClass: string,
): Error | undefined {
  if (code === SectionCode.APPLIED || code === SectionCode.PENDING) {
    return undefined;
  }
  if (code === SectionCode.UNSUPPORTED) {
    return new UnimplementedError(
      section,
      "esta imagen no tiene esta función implementada todavía",
      CONFIGURE_DOC,
    );
  }
  const reason = errorClass || codeName(code);
  return new SandboxError(`${section}: ${reason}`);
}

/**
 * `ConfigureSection.checkResult` para una sección sin error propio (hoy
 * `gateways`): lanza lo que `sectionError` diga, y nada si se aplicó o
 * sigue `PENDING`. Así un `INVALID`/`FAILED`/`UNSUPPORTED` nunca pasa en
 * silencio, ni en el `Configure` de `create()`/`take()` ni en uno
 * posterior de una sola sección (`SectionApplied.reapply`), que pasan los
 * dos por `checkConfigureResponse`.
 */
export function raiseSectionError(section: string, code: SectionCode, errorClass: string): void {
  const error = sectionError(section, code, errorClass);
  if (error !== undefined) {
    throw error;
  }
}

/** Nombre snake_case de `section`, como cada función nombra su propia
 * `ConfigureSection.section` (mirroring Python's `ConfigSection.Name(...)`,
 * que protobuf-es no genera). */
export function configSectionName(section: ConfigSection): string {
  switch (section) {
    case ConfigSection.S3_MOUNTS:
      return "s3_mounts";
    case ConfigSection.EFS_VOLUMES:
      return "efs_volumes";
    case ConfigSection.LIFECYCLE_EVENTS:
      return "lifecycle_events";
    case ConfigSection.TELEMETRY_EXPORT:
      return "telemetry_export";
    case ConfigSection.SECRET_GATEWAY:
      return "secret_gateway";
    default:
      return "unspecified";
  }
}

function codeName(code: SectionCode): string {
  switch (code) {
    case SectionCode.INVALID:
      return "invalid";
    case SectionCode.FAILED:
      return "failed";
    default:
      return "unspecified";
  }
}

/**
 * Puerta de capacidad previa al envío: la primera sección cuyo propio
 * `requiredFlag` esté en `false` en `features` lanza `UnimplementedError`
 * nombrándola, antes de construir un solo `ConfigureRequest`.
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
 * rellenas (`fill` puede ser asíncrono: `gateways` resuelve cada cabecera
 * de la `SecretCache` ahí); `requestId` es nuevo en cada llamada.
 */
export async function buildConfigureRequest(
  sections: readonly ConfigureSection[],
): Promise<ConfigureRequest> {
  const request = create(ConfigureRequestSchema, { requestId: randomUUID() });
  for (const entry of sections) {
    await entry.fill(request);
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
    const entry = byName.get(configSectionName(result.section));
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

export { SectionCode };
