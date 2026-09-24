/**
 * `sandbox.files`: validación de argumentos, aritmética de deadlines,
 * construcción de requests, troceado del stream de `Write`, conversión de
 * protos, `Filesystem` y `WatchHandle` (con su re-emisión tras un suspend).
 * Con `transfer` configurado, las escrituras y lecturas grandes y las URLs
 * prefirmadas van por S3 a través de `TransferClient` (ADR-010).
 */

import { create } from "@bufbuild/protobuf";
import { type CallOptions, Code, ConnectError } from "@connectrpc/connect";
import {
  errorMessage,
  FileNotFoundError,
  InvalidArgumentError,
  SandboxError,
  TimeoutError,
} from "../errors.js";
import {
  type EntryInfo as EntryInfoProto,
  FileType as FileTypeProto,
  UserSchema,
} from "../gen/rayito/v1/common_pb.js";
import {
  type FilesystemEvent as FilesystemEventProto,
  FilesystemEventType as FilesystemEventTypeProto,
  FilesystemService,
  ListDirRequestSchema,
  MakeDirRequestSchema,
  MoveRequestSchema,
  ReadRequestSchema,
  type ReadResponse,
  RemoveRequestSchema,
  StatRequestSchema,
  type WatchDirRequest,
  WatchDirRequestSchema,
  type WatchDirResponse,
  type WriteRequest,
  WriteRequestSchema,
} from "../gen/rayito/v1/filesystem_pb.js";
import {
  COMPRESSION_OPT_IN_HEADER,
  METADATA_MAX_BYTES,
  METADATA_MAX_KEYS,
  METADATA_XATTR_PREFIX,
  TRANSFER_DEFAULT_EXPIRES_IN_SECONDS,
} from "../limits.js";
import {
  type EntryInfo,
  type FilesystemEvent,
  FilesystemEventType,
  FileType,
  type WriteData,
  type WriteEntry,
} from "../models.js";
import { isStreamReset, translateRpcError } from "../transport/errors.js";
import { deadlineAt, type RequestOptions, remainingDeadlineMs } from "./commands.js";
import { type Abortable, type OpenedStream, type SandboxCore, withTimeout } from "./core.js";
import { ReconnectBudget } from "./readiness.js";
import {
  type DownloadLink,
  type DownloadUrlOptions,
  downloadFilename,
  effectiveExpiresIn,
  nextWithinIdle,
  OCTET_STREAM,
  OperationDeadline,
  shouldRoute,
  TransferClient,
  type UploadTicket,
  type UploadUrlOptions,
} from "./transfer.js";

export const FILE_REQUEST_BASE_MS = 60_000;
export const FILE_REQUEST_MS_PER_MB = 1000;
export const MB = 1_000_000;
export const WRITE_CHUNK_BYTES = 1_048_576;
export const READ_CHUNK_BYTES = 262_144;
export const WATCH_STOP_JOIN_MS = 5000;
export const MODE_MAX = 0o7777;
export const DEFAULT_DEPTH = 1;
export const READ_FORMATS = ["text", "bytes", "stream", "blob"] as const;
export const METADATA_KEY_MAX_CHARS = 255;
export const GZIP_ENCODING = "gzip";
const METADATA_KEY_PATTERN = /^[A-Za-z0-9!#$%&'*+\-.^_`|~]+$/;
const METADATA_VALUE_PATTERN = /^[\x20-\x7e]*$/;
const GZIP_READ_HEADERS: Readonly<Record<string, string>> = {
  [COMPRESSION_OPT_IN_HEADER]: GZIP_ENCODING,
};

export type ReadFormat = (typeof READ_FORMATS)[number];
export type EventCallback = (event: FilesystemEvent) => void;
export type ExitCallback = (error: Error) => void;

export interface PreparedWrite {
  readonly path: string;
  readonly data: Uint8Array;
  readonly mode: number | undefined;
}

export interface UserOptions extends RequestOptions {
  readonly user?: string | undefined;
}

export interface ReadOptions extends UserOptions {
  readonly format?: ReadFormat | undefined;
  /** Pide a `rayd` la respuesta comprimida con gzip (`rayito-compress: gzip`); inocuo en un agente anterior. */
  readonly gzip?: boolean | undefined;
  /** Cancela la lectura con `TimeoutError` si pasan estos ms sin un chunk; `0` o `undefined` = sin guardia. */
  readonly streamIdleTimeoutMs?: number | undefined;
}

export interface WriteFilesOptions extends UserOptions {
  /** Comprime los mensajes del stream `Write` con gzip; se ignora en lo que va por S3. */
  readonly gzip?: boolean | undefined;
  /** Metadatos de cada fichero escrito (sustituyen los que tuviera); claves en minúsculas al guardarse. */
  readonly metadata?: Readonly<Record<string, string>> | undefined;
  /** Se acepta sin efecto: gRPC no tiene formulario multiparte (el `useOctetStream` de E2B). */
  readonly useOctetStream?: boolean | undefined;
}

export interface WriteOptions extends WriteFilesOptions {
  readonly mode?: number | undefined;
}

export interface ListOptions extends UserOptions {
  readonly depth?: number | undefined;
}

export interface RemoveOptions extends UserOptions {
  readonly recursive?: boolean | undefined;
}

export interface WatchOptions extends UserOptions {
  readonly onEvent?: EventCallback | undefined;
  readonly onExit?: ExitCallback | undefined;
  readonly recursive?: boolean | undefined;
  readonly includeEntry?: boolean | undefined;
  /** Deadline del stream en ms; `0` (por defecto) o `undefined` = sin deadline. */
  readonly timeoutMs?: number | undefined;
}

const FILE_TYPES: Readonly<Record<number, FileType>> = {
  [FileTypeProto.FILE]: FileType.FILE,
  [FileTypeProto.DIRECTORY]: FileType.DIR,
  [FileTypeProto.SYMLINK]: FileType.SYMLINK,
};
const EVENT_TYPES: Readonly<Record<number, FilesystemEventType>> = {
  [FilesystemEventTypeProto.CREATE]: FilesystemEventType.CREATE,
  [FilesystemEventTypeProto.WRITE]: FilesystemEventType.WRITE,
  [FilesystemEventTypeProto.REMOVE]: FilesystemEventType.REMOVE,
  [FilesystemEventTypeProto.RENAME]: FilesystemEventType.RENAME,
  [FilesystemEventTypeProto.CHMOD]: FilesystemEventType.CHMOD,
};
const MAX_DATE_MS = 8.64e15;

// ------------------------------------------------------------------ deadlines

/** `60 s + 1 s por MB` salvo que el caller fije `requestTimeoutMs`. */
export function fileRequestDeadlineMs(
  totalBytes: number,
  requestTimeoutMs: number | undefined,
): number {
  if (requestTimeoutMs !== undefined) {
    return requestTimeoutMs;
  }
  return FILE_REQUEST_BASE_MS + FILE_REQUEST_MS_PER_MB * Math.ceil(totalBytes / MB);
}

/** `0` y `undefined` significan sin deadline gRPC; `> 0` es el deadline del stream. */
export function validateWatchTimeout(timeoutMs: number | undefined): number | undefined {
  if (timeoutMs === undefined) {
    return undefined;
  }
  if (typeof timeoutMs !== "number" || Number.isNaN(timeoutMs)) {
    throw new InvalidArgumentError(
      `timeoutMs debe ser un número de milisegundos, recibido ${String(timeoutMs)}`,
    );
  }
  if (timeoutMs < 0) {
    throw new InvalidArgumentError(`timeoutMs no puede ser negativo, recibido ${timeoutMs}`);
  }
  return timeoutMs === 0 ? undefined : timeoutMs;
}

// ----------------------------------------------------------------- validation

export function validatePath(path: unknown, field = "path"): string {
  if (typeof path !== "string" || path.length === 0) {
    throw new InvalidArgumentError(
      `${field} debe ser una cadena no vacía, recibido ${JSON.stringify(path)}`,
    );
  }
  if (path.includes("\0")) {
    throw new InvalidArgumentError(`${field} no puede contener NUL`);
  }
  return path;
}

export function validateMode(mode: unknown): number | undefined {
  if (mode === undefined) {
    return undefined;
  }
  if (typeof mode !== "number" || !Number.isInteger(mode) || mode < 0 || mode > MODE_MAX) {
    throw new InvalidArgumentError(
      `mode debe ser un entero entre 0 y 0o7777, recibido ${String(mode)}`,
    );
  }
  return mode;
}

/** `0` se envía tal cual: el agente lo interpreta como `1`. */
export function validateDepth(depth: unknown): number {
  if (typeof depth !== "number" || !Number.isInteger(depth) || depth < 0) {
    throw new InvalidArgumentError(`depth debe ser un entero >= 0, recibido ${String(depth)}`);
  }
  return depth;
}

export function validateReadFormat(format: unknown): ReadFormat {
  if (!READ_FORMATS.includes(format as ReadFormat)) {
    throw new InvalidArgumentError(
      `format debe ser 'text', 'bytes', 'stream' o 'blob', recibido ${JSON.stringify(format)}`,
    );
  }
  return format as ReadFormat;
}

/** `0` y `undefined` son sin guardia; `> 0` son los ms que `read` espera como mucho entre dos chunks. */
export function validateStreamIdleTimeout(idleMs: unknown): number | undefined {
  if (idleMs === undefined) {
    return undefined;
  }
  if (typeof idleMs !== "number" || Number.isNaN(idleMs)) {
    throw new InvalidArgumentError(
      `streamIdleTimeoutMs debe ser un número de milisegundos, recibido ${String(idleMs)}`,
    );
  }
  if (idleMs < 0) {
    throw new InvalidArgumentError(`streamIdleTimeoutMs no puede ser negativo, recibido ${idleMs}`);
  }
  return idleMs === 0 ? undefined : idleMs;
}

function metadataSize(metadata: Readonly<Record<string, string>>): number {
  return Object.entries(metadata).reduce(
    (total, [key, value]) => total + METADATA_XATTR_PREFIX.length + key.length + value.length,
    0,
  );
}

function validateMetadataKey(key: string): string {
  if (key.length === 0 || key.length > METADATA_KEY_MAX_CHARS) {
    throw new InvalidArgumentError(
      `metadatos inválidos: cada clave es una cadena de 1 a ${METADATA_KEY_MAX_CHARS} caracteres`,
    );
  }
  if (!METADATA_KEY_PATTERN.test(key)) {
    throw new InvalidArgumentError(
      "metadatos inválidos: las claves sólo admiten caracteres token de HTTP " +
        "(letras, dígitos y !#$%&'*+-.^_`|~)",
    );
  }
  return key.toLowerCase();
}

function validateMetadataValue(value: unknown): string {
  if (typeof value !== "string" || !METADATA_VALUE_PATTERN.test(value)) {
    throw new InvalidArgumentError(
      "metadatos inválidos: los valores son cadenas de ASCII imprimible (0x20-0x7E)",
    );
  }
  return value;
}

/**
 * Las reglas de `rayd` para `WriteRequest.metadata`, antes de cualquier RPC:
 * claves de 1-255 caracteres token de HTTP (se envían en minúsculas; dos
 * iguales tras bajarlas se rechazan), valores ASCII imprimible, como mucho 64
 * claves y 4000 bytes contando `user.rayito.`. Los mensajes nunca repiten
 * una clave ni un valor.
 */
export function validateMetadata(metadata: unknown): Readonly<Record<string, string>> {
  if (metadata === undefined) {
    return {};
  }
  if (typeof metadata !== "object" || metadata === null || Array.isArray(metadata)) {
    throw new InvalidArgumentError("metadata debe ser un objeto de string a string");
  }
  const entries = Object.entries(metadata);
  if (entries.length > METADATA_MAX_KEYS) {
    throw new InvalidArgumentError(`metadatos inválidos: como mucho ${METADATA_MAX_KEYS} claves`);
  }
  const normalized: Record<string, string> = {};
  for (const [key, value] of entries) {
    const lowered = validateMetadataKey(key);
    if (Object.hasOwn(normalized, lowered)) {
      throw new InvalidArgumentError(
        "metadatos inválidos: dos claves coinciden al pasarlas a minúsculas",
      );
    }
    normalized[lowered] = validateMetadataValue(value);
  }
  if (metadataSize(normalized) > METADATA_MAX_BYTES) {
    throw new InvalidArgumentError(
      `metadatos inválidos: superan ${METADATA_MAX_BYTES} bytes contando el prefijo ` +
        METADATA_XATTR_PREFIX,
    );
  }
  return Object.freeze(normalized);
}

/**
 * `string` va en UTF-8; `Uint8Array`/`ArrayBuffer` tal cual; `Blob` y
 * `ReadableStream` se leen enteros. Siempre se materializa en memoria para
 * que el tamaño, el deadline y el reintento del 403 estén definidos.
 */
export async function materialiseWriteData(data: WriteData): Promise<Uint8Array> {
  if (typeof data === "string") {
    return new TextEncoder().encode(data);
  }
  if (data instanceof Uint8Array) {
    return data;
  }
  if (data instanceof ArrayBuffer) {
    return new Uint8Array(data);
  }
  if (typeof Blob !== "undefined" && data instanceof Blob) {
    return new Uint8Array(await data.arrayBuffer());
  }
  if (typeof ReadableStream !== "undefined" && data instanceof ReadableStream) {
    return new Uint8Array(await new Response(data).arrayBuffer());
  }
  throw new InvalidArgumentError(
    "data acepta string, Uint8Array, ArrayBuffer, Blob o ReadableStream, recibido " +
      `${data === null ? "null" : typeof data}`,
  );
}

export async function prepareWriteEntries(files: readonly WriteEntry[]): Promise<PreparedWrite[]> {
  if (!Array.isArray(files) || files.length === 0) {
    throw new InvalidArgumentError("writeFiles necesita al menos un fichero");
  }
  const prepared: PreparedWrite[] = [];
  for (const entry of files) {
    if (typeof entry !== "object" || entry === null) {
      throw new InvalidArgumentError(`writeFiles acepta WriteEntry, recibido ${typeof entry}`);
    }
    prepared.push({
      path: validatePath(entry.path),
      data: await materialiseWriteData(entry.data),
      mode: validateMode(entry.mode),
    });
  }
  return prepared;
}

export function totalWriteBytes(entries: readonly PreparedWrite[]): number {
  return entries.reduce((total, entry) => total + entry.data.byteLength, 0);
}

/** Una entrada de `writeFiles` validada y aún sin leer: `data` sólo se materializa si va por gRPC. */
export interface WriteSource {
  readonly path: string;
  readonly data: WriteData;
  readonly mode: number | undefined;
}

function isWriteData(data: unknown): data is WriteData {
  return (
    typeof data === "string" ||
    data instanceof Uint8Array ||
    data instanceof ArrayBuffer ||
    (typeof Blob !== "undefined" && data instanceof Blob) ||
    (typeof ReadableStream !== "undefined" && data instanceof ReadableStream)
  );
}

export function validateWriteSources(files: readonly WriteEntry[]): WriteSource[] {
  if (!Array.isArray(files) || files.length === 0) {
    throw new InvalidArgumentError("writeFiles necesita al menos un fichero");
  }
  return files.map((entry: unknown) => {
    if (typeof entry !== "object" || entry === null) {
      throw new InvalidArgumentError(`writeFiles acepta WriteEntry, recibido ${typeof entry}`);
    }
    const { path, data, mode } = entry as WriteEntry;
    if (!isWriteData(data)) {
      throw new InvalidArgumentError(
        "data acepta string, Uint8Array, ArrayBuffer, Blob o ReadableStream, recibido " +
          `${data === null ? "null" : typeof data}`,
      );
    }
    return { path: validatePath(path), data, mode: validateMode(mode) };
  });
}

/** Los bytes de `data` sin leerlo; `undefined` para un `ReadableStream` (tamaño desconocido). */
export function knownWriteSize(data: WriteData): number | undefined {
  if (typeof data === "string") {
    return Buffer.byteLength(data, "utf8");
  }
  if (data instanceof Uint8Array || data instanceof ArrayBuffer) {
    return data.byteLength;
  }
  if (data instanceof Blob) {
    return data.size;
  }
  return undefined;
}

/** El cuerpo de una escritura por S3: los bytes tal cual o un stream; un `Blob` nunca se materializa. */
function routedBody(data: WriteData): Uint8Array | ReadableStream<Uint8Array> {
  if (typeof data === "string") {
    return new TextEncoder().encode(data);
  }
  if (data instanceof Uint8Array) {
    return data;
  }
  if (data instanceof ArrayBuffer) {
    return new Uint8Array(data);
  }
  if (data instanceof Blob) {
    return data.stream();
  }
  return data;
}

// ------------------------------------------------------------------- requests

function user(username: string | undefined) {
  return username ? create(UserSchema, { username }) : undefined;
}

export function readRequest(path: string, username: string | undefined) {
  return create(ReadRequestSchema, { path: validatePath(path), user: user(username) });
}

export function statRequest(path: string, username: string | undefined) {
  return create(StatRequestSchema, { path: validatePath(path), user: user(username) });
}

export function listDirRequest(path: string, depth: number, username: string | undefined) {
  return create(ListDirRequestSchema, {
    path: validatePath(path),
    depth: validateDepth(depth),
    user: user(username),
  });
}

export function makeDirRequest(path: string, username: string | undefined) {
  return create(MakeDirRequestSchema, { path: validatePath(path), user: user(username) });
}

export function moveRequest(source: string, destination: string, username: string | undefined) {
  return create(MoveRequestSchema, {
    source: validatePath(source, "oldPath"),
    destination: validatePath(destination, "newPath"),
    user: user(username),
  });
}

export function removeRequest(path: string, recursive: boolean, username: string | undefined) {
  return create(RemoveRequestSchema, {
    path: validatePath(path),
    recursive: Boolean(recursive),
    user: user(username),
  });
}

export function watchDirRequest(
  path: string,
  recursive: boolean,
  includeEntry: boolean,
  username: string | undefined,
): WatchDirRequest {
  return create(WatchDirRequestSchema, {
    path: validatePath(path),
    recursive: Boolean(recursive),
    includeEntry: Boolean(includeEntry),
    user: user(username),
  });
}

/**
 * Un `WriteRequest` con `path` (y `user`/`mode`/`metadata` si los hay) abre
 * cada fichero con su primer chunk, posiblemente vacío; el resto viaja en
 * chunks de 1 MiB sin `path`. Es un iterable nuevo por llamada, que es lo que
 * el reintento del 403 necesita.
 */
export async function* buildWriteRequests(
  entries: readonly PreparedWrite[],
  username: string | undefined,
  metadata: Readonly<Record<string, string>> = {},
): AsyncGenerator<WriteRequest, void, undefined> {
  const hasMetadata = Object.keys(metadata).length > 0;
  for (const entry of entries) {
    const first = create(WriteRequestSchema, {
      path: entry.path,
      chunk: entry.data.subarray(0, WRITE_CHUNK_BYTES),
    });
    if (entry.mode !== undefined) {
      first.mode = entry.mode;
    }
    const owner = user(username);
    if (owner !== undefined) {
      first.user = owner;
    }
    if (hasMetadata) {
      first.metadata = { ...metadata };
    }
    yield first;
    for (
      let offset = WRITE_CHUNK_BYTES;
      offset < entry.data.byteLength;
      offset += WRITE_CHUNK_BYTES
    ) {
      yield create(WriteRequestSchema, {
        chunk: entry.data.subarray(offset, offset + WRITE_CHUNK_BYTES),
      });
    }
  }
}

// ---------------------------------------------------------------- conversions

export function fileTypeFromProto(fileType: number): FileType | undefined {
  return FILE_TYPES[fileType];
}

/** Un mtime corrupto en el sandbox no debe tumbar `list`/`getInfo`: fuera del rango de `Date` se recorta. */
export function modifiedTimeFromMs(unixMs: number): Date {
  if (!Number.isFinite(unixMs) || Math.abs(unixMs) > MAX_DATE_MS) {
    return new Date(unixMs < 0 ? -MAX_DATE_MS : MAX_DATE_MS);
  }
  return new Date(unixMs);
}

export function entryInfoFromProto(entry: EntryInfoProto): EntryInfo {
  return Object.freeze({
    name: entry.name,
    type: fileTypeFromProto(entry.type),
    path: entry.path,
    size: Number(entry.size),
    mode: entry.mode,
    permissions: entry.permissions,
    owner: entry.owner,
    group: entry.group,
    modifiedTime: modifiedTimeFromMs(Number(entry.modifiedTimeUnixMs)),
    symlinkTarget: entry.symlinkTarget,
    metadata: Object.freeze({ ...entry.metadata }),
  });
}

export function filesystemEventFromProto(event: FilesystemEventProto): FilesystemEvent {
  const kind = EVENT_TYPES[event.type];
  if (kind === undefined) {
    throw new SandboxError(`tipo de evento de watch desconocido: ${event.type}`);
  }
  return Object.freeze({
    name: event.name,
    type: kind,
    entry: event.entry === undefined ? undefined : entryInfoFromProto(event.entry),
  });
}

export function decodeText(data: Uint8Array): string {
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(data);
  } catch (error) {
    throw new InvalidArgumentError("el fichero no es UTF-8 válido; usa format: 'bytes'", {
      cause: error,
    });
  }
}

/** `read` sólo abre ficheros regulares: el agente abre con `O_NOFOLLOW`. */
export function requireRegularFile(entry: EntryInfo): EntryInfo {
  if (entry.type === FileType.FILE) {
    return entry;
  }
  if (entry.type === FileType.DIR) {
    throw new InvalidArgumentError(`${entry.path} es un directorio`);
  }
  if (entry.type === FileType.SYMLINK) {
    throw new InvalidArgumentError(
      `${entry.path} es un enlace simbólico; el agente no sigue symlinks al leer`,
    );
  }
  throw new InvalidArgumentError(`${entry.path} no es un fichero regular`);
}

/** El primer mensaje de `WatchDir` es siempre `WatchStarted`. */
export function requireWatchStarted(response: WatchDirResponse | undefined): void {
  const kind = response?.event.case;
  if (kind !== "started") {
    throw new SandboxError(
      `el stream de WatchDir no empezó con WatchStarted (llegó ${JSON.stringify(kind ?? null)})`,
    );
  }
}

export function isAlreadyExists(error: unknown): boolean {
  return error instanceof ConnectError && error.code === Code.AlreadyExists;
}

export function concatChunks(chunks: readonly Uint8Array[]): Uint8Array {
  const total = chunks.reduce((sum, chunk) => sum + chunk.byteLength, 0);
  const joined = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    joined.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return joined;
}

// ------------------------------------------------------------------- watching

/**
 * Estado de un `WatchHandle`. Los eventos sólo se encolan cuando no hay
 * `onEvent`; con callback se entregan al vuelo y un error dentro del callback
 * se loguea sin parar el watch. `drain` devuelve lo pendiente y, cuando no
 * queda nada, lanza una sola vez el error terminal.
 */
export class WatchState {
  readonly #onEvent: EventCallback | undefined;
  readonly #warn: ((message: string, fields: Record<string, unknown>) => void) | undefined;
  #pending: FilesystemEvent[] = [];
  #failure: Error | undefined;
  #failurePending = false;
  stopped = false;
  ended = false;

  constructor(
    onEvent: EventCallback | undefined,
    warn?: (message: string, fields: Record<string, unknown>) => void,
  ) {
    this.#onEvent = onEvent;
    this.#warn = warn;
  }

  get isRunning(): boolean {
    return !(this.stopped || this.ended);
  }

  feed(response: WatchDirResponse): FilesystemEvent | undefined {
    const kind = response.event.case;
    if (kind === "started") {
      throw new SandboxError("WatchStarted repetido en mitad del stream");
    }
    if (kind !== "filesystem") {
      return undefined;
    }
    const event = filesystemEventFromProto(response.event.value);
    this.#dispatch(event);
    return event;
  }

  recordEnd(failure: Error | undefined): void {
    this.ended = true;
    this.#failure = failure;
    this.#failurePending = failure !== undefined;
  }

  drain(): FilesystemEvent[] {
    const events = this.#pending;
    this.#pending = [];
    if (events.length === 0) {
      const failure = this.#takeFailure();
      if (failure !== undefined) {
        throw failure;
      }
    }
    return events;
  }

  #takeFailure(): Error | undefined {
    if (!this.#failurePending) {
      return undefined;
    }
    this.#failurePending = false;
    return this.#failure;
  }

  #dispatch(event: FilesystemEvent): void {
    if (this.#onEvent === undefined) {
      this.#pending.push(event);
      return;
    }
    try {
      this.#onEvent(event);
    } catch (error) {
      this.#warn?.("onEvent lanzó una excepción; el watch sigue", { reason: errorMessage(error) });
    }
  }
}

/** `Canceled` tras `stop()` es el final limpio; lo demás sigue la tabla unaria con `filesystem: true`. */
export function watchFailure(error: unknown, stopped: boolean): Error | undefined {
  if (stopped && error instanceof ConnectError && error.code === Code.Canceled) {
    return undefined;
  }
  return translateRpcError(error, { filesystem: true });
}

// ----------------------------------------------------------------- filesystem

export type FilesystemClient = SandboxCore["clients"]["filesystem"];

/** Ficheros del sandbox (`FilesystemService`). */
export class Filesystem {
  readonly core: SandboxCore;
  readonly #transfers: TransferClient;

  constructor(core: SandboxCore) {
    this.core = core;
    this.#transfers = new TransferClient(core, entryInfoFromProto);
  }

  read(path: string, options?: ReadOptionsFor<{ format?: "text" | undefined }>): Promise<string>;
  read(path: string, options: ReadOptionsFor<{ format: "bytes" }>): Promise<Uint8Array>;
  read(
    path: string,
    options: ReadOptionsFor<{ format: "stream" }>,
  ): Promise<ReadableStream<Uint8Array>>;
  read(path: string, options: ReadOptionsFor<{ format: "blob" }>): Promise<Blob>;
  read(path: string, options?: ReadOptions): Promise<ReadResult>;
  /**
   * Con `transfer` configurado y un fichero `>= thresholdBytes`, `rayd` lo
   * exporta a S3 y el SDK lo baja con tus credenciales comprobando el
   * sha256; si no, un `Read` por gRPC como siempre.
   */
  async read(path: string, options: ReadOptions = {}): Promise<ReadResult> {
    const format = validateReadFormat(options.format ?? "text");
    const idleMs = validateStreamIdleTimeout(options.streamIdleTimeoutMs);
    const request = readRequest(path, options.user);
    const entry = requireRegularFile(await this.getInfo(path, options));
    const deadlineMs = fileRequestDeadlineMs(entry.size, options.requestTimeoutMs);
    const deadline =
      format === "stream"
        ? undefined
        : new OperationDeadline(
            () => deadlineMs,
            this.core.now,
            options.requestTimeoutMs !== undefined,
          );
    const stream = (await this.#routes(entry.size))
      ? await this.#readThroughS3(path, options.user, entry.size, deadlineMs, idleMs, deadline)
      : await this.readStream(request, deadlineMs, { gzip: options.gzip ?? false, idleMs });
    return formatRead(stream, format);
  }

  async write(path: string, data: WriteData, options: WriteOptions = {}): Promise<EntryInfo> {
    const entries = await this.writeFiles([{ path, data, mode: options.mode }], options);
    return entries[0] as EntryInfo;
  }

  /**
   * Un solo stream `Write` para lo que va por gRPC, cada fichero atómico por
   * separado. Con `transfer` configurado, lo que mide `>= thresholdBytes` y
   * todo `ReadableStream` van por S3 uno a uno. Los resultados siguen el
   * orden pedido. `metadata` y `gzip` exigen un agente M9.
   */
  async writeFiles(
    files: readonly WriteEntry[],
    options: WriteFilesOptions = {},
  ): Promise<EntryInfo[]> {
    const sources = validateWriteSources(files);
    const metadata = validateMetadata(options.metadata);
    const gzip = options.gzip ?? false;
    await this.#requireWriteFeatures(metadata, gzip);
    const deadline = new OperationDeadline(
      (size) => fileRequestDeadlineMs(size, options.requestTimeoutMs),
      this.core.now,
      options.requestTimeoutMs !== undefined,
    );
    const routed = await this.#routedIndexes(sources);
    const results: Array<EntryInfo | undefined> = sources.map(() => undefined);
    const grpcIndexes = sources.flatMap((_, index) => (routed.has(index) ? [] : [index]));
    if (grpcIndexes.length > 0) {
      const written = await this.#writeGrpc(
        grpcIndexes.map((index) => sources[index] as WriteSource),
        options,
        metadata,
        gzip,
      );
      grpcIndexes.forEach((index, position) => {
        results[index] = written[position];
      });
    }
    for (const index of routed) {
      const source = sources[index] as WriteSource;
      results[index] = await this.#transfers.importUpload(
        { path: source.path, mode: source.mode, body: routedBody(source.data) },
        { user: options.user, metadata, deadline },
      );
    }
    return results as EntryInfo[];
  }

  /**
   * Una URL prefirmada de S3 a la que cualquier cliente HTTP sube el fichero
   * sin cabeceras de Rayito (`PUT`, o `POST` con `form: true`); `rayd` lo
   * importa a `path` como `user` en cuanto aparece. Necesita `transfer` (o
   * `RAYITO_TRANSFER_BUCKET`) y un agente M9.
   */
  async uploadUrl(path: string, options: UploadUrlOptions = {}): Promise<UploadTicket> {
    this.#transfers.requireStaging("uploadUrl");
    return this.#transfers.uploadUrl(validatePath(path), options);
  }

  /**
   * Una URL prefirmada de S3 con una foto de `path` tomada ahora, servida a
   * cualquier cliente HTTP sin cabeceras hasta que caduque. Un fichero que
   * no existe lanza `FileNotFoundError` antes de firmar nada.
   */
  async downloadUrl(path: string, options: DownloadUrlOptions = {}): Promise<DownloadLink> {
    const staging = this.#transfers.requireStaging("downloadUrl");
    const lifetime = effectiveExpiresIn(
      options.expiresIn ?? TRANSFER_DEFAULT_EXPIRES_IN_SECONDS,
      staging,
    );
    const filename = downloadFilename(validatePath(path), options.filename);
    await this.#transfers.requireSupport("downloadUrl");
    const entry = requireRegularFile(await this.getInfo(path, options));
    return this.#transfers.downloadUrl({
      feature: "downloadUrl",
      path,
      user: options.user,
      size: entry.size,
      deadlineMs: fileRequestDeadlineMs(entry.size, options.requestTimeoutMs),
      entry,
      lifetime,
      filename,
    });
  }

  async #requireWriteFeatures(
    metadata: Readonly<Record<string, string>>,
    gzip: boolean,
  ): Promise<void> {
    if (gzip) {
      await this.#transfers.requireSupport("files.write(gzip)");
    }
    if (Object.keys(metadata).length > 0) {
      await this.#transfers.requireSupport("files.write(metadata)");
    }
  }

  /** Lo que va por S3; nada si no hay staging o el agente no tiene transferencias. */
  async #routedIndexes(sources: readonly WriteSource[]): Promise<ReadonlySet<number>> {
    const staging = this.core.transfer;
    const candidates = sources.flatMap((source, index) =>
      shouldRoute(knownWriteSize(source.data), staging) ? [index] : [],
    );
    if (candidates.length === 0 || !(await this.#transfers.supportsTransfers())) {
      return new Set();
    }
    return new Set(candidates);
  }

  async #writeGrpc(
    sources: readonly WriteSource[],
    options: WriteFilesOptions,
    metadata: Readonly<Record<string, string>>,
    gzip: boolean,
  ): Promise<EntryInfo[]> {
    const prepared: PreparedWrite[] = [];
    for (const source of sources) {
      prepared.push({
        path: source.path,
        data: await materialiseWriteData(source.data),
        mode: source.mode,
      });
    }
    const deadlineMs = fileRequestDeadlineMs(totalWriteBytes(prepared), options.requestTimeoutMs);
    const invoke = (client: FilesystemClient, callOptions: CallOptions) =>
      client.write(buildWriteRequests(prepared, options.user, metadata), callOptions);
    const response = gzip
      ? await this.core.translatedUnary(
          () => invoke(this.core.gzipFilesystemClient(), { timeoutMs: deadlineMs }),
          { filesystem: true },
        )
      : await this.core.filesCall(invoke, deadlineMs);
    return response.entries.map(entryInfoFromProto);
  }

  async #routes(size: number): Promise<boolean> {
    return shouldRoute(size, this.core.transfer) && (await this.#transfers.supportsTransfers());
  }

  async #readThroughS3(
    path: string,
    user: string | undefined,
    size: number,
    deadlineMs: number,
    idleMs: number | undefined,
    deadline: OperationDeadline | undefined,
  ): Promise<ReadableStream<Uint8Array>> {
    const exported = await this.#transfers.exportToStaging({
      feature: "read",
      path,
      user,
      size,
      deadlineMs,
      idleMs,
    });
    return this.#transfers.streamExported(exported, idleMs, deadline);
  }

  async list(path: string, options: ListOptions = {}): Promise<EntryInfo[]> {
    const request = listDirRequest(path, options.depth ?? DEFAULT_DEPTH, options.user);
    const response = await this.core.filesCall(
      (client, callOptions) => client.listDir(request, callOptions),
      options.requestTimeoutMs,
      options.signal,
    );
    return response.entries.map(entryInfoFromProto);
  }

  async exists(path: string, options: UserOptions = {}): Promise<boolean> {
    try {
      await this.getInfo(path, options);
    } catch (error) {
      if (error instanceof FileNotFoundError) {
        return false;
      }
      throw error;
    }
    return true;
  }

  async getInfo(path: string, options: UserOptions = {}): Promise<EntryInfo> {
    const request = statRequest(path, options.user);
    const response = await this.core.filesCall(
      (client, callOptions) => client.stat(request, callOptions),
      options.requestTimeoutMs,
      options.signal,
    );
    if (response.entry === undefined) {
      throw new SandboxError("Stat respondió sin entry");
    }
    return entryInfoFromProto(response.entry);
  }

  async remove(path: string, options: RemoveOptions = {}): Promise<void> {
    const request = removeRequest(path, options.recursive ?? true, options.user);
    await this.core.filesCall(
      (client, callOptions) => client.remove(request, callOptions),
      options.requestTimeoutMs,
      options.signal,
    );
  }

  async rename(oldPath: string, newPath: string, options: UserOptions = {}): Promise<EntryInfo> {
    const request = moveRequest(oldPath, newPath, options.user);
    const response = await this.core.filesCall(
      (client, callOptions) => client.move(request, callOptions),
      options.requestTimeoutMs,
      options.signal,
    );
    if (response.entry === undefined) {
      throw new SandboxError("Move respondió sin entry");
    }
    return entryInfoFromProto(response.entry);
  }

  /** `false` cuando el directorio ya existía (`AlreadyExists`). */
  async makeDir(path: string, options: UserOptions = {}): Promise<boolean> {
    const request = makeDirRequest(path, options.user);
    const timeoutMs = this.core.resolveRequestTimeout(options.requestTimeoutMs);
    try {
      await this.core.callUnary(() => this.core.clients.filesystem.makeDir(request, { timeoutMs }));
    } catch (error) {
      if (isAlreadyExists(error)) {
        return false;
      }
      throw translateRpcError(error, { filesystem: true });
    }
    return true;
  }

  /** Resuelve tras `WatchStarted`; el consumidor corre en una tarea aparte. */
  async watchDir(path: string, options: WatchOptions = {}): Promise<WatchHandle> {
    const request = watchDirRequest(
      path,
      options.recursive ?? false,
      options.includeEntry ?? false,
      options.user,
    );
    const deadline = validateWatchTimeout(options.timeoutMs);
    const opened = await this.openWatch(request, deadline, options.signal);
    const handle = new WatchHandle({
      filesystem: this,
      opened,
      path,
      state: new WatchState(options.onEvent, (message, fields) =>
        this.core.logger?.warn?.(message, { ...fields, path }),
      ),
      onExit: options.onExit,
      request,
      deadlineAt: deadlineAt(deadline, this.core.now),
    });
    this.core.trackWatch(handle);
    handle.start();
    return handle;
  }

  async openWatch(
    request: WatchDirRequest,
    deadlineMs: number | undefined,
    signal?: AbortSignal,
  ): Promise<OpenedStream<WatchDirResponse>> {
    const opened = await this.core.openStream(
      (client, callOptions) => client.watchDir(request, withTimeout(callOptions, deadlineMs)),
      { service: FilesystemService, stream: true, filesystem: true, signal },
    );
    try {
      requireWatchStarted(opened.first);
    } catch (error) {
      opened.controller.abort();
      throw error;
    }
    return opened;
  }

  async readStream(
    request: ReturnType<typeof readRequest>,
    deadlineMs: number,
    options: ReadStreamOptions = {},
  ): Promise<ReadableStream<Uint8Array>> {
    const gzip = options.gzip ?? false;
    const opened = await this.core.openStream(
      (client, callOptions) =>
        client.read(request, readCallOptions(withTimeout(callOptions, deadlineMs), gzip)),
      { service: FilesystemService, stream: false, allowEmpty: true, filesystem: true },
    );
    this.core.trackStream(opened.controller);
    return readableFromOpened(
      opened,
      (error) => this.core.streamFailure(error, { filesystem: true }),
      options.idleMs,
    );
  }
}

type ReadOptionsFor<F> = Omit<ReadOptions, "format"> & F;
export type ReadResult = string | Uint8Array | ReadableStream<Uint8Array> | Blob;

export interface ReadStreamOptions {
  readonly gzip?: boolean | undefined;
  readonly idleMs?: number | undefined;
}

/** `rayito-compress: gzip` pide a `rayd` la respuesta comprimida; sin ella responde `identity`. */
function readCallOptions(options: CallOptions, gzip: boolean): CallOptions {
  return gzip ? { ...options, headers: GZIP_READ_HEADERS } : options;
}

async function formatRead(
  stream: ReadableStream<Uint8Array>,
  format: ReadFormat,
): Promise<ReadResult> {
  if (format === "stream") {
    return stream;
  }
  const data = concatChunks(await collect(stream));
  if (format === "bytes") {
    return data;
  }
  if (format === "blob") {
    return new Blob([data], { type: OCTET_STREAM });
  }
  return decodeText(data);
}

function readableFromOpened(
  opened: OpenedStream<ReadResponse>,
  classify: (error: unknown) => Promise<Error>,
  idleMs?: number | undefined,
): ReadableStream<Uint8Array> {
  let first: ReadResponse | undefined = opened.first;
  return new ReadableStream<Uint8Array>({
    async pull(controller) {
      if (first !== undefined) {
        const chunk = first.chunk;
        first = undefined;
        controller.enqueue(chunk);
        return;
      }
      let result: IteratorResult<ReadResponse>;
      try {
        result = await nextWithinIdle(opened.iterator.next(), idleMs, () =>
          opened.controller.abort(),
        );
      } catch (error) {
        opened.controller.abort();
        throw await classify(error);
      }
      if (result.done) {
        opened.controller.abort();
        controller.close();
        return;
      }
      controller.enqueue(result.value.chunk);
    },
    cancel() {
      opened.controller.abort();
    },
  });
}

function withinMs(task: Promise<void>, ms: number): Promise<void> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<void>((resolve) => {
    timer = setTimeout(resolve, ms);
    timer.unref?.();
  });
  return Promise.race([task, deadline]).finally(() => {
    if (timer !== undefined) {
      clearTimeout(timer);
    }
  });
}

export async function collect(stream: ReadableStream<Uint8Array>): Promise<Uint8Array[]> {
  const chunks: Uint8Array[] = [];
  const reader = stream.getReader();
  while (true) {
    const { done, value } = await reader.read();
    if (done) {
      return chunks;
    }
    chunks.push(value);
  }
}

export interface WatchHandleInit {
  readonly filesystem: Filesystem;
  readonly opened: OpenedStream<WatchDirResponse>;
  readonly path: string;
  readonly state: WatchState;
  readonly onExit: ExitCallback | undefined;
  readonly request: WatchDirRequest;
  readonly deadlineAt: number | undefined;
}

/**
 * Un `watchDir` vivo: `getNewEvents()`, `stop()`, `await using`. Tras un
 * suspend/resume re-emite el mismo `WatchDir` sin llamar a `onExit`; leerlo
 * nunca despierta un sandbox suspendido.
 */
export class WatchHandle implements Abortable {
  readonly #filesystem: Filesystem;
  #opened: OpenedStream<WatchDirResponse>;
  readonly #path: string;
  readonly #state: WatchState;
  readonly #onExit: ExitCallback | undefined;
  readonly #request: WatchDirRequest;
  readonly #deadlineAt: number | undefined;
  #generation: number;
  #reconnects = 0;
  readonly #budget = new ReconnectBudget();
  #pendingCut: ConnectError | undefined;
  #task: Promise<void> | undefined;

  constructor(init: WatchHandleInit) {
    this.#filesystem = init.filesystem;
    this.#opened = init.opened;
    this.#path = init.path;
    this.#state = init.state;
    this.#onExit = init.onExit;
    this.#request = init.request;
    this.#deadlineAt = init.deadlineAt;
    this.#generation = init.filesystem.core.resumeGeneration;
  }

  get path(): string {
    return this.#path;
  }

  get reconnects(): number {
    return this.#reconnects;
  }

  get isRunning(): boolean {
    return this.#state.isRunning;
  }

  getNewEvents(): FilesystemEvent[] {
    return this.#state.drain();
  }

  /** Aborta el stream y espera al consumidor como mucho 5 s; idempotente. */
  async stop(): Promise<void> {
    if (this.#state.stopped) {
      return;
    }
    this.abortNow();
    if (this.#task !== undefined) {
      await withinMs(this.#task, WATCH_STOP_JOIN_MS);
    }
  }

  abortNow(): void {
    this.#state.stopped = true;
    this.#opened.controller.abort();
    this.#filesystem.core.untrackWatch(this);
  }

  async [Symbol.asyncDispose](): Promise<void> {
    await this.stop();
  }

  start(): void {
    this.#task = this.#consume();
  }

  get core(): SandboxCore {
    return this.#filesystem.core;
  }

  async #consume(): Promise<void> {
    let failure: Error | undefined;
    try {
      await this.#pump();
    } catch (error) {
      failure = await this.#classify(error);
    } finally {
      this.#finish(failure);
    }
  }

  async #pump(): Promise<void> {
    let reopen = await this.#pumpUntilCut();
    while (reopen) {
      reopen = (await this.#reissue()) && (await this.#pumpUntilCut());
    }
  }

  /** `true` si terminó por un corte reconectable con el watch vivo (hay que reabrirlo); `false` si terminó limpiamente. */
  async #pumpUntilCut(): Promise<boolean> {
    try {
      while (true) {
        const result = await this.#opened.iterator.next();
        if (result.done) {
          return false;
        }
        this.#state.feed(result.value);
      }
    } catch (error) {
      if (this.#state.stopped || !this.core.isReconnectable(error)) {
        throw error;
      }
      this.#pendingCut = error as ConnectError;
      return true;
    }
  }

  /** Espera al agente y vuelve a emitir el mismo `WatchDir`; `false` si alguien llamó a `stop()`. */
  async #reissue(): Promise<boolean> {
    const reason = this.#pendingCut;
    if (reason === undefined) {
      throw new SandboxError("el watch no puede reabrirse sin su motivo de corte");
    }
    this.#pendingCut = undefined;
    const outcome = await this.core.reconnect(reason, this.#generation, { wake: false });
    if (!outcome.resumed) {
      throw this.core.reconnectError(outcome, reason);
    }
    this.#generation = outcome.resumeGeneration;
    if (this.#state.stopped) {
      return false;
    }
    if (!this.#budget.allows(outcome)) {
      throw reason;
    }
    this.#opened = await this.#filesystem.openWatch(this.#request, this.#remainingDeadline());
    this.#reconnects += 1;
    if (this.#state.stopped) {
      this.#opened.controller.abort();
    }
    return true;
  }

  #remainingDeadline(): number | undefined {
    const remaining = remainingDeadlineMs(this.#deadlineAt, this.core.now);
    if (remaining !== undefined && remaining <= 0) {
      throw new TimeoutError(`el deadline del watch de ${this.#path} venció durante la reconexión`);
    }
    return remaining;
  }

  async #classify(error: unknown): Promise<Error | undefined> {
    if (!this.#state.stopped && error instanceof ConnectError && isStreamReset(error)) {
      return this.core.streamFailure(error, { filesystem: true });
    }
    if (error instanceof ConnectError) {
      return watchFailure(error, this.#state.stopped);
    }
    return error instanceof Error ? error : new SandboxError(String(error));
  }

  /** Cancela el RPC antes de dar el watch por terminado: `rayd` retendría el inotify hasta cerrar la sesión. */
  #finish(failure: Error | undefined): void {
    this.#opened.controller.abort();
    this.#state.recordEnd(failure);
    this.core.untrackWatch(this);
    if (failure !== undefined && this.#onExit !== undefined) {
      try {
        this.#onExit(failure);
      } catch (error) {
        this.core.logger?.warn?.("onExit lanzó una excepción", {
          path: this.#path,
          reason: errorMessage(error),
        });
      }
    }
  }
}
