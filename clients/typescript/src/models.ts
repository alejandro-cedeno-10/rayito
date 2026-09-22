/**
 * Modelos públicos del SDK: objetos inmutables construidos por mappers, sin
 * I/O. `Execution`, `Result` y `HostAccess` son clases porque llevan
 * comportamiento (`text`, `formats()`, `toJSON()`, `toString()`).
 */

import type { Chart } from "./charts.js";
import { InvalidArgumentError } from "./errors.js";
import {
  IDLE_MAX_IDLE_MIN_SECONDS,
  IDLE_SUSPENDED_MIN_SECONDS,
  PORT_MAX,
  PORT_MIN,
} from "./limits.js";

export const PROXY_AUTH_HEADER = "x-aws-proxy-auth";
export const PROXY_PORT_HEADER = "x-aws-proxy-port";

export const DEFAULT_MAX_IDLE_SECONDS = 300;

/**
 * Espejo de `idlePolicy` de `run-microvm`. `suspendedDurationSeconds`
 * ausente se resuelve en `create()` como `timeout − maxIdleSeconds`; `0`
 * significa terminar al suspender. Un ciclo suspend/resume cuesta ≈ 5 min de
 * cómputo a 2 GB (SPEC.md §7), así que un `maxIdleSeconds` menor que 300 no
 * ahorra dinero; el mínimo que acepta la API es 60.
 */
export interface IdlePolicy {
  readonly maxIdleSeconds: number;
  readonly suspendedDurationSeconds?: number | undefined;
  readonly autoResume: boolean;
}

export type IdlePolicyInput = Partial<IdlePolicy>;

export function defaultIdlePolicy(): IdlePolicy {
  return Object.freeze({ maxIdleSeconds: DEFAULT_MAX_IDLE_SECONDS, autoResume: true });
}

export function validateIdlePolicy(input: IdlePolicyInput): IdlePolicy {
  const maxIdleSeconds = input.maxIdleSeconds ?? DEFAULT_MAX_IDLE_SECONDS;
  if (!Number.isInteger(maxIdleSeconds) || maxIdleSeconds < IDLE_MAX_IDLE_MIN_SECONDS) {
    throw new InvalidArgumentError(
      `maxIdleSeconds debe ser un entero >= ${IDLE_MAX_IDLE_MIN_SECONDS}, recibido ${maxIdleSeconds}`,
    );
  }
  const suspended = input.suspendedDurationSeconds;
  if (
    suspended !== undefined &&
    (!Number.isInteger(suspended) || suspended < IDLE_SUSPENDED_MIN_SECONDS)
  ) {
    throw new InvalidArgumentError(
      `suspendedDurationSeconds debe ser un entero >= ${IDLE_SUSPENDED_MIN_SECONDS}, recibido ${suspended}`,
    );
  }
  return Object.freeze({
    maxIdleSeconds,
    suspendedDurationSeconds: suspended,
    autoResume: input.autoResume ?? true,
  });
}

export interface SandboxInfoFields {
  readonly sandboxId: string;
  readonly state: string;
  readonly endpoint: string;
  readonly template: string;
  readonly templateVersion: string;
  readonly startedAt: Date;
  readonly maximumDurationSeconds: number;
  readonly terminatedAt?: Date | undefined;
  readonly stateReason?: string | undefined;
  readonly idle?: IdlePolicy | undefined;
  readonly executionRoleArn?: string | undefined;
}

/**
 * Lo que devuelve `get-microvm` (y `run-microvm`), normalizado. `endpoint`
 * es siempre el hostname pelado; `endpointUrl` le antepone `https://`.
 * `template` es el ARN de la imagen.
 */
export interface SandboxInfo extends SandboxInfoFields {
  readonly terminatedAt: Date | undefined;
  readonly stateReason: string | undefined;
  readonly idle: IdlePolicy | undefined;
  readonly executionRoleArn: string | undefined;
  readonly endpointUrl: string;
  readonly expiresAt: Date;
  readonly templateName: string;
  remainingSeconds(now?: Date): number;
}

export function sandboxInfo(fields: SandboxInfoFields): SandboxInfo {
  const expiresAt = new Date(fields.startedAt.getTime() + fields.maximumDurationSeconds * 1000);
  return Object.freeze({
    sandboxId: fields.sandboxId,
    state: fields.state,
    endpoint: fields.endpoint,
    template: fields.template,
    templateVersion: fields.templateVersion,
    startedAt: fields.startedAt,
    maximumDurationSeconds: fields.maximumDurationSeconds,
    terminatedAt: fields.terminatedAt,
    stateReason: fields.stateReason,
    idle: fields.idle,
    executionRoleArn: fields.executionRoleArn,
    endpointUrl: `https://${fields.endpoint}`,
    expiresAt,
    templateName: templateNameFromArn(fields.template),
    remainingSeconds(now?: Date): number {
      const current = now ?? new Date();
      return Math.max(0, (expiresAt.getTime() - current.getTime()) / 1000);
    },
  });
}

/** Un item de `list-microvms`: no trae `endpoint` ni `stateReason`. */
export interface SandboxListItem {
  readonly sandboxId: string;
  readonly state: string;
  readonly template: string;
  readonly templateVersion: string;
  readonly startedAt: Date;
  readonly templateName: string;
}

export function sandboxListItem(fields: Omit<SandboxListItem, "templateName">): SandboxListItem {
  return Object.freeze({ ...fields, templateName: templateNameFromArn(fields.template) });
}

/**
 * Resultado de `getHost(port)`. `toString()` devuelve el hostname del MicroVM
 * para que `` `https://${host}` `` siga funcionando como en E2B; además expone
 * `url`, `port` y `headers`. Las cabeceras se leen en cada acceso para
 * reflejar el JWE renovado por el refresher; nunca incluyen
 * `x-aws-proxy-force-h2` (eso es sólo para gRPC).
 */
export class HostAccess {
  readonly host: string;
  readonly port: number;
  readonly #tokenProvider: () => string;

  constructor(host: string, port: number, tokenProvider: () => string) {
    this.host = host;
    this.port = port;
    this.#tokenProvider = tokenProvider;
  }

  get url(): string {
    return `https://${this.host}`;
  }

  get headers(): Record<string, string> {
    return { [PROXY_AUTH_HEADER]: this.#tokenProvider(), [PROXY_PORT_HEADER]: String(this.port) };
  }

  toString(): string {
    return this.host;
  }

  toJSON(): { host: string; port: number } {
    return { host: this.host, port: this.port };
  }
}

export type ProcessKindName = "process" | "pty";

export const MAX_PTY_DIMENSION = 4096;
export const DEFAULT_PTY_COLS = 80;
export const DEFAULT_PTY_ROWS = 24;

/** Tamaño de una terminal (`PtyService` `PtySize`): `1 <= cols, rows <= 4096`. */
export interface PtySize {
  readonly cols: number;
  readonly rows: number;
}

export function validatePtyDimension(value: unknown, field: string): number {
  if (
    typeof value !== "number" ||
    !Number.isInteger(value) ||
    value < 1 ||
    value > MAX_PTY_DIMENSION
  ) {
    throw new InvalidArgumentError(
      `${field} debe ser un entero entre 1 y ${MAX_PTY_DIMENSION}, recibido ${String(value)}`,
    );
  }
  return value;
}

export function validatePtySize(size: Partial<PtySize> | undefined): PtySize | undefined {
  if (size === undefined) {
    return undefined;
  }
  if (typeof size !== "object" || size === null) {
    throw new InvalidArgumentError(`size debe ser un PtySize, recibido ${typeof size}`);
  }
  return Object.freeze({
    cols: validatePtyDimension(size.cols ?? DEFAULT_PTY_COLS, "cols"),
    rows: validatePtyDimension(size.rows ?? DEFAULT_PTY_ROWS, "rows"),
  });
}

/**
 * Salida completa de un comando que terminó con exit code 0. Un exit code
 * distinto de cero llega como `CommandExitError`, que lleva los mismos campos;
 * `error` queda reservado para futuros estados sin excepción.
 */
export interface CommandResult {
  readonly stdout: string;
  readonly stderr: string;
  readonly exitCode: number;
  readonly error: string | undefined;
}

/**
 * Un item de `ProcessService.List`: `cmd`/`args` son los del wrapper
 * (`/bin/bash -l -c <cmd>`), no el comando que escribió el usuario.
 */
export interface ProcessInfo {
  readonly pid: number;
  readonly cmd: string;
  readonly args: readonly string[];
  readonly envs: Readonly<Record<string, string>>;
  readonly cwd: string | undefined;
  readonly tag: string | undefined;
  readonly kind: ProcessKindName;
}

/**
 * `HealthService.Health` tal como lo expone `getHealth()`. `resumeGeneration`
 * cuenta los `/resume` aceptados desde el arranque del agente;
 * `clockOffsetMs` es `wall_delta − monotonic_delta` entre el último
 * `/suspend` y su `/resume` (0 si no hubo); `kernelStateLost` es `true`
 * cuando algún kernel no respondió a la sonda de `/resume` y fue reiniciado.
 * `uptimeMs` incluye el tiempo suspendido.
 */
export interface SandboxHealth {
  readonly agentReady: boolean;
  readonly kernelReady: boolean;
  readonly agentVersion: string;
  readonly uptimeMs: number;
  readonly sandboxId: string;
  readonly resumeGeneration: number;
  readonly clockOffsetMs: number;
  readonly kernelStateLost: boolean;
}

/** `HealthService.Metrics`: instantánea procfs del MicroVM. */
export interface SandboxMetrics {
  readonly cpuUsedPct: number;
  readonly memUsedBytes: number;
  readonly memTotalBytes: number;
  readonly diskUsedBytes: number;
  readonly diskTotalBytes: number;
  readonly cpuCount: number;
  readonly timestamp: Date;
}

export const FileType = { FILE: "file", DIR: "dir", SYMLINK: "symlink" } as const;
export type FileType = (typeof FileType)[keyof typeof FileType];

/**
 * `EntryInfo` de `FilesystemService`, sin seguir enlaces simbólicos. `type`
 * es `undefined` para lo que no es fichero regular, directorio ni symlink
 * (FIFO, socket, dispositivo). `path` es la ruta pedida normalizada, nunca la
 * canónica. `permissions` tiene la forma de `ls -l` (`-rw-r--r--`) y `mode`
 * son los bits `st_mode & 0o7777`. `symlinkTarget` sólo en symlinks.
 */
export interface EntryInfo {
  readonly name: string;
  readonly type: FileType | undefined;
  readonly path: string;
  readonly size: number;
  readonly mode: number;
  readonly permissions: string;
  readonly owner: string;
  readonly group: string;
  readonly modifiedTime: Date;
  readonly symlinkTarget: string | undefined;
}

export const FilesystemEventType = {
  CREATE: "create",
  WRITE: "write",
  REMOVE: "remove",
  RENAME: "rename",
  CHMOD: "chmod",
} as const;
export type FilesystemEventType = (typeof FilesystemEventType)[keyof typeof FilesystemEventType];

/**
 * Un evento de `watchDir`: `name` es relativo al directorio observado
 * (`sub/b.txt` en modo recursivo) y `entry` sólo llega con `includeEntry` en
 * `create`/`write`/`chmod`.
 */
export interface FilesystemEvent {
  readonly name: string;
  readonly type: FilesystemEventType;
  readonly entry: EntryInfo | undefined;
}

export type WriteData = string | Uint8Array | ArrayBuffer | Blob | ReadableStream<Uint8Array>;

/** Un fichero de `files.writeFiles`: `data` se materializa en memoria. */
export interface WriteEntry {
  readonly path: string;
  readonly data: WriteData;
  readonly mode?: number | undefined;
}

/** Un contexto de `CodeService`: un kernel con su propio scope y cwd. */
export interface CodeContext {
  readonly id: string;
  readonly language: string;
  readonly cwd: string;
}

/**
 * Un `OutputChunk` de `Execute`: `timestamp` en ns Unix del sidecar y
 * `error === true` cuando vino por stderr. Un chunk no es una línea.
 */
export interface OutputMessage {
  readonly line: string;
  readonly timestamp: number;
  readonly error: boolean;
}

/** Salida de una ejecución, un elemento por `OutputChunk` recibido. */
export interface Logs {
  readonly stdout: string[];
  readonly stderr: string[];
}

/**
 * Error del kernel (`ZeroDivisionError`…) o sintético del agente
 * (`ExecutionTimeout`, `KernelDied`, `KernelRestarted`, `ContextDestroyed`,
 * `ExecutionAborted`, `OutputTruncated`); `traceback` son las líneas de
 * Jupyter unidas con `"\n"`, vacío en los sintéticos.
 */
export interface ExecutionError {
  readonly name: string;
  readonly value: string;
  readonly traceback: string;
}

export const RESULT_FORMAT_ORDER = [
  "text",
  "html",
  "markdown",
  "svg",
  "png",
  "jpeg",
  "pdf",
  "latex",
  "javascript",
  "json",
  "data",
  "chart",
] as const;
export type ResultFormat = (typeof RESULT_FORMAT_ORDER)[number];

export interface ResultFields {
  readonly text?: string | undefined;
  readonly html?: string | undefined;
  readonly markdown?: string | undefined;
  readonly svg?: string | undefined;
  readonly png?: string | undefined;
  readonly jpeg?: string | undefined;
  readonly pdf?: string | undefined;
  readonly latex?: string | undefined;
  readonly javascript?: string | undefined;
  readonly json?: unknown;
  readonly data?: unknown;
  readonly chart?: Chart | undefined;
  readonly isMainResult?: boolean | undefined;
  readonly extra?: Readonly<Record<string, string>> | undefined;
  readonly raw?: Readonly<Record<string, string>> | undefined;
}

/**
 * Un mime bundle de Jupyter (`display_data` o `execute_result`). Las imágenes
 * (`png`, `jpeg`, `pdf`) son base64 tal cual llegan; `json` y `data` son el
 * documento parseado (o la cadena original si no era JSON válido); `chart` es
 * el `Chart` de `e2b/chart`. `raw` conserva cada mime como cadena y `extra`
 * los mime types sin campo propio (incluido `rayito/omitted` cuando el
 * sidecar descartó un valor de más de 8 MiB).
 */
export class Result {
  readonly text: string | undefined;
  readonly html: string | undefined;
  readonly markdown: string | undefined;
  readonly svg: string | undefined;
  readonly png: string | undefined;
  readonly jpeg: string | undefined;
  readonly pdf: string | undefined;
  readonly latex: string | undefined;
  readonly javascript: string | undefined;
  readonly json: unknown;
  readonly data: unknown;
  readonly chart: Chart | undefined;
  readonly isMainResult: boolean;
  readonly extra: Readonly<Record<string, string>>;
  readonly raw: Readonly<Record<string, string>>;

  constructor(fields: ResultFields = {}) {
    this.text = fields.text;
    this.html = fields.html;
    this.markdown = fields.markdown;
    this.svg = fields.svg;
    this.png = fields.png;
    this.jpeg = fields.jpeg;
    this.pdf = fields.pdf;
    this.latex = fields.latex;
    this.javascript = fields.javascript;
    this.json = fields.json;
    this.data = fields.data;
    this.chart = fields.chart;
    this.isMainResult = fields.isMainResult ?? false;
    this.extra = Object.freeze({ ...(fields.extra ?? {}) });
    this.raw = Object.freeze({ ...(fields.raw ?? {}) });
    Object.freeze(this);
  }

  /** Campos presentes, en el orden de `RESULT_FORMAT_ORDER`, y después las claves de `extra`. */
  formats(): string[] {
    const present = RESULT_FORMAT_ORDER.filter((name) => this[name] !== undefined);
    return [...present, ...Object.keys(this.extra)];
  }

  toString(): string {
    return this.text ?? "";
  }

  toJSON(): Record<string, unknown> {
    return { is_main_result: this.isMainResult, ...this.raw };
  }
}

/**
 * Resultado de `runCode`. Un error del kernel es dato (`error`), nunca
 * excepción; `executionCount` es el de Jupyter (`undefined` si el kernel no
 * llegó a empezar la celda).
 */
export class Execution {
  readonly results: Result[];
  readonly logs: Logs;
  error: ExecutionError | undefined;
  executionCount: number | undefined;

  constructor(
    fields: Partial<Pick<Execution, "results" | "logs" | "error" | "executionCount">> = {},
  ) {
    this.results = fields.results ?? [];
    this.logs = fields.logs ?? { stdout: [], stderr: [] };
    this.error = fields.error;
    this.executionCount = fields.executionCount;
  }

  /** `text/plain` del `execute_result` (la última expresión de la celda). */
  get text(): string | undefined {
    return this.results.find((result) => result.isMainResult)?.text;
  }

  toJSON(): Record<string, unknown> {
    return {
      results: this.results.map((result) => result.toJSON()),
      logs: { stdout: [...this.logs.stdout], stderr: [...this.logs.stderr] },
      error: this.error === undefined ? null : executionErrorToJson(this.error),
      execution_count: this.executionCount ?? null,
    };
  }
}

export function executionErrorToJson(error: ExecutionError): Record<string, string> {
  return { name: error.name, value: error.value, traceback: error.traceback };
}

/** Lo que entrega la iteración de un `CommandHandle` (`stdout`/`stderr`) o de un `PtyHandle` (`pty`). */
export type OutputChunk =
  | { readonly stdout: string; readonly stderr?: undefined; readonly pty?: undefined }
  | { readonly stderr: string; readonly stdout?: undefined; readonly pty?: undefined }
  | { readonly pty: Uint8Array; readonly stdout?: undefined; readonly stderr?: undefined };

export function templateNameFromArn(imageArn: string): string {
  const index = imageArn.lastIndexOf(":");
  return index < 0 ? imageArn : imageArn.slice(index + 1);
}

export function validatePort(port: unknown, field = "port"): number {
  if (typeof port !== "number" || !Number.isInteger(port) || port < PORT_MIN || port > PORT_MAX) {
    throw new InvalidArgumentError(
      `${field} debe ser un entero entre ${PORT_MIN} y ${PORT_MAX}, recibido ${String(port)}`,
    );
  }
  return port;
}
