/**
 * `Checkpoint`/`Restore` (`m7-s3-persistence`, diseño D10): `S3Prefix`, los
 * resultados, las reglas puras de `create({ persist })` y `reincarnate()`, la
 * traducción de eventos y errores, y `PersistenceClient` sobre `SandboxCore`.
 * Ambos RPCs van por el transporte de streams con el 403 del proxy
 * reintentado una vez antes del primer mensaje; un corte a mitad no se
 * reconecta: el error dice que la operación se interrumpió.
 */

import { create } from "@bufbuild/protobuf";
import { Code, ConnectError } from "@connectrpc/connect";
import {
  AuthenticationError,
  InvalidArgumentError,
  NotFoundError,
  PersistenceError,
  SandboxError,
  TimeoutError,
} from "../errors.js";
import { type StreamError, UserSchema } from "../gen/rayito/v1/common_pb.js";
import {
  type CheckpointEvent,
  type CheckpointRequest,
  CheckpointRequestSchema,
  FilesystemService,
  type RestoreEvent,
  type RestoreRequest,
  RestoreRequestSchema,
  S3LocationSchema,
} from "../gen/rayito/v1/filesystem_pb.js";
import {
  DEFAULT_PERSIST_TIMEOUT_SECONDS,
  PERSIST_EXCLUDE_MAX,
  PERSIST_KEY_PREFIX_MAX_BYTES,
  S3_BUCKET_NAME_MAX,
  S3_BUCKET_NAME_MIN,
} from "../limits.js";
import type { IdlePolicyInput } from "../models.js";
import { asConnectError, isProxyForbidden, isStreamReset } from "../transport/errors.js";
import { type OpenedStream, type SandboxCore, type StreamStarter, withTimeout } from "./core.js";
import type { LoggingOption, PortLike } from "./launch.js";

export const DEFAULT_PERSIST_TIMEOUT_MS = DEFAULT_PERSIST_TIMEOUT_SECONDS * 1000;
export const EXCLUDE_MAX_BYTES = 4096;
export const PERSIST_ARCHIVE_KEY = "home.tar.gz";
export const PERSIST_MANIFEST_KEY = "manifest.json";
export const UNIMPLEMENTED_MESSAGE =
  "la imagen corre un rayd sin Checkpoint/Restore: republica la imagen con un agente que " +
  "implemente FilesystemService.Checkpoint (rayd >= 0.2.0)";

const BUCKET_CHARS = /^[a-z0-9.-]+$/;
const KEY_CHARS = /^[A-Za-z0-9!_.*'()/-]+$/;
const IPV4 = /^\d+\.\d+\.\d+\.\d+$/;

export function interruptedMessage(reason: string): string {
  return `la operación de persistencia se interrumpió a mitad (${reason}); el SDK no la reanuda: vuelve a ejecutarla`;
}

// ------------------------------------------------------------------ models

export interface S3PrefixInit {
  readonly bucket: string;
  /** La base del operador (la que acota la política IAM); `rayito` por defecto. */
  readonly prefix?: string | undefined;
  /** El home concreto; `create({ persist })` lo fija al `sandboxId` si falta. */
  readonly name?: string | undefined;
  /** Sólo si el bucket no está en la región del sandbox. */
  readonly region?: string | undefined;
}

/** Reglas de D2 (las mismas que aplica `rayd`) sobre un nombre de bucket. */
export function validateBucketName(bucket: unknown): string {
  if (typeof bucket !== "string") {
    throw new InvalidArgumentError(`bucket debe ser string, recibido ${JSON.stringify(bucket)}`);
  }
  if (bucket.length < S3_BUCKET_NAME_MIN || bucket.length > S3_BUCKET_NAME_MAX) {
    throw new InvalidArgumentError(
      `bucket debe tener entre ${S3_BUCKET_NAME_MIN} y ${S3_BUCKET_NAME_MAX} caracteres`,
    );
  }
  if (!BUCKET_CHARS.test(bucket)) {
    throw new InvalidArgumentError("bucket sólo admite minúsculas, dígitos, `.` y `-`");
  }
  if (!/^[a-z0-9]/.test(bucket) || !/[a-z0-9]$/.test(bucket)) {
    throw new InvalidArgumentError("bucket debe empezar y terminar en letra o dígito");
  }
  if (bucket.includes("..")) {
    throw new InvalidArgumentError("bucket no puede contener `..`");
  }
  if (IPV4.test(bucket)) {
    throw new InvalidArgumentError("bucket no puede parecer una dirección IPv4");
  }
  return bucket;
}

/** Reglas de D2 sobre un prefijo de clave (`field` nombra el campo en el error). */
export function validateKeyPrefix(keyPrefix: unknown, field = "prefix"): string {
  if (typeof keyPrefix !== "string" || keyPrefix.length === 0) {
    throw new InvalidArgumentError(`${field} debe ser una cadena no vacía`);
  }
  if (new TextEncoder().encode(keyPrefix).length > PERSIST_KEY_PREFIX_MAX_BYTES) {
    throw new InvalidArgumentError(
      `${field} no puede superar ${PERSIST_KEY_PREFIX_MAX_BYTES} bytes`,
    );
  }
  if (keyPrefix.startsWith("/") || keyPrefix.endsWith("/")) {
    throw new InvalidArgumentError(`${field} no puede empezar ni terminar en \`/\``);
  }
  for (const component of keyPrefix.split("/")) {
    if (component === "") {
      throw new InvalidArgumentError(`${field} no puede contener \`//\``);
    }
    if (component === "." || component === "..") {
      throw new InvalidArgumentError(`${field} no puede contener componentes \`.\` o \`..\``);
    }
  }
  if (!KEY_CHARS.test(keyPrefix)) {
    throw new InvalidArgumentError(
      `${field} sólo admite letras, dígitos y \`!_.*'()-/\` (conjunto seguro de S3)`,
    );
  }
  return keyPrefix;
}

/** Dónde vive un `HOME` persistido: `s3://bucket/prefix/name/`. Inmutable y validado en cliente. */
export class S3Prefix {
  readonly bucket: string;
  readonly prefix: string;
  readonly name: string | undefined;
  readonly region: string | undefined;

  constructor(init: S3PrefixInit) {
    this.bucket = validateBucketName(init.bucket);
    this.prefix = validateKeyPrefix(init.prefix ?? "rayito");
    if (init.name !== undefined) {
      validateKeyPrefix(`${this.prefix}/${init.name}`, "name");
    }
    if (init.region !== undefined && init.region.length === 0) {
      throw new InvalidArgumentError("region no puede ser una cadena vacía");
    }
    this.name = init.name;
    this.region = init.region;
    Object.freeze(this);
  }

  /** `prefix/name`; `InvalidArgumentError` mientras `name` sea `undefined`. */
  get keyPrefix(): string {
    if (this.name === undefined) {
      throw new InvalidArgumentError(
        "S3Prefix sin name: create({ persist }) lo fija al sandboxId; para connect()/restore " +
          "explícito pásalo (new S3Prefix({ ..., name }))",
      );
    }
    return `${this.prefix}/${this.name}`;
  }

  get archiveKey(): string {
    return `${this.keyPrefix}/${PERSIST_ARCHIVE_KEY}`;
  }

  get manifestKey(): string {
    return `${this.keyPrefix}/${PERSIST_MANIFEST_KEY}`;
  }

  get uri(): string {
    return `s3://${this.bucket}/${this.keyPrefix}`;
  }

  withName(name: string): S3Prefix {
    return new S3Prefix({ bucket: this.bucket, prefix: this.prefix, name, region: this.region });
  }

  equals(other: S3Prefix | undefined): boolean {
    return (
      other !== undefined &&
      this.bucket === other.bucket &&
      this.prefix === other.prefix &&
      this.name === other.name &&
      this.region === other.region
    );
  }
}

export interface CheckpointProgress {
  readonly filesDone: number;
  readonly bytesRead: number;
  readonly bytesUploaded: number;
}

export interface CheckpointResult {
  readonly bucket: string;
  readonly keyPrefix: string;
  readonly uri: string;
  readonly files: number;
  readonly bytesRead: number;
  readonly archiveBytes: number;
  readonly sha256: string;
  readonly skipped: number;
  readonly durationMs: number;
}

export interface RestoreProgress {
  readonly filesDone: number;
  readonly bytesDownloaded: number;
}

export interface RestoreResult {
  readonly bucket: string;
  readonly keyPrefix: string;
  readonly uri: string;
  readonly files: number;
  readonly bytesWritten: number;
  readonly archiveBytes: number;
  readonly sha256: string;
  readonly skipped: number;
  readonly durationMs: number;
}

/** Lo que `create()` recibió, para que `reincarnate()` lance el siguiente igual. */
export interface LaunchOptions {
  readonly template: string;
  readonly templateVersion: string | undefined;
  readonly timeoutMs: number | undefined;
  readonly idle: IdlePolicyInput | null | undefined;
  readonly envs: Readonly<Record<string, string>> | undefined;
  readonly metadata: Readonly<Record<string, string>> | undefined;
  readonly cpuTimeLimit: number | undefined;
  readonly executionRoleArn: string | undefined;
  readonly allowedPorts: readonly PortLike[] | undefined;
  readonly ingress: readonly string[] | undefined;
  readonly egress: readonly string[] | undefined;
  readonly logging: LoggingOption | undefined;
  readonly accessToken: string | undefined;
  readonly readyTimeoutMs: number | undefined;
  readonly requestTimeoutMs: number | undefined;
  readonly reconnectTimeoutMs: number | undefined;
  readonly keepOnFailure: boolean | undefined;
}

export type CheckpointProgressCallback = (progress: CheckpointProgress) => void;
export type RestoreProgressCallback = (progress: RestoreProgress) => void;

export interface CheckpointFilesOptions {
  /** Por defecto `sandbox.persist`. */
  readonly target?: S3Prefix | undefined;
  /** Directorios relativos al `HOME` (sin globs, máx. 64). */
  readonly exclude?: readonly string[] | undefined;
  readonly timeoutMs?: number | undefined;
  readonly onProgress?: CheckpointProgressCallback | undefined;
  readonly user?: string | undefined;
}

export interface RestoreFilesOptions {
  readonly source?: S3Prefix | undefined;
  readonly timeoutMs?: number | undefined;
  readonly onProgress?: RestoreProgressCallback | undefined;
  readonly user?: string | undefined;
}

export interface ReincarnateOptions {
  readonly exclude?: readonly string[] | undefined;
  readonly persistTimeoutMs?: number | undefined;
}

// ---------------------------------------------------------------- requests

/** Las reglas de `RequestPath` sobre rutas relativas al HOME; como mucho 64 entradas. */
export function validateExclude(exclude: readonly string[]): readonly string[] {
  if (exclude.length > PERSIST_EXCLUDE_MAX) {
    throw new InvalidArgumentError(
      `exclude admite como mucho ${PERSIST_EXCLUDE_MAX} entradas, recibidas ${exclude.length}`,
    );
  }
  for (const entry of exclude) {
    validateExcludeEntry(entry);
  }
  return exclude;
}

export function validateExcludeEntry(entry: unknown): string {
  if (typeof entry !== "string" || entry.length === 0) {
    throw new InvalidArgumentError("cada entrada de exclude debe ser una cadena no vacía");
  }
  if (entry.includes("\0")) {
    throw new InvalidArgumentError("una entrada de exclude contiene NUL");
  }
  if (entry.startsWith("/")) {
    throw new InvalidArgumentError(
      `exclude admite rutas relativas al HOME, no absolutas: ${JSON.stringify(entry)}`,
    );
  }
  if (new TextEncoder().encode(entry).length > EXCLUDE_MAX_BYTES) {
    throw new InvalidArgumentError("una entrada de exclude supera 4096 bytes");
  }
  const components = entry.split("/").filter((component) => component !== "" && component !== ".");
  if (components.includes("..")) {
    throw new InvalidArgumentError(`exclude no admite \`..\`: ${JSON.stringify(entry)}`);
  }
  if (components.length === 0) {
    throw new InvalidArgumentError(`exclude no admite una ruta vacía: ${JSON.stringify(entry)}`);
  }
  return entry;
}

export function validatePersistTimeoutMs(timeoutMs: number | undefined): number {
  const value = timeoutMs ?? DEFAULT_PERSIST_TIMEOUT_MS;
  if (!Number.isFinite(value) || value <= 0) {
    throw new InvalidArgumentError(
      `timeoutMs debe ser un número > 0, recibido ${String(timeoutMs)}`,
    );
  }
  return value;
}

export function checkpointRequest(
  target: S3Prefix,
  exclude: readonly string[],
  user: string | undefined,
): CheckpointRequest {
  return create(CheckpointRequestSchema, {
    target: create(S3LocationSchema, {
      bucket: target.bucket,
      keyPrefix: target.keyPrefix,
      region: target.region,
    }),
    exclude: [...validateExclude(exclude)],
    user: user === undefined ? undefined : create(UserSchema, { username: user }),
  });
}

export function restoreRequest(source: S3Prefix, user: string | undefined): RestoreRequest {
  return create(RestoreRequestSchema, {
    source: create(S3LocationSchema, {
      bucket: source.bucket,
      keyPrefix: source.keyPrefix,
      region: source.region,
    }),
    user: user === undefined ? undefined : create(UserSchema, { username: user }),
  });
}

/** `target`/`source` explícito, si no el `persist` del sandbox; siempre con `name`. */
export function resolveTarget(
  explicit: S3Prefix | undefined,
  bound: S3Prefix | undefined,
): S3Prefix {
  const target = explicit ?? bound;
  if (target === undefined) {
    throw new InvalidArgumentError(
      "no hay destino: pasa target (S3Prefix) o crea el sandbox con create({ persist })",
    );
  }
  if (target.name === undefined) {
    throw new InvalidArgumentError(
      "S3Prefix sin name: pásalo (new S3Prefix({ ..., name })) o usa el persist de " +
        "create({ persist }), que lo fija al sandboxId",
    );
  }
  return target;
}

// ------------------------------------------------------------ create rules

/** Regla 1 de D10: sin execution role no hay S3, y el SDK no adivina uno. */
export function requireRoleForPersist(
  persist: S3Prefix | undefined,
  executionRoleArn: string | undefined,
): void {
  if (persist !== undefined && executionRoleArn === undefined) {
    throw new InvalidArgumentError(
      "create({ persist }) requiere executionRoleArn: rayd lee S3 con las credenciales del " +
        "execution role (política `persistence` de spike/m0/iam.yaml)",
    );
  }
}

/** Regla 2 de D10: el `name` por defecto es el `sandboxId`. */
export function bindPersist(persist: S3Prefix, sandboxId: string): S3Prefix {
  return persist.name === undefined ? persist.withName(sandboxId) : persist;
}

/** Regla 4 de D10: `connect(id, { persist })` sólo enlaza y necesita el `name`. */
export function requireNamedPersist(persist: S3Prefix): S3Prefix {
  if (persist.name === undefined) {
    throw new InvalidArgumentError(
      "connect({ persist }) requiere S3Prefix con name: es el home que checkpointFiles() y " +
        "reincarnate() usarán",
    );
  }
  return persist;
}

/** Regla 3 de D10: sólo un `name` dado por el caller puede tener un checkpoint. */
export function shouldAutoRestore(persist: S3Prefix): boolean {
  return persist.name !== undefined;
}

export function reincarnateRequiresCreateError(): InvalidArgumentError {
  return new InvalidArgumentError(
    "reincarnate() necesita un sandbox creado con Sandbox.create({ persist }): un handle de " +
      "connect() no conoce las opciones de lanzamiento",
  );
}

export function reincarnateRequiresPersistError(): InvalidArgumentError {
  return new InvalidArgumentError(
    "reincarnate() necesita un sandbox con persist (create({ persist }) o " +
      "connect(id, { persist: new S3Prefix({ ..., name }) }))",
  );
}

export function reincarnateNote(uri: string): string {
  return `reincarnate(): el checkpoint en ${uri} está completo; el sandbox original sigue vivo y sin cambios`;
}

/**
 * Añade la nota de `reincarnate()` al `message` del error de `create()` y
 * devuelve la MISMA instancia, para que `code`, `state`, `exitCode` y el
 * resto de campos tipados sobrevivan (paridad con `add_note` en Python).
 * Un valor lanzado que no sea `Error` se envuelve en `SandboxError` con
 * `cause`.
 */
export function withReincarnateNote(error: unknown, uri: string): Error {
  if (error instanceof Error) {
    error.message = `${error.message}; ${reincarnateNote(uri)}`;
    return error;
  }
  return new SandboxError(`${String(error)}; ${reincarnateNote(uri)}`, { cause: error });
}

// ------------------------------------------------------------------ events

export function checkpointResultFrom(
  target: S3Prefix,
  done: {
    files: bigint;
    bytesRead: bigint;
    archiveBytes: bigint;
    sha256: string;
    skipped: bigint;
    durationMs: number;
  },
): CheckpointResult {
  return {
    bucket: target.bucket,
    keyPrefix: target.keyPrefix,
    uri: target.uri,
    files: Number(done.files),
    bytesRead: Number(done.bytesRead),
    archiveBytes: Number(done.archiveBytes),
    sha256: done.sha256,
    skipped: Number(done.skipped),
    durationMs: done.durationMs,
  };
}

export function restoreResultFrom(
  source: S3Prefix,
  done: {
    files: bigint;
    bytesWritten: bigint;
    archiveBytes: bigint;
    sha256: string;
    skipped: bigint;
    durationMs: number;
  },
): RestoreResult {
  return {
    bucket: source.bucket,
    keyPrefix: source.keyPrefix,
    uri: source.uri,
    files: Number(done.files),
    bytesWritten: Number(done.bytesWritten),
    archiveBytes: Number(done.archiveBytes),
    sha256: done.sha256,
    skipped: Number(done.skipped),
    durationMs: done.durationMs,
  };
}

/** El primer mensaje de ambos streams es `started`; un `error` temprano se traduce. */
export function requireStarted(
  first: CheckpointEvent | RestoreEvent | undefined,
  rpc: string,
): void {
  if (first === undefined) {
    throw new SandboxError(`${rpc} terminó sin mensajes`);
  }
  if (first.event.case === "error") {
    throw streamErrorFrom(first.event.value);
  }
  if (first.event.case !== "started") {
    throw new SandboxError(
      `${rpc} empezó con ${JSON.stringify(first.event.case ?? null)} en vez de started`,
    );
  }
}

/** `undefined` mientras el stream siga; el resultado en `done`; excepción en `error`. */
export function handleCheckpointEvent(
  event: CheckpointEvent,
  target: S3Prefix,
  onProgress: CheckpointProgressCallback | undefined,
): CheckpointResult | undefined {
  switch (event.event.case) {
    case "progress":
      onProgress?.({
        filesDone: Number(event.event.value.filesDone),
        bytesRead: Number(event.event.value.bytesRead),
        bytesUploaded: Number(event.event.value.bytesUploaded),
      });
      return undefined;
    case "done":
      return checkpointResultFrom(target, event.event.value);
    case "error":
      throw streamErrorFrom(event.event.value);
    default:
      return undefined;
  }
}

export function handleRestoreEvent(
  event: RestoreEvent,
  source: S3Prefix,
  onProgress: RestoreProgressCallback | undefined,
): RestoreResult | undefined {
  switch (event.event.case) {
    case "progress":
      onProgress?.({
        filesDone: Number(event.event.value.filesDone),
        bytesDownloaded: Number(event.event.value.bytesDownloaded),
      });
      return undefined;
    case "done":
      return restoreResultFrom(source, event.event.value);
    case "error":
      throw streamErrorFrom(event.event.value);
    default:
      return undefined;
  }
}

export function streamEndedEarly(rpc: string): PersistenceError {
  return new PersistenceError(interruptedMessage(`${rpc} terminó sin done`), {
    code: "interrupted",
  });
}

// ------------------------------------------------------------------ errors

/** `StreamError.code` tras `started` (tabla D8). */
export function streamErrorFrom(error: StreamError): Error {
  const { code, message } = error;
  switch (code) {
    case "not_found":
      return new NotFoundError(message);
    case "invalid_argument":
      return new InvalidArgumentError(message);
    case "deadline_exceeded":
      return new TimeoutError(message);
    case "suspending":
      return new PersistenceError(interruptedMessage("el sandbox se está suspendiendo"), {
        code: "interrupted",
      });
    case "unimplemented":
      return new PersistenceError(UNIMPLEMENTED_MESSAGE, { code: "unimplemented" });
    default:
      return new PersistenceError(message, { code: code === "" ? "internal" : code });
  }
}

/** Status gRPC antes del primer mensaje (tabla D8). */
export function statusError(error: unknown): Error {
  const connect = asConnectError(error);
  if (connect === undefined) {
    return error instanceof Error ? error : new SandboxError(String(error));
  }
  const message = connect.rawMessage;
  const base = { grpcCode: connect.code, cause: connect };
  switch (connect.code) {
    case Code.NotFound:
      return new NotFoundError(message, base);
    case Code.InvalidArgument:
      return new InvalidArgumentError(message, base);
    case Code.FailedPrecondition:
      return new PersistenceError(message, { ...base, code: "failed_precondition" });
    case Code.PermissionDenied:
      if (isProxyForbidden(connect)) {
        return new AuthenticationError(message, {
          grpcCode: connect.code,
          proxyRejected: true,
          cause: connect,
        });
      }
      return new PersistenceError(message, { ...base, code: "permission_denied" });
    case Code.Unauthenticated:
      return new AuthenticationError(message, { grpcCode: connect.code, cause: connect });
    case Code.Unimplemented:
      return new PersistenceError(UNIMPLEMENTED_MESSAGE, { ...base, code: "unimplemented" });
    case Code.DeadlineExceeded:
      return new TimeoutError(message, base);
    case Code.ResourceExhausted:
      return new PersistenceError(message, { ...base, code: "resource_exhausted" });
    case Code.Canceled:
      return new SandboxError(`llamada cancelada por el cliente: ${message}`, base);
    default:
      if (connect.code === Code.Unavailable || isStreamReset(connect)) {
        return new PersistenceError(
          interruptedMessage(message || Code[connect.code].toLowerCase()),
          {
            ...base,
            code: "interrupted",
          },
        );
      }
      return new PersistenceError(message, { ...base, code: "internal" });
  }
}

/** Un error después de `started`: nunca se reconecta. */
export function midStreamError(error: unknown): Error {
  const connect = asConnectError(error);
  if (connect === undefined) {
    return error instanceof Error ? error : new SandboxError(String(error));
  }
  const base = { grpcCode: connect.code, cause: connect };
  if (connect.code === Code.DeadlineExceeded) {
    return new TimeoutError(connect.rawMessage, base);
  }
  if (connect.code === Code.Canceled) {
    return new SandboxError(`llamada cancelada por el cliente: ${connect.rawMessage}`, base);
  }
  return new PersistenceError(
    interruptedMessage(connect.rawMessage || Code[connect.code].toLowerCase()),
    { ...base, code: "interrupted" },
  );
}

// ------------------------------------------------------------------ client

/** Los dos RPCs de persistencia de un `Sandbox`, sobre el transporte de streams. */
export class PersistenceClient {
  readonly #core: SandboxCore;

  constructor(core: SandboxCore) {
    this.#core = core;
  }

  async checkpoint(
    bound: S3Prefix | undefined,
    options: CheckpointFilesOptions,
  ): Promise<CheckpointResult> {
    const destination = resolveTarget(options.target, bound);
    const timeoutMs = validatePersistTimeoutMs(options.timeoutMs);
    const request = checkpointRequest(destination, options.exclude ?? [], options.user);
    this.#core.logger?.info?.("checkpoint hacia S3", {
      sandboxId: this.#core.sandboxId,
      uri: destination.uri,
    });
    const opened = await this.#open(
      (client, callOptions) => client.checkpoint(request, withTimeout(callOptions, timeoutMs)),
      "Checkpoint",
    );
    const result = await this.#consume(opened, (event) =>
      handleCheckpointEvent(event, destination, options.onProgress),
    );
    if (result === undefined) {
      throw streamEndedEarly("Checkpoint");
    }
    this.#core.logger?.info?.("checkpoint completo", {
      sandboxId: this.#core.sandboxId,
      uri: destination.uri,
      files: result.files,
      archiveBytes: result.archiveBytes,
    });
    return result;
  }

  async restore(bound: S3Prefix | undefined, options: RestoreFilesOptions): Promise<RestoreResult> {
    const origin = resolveTarget(options.source, bound);
    const timeoutMs = validatePersistTimeoutMs(options.timeoutMs);
    const request = restoreRequest(origin, options.user);
    this.#core.logger?.info?.("restore desde S3", {
      sandboxId: this.#core.sandboxId,
      uri: origin.uri,
    });
    const opened = await this.#open(
      (client, callOptions) => client.restore(request, withTimeout(callOptions, timeoutMs)),
      "Restore",
    );
    const result = await this.#consume(opened, (event) =>
      handleRestoreEvent(event, origin, options.onProgress),
    );
    if (result === undefined) {
      throw streamEndedEarly("Restore");
    }
    this.#core.logger?.info?.("restore completo", {
      sandboxId: this.#core.sandboxId,
      uri: origin.uri,
      files: result.files,
      bytesWritten: result.bytesWritten,
    });
    return result;
  }

  async #open<T extends CheckpointEvent | RestoreEvent>(
    start: StreamStarter<typeof FilesystemService, T>,
    rpc: string,
  ): Promise<OpenedStream<T>> {
    const opened = await this.#core.openStream(start, {
      service: FilesystemService,
      stream: true,
      translate: statusError,
    });
    try {
      requireStarted(opened.first, rpc);
    } catch (error) {
      opened.controller.abort();
      throw error;
    }
    return opened;
  }

  async #consume<T, R>(
    opened: OpenedStream<T>,
    handle: (event: T) => R | undefined,
  ): Promise<R | undefined> {
    try {
      if (opened.first !== undefined) {
        const result = handle(opened.first);
        if (result !== undefined) {
          return result;
        }
      }
      while (true) {
        const next = await opened.iterator.next();
        if (next.done) {
          return undefined;
        }
        const result = handle(next.value);
        if (result !== undefined) {
          return result;
        }
      }
    } catch (error) {
      if (error instanceof ConnectError) {
        throw midStreamError(error);
      }
      throw error;
    } finally {
      opened.controller.abort();
    }
  }
}
