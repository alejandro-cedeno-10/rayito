/**
 * `SandboxPool`: N MicroVMs aparcados (`SUSPENDED`) que `take()` entrega en
 * menos de un segundo (ADR-008, `m7-suspended-pool`); la misma semántica que
 * el `SandboxPool` del SDK Python.
 *
 * Un bucle de relleno (una promesa gobernada por un `AbortController`)
 * mantiene `ready + warming == size`: cada plaza se calienta con el
 * `create()` normal (token fresco por plaza), se aparca con
 * `pause({ wait: true })` y se cierra el handle: el pool guarda **datos**
 * (`SlotRecord` en un `PoolBackend`), nunca sesiones ni JWE. `take()` saca la
 * plaza más vieja con vida suficiente, borra su registro antes de tocar la
 * red, hace `resumeMicrovm` explícito y abre el handle con el token de la
 * plaza; sin plaza (o con la plaza muerta) cae a un `create()` normal. Un
 * sweeper recicla las plazas que se acercan al muro de 8 h y reconcilia con
 * `listMicrovms`. Los temporizadores del sweeper y del backoff van con
 * `unref()`: un pool ocioso no mantiene vivo el proceso; un calentamiento en
 * vuelo (un fetch pendiente) sí. Todas las llamadas a AWS pasan por el plano
 * de control compartido y sus token buckets.
 *
 * Custodia del secreto (SECURITY.md T14): el sha256 del token viaja en el
 * `runHookPayload` y `/run` se acepta una vez por arranque, así que el token
 * que abre una plaza es el que acuñó el pool durante toda la vida del VM; no
 * hay rotación en `take()`. Un pool que nunca se cierra deja `size` VMs
 * suspendidos que se terminan solos en su `timeoutMs` (política de idle).
 */

import {
  type AwsClientSettings,
  awsClientSettingsOf,
  type ControlPlane,
  type ControlPlaneCallOptions,
  type LaunchRequest,
  type ListMicrovmsOptions,
  type ListMicrovmsPageOptions,
  PortSpec,
} from "../aws/control-plane.js";
import {
  errorMessage,
  InvalidArgumentError,
  PoolClosedError,
  SandboxNotFoundError,
  SandboxStateError,
} from "../errors.js";
import { DEFAULT_PORT, TERMINAL_STATES } from "../limits.js";
import type { Logger } from "../logger.js";
import type { MicrovmListPage, SandboxInfo, SandboxListItem } from "../models.js";
import { generateAccessToken } from "../payload.js";
import {
  DEFAULT_READY_TIMEOUT_MS,
  DEFAULT_RECONNECT_TIMEOUT_MS,
  DEFAULT_REQUEST_TIMEOUT_MS,
  resolveTemplate,
} from "../sandbox/launch.js";
import { type ControlPlaneOptions, resolveControlPlane, Sandbox } from "../sandbox/sandbox.js";
import { resolveTransportSettings, type TransportSettings } from "../transport/transport.js";
import { InMemoryPoolBackend, type PoolBackend } from "./backend.js";
import {
  launchOptions,
  type PoolConfig,
  type ResolvedPoolConfig,
  validatePoolConfig,
} from "./config.js";
import {
  FillBackoff,
  newCounters,
  type PoolCounters,
  type PoolStats,
  pickReadySlot,
  reconcileAction,
  recordFromInfo,
  type SlotRecord,
  sandboxInfoFromRecord,
  slotIsStale,
  statsFromRecords,
  TakePoll,
} from "./core.js";

export const CLOSE_JOIN_MS = 30_000;
export const SETTLE_CELL = "pass";
export const SETTLE_GRACE_MS = 300;
export const SETTLE_ATTEMPTS = 3;

export interface SandboxPoolOptions extends ControlPlaneOptions {
  readonly backend?: PoolBackend | undefined;
  readonly transport?: Partial<TransportSettings> | undefined;
  readonly logger?: Logger | undefined;
  /** Reloj monótono en ms (tests). */
  readonly monotonic?: (() => number) | undefined;
  /** Reloj de pared (tests). */
  readonly now?: (() => Date) | undefined;
  /** Espera del backoff del relleno; por defecto un `setTimeout` sin referencia que `close()` interrumpe. */
  readonly sleep?: ((ms: number) => Promise<void>) | undefined;
  readonly random?: (() => number) | undefined;
}

export interface TakeOptions {
  /** Cuánto esperar (ms) a que el relleno aparque una plaza antes de caer a `create()`; por defecto 0. */
  readonly waitMs?: number | undefined;
  readonly readyTimeoutMs?: number | undefined;
  readonly requestTimeoutMs?: number | undefined;
  readonly reconnectTimeoutMs?: number | undefined;
  readonly logger?: Logger | undefined;
}

export interface CloseOptions {
  /** Terminar las plazas `ready` (por defecto); `false` sólo con un backend persistente. */
  readonly drain?: boolean | undefined;
}

class PoolStopping extends Error {}

/** Un `ControlPlane` que delega todo y avisa con la `SandboxInfo` de cada `run-microvm` aceptado. */
export class LaunchObserver implements ControlPlane {
  readonly #plane: ControlPlane;
  readonly #onLaunch: (info: SandboxInfo) => Promise<void>;

  constructor(plane: ControlPlane, onLaunch: (info: SandboxInfo) => Promise<void>) {
    this.#plane = plane;
    this.#onLaunch = onLaunch;
  }

  get region(): string {
    return this.#plane.region;
  }

  get awsClientSettings(): AwsClientSettings {
    return awsClientSettingsOf(this.#plane);
  }

  resolveTemplateArn(template: string, options?: ControlPlaneCallOptions): Promise<string> {
    return this.#plane.resolveTemplateArn(template, options);
  }

  async runMicrovm(request: LaunchRequest): Promise<SandboxInfo> {
    const info = await this.#plane.runMicrovm(request);
    await this.#onLaunch(info);
    return info;
  }

  getMicrovm(sandboxId: string, options?: ControlPlaneCallOptions): Promise<SandboxInfo> {
    return this.#plane.getMicrovm(sandboxId, options);
  }

  listMicrovms(options?: ListMicrovmsOptions): AsyncIterable<SandboxListItem> {
    return this.#plane.listMicrovms(options);
  }

  listMicrovmsPage(options: ListMicrovmsPageOptions): Promise<MicrovmListPage> {
    return this.#plane.listMicrovmsPage(options);
  }

  terminateMicrovm(sandboxId: string): Promise<boolean> {
    return this.#plane.terminateMicrovm(sandboxId);
  }

  suspendMicrovm(sandboxId: string): Promise<boolean> {
    return this.#plane.suspendMicrovm(sandboxId);
  }

  resumeMicrovm(sandboxId: string, options?: ControlPlaneCallOptions): Promise<boolean> {
    return this.#plane.resumeMicrovm(sandboxId, options);
  }

  createAuthToken(
    sandboxId: string,
    ports: readonly PortSpec[],
    options?: ControlPlaneCallOptions,
  ): Promise<string> {
    return this.#plane.createAuthToken(sandboxId, ports, options);
  }
}

class Semaphore {
  #free: number;
  readonly #queue: Array<() => void> = [];

  constructor(permits: number) {
    this.#free = permits;
  }

  acquire(): Promise<void> {
    if (this.#free > 0) {
      this.#free -= 1;
      return Promise.resolve();
    }
    return new Promise((resolve) => this.#queue.push(resolve));
  }

  release(): void {
    const next = this.#queue.shift();
    if (next !== undefined) {
      next();
      return;
    }
    this.#free += 1;
  }
}

function elapsedMs(now: number, started: number): number {
  return Math.round(Math.max(0, now - started));
}

export class SandboxPool implements AsyncDisposable {
  readonly #config: ResolvedPoolConfig;
  readonly #backend: PoolBackend;
  readonly #plane: ControlPlane;
  readonly #transport: TransportSettings;
  readonly #logger: Logger | undefined;
  readonly #monotonic: () => number;
  readonly #now: () => Date;
  readonly #sleep: ((ms: number) => Promise<void>) | undefined;
  readonly #backoff: FillBackoff;
  readonly #records = new Map<string, SlotRecord>();
  readonly #counters: PoolCounters = newCounters();
  readonly #wakers = new Set<() => void>();
  readonly #timers = new Set<ReturnType<typeof setTimeout>>();
  readonly #warmUps = new Set<Promise<void>>();
  readonly #abort = new AbortController();
  #backendQueue: Promise<unknown> = Promise.resolve();
  #semaphore: Semaphore | undefined;
  #filler: Promise<void> | undefined;
  #inflight = 0;
  #pendingBackoffMs: number | undefined;
  #sweepRequested = false;
  #lastSweep: number;
  #imageArn: string | undefined;
  #started = false;
  #closed = false;

  constructor(config: PoolConfig, options: SandboxPoolOptions = {}) {
    this.#config = validatePoolConfig(config);
    this.#backend = options.backend ?? new InMemoryPoolBackend();
    this.#plane = resolveControlPlane(options);
    this.#transport = resolveTransportSettings(options.transport);
    this.#logger = options.logger;
    this.#monotonic = options.monotonic ?? (() => performance.now());
    this.#now = options.now ?? (() => new Date());
    this.#sleep = options.sleep;
    this.#backoff = new FillBackoff(options.random ?? Math.random);
    this.#lastSweep = this.#monotonic();
  }

  get config(): ResolvedPoolConfig {
    return this.#config;
  }

  get backend(): PoolBackend {
    return this.#backend;
  }

  /** Acceso interno para los tests del paquete: los temporizadores vivos del pool. */
  static timersOf(pool: SandboxPool): ReadonlySet<ReturnType<typeof setTimeout>> {
    return pool.#timers;
  }

  // --------------------------------------------------------------- lifecycle

  /** Resuelve la imagen, recupera el backend, barre y arranca el relleno. Idempotente. */
  async start(): Promise<this> {
    if (this.#closed) {
      throw poolClosedError();
    }
    if (this.#started) {
      return this;
    }
    this.#imageArn = await this.#plane.resolveTemplateArn(resolveTemplate(this.#config.template));
    await this.#recover();
    await this.#sweep();
    this.#semaphore = new Semaphore(this.#config.fillConcurrency);
    this.#started = true;
    this.#filler = this.#runFiller();
    return this;
  }

  /**
   * Para el relleno y, con `drain` (por defecto), termina todas las plazas
   * `ready`. `drain: false` sólo con un backend persistente: deja las plazas
   * aparcadas para que otro pool las recupere. Idempotente.
   */
  async close(options: CloseOptions = {}): Promise<void> {
    const drain = options.drain ?? true;
    if (!drain && !this.#backend.persistent) {
      throw new InvalidArgumentError(
        "close({ drain: false }) requiere un backend persistent: con el backend en memoria las " +
          "plazas aparcadas serían irrecuperables",
      );
    }
    if (this.#closed) {
      return;
    }
    this.#closed = true;
    this.#abort.abort();
    this.#notifyAll();
    if (this.#filler !== undefined) {
      await this.#withTimeout(this.#filler, CLOSE_JOIN_MS);
    }
    await Promise.allSettled([...this.#warmUps]);
    if (drain) {
      await this.#drain();
    }
  }

  async [Symbol.asyncDispose](): Promise<void> {
    await this.close({ drain: true });
  }

  // -------------------------------------------------------------------- take

  /**
   * La plaza `ready` más vieja con al menos `minRemainingMs` de vida,
   * reanudada y abierta con su token; sin plaza (tras esperar hasta `waitMs`)
   * o con una plaza muerta, un `create()` normal.
   */
  async take(options: TakeOptions = {}): Promise<Sandbox> {
    this.#requireOpen();
    const record = await this.#claimReadySlot(options.waitMs ?? 0);
    const sandbox = record === undefined ? undefined : await this.#openSlot(record, options);
    return sandbox ?? this.#fallback(options);
  }

  stats(): PoolStats {
    return statsFromRecords(this.#records.values(), {
      size: this.#config.size,
      warming: this.#inflight,
      counters: { ...this.#counters },
    });
  }

  // ------------------------------------------------------------ take internals

  #requireOpen(): void {
    if (this.#closed) {
      throw poolClosedError();
    }
    if (!this.#started) {
      throw poolNotStartedError();
    }
  }

  async #claimReadySlot(waitMs: number): Promise<SlotRecord | undefined> {
    const deadline = this.#monotonic() + waitMs;
    let record = await this.#popReady();
    while (record === undefined && waitMs > 0) {
      const remaining = deadline - this.#monotonic();
      if (remaining <= 0 || !(await this.#waitForWake(remaining))) {
        break;
      }
      this.#requireOpen();
      record = await this.#popReady();
    }
    this.#notifyAll();
    return record;
  }

  async #popReady(): Promise<SlotRecord | undefined> {
    const record = pickReadySlot(this.#records.values(), this.#now(), this.#config.minRemainingMs);
    if (record !== undefined) {
      this.#records.delete(record.sandboxId);
      await this.#backendCall(() => this.#backend.delete(record.sandboxId));
    }
    return record;
  }

  async #openSlot(record: SlotRecord, options: TakeOptions): Promise<Sandbox | undefined> {
    const started = this.#monotonic();
    if (!(await this.#resumeSlot(record))) {
      return undefined;
    }
    let sandbox: Sandbox;
    try {
      sandbox = await Sandbox.openWith(sandboxInfoFromRecord(record), {
        accessToken: record.accessToken,
        controlPlane: this.#plane,
        transport: this.#transport,
        proxyPorts: [PortSpec.single(DEFAULT_PORT)],
        requestTimeoutMs: options.requestTimeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS,
        readyTimeoutMs: options.readyTimeoutMs ?? DEFAULT_READY_TIMEOUT_MS,
        reconnectTimeoutMs: options.reconnectTimeoutMs ?? DEFAULT_RECONNECT_TIMEOUT_MS,
        terminateOnFailure: true,
        logger: options.logger ?? this.#logger,
        readiness: TakePoll,
      });
    } catch (error) {
      this.#counters.failed += 1;
      this.#logger?.warn?.("plaza: no se pudo abrir al tomarla; fallback a create()", {
        sandboxId: record.sandboxId,
        reason: errorName(error),
      });
      return undefined;
    }
    this.#counters.takes += 1;
    this.#counters.hits += 1;
    this.#logger?.info?.("plaza tomada", {
      sandboxId: record.sandboxId,
      ms: elapsedMs(this.#monotonic(), started),
    });
    return sandbox;
  }

  async #resumeSlot(record: SlotRecord): Promise<boolean> {
    let resumed: boolean;
    try {
      resumed = await this.#plane.resumeMicrovm(record.sandboxId);
    } catch (error) {
      if (error instanceof SandboxNotFoundError || error instanceof SandboxStateError) {
        resumed = false;
      } else {
        this.#counters.failed += 1;
        this.#logger?.warn?.("plaza: resume-microvm falló; fallback a create()", {
          sandboxId: record.sandboxId,
          reason: errorName(error),
        });
        await this.#terminate(record.sandboxId);
        return false;
      }
    }
    if (!resumed) {
      this.#counters.lost += 1;
      this.#logger?.warn?.("plaza perdida al tomarla: ya no estaba SUSPENDED", {
        sandboxId: record.sandboxId,
      });
      await this.#terminate(record.sandboxId);
    }
    return resumed;
  }

  async #fallback(options: TakeOptions): Promise<Sandbox> {
    const plane = new LaunchObserver(this.#plane, async () => {
      this.#counters.launched += 1;
    });
    let sandbox: Sandbox;
    try {
      sandbox = await Sandbox.create({
        ...launchOptions(this.#config),
        readyTimeoutMs: options.readyTimeoutMs ?? this.#config.readyTimeoutMs,
        requestTimeoutMs: options.requestTimeoutMs,
        reconnectTimeoutMs: options.reconnectTimeoutMs,
        accessToken: generateAccessToken(),
        keepOnFailure: false,
        controlPlane: plane,
        transport: this.#transport,
        logger: options.logger ?? this.#logger,
      });
    } catch (error) {
      this.#counters.failed += 1;
      throw error;
    }
    this.#counters.takes += 1;
    this.#counters.misses += 1;
    this.#logger?.info?.("pool sin plaza lista: sandbox creado con create()", {
      sandboxId: sandbox.sandboxId,
    });
    return sandbox;
  }

  // ------------------------------------------------------------------ filler

  async #runFiller(): Promise<void> {
    while (!this.#stopping) {
      await this.#fill();
      await this.#waitForWork();
      if (!this.#stopping && this.#sweepDue()) {
        await this.#sweep();
      }
    }
  }

  get #stopping(): boolean {
    return this.#abort.signal.aborted;
  }

  async #fill(): Promise<void> {
    const delay = this.#pendingBackoffMs;
    this.#pendingBackoffMs = undefined;
    if (delay !== undefined) {
      await this.#pause(delay);
    }
    if (this.#stopping) {
      return;
    }
    this.#submitWarmUps();
  }

  #pause(ms: number): Promise<void> {
    if (this.#sleep !== undefined) {
      return this.#sleep(ms);
    }
    return this.#interruptibleDelay(ms);
  }

  /** Un `setTimeout` sin referencia que `close()` interrumpe. */
  #interruptibleDelay(ms: number): Promise<void> {
    return new Promise((resolve) => {
      const done = () => {
        this.#abort.signal.removeEventListener("abort", done);
        this.#clearTimer(timer);
        resolve();
      };
      const timer = this.#timer(done, ms);
      this.#abort.signal.addEventListener("abort", done, { once: true });
    });
  }

  #submitWarmUps(): void {
    const deficit = this.#deficit();
    if (deficit <= 0) {
      return;
    }
    this.#inflight += deficit;
    for (let index = 0; index < deficit; index += 1) {
      const task = this.#warmUp();
      this.#warmUps.add(task);
      void task.finally(() => this.#warmUps.delete(task));
    }
  }

  #deficit(): number {
    let ready = 0;
    for (const record of this.#records.values()) {
      if (record.state === "ready") {
        ready += 1;
      }
    }
    return this.#config.size - ready - this.#inflight;
  }

  async #waitForWork(): Promise<void> {
    if (this.#stopping || this.#pendingBackoffMs !== undefined) {
      return;
    }
    if (this.#deficit() > 0 || this.#sweepDue()) {
      return;
    }
    await this.#waitForWake(this.#msUntilSweep());
  }

  #sweepDue(): boolean {
    return this.#sweepRequested || this.#msUntilSweep() <= 0;
  }

  #msUntilSweep(): number {
    const elapsed = this.#monotonic() - this.#lastSweep;
    return Math.max(0, this.#config.sweepIntervalMs - elapsed);
  }

  /** Adelanta el siguiente barrido (tests). */
  requestSweep(): void {
    this.#sweepRequested = true;
    this.#notifyAll();
  }

  /** Resuelve `true` al ser despertado y `false` si venció el plazo (temporizador sin referencia). */
  #waitForWake(ms: number): Promise<boolean> {
    return new Promise((resolve) => {
      const finish = (woken: boolean) => {
        this.#wakers.delete(waker);
        this.#clearTimer(timer);
        resolve(woken);
      };
      const waker = () => finish(true);
      const timer = this.#timer(() => finish(false), ms);
      this.#wakers.add(waker);
    });
  }

  #notifyAll(): void {
    for (const waker of [...this.#wakers]) {
      waker();
    }
  }

  #timer(callback: () => void, ms: number): ReturnType<typeof setTimeout> {
    const timer = setTimeout(
      () => {
        this.#timers.delete(timer);
        callback();
      },
      Math.max(0, Math.min(ms, 2_147_483_647)),
    );
    timer.unref();
    this.#timers.add(timer);
    return timer;
  }

  #clearTimer(timer: ReturnType<typeof setTimeout>): void {
    clearTimeout(timer);
    this.#timers.delete(timer);
  }

  #withTimeout<T>(promise: Promise<T>, ms: number): Promise<T | undefined> {
    return new Promise((resolve, reject) => {
      const timer = this.#timer(() => resolve(undefined), ms);
      promise.then(
        (value) => {
          this.#clearTimer(timer);
          resolve(value);
        },
        (error) => {
          this.#clearTimer(timer);
          reject(error);
        },
      );
    });
  }

  // ------------------------------------------------------------------ warm-up

  async #warmUp(): Promise<void> {
    const semaphore = this.#semaphore;
    try {
      if (semaphore === undefined) {
        return;
      }
      await semaphore.acquire();
      try {
        await this.#warmUpOnce();
      } finally {
        semaphore.release();
      }
    } finally {
      this.#inflight -= 1;
      this.#notifyAll();
    }
  }

  async #warmUpOnce(): Promise<void> {
    const token = generateAccessToken();
    const started = this.#monotonic();
    let launchedId: string | undefined;
    const plane = new LaunchObserver(this.#plane, async (info) => {
      launchedId = info.sandboxId;
      await this.#recordLaunch(info, token);
    });
    let sandbox: Sandbox;
    try {
      sandbox = await Sandbox.create({
        ...launchOptions(this.#config),
        accessToken: token,
        keepOnFailure: false,
        controlPlane: plane,
        transport: this.#transport,
        logger: this.#logger,
      });
    } catch (error) {
      await this.#warmUpFailed(error, launchedId);
      return;
    }
    this.#logger?.info?.("plaza caliente", {
      sandboxId: sandbox.sandboxId,
      ms: elapsedMs(this.#monotonic(), started),
    });
    await this.#park(sandbox, token);
  }

  async #park(sandbox: Sandbox, token: string): Promise<void> {
    const started = this.#monotonic();
    const sandboxId = sandbox.sandboxId;
    try {
      await this.#settle(sandbox);
      if (this.#stopping) {
        throw new PoolStopping();
      }
      await sandbox.pause({ wait: true });
      if (this.#stopping) {
        throw new PoolStopping();
      }
    } catch (error) {
      sandbox.close();
      if (error instanceof PoolStopping) {
        await this.#discard(sandboxId);
        return;
      }
      await this.#warmUpFailed(error, sandboxId);
      await this.#terminate(sandboxId);
      return;
    }
    sandbox.close();
    const record = recordFromInfo(sandbox.info, {
      accessToken: token,
      region: this.#plane.region,
      state: "ready",
      parkedAt: this.#now(),
    });
    await this.#recordReady(record);
    this.#logger?.info?.("plaza aparcada", {
      sandboxId,
      ms: elapsedMs(this.#monotonic(), started),
    });
  }

  /**
   * Cierra la ventana entre el 200 de `/run` y el arranque de la rotación del
   * kernel: `rayd` la despacha en segundo plano y un `Health` que llega antes
   * ve `kernelReady` del kernel sin rotar; una plaza aparcada así pierde el
   * kernel al reanudar. Una celda trivial sólo vuelve sobre el kernel rotado
   * (`rayd` retiene `Execute` mientras el contexto reinicia) y un `Health`
   * tras una pausa breve confirma que no arrancó una rotación después.
   */
  async #settle(sandbox: Sandbox): Promise<void> {
    for (let attempt = 0; attempt < SETTLE_ATTEMPTS; attempt += 1) {
      await sandbox.runCode(SETTLE_CELL, { requestTimeoutMs: this.#config.readyTimeoutMs });
      await this.#interruptibleDelay(SETTLE_GRACE_MS);
      if ((await sandbox.getHealth()).kernelReady) {
        return;
      }
      await Sandbox.coreOf(sandbox).waitUntilReady({ terminateOnFailure: false });
    }
    this.#logger?.warn?.("plaza: el kernel siguió rotando tras asentarla", {
      sandboxId: sandbox.sandboxId,
    });
  }

  /**
   * Runs inside `runMicrovm`, before `create()` has seen the VM: a backend
   * failure here cannot be cleaned up by `create()`, so the VM is terminated
   * at this point and the error propagates to the warm-up.
   */
  async #recordLaunch(info: SandboxInfo, token: string): Promise<void> {
    const record = recordFromInfo(info, {
      accessToken: token,
      region: this.#plane.region,
      state: "warming",
    });
    try {
      await this.#backendCall(() => this.#backend.save(record));
    } catch (error) {
      await this.#terminate(info.sandboxId);
      throw error;
    }
    this.#records.set(record.sandboxId, record);
    this.#counters.launched += 1;
  }

  async #recordReady(record: SlotRecord): Promise<void> {
    this.#records.set(record.sandboxId, record);
    this.#backoff.reset();
    await this.#backendCall(() => this.#backend.save(record));
    this.#notifyAll();
  }

  async #warmUpFailed(error: unknown, sandboxId: string | undefined): Promise<void> {
    if (sandboxId !== undefined) {
      await this.#forget(sandboxId);
    }
    this.#counters.failed += 1;
    const delay = this.#backoff.nextDelayMs();
    this.#pendingBackoffMs = delay;
    this.#notifyAll();
    this.#logger?.warn?.("calentamiento fallido; siguiente intento tras el backoff", {
      sandboxId: sandboxId ?? "sin lanzar",
      reason: errorName(error),
      delayMs: Math.round(delay),
    });
  }

  /** Un calentamiento que terminó con el pool parándose: se termina el VM en vez de aparcarlo. */
  async #discard(sandboxId: string): Promise<void> {
    await this.#forget(sandboxId);
    await this.#terminate(sandboxId);
  }

  // ------------------------------------------------------------------- sweep

  async #sweep(): Promise<void> {
    this.#sweepRequested = false;
    this.#lastSweep = this.#monotonic();
    await this.#recycleStale();
    await this.#reconcile();
  }

  async #recycleStale(): Promise<void> {
    const now = this.#now();
    for (const record of this.#readyRecords()) {
      if (!slotIsStale(record, now, this.#config.minRemainingMs)) {
        continue;
      }
      if (await this.#forget(record.sandboxId)) {
        this.#counters.recycled += 1;
        this.#logger?.warn?.("plaza reciclada antes del muro", {
          sandboxId: record.sandboxId,
          remainingMs: Math.round(Math.max(0, expiresIn(record, now))),
          minRemainingMs: this.#config.minRemainingMs,
        });
        await this.#terminate(record.sandboxId);
      }
    }
  }

  async #reconcile(): Promise<void> {
    let listed: Map<string, string>;
    try {
      listed = await this.#listedStates();
    } catch (error) {
      this.#logger?.warn?.("barrido abortado: list-microvms falló", { reason: errorName(error) });
      return;
    }
    for (const record of this.#readyRecords()) {
      const action = reconcileAction(record, listed.get(record.sandboxId));
      if (action === "repark") {
        await this.#repark(record);
      } else if (action === "check") {
        await this.#check(record);
      } else if (action === "drop") {
        await this.#lose(record, listed.get(record.sandboxId) ?? "terminal");
      }
    }
  }

  async #listedStates(): Promise<Map<string, string>> {
    const states = new Map<string, string>();
    for await (const item of this.#plane.listMicrovms({
      imageArn: this.#imageArn,
      imageVersion: this.#config.templateVersion,
    })) {
      states.set(item.sandboxId, item.state);
    }
    return states;
  }

  async #repark(record: SlotRecord): Promise<void> {
    try {
      await this.#plane.suspendMicrovm(record.sandboxId);
    } catch (error) {
      this.#logger?.warn?.("plaza: no se pudo volver a aparcar", {
        sandboxId: record.sandboxId,
        reason: errorName(error),
      });
      return;
    }
    this.#logger?.warn?.("plaza reanudada fuera del pool; vuelta a aparcar", {
      sandboxId: record.sandboxId,
    });
  }

  async #check(record: SlotRecord): Promise<void> {
    let info: SandboxInfo;
    try {
      info = await this.#plane.getMicrovm(record.sandboxId);
    } catch (error) {
      if (error instanceof SandboxNotFoundError) {
        await this.#lose(record, "not found");
        return;
      }
      this.#logger?.warn?.("plaza: get-microvm falló en el barrido", {
        sandboxId: record.sandboxId,
        reason: errorName(error),
      });
      return;
    }
    if (TERMINAL_STATES.has(info.state)) {
      await this.#lose(record, `${info.state}: ${info.stateReason ?? "sin stateReason"}`);
    }
  }

  async #lose(record: SlotRecord, reason: string): Promise<void> {
    if (await this.#forget(record.sandboxId)) {
      this.#counters.lost += 1;
      this.#logger?.warn?.("plaza perdida fuera del pool", {
        sandboxId: record.sandboxId,
        reason,
      });
    }
  }

  // ---------------------------------------------------------------- recovery

  async #recover(): Promise<void> {
    for (const record of await this.#backendCall(() => this.#backend.load())) {
      if (record.state === "warming") {
        await this.#backendCall(() => this.#backend.delete(record.sandboxId));
        this.#counters.lost += 1;
        this.#logger?.warn?.("plaza huérfana de un calentamiento interrumpido; terminada", {
          sandboxId: record.sandboxId,
        });
        await this.#terminate(record.sandboxId);
      } else {
        this.#records.set(record.sandboxId, record);
      }
    }
  }

  async #drain(): Promise<void> {
    for (const record of [...this.#records.values()]) {
      if (await this.#forget(record.sandboxId)) {
        await this.#terminate(record.sandboxId);
      }
    }
  }

  // ----------------------------------------------------------------- helpers

  #readyRecords(): SlotRecord[] {
    return [...this.#records.values()].filter((record) => record.state === "ready");
  }

  async #forget(sandboxId: string): Promise<boolean> {
    if (!this.#records.delete(sandboxId)) {
      return false;
    }
    await this.#backendCall(() => this.#backend.delete(sandboxId));
    this.#notifyAll();
    return true;
  }

  async #terminate(sandboxId: string): Promise<void> {
    try {
      await this.#plane.terminateMicrovm(sandboxId);
    } catch (error) {
      this.#logger?.warn?.("no se pudo terminar la plaza", {
        sandboxId,
        reason: errorMessage(error),
      });
    }
  }

  /** Toda llamada al backend va en serie: un backend no necesita ser reentrante. */
  #backendCall<T>(call: () => Promise<T>): Promise<T> {
    const next = this.#backendQueue.then(call, call);
    this.#backendQueue = next.catch(() => undefined);
    return next;
  }
}

function expiresIn(record: SlotRecord, now: Date): number {
  return record.startedAt.getTime() + record.maximumDurationSeconds * 1000 - now.getTime();
}

function errorName(error: unknown): string {
  return error instanceof Error ? error.name : typeof error;
}

function poolNotStartedError(): PoolClosedError {
  return new PoolClosedError("pool not started: call start() or use it as a context manager");
}

function poolClosedError(): PoolClosedError {
  return new PoolClosedError("el pool está cerrado: take() ya no entrega sandboxes");
}
