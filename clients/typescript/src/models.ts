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
  LIFECYCLE_TIMEOUT_EXIT_CODE,
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

/** El `stateReason` de un MicroVM cuyo `rayd` salió al vencer el plazo en modo `kill` (Q63). */
export const TIMED_OUT_STATE_REASON = `Container Stopped with Exit Code: ${LIFECYCLE_TIMEOUT_EXIT_CODE}`;

export type LifecyclePhaseName = "unmanaged" | "active" | "resumeGrace" | "expired";

/**
 * El plazo lógico que impone `rayd` (ADR-011), leído de `Health` o de la
 * respuesta de `setTimeout`. `unmanaged`: el sandbox se creó sin
 * `maxLifetimeMs` ni `onTimeout` y su vida es la de la plataforma.
 * `resumeGrace`: reanudado tras el plazo, espera un `connect()` 30 s.
 * `expired`: vencido; sólo `Health` y `setTimeout` responden. `deadline` y
 * `cap` son reloj de pared (`undefined` en `unmanaged`); `cap` es
 * `maxLifetimeMs − 60 s` desde el arranque. `extensions` cuenta los
 * `setTimeout`/`connect` que movieron el plazo en este arranque.
 */
export interface SandboxLifecycle {
  readonly phase: LifecyclePhaseName;
  readonly deadline: Date | undefined;
  readonly cap: Date | undefined;
  readonly timeoutMs: number;
  readonly onTimeout: "kill" | "pause" | undefined;
  readonly autoResume: boolean;
  readonly extensions: number;
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
  readonly ingress?: readonly string[] | undefined;
  readonly egress?: readonly string[] | undefined;
  readonly lifecycle?: SandboxLifecycle | undefined;
  readonly agentVersion?: string | undefined;
  readonly cpuCount?: number | undefined;
  readonly memoryMb?: number | undefined;
  readonly metadata?: Readonly<Record<string, string>> | undefined;
}

/**
 * Lo que devuelve `get-microvm` (y `run-microvm`), normalizado. `endpoint`
 * es siempre el hostname pelado; `endpointUrl` le antepone `https://`.
 * `template` es el ARN de la imagen. `expiresAt` es el plazo lógico cuando
 * `rayd` lo gestiona (`lifecycle` con fase distinta de `unmanaged`) y el tope
 * de la plataforma en otro caso; `platformExpiresAt` es siempre ese tope
 * (`startedAt + maximumDurationSeconds`). `timedOut` es `true` cuando el
 * MicroVM terminó porque `rayd` salió al vencer el plazo. `agentVersion`,
 * `cpuCount` y `memoryMb` son la vista del guest leída de `Health` (sólo los
 * rellena `getInfo()` de una instancia; `undefined` = no leído o agente
 * anterior a M9): `memoryMb` es el `MemTotal` del guest en MiB, no el
 * `minimumMemoryInMiB` de la versión de imagen. `ingress`/`egress` son los
 * ARNs de conectores que devuelve `get-microvm` (`ingressNetworkConnectors`,
 * `egressNetworkConnectors`), vacíos si la respuesta no los trae.
 * `metadata` son los de `create({ metadata })` tal como los devolvió el
 * último `Health` (`get-microvm` no los conoce): `undefined` si no se
 * leyeron del agente y `{}` si se leyeron vacíos.
 */
export interface SandboxInfo extends SandboxInfoFields {
  readonly terminatedAt: Date | undefined;
  readonly stateReason: string | undefined;
  readonly idle: IdlePolicy | undefined;
  readonly executionRoleArn: string | undefined;
  readonly ingress: readonly string[];
  readonly egress: readonly string[];
  readonly lifecycle: SandboxLifecycle | undefined;
  readonly agentVersion: string | undefined;
  readonly cpuCount: number | undefined;
  readonly memoryMb: number | undefined;
  readonly metadata: Readonly<Record<string, string>> | undefined;
  readonly endpointUrl: string;
  readonly expiresAt: Date;
  readonly platformExpiresAt: Date;
  readonly timedOut: boolean;
  readonly templateName: string;
  remainingSeconds(now?: Date): number;
}

function logicalDeadline(lifecycle: SandboxLifecycle | undefined): Date | undefined {
  if (lifecycle === undefined || lifecycle.phase === "unmanaged") {
    return undefined;
  }
  return lifecycle.deadline;
}

export function sandboxInfo(fields: SandboxInfoFields): SandboxInfo {
  const platformExpiresAt = new Date(
    fields.startedAt.getTime() + fields.maximumDurationSeconds * 1000,
  );
  const expiresAt = logicalDeadline(fields.lifecycle) ?? platformExpiresAt;
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
    ingress: Object.freeze([...(fields.ingress ?? [])]),
    egress: Object.freeze([...(fields.egress ?? [])]),
    lifecycle: fields.lifecycle,
    agentVersion: fields.agentVersion,
    cpuCount: fields.cpuCount,
    memoryMb: fields.memoryMb,
    metadata: fields.metadata === undefined ? undefined : Object.freeze({ ...fields.metadata }),
    endpointUrl: `https://${fields.endpoint}`,
    expiresAt,
    platformExpiresAt,
    timedOut: fields.stateReason === TIMED_OUT_STATE_REASON,
    templateName: templateNameFromArn(fields.template),
    remainingSeconds(now?: Date): number {
      const current = now ?? new Date();
      return Math.max(0, (expiresAt.getTime() - current.getTime()) / 1000);
    },
  });
}

/** La misma `SandboxInfo` de `get-microvm` con el plazo lógico leído de `rayd`. */
export function withLifecycle(
  info: SandboxInfo,
  lifecycle: SandboxLifecycle | undefined,
): SandboxInfo {
  return sandboxInfo({ ...info, lifecycle });
}

/**
 * Un item de `list-microvms`: no trae `endpoint` ni `stateReason`.
 * `metadata` sólo llega cuando el listado filtró por metadatos (`undefined` =
 * no leído).
 */
export interface SandboxListItem {
  readonly sandboxId: string;
  readonly state: string;
  readonly template: string;
  readonly templateVersion: string;
  readonly startedAt: Date;
  readonly templateName: string;
  readonly metadata?: Readonly<Record<string, string>> | undefined;
}

export function sandboxListItem(fields: Omit<SandboxListItem, "templateName">): SandboxListItem {
  return Object.freeze({ ...fields, templateName: templateNameFromArn(fields.template) });
}

/** Una página de `list-microvms` sin filtrar; `nextToken` es `undefined` en la última. */
export interface MicrovmListPage {
  readonly items: readonly SandboxListItem[];
  readonly nextToken: string | undefined;
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

/** El `ALL_TRAFFIC` de E2B: en una lista de egress cubre IPv4 e IPv6 (`0.0.0.0/0` y `::/0`). */
export const ALL_TRAFFIC = "0.0.0.0/0";

/**
 * Lo que recibe un selector función de `allowOut`/`denyOut`, con los nombres
 * de E2B (`ctx.allTraffic`, `ctx.rules`). `rules` siempre está vacío: Rayito
 * no tiene `network.rules`.
 */
export interface NetworkSelectorContext {
  readonly allTraffic: string;
  readonly rules: ReadonlyMap<string, readonly unknown[]>;
}

/** Una lista de entradas o una función que la devuelve; se evalúa en el SDK, nunca en `rayd`. */
export type NetworkSelector =
  | readonly string[]
  | ((ctx: NetworkSelectorContext) => readonly string[]);

/**
 * Proxy SOCKS5 del operador (`host:puerto` o `[IPv6]:puerto`). Las
 * credenciales (RFC 1929, 1–255 bytes) sólo viajan en `UpdateNetwork`;
 * `rayd` nunca las devuelve ni las registra.
 */
export interface EgressProxyInput {
  readonly address: string;
  readonly username?: string | undefined;
  readonly password?: string | undefined;
}

/**
 * Política de egress en el guest (sólo `rayito-base-caps`). `allowOut`
 * admite CIDR, IP, `ALL_TRAFFIC` y nombres de host (exacto o `*.sufijo`, sólo
 * 80/443 a través del proxy local); `denyOut`, CIDR e IP. Una entrada
 * permitida gana siempre a una denegada, y `allowOut` sin `denyOut` no
 * restringe nada.
 */
export interface NetworkPolicyInput {
  readonly allowOut?: NetworkSelector | undefined;
  readonly denyOut?: NetworkSelector | undefined;
  readonly egressProxy?: EgressProxyInput | undefined;
}

/**
 * Cómo aplica el guest la política (`Health.egress_enforcement`):
 * `unspecified` es un agente anterior a M9 y, como `none`, significa que no
 * hay política en el guest.
 */
export const EgressEnforcement = {
  UNSPECIFIED: "unspecified",
  NONE: "none",
  GUEST_ROUTES: "guest_routes",
  GUEST_ROUTES_AND_PROXY: "guest_routes_and_proxy",
} as const;
export type EgressEnforcement = (typeof EgressEnforcement)[keyof typeof EgressEnforcement];

/**
 * `NetworkService.GetNetwork`/`UpdateNetwork`: las listas tal como se
 * enviaron, si hay proxy del operador (nunca su dirección ni credenciales) y
 * el puerto del proxy local en `127.0.0.1` (`undefined` si no corre).
 */
export interface NetworkState {
  readonly allowOut: readonly string[];
  readonly denyOut: readonly string[];
  readonly egressProxyConfigured: boolean;
  readonly enforcement: EgressEnforcement;
  readonly localProxyPort: number | undefined;
}

/**
 * `HealthService.Health` tal como lo expone `getHealth()`. `resumeGeneration`
 * cuenta los `/resume` aceptados desde el arranque del agente;
 * `clockOffsetMs` es `wall_delta − monotonic_delta` entre el último
 * `/suspend` y su `/resume` (0 si no hubo); `kernelStateLost` es `true`
 * cuando algún kernel no respondió a la sonda de `/resume` y fue reiniciado.
 * `uptimeMs` incluye el tiempo suspendido. `lifecycle` es `undefined` en un
 * agente anterior a M9, que no impone el plazo lógico. `egressEnforcement`
 * es la política de egress que el guest verificó (`unspecified` en un agente
 * anterior a M9, y cuenta como `none`).
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
  readonly egressEnforcement: EgressEnforcement;
  readonly lifecycle: SandboxLifecycle | undefined;
  /** CPUs que ve el guest; 0 si no se pudieron leer o en un agente anterior a M9. */
  readonly cpuCount: number;
  /** `MemTotal` del guest en bytes (no el tamaño de la imagen); 0 como `cpuCount`. */
  readonly memoryTotalBytes: number;
}

/**
 * `HealthService.Metrics` (instantánea procfs del MicroVM) o una muestra de
 * `MetricsHistory`. `memCacheBytes` es el `Cached` de `/proc/meminfo`; 0 en
 * un agente anterior a M9.
 */
export interface SandboxMetrics {
  readonly cpuUsedPct: number;
  readonly memUsedBytes: number;
  readonly memTotalBytes: number;
  readonly diskUsedBytes: number;
  readonly diskTotalBytes: number;
  readonly cpuCount: number;
  readonly timestamp: Date;
  readonly memCacheBytes: number;
}

export const FileType = { FILE: "file", DIR: "dir", SYMLINK: "symlink" } as const;
export type FileType = (typeof FileType)[keyof typeof FileType];

/**
 * `EntryInfo` de `FilesystemService`, sin seguir enlaces simbólicos. `type`
 * es `undefined` para lo que no es fichero regular, directorio ni symlink
 * (FIFO, socket, dispositivo). `path` es la ruta pedida normalizada, nunca la
 * canónica. `permissions` tiene la forma de `ls -l` (`-rw-r--r--`) y `mode`
 * son los bits `st_mode & 0o7777`. `symlinkTarget` sólo en symlinks.
 * `metadata` son los metadatos de `write({ metadata })` con las claves en
 * minúsculas; vacío si no hay o si el sistema de ficheros no los admite.
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
  readonly metadata: Readonly<Record<string, string>>;
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

/**
 * Un fichero de `files.writeFiles`: `data` se materializa en memoria salvo
 * que vaya por S3 (`transfer` configurado y `>= thresholdBytes`, o un
 * `ReadableStream`, que se sube en streaming).
 */
export interface WriteEntry {
  readonly path: string;
  readonly data: WriteData;
  readonly mode?: number | undefined;
}

/**
 * El bucket de transferencias de `uploadUrl`/`downloadUrl` y de los ficheros
 * grandes (ADR-010). El SDK firma cada URL con tus credenciales; `rayd` nunca
 * guarda ninguna. `bucket` es un nombre DNS sin puntos. `region` ausente usa
 * la del sandbox y debe ser la del bucket. `maxExpiresIn` topa la vida de las
 * URLs de usuario en segundos (el techo real es también la caducidad de tus
 * credenciales). `thresholdBytes` es el tamaño a partir del cual
 * `files.write`/`files.read` van por S3 y `multipartThresholdBytes` el de una
 * exportación multiparte. No hay bucket por defecto: sin `transfer` se leen
 * `RAYITO_TRANSFER_BUCKET`, `RAYITO_TRANSFER_PREFIX` y `RAYITO_TRANSFER_REGION`.
 */
export interface S3Staging {
  readonly bucket: string;
  readonly prefix?: string | undefined;
  readonly region?: string | undefined;
  readonly maxExpiresIn?: number | undefined;
  readonly thresholdBytes?: number | undefined;
  readonly multipartThresholdBytes?: number | undefined;
}

/** Un `S3Staging` validado y con sus valores por defecto, como lo expone `sandbox.transfer`. */
export interface ResolvedS3Staging {
  readonly bucket: string;
  readonly prefix: string;
  readonly region: string | undefined;
  readonly maxExpiresIn: number;
  readonly thresholdBytes: number;
  readonly multipartThresholdBytes: number;
}

export type TransferDirectionName = "import" | "export";
export type TransferPhaseName = "waiting" | "running" | "done" | "failed" | "cancelled";

/**
 * Una foto de `GetTransfer`: `bytesTotal` es 0 hasta que la importación ve
 * el objeto; `probes` cuenta los sondeos del GET; `errorCode` y
 * `errorReason` sólo en `failed`/`cancelled`.
 */
export interface TransferStatus {
  readonly transferId: string;
  readonly direction: TransferDirectionName;
  readonly phase: TransferPhaseName;
  readonly bytesDone: number;
  readonly bytesTotal: number;
  readonly probes: number;
  readonly errorCode: string | undefined;
  readonly errorReason: string | undefined;
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
