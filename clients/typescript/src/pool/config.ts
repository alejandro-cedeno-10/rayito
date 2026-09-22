/**
 * `PoolConfig`: la configuración de lanzamiento, inmutable y común a todas
 * las plazas de un `SandboxPool` (ADR-008). Cada campo se corresponde con la
 * opción homónima de `Sandbox.create()`: `envs`, `metadata`, `cpuTimeLimit`,
 * la política de idle, la vida máxima y los conectores son por pool, nunca
 * por toma. Sin I/O.
 */

import { InvalidArgumentError } from "../errors.js";
import { type IdlePolicy, type IdlePolicyInput, validateIdlePolicy } from "../models.js";
import { validatedCpuTimeLimit, validatedStringMap } from "../payload.js";
import {
  DEFAULT_READY_TIMEOUT_MS,
  type LoggingOption,
  MAX_DURATION_MS,
  validateTimeoutMs,
} from "../sandbox/launch.js";

export const POOL_SIZE_MAX = 64;
export const FILL_CONCURRENCY_MAX = 8;
export const MIN_REMAINING_FLOOR_MS = 60_000;
export const SWEEP_INTERVAL_MIN_MS = 5000;
export const DEFAULT_POOL_TIMEOUT_MS = MAX_DURATION_MS;
export const DEFAULT_MIN_REMAINING_MS = 3_600_000;
export const DEFAULT_FILL_CONCURRENCY = 4;
export const DEFAULT_SWEEP_INTERVAL_MS = 30_000;

export interface PoolConfig {
  /** Plazas aparcadas que el pool mantiene (`1..=64`). */
  readonly size: number;
  /** ARN o nombre de la imagen; por defecto `RAYITO_TEMPLATE`. */
  readonly template?: string | undefined;
  readonly templateVersion?: string | undefined;
  /** Vida máxima de cada plaza en ms; por defecto 28 800 000 (todo lo que AWS permite). */
  readonly timeoutMs?: number | undefined;
  /** Obligatoriamente con `autoResume: true`; por defecto `{ maxIdleSeconds: 300 }`. */
  readonly idle?: IdlePolicyInput | undefined;
  readonly envs?: Readonly<Record<string, string>> | undefined;
  readonly metadata?: Readonly<Record<string, string>> | undefined;
  readonly cpuTimeLimit?: number | undefined;
  readonly executionRoleArn?: string | undefined;
  readonly ingress?: readonly string[] | undefined;
  readonly egress?: readonly string[] | undefined;
  readonly logging?: LoggingOption | undefined;
  /** Vida mínima con la que se entrega una plaza; por debajo se recicla. Por defecto 3 600 000. */
  readonly minRemainingMs?: number | undefined;
  /** Calentamientos en vuelo a la vez (`1..=8`); por defecto 4. */
  readonly fillConcurrency?: number | undefined;
  /** Cadencia del sweeper (reciclado + reconciliación), mínimo 5 000; por defecto 30 000. */
  readonly sweepIntervalMs?: number | undefined;
  /** Plazo de readiness de cada calentamiento; por defecto 90 000. */
  readonly readyTimeoutMs?: number | undefined;
}

/** `PoolConfig` con todos los valores por defecto aplicados y validados. */
export interface ResolvedPoolConfig {
  readonly size: number;
  readonly template: string | undefined;
  readonly templateVersion: string | undefined;
  readonly timeoutMs: number;
  readonly idle: IdlePolicy;
  readonly envs: Readonly<Record<string, string>> | undefined;
  readonly metadata: Readonly<Record<string, string>> | undefined;
  readonly cpuTimeLimit: number | undefined;
  readonly executionRoleArn: string | undefined;
  readonly ingress: readonly string[] | undefined;
  readonly egress: readonly string[] | undefined;
  readonly logging: LoggingOption;
  readonly minRemainingMs: number;
  readonly fillConcurrency: number;
  readonly sweepIntervalMs: number;
  readonly readyTimeoutMs: number;
}

function requireInteger(name: string, value: unknown, min: number, max: number): number {
  if (typeof value !== "number" || !Number.isInteger(value) || value < min || value > max) {
    throw new InvalidArgumentError(
      `${name} debe ser un entero en ${min}..=${max}, recibido ${String(value)}`,
    );
  }
  return value;
}

export function validatePoolIdle(idle: IdlePolicyInput | undefined, timeoutMs: number): IdlePolicy {
  const validated = validateIdlePolicy(idle ?? {});
  if (!validated.autoResume) {
    throw new InvalidArgumentError(
      "idle debe tener autoResume: true: una plaza aparcada debe despertar sola",
    );
  }
  const timeoutSeconds = Math.ceil(timeoutMs / 1000);
  if (validated.maxIdleSeconds >= timeoutSeconds) {
    throw new InvalidArgumentError(
      `idle.maxIdleSeconds=${validated.maxIdleSeconds} debe ser menor que el timeout de ${timeoutSeconds} s`,
    );
  }
  return validated;
}

export function validatePoolConfig(config: PoolConfig): ResolvedPoolConfig {
  const size = requireInteger("size", config.size, 1, POOL_SIZE_MAX);
  const timeoutMs = config.timeoutMs ?? DEFAULT_POOL_TIMEOUT_MS;
  validateTimeoutMs(timeoutMs);
  const idle = validatePoolIdle(config.idle, timeoutMs);
  const minRemainingMs = requireInteger(
    "minRemainingMs",
    config.minRemainingMs ?? DEFAULT_MIN_REMAINING_MS,
    MIN_REMAINING_FLOOR_MS,
    timeoutMs - MIN_REMAINING_FLOOR_MS,
  );
  const fillConcurrency = requireInteger(
    "fillConcurrency",
    config.fillConcurrency ?? DEFAULT_FILL_CONCURRENCY,
    1,
    FILL_CONCURRENCY_MAX,
  );
  const sweepIntervalMs = config.sweepIntervalMs ?? DEFAULT_SWEEP_INTERVAL_MS;
  if (typeof sweepIntervalMs !== "number" || !(sweepIntervalMs >= SWEEP_INTERVAL_MIN_MS)) {
    throw new InvalidArgumentError(
      `sweepIntervalMs debe ser >= ${SWEEP_INTERVAL_MIN_MS}, recibido ${String(sweepIntervalMs)}`,
    );
  }
  const readyTimeoutMs = config.readyTimeoutMs ?? DEFAULT_READY_TIMEOUT_MS;
  if (typeof readyTimeoutMs !== "number" || !(readyTimeoutMs > 0)) {
    throw new InvalidArgumentError(
      `readyTimeoutMs debe ser > 0, recibido ${String(readyTimeoutMs)}`,
    );
  }
  if (config.envs !== undefined) {
    validatedStringMap(config.envs, "envs");
  }
  if (config.metadata !== undefined) {
    validatedStringMap(config.metadata, "metadata");
  }
  if (config.cpuTimeLimit !== undefined) {
    validatedCpuTimeLimit(config.cpuTimeLimit);
  }
  return Object.freeze({
    size,
    template: config.template,
    templateVersion: config.templateVersion,
    timeoutMs,
    idle,
    envs: config.envs,
    metadata: config.metadata,
    cpuTimeLimit: config.cpuTimeLimit,
    executionRoleArn: config.executionRoleArn,
    ingress: config.ingress,
    egress: config.egress,
    logging: config.logging ?? "disabled",
    minRemainingMs,
    fillConcurrency,
    sweepIntervalMs,
    readyTimeoutMs,
  });
}

/** Las opciones de `Sandbox.create()` de cada plaza; el pool añade el token, `keepOnFailure`, el plano y el transporte. */
export function launchOptions(config: ResolvedPoolConfig): {
  readonly template: string | undefined;
  readonly templateVersion: string | undefined;
  readonly timeoutMs: number;
  readonly idle: IdlePolicy;
  readonly envs: Readonly<Record<string, string>> | undefined;
  readonly metadata: Readonly<Record<string, string>> | undefined;
  readonly cpuTimeLimit: number | undefined;
  readonly executionRoleArn: string | undefined;
  readonly ingress: readonly string[] | undefined;
  readonly egress: readonly string[] | undefined;
  readonly logging: LoggingOption;
  readonly readyTimeoutMs: number;
} {
  return {
    template: config.template,
    templateVersion: config.templateVersion,
    timeoutMs: config.timeoutMs,
    idle: config.idle,
    envs: config.envs,
    metadata: config.metadata,
    cpuTimeLimit: config.cpuTimeLimit,
    executionRoleArn: config.executionRoleArn,
    ingress: config.ingress,
    egress: config.egress,
    logging: config.logging,
    readyTimeoutMs: config.readyTimeoutMs,
  };
}
