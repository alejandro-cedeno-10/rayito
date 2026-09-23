/**
 * `Sandbox` de E2B JS 2.51 por composición sobre el `Sandbox` nativo
 * (`readonly native`): los estáticos y los miembros de instancia llevan los
 * nombres, las unidades y los valores de retorno de E2B y delegan en el SDK
 * nativo. Lo que Lambda MicroVMs no puede dar rechaza (o lanza, si E2B es
 * síncrono) `UnimplementedError` con el motivo de la tabla D14 antes de
 * cualquier llamada. `new E2B(opts).Sandbox` es una subclase ligada a `opts`.
 */

import { InvalidArgumentError, LifecycleUnsupportedError, UnimplementedError } from "../errors.js";
import type {
  CodeContext,
  Execution,
  SandboxInfo as NativeSandboxInfo,
  NetworkState,
} from "../models.js";
import type { ContextLike, CreateContextOptions, RunCodeOptions } from "../sandbox/code.js";
import type { Commands, RequestOptions } from "../sandbox/commands.js";
import type { Git } from "../sandbox/git.js";
import { validateHostPort } from "../sandbox/launch.js";
import { HISTORY_UNIMPLEMENTED_REASON, isHistoryUnavailable } from "../sandbox/metrics.js";
import type { SandboxListPaginator } from "../sandbox/paginator.js";
import { Sandbox as NativeSandbox } from "../sandbox/sandbox.js";
import {
  emitIgnoredWarnings,
  infoFromNative,
  mapCreateOptions,
  mapListOptions,
  mapNetworkUpdate,
  mergeBoundOpts,
  metricsFromNative,
  type NativeConnection,
  normalizedLanguageOrUnimplemented,
  splitConnectionOpts,
  unimplementedLanguage,
  validateKeepMemory,
  validateOnResume,
} from "./compat.js";
import { ConnectionConfig, type ConnectionOpts } from "./connection.js";
import { Filesystem } from "./filesystem.js";
import { Pty } from "./pty.js";
import type {
  SandboxConnectOpts,
  SandboxInfo,
  SandboxListOpts,
  SandboxMetrics,
  SandboxMetricsOpts,
  SandboxNetworkUpdate,
  SandboxOnResume,
  SandboxOpts,
  SandboxPauseOpts,
  SandboxUrlOpts,
} from "./types.js";
import { COMPAT_DOC_PATH, rejectUnimplemented, unimplemented } from "./unimplemented.js";

/** El puerto de `rayd`: el JWE de `trafficAccessToken` es el que cubre este puerto. */
export const ENVD_PORT = 8080;
export const LIFECYCLE_IMAGE_REASON =
  "la imagen no impone el timeout del servidor: publica una imagen M9 (ADR-011)";
export const UPLOAD_URL_PATH_MESSAGE =
  "uploadUrl necesita la ruta de destino: la URL de S3 no lleva el nombre del fichero";

/** El paginador de `Sandbox.list()` de E2B: `hasNext`, `nextToken` y `nextItems()` con `SandboxInfo`. */
export class SandboxPaginator {
  readonly #native: SandboxListPaginator;

  constructor(native: SandboxListPaginator) {
    this.#native = native;
  }

  get hasNext(): boolean {
    return this.#native.hasNext;
  }

  get nextToken(): string | undefined {
    return this.#native.nextToken;
  }

  async nextItems(): Promise<SandboxInfo[]> {
    const items = await this.#native.nextItems();
    return items.map((item) => infoFromNative(item));
  }
}

/** Las opciones de instancia de `connect` de E2B. */
export interface SandboxInstanceConnectOpts extends ConnectionOpts {
  readonly timeoutMs?: number | undefined;
  readonly onResume?: SandboxOnResume | undefined;
}

function nativeConnection(bound: ConnectionOpts, call: ConnectionOpts): NativeConnection {
  const { connection, ignored } = splitConnectionOpts(mergeBoundOpts(bound, call));
  emitIgnoredWarnings(ignored);
  return connection;
}

function historyImageError(feature: string, error: unknown): unknown {
  if (!isHistoryUnavailable(error)) {
    return error;
  }
  return new UnimplementedError(feature, HISTORY_UNIMPLEMENTED_REASON, COMPAT_DOC_PATH, {
    cause: error,
  });
}

function lifecycleImageError(error: unknown): unknown {
  return error instanceof LifecycleUnsupportedError
    ? new UnimplementedError("lifecycle", LIFECYCLE_IMAGE_REASON, COMPAT_DOC_PATH)
    : error;
}

export class Sandbox implements AsyncDisposable {
  /** El `Sandbox` nativo de `rayito`: `checkpointFiles`, `reincarnate`, `getHealth`... */
  readonly native: NativeSandbox;
  readonly files: Filesystem;
  /** Los `Commands` nativos: su forma camelCase ya es la de E2B. */
  readonly commands: Commands;
  readonly pty: Pty;
  readonly git: Git;
  /** La configuración efectiva al crear o conectar (región, timeouts, `retries`, `headers`, `proxy`). */
  readonly connectionConfig: ConnectionConfig;
  readonly #bound: ConnectionOpts;

  protected constructor(native: NativeSandbox, bound: ConnectionOpts, config: ConnectionConfig) {
    this.native = native;
    this.files = new Filesystem(native.files.core);
    this.commands = native.commands;
    this.pty = new Pty(native.pty);
    this.git = native.git;
    this.connectionConfig = config;
    this.#bound = bound;
  }

  // ---------------------------------------------------------------- statics

  static create(opts?: SandboxOpts): Promise<Sandbox>;
  static create(template: string, opts?: SandboxOpts): Promise<Sandbox>;
  static create(templateOrOpts?: string | SandboxOpts, opts?: SandboxOpts): Promise<Sandbox> {
    return Sandbox.createFor(Sandbox, {}, templateOrOpts, opts);
  }

  /** Se conecta a un sandbox existente y lo reanuda si está pausado; `timeoutMs` sólo alarga el plazo. */
  static connect(sandboxId: string, opts: SandboxConnectOpts = {}): Promise<Sandbox> {
    return Sandbox.connectFor(Sandbox, {}, sandboxId, opts);
  }

  static kill(sandboxId: string, opts: ConnectionOpts = {}): Promise<boolean> {
    return Sandbox.killFor({}, sandboxId, opts);
  }

  static getInfo(sandboxId: string, opts: ConnectionOpts = {}): Promise<SandboxInfo> {
    return Sandbox.infoFor({}, sandboxId, opts);
  }

  /** Igual que `getInfo`: Rayito no tiene más campos que dar. */
  static getFullInfo(sandboxId: string, opts: ConnectionOpts = {}): Promise<SandboxInfo> {
    return Sandbox.infoFor({}, sandboxId, opts);
  }

  /** Sólo plano de control: `true` cuando `get-microvm` responde `RUNNING`. */
  static isRunning(sandboxId: string, opts: ConnectionOpts = {}): Promise<boolean> {
    return Sandbox.isRunningFor({}, sandboxId, opts);
  }

  /** `true` si lo suspendió; `false` si ya estaba `SUSPENDING`/`SUSPENDED`. */
  static pause(sandboxId: string, opts: SandboxPauseOpts = {}): Promise<boolean> {
    return Sandbox.pauseFor({}, sandboxId, opts);
  }

  static betaPause(sandboxId: string, opts: SandboxPauseOpts = {}): Promise<boolean> {
    return Sandbox.pauseFor({}, sandboxId, opts);
  }

  /** `SetTimeout(EXACT)` con el access token (`accessToken` o `RAYITO_ACCESS_TOKEN`). */
  static setTimeout(
    sandboxId: string,
    timeoutMs: number,
    opts: ConnectionOpts = {},
  ): Promise<void> {
    return Sandbox.setTimeoutFor({}, sandboxId, timeoutMs, opts);
  }

  /** La serie de métricas de un sandbox `RUNNING` por su id (necesita el access token). */
  static getMetrics(sandboxId: string, opts: SandboxMetricsOpts = {}): Promise<SandboxMetrics[]> {
    return Sandbox.metricsFor({}, sandboxId, opts);
  }

  static list(opts: SandboxListOpts = {}): SandboxPaginator {
    return Sandbox.listFor({}, opts);
  }

  /** Sustituye la política de egress entera (lo omitido se borra, como en E2B). */
  static updateNetwork(
    sandboxId: string,
    network: SandboxNetworkUpdate,
    opts: ConnectionOpts = {},
  ): Promise<void> {
    return Sandbox.updateNetworkFor({}, sandboxId, network, opts);
  }

  static fork(..._args: unknown[]): Promise<never> {
    return rejectUnimplemented("fork");
  }

  static deleteSnapshot(..._args: unknown[]): Promise<never> {
    return rejectUnimplemented("deleteSnapshot");
  }

  static listSnapshots(..._args: unknown[]): never {
    throw unimplemented("listSnapshots");
  }

  // ------------------------------------------------- static implementations

  protected static async createFor(
    cls: typeof Sandbox,
    bound: ConnectionOpts,
    templateOrOpts: string | SandboxOpts | undefined,
    opts: SandboxOpts | undefined,
  ): Promise<Sandbox> {
    const template = typeof templateOrOpts === "string" ? templateOrOpts : undefined;
    const call = typeof templateOrOpts === "string" ? (opts ?? {}) : (templateOrOpts ?? {});
    const merged = mergeBoundOpts(bound, call);
    const mapping = mapCreateOptions(
      template ?? merged,
      template === undefined ? undefined : merged,
    );
    const config = new ConnectionConfig(merged);
    emitIgnoredWarnings(mapping.ignored);
    try {
      const native = await NativeSandbox.create(mapping.native);
      return new cls(native, bound, config);
    } catch (error) {
      throw lifecycleImageError(error);
    }
  }

  protected static async connectFor(
    cls: typeof Sandbox,
    bound: ConnectionOpts,
    sandboxId: string,
    opts: SandboxConnectOpts,
  ): Promise<Sandbox> {
    validateOnResume(opts.onResume);
    const merged = mergeBoundOpts(bound, opts);
    const config = new ConnectionConfig(merged);
    const native = await NativeSandbox.connect(sandboxId, {
      ...nativeConnection({}, merged),
      timeoutMs: opts.timeoutMs,
      readyTimeoutMs: opts.readyTimeoutMs,
      reconnectTimeoutMs: opts.reconnectTimeoutMs,
    });
    return new cls(native, bound, config);
  }

  /**
   * Como `Sandbox.get_info(sandbox_id)` de Python: sobre un sandbox `RUNNING`,
   * un `Health` rellena `endAt` (el plazo lógico), `lifecycle`, los metadatos
   * y la vista del guest; un sandbox en otro estado no se sondea.
   */
  protected static async infoFor(
    bound: ConnectionOpts,
    sandboxId: string,
    opts: ConnectionOpts,
  ): Promise<SandboxInfo> {
    return infoFromNative(await NativeSandbox.probedInfo(sandboxId, nativeConnection(bound, opts)));
  }

  protected static async isRunningFor(
    bound: ConnectionOpts,
    sandboxId: string,
    opts: ConnectionOpts,
  ): Promise<boolean> {
    const info = await NativeSandbox.getInfo(sandboxId, nativeConnection(bound, opts));
    return info.state === "RUNNING";
  }

  protected static async pauseFor(
    bound: ConnectionOpts,
    sandboxId: string,
    opts: SandboxPauseOpts,
  ): Promise<boolean> {
    validateKeepMemory(opts.keepMemory);
    return NativeSandbox.pause(sandboxId, { ...nativeConnection(bound, opts), wait: true });
  }

  protected static setTimeoutFor(
    bound: ConnectionOpts,
    sandboxId: string,
    timeoutMs: number,
    opts: ConnectionOpts,
  ): Promise<void> {
    return NativeSandbox.setTimeout(sandboxId, timeoutMs, nativeConnection(bound, opts));
  }

  protected static killFor(
    bound: ConnectionOpts,
    sandboxId: string,
    opts: ConnectionOpts,
  ): Promise<boolean> {
    return NativeSandbox.kill(sandboxId, nativeConnection(bound, opts));
  }

  protected static async metricsFor(
    bound: ConnectionOpts,
    sandboxId: string,
    opts: SandboxMetricsOpts,
  ): Promise<SandboxMetrics[]> {
    try {
      const history = await NativeSandbox.getMetricsHistory(sandboxId, {
        ...nativeConnection(bound, opts),
        start: opts.start,
        end: opts.end,
      });
      return history.map(metricsFromNative);
    } catch (error) {
      throw historyImageError("Sandbox.getMetrics(sandboxId)", error);
    }
  }

  protected static listFor(bound: ConnectionOpts, opts: SandboxListOpts): SandboxPaginator {
    const merged = mergeBoundOpts(bound, opts);
    emitIgnoredWarnings(splitConnectionOpts(merged).ignored);
    return new SandboxPaginator(NativeSandbox.paginate(mapListOptions(merged)));
  }

  protected static async updateNetworkFor(
    bound: ConnectionOpts,
    sandboxId: string,
    network: SandboxNetworkUpdate,
    opts: ConnectionOpts,
  ): Promise<void> {
    const { policy, allowInternetAccess } = mapNetworkUpdate(network);
    await NativeSandbox.updateNetwork(sandboxId, policy, {
      ...nativeConnection(bound, opts),
      allowInternetAccess,
    });
  }

  // ------------------------------------------------------------- properties

  get sandboxId(): string {
    return this.native.sandboxId;
  }

  /** El hostname del endpoint del MicroVM. */
  get sandboxDomain(): string {
    return this.native.endpoint;
  }

  /**
   * El JWE del proxy que el SDK tiene para el puerto 8080 (`rayd`). Es una
   * credencial al portador para el endpoint, válida 60 min como mucho y
   * renovada a los 45: no la registres.
   */
  get trafficAccessToken(): string | undefined {
    return this.native.currentProxyToken(ENVD_PORT);
  }

  /**
   * El hostname del endpoint, síncrono como en E2B, tras validar el puerto
   * (9000 está prohibido). Cada petición necesita además `getHostHeaders(port)`.
   */
  getHost(port: number): string {
    validateHostPort(port);
    return this.native.endpoint;
  }

  /** `x-aws-proxy-auth` y `x-aws-proxy-port` para `port`, con el JWE acuñado si hace falta. */
  async getHostHeaders(port: number): Promise<Record<string, string>> {
    const host = await this.native.getHost(port);
    return { ...host.headers };
  }

  // -------------------------------------------------------------- lifecycle

  async kill(opts: ConnectionOpts = {}): Promise<boolean> {
    this.#connection(opts).signal?.throwIfAborted();
    return this.native.kill();
  }

  /**
   * `get-microvm` más el `Health` del nativo y, si está `RUNNING`, la política
   * de egress del guest (`GetNetwork`; una imagen sin ella deja `network`
   * vacío y `allowInternetAccess` sale de los conectores de egress). Otro
   * fallo de `GetNetwork` se propaga, como en Python.
   */
  async getInfo(opts: ConnectionOpts = {}): Promise<SandboxInfo> {
    const connection = this.#connection(opts);
    connection.signal?.throwIfAborted();
    const info = await this.native.getInfo();
    const guest = await this.#guestNetwork(info, connection);
    return infoFromNative(info, guest);
  }

  /** Un `Health` acotado por `requestTimeoutMs`. */
  isRunning(opts: ConnectionOpts = {}): Promise<boolean> {
    const connection = this.#connection(opts);
    return this.native.isRunning({
      requestTimeoutMs: connection.requestTimeoutMs,
      signal: connection.signal,
    });
  }

  /** Fija el plazo lógico en ahora + `timeoutMs` (puede acortarlo). */
  setTimeout(timeoutMs: number, opts: ConnectionOpts = {}): Promise<void> {
    const connection = this.#connection(opts);
    return this.native.setTimeout(timeoutMs, {
      requestTimeoutMs: connection.requestTimeoutMs,
      signal: connection.signal,
    });
  }

  async pause(opts: SandboxPauseOpts = {}): Promise<boolean> {
    validateKeepMemory(opts.keepMemory);
    this.#connection(opts).signal?.throwIfAborted();
    return this.native.pause();
  }

  betaPause(opts: SandboxPauseOpts = {}): Promise<boolean> {
    return this.pause(opts);
  }

  /**
   * Reanuda este mismo handle si está pausado y, con `timeoutMs`, alarga el
   * plazo a al menos ahora + `timeoutMs`. Devuelve `this`.
   */
  async connect(opts: SandboxInstanceConnectOpts = {}): Promise<this> {
    validateOnResume(opts.onResume);
    const connection = this.#connection(opts);
    await this.native.connect({
      timeoutMs: opts.timeoutMs,
      requestTimeoutMs: connection.requestTimeoutMs,
      signal: connection.signal,
    });
    return this;
  }

  /**
   * La serie de `MetricsHistory` en orden ascendente. Sin `start`/`end`, una
   * serie vacía o una imagen anterior a M9 devuelven la instantánea actual.
   */
  async getMetrics(opts: SandboxMetricsOpts = {}): Promise<SandboxMetrics[]> {
    const connection = this.#connection(opts);
    const unbounded = opts.start === undefined && opts.end === undefined;
    const { requestTimeoutMs, signal } = connection;
    let history: SandboxMetrics[];
    try {
      const native = await this.native.getMetricsHistory({
        start: opts.start,
        end: opts.end,
        requestTimeoutMs,
        signal,
      });
      history = native.map(metricsFromNative);
    } catch (error) {
      if (!isHistoryUnavailable(error)) {
        throw error;
      }
      if (!unbounded) {
        throw historyImageError("getMetrics({ start, end })", error);
      }
      history = [];
    }
    if (history.length > 0 || !unbounded) {
      return history;
    }
    return [metricsFromNative(await this.native.getMetrics({ requestTimeoutMs, signal }))];
  }

  // -------------------------------------------------------------- transfers

  /** La URL prefirmada de S3 para subir a `path` (`useSignatureExpiration` en segundos). */
  async uploadUrl(path?: string, opts: SandboxUrlOpts = {}): Promise<string> {
    if (path === undefined) {
      throw new InvalidArgumentError(UPLOAD_URL_PATH_MESSAGE);
    }
    return this.native.uploadUrl(path, opts);
  }

  downloadUrl(path: string, opts: SandboxUrlOpts = {}): Promise<string> {
    return this.native.downloadUrl(path, opts);
  }

  // ---------------------------------------------------------------- network

  async updateNetwork(network: SandboxNetworkUpdate, opts: ConnectionOpts = {}): Promise<void> {
    const { policy, allowInternetAccess } = mapNetworkUpdate(network);
    const connection = this.#connection(opts);
    await this.native.updateNetwork(policy, {
      allowInternetAccess,
      requestTimeoutMs: connection.requestTimeoutMs,
      signal: connection.signal,
    });
  }

  // ------------------------------------------------------------------- code

  /**
   * `runCode` de E2B: `language` `python`, `bash`, `javascript` o
   * `typescript` (alias `js`/`ts`; ausente es Python) viaja al nativo, que
   * decide en el agente si la imagen lo tiene. En una imagen sin el kernel,
   * el `Unimplemented` del agente es `UnimplementedError` con el error
   * nativo en `cause`; `r`, `java` y el resto son `UnimplementedError` sin
   * tocar el agente. Cualquier otro error nativo se propaga tal cual.
   */
  async runCode(code: string, opts: RunCodeOptions = {}): Promise<Execution> {
    const language = normalizedLanguageOrUnimplemented(opts.language, "runCode");
    try {
      return await this.native.runCode(code, { ...opts, language });
    } catch (error) {
      throw unimplementedLanguage(error, "runCode", opts.language) ?? error;
    }
  }

  /** `createCodeContext` de E2B con el mismo contrato de `language` que `runCode`. */
  async createCodeContext(opts: CreateContextOptions = {}): Promise<CodeContext> {
    const language = normalizedLanguageOrUnimplemented(opts.language, "createCodeContext");
    try {
      return await this.native.createCodeContext({ ...opts, language });
    } catch (error) {
      throw unimplementedLanguage(error, "createCodeContext", opts.language) ?? error;
    }
  }

  listCodeContexts(opts: RequestOptions = {}): Promise<CodeContext[]> {
    return this.native.listCodeContexts(opts);
  }

  removeCodeContext(context: ContextLike, opts: RequestOptions = {}): Promise<void> {
    return this.native.removeCodeContext(context, opts);
  }

  restartCodeContext(context: ContextLike, opts: RequestOptions = {}): Promise<void> {
    return this.native.restartCodeContext(context, opts);
  }

  // ---------------------------------------------------------- unimplemented

  fork(..._args: unknown[]): Promise<never> {
    return rejectUnimplemented("fork");
  }

  createSnapshot(..._args: unknown[]): Promise<never> {
    return rejectUnimplemented("createSnapshot");
  }

  listSnapshots(..._args: unknown[]): never {
    throw unimplemented("listSnapshots");
  }

  getMcpUrl(..._args: unknown[]): never {
    throw unimplemented("getMcpUrl");
  }

  getMcpToken(..._args: unknown[]): Promise<never> {
    return rejectUnimplemented("getMcpToken");
  }

  // ------------------------------------------------------------------ misc

  /** Cierra las sesiones con el agente sin tocar el MicroVM (extensión de Rayito). */
  close(): void {
    this.native.close();
  }

  async [Symbol.asyncDispose](): Promise<void> {
    await this.kill();
  }

  toString(): string {
    return `Sandbox(${this.sandboxId})`;
  }

  #connection(opts: ConnectionOpts): NativeConnection {
    return nativeConnection(this.#bound, opts);
  }

  /** Sólo un `UnimplementedError` (imagen sin `GetNetwork`) cuenta como "leído sin política"; el resto se propaga. */
  async #guestNetwork(
    info: NativeSandboxInfo,
    connection: NativeConnection,
  ): Promise<{ readonly network: NetworkState | undefined; readonly networkRead: boolean }> {
    if (info.state !== "RUNNING") {
      return { network: undefined, networkRead: false };
    }
    try {
      const network = await this.native.getNetwork({
        requestTimeoutMs: connection.requestTimeoutMs,
        signal: connection.signal,
      });
      return { network, networkRead: true };
    } catch (error) {
      if (error instanceof UnimplementedError) {
        return { network: undefined, networkRead: true };
      }
      throw error;
    }
  }
}

/**
 * La subclase que devuelve `new E2B(opts).Sandbox`: cada estático fusiona
 * `opts` con los de la llamada (gana la llamada salvo `undefined`) y las
 * instancias guardan el enlace para sus propias llamadas.
 */
export function bindSandbox(opts: ConnectionOpts): typeof Sandbox {
  const bound: ConnectionOpts = Object.freeze({ ...opts });
  return class BoundSandbox extends Sandbox {
    static override create(
      templateOrOpts?: string | SandboxOpts,
      createOpts?: SandboxOpts,
    ): Promise<Sandbox> {
      return Sandbox.createFor(BoundSandbox, bound, templateOrOpts, createOpts);
    }

    static override connect(sandboxId: string, connectOpts: SandboxConnectOpts = {}) {
      return Sandbox.connectFor(BoundSandbox, bound, sandboxId, connectOpts);
    }

    static override kill(sandboxId: string, callOpts: ConnectionOpts = {}) {
      return Sandbox.killFor(bound, sandboxId, callOpts);
    }

    static override getInfo(sandboxId: string, callOpts: ConnectionOpts = {}) {
      return Sandbox.infoFor(bound, sandboxId, callOpts);
    }

    static override getFullInfo(sandboxId: string, callOpts: ConnectionOpts = {}) {
      return Sandbox.infoFor(bound, sandboxId, callOpts);
    }

    static override isRunning(sandboxId: string, callOpts: ConnectionOpts = {}) {
      return Sandbox.isRunningFor(bound, sandboxId, callOpts);
    }

    static override pause(sandboxId: string, pauseOpts: SandboxPauseOpts = {}) {
      return Sandbox.pauseFor(bound, sandboxId, pauseOpts);
    }

    static override betaPause(sandboxId: string, pauseOpts: SandboxPauseOpts = {}) {
      return Sandbox.pauseFor(bound, sandboxId, pauseOpts);
    }

    static override setTimeout(
      sandboxId: string,
      timeoutMs: number,
      callOpts: ConnectionOpts = {},
    ) {
      return Sandbox.setTimeoutFor(bound, sandboxId, timeoutMs, callOpts);
    }

    static override getMetrics(sandboxId: string, metricsOpts: SandboxMetricsOpts = {}) {
      return Sandbox.metricsFor(bound, sandboxId, metricsOpts);
    }

    static override list(listOpts: SandboxListOpts = {}) {
      return Sandbox.listFor(bound, listOpts);
    }

    static override updateNetwork(
      sandboxId: string,
      network: SandboxNetworkUpdate,
      callOpts: ConnectionOpts = {},
    ) {
      return Sandbox.updateNetworkFor(bound, sandboxId, network, callOpts);
    }
  };
}
