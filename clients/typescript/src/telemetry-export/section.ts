/**
 * Adapts `TelemetryExport` to `ConfigureRequest.telemetryExport` (M15,
 * m15-rayd-otlp). Pure except `resolveBearerToken`, the only function in
 * this module that calls AWS (`secretsmanager:GetSecretValue`, and only
 * with `OtlpAuth.bearer(...)`): built already with the image facts (only
 * known after `run-microvm`) and, if applicable, the bearer's value already
 * resolved -- never its secret name, and never in a log or an exception.
 */

import { create } from "@bufbuild/protobuf";
import type { AwsClientSettings } from "../aws/control-plane.js";
import { loadOptionalSdkClient } from "../aws/optional-client.js";
import type { ConfigureSection } from "../configure/base.js";
import { SecretError } from "../errors.js";
import type { ConfigureRequest } from "../gen/rayito/v1/configure_pb.js";
import {
  BearerAuthSchema,
  ExecutionRoleAuthSchema,
  NameStyle,
  TelemetryExportConfigSchema,
} from "../gen/rayito/v1/telemetry_export_pb.js";
import type { NameStyleOption, TelemetryExport } from "./domain.js";

const SECRETS_MANAGER_PEER = "@aws-sdk/client-secrets-manager";

const NAMES_WIRE: Record<NameStyleOption, NameStyle> = {
  rayito: NameStyle.RAYITO,
  e2b: NameStyle.E2B,
};

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
          ? { case: "executionRole", value: create(ExecutionRoleAuthSchema, {}) }
          : { case: "bearer", value: create(BearerAuthSchema, { token: this.bearerToken }) },
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
