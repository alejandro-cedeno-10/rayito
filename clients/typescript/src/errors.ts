/**
 * Jerarquía de errores del SDK (misma forma que la de E2B para JavaScript).
 *
 * `SandboxError` es la raíz de todo lo que le ocurre a un sandbox concreto.
 * `AuthenticationError`, `QuotaExceededError` y `CapacityError` quedan fuera
 * de la jerarquía, como en E2B, porque describen la cuenta o el caller, no el
 * estado de un sandbox. Cada clase fija `name` y su prototipo para que
 * `instanceof` funcione también en el bundle CommonJS.
 */

import type { Code } from "@connectrpc/connect";

export interface SandboxErrorOptions {
  readonly statusCode?: number | undefined;
  readonly grpcCode?: Code | undefined;
  readonly awsCode?: string | undefined;
  readonly cause?: unknown;
}

export class SandboxError extends Error {
  readonly statusCode: number | undefined;
  readonly grpcCode: Code | undefined;
  readonly awsCode: string | undefined;

  constructor(message: string, options: SandboxErrorOptions = {}) {
    super(message, options.cause === undefined ? undefined : { cause: options.cause });
    Object.setPrototypeOf(this, new.target.prototype);
    this.name = new.target.name;
    this.statusCode = options.statusCode;
    this.grpcCode = options.grpcCode;
    this.awsCode = options.awsCode;
  }
}

export class TimeoutError extends SandboxError {}

export class InvalidArgumentError extends SandboxError {}

export class NotFoundError extends SandboxError {}

export class FileNotFoundError extends NotFoundError {}

export class SandboxNotFoundError extends NotFoundError {}

export interface SandboxNotReadyErrorOptions extends SandboxErrorOptions {
  readonly state?: string | undefined;
  readonly stateReason?: string | undefined;
}

/**
 * El agente no respondió a `Health` dentro de `readyTimeoutMs`. `state` y
 * `stateReason` vienen de la única llamada a `get-microvm` posterior al
 * timeout; si el MicroVM murió en `/run`, `stateReason` lo dice.
 */
export class SandboxNotReadyError extends SandboxError {
  readonly state: string | undefined;
  readonly stateReason: string | undefined;

  constructor(message: string, options: SandboxNotReadyErrorOptions = {}) {
    super(message, options);
    this.state = options.state;
    this.stateReason = options.stateReason;
  }
}

export class SandboxStateError extends SandboxError {}

export class SandboxLifetimeError extends SandboxError {}

/** `take()` sobre un `SandboxPool` que no fue arrancado o ya fue cerrado. */
export class PoolClosedError extends SandboxError {}

export interface PersistenceErrorOptions extends SandboxErrorOptions {
  readonly code: string;
}

/**
 * `Checkpoint`/`Restore` fallaron con un `code` cerrado: `permission_denied`
 * (sin execution role, `AccessDenied` o credenciales rechazadas), `internal`
 * (red, S3 5xx, tar/gzip, checksum, disco lleno), `failed_precondition` (otra
 * operación en curso o región desconocida), `unimplemented` (imagen con un
 * `rayd` anterior a `Checkpoint`), `interrupted` (stream cortado a mitad; no
 * se reanuda) o `resource_exhausted`.
 */
export class PersistenceError extends SandboxError {
  readonly code: string;

  constructor(message: string, options: PersistenceErrorOptions) {
    super(message, options);
    this.code = options.code;
  }
}

export interface CommandExitErrorOptions {
  readonly exitCode: number;
  readonly stdout?: string | undefined;
  readonly stderr?: string | undefined;
  readonly error?: string | undefined;
  readonly grpcCode?: Code | undefined;
}

export class CommandExitError extends SandboxError {
  readonly exitCode: number;
  readonly stdout: string;
  readonly stderr: string;
  readonly error: string | undefined;

  constructor(message: string, options: CommandExitErrorOptions) {
    super(message, { grpcCode: options.grpcCode });
    this.exitCode = options.exitCode;
    this.stdout = options.stdout ?? "";
    this.stderr = options.stderr ?? "";
    this.error = options.error;
  }
}

export interface RateLimitErrorOptions extends SandboxErrorOptions {
  readonly retryAfter?: number | undefined;
}

export class RateLimitError extends SandboxError {
  readonly retryAfter: number | undefined;

  constructor(message: string, options: RateLimitErrorOptions = {}) {
    super(message, options);
    this.retryAfter = options.retryAfter;
  }
}

export interface AuthenticationErrorOptions {
  readonly proxyRejected?: boolean | undefined;
  readonly grpcCode?: Code | undefined;
  readonly awsCode?: string | undefined;
  readonly cause?: unknown;
}

/**
 * Credenciales o token rechazados por AWS, por el proxy o por el agente.
 * `proxyRejected` es `true` sólo cuando el 403 vino del proxy de AWS (JWE
 * ausente, expirado o sin el puerto): el SDK reacuña y reintenta una vez.
 */
export class AuthenticationError extends Error {
  readonly proxyRejected: boolean;
  readonly grpcCode: Code | undefined;
  readonly awsCode: string | undefined;

  constructor(message: string, options: AuthenticationErrorOptions = {}) {
    super(message, options.cause === undefined ? undefined : { cause: options.cause });
    Object.setPrototypeOf(this, new.target.prototype);
    this.name = new.target.name;
    this.proxyRejected = options.proxyRejected ?? false;
    this.grpcCode = options.grpcCode;
    this.awsCode = options.awsCode;
  }
}

export class QuotaExceededError extends Error {
  readonly quotaCode: string | undefined;

  constructor(message: string, options: { readonly quotaCode?: string | undefined } = {}) {
    super(message);
    Object.setPrototypeOf(this, new.target.prototype);
    this.name = new.target.name;
    this.quotaCode = options.quotaCode;
  }
}

export class CapacityError extends Error {
  constructor(message: string) {
    super(message);
    Object.setPrototypeOf(this, new.target.prototype);
    this.name = new.target.name;
  }
}

export function errorMessage(error: unknown): string {
  if (error instanceof Error) {
    return error.message;
  }
  return String(error);
}
