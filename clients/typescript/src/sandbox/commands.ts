/**
 * `sandbox.commands`: construcción de `StartRequest`, aritmética de
 * deadlines, decodificación incremental de la salida, el estado de un
 * `CommandHandle` (y de un `PtyHandle`, a través de un `StreamAdapter`), el
 * mapeo de `EndEvent` a resultado o error y el contrato de reconexión de un
 * handle (`Connect(pid, fromSeq = lastSeq + 1)`).
 */

import { create } from "@bufbuild/protobuf";
import { Code, ConnectError } from "@connectrpc/connect";
import {
  CommandExitError,
  InvalidArgumentError,
  NotFoundError,
  SandboxError,
  SandboxStateError,
  TimeoutError,
} from "../errors.js";
import { UserSchema } from "../gen/rayito/v1/common_pb.js";
import type { MetricsResponse } from "../gen/rayito/v1/health_pb.js";
import {
  CloseStdinRequestSchema,
  ConnectRequestSchema,
  type DataEvent,
  type EndEvent,
  ListRequestSchema,
  ProcessConfigSchema,
  type ProcessEvent,
  type ProcessInfo as ProcessInfoProto,
  ProcessKind,
  ProcessService,
  SendInputRequestSchema,
  SendSignalRequestSchema,
  type StartRequest,
  StartRequestSchema,
} from "../gen/rayito/v1/process_pb.js";
import { COMMAND_OUTPUT_MAX_BYTES } from "../limits.js";
import type { CommandResult, OutputChunk, ProcessInfo, SandboxMetrics } from "../models.js";
import { type Instrumentation, NOOP } from "../otel.js";
import { validatedEnvs } from "../payload.js";
import type { SecretEnvs, SecretsInput } from "../secrets/inject.js";
import { sandboxTimeoutError, translateStreamError } from "../transport/errors.js";
import { type OpenedStream, type SandboxCore, type StreamStarter, withTimeout } from "./core.js";
import { ReconnectBudget } from "./readiness.js";

export { STREAM_PROBE_TIMEOUT_MS, streamFailureError } from "./stream-errors.js";

export const DEFAULT_COMMAND_TIMEOUT_MS = 60_000;
export const STREAM_DEADLINE_GRACE_MS = 5000;
export const SIGKILL = 9;
export const SHELL = "/bin/bash";
export const SHELL_ARGS = ["-l", "-c"] as const;
export const PID_MAX = 2 ** 32 - 1;

export const STATUS_EXITED = "exited";
export const STATUS_SIGNALED = "signaled";
export const STATUS_TIMEOUT = "timeout";
export const STATUS_SUSPENDING = "suspending";
export const STATUS_OUTPUT_TRUNCATED = "output_truncated";
export const STATUS_SANDBOX_TIMEOUT = "sandbox_timeout";

export type OutputCallback = (text: string) => void;
export type CommandOutcome = CommandResult | Error;
export type Monotonic = () => number;

export interface RequestOptions {
  readonly requestTimeoutMs?: number | undefined;
  /**
   * Cancela la llamada (unaria o stream): uno ya abortado rechaza sin enviar
   * nada y abortarlo después rechaza con `signal.reason` (por defecto un
   * `DOMException` `AbortError`, la convención de `fetch`), sin envolverlo.
   */
  readonly signal?: AbortSignal | undefined;
}

export interface CommandOptions extends RequestOptions {
  readonly background?: boolean | undefined;
  readonly envs?: Readonly<Record<string, string>> | undefined;
  readonly user?: string | undefined;
  readonly cwd?: string | undefined;
  readonly onStdout?: OutputCallback | undefined;
  readonly onStderr?: OutputCallback | undefined;
  readonly stdin?: boolean | undefined;
  /** Timeout del servidor en ms (60 000 por defecto; `0` = sin límite). */
  readonly timeoutMs?: number | undefined;
  readonly tag?: string | undefined;
  /**
   * Tope de la salida guardada por descriptor (la del resultado y la del
   * handle), en bytes: `COMMAND_OUTPUT_MAX_BYTES` (64 MiB) por defecto. Lo
   * más antiguo se descarta y `truncated` lo indica; `0` no guarda nada.
   * `onStdout`/`onStderr` reciben siempre todo.
   */
  readonly maxOutputBytes?: number | undefined;
  /**
   * Secretos de Secrets Manager como variables de entorno de este comando
   * (más los del handle; la llamada gana en una clave repetida y una clave que
   * también esté en `envs` es `InvalidArgumentError`).
   *
   * Coste y activación
   * -------------------
   * Activa: la inyección de secretos en este comando (apagada si falta).
   * Recursos y llamadas AWS: `GetSecretValueCommand` sólo en un fallo de
   *   `SecretCache` (TTL 300 s): tres comandos seguidos = una lectura.
   * Coste aproximado: $0,05 por 10 000 llamadas (us-east-1, 2026-09-30).
   * IAM: `secretsmanager:GetSecretValue` en las credenciales del llamante.
   * Cómo apagarla: no pases `secrets` (por defecto `undefined`).
   * Ejemplo:
   *   await sbx.commands.run("python agent.py", { secrets: { OPENAI_API_KEY: "openai" } });
   */
  readonly secrets?: SecretsInput | undefined;
}

export interface ConnectOptions extends RequestOptions {
  readonly fromSeq?: number | undefined;
  readonly onStdout?: OutputCallback | undefined;
  readonly onStderr?: OutputCallback | undefined;
  /** Deadline del stream en ms (`undefined`/`0` = sin deadline). */
  readonly timeoutMs?: number | undefined;
  /** Tope de la salida guardada, como en `CommandOptions.maxOutputBytes`. */
  readonly maxOutputBytes?: number | undefined;
}

// ------------------------------------------------------------------ requests

export interface StartRequestInput {
  readonly envs?: Readonly<Record<string, string>> | undefined;
  readonly user?: string | undefined;
  readonly cwd?: string | undefined;
  readonly stdin?: boolean | undefined;
  readonly timeoutMs?: number | undefined;
  readonly tag?: string | undefined;
}

/**
 * `ProcessConfig{cmd:"/bin/bash", args:["-l","-c", cmd]}` con el `timeout_ms`
 * que impone el servidor. `user=""` y `cwd=""` se omiten; el servidor resuelve
 * los defaults del `/run` payload.
 */
export function buildStartRequest(cmd: string, input: StartRequestInput = {}): StartRequest {
  if (typeof cmd !== "string" || cmd.trim().length === 0) {
    throw new InvalidArgumentError("cmd no puede estar vacío");
  }
  const config = create(ProcessConfigSchema, { cmd: SHELL, args: [...SHELL_ARGS, cmd] });
  if (input.envs !== undefined && Object.keys(input.envs).length > 0) {
    config.envs = validatedEnvs(input.envs);
  }
  if (input.cwd) {
    config.cwd = input.cwd;
  }
  const request = create(StartRequestSchema, {
    process: config,
    timeoutMs: BigInt(timeoutToMs(input.timeoutMs ?? DEFAULT_COMMAND_TIMEOUT_MS)),
    stdin: Boolean(input.stdin),
  });
  if (input.user) {
    request.user = create(UserSchema, { username: input.user });
  }
  if (input.tag !== undefined) {
    request.tag = input.tag;
  }
  return request;
}

/** `undefined` y `0` significan sin límite; negativo o no numérico es un error del caller. */
export function timeoutToMs(timeoutMs: number | undefined): number {
  if (timeoutMs === undefined) {
    return 0;
  }
  if (typeof timeoutMs !== "number" || Number.isNaN(timeoutMs)) {
    throw new InvalidArgumentError(
      `timeoutMs debe ser un número de milisegundos, recibido ${String(timeoutMs)}`,
    );
  }
  if (timeoutMs < 0) {
    throw new InvalidArgumentError(`timeoutMs no puede ser negativo, recibido ${timeoutMs}`);
  }
  if (timeoutMs === 0) {
    return 0;
  }
  return Math.max(1, Math.round(timeoutMs));
}

/** Deadline gRPC del stream: `timeoutMs + 5 s`, para que el `EndEvent` del servidor llegue antes. */
export function streamDeadlineMs(timeoutMs: number | undefined): number | undefined {
  const ms = timeoutToMs(timeoutMs);
  return ms === 0 ? undefined : ms + STREAM_DEADLINE_GRACE_MS;
}

/** Instante monotónico en el que vence un deadline; `undefined` si no hay. */
export function deadlineAt(deadlineMs: number | undefined, now: Monotonic): number | undefined {
  return deadlineMs === undefined ? undefined : now() + deadlineMs;
}

/**
 * Lo que le queda a un stream re-emitido tras una reconexión. El deadline del
 * cliente corre en el reloj del cliente (una pausa no lo alarga); `0`
 * significa que ya venció.
 */
export function remainingDeadlineMs(at: number | undefined, now: Monotonic): number | undefined {
  return at === undefined ? undefined : Math.max(0, Math.ceil(at - now()));
}

export function validatePid(pid: unknown): number {
  if (typeof pid !== "number" || !Number.isInteger(pid) || pid < 1 || pid > PID_MAX) {
    throw new InvalidArgumentError(
      `pid debe ser un entero entre 1 y ${PID_MAX}, recibido ${String(pid)}`,
    );
  }
  return pid;
}

export function validateFromSeq(fromSeq: unknown): number {
  if (typeof fromSeq !== "number" || !Number.isInteger(fromSeq) || fromSeq < 0) {
    throw new InvalidArgumentError(`fromSeq debe ser un entero >= 0, recibido ${String(fromSeq)}`);
  }
  return fromSeq;
}

export function encodeStdin(data: string | Uint8Array): Uint8Array {
  if (typeof data === "string") {
    return new TextEncoder().encode(data);
  }
  if (data instanceof Uint8Array) {
    return data;
  }
  throw new InvalidArgumentError(`stdin acepta string o Uint8Array, recibido ${typeof data}`);
}

/** El primer mensaje de `Start` y `Connect` es siempre `StartEvent{pid}`. */
export function pidFromStartEvent(event: ProcessEvent | undefined): number {
  if (event === undefined || event.event.case !== "start") {
    throw new SandboxError(
      `el stream no empezó con StartEvent (llegó ${JSON.stringify(event?.event.case ?? null)})`,
    );
  }
  return event.event.value.pid;
}

// -------------------------------------------------------------------- output

/** `maxOutputBytes`: un entero >= 0 (`0` no guarda nada; la salida sólo llega a los callbacks). */
export function validateMaxOutputBytes(value: number): number {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new InvalidArgumentError(`maxOutputBytes debe ser un entero >= 0: ${String(value)}`);
  }
  return value;
}

/**
 * Un stream de bytes decodificado incrementalmente a texto (UTF-8 con
 * reemplazo). Guarda como mucho `maxBytes` bytes recibidos (la cola: lo más
 * antiguo se descarta y cuenta en `droppedBytes`), para que un proceso del
 * sandbox que escribe sin parar no agote la memoria del cliente. El callback
 * recibe siempre todo. Espejo de `DecodedStream` de `_process_base.py`.
 */
export class DecodedStream {
  readonly #decoder = new TextDecoder("utf-8", { fatal: false });
  readonly #callback: OutputCallback | undefined;
  readonly #maxBytes: number;
  #parts: Array<{ text: string; size: number }> = [];
  #keptBytes = 0;
  #pendingBytes = 0;
  #droppedBytes = 0;

  constructor(callback: OutputCallback | undefined, maxBytes: number = COMMAND_OUTPUT_MAX_BYTES) {
    this.#callback = callback;
    this.#maxBytes = maxBytes;
  }

  get text(): string {
    return this.#parts.map((part) => part.text).join("");
  }

  get droppedBytes(): number {
    return this.#droppedBytes;
  }

  get truncated(): boolean {
    return this.#droppedBytes > 0;
  }

  feed(payload: Uint8Array): string | undefined {
    this.#pendingBytes += payload.length;
    return this.#emit(this.#decoder.decode(payload, { stream: true }));
  }

  flush(): void {
    this.#emit(this.#decoder.decode());
  }

  #emit(text: string): string | undefined {
    if (text.length === 0) {
      return undefined;
    }
    const size = this.#pendingBytes;
    this.#pendingBytes = 0;
    this.#parts.push({ text, size });
    this.#keptBytes += size;
    this.#trim();
    this.#callback?.(text);
    return text;
  }

  /** Descarta por delante hasta volver a `maxBytes`; si el exceso cae dentro de una pieza, se queda con su final. */
  #trim(): void {
    let dropFrom = 0;
    while (this.#keptBytes > this.#maxBytes) {
      const oldest = this.#parts[dropFrom];
      if (oldest === undefined) {
        break;
      }
      const overflow = this.#keptBytes - this.#maxBytes;
      if (oldest.size <= overflow) {
        this.#keptBytes -= oldest.size;
        this.#droppedBytes += oldest.size;
        dropFrom += 1;
        continue;
      }
      const tail = Buffer.from(oldest.text, "utf8").subarray(overflow);
      this.#parts[dropFrom] = {
        text: new TextDecoder().decode(tail).replace(/^\uFFFD+/, ""),
        size: oldest.size - overflow,
      };
      this.#keptBytes -= overflow;
      this.#droppedBytes += overflow;
    }
    if (dropFrom > 0) {
      this.#parts = this.#parts.slice(dropFrom);
    }
  }
}

/**
 * Salida acumulada de un proceso: un `DecodedStream` por descriptor, el
 * último `seq` visto (para `Connect(fromSeq)`) y el cierre en `finish`.
 */
export class OutputAccumulator {
  readonly #stdout: DecodedStream;
  readonly #stderr: DecodedStream;
  #lastSeq = 0;

  constructor(
    options: {
      onStdout?: OutputCallback | undefined;
      onStderr?: OutputCallback | undefined;
      maxBytes?: number | undefined;
    } = {},
  ) {
    const maxBytes = options.maxBytes ?? COMMAND_OUTPUT_MAX_BYTES;
    this.#stdout = new DecodedStream(options.onStdout, maxBytes);
    this.#stderr = new DecodedStream(options.onStderr, maxBytes);
  }

  /** Si se descartó salida antigua de stdout o de stderr por el tope. */
  get truncated(): boolean {
    return this.#stdout.truncated || this.#stderr.truncated;
  }

  get lastSeq(): number {
    return this.#lastSeq;
  }

  get stdout(): string {
    return this.#stdout.text;
  }

  get stderr(): string {
    return this.#stderr.text;
  }

  /** Decodifica un `DataEvent`; devuelve el texto nuevo de cada stream. */
  feed(data: DataEvent): { stdout: string | undefined; stderr: string | undefined } {
    this.#lastSeq = Math.max(this.#lastSeq, Number(data.seq));
    if (data.output.case === "stderr") {
      return { stdout: undefined, stderr: this.#stderr.feed(data.output.value) };
    }
    const payload = data.output.case === "stdout" ? data.output.value : new Uint8Array();
    return { stdout: this.#stdout.feed(payload), stderr: undefined };
  }

  /** Bytes crudos de una PTY: van al decoder de stdout y avanzan `lastSeq`. */
  feedTerminal(seq: number, payload: Uint8Array): string | undefined {
    this.#lastSeq = Math.max(this.#lastSeq, seq);
    return this.#stdout.feed(payload);
  }

  finish(end: EndEvent): CommandOutcome {
    this.#stdout.flush();
    this.#stderr.flush();
    return outcomeFromEnd(end, this.stdout, this.stderr, this.truncated);
  }
}

/**
 * Tabla cerrada de `EndEvent.status`: `exited`/`signaled` con exit 0 →
 * `CommandResult`; distinto de cero → `CommandExitError`; `timeout` →
 * `TimeoutError`; `output_truncated` → `SandboxError`; `suspending` →
 * `SandboxStateError`; `sandbox_timeout` (el plazo lógico del sandbox venció,
 * ADR-011) → `TimeoutError`, terminal. Un status desconocido con `error` sigue
 * la tabla de `StreamError.code`.
 */
export function outcomeFromEnd(
  end: EndEvent,
  stdout: string,
  stderr: string,
  truncated = false,
): CommandOutcome {
  const status = end.status;
  const exitCode = end.exitCode;
  const detail = endErrorMessage(end);
  if (status === STATUS_SANDBOX_TIMEOUT) {
    return sandboxTimeoutError();
  }
  if (status === STATUS_TIMEOUT) {
    return new TimeoutError(
      `el comando superó su timeout y fue terminado por el agente ` +
        `(exitCode=${exitCode}, signal=${end.signal ?? 0}): ${detail}`,
    );
  }
  if (status === STATUS_OUTPUT_TRUNCATED) {
    return new SandboxError(
      `output_truncated: el agente descartó este suscriptor (${detail}); el proceso ` +
        "sigue vivo, reconecta con commands.connect(pid, { fromSeq: lastSeq + 1 })",
    );
  }
  if (status === STATUS_SUSPENDING) {
    return new SandboxStateError(`el sandbox se está suspendiendo: ${detail}`);
  }
  if (status !== STATUS_EXITED && status !== STATUS_SIGNALED && end.error !== undefined) {
    return translateStreamError(end.error.code, end.error.message);
  }
  if (exitCode === 0) {
    return Object.freeze({
      stdout,
      stderr,
      exitCode: 0,
      error: undefined,
      ...(truncated ? { truncated: true as const } : {}),
    });
  }
  return new CommandExitError(`el comando terminó con exitCode=${exitCode} (${status})`, {
    exitCode,
    stdout,
    stderr,
    error: status,
    truncated,
  });
}

export function endErrorMessage(end: EndEvent): string {
  if (end.error?.message) {
    return end.error.message;
  }
  return end.status;
}

/** Lo que expone `CommandHandle.error`: el `StreamError.code` si lo hubo, el status para un exit distinto de cero. */
export function endErrorName(end: EndEvent | undefined): string | undefined {
  if (end === undefined) {
    return undefined;
  }
  if (end.error !== undefined) {
    return end.error.code;
  }
  if (end.exitCode !== 0) {
    return end.status;
  }
  return undefined;
}

// ------------------------------------------------------------------ consumed

export interface ConsumedChunk {
  readonly kind: "chunk";
  readonly seq: number;
  readonly output: OutputChunk;
}
export interface ConsumedNothing {
  readonly kind: "nothing";
}
export interface ConsumedEnded {
  readonly kind: "ended";
  readonly end: EndEvent;
}
export interface ConsumedSuspending {
  readonly kind: "suspending";
  readonly end: EndEvent;
}
export type Consumed = ConsumedChunk | ConsumedNothing | ConsumedEnded | ConsumedSuspending;

export const NOTHING: ConsumedNothing = Object.freeze({ kind: "nothing" });

/** Traduce los mensajes de un stream (`ProcessEvent` o `PtyServerMessage`) al vocabulario de `CommandProgress`. */
export interface StreamAdapter<T> {
  pid(first: T | undefined): number;
  consume(message: T, accumulator: OutputAccumulator): Consumed;
}

export function consumedEnd(end: EndEvent): Consumed {
  if (end.status === STATUS_SUSPENDING) {
    return { kind: "suspending", end };
  }
  return { kind: "ended", end };
}

/** `StreamAdapter` de `ProcessService.Start`/`Connect`. */
export class ProcessEvents implements StreamAdapter<ProcessEvent> {
  pid(first: ProcessEvent | undefined): number {
    return pidFromStartEvent(first);
  }

  consume(message: ProcessEvent, accumulator: OutputAccumulator): Consumed {
    const event = message.event;
    if (event.case === "data") {
      const { stdout, stderr } = accumulator.feed(event.value);
      const seq = Number(event.value.seq);
      if (stdout !== undefined) {
        return { kind: "chunk", seq, output: { stdout } };
      }
      if (stderr !== undefined) {
        return { kind: "chunk", seq, output: { stderr } };
      }
      return NOTHING;
    }
    if (event.case === "end") {
      return consumedEnd(event.value);
    }
    return NOTHING;
  }
}

/**
 * Estado de un `CommandHandle`: salida acumulada, `EndEvent` recibido,
 * resultado o error final, si el handle fue desconectado a propósito y si el
 * último mensaje fue un final `suspending` pendiente de reconexión.
 */
export class CommandProgress<T = ProcessEvent> {
  readonly pid: number;
  readonly accumulator: OutputAccumulator;
  readonly adapter: StreamAdapter<T>;
  end: EndEvent | undefined;
  outcome: CommandOutcome | undefined;
  disconnected = false;
  suspended = false;

  constructor(pid: number, accumulator: OutputAccumulator, adapter: StreamAdapter<T>) {
    this.pid = pid;
    this.accumulator = accumulator;
    this.adapter = adapter;
  }

  isFinished(): boolean {
    return this.outcome !== undefined;
  }

  get exitCode(): number | undefined {
    return this.end?.exitCode;
  }

  get error(): string | undefined {
    return endErrorName(this.end);
  }

  consume(message: T): Consumed {
    const consumed = this.adapter.consume(message, this.accumulator);
    if (consumed.kind === "ended") {
      this.end = consumed.end;
      this.outcome = this.accumulator.finish(consumed.end);
    } else if (consumed.kind === "suspending") {
      this.suspended = true;
    }
    return consumed;
  }

  resubscribed(): void {
    this.suspended = false;
  }

  fail(failure: Error): Error {
    this.outcome = failure;
    return failure;
  }

  /** Resultado final tras consumir el stream; idempotente. */
  resolve(): CommandResult {
    this.outcome ??= this.#missingOutcome();
    if (this.outcome instanceof Error) {
      throw this.outcome;
    }
    return this.outcome;
  }

  #missingOutcome(): Error {
    if (this.disconnected) {
      return new SandboxError(
        `el handle del pid ${this.pid} está desconectado; vuelve con commands.connect(pid)`,
      );
    }
    return new SandboxError(`el stream del pid ${this.pid} terminó sin EndEvent`);
  }
}

export function suspendingReason(progress: CommandProgress<unknown>): SandboxStateError {
  return new SandboxStateError(
    `el sandbox se está suspendiendo; el stream del pid ${progress.pid} fue cerrado`,
  );
}

/** Tras una reconexión se pide todo lo que no se vio: `lastSeq + 1`. */
export function resubscribeFromSeq(lastSeq: number): number {
  return lastSeq + 1;
}

export function processInfoFromProto(info: ProcessInfoProto): ProcessInfo {
  const config = info.config;
  return Object.freeze({
    pid: info.pid,
    cmd: config?.cmd ?? "",
    args: Object.freeze([...(config?.args ?? [])]),
    envs: Object.freeze({ ...(config?.envs ?? {}) }),
    cwd: config?.cwd,
    tag: info.tag,
    kind: info.kind === ProcessKind.PTY ? "pty" : "process",
  });
}

export function metricsFromProto(response: MetricsResponse): SandboxMetrics {
  return Object.freeze({
    cpuUsedPct: response.cpuUsedPct,
    memUsedBytes: Number(response.memUsedBytes),
    memTotalBytes: Number(response.memTotalBytes),
    diskUsedBytes: Number(response.diskUsedBytes),
    diskTotalBytes: Number(response.diskTotalBytes),
    cpuCount: response.cpuCount,
    timestamp: new Date(Number(response.timestampUnixMs)),
    memCacheBytes: Number(response.memCacheBytes),
  });
}

// ------------------------------------------------------------------ commands

export type ProcessClient = SandboxCore["clients"]["process"];

/** Comandos del sandbox (`ProcessService`). */
export class Commands {
  readonly core: SandboxCore;
  readonly #secrets: SecretEnvs;
  readonly #instrumentation: () => Instrumentation;

  constructor(
    core: SandboxCore,
    secrets: SecretEnvs,
    instrumentation: () => Instrumentation = () => NOOP,
  ) {
    this.core = core;
    this.#secrets = secrets;
    this.#instrumentation = instrumentation;
  }

  run(cmd: string, options: CommandOptions & { background: true }): Promise<CommandHandle>;
  run(
    cmd: string,
    options?: CommandOptions & { background?: false | undefined },
  ): Promise<CommandResult>;
  run(cmd: string, options?: CommandOptions): Promise<CommandResult | CommandHandle>;
  /**
   * Con `tracerProvider` en `create()`/`connect()`, cada llamada abre un
   * span `rayito.commands.run` (nunca con el texto del comando, sus
   * argumentos ni sus `envs`): `rayito.commands.background` siempre, y en
   * foreground `rayito.commands.exit_code` al terminar (también cuando sale
   * con un código distinto de cero). En segundo plano el span se cierra en
   * cuanto `run` devuelve el handle, no cuando el proceso termina.
   */
  async run(cmd: string, options: CommandOptions = {}): Promise<CommandResult | CommandHandle> {
    const maxOutputBytes = validateMaxOutputBytes(
      options.maxOutputBytes ?? COMMAND_OUTPUT_MAX_BYTES,
    );
    const envs = await this.#secrets.apply(options.envs, options.secrets);
    const request = buildStartRequest(cmd, { ...options, envs });
    const deadline = streamDeadlineMs(options.timeoutMs ?? DEFAULT_COMMAND_TIMEOUT_MS);
    const background = options.background === true;
    return this.#instrumentation().span(
      "rayito.commands.run",
      { "rayito.commands.background": background },
      async (span) => {
        const handle = await this.attach(
          (client, callOptions) => client.start(request, withTimeout(callOptions, deadline)),
          {
            stream: background,
            deadlineMs: deadline,
            onStdout: options.onStdout,
            onStderr: options.onStderr,
            requestTimeoutMs: options.requestTimeoutMs,
            foreground: !background,
            signal: options.signal,
            maxOutputBytes,
          },
        );
        if (background) {
          return handle;
        }
        let result: CommandResult;
        try {
          result = await handle.wait();
        } catch (error) {
          if (error instanceof CommandExitError) {
            span?.setAttribute("rayito.commands.exit_code", error.exitCode);
          }
          throw error;
        }
        span?.setAttribute("rayito.commands.exit_code", result.exitCode);
        return result;
      },
    );
  }

  /** Se engancha a un proceso vivo (o terminado hace < 30 s); `fromSeq` reenvía salida retenida. */
  async connect(pid: number, options: ConnectOptions = {}): Promise<CommandHandle> {
    const deadline =
      options.timeoutMs === undefined ? undefined : timeoutToMs(options.timeoutMs) || undefined;
    return this.attach(this.connectStarter(pid, options.fromSeq ?? 0, deadline), {
      stream: true,
      deadlineMs: deadline,
      onStdout: options.onStdout,
      onStderr: options.onStderr,
      requestTimeoutMs: options.requestTimeoutMs,
      foreground: false,
      signal: options.signal,
      maxOutputBytes: validateMaxOutputBytes(options.maxOutputBytes ?? COMMAND_OUTPUT_MAX_BYTES),
    });
  }

  async list(options: RequestOptions = {}): Promise<ProcessInfo[]> {
    const response = await this.core.processCall(
      (client, callOptions) => client.list(create(ListRequestSchema, {}), callOptions),
      options.requestTimeoutMs,
      options.signal,
    );
    return response.processes.map(processInfoFromProto);
  }

  /** `SendSignal(pid, SIGKILL)`; `false` cuando el pid no existe. */
  async kill(pid: number, options: RequestOptions = {}): Promise<boolean> {
    const request = create(SendSignalRequestSchema, { pid: validatePid(pid), signal: SIGKILL });
    try {
      await this.core.processCall(
        (client, callOptions) => client.sendSignal(request, callOptions),
        options.requestTimeoutMs,
        options.signal,
      );
    } catch (error) {
      if (error instanceof NotFoundError) {
        return false;
      }
      throw error;
    }
    return true;
  }

  async sendStdin(
    pid: number,
    data: string | Uint8Array,
    options: RequestOptions = {},
  ): Promise<void> {
    const request = create(SendInputRequestSchema, {
      pid: validatePid(pid),
      data: encodeStdin(data),
    });
    await this.core.processCall(
      (client, callOptions) => client.sendInput(request, callOptions),
      options.requestTimeoutMs,
      options.signal,
    );
  }

  async closeStdin(pid: number, options: RequestOptions = {}): Promise<void> {
    const request = create(CloseStdinRequestSchema, { pid: validatePid(pid) });
    await this.core.processCall(
      (client, callOptions) => client.closeStdin(request, callOptions),
      options.requestTimeoutMs,
      options.signal,
    );
  }

  connectStarter(
    pid: number,
    fromSeq: number,
    deadlineMs: number | undefined,
  ): StreamStarter<typeof ProcessService, ProcessEvent> {
    const request = create(ConnectRequestSchema, {
      pid: validatePid(pid),
      fromSeq: BigInt(validateFromSeq(fromSeq)),
    });
    return (client, callOptions) => client.connect(request, withTimeout(callOptions, deadlineMs));
  }

  async openConnect(
    pid: number,
    fromSeq: number,
    deadlineMs: number | undefined,
    signal?: AbortSignal,
  ): Promise<OpenedStream<ProcessEvent>> {
    const opened = await this.core.openStream(this.connectStarter(pid, fromSeq, deadlineMs), {
      service: ProcessService,
      stream: true,
      signal,
    });
    if (pidFromStartEvent(opened.first) !== pid) {
      opened.controller.abort();
      throw new SandboxError(`Connect(${pid}) respondió con otro pid`);
    }
    return opened;
  }

  async attach(
    start: StreamStarter<typeof ProcessService, ProcessEvent>,
    options: {
      readonly stream: boolean;
      readonly deadlineMs: number | undefined;
      readonly onStdout: OutputCallback | undefined;
      readonly onStderr: OutputCallback | undefined;
      readonly requestTimeoutMs: number | undefined;
      readonly foreground: boolean;
      readonly signal?: AbortSignal | undefined;
      readonly maxOutputBytes?: number | undefined;
    },
  ): Promise<CommandHandle> {
    const opened = await this.core.openStream(start, {
      service: ProcessService,
      stream: options.stream,
      signal: options.signal,
    });
    const accumulator = new OutputAccumulator({
      onStdout: options.onStdout,
      onStderr: options.onStderr,
      maxBytes: options.maxOutputBytes,
    });
    const adapter = new ProcessEvents();
    const progress = new CommandProgress<ProcessEvent>(
      adapter.pid(opened.first),
      accumulator,
      adapter,
    );
    return new CommandHandle({
      commands: this,
      opened,
      progress,
      requestTimeoutMs: options.requestTimeoutMs,
      deadlineAt: deadlineAt(options.deadlineMs, this.core.now),
      foreground: options.foreground,
      signal: options.signal,
    });
  }
}

export interface CommandHandleInit<T> {
  readonly commands: Commands;
  readonly opened: OpenedStream<T>;
  readonly progress: CommandProgress<T>;
  readonly requestTimeoutMs: number | undefined;
  readonly deadlineAt: number | undefined;
  readonly foreground: boolean;
  /** El `signal` del caller, ya enlazado al stream: abortado, `wait()`/`for await` rechazan con su `reason`. */
  readonly signal?: AbortSignal | undefined;
}

/**
 * Handle de un proceso: `wait()`, `for await`, `kill()`, `disconnect()`,
 * `sendStdin()`, `closeStdin()`. Se reengancha solo tras un suspend/resume
 * con `Connect(pid, fromSeq = lastSeq + 1)`; leerlo nunca despierta un
 * sandbox suspendido (sólo un `run` en foreground puede hacerlo).
 */
export class CommandHandle<T = ProcessEvent> implements AsyncIterable<OutputChunk> {
  protected readonly commands: Commands;
  protected opened: OpenedStream<T>;
  protected readonly progress: CommandProgress<T>;
  protected readonly requestTimeoutMs: number | undefined;
  protected readonly deadlineAtMs: number | undefined;
  protected readonly foreground: boolean;
  protected readonly signal: AbortSignal | undefined;
  protected generation: number;
  protected pendingCut: ConnectError | undefined;
  protected reconnectCount = 0;
  protected readonly budget = new ReconnectBudget();

  constructor(init: CommandHandleInit<T>) {
    this.commands = init.commands;
    this.opened = init.opened;
    this.progress = init.progress;
    this.requestTimeoutMs = init.requestTimeoutMs;
    this.deadlineAtMs = init.deadlineAt;
    this.foreground = init.foreground;
    this.signal = init.signal;
    this.generation = init.commands.core.resumeGeneration;
    init.commands.core.trackStream(init.opened.controller);
  }

  get pid(): number {
    return this.progress.pid;
  }

  get lastSeq(): number {
    return this.progress.accumulator.lastSeq;
  }

  get stdout(): string {
    return this.progress.accumulator.stdout;
  }

  get stderr(): string {
    return this.progress.accumulator.stderr;
  }

  get exitCode(): number | undefined {
    return this.progress.exitCode;
  }

  get error(): string | undefined {
    return this.progress.error;
  }

  get reconnects(): number {
    return this.reconnectCount;
  }

  async wait(): Promise<CommandResult> {
    const iterator = this.chunks();
    let step = await iterator.next();
    while (!step.done) {
      step = await iterator.next();
    }
    return this.progress.resolve();
  }

  kill(): Promise<boolean> {
    return this.commands.kill(this.pid, { requestTimeoutMs: this.requestTimeoutMs });
  }

  disconnect(): void {
    if (this.progress.disconnected) {
      return;
    }
    this.progress.disconnected = true;
    this.opened.controller.abort();
  }

  sendStdin(data: string | Uint8Array): Promise<void> {
    return this.commands.sendStdin(this.pid, data, { requestTimeoutMs: this.requestTimeoutMs });
  }

  closeStdin(): Promise<void> {
    return this.commands.closeStdin(this.pid, { requestTimeoutMs: this.requestTimeoutMs });
  }

  [Symbol.asyncIterator](): AsyncIterator<OutputChunk> {
    return this.chunks();
  }

  protected async *chunks(): AsyncGenerator<OutputChunk, void, undefined> {
    const progress = this.progress;
    while (!(progress.isFinished() || progress.disconnected)) {
      yield* this.consumeStream();
      if (progress.isFinished() || progress.disconnected || !this.cut()) {
        return;
      }
      await this.resubscribeAfterCut();
    }
  }

  protected cut(): boolean {
    return this.progress.suspended || this.pendingCut !== undefined;
  }

  /**
   * Un `disconnect()` aborta el stream: `next()` falla con `Canceled`, que es
   * fin, no fallo. Todo final (limpio, corte o fallo) suelta el stream del
   * registro del sandbox; sólo un corte reconectable lo sustituye después.
   */
  protected async *consumeStream(): AsyncGenerator<OutputChunk, void, undefined> {
    const progress = this.progress;
    while (true) {
      let result: IteratorResult<T>;
      try {
        result = await this.opened.iterator.next();
      } catch (error) {
        this.releaseStream();
        if (this.signal?.aborted) {
          throw this.signal.reason;
        }
        if (progress.disconnected) {
          return;
        }
        if (this.core.isReconnectable(error)) {
          this.pendingCut = error as ConnectError;
          return;
        }
        throw progress.fail(await this.core.streamFailure(error));
      }
      if (result.done) {
        this.releaseStream();
        return;
      }
      const consumed = progress.consume(result.value);
      if (consumed.kind === "chunk") {
        yield consumed.output;
      }
      if (progress.isFinished() || progress.suspended) {
        this.releaseStream();
        return;
      }
    }
  }

  protected releaseStream(): void {
    this.core.releaseStream(this.opened.controller);
  }

  protected async resubscribeAfterCut(): Promise<void> {
    const reason: Error = this.pendingCut ?? suspendingReason(this.progress);
    this.pendingCut = undefined;
    const outcome = await this.core.reconnect(reason, this.generation, { wake: this.wakes() });
    if (!outcome.resumed) {
      throw this.progress.fail(this.core.reconnectError(outcome, reason));
    }
    this.generation = outcome.resumeGeneration;
    if (this.progress.disconnected) {
      return;
    }
    if (!this.budget.allows(outcome)) {
      throw this.progress.fail(await this.futileCut(reason));
    }
    try {
      this.opened = await this.resubscribe(resubscribeFromSeq(this.lastSeq));
    } catch (error) {
      if (!(error instanceof NotFoundError) || error.grpcCode !== Code.OutOfRange) {
        throw this.progress.fail(error instanceof Error ? error : new SandboxError(String(error)));
      }
      this.opened = await this.resubscribeFromLive(error);
    }
    this.core.trackStream(this.opened.controller);
    this.progress.resubscribed();
    this.reconnectCount += 1;
  }

  protected async futileCut(reason: Error): Promise<Error> {
    if (reason instanceof ConnectError) {
      return this.core.streamFailure(reason);
    }
    return reason;
  }

  protected async resubscribeFromLive(error: NotFoundError): Promise<OpenedStream<T>> {
    this.core.logger?.warn?.(
      "se perdió salida entre el último seq visto y lo que el agente retiene; se sigue desde la salida nueva",
      { pid: this.pid, lastSeq: this.lastSeq },
    );
    try {
      return await this.resubscribe(0);
    } catch (retry) {
      const failure = retry instanceof Error ? retry : new SandboxError(String(retry));
      Object.defineProperty(failure, "cause", { value: error, configurable: true, writable: true });
      throw this.progress.fail(failure);
    }
  }

  protected resubscribe(fromSeq: number): Promise<OpenedStream<T>> {
    return this.commands.openConnect(
      this.pid,
      fromSeq,
      this.remainingDeadline(),
      this.signal,
    ) as Promise<OpenedStream<T>>;
  }

  protected remainingDeadline(): number | undefined {
    const remaining = remainingDeadlineMs(this.deadlineAtMs, this.core.now);
    if (remaining !== undefined && remaining <= 0) {
      throw this.progress.fail(
        new TimeoutError(`el deadline del stream del pid ${this.pid} venció durante la reconexión`),
      );
    }
    return remaining;
  }

  /** Sólo un `run` en foreground puede despertar al VM, y no mientras el sandbox tenga una pausa pendiente. */
  protected wakes(): boolean {
    return this.foreground && this.core.foregroundStreamWakes();
  }

  protected get core(): SandboxCore {
    return this.commands.core;
  }
}
