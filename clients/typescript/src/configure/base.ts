/**
 * Parte pura de `ConfigureSandbox` (ADR-015): qué dice `Health.features`
 * sobre un agente, cómo se traduce el resultado de una sección de
 * `Configure` a su excepción y qué recibe una sección tras aplicarse
 * (`PostApplySection`). Espejo de `rayito._configure_base`. Las llamadas
 * gRPC viven en `sandbox/sandbox.ts` (vía los clientes Connect-ES de
 * `ConfigureService`); este módulo no importa `@connectrpc/connect`.
 *
 * Vive en `configure/` (la ruta que la arquitectura de 0.6 reserva al seam
 * de foundations): foundations aún no lo había creado para TypeScript, y
 * otras funciones 0.6 abiertas en paralelo crean el mismo módulo; quien
 * fusione después las reconcilia en este fichero.
 */

import { SandboxError, UnimplementedError } from "../errors.js";
import {
  ConfigSection,
  type ConfigureRequest,
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

/** Lo que una función 0.6 implementa para participar en una sola llamada a
 * `Configure`: el nombre de su sección (para el orden) y el flag de
 * `AgentFeatures` que debe estar activo, y cómo rellena su campo del
 * `ConfigureRequest` compartido. */
export interface ConfigureSection {
  readonly section: string;
  readonly requiredFlag: keyof AgentFeatures;
  fill(request: ConfigureRequest): void | Promise<void>;
}

/**
 * Lo que `planFeatures` pone en `FeaturePlan.configureSections`: un
 * `ConfigureSection` todavía sin resolver, a la espera de la `SecretCache`
 * que `create()`/`take()` ya calculan para `secrets` (ver
 * `GatewaySectionFactory`).
 */
export interface ConfigureSectionFactory {
  build(cache: SecretCache): ConfigureSection;
}

/**
 * Lo que `PostApplySection.afterApply` recibe una vez su `Configure` se
 * aplicó: el `ConfigureStatus` de ese momento y cómo volver a mandar esa
 * misma sección más tarde (`fill` otra vez, `Configure`, `raiseForResults`
 * y el `ConfigureStatus` nuevo). `sandbox.ts` lo construye igual para
 * cualquier función, sin saber cuál es.
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
export function sectionError(section: string, result: SectionResult): Error | undefined {
  if (result.code === SectionCode.APPLIED || result.code === SectionCode.PENDING) {
    return undefined;
  }
  if (result.code === SectionCode.UNSUPPORTED) {
    return new UnimplementedError(
      section,
      "esta imagen no tiene esta función implementada todavía",
      CONFIGURE_DOC,
    );
  }
  const reason = result.errorClass || codeName(result.code);
  return new SandboxError(`${section}: ${reason}`);
}

/**
 * Traduce cada `SectionResult` de `response` con `sectionError` y lanza el
 * primero que no sea `undefined`. El único punto por el que deben pasar
 * tanto el `Configure` agrupado que manda `create()`/`take()` como una
 * llamada posterior de una sola sección (`SectionApplied.reapply`): los
 * dos deben fallar exactamente igual ante `INVALID`/`FAILED`/`UNSUPPORTED`,
 * nunca sólo uno de ellos en silencio.
 */
export function raiseForResults(response: ConfigureResponse): void {
  for (const result of response.results) {
    const error = sectionError(configSectionName(result.section), result);
    if (error !== undefined) {
      throw error;
    }
  }
}

/** Nombre snake_case de `result.section`, como cada feature nombra su
 * propia `ConfigureSection.section` (mirroring Python's
 * `ConfigSection.Name(...)`, que protobuf-es no genera). */
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

export { SectionCode };
