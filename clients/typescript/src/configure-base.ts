/**
 * Parte pura de `ConfigureSandbox` (ADR-015): qué dice `Health.features`
 * sobre un agente, y cómo se traduce el resultado de una sección de
 * `Configure` a su excepción. Espejo de `rayito._configure_base`. Las
 * llamadas gRPC viven en `sandbox/core.ts` (vía los clientes Connect-ES de
 * `ConfigureService`); este módulo no importa `@connectrpc/connect`.
 */

import { SandboxError, UnimplementedError } from "./errors.js";
import {
  ConfigSection,
  type ConfigureRequest,
  SectionCode,
  type SectionResult,
} from "./gen/rayito/v1/configure_pb.js";
import type { AgentFeatures as WireAgentFeatures } from "./gen/rayito/v1/features_pb.js";
import type { HealthResponse } from "./gen/rayito/v1/health_pb.js";

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
