/**
 * Adapts `TelemetryExport` to `ConfigureRequest.telemetryExport` (M15,
 * m15-rayd-otlp). Pure except `resolveBearerToken`, the only function in
 * this module that calls AWS (`secretsmanager:GetSecretValue`, and only
 * with `OtlpAuth.bearer(...)`): built already with the image facts (only
 * known after `run-microvm`) and, if applicable, the bearer's value already
 * resolved -- never its secret name, and never in a log or an exception.
 *
 * `requireTelemetrySupport` is the gate `Sandbox.#applyTelemetry` uses,
 * split out so its message can never drift from a second copy.
 */

import { create } from "@bufbuild/protobuf";
import type { AwsClientSettings } from "../aws/control-plane.js";
import { loadOptionalSdkClient } from "../aws/optional-client.js";
import type { ConfigureSection } from "../configure/base.js";
import { requireConfigureSupport } from "../configure/base.js";
import { SecretError, UnimplementedError } from "../errors.js";
import type { ConfigureRequest } from "../gen/rayito/v1/configure_pb.js";
import type { AgentFeatures } from "../gen/rayito/v1/features_pb.js";
import {
  TelemetryExportBearerAuthSchema,
  TelemetryExportConfigSchema,
  TelemetryExportExecutionRoleAuthSchema,
  TelemetryExportNameStyle,
} from "../gen/rayito/v1/telemetry_export_pb.js";
import type { NameStyleOption, TelemetryExport } from "./domain.js";

const SECRETS_MANAGER_PEER = "@aws-sdk/client-secrets-manager";

const NAMES_WIRE: Record<NameStyleOption, TelemetryExportNameStyle> = {
  rayito: TelemetryExportNameStyle.RAYITO,
  e2b: TelemetryExportNameStyle.E2B,
};

const TELEMETRY_FEATURE_NAME = "telemetry";
const TELEMETRY_DOC = "docs/site/docs/funciones-opcionales/exportacion-otlp.md";

/**
 * Throws `UnimplementedError` unless the running agent reports
 * `telemetryExport: true`: absent entirely (an agent older than 0.6.0, via
 * `requireConfigureSupport`) or present but `false` (a 0.6.0 image without
 * the OTLP exporter implemented, `features::slot::Unsupported` on `rayd`'s
 * side). Shared by `Sandbox.#applyTelemetry` so the gate and its message
 * never drift from a second copy.
 */
export function requireTelemetrySupport(features: AgentFeatures | undefined): void {
  const resolved = requireConfigureSupport(features, TELEMETRY_FEATURE_NAME);
  if (!resolved.telemetryExport) {
    throw new UnimplementedError(
      TELEMETRY_FEATURE_NAME,
      "esta imagen no tiene el exportador OTLP implementado todavía " +
        "(pendiente de medición, docs/research/2026-10-e2b-out-of-scope.md §6)",
      TELEMETRY_DOC,
    );
  }
}

interface SecretsManagerModule {
  readonly SecretsManagerClient: new (
    config: object,
  ) => { send(command: unknown): Promise<unknown> };
  readonly GetSecretValueCommand: new (input: object) => unknown;
}

/**
 * Resolves `secretName`'s value once, when the section is sent (not
 * cached: unlike `SecretCache`, the value goes straight to `rayd` and is
 * not read again until the next `ConfigureSandbox`). The error message
 * never repeats the secret's name or its value.
 */
export async function resolveBearerToken(
  secretName: string,
  region: string,
  credentials: AwsClientSettings["credentials"],
): Promise<string> {
  const { sdk, send } = await loadOptionalSdkClient<SecretsManagerModule>(
    SECRETS_MANAGER_PEER,
    "telemetry (OtlpAuth.bearer)",
    (module) => module.SecretsManagerClient,
    region,
    credentials,
  );
  let response: { SecretString?: string };
  try {
    response = await send(new sdk.GetSecretValueCommand({ SecretId: secretName }));
  } catch (error) {
    throw new SecretError("no se pudo leer el secreto de telemetry", { cause: error });
  }
  const value = response.SecretString;
  if (!value) {
    throw new SecretError("el secreto de telemetry no tiene SecretString");
  }
  return value;
}

export class TelemetryExportSection implements ConfigureSection {
  readonly section = "telemetry_export";
  readonly requiredFlag = "telemetry_export";

  constructor(
    private readonly telemetry: TelemetryExport,
    private readonly imageArn: string,
    private readonly imageVersion: string,
    private readonly imageMemoryMib: number,
    private readonly bearerToken: string | undefined,
  ) {}

  fill(request: ConfigureRequest): void {
    request.telemetryExport = create(TelemetryExportConfigSchema, {
      intervalS: this.telemetry.intervalS,
      serviceName: this.telemetry.serviceName,
      names: NAMES_WIRE[this.telemetry.names],
      imageArn: this.imageArn,
      imageVersion: this.imageVersion,
      imageMemoryMib: this.imageMemoryMib,
      auth:
        this.bearerToken === undefined
          ? { case: "executionRole", value: create(TelemetryExportExecutionRoleAuthSchema, {}) }
          : {
              case: "bearer",
              value: create(TelemetryExportBearerAuthSchema, { token: this.bearerToken }),
            },
    });
  }
}

export interface BuildSectionOptions {
  readonly imageArn: string;
  readonly imageVersion: string;
  readonly imageMemoryMib: number;
  readonly region: string;
  readonly credentials: AwsClientSettings["credentials"];
}

/** Resolves `OtlpAuth.bearer` (if applicable) and builds the section ready
 * for `fill()`. Never called with `telemetry` absent. */
export async function buildSection(
  telemetry: TelemetryExport,
  options: BuildSectionOptions,
): Promise<TelemetryExportSection> {
  const bearerToken =
    telemetry.auth.kind === "bearer" && telemetry.auth.secretName !== undefined
      ? await resolveBearerToken(telemetry.auth.secretName, options.region, options.credentials)
      : undefined;
  return new TelemetryExportSection(
    telemetry,
    options.imageArn,
    options.imageVersion,
    options.imageMemoryMib,
    bearerToken,
  );
}
