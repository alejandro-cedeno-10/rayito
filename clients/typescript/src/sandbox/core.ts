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
import { abortReasonOr, raceAbort } from "../abort.js";
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
import { LifecycleService, TimeoutMode } from "../gen/rayito/v1/lifecycle_pb.js";
import { ProcessService } from "../gen/rayito/v1/process_pb.js";
import { PtyService } from "../gen/rayito/v1/pty_pb.js";
import { defineHidden } from "../hidden.js";
import { DEFAULT_PORT, SUSPENDED_STATES, TERMINAL_STATES } from "../limits.js";
import type { Logger } from "../logger.js";
import type { ResolvedS3Staging, SandboxInfo, SandboxLifecycle } from "../models.js";
import {
  isNotYetReachable,
  isProxyForbidden,
  isReconnectable,
  isSandboxTimeout,
  isStreamReset,
  translateRpcError,
} from "../transport/errors.js";
import { proxyAuthInterceptor } from "../transport/headers.js";
import type { TokenRefresher } from "../transport/tokens.js";
import {
  type OpenedTransport,
  openGzipTransport,
  openTransport,
  type TransportSettings,
} from "../transport/transport.js";
import { STREAM_PROBE_TIMEOUT_MS, streamFailureError } from "./commands.js";
import { DeadlineTrigger } from "./deadline-trigger.js";
import {
  autoResumeReopenMs,
  lifecycleFromProto,
  pauseTriggerDelayMs,
  setTimeoutRequest,
} from "./lifecycle.js";
import {
  CLOCK_OFFSET_WARN_MS,
  closedDuringReconnect,
  formatSeconds,
  type GuestFacts,
  guestFactsFromHealth,
  healthReady,
  healthReconnected,
  isSuspendingReason,
  metadataFromHealth,
  notReadyError,
  ReadinessPoll,
  type ReconnectOutcome,
  ReconnectPoll,
  reconnectFailure,
  terminatedDuringBootError,
  UNKNOWN_GUEST_FACTS,
} from "./readiness.js";
import type { S3ClientOverrides } from "./transfer.js";

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
  /** El `AbortSignal` del caller: abortarlo cancela el stream y rechaza con su `reason`. */
  readonly signal?: AbortSignal | undefined;
}

/**
 * Enlaza el `signal` del caller con el `AbortController` de un stream:
 * abortar uno aborta el otro con el mismo `reason`, y el listener se suelta
 * cuando el stream termina (su controller se aborta al liberarlo).
 */
export function linkAbortSignal(
  signal: AbortSignal | undefined,
  controller: AbortController,
): void {
  if (signal === undefined) {
    return;
  }
  const onAbort = () => controller.abort(signal.reason);
  signal.addEventListener("abort", onAbort, { once: true });
  controller.signal.addEventListener("abort", () => signal.removeEventListener("abort", onAbort), {
    once: true,
  });
}

/**
 * Suelta el timer del deadline de un server-stream en cuanto se aborta su
 * controller. En `@connectrpc/connect` 2.x el `setTimeout` de `timeoutMs`
 * (con ref) solo se limpia cuando la respuesta llega a `done` o cuando un
 * `next()` encuentra la llamada abortada; abortar sin volver a leer —lo que
 * hace todo consumidor que ya tiene su `EndEvent`, o que se rinde— lo deja
 * vivo hasta el deadline, y con él el proceso de Node (315 s tras un
 * `runCode`, 65 s tras un `commands.run`). Al abortar se pide un `next()`
 * más, que connect contesta limpiando el timer (o con `done` si el stream ya
 * había terminado); ese resultado se guarda para el primer `next()` posterior
 * del consumidor, que ve lo mismo que habría visto sin este drenaje.
 */
export function drainOnAbort<T>(iterator: AsyncIterator<T>, signal: AbortSignal): AsyncIterator<T> {
  let drained: Promise<IteratorResult<T>> | undefined;
  signal.addEventListener(
    "abort",
    () => {
      drained = iterator.next();
      drained.catch(() => undefined);
    },
    { once: true },
  );
  return {
    next: () => {
      const pending = drained;
      if (pending === undefined) {
        return iterator.next();
      }
      drained = undefined;
      return pending;
    },
  };
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

/** `CallOptions` de una unaria: el deadline y, si lo hay, el `signal` del caller. */
export function callOptions(timeoutMs: number, signal: AbortSignal | undefined): CallOptions {
  return signal === undefined ? { timeoutMs } : { timeoutMs, signal };
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

/** El disparador vuelve a armarse sólo si `rayd` movió el plazo a un instante futuro. */
function stillActiveAhead(lifecycle: SandboxLifecycle | undefined): boolean {
  return (
    lifecycle?.phase === "active" &&
    lifecycle.deadline !== undefined &&
    lifecycle.deadline.getTime() > Date.now()
  );
}

/** Sólo el nombre de la clase: el mensaje de un error de red o del plano nunca va al log del disparador. */
function errorName(error: unknown): string {
  return error instanceof Error ? error.name : typeof error;
}

export class SandboxCore {
  info: SandboxInfo;
  /** La `SandboxInfo` con la que se abrió el handle, nunca refrescada. */
  readonly launchInfo: SandboxInfo;
  declare readonly accessToken: string;
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
  /** El último plazo lógico leído de `Health` o de `SetTimeout`; `undefined` en un agente anterior a M9. */
  lifecycle: SandboxLifecycle | undefined;
  /** Versión, CPUs y memoria del guest del último `Health`, se lea en el arranque, en `getHealth` o al reconectar. */
  guestFacts: GuestFacts = UNKNOWN_GUEST_FACTS;
  /** Los metadatos de `create({ metadata })` del último `Health`; `undefined` hasta leer uno. */
  metadata: Readonly<Record<string, string>> | undefined;
  /** El bucket de transferencias de `create`/`connect({ transfer })`; `undefined` sin staging. */
  transfer: ResolvedS3Staging | undefined;
  /** Configuración extra de los clientes S3 del SDK: sólo la fijan los tests, hacia un S3 falso local. */
  s3ClientOverrides: S3ClientOverrides | undefined;

  readonly unaryTransport: Transport;
  readonly #unarySession: OpenedTransport;
  #streamSession: OpenedTransport | undefined;
  #gzipFilesystem: Client<typeof FilesystemService> | undefined;
  readonly #unaryClients = new Map<string, unknown>();
  readonly #streamClients = new Map<string, unknown>();
  readonly clients: {
    readonly health: Client<typeof HealthService>;
    readonly process: Client<typeof ProcessService>;
    readonly filesystem: Client<typeof FilesystemService>;
    readonly code: Client<typeof CodeService>;
    readonly pty: Client<typeof PtyService>;
    readonly lifecycle: Client<typeof LifecycleService>;
  };

  readonly #liveStreams = new Set<AbortController>();
  readonly #watches = new Set<Abortable>();
  #reconnectLock: Promise<void> | undefined;
  #resumed: Deferred = deferred();
  readonly #deadlineTrigger = new DeadlineTrigger(() => this.#onDeadline());
  #deadlineFiring = false;
  #deadlinePauseGeneration: number | undefined;
  #reopenInFlight: Promise<boolean> | undefined;
  #reopens = 0;

  constructor(init: SandboxCoreInit) {
    this.info = init.info;
    this.launchInfo = init.info;
    defineHidden(this, "accessToken", init.accessToken);
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
      lifecycle: this.clientFor(LifecycleService, false),
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

  get deadlineTriggerArmed(): boolean {
    return this.#deadlineTrigger.armed;
  }

  #openTransport(): OpenedTransport {
    return openTransport(this.info.endpoint, this.transportSettings, this.#proxyInterceptor());
  }

  #proxyInterceptor() {
    return proxyAuthInterceptor(this.refresher.store, {
      port: DEFAULT_PORT,
      accessToken: this.accessToken,
      extraHeaders: this.transportSettings.extraHeaders,
    });
  }

  /**
   * `FilesystemService` con los mensajes del cliente comprimidos con gzip
   * (`write({ gzip: true })`), creado en el primer uso sobre la sesión de los
   * unarios: nunca abre una tercera conexión HTTP/2.
   */
  gzipFilesystemClient(): Client<typeof FilesystemService> {
    this.#gzipFilesystem ??= createClient(
      FilesystemService,
      openGzipTransport(
        this.info.endpoint,
        this.transportSettings,
        this.#proxyInterceptor(),
        this.#unarySession,
      ),
    );
    return this.#gzipFilesystem;
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
  callUnary<T>(call: () => Promise<T>): Promise<T> {
    return this.#callUnary(call, true);
  }

  /**
   * `reopen: false` es el `SetTimeout` de la propia reapertura tras la pausa
   * del plazo: pasa por la reconexión como cualquier unaria pero no puede
   * disparar otra reapertura.
   */
  async #callUnary<T>(call: () => Promise<T>, reopen: boolean): Promise<T> {
    const seenGeneration = this.resumeGeneration;
    const seenReopens = this.#reopens;
    let reason: ConnectError;
    try {
      return await this.callUnaryOnce(call);
    } catch (error) {
      if (reopen && (await this.#reopenedAfterDeadlinePause(error, seenReopens))) {
        return this.callUnaryOnce(call);
      }
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

  /**
   * La unaria con la tabla de errores. Con `signal`, uno ya abortado rechaza
   * antes de enviar nada y abortarlo durante la llamada rechaza con su
   * `reason` (el `Canceled` de Connect nunca llega al caller).
   */
  translatedUnary<T>(
    call: () => Promise<T>,
    options: {
      readonly filesystem?: boolean | undefined;
      readonly signal?: AbortSignal | undefined;
    } = {},
  ): Promise<T> {
    return this.signalledUnary(call, options.signal, (error) =>
      translateRpcError(error, { filesystem: options.filesystem }),
    );
  }

  /**
   * `callUnary` con `signal` y una tabla de errores propia: uno ya abortado
   * rechaza antes de enviar nada y abortarlo durante la llamada rechaza con
   * su `reason` sin pasar por `translate`.
   */
  async signalledUnary<T>(
    call: () => Promise<T>,
    signal: AbortSignal | undefined,
    translate: (error: unknown) => unknown,
  ): Promise<T> {
    signal?.throwIfAborted();
    try {
      return await this.callUnary(call);
    } catch (error) {
      throw abortReasonOr(signal, translate(error));
    }
  }

  resolveRequestTimeout(requestTimeoutMs: number | undefined): number {
    return requestTimeoutMs ?? this.requestTimeoutMs;
  }

  processCall<T>(
    invoke: UnaryInvoker<typeof ProcessService, T>,
    requestTimeoutMs: number | undefined,
    signal?: AbortSignal,
  ): Promise<T> {
    const timeoutMs = this.resolveRequestTimeout(requestTimeoutMs);
    return this.translatedUnary(
      () => invoke(this.clients.process, callOptions(timeoutMs, signal)),
      {
        signal,
      },
    );
  }

  ptyCall<T>(
    invoke: UnaryInvoker<typeof PtyService, T>,
    requestTimeoutMs: number | undefined,
    signal?: AbortSignal,
  ): Promise<T> {
    const timeoutMs = this.resolveRequestTimeout(requestTimeoutMs);
    return this.translatedUnary(() => invoke(this.clients.pty, callOptions(timeoutMs, signal)), {
      signal,
    });
  }

  filesCall<T>(
    invoke: UnaryInvoker<typeof FilesystemService, T>,
    requestTimeoutMs: number | undefined,
    signal?: AbortSignal,
  ): Promise<T> {
    const timeoutMs = this.resolveRequestTimeout(requestTimeoutMs);
    return this.translatedUnary(
      () => invoke(this.clients.filesystem, callOptions(timeoutMs, signal)),
      { filesystem: true, signal },
    );
  }

  codeCall<T>(
    invoke: UnaryInvoker<typeof CodeService, T>,
    requestTimeoutMs: number | undefined,
    defaultTimeoutMs?: number,
    signal?: AbortSignal,
  ): Promise<T> {
    const timeoutMs =
      requestTimeoutMs === undefined && defaultTimeoutMs !== undefined
        ? defaultTimeoutMs
        : this.resolveRequestTimeout(requestTimeoutMs);
    return this.translatedUnary(() => invoke(this.clients.code, callOptions(timeoutMs, signal)), {
      signal,
    });
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
    options.signal?.throwIfAborted();
    try {
      return await this.#openStream(start, options);
    } catch (error) {
      throw abortReasonOr(options.signal, error);
    }
  }

  async #openStream<S extends DescService, T>(
    start: StreamStarter<S, T>,
    options: OpenStreamOptions<S>,
  ): Promise<OpenedStream<T>> {
    const client = this.clientFor(options.service, options.stream);
    const seenGeneration = this.resumeGeneration;
    const seenReopens = this.#reopens;
    const reconnect = options.reconnect ?? true;
    const allowEmpty = options.allowEmpty ?? false;
    let failure: unknown;
    try {
      return await this.#firstMessageReminting(start, client, allowEmpty, options.signal);
    } catch (error) {
      failure = error;
    }
    if (!(await this.#reopenedAfterDeadlinePause(failure, seenReopens))) {
      if (!(reconnect && this.isReconnectable(failure))) {
        throw await this.#openFailure(failure, options);
      }
      const reason = failure as ConnectError;
      const outcome = await this.reconnect(reason, seenGeneration, { wake: true });
      if (!outcome.resumed) {
        throw this.reconnectError(outcome, reason);
      }
    }
    try {
      return await this.#firstMessage(start, client, allowEmpty, options.signal);
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
    signal: AbortSignal | undefined,
  ): Promise<OpenedStream<T>> {
    try {
      return await this.#firstMessage(start, client, allowEmpty, signal);
    } catch (error) {
      if (!isProxyForbidden(error)) {
        throw error;
      }
    }
    this.logger?.info?.("el proxy rechazó el token del sandbox al abrir un stream; reacuñando", {
      sandboxId: this.sandboxId,
    });
    await this.refresher.refreshAll();
    return this.#firstMessage(start, client, allowEmpty, signal);
  }

  async #firstMessage<S extends DescService, T>(
    start: StreamStarter<S, T>,
    client: Client<S>,
    allowEmpty: boolean,
    signal: AbortSignal | undefined,
  ): Promise<OpenedStream<T>> {
    const controller = new AbortController();
    linkAbortSignal(signal, controller);
    const iterator = drainOnAbort(
      start(client, { signal: controller.signal })[Symbol.asyncIterator](),
      controller.signal,
    );
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

  async probeHealth(timeoutMs: number, signal?: AbortSignal): Promise<HealthResponse | undefined> {
    signal?.throwIfAborted();
    try {
      return await this.callUnaryOnce(() =>
        this.clients.health.health(
          create(HealthRequestSchema, {}),
          withTimeout(signal === undefined ? {} : { signal }, timeoutMs),
        ),
      );
    } catch (error) {
      if (!signal?.aborted && isNotYetReachable(error)) {
        return undefined;
      }
      throw abortReasonOr(signal, translateRpcError(error));
    }
  }

  // -------------------------------------------------------------- readiness

  /**
   * `readiness` es el calendario del sondeo: `create()`, `connect()` y
   * `resume()` usan `ReadinessPoll`; el pool pasa `TakePoll`. `signal`
   * abortado corta el sondeo entre intentos con su `reason`.
   */
  async waitUntilReady(options: {
    readonly terminateOnFailure: boolean;
    readonly readiness?: typeof ReadinessPoll | undefined;
    readonly signal?: AbortSignal | undefined;
  }): Promise<HealthResponse> {
    const Poll = options.readiness ?? ReadinessPoll;
    const poll = new Poll({ timeoutMs: this.readyTimeoutMs });
    while (true) {
      options.signal?.throwIfAborted();
      const response = await raceAbort(this.probeHealth(poll.rpcTimeoutMs()), options.signal);
      if (healthReady(response)) {
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
      const delay = sleep(poll.nextDelayMs());
      try {
        await raceAbort(delay.promise, options.signal);
      } finally {
        delay.cancel();
      }
    }
  }

  recordHealth(response: HealthResponse): void {
    this.recordLifecycle(lifecycleFromProto(response.lifecycle));
    this.guestFacts = guestFactsFromHealth(response);
    this.metadata = metadataFromHealth(response);
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

  // ---------------------------------------------------------------- deadline

  /**
   * Todo plazo leído (de `Health` o de `SetTimeout`) rearma el disparador del
   * modo `pause` (design D7), salvo mientras el propio disparador corre: su
   * `Health` decide al terminar si hay que rearmar.
   */
  recordLifecycle(lifecycle: SandboxLifecycle | undefined): void {
    this.lifecycle = lifecycle;
    if (this.closed || this.#deadlineFiring) {
      return;
    }
    this.#deadlineTrigger.arm(pauseTriggerDelayMs(lifecycle, Date.now()));
  }

  /**
   * `rayd` sigue siendo la fuente de verdad: nunca sondea `Health` si
   * `get-microvm` no dice `RUNNING` (lo despertaría), sólo suspende si
   * `Health` dice `expired` y rearma si sigue `active` con un plazo futuro.
   * Cualquier fallo se registra y se traga: la política de idle de la
   * plataforma es el respaldo. El log nunca lleva el token ni el JWE.
   */
  async #onDeadline(): Promise<void> {
    this.#deadlineFiring = true;
    try {
      await this.#suspendIfExpired();
    } catch (error) {
      this.logger?.warn?.(
        "el disparador del plazo falló; la política de idle de la plataforma lo suspenderá",
        { sandboxId: this.sandboxId, reason: errorName(error) },
      );
    } finally {
      this.#deadlineFiring = false;
    }
    if (!this.closed && stillActiveAhead(this.lifecycle)) {
      this.#deadlineTrigger.arm(pauseTriggerDelayMs(this.lifecycle, Date.now()));
    }
  }

  async #suspendIfExpired(): Promise<void> {
    if (this.closed) {
      return;
    }
    this.info = await this.controlPlane.getMicrovm(this.sandboxId);
    if (this.info.state !== "RUNNING") {
      return;
    }
    const response = await this.callUnaryOnce(() =>
      this.clients.health.health(
        create(HealthRequestSchema, {}),
        withTimeout({}, ReadinessPoll.maxRpcTimeoutMs),
      ),
    );
    this.recordHealth(response);
    if (this.lifecycle?.phase === "expired") {
      await this.#suspendForDeadline();
    }
  }

  /**
   * `suspend-microvm` (el bucket de 2 TPS del plano) sin la marca de pausa
   * pendiente: la próxima petición auto-reanuda el sandbox igual que tras una
   * suspensión por idle (AWS_API_NOTES.md Q40).
   */
  async #suspendForDeadline(): Promise<void> {
    const generation = this.resumeGeneration;
    const suspended = await this.controlPlane.suspendMicrovm(this.sandboxId);
    if (suspended) {
      this.#deadlinePauseGeneration = generation;
    }
    this.logger?.info?.("plazo lógico vencido en modo pause: suspend-microvm", {
      sandboxId: this.sandboxId,
      accepted: suspended,
    });
  }

  /**
   * Tras una suspensión por el plazo de este cliente, el primer
   * `sandbox_timeout` de un sandbox ya reanudado aplica la regla del
   * auto-resume con `SetTimeout` (`autoResumeReopenMs`): `rayd` no la aplica
   * si la congelación duró menos de 2 s. Una sola reapertura por suspensión,
   * compartida: los callers concurrentes esperan la misma promesa y quien
   * llega tras una reapertura hecha desde que empezó su llamada
   * (`seenReopens`) reintenta sin otro `SetTimeout`. Si no toca o falla, el
   * caller ve el error original.
   */
  async #reopenedAfterDeadlinePause(error: unknown, seenReopens: number): Promise<boolean> {
    if (this.closed || !isSandboxTimeout(error)) {
      return false;
    }
    if (this.#reopens > seenReopens) {
      return true;
    }
    if (this.#reopenInFlight === undefined) {
      const pausedGeneration = this.#deadlinePauseGeneration;
      if (pausedGeneration === undefined) {
        return false;
      }
      const inFlight = this.#reopenAfterDeadlinePause(pausedGeneration);
      this.#reopenInFlight = inFlight;
      void inFlight.finally(() => {
        if (this.#reopenInFlight === inFlight) {
          this.#reopenInFlight = undefined;
        }
      });
    }
    return this.#reopenInFlight;
  }

  /**
   * El cuerpo de la reapertura. La marca sólo se consume cuando la
   * `resumeGeneration` ya avanzó (un `sandbox_timeout` anterior a la
   * congelación no la gasta) y el `SetTimeout`, por la unaria que reconecta,
   * respondió. El log nunca lleva el token.
   */
  async #reopenAfterDeadlinePause(pausedGeneration: number): Promise<boolean> {
    try {
      const response = await this.probeHealth(
        Math.min(ReadinessPoll.maxRpcTimeoutMs, this.requestTimeoutMs),
      );
      if (response !== undefined) {
        this.recordHealth(response);
      }
      if (this.resumeGeneration <= pausedGeneration) {
        return false;
      }
      const timeoutMs = autoResumeReopenMs(this.lifecycle, {
        pausedGeneration,
        generation: this.resumeGeneration,
        nowUnixMs: Date.now(),
      });
      if (timeoutMs === undefined) {
        this.#consumeDeadlinePause(pausedGeneration);
        return false;
      }
      const state = await this.#callUnary(
        () =>
          this.clients.lifecycle.setTimeout(
            setTimeoutRequest(TimeoutMode.EXACT, timeoutMs),
            withTimeout({}, this.requestTimeoutMs),
          ),
        false,
      );
      this.recordLifecycle(lifecycleFromProto(state));
      this.#consumeDeadlinePause(pausedGeneration);
      this.#reopens += 1;
      this.logger?.info?.("reanudado tras la pausa del plazo; plazo reabierto", {
        sandboxId: this.sandboxId,
        timeoutMs,
      });
      return true;
    } catch (failure) {
      this.logger?.warn?.("no se pudo reabrir tras la pausa del plazo", {
        sandboxId: this.sandboxId,
        reason: errorName(failure),
      });
      return false;
    }
  }

  /** Una suspensión posterior del disparador pone su propia marca: sólo se borra la de esta. */
  #consumeDeadlinePause(pausedGeneration: number): void {
    if (this.#deadlinePauseGeneration === pausedGeneration) {
      this.#deadlinePauseGeneration = undefined;
    }
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
    this.#deadlineTrigger.cancel();
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
