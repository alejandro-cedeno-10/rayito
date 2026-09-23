/**
 * Transferencias por S3 (ADR-010): `uploadUrl`/`downloadUrl` y el enrutado de
 * ficheros grandes. El SDK firma cada URL con las credenciales del llamante y
 * `rayd` sólo mueve los bytes entre el VM y S3. Primero las funciones puras
 * (configuración, claves, vidas, plan multiparte, conversiones y la tabla de
 * errores de D13); después `S3Access`, que carga los paquetes de S3 con
 * `import()` en el primer uso (`import { Sandbox } from "rayito"` no los
 * carga); después `TransferClient`, que hace los cinco RPCs; al final
 * `UploadTicket` y `DownloadLink`.
 *
 * Nada aquí registra ni pone en un mensaje una URL, un bucket, una clave o
 * una ruta: una URL prefirmada es una credencial al portador.
 */

import { createHash, randomBytes } from "node:crypto";
import { inspect } from "node:util";
import type { S3Client, S3ClientConfig } from "@aws-sdk/client-s3";
import { create } from "@bufbuild/protobuf";
import { Code, ConnectError } from "@connectrpc/connect";
import { NodeHttpHandler } from "@smithy/node-http-handler";
import { awsClientSettingsOf, type ControlPlane } from "../aws/control-plane.js";
import {
  AuthenticationError,
  DiskFullError,
  FileNotFoundError,
  FileUploadError,
  InvalidArgumentError,
  SandboxError,
  TimeoutError,
  TransferError,
  UnimplementedError,
} from "../errors.js";
import { type EntryInfo as EntryInfoProto, UserSchema } from "../gen/rayito/v1/common_pb.js";
import {
  CancelTransferRequestSchema,
  FilesystemService,
  GetTransferRequestSchema,
  PresignedMultipartSchema,
  PresignedRequestSchema,
  S3ObjectSchema,
  type StartExportRequest,
  StartExportRequestSchema,
  type StartImportRequest,
  StartImportRequestSchema,
  TransferDirection,
  type TransferEvent,
  TransferPhase,
  type TransferState,
  WatchTransferRequestSchema,
} from "../gen/rayito/v1/filesystem_pb.js";
import {
  TRANSFER_DEFAULT_EXPIRES_IN_SECONDS,
  TRANSFER_DEFAULT_MAX_EXPIRES_IN_SECONDS,
  TRANSFER_DEFAULT_MULTIPART_THRESHOLD_BYTES,
  TRANSFER_DEFAULT_PREFIX,
  TRANSFER_DEFAULT_THRESHOLD_BYTES,
  TRANSFER_INTERNAL_EXPIRES_IN_SECONDS,
  TRANSFER_MULTIPART_THRESHOLD_MIN_BYTES,
  TRANSFER_PART_SIZE_MIN_BYTES,
  TRANSFER_PRESIGN_MAX_SECONDS,
  TRANSFER_SINGLE_PUT_MAX_BYTES,
  TRANSFER_THRESHOLD_MIN_BYTES,
} from "../limits.js";
import type {
  EntryInfo,
  ResolvedS3Staging,
  S3Staging,
  TransferDirectionName,
  TransferPhaseName,
  TransferStatus,
} from "../models.js";
import { asConnectError, isStreamReset, translateRpcError } from "../transport/errors.js";
import { ProxyTunnelAgent } from "../transport/proxy-tunnel.js";
import type { OpenedStream, SandboxCore } from "./core.js";
import type { S3Prefix } from "./persistence.js";
import { ReconnectBudget } from "./readiness.js";

const MIB = 1_048_576;
export const OCTET_STREAM = "application/octet-stream";
export const TRANSFER_BUCKET_ENV_VAR = "RAYITO_TRANSFER_BUCKET";
export const TRANSFER_PREFIX_ENV_VAR = "RAYITO_TRANSFER_PREFIX";
export const TRANSFER_REGION_ENV_VAR = "RAYITO_TRANSFER_REGION";
export const MISSING_STAGING_REASON =
  "configura transfer: { bucket } (S3Staging) o RAYITO_TRANSFER_BUCKET";
export const OUTDATED_IMAGE_REASON = "actualiza la imagen: este rayd no tiene transferencias";
export const UPLOAD_PART_BYTES = 8 * MIB;
export const UPLOAD_QUEUE_SIZE = 8;
const CONTENT_TYPE_HEADER = "Content-Type";
const STAGING_TOKEN_BYTES = 16;
const EXPORT_BUDGET_BASE_SECONDS = 900;
const EXPORT_BUDGET_BYTES_PER_SECOND = 1_000_000;
const DELETE_GRACE_SECONDS = 3600;
const PART_SIZE_DIVISOR = 1000;
const CAPABILITY_PROBE_ID = "";
const TRANSFER_PREFIX_MAX_BYTES = 256;
const ARTIFACT_NAMESPACE = "rayito";
const DNS_BUCKET_PATTERN = /^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$/;
const PREFIX_SEGMENT_PATTERN = /^[A-Za-z0-9_.-]+$/;
const REGION_PATTERN = /^[a-z]{2}(-gov)?-[a-z]+-[0-9]$/;
const RFC3986_UNRESERVED = /^[A-Za-z0-9\-._~]$/;
const S3_CREDENTIAL_ERRORS: ReadonlySet<string> = new Set([
  "AccessDenied",
  "ExpiredToken",
  "InvalidAccessKeyId",
  "SignatureDoesNotMatch",
  "InvalidToken",
]);
const S3_REGION_ERRORS: ReadonlySet<string> = new Set([
  "PermanentRedirect",
  "AuthorizationHeaderMalformed",
  "IllegalLocationConstraintException",
]);
const CREDENTIALS_PROVIDER_ERROR = "CredentialsProviderError";
const ABORT_ERROR = "AbortError";

export type KeyDirection = "up" | "down";
export type UploadMethod = "PUT" | "POST";
export type Environment = Readonly<Record<string, string | undefined>>;

/** Configuración extra de los clientes S3 del SDK; sólo la usan los tests, hacia un S3 falso local. */
export type S3ClientOverrides = Readonly<Partial<S3ClientConfig>>;

const PHASE_NAMES: Readonly<Record<number, TransferPhaseName>> = {
  [TransferPhase.WAITING]: "waiting",
  [TransferPhase.RUNNING]: "running",
  [TransferPhase.DONE]: "done",
  [TransferPhase.FAILED]: "failed",
  [TransferPhase.CANCELLED]: "cancelled",
};
const DIRECTION_NAMES: Readonly<Record<number, TransferDirectionName>> = {
  [TransferDirection.IMPORT]: "import",
  [TransferDirection.EXPORT]: "export",
};
const TERMINAL_PHASES: ReadonlySet<TransferPhase> = new Set([
  TransferPhase.DONE,
  TransferPhase.FAILED,
  TransferPhase.CANCELLED,
]);

// -------------------------------------------------------------- configuration

function validateTransferBucket(bucket: unknown): string {
  if (typeof bucket !== "string" || !DNS_BUCKET_PATTERN.test(bucket)) {
    throw new InvalidArgumentError(
      "S3Staging.bucket debe ser un nombre DNS sin puntos: 3-63 minúsculas, dígitos o `-`, " +
        "empezando y terminando en letra o dígito",
    );
  }
  if (bucket.startsWith("xn--") || bucket.endsWith("-s3alias")) {
    throw new InvalidArgumentError(
      "S3Staging.bucket no puede empezar por `xn--` ni terminar en `-s3alias`",
    );
  }
  return bucket;
}

/** Segmentos `[A-Za-z0-9_.-]` unidos por `/`, 1-256 bytes, disjunto por componentes de `rayito`. */
function validateTransferPrefix(prefix: unknown): string {
  if (typeof prefix !== "string" || prefix.length === 0) {
    throw new InvalidArgumentError("S3Staging.prefix debe ser una cadena no vacía");
  }
  if (Buffer.byteLength(prefix, "utf8") > TRANSFER_PREFIX_MAX_BYTES) {
    throw new InvalidArgumentError(
      `S3Staging.prefix no puede superar ${TRANSFER_PREFIX_MAX_BYTES} bytes`,
    );
  }
  const segments = prefix.split("/");
  if (segments.some((segment) => segment === "" || segment === "." || segment === "..")) {
    throw new InvalidArgumentError(
      "S3Staging.prefix no admite `/` al principio o al final, `//` ni segmentos `.` o `..`",
    );
  }
  if (segments.some((segment) => !PREFIX_SEGMENT_PATTERN.test(segment))) {
    throw new InvalidArgumentError(
      "S3Staging.prefix sólo admite letras, dígitos, `_`, `.` y `-` entre `/`",
    );
  }
  if (prefixesOverlap(prefix, ARTIFACT_NAMESPACE)) {
    throw new InvalidArgumentError(
      "S3Staging.prefix no puede solaparse con el espacio de artefactos `rayito`",
    );
  }
  return prefix;
}

function validateTransferRegion(region: unknown): string | undefined {
  if (region === undefined) {
    return undefined;
  }
  if (typeof region !== "string" || !REGION_PATTERN.test(region)) {
    throw new InvalidArgumentError("S3Staging.region no es un código de región de AWS válido");
  }
  return region;
}

function validateIntegerBetween(value: unknown, field: string, low: number, high: number): number {
  if (typeof value !== "number" || !Number.isInteger(value) || value < low || value > high) {
    throw new InvalidArgumentError(`${field} debe ser un entero entre ${low} y ${high}`);
  }
  return value;
}

/**
 * `transfer` explícito, `null` (sin staging aunque exista el entorno) o
 * `undefined` (`RAYITO_TRANSFER_BUCKET`, `RAYITO_TRANSFER_PREFIX`,
 * `RAYITO_TRANSFER_REGION`). Los mensajes nunca repiten el valor rechazado.
 */
export function resolveS3Staging(
  input: S3Staging | null | undefined,
  env: Environment = process.env,
): ResolvedS3Staging | undefined {
  if (input === null) {
    return undefined;
  }
  if (input === undefined) {
    return stagingFromEnv(env);
  }
  if (typeof input !== "object") {
    throw new InvalidArgumentError("transfer debe ser un S3Staging, null o undefined");
  }
  return Object.freeze({
    bucket: validateTransferBucket(input.bucket),
    prefix: validateTransferPrefix(input.prefix ?? TRANSFER_DEFAULT_PREFIX),
    region: validateTransferRegion(input.region),
    maxExpiresIn: validateIntegerBetween(
      input.maxExpiresIn ?? TRANSFER_DEFAULT_MAX_EXPIRES_IN_SECONDS,
      "S3Staging.maxExpiresIn",
      1,
      TRANSFER_PRESIGN_MAX_SECONDS,
    ),
    thresholdBytes: validateIntegerBetween(
      input.thresholdBytes ?? TRANSFER_DEFAULT_THRESHOLD_BYTES,
      "S3Staging.thresholdBytes",
      TRANSFER_THRESHOLD_MIN_BYTES,
      TRANSFER_SINGLE_PUT_MAX_BYTES,
    ),
    multipartThresholdBytes: validateIntegerBetween(
      input.multipartThresholdBytes ?? TRANSFER_DEFAULT_MULTIPART_THRESHOLD_BYTES,
      "S3Staging.multipartThresholdBytes",
      TRANSFER_MULTIPART_THRESHOLD_MIN_BYTES,
      TRANSFER_SINGLE_PUT_MAX_BYTES,
    ),
  });
}

function stagingFromEnv(env: Environment): ResolvedS3Staging | undefined {
  const bucket = env[TRANSFER_BUCKET_ENV_VAR];
  if (!bucket) {
    return undefined;
  }
  return resolveS3Staging(
    {
      bucket,
      prefix: env[TRANSFER_PREFIX_ENV_VAR] || undefined,
      region: env[TRANSFER_REGION_ENV_VAR] || undefined,
    },
    env,
  );
}

/** Dos prefijos se pisan cuando uno es prefijo del otro por componentes (`data` y `data/tmp`, no `data-tmp`). */
export function prefixesOverlap(first: string, second: string): boolean {
  const firstParts = first.split("/");
  const secondParts = second.split("/");
  const shared = Math.min(firstParts.length, secondParts.length);
  return firstParts.slice(0, shared).every((segment, index) => segment === secondParts[index]);
}

/**
 * En el mismo bucket, el prefijo de transferencias y el de persistencia deben
 * ser disjuntos por componentes: la regla de ciclo de vida de un día del
 * primero borraría los checkpoints del segundo.
 */
export function validateStagingAgainstPersist(
  staging: ResolvedS3Staging | undefined,
  persist: S3Prefix | undefined,
): void {
  if (staging === undefined || persist === undefined || staging.bucket !== persist.bucket) {
    return;
  }
  if (prefixesOverlap(staging.prefix, persist.prefix)) {
    throw new InvalidArgumentError(
      "transfer.prefix y persist.prefix comparten bucket y se solapan: la regla de ciclo de " +
        "vida del prefijo de transferencias borraría los checkpoints; usa prefijos disjuntos",
    );
  }
}

export function missingStagingError(feature: string): UnimplementedError {
  return new UnimplementedError(feature, MISSING_STAGING_REASON);
}

export function outdatedImageError(feature: string): UnimplementedError {
  return new UnimplementedError(feature, OUTDATED_IMAGE_REASON);
}

// --------------------------------------------------------------- staging keys

/** Un objeto de staging: `key` es `<prefix>/<sandboxId>/<up|down>/<hex>` y nunca lleva la ruta del usuario. */
export interface StagingObject {
  readonly bucket: string;
  readonly key: string;
  readonly region: string;
}

export function newStagingToken(): string {
  return randomBytes(STAGING_TOKEN_BYTES).toString("hex");
}

export function stagingKey(
  prefix: string,
  sandboxId: string,
  direction: KeyDirection,
  token: string = newStagingToken(),
): string {
  return `${prefix}/${sandboxId}/${direction}/${token}`;
}

// ------------------------------------------------------------------ lifetimes

export function validateExpiresIn(expiresIn: unknown, field = "expiresIn"): number {
  if (typeof expiresIn !== "number" || !Number.isInteger(expiresIn)) {
    throw new InvalidArgumentError(`${field} debe ser un entero de segundos`);
  }
  if (expiresIn <= 0) {
    throw new InvalidArgumentError(`${field} debe ser > 0`);
  }
  return expiresIn;
}

/** `min(expiresIn, maxExpiresIn, 604800)`: S3 rechaza una firma de más de 7 días. */
export function effectiveExpiresIn(expiresIn: unknown, staging: ResolvedS3Staging): number {
  return Math.min(validateExpiresIn(expiresIn), staging.maxExpiresIn, TRANSFER_PRESIGN_MAX_SECONDS);
}

/** `useSignatureExpiration` de E2B: ausente son 3600 s; `<= 0` no tiene sentido con URLs que siempre caducan. */
export function expiresInFromSignatureExpiration(useSignatureExpiration: unknown): number {
  if (useSignatureExpiration === undefined) {
    return TRANSFER_DEFAULT_EXPIRES_IN_SECONDS;
  }
  return validateExpiresIn(useSignatureExpiration, "useSignatureExpiration");
}

function cappedLifetime(seconds: number): number {
  return Math.min(seconds, TRANSFER_PRESIGN_MAX_SECONDS);
}

/** El DELETE de limpieza vive una hora más que el ticket: la importación puede terminar justo al vencer el GET. */
export function deleteLifetime(userLifetime: number): number {
  return cappedLifetime(userLifetime + DELETE_GRACE_SECONDS);
}

export function internalGetLifetime(staging: ResolvedS3Staging): number {
  return Math.min(TRANSFER_INTERNAL_EXPIRES_IN_SECONDS, staging.maxExpiresIn);
}

/** 900 s más 1 s por MB (una exportación a 1 MB/s), topado en 7 días. */
export function exportBudgetSeconds(size: number): number {
  return cappedLifetime(
    EXPORT_BUDGET_BASE_SECONDS + Math.ceil(size / EXPORT_BUDGET_BYTES_PER_SECOND),
  );
}

export function validateMaxBytes(maxBytes: unknown): number | undefined {
  if (maxBytes === undefined) {
    return undefined;
  }
  if (typeof maxBytes !== "number" || !Number.isInteger(maxBytes) || maxBytes < 1) {
    throw new InvalidArgumentError("maxBytes debe ser undefined o un entero >= 1");
  }
  return maxBytes;
}

// ------------------------------------------------------------------ user urls

/**
 * El PUT crudo recomienda `Content-Type: application/octet-stream` (sin
 * firmarlo: un PUT sin cabeceras también vale); el formulario POST lleva sus
 * propios campos.
 */
export function uploadTicketHeaders(form: boolean): Readonly<Record<string, string>> {
  return form ? {} : { [CONTENT_TYPE_HEADER]: OCTET_STREAM };
}

export function downloadFilename(path: string, filename: unknown): string {
  if (filename === undefined) {
    return path.replace(/\/+$/, "").split("/").pop() || "download";
  }
  if (typeof filename !== "string" || filename.length === 0 || filename.includes("\0")) {
    throw new InvalidArgumentError("filename debe ser una cadena no vacía y sin NUL");
  }
  return filename;
}

function percentEncodeByte(byte: number): string {
  const char = String.fromCharCode(byte);
  if (byte < 0x80 && RFC3986_UNRESERVED.test(char)) {
    return char;
  }
  return `%${byte.toString(16).toUpperCase().padStart(2, "0")}`;
}

/** `attachment; filename="<ascii>"; filename*=UTF-8''<pct>`: cada byte UTF-8 fuera de 0x20-0x7E, `"` o `\` pasa a `_`. */
export function contentDisposition(filename: string): string {
  const utf8 = new TextEncoder().encode(filename);
  const ascii = Array.from(utf8, (byte) =>
    byte >= 0x20 && byte <= 0x7e && byte !== 0x22 && byte !== 0x5c
      ? String.fromCharCode(byte)
      : "_",
  ).join("");
  const encoded = Array.from(utf8, percentEncodeByte).join("");
  return `attachment; filename="${ascii}"; filename*=UTF-8''${encoded}`;
}

// ------------------------------------------------------------------ multipart

export interface PartPlan {
  readonly partSize: number;
  readonly parts: number;
}

function ceilToMib(size: number): number {
  return Math.ceil(size / MIB) * MIB;
}

/**
 * `undefined` por debajo de `multipartThresholdBytes` (un solo PUT); si no,
 * partes de `max(8 MiB, ceil_to_MiB(ceil(size / 1000)))`: como mucho 1000
 * URLs, que caben en un `StartExportRequest`.
 */
export function partPlan(size: number, staging: ResolvedS3Staging): PartPlan | undefined {
  if (size < staging.multipartThresholdBytes) {
    return undefined;
  }
  const partSize = Math.max(
    TRANSFER_PART_SIZE_MIN_BYTES,
    ceilToMib(Math.ceil(size / PART_SIZE_DIVISOR)),
  );
  return { partSize, parts: Math.max(1, Math.ceil(size / partSize)) };
}

// -------------------------------------------------------------------- routing

/** Por S3 lo que mide `>= thresholdBytes` y todo stream de tamaño desconocido; nada sin staging. */
export function shouldRoute(
  size: number | undefined,
  staging: ResolvedS3Staging | undefined,
): boolean {
  if (staging === undefined) {
    return false;
  }
  return size === undefined || size >= staging.thresholdBytes;
}

/**
 * El plazo de una operación enrutada: `requestTimeoutMs` cubre la operación
 * entera; sin él, `60 s + 1 s por MB` del tamaño, contados desde que empezó.
 */
export class OperationDeadline {
  readonly #startedAt: number;
  readonly #budgetFor: (size: number) => number;
  readonly #now: () => number;
  readonly #explicit: boolean;

  constructor(budgetFor: (size: number) => number, now: () => number, explicit = true) {
    this.#budgetFor = budgetFor;
    this.#now = now;
    this.#explicit = explicit;
    this.#startedAt = now();
  }

  /**
   * Lo que le queda a la pata S3: sin `requestTimeoutMs` y con un stream de
   * tamaño desconocido no hay presupuesto por MB que calcular, así que la
   * subida no se acota (el plazo se aplica al import que sigue).
   */
  budgetMs(size: number | undefined): number | undefined {
    if (size === undefined && !this.#explicit) {
      return undefined;
    }
    return this.remainingMs(size ?? 0);
  }

  /** Siempre un entero de ms: `grpc-timeout` no admite fracciones. */
  remainingMs(size: number): number {
    const left = this.#budgetFor(size) - (this.#now() - this.#startedAt);
    if (left <= 0) {
      throw transferTimeoutError();
    }
    return Math.ceil(left);
  }
}

// ------------------------------------------------------------------- requests

function owner(username: string | undefined) {
  return username ? create(UserSchema, { username }) : undefined;
}

function s3Object(target: StagingObject) {
  return create(S3ObjectSchema, {
    bucket: target.bucket,
    key: target.key,
    region: target.region,
  });
}

function presigned(url: string) {
  return create(PresignedRequestSchema, { url });
}

export interface ImportRequestInit {
  readonly path: string;
  readonly user: string | undefined;
  readonly mode: number | undefined;
  readonly target: StagingObject;
  readonly getUrl: string;
  readonly deleteUrl: string;
  readonly waitForObject: boolean;
  readonly expiresAt: Date;
  readonly maxBytes: number | undefined;
  readonly expectedSha256?: string | undefined;
  readonly metadata?: Readonly<Record<string, string>> | undefined;
}

export function startImportRequest(init: ImportRequestInit): StartImportRequest {
  return create(StartImportRequestSchema, {
    path: init.path,
    user: owner(init.user),
    mode: init.mode,
    object: s3Object(init.target),
    get: presigned(init.getUrl),
    delete: presigned(init.deleteUrl),
    waitForObject: init.waitForObject,
    expiresAtUnixMs: BigInt(init.expiresAt.getTime()),
    maxBytes: BigInt(init.maxBytes ?? 0),
    expectedSha256: init.expectedSha256 ?? "",
    metadata: { ...(init.metadata ?? {}) },
  });
}

export interface ExportRequestInit {
  readonly path: string;
  readonly user: string | undefined;
  readonly target: StagingObject;
  readonly expiresAt: Date;
  readonly putUrl?: string | undefined;
  readonly partUrls?: readonly string[] | undefined;
  readonly partSize?: number | undefined;
}

export function startExportRequest(init: ExportRequestInit): StartExportRequest {
  const target =
    init.putUrl === undefined
      ? {
          case: "multipart" as const,
          value: create(PresignedMultipartSchema, {
            partSize: BigInt(init.partSize ?? 0),
            parts: (init.partUrls ?? []).map(presigned),
          }),
        }
      : { case: "put" as const, value: presigned(init.putUrl) };
  return create(StartExportRequestSchema, {
    path: init.path,
    user: owner(init.user),
    object: s3Object(init.target),
    target,
    expiresAtUnixMs: BigInt(init.expiresAt.getTime()),
  });
}

// ---------------------------------------------------------------- conversions

export function isTerminal(state: TransferState): boolean {
  return TERMINAL_PHASES.has(state.phase);
}

/** `[code, reason]` de `TransferState.error`: `rayd` escribe `"<reason>: <frase>"`; un `CANCELLED` sin error es `cancelled`. */
export function stateError(state: TransferState): readonly [string, string] {
  if (state.error === undefined) {
    const fallback = state.phase === TransferPhase.CANCELLED ? "cancelled" : "internal";
    return [fallback, fallback];
  }
  const code = state.error.code || "internal";
  const message = state.error.message;
  const colon = message.indexOf(":");
  const reason = colon >= 0 ? message.slice(0, colon).trim() : code;
  return [code, reason || code];
}

function failureMessage(state: TransferState): string {
  const [code, reason] = stateError(state);
  const message = state.error?.message ?? "";
  if (message.startsWith(`${reason}:`)) {
    return message;
  }
  return `${reason}: ${message || code}`;
}

export function transferStatusFromProto(state: TransferState): TransferStatus {
  const [errorCode, errorReason] =
    state.error === undefined ? [undefined, undefined] : stateError(state);
  return Object.freeze({
    transferId: state.transferId,
    direction: DIRECTION_NAMES[state.direction] ?? "import",
    phase: PHASE_NAMES[state.phase] ?? "waiting",
    bytesDone: Number(state.bytesDone),
    bytesTotal: Number(state.bytesTotal),
    probes: state.probes,
    errorCode,
    errorReason,
  });
}

/**
 * Tabla D13: cada código con error propio lo usa; el resto es
 * `FileUploadError` en una importación y `TransferError` en una exportación.
 * Todo mensaje empieza por `"<reason>: "`.
 */
export function failureFromState(state: TransferState, direction: TransferDirectionName): Error {
  const [code, reason] = stateError(state);
  const message = failureMessage(state);
  switch (code) {
    case "deadline_exceeded":
      return new TimeoutError(message);
    case "invalid_argument":
      return new InvalidArgumentError(message);
    case "resource_exhausted":
      return new DiskFullError(message);
    case "permission_denied":
      return new AuthenticationError(message, { proxyRejected: false });
    case "not_found":
      return new FileNotFoundError(message);
    default:
      return direction === "import"
        ? new FileUploadError(message, { code, reason })
        : new TransferError(message, { code, reason });
  }
}

export function checksumMismatchError(): TransferError {
  return new TransferError(
    "checksum_mismatch: el sha256 de lo descargado no coincide con el de la exportación",
    { code: "failed_precondition", reason: "checksum_mismatch" },
  );
}

function transferDeadlineError(transferId: string): TimeoutError {
  return new TimeoutError(
    `la transferencia ${transferId} no terminó en el plazo pedido; sigue en curso en el sandbox`,
  );
}

/** La tabla unaria salvo `Unimplemented`, que en un RPC de transferencia sólo significa un `rayd` anterior a M9. */
export function transferRpcError(error: unknown, feature: string, filesystem: boolean): Error {
  if (asConnectError(error)?.code === Code.Unimplemented) {
    return outdatedImageError(feature);
  }
  return translateRpcError(error, { filesystem });
}

/** `GetTransfer("")`: `NotFound` es un agente M9, `Unimplemented` uno anterior; lo demás se propaga. */
export function probeSupportsTransfers(error: unknown): boolean {
  const code = asConnectError(error)?.code;
  if (code === Code.NotFound) {
    return true;
  }
  if (code === Code.Unimplemented) {
    return false;
  }
  throw translateRpcError(error);
}

// ------------------------------------------------------------- idle guarding

export function idleTimeoutError(idleMs: number): TimeoutError {
  return new TimeoutError(
    `stream_idle_timeout: no llegó ningún chunk en ${idleMs} ms; la llamada se canceló`,
  );
}

/**
 * Espera `next` como mucho `idleMs`; al vencer llama a `onIdle` (que cancela
 * la llamada) y rechaza con `TimeoutError("stream_idle_timeout: …")`. Sólo
 * cuenta la espera del siguiente chunk, nunca lo que tarda el consumidor.
 */
export async function nextWithinIdle<T>(
  next: Promise<T>,
  idleMs: number | undefined,
  onIdle: () => void,
): Promise<T> {
  if (idleMs === undefined) {
    return next;
  }
  let timer: ReturnType<typeof setTimeout> | undefined;
  const idle = new Promise<never>((_, reject) => {
    timer = setTimeout(() => {
      onIdle();
      reject(idleTimeoutError(idleMs));
    }, idleMs);
  });
  try {
    return await Promise.race([next, idle]);
  } finally {
    clearTimeout(timer);
  }
}

/** `work` dentro de `ms` (sin `ms`, sin plazo); al vencer, `onTimeout` corta la pata y rechaza. */
async function withinBudget<T>(
  work: Promise<T>,
  ms: number | undefined,
  onTimeout: () => void,
): Promise<T> {
  if (ms === undefined) {
    return work;
  }
  return raceDeadline(work, ms, () => {
    onTimeout();
    return transferTimeoutError();
  });
}

function transferTimeoutError(): TimeoutError {
  return new TimeoutError("la transferencia agotó su plazo antes de terminar");
}

function raceDeadline<T>(work: Promise<T>, ms: number, onTimeout: () => Error): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(onTimeout()), ms);
  });
  return Promise.race([work, deadline]).finally(() => clearTimeout(timer));
}

// ------------------------------------------------------------------ S3 access

interface S3Modules {
  readonly client: typeof import("@aws-sdk/client-s3");
  readonly presigner: typeof import("@aws-sdk/s3-request-presigner");
  readonly post: typeof import("@aws-sdk/s3-presigned-post");
  readonly storage: typeof import("@aws-sdk/lib-storage");
}

interface S3Clients {
  readonly modules: S3Modules;
  readonly presign: S3Client;
  readonly data: S3Client;
}

let s3Modules: Promise<S3Modules> | undefined;

function loadS3Modules(): Promise<S3Modules> {
  s3Modules ??= Promise.all([
    import("@aws-sdk/client-s3"),
    import("@aws-sdk/s3-request-presigner"),
    import("@aws-sdk/s3-presigned-post"),
    import("@aws-sdk/lib-storage"),
  ]).then(
    ([client, presigner, post, storage]) => ({ client, presigner, post, storage }),
    (error: unknown) => {
      s3Modules = undefined;
      throw error;
    },
  );
  return s3Modules;
}

function awsErrorCode(error: Error): string {
  const code = (error as { Code?: unknown }).Code;
  return typeof code === "string" && code.length > 0 ? code : error.name;
}

function isServiceException(error: Error): boolean {
  return "$metadata" in error && "$fault" in error;
}

function isOwnError(error: Error): boolean {
  return (
    error instanceof SandboxError ||
    error instanceof AuthenticationError ||
    error.name === ABORT_ERROR
  );
}

/**
 * Errores de las llamadas del SDK a S3 con las credenciales del llamante, por
 * código: el mensaje nunca lleva bucket, clave ni host (un error de red de
 * Node nombra el host, que contiene el bucket), sólo el nombre del error.
 */
export function translateS3Error(error: unknown): Error {
  if (!(error instanceof Error)) {
    return new SandboxError("fallo desconocido en una transferencia con S3");
  }
  if (isOwnError(error)) {
    return error;
  }
  if (error.name === CREDENTIALS_PROVIDER_ERROR) {
    return new AuthenticationError("no hay credenciales de AWS para firmar la transferencia");
  }
  const code = awsErrorCode(error);
  if (S3_CREDENTIAL_ERRORS.has(code)) {
    return new AuthenticationError(`S3 rechazó las credenciales del llamante (${code})`, {
      awsCode: code,
    });
  }
  if (code === "NoSuchBucket") {
    return new InvalidArgumentError("el bucket de transferencias no existe (NoSuchBucket)", {
      awsCode: code,
    });
  }
  if (S3_REGION_ERRORS.has(code)) {
    return new InvalidArgumentError(`S3Staging.region no es la región del bucket (${code})`, {
      awsCode: code,
    });
  }
  if (isServiceException(error)) {
    return new SandboxError(`S3 respondió ${code} a una transferencia`, { awsCode: code });
  }
  return new SandboxError(`fallo al hablar con S3 en una transferencia (${error.name})`);
}

/**
 * Los dos clientes S3 del SDK, creados en el primer uso: el que prefirma fija
 * `requestChecksumCalculation: "WHEN_REQUIRED"` (sin él la URL de `PutObject`
 * puede llevar parámetros de checksum que un PUT de terceros no manda, Q72);
 * el de datos, para las subidas y bajadas propias del SDK, no lo fija. Las
 * credenciales y el proxy son los del plano de control (`s3ClientOverridesFor`).
 */
export class S3Access {
  readonly #region: string;
  readonly #overrides: S3ClientOverrides;
  #clients: Promise<S3Clients> | undefined;

  constructor(region: string, overrides: S3ClientOverrides = {}) {
    this.#region = region;
    this.#overrides = overrides;
  }

  presignPut(bucket: string, key: string, expiresIn: number): Promise<string> {
    return this.#call(({ modules, presign }) =>
      modules.presigner.getSignedUrl(
        presign,
        new modules.client.PutObjectCommand({ Bucket: bucket, Key: key }),
        { expiresIn },
      ),
    );
  }

  presignGet(
    bucket: string,
    key: string,
    expiresIn: number,
    disposition?: string | undefined,
  ): Promise<string> {
    return this.#call(({ modules, presign }) =>
      modules.presigner.getSignedUrl(
        presign,
        new modules.client.GetObjectCommand({
          Bucket: bucket,
          Key: key,
          ...(disposition === undefined ? {} : { ResponseContentDisposition: disposition }),
        }),
        { expiresIn },
      ),
    );
  }

  presignDelete(bucket: string, key: string, expiresIn: number): Promise<string> {
    return this.#call(({ modules, presign }) =>
      modules.presigner.getSignedUrl(
        presign,
        new modules.client.DeleteObjectCommand({ Bucket: bucket, Key: key }),
        { expiresIn },
      ),
    );
  }

  presignUploadPart(
    bucket: string,
    key: string,
    uploadId: string,
    partNumber: number,
    expiresIn: number,
  ): Promise<string> {
    return this.#call(({ modules, presign }) =>
      modules.presigner.getSignedUrl(
        presign,
        new modules.client.UploadPartCommand({
          Bucket: bucket,
          Key: key,
          UploadId: uploadId,
          PartNumber: partNumber,
        }),
        { expiresIn },
      ),
    );
  }

  /** Formulario POST con `content-length-range [0, maxBytes]` (sin `Fields`). */
  presignPost(
    bucket: string,
    key: string,
    maxBytes: number,
    expiresIn: number,
  ): Promise<{ readonly url: string; readonly fields: Readonly<Record<string, string>> }> {
    return this.#call(({ modules, presign }) =>
      modules.post.createPresignedPost(presign, {
        Bucket: bucket,
        Key: key,
        Conditions: [["content-length-range", 0, maxBytes]],
        Expires: expiresIn,
      }),
    );
  }

  createMultipartUpload(bucket: string, key: string): Promise<string> {
    return this.#call(async ({ modules, data }) => {
      const output = await data.send(
        new modules.client.CreateMultipartUploadCommand({
          Bucket: bucket,
          Key: key,
          ContentType: OCTET_STREAM,
        }),
      );
      if (!output.UploadId) {
        throw new SandboxError("CreateMultipartUpload respondió sin UploadId");
      }
      return output.UploadId;
    });
  }

  completeMultipartUpload(
    bucket: string,
    key: string,
    uploadId: string,
    etags: readonly string[],
  ): Promise<void> {
    return this.#call(async ({ modules, data }) => {
      await data.send(
        new modules.client.CompleteMultipartUploadCommand({
          Bucket: bucket,
          Key: key,
          UploadId: uploadId,
          MultipartUpload: {
            Parts: etags.map((etag, index) => ({ ETag: etag, PartNumber: index + 1 })),
          },
        }),
      );
    });
  }

  abortMultipartUpload(bucket: string, key: string, uploadId: string): Promise<void> {
    return this.#call(async ({ modules, data }) => {
      await data.send(
        new modules.client.AbortMultipartUploadCommand({
          Bucket: bucket,
          Key: key,
          UploadId: uploadId,
        }),
      );
    });
  }

  /** Subida propia del SDK (`@aws-sdk/lib-storage`): partes de 8 MiB, 8 en paralelo, también sin tamaño conocido. */
  upload(
    bucket: string,
    key: string,
    body: Uint8Array | ReadableStream<Uint8Array>,
    abortController?: AbortController,
  ): Promise<void> {
    return this.#call(async ({ modules, data }) => {
      const upload = new modules.storage.Upload({
        client: data,
        params: { Bucket: bucket, Key: key, Body: body, ContentType: OCTET_STREAM },
        partSize: UPLOAD_PART_BYTES,
        queueSize: UPLOAD_QUEUE_SIZE,
        ...(abortController === undefined ? {} : { abortController }),
      });
      await upload.done();
    });
  }

  getObject(bucket: string, key: string, signal: AbortSignal): Promise<ReadableStream<Uint8Array>> {
    return this.#call(async ({ modules, data }) => {
      const output = await data.send(
        new modules.client.GetObjectCommand({ Bucket: bucket, Key: key }),
        { abortSignal: signal },
      );
      if (output.Body === undefined) {
        throw new SandboxError("GetObject respondió sin cuerpo");
      }
      return output.Body.transformToWebStream() as ReadableStream<Uint8Array>;
    });
  }

  deleteObject(bucket: string, key: string): Promise<void> {
    return this.#call(async ({ modules, data }) => {
      await data.send(new modules.client.DeleteObjectCommand({ Bucket: bucket, Key: key }));
    });
  }

  async #call<T>(action: (clients: S3Clients) => Promise<T>): Promise<T> {
    try {
      return await action(await this.#ready());
    } catch (error) {
      throw translateS3Error(error);
    }
  }

  #ready(): Promise<S3Clients> {
    this.#clients ??= loadS3Modules().then((modules) => ({
      modules,
      presign: new modules.client.S3Client({
        region: this.#region,
        requestChecksumCalculation: "WHEN_REQUIRED",
        ...this.#overrides,
      }),
      data: new modules.client.S3Client({ region: this.#region, ...this.#overrides }),
    }));
    return this.#clients;
  }
}

/**
 * La configuración que los clientes S3 heredan del plano de control: sus
 * credenciales y, con `proxy`, un `NodeHttpHandler` cuyo `httpsAgent` es el
 * mismo túnel `CONNECT` (`ProxyTunnelAgent`). Sin nada, la cadena por
 * defecto y conexión directa, como antes.
 */
export function s3ClientOverridesFor(plane: ControlPlane): S3ClientOverrides {
  const settings = awsClientSettingsOf(plane);
  return {
    ...(settings.credentials === undefined ? {} : { credentials: settings.credentials }),
    ...(settings.proxy === undefined
      ? {}
      : {
          requestHandler: new NodeHttpHandler({ httpsAgent: new ProxyTunnelAgent(settings.proxy) }),
        }),
  };
}

export function createS3Access(region: string, overrides: S3ClientOverrides = {}): S3Access {
  return new S3Access(region, overrides);
}

// ------------------------------------------------------------- hashing bodies

interface HashingBody {
  readonly body: Uint8Array | ReadableStream<Uint8Array>;
  digest(): string;
  size(): number;
}

/** Lo que sube `S3Access.upload`: el sha256 y el tamaño se calculan al vuelo, sin materializar un stream. */
function hashingBody(source: Uint8Array | ReadableStream<Uint8Array>): HashingBody {
  if (source instanceof Uint8Array) {
    const digest = createHash("sha256").update(source).digest("hex");
    return { body: source, digest: () => digest, size: () => source.byteLength };
  }
  const hash = createHash("sha256");
  const reader = source.getReader();
  let size = 0;
  let digest: string | undefined;
  const body = new ReadableStream<Uint8Array>({
    async pull(controller) {
      const { done, value } = await reader.read();
      if (done) {
        controller.close();
        return;
      }
      hash.update(value);
      size += value.byteLength;
      controller.enqueue(value);
    },
    cancel(reason) {
      return reader.cancel(reason);
    },
  });
  return {
    body,
    digest: () => {
      digest ??= hash.digest("hex");
      return digest;
    },
    size: () => size,
  };
}

// ----------------------------------------------------------- transfer client

export interface UploadUrlOptions {
  readonly user?: string | undefined;
  readonly expiresIn?: number | undefined;
  readonly maxBytes?: number | undefined;
  readonly form?: boolean | undefined;
  readonly requestTimeoutMs?: number | undefined;
}

export interface DownloadUrlOptions {
  readonly user?: string | undefined;
  readonly expiresIn?: number | undefined;
  readonly filename?: string | undefined;
  readonly requestTimeoutMs?: number | undefined;
}

export interface WaitOptions {
  /** Plazo de la espera en ms; al vencer, `TimeoutError` y la transferencia sigue en el sandbox. */
  readonly timeoutMs?: number | undefined;
}

interface WatchOptions extends WaitOptions {
  readonly idleMs?: number | undefined;
}

/** Lo que un `UploadTicket` necesita del sandbox que lo armó. */
export interface TicketOperations {
  waitForEntry(transferId: string, options: WaitOptions): Promise<EntryInfo>;
  status(transferId: string): Promise<TransferStatus>;
  cancel(transferId: string): Promise<void>;
}

/** Una exportación terminada: dónde quedó y qué bytes se subieron. */
export interface ExportedObject {
  readonly target: StagingObject;
  readonly transferId: string;
  readonly sha256: string;
  readonly size: number;
}

export interface ExportInit {
  readonly feature: string;
  readonly path: string;
  readonly user: string | undefined;
  readonly size: number;
  readonly deadlineMs: number;
  /** `streamIdleTimeoutMs` de una lectura enrutada: también vigila el `WatchTransfer` de la exportación. */
  readonly idleMs?: number | undefined;
}

export interface DownloadInit extends ExportInit {
  readonly entry: EntryInfo;
  readonly lifetime: number;
  readonly filename: string;
}

export interface RoutedUpload {
  readonly path: string;
  readonly mode: number | undefined;
  readonly body: Uint8Array | ReadableStream<Uint8Array>;
}

export interface RoutedUploadOptions {
  readonly user: string | undefined;
  readonly metadata: Readonly<Record<string, string>>;
  readonly deadline: OperationDeadline;
}

type WatchRound =
  | { readonly state: TransferState; readonly cut?: undefined }
  | { readonly state?: undefined; readonly cut: ConnectError };

function watchEndedWithoutTerminal(): ConnectError {
  return new ConnectError("WatchTransfer terminó sin estado final", Code.Unavailable);
}

function terminalState(event: TransferEvent | undefined): TransferState | undefined {
  if (event?.event.case !== "state") {
    return undefined;
  }
  return isTerminal(event.event.value) ? event.event.value : undefined;
}

/**
 * Los cinco RPCs de transferencia de `FilesystemService` más las llamadas a
 * S3 con las credenciales del llamante. La sonda de capacidad
 * (`GetTransfer("")`) se cachea durante la vida del sandbox. `WatchTransfer`
 * va por el transporte de streams y se re-emite tras un suspend, un reset o
 * un EOF sin estado final, igual que un `WatchHandle`.
 */
export class TransferClient implements TicketOperations {
  readonly #core: SandboxCore;
  readonly #toEntry: (entry: EntryInfoProto) => EntryInfo;
  #supported: boolean | undefined;
  #s3: S3Access | undefined;

  constructor(core: SandboxCore, toEntry: (entry: EntryInfoProto) => EntryInfo) {
    this.#core = core;
    this.#toEntry = toEntry;
  }

  get staging(): ResolvedS3Staging | undefined {
    return this.#core.transfer;
  }

  requireStaging(feature: string): ResolvedS3Staging {
    const staging = this.#core.transfer;
    if (staging === undefined) {
      throw missingStagingError(feature);
    }
    return staging;
  }

  async supportsTransfers(): Promise<boolean> {
    this.#supported ??= await this.#probe();
    return this.#supported;
  }

  async requireSupport(feature: string): Promise<void> {
    if (!(await this.supportsTransfers())) {
      throw outdatedImageError(feature);
    }
  }

  /** Arma la importación en `rayd` antes de que la URL exista para el caller. */
  async uploadUrl(path: string, options: UploadUrlOptions): Promise<UploadTicket> {
    const staging = this.requireStaging("uploadUrl");
    const maxBytes = validateMaxBytes(options.maxBytes);
    const lifetime = effectiveExpiresIn(
      options.expiresIn ?? TRANSFER_DEFAULT_EXPIRES_IN_SECONDS,
      staging,
    );
    const form = options.form ?? false;
    await this.requireSupport("uploadUrl");
    const target = this.#newTarget(staging, "up");
    const s3 = this.#s3Access(staging);
    const signedAt = Date.now();
    const signed = form
      ? await s3.presignPost(
          target.bucket,
          target.key,
          maxBytes ?? TRANSFER_SINGLE_PUT_MAX_BYTES,
          lifetime,
        )
      : { url: await s3.presignPut(target.bucket, target.key, lifetime), fields: {} };
    const expiresAt = new Date(signedAt + lifetime * 1000);
    const transferId = await this.#startImport(
      startImportRequest({
        path,
        user: options.user,
        mode: undefined,
        target,
        getUrl: await s3.presignGet(target.bucket, target.key, lifetime),
        deleteUrl: await s3.presignDelete(target.bucket, target.key, deleteLifetime(lifetime)),
        waitForObject: true,
        expiresAt,
        maxBytes,
      }),
      "uploadUrl",
      options.requestTimeoutMs,
    );
    this.#debug("ticket de subida armado", { transferId, direction: "import" });
    return new UploadTicket({
      url: signed.url,
      method: form ? "POST" : "PUT",
      headers: uploadTicketHeaders(form),
      fields: signed.fields,
      path,
      expiresAt,
      transferId,
      operations: this,
    });
  }

  /** Exporta una foto de `entry` y firma el GET del usuario con `Content-Disposition`. */
  async downloadUrl(init: DownloadInit): Promise<DownloadLink> {
    const staging = this.requireStaging(init.feature);
    const exported = await this.exportToStaging(init);
    const signedAt = Date.now();
    const url = await this.#s3Access(staging).presignGet(
      exported.target.bucket,
      exported.target.key,
      init.lifetime,
      contentDisposition(init.filename),
    );
    return new DownloadLink({
      url,
      path: init.path,
      expiresAt: new Date(signedAt + init.lifetime * 1000),
      size: exported.size,
      sha256: exported.sha256,
      transferId: exported.transferId,
    });
  }

  /**
   * `StartExport` con un PUT o con las URLs de una subida multiparte que el
   * SDK crea, completa o aborta con sus credenciales; `rayd` nunca tiene una
   * credencial de multiparte.
   */
  async exportToStaging(init: ExportInit): Promise<ExportedObject> {
    const staging = this.requireStaging(init.feature);
    const target = this.#newTarget(staging, "down");
    const s3 = this.#s3Access(staging);
    const plan = partPlan(init.size, staging);
    const lifetime = exportBudgetSeconds(init.size);
    const expiresAt = new Date(Date.now() + lifetime * 1000);
    if (plan === undefined) {
      const putUrl = await s3.presignPut(target.bucket, target.key, lifetime);
      const request = startExportRequest({ ...init, target, expiresAt, putUrl });
      return (await this.#runExport(request, target, init)).exported;
    }
    const uploadId = await s3.createMultipartUpload(target.bucket, target.key);
    try {
      const partUrls = await Promise.all(
        Array.from({ length: plan.parts }, (_, index) =>
          s3.presignUploadPart(target.bucket, target.key, uploadId, index + 1, lifetime),
        ),
      );
      const request = startExportRequest({
        ...init,
        target,
        expiresAt,
        partUrls,
        partSize: plan.partSize,
      });
      const { exported, etags } = await this.#runExport(request, target, init);
      await s3.completeMultipartUpload(target.bucket, target.key, uploadId, etags);
      return exported;
    } catch (error) {
      await this.#abortQuietly(s3, target, uploadId);
      throw error;
    }
  }

  /**
   * El objeto exportado como stream, bajado con las credenciales del
   * llamante: al terminar comprueba el sha256 de la exportación
   * (`TransferError` `checksum_mismatch`) y borra el objeto de staging.
   */
  async streamExported(
    exported: ExportedObject,
    idleMs: number | undefined,
    deadline?: OperationDeadline,
  ): Promise<ReadableStream<Uint8Array>> {
    const s3 = this.#s3Access(this.requireStaging("read"));
    const abort = new AbortController();
    const finish = () => this.#deleteQuietly(s3, exported.target, exported.transferId);
    const bounded = <T>(work: Promise<T>): Promise<T> =>
      deadline === undefined
        ? work
        : withinBudget(work, deadline.remainingMs(exported.size), () => abort.abort());
    const body = await bounded(
      s3.getObject(exported.target.bucket, exported.target.key, abort.signal),
    ).catch(async (error: unknown) => {
      abort.abort();
      await finish();
      throw error;
    });
    const reader = body.getReader();
    const hash = createHash("sha256");
    return new ReadableStream<Uint8Array>({
      async pull(controller) {
        const result = await nextWithinIdle(
          Promise.resolve().then(() => bounded(reader.read())),
          idleMs,
          () => abort.abort(),
        ).catch(async (error: unknown) => {
          abort.abort();
          await finish();
          throw translateS3Error(error);
        });
        if (!result.done) {
          hash.update(result.value);
          controller.enqueue(result.value);
          return;
        }
        await finish();
        if (hash.digest("hex") !== exported.sha256) {
          throw checksumMismatchError();
        }
        controller.close();
      },
      async cancel(reason) {
        abort.abort();
        await reader.cancel(reason).catch(() => undefined);
        await finish();
      },
    });
  }

  /**
   * Escritura enrutada: el SDK sube con sus credenciales calculando el sha256
   * al vuelo y `rayd` importa una vez (`waitForObject: false`) comprobándolo.
   */
  async importUpload(upload: RoutedUpload, options: RoutedUploadOptions): Promise<EntryInfo> {
    const staging = this.requireStaging("write");
    const target = this.#newTarget(staging, "up");
    const s3 = this.#s3Access(staging);
    const hashing = hashingBody(upload.body);
    const abort = new AbortController();
    const known = upload.body instanceof Uint8Array ? upload.body.byteLength : undefined;
    try {
      await withinBudget(
        s3.upload(target.bucket, target.key, hashing.body, abort),
        options.deadline.budgetMs(known),
        () => abort.abort(),
      );
    } catch (error) {
      if (error instanceof TimeoutError) {
        await this.#deleteQuietly(s3, target);
      }
      throw error;
    }
    const size = hashing.size();
    const getLifetime = internalGetLifetime(staging);
    const signedAt = Date.now();
    let transferId: string;
    try {
      transferId = await this.#startImport(
        startImportRequest({
          path: upload.path,
          user: options.user,
          mode: upload.mode,
          target,
          getUrl: await s3.presignGet(target.bucket, target.key, getLifetime),
          deleteUrl: await s3.presignDelete(target.bucket, target.key, exportBudgetSeconds(size)),
          waitForObject: false,
          expiresAt: new Date(signedAt + getLifetime * 1000),
          maxBytes: size,
          expectedSha256: hashing.digest(),
          metadata: options.metadata,
        }),
        "write",
        options.deadline.remainingMs(size),
      );
    } catch (error) {
      await this.#deleteQuietly(s3, target);
      throw error;
    }
    const entry = await this.waitForEntry(transferId, {
      timeoutMs: options.deadline.remainingMs(size),
    });
    this.#debug("escritura enrutada por S3 terminada", {
      transferId,
      direction: "import",
      bytes: size,
    });
    return entry;
  }

  async waitForEntry(transferId: string, options: WaitOptions): Promise<EntryInfo> {
    const state = await this.waitTransfer(transferId, options);
    if (state.phase !== TransferPhase.DONE) {
      this.#debug("importación fallida", { transferId, direction: "import", outcome: "failed" });
      throw failureFromState(state, "import");
    }
    if (state.entry === undefined) {
      throw new SandboxError("la importación terminó sin entry");
    }
    this.#debug("importación terminada", {
      transferId,
      direction: "import",
      bytes: Number(state.bytesDone),
      durationMs: state.durationMs,
    });
    return this.#toEntry(state.entry);
  }

  async status(transferId: string): Promise<TransferStatus> {
    const request = create(GetTransferRequestSchema, { transferId });
    const timeoutMs = this.#core.resolveRequestTimeout(undefined);
    try {
      const state = await this.#core.callUnary(() =>
        this.#core.clients.filesystem.getTransfer(request, { timeoutMs }),
      );
      return transferStatusFromProto(state);
    } catch (error) {
      throw transferRpcError(error, "UploadTicket.status", false);
    }
  }

  /** Idempotente: cancelar una transferencia terminada no cambia nada. */
  async cancel(transferId: string): Promise<void> {
    const request = create(CancelTransferRequestSchema, { transferId });
    const timeoutMs = this.#core.resolveRequestTimeout(undefined);
    try {
      await this.#core.callUnary(() =>
        this.#core.clients.filesystem.cancelTransfer(request, { timeoutMs }),
      );
    } catch (error) {
      throw transferRpcError(error, "UploadTicket.cancel", false);
    }
  }

  /** Sigue la transferencia hasta su estado final; `timeoutMs` vencido es `TimeoutError` y la deja en curso. */
  waitTransfer(transferId: string, options: WatchOptions = {}): Promise<TransferState> {
    const stop = new AbortController();
    const watching = this.#watchUntilTerminal(transferId, stop.signal, options.idleMs);
    if (options.timeoutMs === undefined) {
      return watching;
    }
    return raceDeadline(watching, options.timeoutMs, () => {
      stop.abort();
      return transferDeadlineError(transferId);
    });
  }

  // ------------------------------------------------------------- internals

  async #probe(): Promise<boolean> {
    const request = create(GetTransferRequestSchema, { transferId: CAPABILITY_PROBE_ID });
    const timeoutMs = this.#core.resolveRequestTimeout(undefined);
    try {
      await this.#core.callUnary(() =>
        this.#core.clients.filesystem.getTransfer(request, { timeoutMs }),
      );
    } catch (error) {
      return probeSupportsTransfers(error);
    }
    return true;
  }

  #s3Access(staging: ResolvedS3Staging): S3Access {
    this.#s3 ??= new S3Access(staging.region ?? this.#core.controlPlane.region, {
      ...s3ClientOverridesFor(this.#core.controlPlane),
      ...(this.#core.s3ClientOverrides ?? {}),
    });
    return this.#s3;
  }

  #newTarget(staging: ResolvedS3Staging, direction: KeyDirection): StagingObject {
    return {
      bucket: staging.bucket,
      key: stagingKey(staging.prefix, this.#core.sandboxId, direction),
      region: staging.region ?? this.#core.controlPlane.region,
    };
  }

  async #startImport(
    request: StartImportRequest,
    feature: string,
    requestTimeoutMs: number | undefined,
  ): Promise<string> {
    const timeoutMs = this.#core.resolveRequestTimeout(requestTimeoutMs);
    try {
      const response = await this.#core.callUnary(() =>
        this.#core.clients.filesystem.startImport(request, { timeoutMs }),
      );
      return response.transferId;
    } catch (error) {
      throw transferRpcError(error, feature, true);
    }
  }

  async #runExport(
    request: StartExportRequest,
    target: StagingObject,
    init: ExportInit,
  ): Promise<{ readonly exported: ExportedObject; readonly etags: readonly string[] }> {
    const timeoutMs = this.#core.resolveRequestTimeout(undefined);
    let transferId: string;
    try {
      const response = await this.#core.callUnary(() =>
        this.#core.clients.filesystem.startExport(request, { timeoutMs }),
      );
      transferId = response.transferId;
    } catch (error) {
      throw transferRpcError(error, init.feature, true);
    }
    const state = await this.waitTransfer(transferId, {
      timeoutMs: init.deadlineMs,
      idleMs: init.idleMs,
    });
    if (state.phase !== TransferPhase.DONE) {
      this.#debug("exportación fallida", { transferId, direction: "export", outcome: "failed" });
      throw failureFromState(state, "export");
    }
    this.#debug("exportación terminada", {
      transferId,
      direction: "export",
      bytes: Number(state.bytesDone),
      durationMs: state.durationMs,
    });
    return {
      exported: { target, transferId, sha256: state.sha256, size: Number(state.bytesDone) },
      etags: state.partEtags,
    };
  }

  async #watchUntilTerminal(
    transferId: string,
    signal: AbortSignal,
    idleMs: number | undefined,
  ): Promise<TransferState> {
    let generation = this.#core.resumeGeneration;
    const budget = new ReconnectBudget();
    while (true) {
      const round = await this.#watchOnce(transferId, signal, idleMs);
      if (round.state !== undefined) {
        return round.state;
      }
      const outcome = await this.#core.reconnect(round.cut, generation, { wake: false });
      if (!outcome.resumed) {
        throw this.#core.reconnectError(outcome, round.cut);
      }
      generation = outcome.resumeGeneration;
      if (!budget.allows(outcome)) {
        throw await this.#core.streamFailure(round.cut);
      }
    }
  }

  async #watchOnce(
    transferId: string,
    signal: AbortSignal,
    idleMs: number | undefined,
  ): Promise<WatchRound> {
    if (signal.aborted) {
      throw transferDeadlineError(transferId);
    }
    const request = create(WatchTransferRequestSchema, { transferId });
    const opened = await this.#core.openStream(
      (client, callOptions) => client.watchTransfer(request, callOptions),
      {
        service: FilesystemService,
        stream: true,
        translate: (error) => transferRpcError(error, "WatchTransfer", false),
      },
    );
    this.#core.trackStream(opened.controller);
    const stopWatching = () => opened.controller.abort();
    signal.addEventListener("abort", stopWatching, { once: true });
    try {
      return await this.#pump(opened, idleMs);
    } catch (error) {
      if (!signal.aborted && this.#core.isReconnectable(error)) {
        return { cut: error as ConnectError };
      }
      if (error instanceof ConnectError && isStreamReset(error)) {
        throw await this.#core.streamFailure(error);
      }
      throw transferRpcError(error, "WatchTransfer", false);
    } finally {
      signal.removeEventListener("abort", stopWatching);
      this.#core.releaseStream(opened.controller);
    }
  }

  async #pump(
    opened: OpenedStream<TransferEvent>,
    idleMs: number | undefined,
  ): Promise<WatchRound> {
    let event: TransferEvent | undefined = opened.first;
    while (true) {
      const state = terminalState(event);
      if (state !== undefined) {
        return { state };
      }
      const result = await nextWithinIdle(opened.iterator.next(), idleMs, () =>
        opened.controller.abort(),
      );
      if (result.done) {
        return { cut: watchEndedWithoutTerminal() };
      }
      event = result.value;
    }
  }

  async #abortQuietly(s3: S3Access, target: StagingObject, uploadId: string): Promise<void> {
    try {
      await s3.abortMultipartUpload(target.bucket, target.key, uploadId);
    } catch (error) {
      this.#debug("no se pudo abortar la subida multiparte", {
        direction: "export",
        outcome: (error as Error).name,
      });
    }
  }

  /** `transferId` falta cuando el objeto aún no tiene transferencia en el agente (subida cortada). */
  async #deleteQuietly(s3: S3Access, target: StagingObject, transferId?: string): Promise<void> {
    try {
      await s3.deleteObject(target.bucket, target.key);
    } catch (error) {
      this.#debug("no se pudo borrar el objeto de staging", {
        ...(transferId === undefined ? {} : { transferId }),
        outcome: (error as Error).name,
      });
    }
  }

  #debug(message: string, fields: Readonly<Record<string, unknown>>): void {
    this.#core.logger?.debug?.(message, fields);
  }
}

// ------------------------------------------------------------ ticket and link

export interface UploadTicketInit {
  readonly url: string;
  readonly method: UploadMethod;
  readonly headers: Readonly<Record<string, string>>;
  readonly fields: Readonly<Record<string, string>>;
  readonly path: string;
  readonly expiresAt: Date;
  readonly transferId: string;
  readonly operations: TicketOperations;
}

/**
 * Resultado de `files.uploadUrl`: la importación ya está armada en `rayd` y
 * es de un solo uso. `toString()` es la URL (`fetch(String(ticket), …)`);
 * `method` es `PUT` (cuerpo crudo, con `headers`) o `POST` (formulario con
 * `fields` y el fichero en el campo `file`). `wait()` sigue la importación
 * hasta su final y devuelve el `EntryInfo` escrito; con `timeoutMs` vencido
 * lanza `TimeoutError` y el ticket sigue armado. Nunca se serializa
 * (`toJSON` lanza) ni imprime la URL.
 */
export class UploadTicket {
  readonly #url: string;
  readonly method: UploadMethod;
  readonly #headers: Readonly<Record<string, string>>;
  readonly #fields: Readonly<Record<string, string>>;
  readonly path: string;
  readonly expiresAt: Date;
  readonly transferId: string;
  readonly #operations: TicketOperations;

  constructor(init: UploadTicketInit) {
    this.#url = init.url;
    this.method = init.method;
    this.#headers = { ...init.headers };
    this.#fields = { ...init.fields };
    this.path = init.path;
    this.expiresAt = init.expiresAt;
    this.transferId = init.transferId;
    this.#operations = init.operations;
  }

  get url(): string {
    return this.#url;
  }

  get headers(): Record<string, string> {
    return { ...this.#headers };
  }

  get fields(): Record<string, string> {
    return { ...this.#fields };
  }

  wait(options: WaitOptions = {}): Promise<EntryInfo> {
    return this.#operations.waitForEntry(this.transferId, options);
  }

  status(): Promise<TransferStatus> {
    return this.#operations.status(this.transferId);
  }

  cancel(): Promise<void> {
    return this.#operations.cancel(this.transferId);
  }

  toString(): string {
    return this.#url;
  }

  toJSON(): never {
    throw new TypeError(
      "UploadTicket no se serializa: lleva una URL prefirmada ligada a un sandbox vivo",
    );
  }

  [inspect.custom](): string {
    return (
      `UploadTicket(path=${JSON.stringify(this.path)}, method=${this.method}, ` +
      `expiresAt=${this.expiresAt.toISOString()})`
    );
  }
}

export interface DownloadLinkInit {
  readonly url: string;
  readonly path: string;
  readonly expiresAt: Date;
  readonly size: number;
  readonly sha256: string;
  readonly transferId: string;
}

/**
 * Resultado de `files.downloadUrl`: una foto del fichero tomada al llamar,
 * servida por S3 hasta `expiresAt` a cualquier cliente HTTP sin cabeceras
 * (`Range` incluido). `toString()` es la URL; nunca se serializa ni imprime.
 */
export class DownloadLink {
  readonly #url: string;
  readonly path: string;
  readonly expiresAt: Date;
  readonly size: number;
  readonly sha256: string;
  readonly transferId: string;

  constructor(init: DownloadLinkInit) {
    this.#url = init.url;
    this.path = init.path;
    this.expiresAt = init.expiresAt;
    this.size = init.size;
    this.sha256 = init.sha256;
    this.transferId = init.transferId;
  }

  get url(): string {
    return this.#url;
  }

  toString(): string {
    return this.#url;
  }

  toJSON(): never {
    throw new TypeError(
      "DownloadLink no se serializa: lleva una URL prefirmada, una credencial al portador",
    );
  }

  [inspect.custom](): string {
    return (
      `DownloadLink(path=${JSON.stringify(this.path)}, size=${this.size}, ` +
      `expiresAt=${this.expiresAt.toISOString()})`
    );
  }
}
