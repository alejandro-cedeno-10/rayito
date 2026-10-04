/**
 * Adapts `TelemetryExport` to `ConfigureRequest.telemetryExport` (M15,
 * m15-rayd-otlp). `TelemetryExportSection` is pure: built with the image
 * facts (only known after `run-microvm`) and, if applicable, the bearer's
 * value already resolved -- never its secret name, and never in a log, an
 * `inspect()` or an exception.
 *
 * `buildSection` reads `OtlpAuth.bearer(...)`'s secret through the handle's
 * shared `SecretCache` (the same one `secrets` uses, so the same
 * `SecretStore`): the same `rayito/` prefix rule (`resolveSecretId`), the
 * same error translation and at most one `GetSecretValue` per TTL. That is
 * this module's only AWS call, and only with `OtlpAuth.bearer(...)`.
 *
 * `requireTelemetrySupport` is the section's own capability gate
 * (`configure/base.ts`'s `CapabilityGate`): `create()` sends the section
 * through the same `#applyConfigureSections` as `mounts`/`gateways`, from
 * a `TelemetrySectionFactory` (the image facts are only known after
 * `run-microvm` and the first `Health`).
 */

import { create } from "@bufbuild/protobuf";
import {
  type AgentFeatures,
  type CapabilityGate,
  type ConfigureSectionFactory,
  ImmediateSection,
  requireConfigureSupport,
} from "../configure/base.js";
import { UnimplementedError } from "../errors.js";
import type { ConfigureRequest } from "../gen/rayito/v1/configure_pb.js";
import {
  TelemetryExportBearerAuthSchema,
  TelemetryExportConfigSchema,
  TelemetryExportExecutionRoleAuthSchema,
  TelemetryExportNameStyle,
} from "../gen/rayito/v1/telemetry_export_pb.js";
import type { SecretCache } from "../secrets/cache.js";
import {
  imageMemoryMibFromGuestBytes,
  type NameStyleOption,
  type TelemetryExport,
} from "./domain.js";

const NAMES_WIRE: Record<NameStyleOption, TelemetryExportNameStyle> = {
  rayito: TelemetryExportNameStyle.RAYITO,
  e2b: TelemetryExportNameStyle.E2B,
};

const TELEMETRY_FEATURE_NAME = "telemetry";
const TELEMETRY_DOC = "docs/site/docs/funciones-opcionales/exportacion-otlp.md";
/** The first `rayd` that ships the OTLP exporter (m15-rayd-otlp). */
const REQUIRED_AGENT = "rayd 0.6.0";

/**
 * Throws `UnimplementedError` unless the running agent reports
 * `telemetryExport: true`: absent entirely (an agent older than 0.6.0, via
 * `requireConfigureSupport`) or present but `false` (a 0.6 `rayd` started
 * without `AWS_REGION`, so it has no CloudWatch endpoint to export to).
 * `TelemetryExportSection.requireSupport`.
 */
export function requireTelemetrySupport(features: AgentFeatures | undefined): void {
  const resolved = requireConfigureSupport(features, TELEMETRY_FEATURE_NAME);
  if (!resolved.telemetryExport) {
    throw new UnimplementedError(
      TELEMETRY_FEATURE_NAME,
      `el rayd de esta imagen no admite exportar telemetría: es anterior a ${REQUIRED_AGENT} ` +
        "o arrancó sin AWS_REGION. Usa una imagen rayito-base (o rayito-base-caps para " +
        `OtlpAuth.executionRole()) publicada con ${REQUIRED_AGENT} o posterior`,
      TELEMETRY_DOC,
    );
  }
}

/**
 * `OtlpAuth.bearer(...)`'s secret value, read through `cache` (its
 * `rayito/` prefix and TTL), or `undefined` with `OtlpAuth.executionRole()`.
 * Errors are `SecretCache.get`'s: they never repeat the name or the value.
 */
export async function resolveBearerToken(
  telemetry: TelemetryExport,
  cache: SecretCache,
): Promise<string | undefined> {
  const { kind, secretName } = telemetry.auth;
  return kind === "bearer" && secretName !== undefined ? cache.get(secretName) : undefined;
}

export class TelemetryExportSection extends ImmediateSection implements CapabilityGate {
  readonly section = "telemetry_export";
  readonly requiredFlag = "telemetryExport";
  /** An ES private field, never a plain property: `util.inspect`,
   * `console.log` and `JSON.stringify` cannot see it. */
  readonly #bearerToken: string | undefined;

  constructor(
    private readonly telemetry: TelemetryExport,
    private readonly imageArn: string,
    private readonly imageVersion: string,
    private readonly imageMemoryMib: number,
    bearerToken: string | undefined,
  ) {
    super();
    this.#bearerToken = bearerToken;
  }

  requireSupport(features: AgentFeatures): void {
    requireTelemetrySupport(features);
  }

  /** Whether a bearer token was resolved (never the token itself). */
  get hasBearerToken(): boolean {
    return this.#bearerToken !== undefined;
  }

  fill(request: ConfigureRequest): void {
    request.telemetryExport = create(TelemetryExportConfigSchema, {
      intervalS: this.telemetry.intervalS,
      serviceName: this.telemetry.serviceName,
      names: NAMES_WIRE[this.telemetry.names],
      imageArn: this.imageArn,
      imageVersion: this.imageVersion,
      imageMemoryMib: this.imageMemoryMib,
      auth:
        this.#bearerToken === undefined
          ? { case: "executionRole", value: create(TelemetryExportExecutionRoleAuthSchema, {}) }
          : {
              case: "bearer",
              value: create(TelemetryExportBearerAuthSchema, { token: this.#bearerToken }),
            },
    });
  }
}

export interface BuildSectionOptions {
  readonly imageArn: string;
  readonly imageVersion: string;
  readonly imageMemoryMib: number;
  /** The handle's shared `SecretCache` (`sharedSecretCache(region, credentials)`). */
  readonly secretCache: SecretCache;
}

/** Resolves `OtlpAuth.bearer` (if applicable, `resolveBearerToken`) and
 * builds the section ready for `fill()`. Never called with `telemetry`
 * absent. */
export async function buildSection(
  telemetry: TelemetryExport,
  options: BuildSectionOptions,
): Promise<TelemetryExportSection> {
  return new TelemetryExportSection(
    telemetry,
    options.imageArn,
    options.imageVersion,
    options.imageMemoryMib,
    await resolveBearerToken(telemetry, options.secretCache),
  );
}

/** What only `run-microvm` and the first `Health` know. */
export interface TelemetryLaunchFacts {
  readonly imageArn: string;
  readonly imageVersion: string;
  readonly guestMemoryBytes: number | undefined;
}

/**
 * `configure/base.ts`'s `ConfigureSectionFactory` for `telemetry`: the
 * image facts already fixed; `OtlpAuth.bearer(...)`'s secret is read
 * through the handle's `SecretCache` when `resolveSections` builds it,
 * right before the `Configure`.
 */
export class TelemetrySectionFactory implements ConfigureSectionFactory {
  constructor(
    private readonly telemetry: TelemetryExport,
    private readonly facts: TelemetryLaunchFacts,
  ) {}

  build(cache: SecretCache): Promise<TelemetryExportSection> {
    return buildSection(this.telemetry, {
      imageArn: this.facts.imageArn,
      imageVersion: this.facts.imageVersion,
      imageMemoryMib: imageMemoryMibFromGuestBytes(this.facts.guestMemoryBytes),
      secretCache: cache,
    });
  }
}
