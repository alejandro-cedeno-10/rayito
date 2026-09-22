/**
 * Núcleo interno de un `Sandbox`: los dos transportes (unarios y streams),
 * los clientes por servicio, el `TokenRefresher`, la política de readiness y
 * el contrato de reconexión M5 (D13/D14 del diseño): sondeo compartido de
 * `Health`, reglas de despertar, handles dormidos y reintentos tras un 403 del
 * proxy. `Sandbox`, `Commands`, `Filesystem`, `Pty` y `CodeClient` delegan aquí.
 */

import type { DescService } from "@bufbuild/protobuf";
import { create } from "@bufbuild/protobuf";
import {
  type CallOptions,
  type Client,
  ConnectError,
  createClient,
  type Transport,
} from "@connectrpc/connect";
import type { ControlPlane } from "../aws/control-plane.js";
import {
  AuthenticationError,
  errorMessage,
  SandboxError,
  SandboxNotFoundError,
} from "../errors.js";
import { CodeService } from "../gen/rayito/v1/code_pb.js";
import { FilesystemService } from "../gen/rayito/v1/filesystem_pb.js";
import {
  HealthRequestSchema,
  type HealthResponse,
  HealthService,
} from "../gen/rayito/v1/health_pb.js";
import { ProcessService } from "../gen/rayito/v1/process_pb.js";
import { PtyService } from "../gen/rayito/v1/pty_pb.js";
import { DEFAULT_PORT, SUSPENDED_STATES, TERMINAL_STATES } from "../limits.js";
import type { Logger } from "../logger.js";
import type { SandboxInfo } from "../models.js";
import {
  isNotYetReachable,
  isProxyForbidden,
  isReconnectable,
  isStreamReset,
  translateRpcError,
} from "../transport/errors.js";
import { proxyAuthInterceptor } from "../transport/headers.js";
import type { TokenRefresher } from "../transport/tokens.js";
import {
  type OpenedTransport,
  openTransport,
  type TransportSettings,
} from "../transport/transport.js";
import { STREAM_PROBE_TIMEOUT_MS, streamFailureError } from "./commands.js";
import {
  CLOCK_OFFSET_WARN_MS,
  closedDuringReconnect,
  formatSeconds,
  healthReconnected,
  isSuspendingReason,
  notReadyError,
  ReadinessPoll,
  type ReconnectOutcome,
  ReconnectPoll,
  reconnectFailure,
  terminatedDuringBootError,
} from "./readiness.js";

export type StreamStarter<S extends DescService, T> = (
  client: Client<S>,
  options: CallOptions,
) => AsyncIterable<T>;

export type UnaryInvoker<S extends DescService, T> = (
  client: Client<S>,
  options: CallOptions,
) => Promise<T>;

/** Un server-stream abierto: el iterador tras el primer mensaje, ese mensaje y su `AbortController`. */
export interface OpenedStream<T> {
  readonly iterator: AsyncIterator<T>;
  readonly first: T | undefined;
  readonly controller: AbortController;
}

export interface OpenStreamOptions<S extends DescService> {
  readonly service: S;
  readonly stream: boolean;
  readonly allowEmpty?: boolean | undefined;
  readonly filesystem?: boolean | undefined;
  readonly reconnect?: boolean | undefined;
  /** Sustituye la tabla unaria para un status que no es un corte (persistencia). */
  readonly translate?: ((error: unknown) => Error) | undefined;
}

export interface SandboxCoreInit {
  readonly info: SandboxInfo;
  readonly accessToken: string;
  readonly controlPlane: ControlPlane;
  readonly transport: TransportSettings;
  readonly refresher: TokenRefresher;
  readonly requestTimeoutMs: number;
  readonly readyTimeoutMs: number;
  readonly reconnectTimeoutMs: number;
  readonly logger: Logger | undefined;
}

/** Un `WatchHandle` visto por `close()`: se marca parado y se aborta su stream sin esperar. */
export interface Abortable {
  abortNow(): void;
}

/** `grpc-timeout` es un entero: todo deadline derivado de `performance.now()` se redondea hacia arriba. */
export function withTimeout(options: CallOptions, timeoutMs: number | undefined): CallOptions {
  return timeoutMs === undefined ? options : { ...options, timeoutMs: Math.ceil(timeoutMs) };
}

interface Deferred {
  readonly promise: Promise<void>;
  readonly resolve: () => void;
}

function deferred(): Deferred {
  let resolve: () => void = () => undefined;
  const promise = new Promise<void>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function sleep(ms: number): { promise: Promise<void>; cancel: () => void } {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const promise = new Promise<void>((resolve) => {
    timer = setTimeout(resolve, ms);
  });
  return {
    promise,
    cancel: () => {
      if (timer !== undefined) {
        clearTimeout(timer);
      }
    },
  };
}

export class SandboxCore {
  info: SandboxInfo;
  /** La `SandboxInfo` con la que se abrió el handle, nunca refrescada. */
  readonly launchInfo: SandboxInfo;
  readonly accessToken: string;
  readonly controlPlane: ControlPlane;
  readonly transportSettings: TransportSettings;
  readonly refresher: TokenRefresher;
  readonly requestTimeoutMs: number;
  readonly readyTimeoutMs: number;
  readonly reconnectTimeoutMs: number;
  readonly logger: Logger | undefined;
  readonly now: () => number = () => performance.now();

  closed = false;
  resumeGeneration = 0;
  paused = false;

  readonly unaryTransport: Transport;
  readonly #unarySession: OpenedTransport;
  #streamSession: OpenedTransport | undefined;
  readonly #unaryClients = new Map<string, unknown>();
  readonly #streamClients = new Map<string, unknown>();
  readonly clients: {
    readonly health: Client<typeof HealthService>;
    readonly process: Client<typeof ProcessService>;
    readonly filesystem: Client<typeof FilesystemService>;
    readonly code: Client<typeof CodeService>;
    readonly pty: Client<typeof PtyService>;
  };

  readonly #liveStreams = new Set<AbortController>();
  readonly #watches = new Set<Abortable>();
  #reconnectLock: Promise<void> | undefined;
  #resumed: Deferred = deferred();

  constructor(init: SandboxCoreInit) {
    this.info = init.info;
    this.launchInfo = init.info;
    this.accessToken = init.accessToken;
    this.controlPlane = init.controlPlane;
    this.transportSettings = init.transport;
    this.refresher = init.refresher;
    this.requestTimeoutMs = init.requestTimeoutMs;
    this.readyTimeoutMs = init.readyTimeoutMs;
    this.reconnectTimeoutMs = init.reconnectTimeoutMs;
    this.logger = init.logger;
    this.#unarySession = this.#openTransport();
    this.unaryTransport = this.#unarySession.transport;
    this.clients = {
      health: this.clientFor(HealthService, false),
      process: this.clientFor(ProcessService, false),
      filesystem: this.clientFor(FilesystemService, false),
      code: this.clientFor(CodeService, false),
      pty: this.clientFor(PtyService, false),
    };
  }

  get sandboxId(): string {
    return this.info.sandboxId;
  }

  get streamTransportOpened(): boolean {
    return this.#streamSession !== undefined;
  }

  get liveStreamCount(): number {
    return this.#liveStreams.size;
  }

  #openTransport(): OpenedTransport {
    const interceptor = proxyAuthInterceptor(this.refresher.store, {
      port: DEFAULT_PORT,
      accessToken: this.accessToken,
    });
    return openTransport(this.info.endpoint, this.transportSettings, interceptor);
  }

  /** El transporte de streams se abre en el primer uso: es el segundo y último del sandbox. */
  clientFor<S extends DescService>(service: S, stream: boolean): Client<S> {
    const cache = stream ? this.#streamClients : this.#unaryClients;
    const cached = cache.get(service.typeName);
    if (cached !== undefined) {
      return cached as Client<S>;
    }
    if (stream && this.#streamSession === undefined) {
      this.#streamSession = this.#openTransport();
    }
    const transport = stream
      ? (this.#streamSession as OpenedTransport).transport
      : this.unaryTransport;
    const client = createClient(service, transport);
    cache.set(service.typeName, client);
    return client;
  }

  currentJwe(port: number): string {
    const jwe = this.refresher.store.jweFor(port);
    if (jwe === undefined) {
      throw new AuthenticationError(
        `no hay token del proxy para el puerto ${port}; llama a getHost`,
      );
    }
    return jwe;
  }

  // ------------------------------------------------------------------ unary

  async callUnaryOnce<T>(call: () => Promise<T>): Promise<T> {
    try {
      return await call();
    } catch (error) {
      if (!isProxyForbidden(error)) {
        throw error;
      }
    }
    this.logger?.info?.("el proxy rechazó el token del sandbox; reacuñando", {
      sandboxId: this.sandboxId,
    });
    await this.refresher.refreshAll();
    return call();
  }

  /** Un 403 del proxy se reintenta tras reacuñar; un corte reconectable espera la reconexión y reintenta una vez. */
  async callUnary<T>(call: () => Promise<T>): Promise<T> {
    const seenGeneration = this.resumeGeneration;
    let reason: ConnectError;
    try {
      return await this.callUnaryOnce(call);
    } catch (error) {
      if (!this.isReconnectable(error)) {
        throw error;
      }
      reason = error as ConnectError;
    }
    const outcome = await this.reconnect(reason, seenGeneration, { wake: true });
    if (!outcome.resumed) {
      throw this.reconnectError(outcome, reason);
    }
    return call();
  }

  async translatedUnary<T>(
    call: () => Promise<T>,
    options: { readonly filesystem?: boolean | undefined } = {},
  ): Promise<T> {
    try {
      return await this.callUnary(call);
    } catch (error) {
      throw translateRpcError(error, { filesystem: options.filesystem });
    }
  }

  resolveRequestTimeout(requestTimeoutMs: number | undefined): number {
    return requestTimeoutMs ?? this.requestTimeoutMs;
  }

  processCall<T>(
    invoke: UnaryInvoker<typeof ProcessService, T>,
    requestTimeoutMs: number | undefined,
  ): Promise<T> {
    const timeoutMs = this.resolveRequestTimeout(requestTimeoutMs);
    return this.translatedUnary(() => invoke(this.clients.process, { timeoutMs }));
  }

  ptyCall<T>(
    invoke: UnaryInvoker<typeof PtyService, T>,
    requestTimeoutMs: number | undefined,
  ): Promise<T> {
    const timeoutMs = this.resolveRequestTimeout(requestTimeoutMs);
    return this.translatedUnary(() => invoke(this.clients.pty, { timeoutMs }));
  }

  filesCall<T>(
    invoke: UnaryInvoker<typeof FilesystemService, T>,
    requestTimeoutMs: number | undefined,
  ): Promise<T> {
    const timeoutMs = this.resolveRequestTimeout(requestTimeoutMs);
    return this.translatedUnary(() => invoke(this.clients.filesystem, { timeoutMs }), {
      filesystem: true,
    });
  }

  codeCall<T>(
    invoke: UnaryInvoker<typeof CodeService, T>,
    requestTimeoutMs: number | undefined,
    defaultTimeoutMs?: number,
  ): Promise<T> {
    const timeoutMs =
      requestTimeoutMs === undefined && defaultTimeoutMs !== undefined
        ? defaultTimeoutMs
        : this.resolveRequestTimeout(requestTimeoutMs);
    return this.translatedUnary(() => invoke(this.clients.code, { timeoutMs }));
  }

  // ---------------------------------------------------------------- streams

  /**
   * Abre un server-stream y consume su primer mensaje: un 403 del proxy antes
   * del primer mensaje se reintenta una vez tras reacuñar; un corte
   * reconectable espera la reconexión y reintenta una vez (salvo
   * `reconnect: false`, p. ej. `Execute`, que nunca corre dos veces).
   */
  async openStream<S extends DescService, T>(
    start: StreamStarter<S, T>,
    options: OpenStreamOptions<S>,
  ): Promise<OpenedStream<T>> {
    const client = this.clientFor(options.service, options.stream);
    const seenGeneration = this.resumeGeneration;
    const reconnect = options.reconnect ?? true;
    let reason: ConnectError;
    try {
      return await this.#firstMessageReminting(start, client, options.allowEmpty ?? false);
    } catch (error) {
      if (!(reconnect && this.isReconnectable(error))) {
        throw await this.#openFailure(error, options);
      }
      reason = error as ConnectError;
    }
    const outcome = await this.reconnect(reason, seenGeneration, { wake: true });
    if (!outcome.resumed) {
      throw this.reconnectError(outcome, reason);
    }
    try {
      return await this.#firstMessage(start, client, options.allowEmpty ?? false);
    } catch (error) {
      throw await this.#openFailure(error, options);
    }
  }

  async #openFailure<S extends DescService>(
    error: unknown,
    options: OpenStreamOptions<S>,
  ): Promise<Error> {
    if (options.translate !== undefined && !isStreamReset(error)) {
      return options.translate(error);
    }
    return this.streamFailure(error, { filesystem: options.filesystem });
  }

  async #firstMessageReminting<S extends DescService, T>(
    start: StreamStarter<S, T>,
    client: Client<S>,
    allowEmpty: boolean,
  ): Promise<OpenedStream<T>> {
    try {
      return await this.#firstMessage(start, client, allowEmpty);
    } catch (error) {
      if (!isProxyForbidden(error)) {
        throw error;
      }
    }
    this.logger?.info?.("el proxy rechazó el token del sandbox al abrir un stream; reacuñando", {
      sandboxId: this.sandboxId,
    });
    await this.refresher.refreshAll();
    return this.#firstMessage(start, client, allowEmpty);
  }

  async #firstMessage<S extends DescService, T>(
    start: StreamStarter<S, T>,
    client: Client<S>,
    allowEmpty: boolean,
  ): Promise<OpenedStream<T>> {
    const controller = new AbortController();
    const iterator = start(client, { signal: controller.signal })[Symbol.asyncIterator]();
    let result: IteratorResult<T>;
    try {
      result = await iterator.next();
    } catch (error) {
      controller.abort();
      throw error;
    }
    if (result.done) {
      if (allowEmpty) {
        return { iterator, first: undefined, controller };
      }
      controller.abort();
      throw new SandboxError("el stream terminó antes del primer mensaje");
    }
    return { iterator, first: result.value, controller };
  }

  async streamFailure(
    error: unknown,
    options: { readonly filesystem?: boolean | undefined } = {},
  ): Promise<Error> {
    if (!(error instanceof ConnectError) || !isStreamReset(error)) {
      return translateRpcError(error, { filesystem: options.filesystem });
    }
    const healthOk = await this.#healthAnswers();
    const state = healthOk ? undefined : await this.#stateAfterReset();
    return streamFailureError(error, { healthOk, state });
  }

  async #healthAnswers(): Promise<boolean> {
    try {
      return (await this.probeHealth(STREAM_PROBE_TIMEOUT_MS)) !== undefined;
    } catch (error) {
      this.logger?.debug?.("Health no respondió tras un corte de stream", {
        sandboxId: this.sandboxId,
        reason: errorMessage(error),
      });
      return false;
    }
  }

  async #stateAfterReset(): Promise<string | undefined> {
    try {
      this.info = await this.controlPlane.getMicrovm(this.sandboxId);
    } catch (error) {
      if (error instanceof SandboxNotFoundError) {
        return "TERMINATED";
      }
      if (error instanceof SandboxError) {
        return undefined;
      }
      throw error;
    }
    return this.info.state;
  }

  async probeHealth(timeoutMs: number): Promise<HealthResponse | undefined> {
    try {
      return await this.callUnaryOnce(() =>
        this.clients.health.health(create(HealthRequestSchema, {}), withTimeout({}, timeoutMs)),
      );
    } catch (error) {
      if (isNotYetReachable(error)) {
        return undefined;
      }
      throw translateRpcError(error);
    }
  }

  // -------------------------------------------------------------- readiness

  /** `readiness` es el calendario del sondeo: `create()`, `connect()` y `resume()` usan `ReadinessPoll`; el pool pasa `TakePoll`. */
  async waitUntilReady(options: {
    readonly terminateOnFailure: boolean;
    readonly readiness?: typeof ReadinessPoll | undefined;
  }): Promise<HealthResponse> {
    const Poll = options.readiness ?? ReadinessPoll;
    const poll = new Poll({ timeoutMs: this.readyTimeoutMs });
    while (true) {
      const response = await this.probeHealth(poll.rpcTimeoutMs());
      if (response?.agentReady && response.kernelReady) {
        this.recordHealth(response);
        this.logger?.info?.("agente listo", {
          sandboxId: this.sandboxId,
          seconds: formatSeconds(poll.elapsedMs()),
        });
        return response;
      }
      if (poll.shouldCheckState()) {
        await this.#failIfTerminal();
      }
      if (poll.timedOut()) {
        throw await this.#notReady(options.terminateOnFailure);
      }
      await sleep(poll.nextDelayMs()).promise;
    }
  }

  recordHealth(response: HealthResponse): void {
    const generation = Number(response.resumeGeneration);
    if (generation === this.resumeGeneration) {
      return;
    }
    this.resumeGeneration = generation;
    this.paused = false;
    this.#notifyResumed();
    if (response.kernelStateLost) {
      this.logger?.warn?.("un kernel perdió su estado en el resume", {
        sandboxId: this.sandboxId,
        resumeGeneration: generation,
      });
    }
    const offset = Number(response.clockOffsetMs);
    if (Math.abs(offset) > CLOCK_OFFSET_WARN_MS) {
      this.logger?.warn?.("desfase de reloj tras el resume", {
        sandboxId: this.sandboxId,
        clockOffsetMs: offset,
        resumeGeneration: generation,
      });
    }
  }

  isReconnectable(error: unknown): boolean {
    return !this.closed && isReconnectable(error);
  }

  reconnectError(outcome: ReconnectOutcome, reason: Error): Error {
    return outcome.error ?? reason;
  }

  async suspendMarkingPaused(): Promise<boolean> {
    const wasPaused = this.paused;
    this.paused = true;
    let suspended: boolean;
    try {
      suspended = await this.controlPlane.suspendMicrovm(this.sandboxId);
    } catch (error) {
      this.paused = wasPaused;
      throw error;
    }
    if (!suspended) {
      this.paused = wasPaused;
    }
    return suspended;
  }

  foregroundStreamWakes(): boolean {
    return !this.paused;
  }

  // -------------------------------------------------------------- reconnect

  /**
   * Fase dormida fuera del lock sin `wake`, sondeo de `Health` bajo el lock,
   * y una señal que `recordHealth` y `close()` disparan para interrumpir las
   * esperas.
   */
  async reconnect(
    reason: Error,
    seenGeneration: number,
    options: { readonly wake: boolean },
  ): Promise<ReconnectOutcome> {
    while (true) {
      let outcome = this.#alreadyBack(seenGeneration);
      if (outcome === undefined && !options.wake) {
        outcome = await this.#sleepWhileSuspended(reason, seenGeneration);
      }
      if (outcome === undefined) {
        outcome = await this.#pollHoldingTheLock(reason, seenGeneration, options.wake);
      }
      if (outcome !== undefined) {
        return outcome;
      }
    }
  }

  #alreadyBack(seenGeneration: number): ReconnectOutcome | undefined {
    if (this.resumeGeneration > seenGeneration) {
      return { resumed: true, generationChanged: true, resumeGeneration: this.resumeGeneration };
    }
    if (this.closed) {
      return this.#failedReconnect(closedDuringReconnect(this.sandboxId));
    }
    return undefined;
  }

  async #sleepWhileSuspended(
    reason: Error,
    seenGeneration: number,
  ): Promise<ReconnectOutcome | undefined> {
    await this.#waitForResume(ReconnectPoll.initialDelayMs);
    while (true) {
      const outcome = this.#alreadyBack(seenGeneration);
      if (outcome !== undefined) {
        return outcome;
      }
      const failure = await this.#checkState(reason, false);
      if (failure !== undefined) {
        return this.#failedReconnect(failure);
      }
      if (!SUSPENDED_STATES.has(this.info.state)) {
        return undefined;
      }
      await this.#waitForResume(ReconnectPoll.stateCheckIntervalMs);
    }
  }

  async #pollHoldingTheLock(
    reason: Error,
    seenGeneration: number,
    wake: boolean,
  ): Promise<ReconnectOutcome | undefined> {
    return this.#withReconnectLock(async () => {
      const outcome = this.#alreadyBack(seenGeneration);
      if (outcome !== undefined) {
        return outcome;
      }
      this.logger?.info?.("stream/unario cortado; esperando al agente", {
        sandboxId: this.sandboxId,
        reason: reason.constructor.name,
        reconnectTimeoutMs: this.reconnectTimeoutMs,
      });
      return this.#pollUntilBack(reason, seenGeneration, wake);
    });
  }

  async #withReconnectLock<T>(fn: () => Promise<T>): Promise<T> {
    while (this.#reconnectLock !== undefined) {
      await this.#reconnectLock;
    }
    const release = deferred();
    this.#reconnectLock = release.promise;
    try {
      return await fn();
    } finally {
      this.#reconnectLock = undefined;
      release.resolve();
    }
  }

  async #pollUntilBack(
    reason: Error,
    seenGeneration: number,
    wake: boolean,
  ): Promise<ReconnectOutcome | undefined> {
    const poll = new ReconnectPoll({ timeoutMs: this.reconnectTimeoutMs });
    const suspending = isSuspendingReason(reason);
    while (true) {
      const outcome = this.#alreadyBack(seenGeneration);
      if (outcome !== undefined) {
        return outcome;
      }
      if (poll.timedOut()) {
        return this.#failedReconnect(this.#reconnectTimedOut(reason));
      }
      if (wake && poll.shouldCheckState()) {
        const failure = await this.#checkState(reason, true);
        if (failure !== undefined) {
          return this.#failedReconnect(failure);
        }
      }
      let response: HealthResponse | undefined;
      try {
        response = await this.probeHealth(poll.rpcTimeoutMs());
      } catch (error) {
        return this.#failedReconnect(this.#probeFailure(error));
      }
      if (healthReconnected(response, { seenGeneration, suspending })) {
        return this.#reconnected(response as HealthResponse, seenGeneration, poll.elapsedMs());
      }
      if (!wake) {
        const failure = await this.#checkState(reason, false);
        if (failure !== undefined) {
          return this.#failedReconnect(failure);
        }
        if (SUSPENDED_STATES.has(this.info.state)) {
          return undefined;
        }
      }
      await this.#waitForResume(poll.nextDelayMs());
    }
  }

  /**
   * Duerme como mucho `timeoutMs`; una generación nueva o `close()` lo
   * interrumpen. La señal se reemplaza al dispararse, así cada espera ve
   * sólo los avisos posteriores a su inicio.
   */
  async #waitForResume(timeoutMs: number): Promise<void> {
    const timer = sleep(timeoutMs);
    try {
      await Promise.race([this.#resumed.promise, timer.promise]);
    } finally {
      timer.cancel();
    }
  }

  #notifyResumed(): void {
    const previous = this.#resumed;
    this.#resumed = deferred();
    previous.resolve();
  }

  #probeFailure(error: unknown): Error {
    if (this.closed) {
      return closedDuringReconnect(this.sandboxId);
    }
    return error instanceof Error ? error : new SandboxError(String(error));
  }

  async #checkState(reason: Error, wake: boolean): Promise<Error | undefined> {
    try {
      this.info = await this.controlPlane.getMicrovm(this.sandboxId);
    } catch (error) {
      if (error instanceof SandboxNotFoundError) {
        return error;
      }
      if (error instanceof SandboxError) {
        this.logger?.debug?.("get-microvm falló durante la reconexión", {
          sandboxId: this.sandboxId,
          reason: errorMessage(error),
        });
        return undefined;
      }
      throw error;
    }
    return reconnectFailure(reason, { info: this.info, wake });
  }

  #reconnectTimedOut(reason: Error): Error {
    return reconnectFailure(reason, { timeoutMs: this.reconnectTimeoutMs }) ?? reason;
  }

  #reconnected(
    response: HealthResponse,
    seenGeneration: number,
    elapsedMs: number,
  ): ReconnectOutcome {
    this.recordHealth(response);
    const generation = Number(response.resumeGeneration);
    this.logger?.info?.("reconectado", {
      sandboxId: this.sandboxId,
      seconds: formatSeconds(elapsedMs),
      fromGeneration: seenGeneration,
      resumeGeneration: generation,
    });
    return {
      resumed: true,
      generationChanged: generation !== seenGeneration,
      resumeGeneration: generation,
    };
  }

  #failedReconnect(error: Error): ReconnectOutcome {
    this.logger?.warn?.("reconexión fallida", {
      sandboxId: this.sandboxId,
      reason: error.constructor.name,
      message: error.message,
    });
    return {
      resumed: false,
      generationChanged: false,
      resumeGeneration: this.resumeGeneration,
      error,
    };
  }

  async #failIfTerminal(): Promise<void> {
    this.info = await this.controlPlane.getMicrovm(this.sandboxId);
    if (TERMINAL_STATES.has(this.info.state)) {
      throw terminatedDuringBootError(this.info);
    }
  }

  async #notReady(terminate: boolean): Promise<SandboxError> {
    let info: SandboxInfo | undefined;
    try {
      info = await this.controlPlane.getMicrovm(this.sandboxId);
    } catch (error) {
      if (!(error instanceof SandboxError)) {
        throw error;
      }
      info = undefined;
    }
    let terminated = false;
    if (terminate && (info === undefined || !TERMINAL_STATES.has(info.state))) {
      await this.controlPlane.terminateMicrovm(this.sandboxId);
      terminated = true;
    }
    return notReadyError(info, { readyTimeoutMs: this.readyTimeoutMs, terminated });
  }

  // ------------------------------------------------------------- registries

  trackStream(controller: AbortController): void {
    this.#liveStreams.add(controller);
    controller.signal.addEventListener("abort", () => this.#liveStreams.delete(controller), {
      once: true,
    });
  }

  /** Un stream que terminó por sí solo: se aborta su controller para soltarlo del registro (idempotente). */
  releaseStream(controller: AbortController): void {
    controller.abort();
    this.#liveStreams.delete(controller);
  }

  trackWatch(watch: Abortable): void {
    this.#watches.add(watch);
  }

  untrackWatch(watch: Abortable): void {
    this.#watches.delete(watch);
  }

  /**
   * Idempotente; nunca toca el VM. Despierta a los que esperan una reconexión
   * con `SandboxError` y cierra las dos sesiones HTTP/2 (sin esto vivirían
   * hasta el idle timeout de 15 min, con PINGs de por medio).
   */
  close(): void {
    if (this.closed) {
      return;
    }
    this.closed = true;
    this.#notifyResumed();
    this.refresher.stop();
    for (const watch of [...this.#watches]) {
      watch.abortNow();
    }
    this.#watches.clear();
    for (const controller of [...this.#liveStreams]) {
      controller.abort();
    }
    this.#liveStreams.clear();
    this.#unarySession.sessionManager.abort();
    this.#streamSession?.sessionManager.abort();
  }
}
