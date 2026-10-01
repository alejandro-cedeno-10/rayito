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

/**
 * `rayd` no pudo escribir por falta de disco: la reserva de 256 MiB no deja
 * sitio (`disk_reserve`) o el disco se llenó a mitad (`disk_full`). También
 * termina así una transferencia con `code` `resource_exhausted`.
 */
export class DiskFullError extends SandboxError {}

export interface TransferErrorOptions extends SandboxErrorOptions {
  readonly code: string;
  readonly reason: string;
}

/**
 * Una transferencia por S3 (`downloadUrl`, lectura grande) terminó `FAILED` o
 * `CANCELLED` con un `code` sin error propio (`failed_precondition`,
 * `unavailable`, `cancelled`, `internal` o uno desconocido). `reason` es el
 * token de `rayd` (`checksum_mismatch`, `file_shrank`, `s3_unavailable`…); el
 * mensaje empieza por `"<reason>: "` y nunca contiene una URL, un bucket, una
 * clave ni una ruta.
 */
export class TransferError extends SandboxError {
  readonly code: string;
  readonly reason: string;

  constructor(message: string, options: TransferErrorOptions) {
    super(message, options);
    this.code = options.code;
    this.reason = options.reason;
  }
}

/**
 * La importación de un `UploadTicket` o de una escritura grande terminó
 * `FAILED` o `CANCELLED` con un `code` sin error propio (el nombre de E2B
 * para una subida fallida).
 */
export class FileUploadError extends TransferError {}

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

/**
 * Error de un secreto de Secrets Manager (`SecretStore`, `SecretCache`,
 * `secrets`, y el `Secret` del shim de E2B). El mensaje nunca contiene el
 * valor ni el nombre del secreto (los nombres son selectores confidenciales,
 * como en E2B); con `secrets` nombra la clave de la variable de entorno.
 * Divergencia con E2B: allí `SecretError` extiende `Error`; aquí
 * `SandboxError`, para que un `catch` de `SandboxError` también lo atrape.
 */
export class SecretError extends SandboxError {}

/**
 * El secreto no existe o está programado para borrarse; nunca se guarda en
 * `SecretCache`. Extiende `SecretError` como en E2B (en TypeScript no puede
 * ser además `NotFoundError`: herencia simple; en Python sí lo es).
 */
export class SecretNotFoundError extends SecretError {}

/** `sandbox.git` sin credenciales (o con password vacío) contra un remoto que las pide; el mensaje nunca lleva la URL. */
export class GitAuthError extends AuthenticationError {}

/** `git push`/`pull` sin upstream configurado; el mensaje dice cómo fijarlo. */
export class GitUpstreamError extends SandboxError {}

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

/**
 * Algo que este sandbox no puede dar: una capacidad de E2B sin primitiva en
 * Lambda MicroVMs o una imagen anterior a la que la ofrece. Queda fuera de la
 * jerarquía de `SandboxError`, como `NotImplementedError` en Python;
 * `feature` nombra lo pedido y `reason` dice qué hacer; `doc` es la página
 * que lo explica (el shim `rayito/e2b` pasa la de compatibilidad) y `cause`
 * el error que lo motivó (p. ej. el `Unimplemented` del agente).
 */
export class UnimplementedError extends Error {
  readonly feature: string;
  readonly reason: string;
  readonly doc: string | undefined;

  constructor(
    feature: string,
    reason: string,
    doc?: string,
    options: { readonly cause?: unknown } = {},
  ) {
    super(
      `${feature} no está disponible: ${reason}${doc === undefined ? "" : `. Ver ${doc}`}`,
      options.cause === undefined ? undefined : { cause: options.cause },
    );
    Object.setPrototypeOf(this, new.target.prototype);
    this.name = new.target.name;
    this.feature = feature;
    this.reason = reason;
    this.doc = doc;
  }
}

/**
 * Se pidió un plazo lógico (`maxLifetimeMs`, `onTimeout`, `setTimeout`,
 * `connect({ timeoutMs })`) a un agente anterior a 0.3.0, que no lo impone
 * (ADR-011): hay que publicar una imagen 0.3.0 o posterior o prescindir del plazo. Es una
 * subclase de `UnimplementedError` (paridad con Python): fuera de la
 * jerarquía de `SandboxError`, nunca un `InvalidArgumentError`.
 */
export class LifecycleUnsupportedError extends UnimplementedError {}

export function errorMessage(error: unknown): string {
  if (error instanceof Error) {
    return error.message;
  }
  return String(error);
}

/**
 * Error del índice opcional de metadatos (`DynamoDbIndex`, M14): la tabla no
 * existe, faltan permisos IAM o `BatchGetItem` dejó claves sin procesar tras
 * los reintentos. Un listado con índice nunca devuelve una lista incompleta
 * en silencio. El mensaje nunca repite el de AWS; `awsCode` es el código de
 * DynamoDB, si lo hubo.
 */
export class SandboxIndexError extends SandboxError {}

/**
 * `create({ index })` no pudo escribir la fila del sandbox (`PutItem`). Con
 * `onWriteFailure: "terminate"` (por defecto) el MicroVM ya se terminó,
 * salvo `keepOnFailure: true`.
 */
export class IndexWriteError extends SandboxIndexError {}

// ------------------------------------------------------------- M15 (Rayito 0.6)
//
// Cada clase la usa la función OpenSpec que la nombra en su docstring; hasta
// entonces nada la lanza (foundations sólo la pre-crea como seam, §1(g) de la
// arquitectura de M15). `code` es una cadena cerrada, nunca el mensaje de AWS
// ni un identificador del usuario.

export interface MountErrorOptions extends SandboxErrorOptions {
  readonly code: string;
}

/**
 * Un montaje de `mounts` (m15-s3-mounts) falló o sigue sin asentarse. `code`
 * es uno de `network`, `iam_denied`, `not_found`, `not_allowed`,
 * `helper_missing`, `timeout`.
 */
export class MountError extends SandboxError {
  readonly code: string;

  constructor(message: string, options: MountErrorOptions) {
    super(message, options);
    this.code = options.code;
  }
}

/** Error de un volumen EFS (m15-efs-volumes, experimental). */
export class VolumeError extends SandboxError {}

/** El `AccessPoint` del volumen no existe. */
export class VolumeNotFoundError extends VolumeError {}

/** Una operación de contenido nombra una ruta fuera del volumen montado. */
export class VolumePathNotFoundError extends VolumeError {}

export interface BuildErrorOptions extends SandboxErrorOptions {
  readonly reason?: string | undefined;
  readonly step?: number | undefined;
  readonly command?: string | undefined;
  readonly exitCode?: number | undefined;
  readonly logTail?: string | undefined;
}

/**
 * `Template.build` (m15-templates) falló.
 *
 * OJO (open item para m15-templates): `src/e2b/errors.ts` ya declara un
 * `BuildError`/`TemplateError` propios — stands-ins que nunca se lanzan
 * (Rayito no tenía API de templates) y que `tests/unit/e2b-exports.test.ts`
 * comprueba con una forma distinta (`BuildError` del shim NO es
 * `SandboxError`). No se exportan desde `index.ts` todavía para no chocar
 * con ellos; m15-templates decide si los retira y realinea el shim a estas
 * clases (el patrón ya usado para `NotEnoughSpaceError`/`FileUploadError`)
 * o si renombra una de las dos parejas.
 */
export class BuildError extends SandboxError {
  readonly reason: string | undefined;
  readonly step: number | undefined;
  readonly command: string | undefined;
  readonly exitCode: number | undefined;
  readonly logTail: string | undefined;

  constructor(message: string, options: BuildErrorOptions = {}) {
    super(message, options);
    this.reason = options.reason;
    this.step = options.step;
    this.command = options.command;
    this.exitCode = options.exitCode;
    this.logTail = options.logTail;
  }
}

/** Un `Template` inválido, o una imagen anterior a 0.6 con un `Template`. */
export class TemplateError extends SandboxError {}

export interface StackErrorOptions extends SandboxErrorOptions {
  readonly code: string;
}

/**
 * Un `OptionalStacks.deploy/status/destroy` (M15 foundations) falló. `code`
 * es `blocked` (pila en ROLLBACK_COMPLETE), `not_found`, `in_progress` o
 * `failed`; el mensaje nunca repite el de CloudFormation.
 */
export class StackError extends SandboxError {
  readonly code: string;

  constructor(message: string, options: StackErrorOptions) {
    super(message, options);
    this.code = options.code;
  }
}

/** `LifecycleEvents` webhooks (m15-events-webhooks) falló. */
export class WebhookError extends SandboxError {}

export interface GatewayErrorOptions extends SandboxErrorOptions {
  readonly code: string;
}

/** Un `SecretGateway` (m15-secrets-gateway) rechazó o no pudo enrutar una petición. */
export class GatewayError extends SandboxError {
  readonly code: string;

  constructor(message: string, options: GatewayErrorOptions) {
    super(message, options);
    this.code = options.code;
  }
}

/** `CustomDomain` (m15-custom-domain) falló. */
export class CustomDomainError extends SandboxError {}
