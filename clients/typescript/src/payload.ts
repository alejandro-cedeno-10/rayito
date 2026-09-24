/**
 * Constructor del `runHookPayload` (ADR-004).
 *
 * Es el único canal per-VM de `run-microvm`. Lleva el sha256 del access token
 * del agente, nunca el token: AWS puede registrar el payload en CloudTrail.
 *
 * Contrato con `rayd` (ARCHITECTURE.md "Auth interna"): el access token es
 * `base64url(secret)` sin padding; `token_sha256 = sha256(secret).hex()`, es
 * decir, el hash de los **bytes decodificados**, no de la cadena. `rayd`
 * decodifica `x-access-token`, tolera padding y compara en tiempo constante.
 */

import { createHash, randomBytes } from "node:crypto";
import { InvalidArgumentError } from "./errors.js";
import { RUN_HOOK_PAYLOAD_MAX_CHARS } from "./limits.js";
import { type LifecycleBlock, lifecycleBlockToWire } from "./sandbox/lifecycle.js";

export const PAYLOAD_VERSION = 1;
export const DEFAULT_USER = "user";
export const DEFAULT_WORKDIR = "/home/user";
export const ACCESS_TOKEN_BYTES = 32;
export const CPU_TIME_LIMIT_MIN_SECONDS = 1;
export const CPU_TIME_LIMIT_MAX_SECONDS = 28_800;

const BASE64URL_ALPHABET = /^[A-Za-z0-9_-]+$/;

export function generateAccessToken(): string {
  return encodeAccessToken(randomBytes(ACCESS_TOKEN_BYTES));
}

export function encodeAccessToken(secret: Uint8Array): string {
  return Buffer.from(secret).toString("base64url");
}

/**
 * Los bytes que `rayd` hashea. Falla si el token no es base64url canónico
 * (alfabeto estricto, bits sobrantes a cero), igual que el decoder de `rayd`.
 */
export function decodeAccessToken(accessToken: string): Uint8Array {
  const stripped = accessToken.replace(/=+$/, "");
  if (!BASE64URL_ALPHABET.test(stripped)) {
    throw new InvalidArgumentError("accessToken no es base64url");
  }
  const secret = Buffer.from(stripped, "base64url");
  if (secret.length === 0 || encodeAccessToken(secret) !== stripped) {
    throw new InvalidArgumentError("accessToken no es base64url");
  }
  return new Uint8Array(secret);
}

export function validateAccessToken(accessToken: string): string {
  decodeAccessToken(accessToken);
  return accessToken;
}

export function accessTokenSha256(accessToken: string): string {
  return createHash("sha256").update(decodeAccessToken(accessToken)).digest("hex");
}

export interface RunHookPayloadOptions {
  readonly accessToken: string;
  readonly envs?: Readonly<Record<string, string>> | undefined;
  readonly metadata?: Readonly<Record<string, string>> | undefined;
  readonly cpuTimeLimit?: number | undefined;
  readonly user?: string | undefined;
  readonly workdir?: string | undefined;
  /**
   * `"network": {"enforce": true}` sólo cuando es `true`: `rayd` instala
   * deny-all antes de responder a `/run`. Nunca lleva reglas ni credenciales
   * (viajan después por `UpdateNetwork`, autenticado con el token).
   */
  readonly networkEnforce?: boolean | undefined;
  /** El plazo lógico (ADR-011), ya validado por `resolveLifecycle`; sin él no viaja la clave. */
  readonly lifecycle?: LifecycleBlock | undefined;
}

/**
 * Serializa el payload (JSON compacto, claves ordenadas, sólo ASCII como
 * `json.dumps` en Python) y falla si supera el límite del modelo (4096 chars).
 * `envs` y `metadata` se omiten del JSON cuando están vacíos para no gastar
 * caracteres; `cpuTimeLimit` (segundos de CPU, `1..=28800`) viaja como
 * `limits: {"cpu_seconds": N}` sólo cuando se pasa, igual que en Python.
 * `lifecycle` (≈ 80 caracteres) sólo viaja cuando se pidió un plazo lógico:
 * un `rayd` anterior a M9 ignora la clave y `v` sigue siendo 1.
 */
export function buildRunHookPayload(options: RunHookPayloadOptions): string {
  if (!options.accessToken) {
    throw new InvalidArgumentError("accessToken no puede estar vacío");
  }
  const payload: Record<string, unknown> = {
    v: PAYLOAD_VERSION,
    token_sha256: accessTokenSha256(options.accessToken),
    user: options.user ?? DEFAULT_USER,
    workdir: options.workdir ?? DEFAULT_WORKDIR,
  };
  if (options.envs !== undefined && Object.keys(options.envs).length > 0) {
    payload.envs = validatedEnvs(options.envs);
  }
  if (options.metadata !== undefined && Object.keys(options.metadata).length > 0) {
    payload.metadata = validatedStringMap(options.metadata, "metadata");
  }
  if (options.cpuTimeLimit !== undefined) {
    payload.limits = { cpu_seconds: validatedCpuTimeLimit(options.cpuTimeLimit) };
  }
  if (options.networkEnforce === true) {
    payload.network = { enforce: true };
  }
  if (options.lifecycle !== undefined) {
    payload.lifecycle = lifecycleBlockToWire(options.lifecycle);
  }
  const text = asciiJson(sortedKeys(payload));
  if (text.length > RUN_HOOK_PAYLOAD_MAX_CHARS) {
    throw new InvalidArgumentError(
      `runHookPayload ocupa ${text.length} caracteres y el máximo es ` +
        `${RUN_HOOK_PAYLOAD_MAX_CHARS}. Reduce \`envs\` en create(); las variables ` +
        "grandes van en `envs` de cada comando o en un fichero con files.write().",
    );
  }
  return text;
}

export function validatedEnvs(envs: Readonly<Record<string, string>>): Record<string, string> {
  return validatedStringMap(envs, "envs");
}

export function validatedStringMap(
  values: Readonly<Record<string, string>>,
  field: string,
): Record<string, string> {
  const validated: Record<string, string> = {};
  for (const [key, value] of Object.entries(values)) {
    if (typeof key !== "string" || key.length === 0) {
      throw new InvalidArgumentError(`clave de ${field} inválida: ${JSON.stringify(key)}`);
    }
    if (typeof value !== "string") {
      throw new InvalidArgumentError(
        `el valor de ${field}[${JSON.stringify(key)}] debe ser string`,
      );
    }
    validated[key] = value;
  }
  return validated;
}

export function validatedCpuTimeLimit(cpuTimeLimit: unknown): number {
  if (
    typeof cpuTimeLimit !== "number" ||
    !Number.isInteger(cpuTimeLimit) ||
    cpuTimeLimit < CPU_TIME_LIMIT_MIN_SECONDS ||
    cpuTimeLimit > CPU_TIME_LIMIT_MAX_SECONDS
  ) {
    throw new InvalidArgumentError(
      `cpuTimeLimit debe ser un entero entre ${CPU_TIME_LIMIT_MIN_SECONDS} y ` +
        `${CPU_TIME_LIMIT_MAX_SECONDS} segundos de CPU, recibido ${String(cpuTimeLimit)}`,
    );
  }
  return cpuTimeLimit;
}

function sortedKeys(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map(sortedKeys);
  }
  if (typeof value === "object" && value !== null) {
    const entries = Object.entries(value as Record<string, unknown>).sort(([a], [b]) =>
      a < b ? -1 : a > b ? 1 : 0,
    );
    return Object.fromEntries(entries.map(([key, item]) => [key, sortedKeys(item)]));
  }
  return value;
}

function asciiJson(value: unknown): string {
  return JSON.stringify(value).replace(
    /[-￿]/g,
    (char) => `\\u${char.charCodeAt(0).toString(16).padStart(4, "0")}`,
  );
}
