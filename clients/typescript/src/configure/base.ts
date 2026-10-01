/**
 * Pure half of `ConfigureSandbox` (M15 foundations, ADR-015). Mirrors
 * `rayito._configure_base`: what `Health.features` says about an agent, and
 * how one section's `SectionResult` becomes an exception. The RPC call
 * itself lives in `configure/rpc.ts`; this module imports no transport.
 */

import { SandboxError, UnimplementedError } from "../errors.js";
import type { ConfigureRequest } from "../gen/rayito/v1/configure_pb.js";
import { SectionCode } from "../gen/rayito/v1/configure_pb.js";
import type { AgentFeatures } from "../gen/rayito/v1/features_pb.js";
import type { HealthResponse } from "../gen/rayito/v1/health_pb.js";

export const CONFIGURE_DOC = "docs/site/docs/funciones-opcionales/pilas-opcionales.md";

/**
 * `undefined` when the agent predates 0.6.0 (proto3 message-field
 * presence: `response.features` is `undefined`, not a zeroed message) —
 * not the same as "0.6.0, but this particular feature's flag is `false`".
 */
export function agentFeaturesFromHealth(response: HealthResponse): AgentFeatures | undefined {
  return response.features;
}

/**
 * Throws `UnimplementedError` naming the 0.6.0 image when `features` is
 * `undefined` (pre-`ConfigureService` agent); otherwise returns it as-is so
 * the caller can still check its own flag.
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
 * What a 0.6 function implements to take part in one `Configure` call: its
 * section's name (for logs and ordering), the `AgentFeatures` flag it
 * needs, and how it fills its own field of the shared `ConfigureRequest`.
 */
export interface ConfigureSection {
  readonly section: string;
  readonly requiredFlag: string;
  fill(request: ConfigureRequest): void;
}

/**
 * `undefined` when the section applied or is still settling (`PENDING`);
 * otherwise the error describing why not.
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
  const reason = errorClass || SectionCode[code]?.toLowerCase() || "unknown";
  return new SandboxError(`${section}: ${reason}`);
}
