/**
 * Todo lo que `Sandbox.create()` valida y construye antes de tocar la red:
 * template, access token, `timeoutMs`, política de idle, puertos del proxy,
 * conectores, logging y el `LaunchRequest`. Sin I/O.
 */

import { randomUUID } from "node:crypto";
import { LaunchRequest, type LoggingConfig, PortSpec } from "../aws/control-plane.js";
import { AuthenticationError, InvalidArgumentError, SandboxLifetimeError } from "../errors.js";
import {
  DEFAULT_PORT,
  HOOKS_PORT,
  MANAGED_NETWORK_CONNECTORS,
  MAX_DURATION_SECONDS,
  MICROVM_ID_MAX_LENGTH,
  MICROVM_ID_MIN_LENGTH,
  MIN_DURATION_SECONDS,
  NETWORK_CONNECTORS_MAX,
} from "../limits.js";
import {
  type IdlePolicy,
  type IdlePolicyInput,
  templateNameFromArn,
  validateIdlePolicy,
  validatePort,
} from "../models.js";
import { buildRunHookPayload, generateAccessToken, validateAccessToken } from "../payload.js";

export const TEMPLATE_ENV_VAR = "RAYITO_TEMPLATE";
export const ACCESS_TOKEN_ENV_VAR = "RAYITO_ACCESS_TOKEN";
export const DEFAULT_TIMEOUT_MS = 3_600_000;
export const DEFAULT_READY_TIMEOUT_MS = 90_000;
export const DEFAULT_REQUEST_TIMEOUT_MS = 60_000;
export const DEFAULT_RECONNECT_TIMEOUT_MS = 60_000;
export const LOG_GROUP_PREFIX = "/rayito";
export const MAX_DURATION_MS = MAX_DURATION_SECONDS * 1000;
export const MIN_DURATION_MS = MIN_DURATION_SECONDS * 1000;

export type LoggingOption = "disabled" | "cloudwatch" | LoggingConfig;
export type PortLike = number | readonly [number, number];

/** Todo lo que `create()` necesita antes de tocar la red. */
export interface LaunchPlan {
  readonly accessToken: string;
  readonly request: LaunchRequest;
  readonly proxyPorts: readonly PortSpec[];
}

export function resolveTemplate(template: string | undefined): string {
  const resolved = template || process.env[TEMPLATE_ENV_VAR];
  if (!resolved) {
    throw new InvalidArgumentError(
      `falta el template: pasa \`template\` o define ${TEMPLATE_ENV_VAR}`,
    );
  }
  return resolved;
}

/**
 * Token explícito, `RAYITO_ACCESS_TOKEN` o uno nuevo; siempre base64url
 * canónico, que es lo único que `rayd` acepta en `x-access-token`.
 */
export function resolveAccessToken(accessToken: string | undefined): string {
  const provided = accessToken || process.env[ACCESS_TOKEN_ENV_VAR];
  if (!provided) {
    return generateAccessToken();
  }
  return validateAccessToken(provided);
}

export function requireAccessToken(accessToken: string | undefined): string {
  const resolved = accessToken || process.env[ACCESS_TOKEN_ENV_VAR];
  if (!resolved) {
    throw new AuthenticationError(
      `connect() necesita el accessToken del sandbox: pásalo o define ${ACCESS_TOKEN_ENV_VAR}`,
    );
  }
  return validateAccessToken(resolved);
}

/** Sólo por longitud: el prefijo real es `microvm-` pero no se parsea. */
export function validateSandboxId(sandboxId: unknown): string {
  if (
    typeof sandboxId !== "string" ||
    sandboxId.length < MICROVM_ID_MIN_LENGTH ||
    sandboxId.length > MICROVM_ID_MAX_LENGTH
  ) {
    throw new InvalidArgumentError(`sandboxId inválido: ${JSON.stringify(sandboxId)}`);
  }
  return sandboxId;
}

/** `maximumDurationInSeconds = ceil(timeoutMs / 1000)` tras validar `1 000 <= timeoutMs <= 28 800 000`. */
export function validateTimeoutMs(timeoutMs: unknown): number {
  if (typeof timeoutMs !== "number" || !Number.isInteger(timeoutMs)) {
    throw new InvalidArgumentError(
      `timeoutMs debe ser un entero en milisegundos, recibido ${String(timeoutMs)}`,
    );
  }
  if (timeoutMs > MAX_DURATION_MS) {
    throw new SandboxLifetimeError(
      `timeoutMs=${timeoutMs} supera el tope de ${MAX_DURATION_MS} ms (8 h, running + ` +
        "suspended); la vida de un MicroVM no se puede extender después",
    );
  }
  if (timeoutMs < MIN_DURATION_MS) {
    throw new InvalidArgumentError(
      `timeoutMs debe ser >= ${MIN_DURATION_MS}, recibido ${timeoutMs}`,
    );
  }
  return Math.ceil(timeoutMs / 1000);
}

/** Rellena `suspendedDurationSeconds` con `timeout − maxIdleSeconds`. */
export function resolveIdlePolicy(
  idle: IdlePolicyInput | null | undefined,
  timeoutSeconds: number,
): IdlePolicy | undefined {
  if (idle === null) {
    return undefined;
  }
  const validated = validateIdlePolicy(idle ?? {});
  if (validated.maxIdleSeconds >= timeoutSeconds) {
    throw new InvalidArgumentError(
      `idle.maxIdleSeconds=${validated.maxIdleSeconds} debe ser menor que el timeout de ${timeoutSeconds} s`,
    );
  }
  if (validated.suspendedDurationSeconds !== undefined) {
    return validated;
  }
  return Object.freeze({
    maxIdleSeconds: validated.maxIdleSeconds,
    suspendedDurationSeconds: timeoutSeconds - validated.maxIdleSeconds,
    autoResume: validated.autoResume,
  });
}

export function portSpec(entry: PortLike): PortSpec {
  if (Array.isArray(entry)) {
    if (entry.length !== 2) {
      throw new InvalidArgumentError(`rango de puertos inválido: ${JSON.stringify(entry)}`);
    }
    return PortSpec.range(
      validatePort(entry[0], "allowedPorts"),
      validatePort(entry[1], "allowedPorts"),
    );
  }
  return PortSpec.single(validatePort(entry, "allowedPorts"));
}

/** Puertos del token principal: 8080 siempre; nunca el de hooks ni `allPorts`. */
export function proxyPortSpecs(allowedPorts: readonly PortLike[] | undefined): PortSpec[] {
  const specs = [PortSpec.single(DEFAULT_PORT)];
  for (const entry of allowedPorts ?? []) {
    const spec = portSpec(entry);
    if (spec.covers(HOOKS_PORT)) {
      throw new InvalidArgumentError(
        `allowedPorts no puede cubrir el puerto de hooks ${HOOKS_PORT} (ADR-006)`,
      );
    }
    if (!specs.some((known) => known.equals(spec))) {
      specs.push(spec);
    }
  }
  return specs;
}

export function validateHostPort(port: number): number {
  const validated = validatePort(port);
  if (validated === HOOKS_PORT) {
    throw new InvalidArgumentError(
      `getHost(${HOOKS_PORT}) no está permitido: es el puerto de hooks (ADR-006)`,
    );
  }
  return validated;
}

export function managedConnectorArn(name: string, region: string): string {
  return `arn:aws:lambda:${region}:aws:network-connector:aws-network-connector:${name}`;
}

/** Nombres gestionados (`ALL_INGRESS`, …) o ARNs propios; máx. 10. */
export function connectorArns(
  values: readonly string[] | undefined,
  region: string,
  field: string,
): string[] {
  if (values === undefined || values.length === 0) {
    return [];
  }
  if (values.length > NETWORK_CONNECTORS_MAX) {
    throw new InvalidArgumentError(`${field}: máximo ${NETWORK_CONNECTORS_MAX} conectores`);
  }
  return values.map((value) => {
    if (value.startsWith("arn:")) {
      return value;
    }
    if (MANAGED_NETWORK_CONNECTORS.has(value)) {
      return managedConnectorArn(value, region);
    }
    throw new InvalidArgumentError(
      `${field}: ${JSON.stringify(value)} no es un ARN ni un conector gestionado ` +
        `(${[...MANAGED_NETWORK_CONNECTORS].sort().join(", ")})`,
    );
  });
}

/** `logging` siempre explícito: `run-microvm` no hereda el log group de la imagen. */
export function loggingConfig(option: LoggingOption, templateName: string): LoggingConfig {
  if (option === "disabled") {
    return { disabled: {} };
  }
  if (option === "cloudwatch") {
    return { cloudWatch: { logGroup: `${LOG_GROUP_PREFIX}/${templateName}` } };
  }
  if (typeof option === "object" && option !== null) {
    const keys = Object.keys(option);
    if (keys.length === 1 && (keys[0] === "disabled" || keys[0] === "cloudWatch")) {
      return option;
    }
  }
  throw new InvalidArgumentError(
    "logging debe ser 'disabled', 'cloudwatch' o un objeto con 'disabled' o 'cloudWatch'",
  );
}

export interface LaunchPlanInput {
  readonly imageArn: string;
  readonly region: string;
  readonly templateVersion?: string | undefined;
  readonly timeoutMs?: number | undefined;
  readonly idle?: IdlePolicyInput | null | undefined;
  readonly envs?: Readonly<Record<string, string>> | undefined;
  readonly metadata?: Readonly<Record<string, string>> | undefined;
  readonly cpuTimeLimit?: number | undefined;
  readonly executionRoleArn?: string | undefined;
  readonly allowedPorts?: readonly PortLike[] | undefined;
  readonly ingress?: readonly string[] | undefined;
  readonly egress?: readonly string[] | undefined;
  readonly logging?: LoggingOption | undefined;
  readonly accessToken?: string | undefined;
}

export function buildLaunchPlan(input: LaunchPlanInput): LaunchPlan {
  const token = resolveAccessToken(input.accessToken);
  const durationSeconds = validateTimeoutMs(input.timeoutMs ?? DEFAULT_TIMEOUT_MS);
  const request = new LaunchRequest({
    imageArn: input.imageArn,
    imageVersion: input.templateVersion,
    maximumDurationSeconds: durationSeconds,
    runHookPayload: buildRunHookPayload({
      accessToken: token,
      envs: input.envs,
      metadata: input.metadata,
      cpuTimeLimit: input.cpuTimeLimit,
    }),
    clientToken: randomUUID().replaceAll("-", ""),
    logging: loggingConfig(input.logging ?? "disabled", templateNameFromArn(input.imageArn)),
    executionRoleArn: input.executionRoleArn,
    idle: resolveIdlePolicy(input.idle, durationSeconds),
    ingressConnectors: connectorArns(input.ingress, input.region, "ingress"),
    egressConnectors: connectorArns(input.egress, input.region, "egress"),
  });
  return Object.freeze({
    accessToken: token,
    request,
    proxyPorts: Object.freeze(proxyPortSpecs(input.allowedPorts)),
  });
}
