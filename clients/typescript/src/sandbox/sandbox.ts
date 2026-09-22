/**
 * `Sandbox`: un MicroVM con `rayd` dentro. Se crea con `await Sandbox.create()`
 * o `await Sandbox.connect(id)`; `await using` lo mata al salir del bloque.
 * La superficie es la del SDK Python en camelCase y milisegundos; toda la
 * mecánica (transportes, readiness, reconexión) vive en `SandboxCore`.
 */

import { create } from "@bufbuild/protobuf";
import {
  type CommandSender,
  type ControlPlane,
  LambdaMicrovmsControlPlane,
  PortSpec,
  sharedControlPlane,
} from "../aws/control-plane.js";
import {
  errorMessage,
  InvalidArgumentError,
  NotFoundError,
  SandboxNotFoundError,
  SandboxNotReadyError,
  TimeoutError,
} from "../errors.js";
import { HealthRequestSchema, MetricsRequestSchema } from "../gen/rayito/v1/health_pb.js";
import { DEFAULT_PORT, TERMINAL_STATES } from "../limits.js";
import type { Logger } from "../logger.js";
import {
  type CodeContext,
  type Execution,
  HostAccess,
  type IdlePolicyInput,
  type SandboxHealth,
  type SandboxInfo,
  type SandboxListItem,
  type SandboxMetrics,
} from "../models.js";
import { rejectLaunchOptionsWithPool } from "../pool/core.js";
import type { SandboxPool } from "../pool/pool.js";
import { TokenRefresher, TokenStore } from "../transport/tokens.js";
import { resolveTransportSettings, type TransportSettings } from "../transport/transport.js";
import {
  CodeClient,
  type ContextLike,
  type CreateContextOptions,
  type RunCodeOptions,
} from "./code.js";
import { Commands, metricsFromProto, type RequestOptions } from "./commands.js";
import { SandboxCore } from "./core.js";
import { Filesystem } from "./filesystem.js";
import {
  buildLaunchPlan,
  DEFAULT_READY_TIMEOUT_MS,
  DEFAULT_RECONNECT_TIMEOUT_MS,
  DEFAULT_REQUEST_TIMEOUT_MS,
  type LoggingOption,
  type PortLike,
  requireAccessToken,
  resolveTemplate,
  validateHostPort,
  validateSandboxId,
} from "./launch.js";
import {
  bindPersist,
  type CheckpointFilesOptions,
  type CheckpointResult,
  type LaunchOptions,
  PersistenceClient,
  type ReincarnateOptions,
  type RestoreFilesOptions,
  type RestoreResult,
  reincarnateRequiresCreateError,
  reincarnateRequiresPersistError,
  requireNamedPersist,
  requireRoleForPersist,
  type S3Prefix,
  shouldAutoRestore,
  validatePersistTimeoutMs,
  withReincarnateNote,
} from "./persistence.js";
import { Pty } from "./pty.js";
import {
  alreadySuspended,
  formatSeconds,
  healthFromProto,
  ReadinessPoll,
  terminalStateError,
} from "./readiness.js";

export const STATE_POLL_INTERVAL_MS = 500;

export interface ControlPlaneOptions {
  readonly region?: string | undefined;
  readonly controlPlane?: ControlPlane | undefined;
  /** Un `LambdaMicrovmsClient` propio: construye un plano privado con sus propios buckets. */
  readonly client?: CommandSender | undefined;
}

export interface SandboxConnectOptions extends ControlPlaneOptions {
  readonly accessToken?: string | undefined;
  readonly readyTimeoutMs?: number | undefined;
  readonly requestTimeoutMs?: number | undefined;
  readonly reconnectTimeoutMs?: number | undefined;
  readonly transport?: Partial<TransportSettings> | undefined;
  readonly logger?: Logger | undefined;
  /**
   * Enlaza el `HOME` a `s3://bucket/prefix/name/`. En `create` requiere
   * `executionRoleArn` y, con `name`, restaura el checkpoint que haya
   * (`sandbox.lastRestore`); sin `name` lo fija al `sandboxId`. En `connect`
   * sólo enlaza (necesita `name`) y no restaura nada.
   */
  readonly persist?: S3Prefix | undefined;
}

export interface SandboxCreateOptions extends SandboxConnectOptions {
  /** ARN o nombre de la imagen; por defecto `RAYITO_TEMPLATE`. */
  readonly template?: string | undefined;
  readonly templateVersion?: string | undefined;
  /** Vida máxima del MicroVM en ms (3 600 000 por defecto; máximo 28 800 000). */
  readonly timeoutMs?: number | undefined;
  /** `null` desactiva el auto-suspend; por defecto `{ maxIdleSeconds: 300, autoResume: true }`. */
  readonly idle?: IdlePolicyInput | null | undefined;
  readonly envs?: Readonly<Record<string, string>> | undefined;
  /** Etiquetas no secretas que viajan en el `runHookPayload` y vuelven en `Health`. */
  readonly metadata?: Readonly<Record<string, string>> | undefined;
  /** Segundos de CPU por proceso (`RLIMIT_CPU`, `1..=28800`), nunca de pared. */
  readonly cpuTimeLimit?: number | undefined;
  readonly executionRoleArn?: string | undefined;
  readonly allowedPorts?: readonly PortLike[] | undefined;
  readonly ingress?: readonly string[] | undefined;
  readonly egress?: readonly string[] | undefined;
  readonly logging?: LoggingOption | undefined;
  readonly keepOnFailure?: boolean | undefined;
  /** Deadline del restore automático de `persist` y de `reincarnate()` (600 000 ms). */
  readonly persistTimeoutMs?: number | undefined;
  /**
   * Azúcar de `pool.take()`: el sandbox sale de una plaza suspendida del
   * `SandboxPool` (ya arrancado) con la configuración de su `PoolConfig`;
   * cualquier otra opción de lanzamiento o de plano es `InvalidArgumentError`.
   * Sólo pasan `readyTimeoutMs`, `requestTimeoutMs`, `reconnectTimeoutMs` y `logger`.
   */
  readonly pool?: SandboxPool | undefined;
}

/** Los objetos de `create()` que `reincarnate()` reutiliza tal cual (no viajan en `LaunchOptions`). */
interface LaunchContext {
  readonly controlPlane: ControlPlane;
  readonly transport: Partial<TransportSettings> | undefined;
  readonly logger: Logger | undefined;
}

export interface SandboxListOptions extends ControlPlaneOptions {
  readonly template?: string | undefined;
  readonly templateVersion?: string | undefined;
  readonly states?: readonly string[] | undefined;
}

export interface PauseOptions {
  readonly wait?: boolean | undefined;
}

export interface StaticPauseOptions extends ControlPlaneOptions, PauseOptions {
  readonly readyTimeoutMs?: number | undefined;
}

export function resolveControlPlane(options: ControlPlaneOptions): ControlPlane {
  if (options.controlPlane !== undefined) {
    return options.controlPlane;
  }
  if (options.client !== undefined) {
    const region = options.region ?? process.env.AWS_REGION ?? process.env.AWS_DEFAULT_REGION ?? "";
    return new LambdaMicrovmsControlPlane({ client: options.client, region });
  }
  return sharedControlPlane(options.region);
}

/** Limpieza best-effort de un MicroVM que no llegó a estar listo: el error original es el que importa. */
async function terminateQuietly(
  controlPlane: ControlPlane,
  sandboxId: string,
  logger: Logger | undefined,
): Promise<void> {
  try {
    await controlPlane.terminateMicrovm(sandboxId);
  } catch (error) {
    logger?.warn?.("no se pudo terminar el sandbox tras un fallo de arranque", {
      sandboxId,
      reason: errorMessage(error),
    });
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** Sondea `get-microvm` hasta `wanted`. Un estado terminal es fatal. */
export async function waitForState(
  controlPlane: ControlPlane,
  sandboxId: string,
  wanted: string,
  timeoutMs: number,
): Promise<SandboxInfo> {
  const deadline = performance.now() + timeoutMs;
  while (true) {
    const info = await controlPlane.getMicrovm(sandboxId);
    if (info.state === wanted) {
      return info;
    }
    if (TERMINAL_STATES.has(info.state)) {
      throw terminalStateError(info);
    }
    if (performance.now() >= deadline) {
      throw new TimeoutError(
        `el sandbox ${sandboxId} sigue ${info.state} tras ${formatSeconds(timeoutMs)} s esperando ${wanted}`,
      );
    }
    await sleep(STATE_POLL_INTERVAL_MS);
  }
}

/** Lo que `Sandbox.#open` necesita; el pool lo usa a través de `Sandbox.openWith`. */
export interface SandboxOpenOptions {
  readonly accessToken: string;
  readonly controlPlane: ControlPlane;
  readonly transport: TransportSettings;
  readonly proxyPorts: readonly PortSpec[];
  readonly requestTimeoutMs: number;
  readonly readyTimeoutMs: number;
  readonly reconnectTimeoutMs: number;
  readonly terminateOnFailure: boolean;
  readonly logger: Logger | undefined;
  readonly readiness?: typeof ReadinessPoll | undefined;
}

export class Sandbox implements AsyncDisposable {
  readonly #core: SandboxCore;
  readonly commands: Commands;
  readonly files: Filesystem;
  readonly pty: Pty;
  readonly #code: CodeClient;
  readonly #persistence: PersistenceClient;
  #persist: S3Prefix | undefined;
  #lastRestore: RestoreResult | undefined;
  #launchOptions: LaunchOptions | undefined;
  #launchContext: LaunchContext | undefined;

  private constructor(core: SandboxCore) {
    this.#core = core;
    this.commands = new Commands(core);
    this.files = new Filesystem(core);
    this.pty = new Pty(core, this.commands);
    this.#code = new CodeClient(core);
    this.#persistence = new PersistenceClient(core);
  }

  // ------------------------------------------------------------------ create

  static async create(options: SandboxCreateOptions = {}): Promise<Sandbox> {
    requireRoleForPersist(options.persist, options.executionRoleArn);
    if (options.pool !== undefined && options.persist !== undefined) {
      throw new InvalidArgumentError(
        "create({ pool }) no admite persist: una plaza del pool no puede restaurar un home con nombre al tomarla",
      );
    }
    if (options.pool !== undefined) {
      rejectLaunchOptionsWithPool(options);
      return options.pool.take({
        readyTimeoutMs: options.readyTimeoutMs,
        requestTimeoutMs: options.requestTimeoutMs,
        reconnectTimeoutMs: options.reconnectTimeoutMs,
        logger: options.logger,
      });
    }
    const plane = resolveControlPlane(options);
    const imageArn = await plane.resolveTemplateArn(resolveTemplate(options.template));
    const plan = buildLaunchPlan({
      imageArn,
      region: plane.region,
      templateVersion: options.templateVersion,
      timeoutMs: options.timeoutMs,
      idle: options.idle,
      envs: options.envs,
      metadata: options.metadata,
      cpuTimeLimit: options.cpuTimeLimit,
      executionRoleArn: options.executionRoleArn,
      allowedPorts: options.allowedPorts,
      ingress: options.ingress,
      egress: options.egress,
      logging: options.logging,
      accessToken: options.accessToken,
    });
    const info = await plane.runMicrovm(plan.request);
    options.logger?.info?.("run-microvm aceptado", {
      sandboxId: info.sandboxId,
      state: info.state,
    });
    const sandbox = await Sandbox.#open(info, {
      accessToken: plan.accessToken,
      controlPlane: plane,
      transport: resolveTransportSettings(options.transport),
      proxyPorts: plan.proxyPorts,
      requestTimeoutMs: options.requestTimeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS,
      readyTimeoutMs: options.readyTimeoutMs ?? DEFAULT_READY_TIMEOUT_MS,
      reconnectTimeoutMs: options.reconnectTimeoutMs ?? DEFAULT_RECONNECT_TIMEOUT_MS,
      terminateOnFailure: !(options.keepOnFailure ?? false),
      logger: options.logger,
    });
    sandbox.#launchOptions = {
      template: imageArn,
      templateVersion: options.templateVersion,
      timeoutMs: options.timeoutMs,
      idle: options.idle,
      envs: options.envs,
      metadata: options.metadata,
      cpuTimeLimit: options.cpuTimeLimit,
      executionRoleArn: options.executionRoleArn,
      allowedPorts: options.allowedPorts,
      ingress: options.ingress,
      egress: options.egress,
      logging: options.logging,
      accessToken: options.accessToken,
      readyTimeoutMs: options.readyTimeoutMs,
      requestTimeoutMs: options.requestTimeoutMs,
      reconnectTimeoutMs: options.reconnectTimeoutMs,
      keepOnFailure: options.keepOnFailure,
    };
    sandbox.#launchContext = {
      controlPlane: plane,
      transport: options.transport,
      logger: options.logger,
    };
    if (options.persist !== undefined) {
      await sandbox.#bindAndRestore(
        options.persist,
        validatePersistTimeoutMs(options.persistTimeoutMs),
        !(options.keepOnFailure ?? false),
      );
    }
    return sandbox;
  }

  /** Se conecta a un sandbox existente; nunca lo termina si algo falla. */
  static async connect(sandboxId: string, options: SandboxConnectOptions = {}): Promise<Sandbox> {
    const token = requireAccessToken(options.accessToken);
    const bound = options.persist === undefined ? undefined : requireNamedPersist(options.persist);
    const plane = resolveControlPlane(options);
    const info = await plane.getMicrovm(validateSandboxId(sandboxId));
    if (TERMINAL_STATES.has(info.state)) {
      throw terminalStateError(info);
    }
    if (info.state === "SUSPENDED" && !(info.idle?.autoResume ?? false)) {
      await plane.resumeMicrovm(sandboxId);
    }
    const sandbox = await Sandbox.#open(info, {
      accessToken: token,
      controlPlane: plane,
      transport: resolveTransportSettings(options.transport),
      proxyPorts: [PortSpec.single(DEFAULT_PORT)],
      requestTimeoutMs: options.requestTimeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS,
      readyTimeoutMs: options.readyTimeoutMs ?? DEFAULT_READY_TIMEOUT_MS,
      reconnectTimeoutMs: options.reconnectTimeoutMs ?? DEFAULT_RECONNECT_TIMEOUT_MS,
      terminateOnFailure: false,
      logger: options.logger,
    });
    sandbox.#persist = bound;
    return sandbox;
  }

  /** Acceso interno para el pool (abre una plaza con su token y el calendario de toma); no forma parte de la API pública. */
  static openWith(info: SandboxInfo, options: SandboxOpenOptions): Promise<Sandbox> {
    return Sandbox.#open(info, options);
  }

  /**
   * Con `terminateOnFailure`, todo fallo previo al primer `agentReady` que no
   * sea `SandboxNotReadyError` termina el MicroVM (ese error ya decidió).
   */
  static async #open(info: SandboxInfo, options: SandboxOpenOptions): Promise<Sandbox> {
    const refresher = new TokenRefresher(
      new TokenStore(),
      (ports) => options.controlPlane.createAuthToken(info.sandboxId, ports),
      { logger: options.logger },
    );
    let sandbox: Sandbox | undefined;
    try {
      await refresher.mint(options.proxyPorts);
      sandbox = new Sandbox(
        new SandboxCore({
          info,
          accessToken: options.accessToken,
          controlPlane: options.controlPlane,
          transport: options.transport,
          refresher,
          requestTimeoutMs: options.requestTimeoutMs,
          readyTimeoutMs: options.readyTimeoutMs,
          reconnectTimeoutMs: options.reconnectTimeoutMs,
          logger: options.logger,
        }),
      );
      await sandbox.#core.waitUntilReady({
        terminateOnFailure: options.terminateOnFailure,
        readiness: options.readiness,
      });
    } catch (error) {
      sandbox?.close();
      if (options.terminateOnFailure && !(error instanceof SandboxNotReadyError)) {
        await terminateQuietly(options.controlPlane, info.sandboxId, options.logger);
      }
      throw error;
    }
    refresher.start();
    return sandbox;
  }

  static async *list(options: SandboxListOptions = {}): AsyncIterable<SandboxListItem> {
    const plane = resolveControlPlane(options);
    const imageArn =
      options.template === undefined ? undefined : await plane.resolveTemplateArn(options.template);
    yield* plane.listMicrovms({
      imageArn,
      imageVersion: options.templateVersion,
      states: options.states,
    });
  }

  static async kill(sandboxId: string, options: ControlPlaneOptions = {}): Promise<boolean> {
    return resolveControlPlane(options).terminateMicrovm(validateSandboxId(sandboxId));
  }

  static async getInfo(sandboxId: string, options: ControlPlaneOptions = {}): Promise<SandboxInfo> {
    return resolveControlPlane(options).getMicrovm(validateSandboxId(sandboxId));
  }

  static async pause(sandboxId: string, options: StaticPauseOptions = {}): Promise<boolean> {
    const plane = resolveControlPlane(options);
    const info = await plane.getMicrovm(validateSandboxId(sandboxId));
    if (alreadySuspended(info)) {
      return false;
    }
    const suspended = await plane.suspendMicrovm(sandboxId);
    if (suspended && (options.wait ?? true)) {
      await waitForState(
        plane,
        sandboxId,
        "SUSPENDED",
        options.readyTimeoutMs ?? DEFAULT_READY_TIMEOUT_MS,
      );
    }
    return suspended;
  }

  static async resume(sandboxId: string, options: StaticPauseOptions = {}): Promise<void> {
    const plane = resolveControlPlane(options);
    await plane.resumeMicrovm(validateSandboxId(sandboxId));
    if (options.wait ?? true) {
      await waitForState(
        plane,
        sandboxId,
        "RUNNING",
        options.readyTimeoutMs ?? DEFAULT_READY_TIMEOUT_MS,
      );
    }
  }

  // -------------------------------------------------------------- properties

  get sandboxId(): string {
    return this.#core.sandboxId;
  }

  get accessToken(): string {
    return this.#core.accessToken;
  }

  get endpoint(): string {
    return this.#core.info.endpoint;
  }

  get endpointUrl(): string {
    return this.#core.info.endpointUrl;
  }

  get info(): SandboxInfo {
    return this.#core.info;
  }

  /** La `SandboxInfo` con la que se abrió el handle (`run-microvm` o `connect`), nunca refrescada. */
  get launchInfo(): SandboxInfo {
    return this.#core.launchInfo;
  }

  get region(): string {
    return this.#core.controlPlane.region;
  }

  get resumeGeneration(): number {
    return this.#core.resumeGeneration;
  }

  /** El `S3Prefix` (con `name`) enlazado por `create({ persist })` o `connect(id, { persist })`. */
  get persist(): S3Prefix | undefined {
    return this.#persist;
  }

  /** El resultado del restore automático de `create({ persist })`; `undefined` en la primera vida del `name`. */
  get lastRestore(): RestoreResult | undefined {
    return this.#lastRestore;
  }

  // --------------------------------------------------------------- lifecycle

  /** `terminate-microvm` y `close()`, también si la llamada falla. */
  async kill(): Promise<boolean> {
    try {
      return await this.#core.controlPlane.terminateMicrovm(this.sandboxId);
    } finally {
      this.close();
    }
  }

  async getInfo(): Promise<SandboxInfo> {
    this.#core.info = await this.#core.controlPlane.getMicrovm(this.sandboxId);
    return this.#core.info;
  }

  /**
   * `false` si ya estaba `SUSPENDING|SUSPENDED`. Mientras la pausa esté
   * pendiente ningún stream en curso de este `Sandbox` sondea `Health`.
   */
  async pause(options: PauseOptions = {}): Promise<boolean> {
    this.#core.info = await this.#core.controlPlane.getMicrovm(this.sandboxId);
    if (alreadySuspended(this.#core.info)) {
      return false;
    }
    const suspended = await this.#core.suspendMarkingPaused();
    if (suspended && (options.wait ?? true)) {
      this.#core.info = await waitForState(
        this.#core.controlPlane,
        this.sandboxId,
        "SUSPENDED",
        this.#core.readyTimeoutMs,
      );
    }
    return suspended;
  }

  /** `resume-microvm` (un conflicto no es error), reacuña los JWE y, con `wait`, espera a `Health`. */
  async resume(options: PauseOptions = {}): Promise<void> {
    this.#core.paused = false;
    await this.#core.controlPlane.resumeMicrovm(this.sandboxId);
    await this.#core.refresher.refreshAll();
    if (options.wait ?? true) {
      await this.#core.waitUntilReady({ terminateOnFailure: false });
    }
  }

  async isRunning(): Promise<boolean> {
    const response = await this.#core.probeHealth(
      Math.min(ReadinessPoll.maxRpcTimeoutMs, this.#core.requestTimeoutMs),
    );
    return response?.agentReady ?? false;
  }

  async getHealth(options: RequestOptions = {}): Promise<SandboxHealth> {
    const timeoutMs = this.#core.resolveRequestTimeout(options.requestTimeoutMs);
    const response = await this.#core.translatedUnary(() =>
      this.#core.clients.health.health(create(HealthRequestSchema, {}), { timeoutMs }),
    );
    this.#core.recordHealth(response);
    return healthFromProto(response);
  }

  /** Un `HostAccess` con el JWE que cubre `port` (acuñado si hace falta); 9000 está prohibido (ADR-006). */
  async getHost(port: number): Promise<HostAccess> {
    const validated = validateHostPort(port);
    await this.#core.refresher.ensure(validated);
    return new HostAccess(this.endpoint, validated, () => this.#core.currentJwe(validated));
  }

  async getMetrics(options: RequestOptions = {}): Promise<SandboxMetrics> {
    const timeoutMs = this.#core.resolveRequestTimeout(options.requestTimeoutMs);
    const response = await this.#core.translatedUnary(() =>
      this.#core.clients.health.metrics(create(MetricsRequestSchema, {}), { timeoutMs }),
    );
    return metricsFromProto(response);
  }

  // ------------------------------------------------------------ persistence

  /**
   * `FilesystemService.Checkpoint`: `rayd` empaqueta el `HOME` (tar.gz, sin
   * `.cache`, `__pycache__`, `.ipynb_checkpoints` ni el runtime de Jupyter) y
   * lo sube como root con el execution role a `home.tar.gz` + `manifest.json`
   * bajo `target` (por defecto `persist`). Un solo checkpoint o restore a la
   * vez por sandbox (`PersistenceError` `failed_precondition`).
   */
  checkpointFiles(options: CheckpointFilesOptions = {}): Promise<CheckpointResult> {
    return this.#persistence.checkpoint(this.#persist, options);
  }

  /**
   * `FilesystemService.Restore`: extrae el checkpoint de `source` (por defecto
   * `persist`) sobre el `HOME`; `NotFoundError` si no hay ninguno. Un fallo a
   * mitad deja el `HOME` parcialmente restaurado: `kill()` + `create({ persist })`.
   */
  restoreFiles(options: RestoreFilesOptions = {}): Promise<RestoreResult> {
    return this.#persistence.restore(this.#persist, options);
  }

  /**
   * La respuesta a `setTimeout`: `checkpointFiles()` → `create({ persist })` con
   * las mismas opciones (que restaura) → `kill()` de este sandbox. El nuevo
   * tiene 8 h frescas, otro `sandboxId` y otro token salvo que el original
   * fuera explícito; kernels, procesos y PTY no sobreviven (ADR-007). Si el
   * `create()` falla, este sandbox sigue vivo y se relanza el mismo error
   * (con sus campos tipados: `code`, `state`...) con la `uri` del checkpoint
   * completo añadida a su `message`.
   */
  async reincarnate(options: ReincarnateOptions = {}): Promise<Sandbox> {
    const launch = this.#launchOptions;
    const context = this.#launchContext;
    if (launch === undefined || context === undefined) {
      throw reincarnateRequiresCreateError();
    }
    const persist = this.#persist;
    if (persist === undefined) {
      throw reincarnateRequiresPersistError();
    }
    const persistTimeoutMs = validatePersistTimeoutMs(options.persistTimeoutMs);
    await this.checkpointFiles({ exclude: options.exclude, timeoutMs: persistTimeoutMs });
    let successor: Sandbox;
    try {
      successor = await Sandbox.create({
        ...launch,
        controlPlane: context.controlPlane,
        transport: context.transport,
        logger: context.logger,
        persist,
        persistTimeoutMs,
      });
    } catch (error) {
      throw withReincarnateNote(error, persist.uri);
    }
    try {
      await this.kill();
    } catch (error) {
      if (!(error instanceof SandboxNotFoundError)) {
        throw error;
      }
      this.#core.logger?.info?.("el sandbox ya no existía al reencarnar", {
        sandboxId: this.sandboxId,
      });
    }
    return successor;
  }

  /** Reglas 2 y 3 de D10: enlaza el prefijo y, con `name`, restaura lo que haya. */
  async #bindAndRestore(
    persist: S3Prefix,
    persistTimeoutMs: number,
    terminateOnFailure: boolean,
  ): Promise<void> {
    this.#persist = bindPersist(persist, this.sandboxId);
    if (!shouldAutoRestore(persist)) {
      return;
    }
    try {
      this.#lastRestore = await this.restoreFiles({ timeoutMs: persistTimeoutMs });
    } catch (error) {
      if (error instanceof NotFoundError) {
        this.#core.logger?.info?.("sin checkpoint bajo el prefijo", {
          sandboxId: this.sandboxId,
          uri: this.#persist.uri,
        });
        this.#lastRestore = undefined;
        return;
      }
      this.close();
      if (terminateOnFailure) {
        await terminateQuietly(this.#core.controlPlane, this.sandboxId, this.#core.logger);
      }
      throw error;
    }
  }

  /** Cierra las dos sesiones HTTP/2, los watches y los streams sin tocar el VM; idempotente. */
  close(): void {
    this.#core.close();
  }

  async [Symbol.asyncDispose](): Promise<void> {
    await this.kill();
  }

  // -------------------------------------------------------------------- code

  runCode(code: string, options: RunCodeOptions = {}): Promise<Execution> {
    return this.#code.runCode(code, options);
  }

  createCodeContext(options: CreateContextOptions = {}): Promise<CodeContext> {
    return this.#code.createContext(options);
  }

  listCodeContexts(options: RequestOptions = {}): Promise<CodeContext[]> {
    return this.#code.listContexts(options);
  }

  removeCodeContext(context: ContextLike, options: RequestOptions = {}): Promise<void> {
    return this.#code.removeContext(context, options);
  }

  restartCodeContext(context: ContextLike, options: RequestOptions = {}): Promise<void> {
    return this.#code.restartContext(context, options);
  }

  toString(): string {
    return `Sandbox(${this.sandboxId}, ${this.#core.info.state})`;
  }

  /** Acceso interno para los tests del paquete; no forma parte de la API pública. */
  static coreOf(sandbox: Sandbox): SandboxCore {
    return sandbox.#core;
  }
}
