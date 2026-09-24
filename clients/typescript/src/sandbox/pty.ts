/**
 * `sandbox.pty`: terminales interactivas (`PtyService`). `PtyHandle` es un
 * `CommandHandle` cuya iteración entrega `{ pty: Uint8Array }` y que se
 * reengancha con `Pty.Connect(pid, fromSeq = lastSeq + 1)`.
 */

import { create } from "@bufbuild/protobuf";
import { InvalidArgumentError, NotFoundError, SandboxError } from "../errors.js";
import { UserSchema } from "../gen/rayito/v1/common_pb.js";
import {
  ConnectRequestSchema,
  type EndEvent,
  EndEventSchema,
  SendInputRequestSchema,
} from "../gen/rayito/v1/process_pb.js";
import {
  KillPtyRequestSchema,
  type PtyExited,
  type PtyServerMessage,
  PtyService,
  PtySizeSchema,
  type PtyStart,
  PtyStartSchema,
  ResizeRequestSchema,
} from "../gen/rayito/v1/pty_pb.js";
import { type PtySize, validatePtySize } from "../models.js";
import { validatedEnvs } from "../payload.js";
import {
  CommandHandle,
  CommandProgress,
  type Commands,
  type Consumed,
  consumedEnd,
  deadlineAt,
  encodeStdin,
  NOTHING,
  OutputAccumulator,
  type RequestOptions,
  type StreamAdapter,
  streamDeadlineMs,
  timeoutToMs,
  validateFromSeq,
  validatePid,
} from "./commands.js";
import { type OpenedStream, type SandboxCore, type StreamStarter, withTimeout } from "./core.js";

export const DEFAULT_PTY_TIMEOUT_MS = 60_000;

export type PtyDataCallback = (chunk: Uint8Array) => void;
export type PtyClient = SandboxCore["clients"]["pty"];

export interface PtyCreateOptions extends RequestOptions {
  readonly size?: Partial<PtySize> | undefined;
  readonly user?: string | undefined;
  readonly cwd?: string | undefined;
  readonly envs?: Readonly<Record<string, string>> | undefined;
  readonly shell?: string | undefined;
  readonly onData?: PtyDataCallback | undefined;
  /** Timeout del servidor en ms (60 000 por defecto; `0` = sin límite). */
  readonly timeoutMs?: number | undefined;
}

export interface PtyConnectOptions extends RequestOptions {
  readonly fromSeq?: number | undefined;
  readonly onData?: PtyDataCallback | undefined;
  readonly timeoutMs?: number | undefined;
}

/** `undefined` y `""` dejan el shell de login del usuario; si se indica, debe ser una ruta absoluta. */
export function validateShell(shell: string | undefined): string | undefined {
  if (shell === undefined || shell === "") {
    return undefined;
  }
  if (typeof shell !== "string" || shell.includes("\0") || !shell.startsWith("/")) {
    throw new InvalidArgumentError(
      `shell debe ser una ruta absoluta, recibido ${JSON.stringify(shell)}`,
    );
  }
  return shell;
}

export interface PtyStartInput {
  readonly size?: Partial<PtySize> | undefined;
  readonly user?: string | undefined;
  readonly cwd?: string | undefined;
  readonly envs?: Readonly<Record<string, string>> | undefined;
  readonly shell?: string | undefined;
  readonly timeoutMs?: number | undefined;
}

/** `PtyStart` con `timeout_ms` (0 = sin límite); `size` ausente deja que el agente aplique 80x24. */
export function buildPtyStartRequest(input: PtyStartInput = {}): PtyStart {
  const request = create(PtyStartSchema, {
    timeoutMs: BigInt(timeoutToMs(input.timeoutMs ?? DEFAULT_PTY_TIMEOUT_MS)),
  });
  const size = validatePtySize(input.size);
  if (size !== undefined) {
    request.size = create(PtySizeSchema, { cols: size.cols, rows: size.rows });
  }
  if (input.envs !== undefined && Object.keys(input.envs).length > 0) {
    request.envs = validatedEnvs(input.envs);
  }
  if (input.cwd) {
    request.cwd = input.cwd;
  }
  if (input.user) {
    request.user = create(UserSchema, { username: input.user });
  }
  const shell = validateShell(input.shell);
  if (shell !== undefined) {
    request.shell = shell;
  }
  return request;
}

export function buildPtyConnectRequest(pid: number, fromSeq: number) {
  return create(ConnectRequestSchema, {
    pid: validatePid(pid),
    fromSeq: BigInt(validateFromSeq(fromSeq)),
  });
}

export function buildPtySendInputRequest(pid: number, data: string | Uint8Array) {
  return create(SendInputRequestSchema, { pid: validatePid(pid), data: encodeStdin(data) });
}

export function buildResizeRequest(pid: number, size: Partial<PtySize>) {
  const validated = validatePtySize(size);
  if (validated === undefined) {
    throw new InvalidArgumentError("resize necesita un PtySize");
  }
  return create(ResizeRequestSchema, {
    pid: validatePid(pid),
    size: create(PtySizeSchema, { cols: validated.cols, rows: validated.rows }),
  });
}

export function buildKillRequest(pid: number) {
  return create(KillPtyRequestSchema, { pid: validatePid(pid) });
}

/** El primer mensaje de `Create` y `Connect` es siempre `started{pid}`. */
export function pidFromPtyStarted(message: PtyServerMessage | undefined): number {
  if (message === undefined || message.message.case !== "started") {
    throw new SandboxError(
      `el stream de la PTY no empezó con started (llegó ${JSON.stringify(message?.message.case ?? null)})`,
    );
  }
  return message.message.value.pid;
}

/** `PtyExited` tiene la misma forma que `EndEvent`; convertirlo deja que `CommandProgress` aplique una única tabla. */
export function endEventFromPtyExited(exited: PtyExited): EndEvent {
  const end = create(EndEventSchema, {
    exitCode: exited.exitCode,
    exited: exited.exited,
    status: exited.status,
  });
  if (exited.error !== undefined) {
    end.error = exited.error;
  }
  if (exited.signal !== undefined) {
    end.signal = exited.signal;
  }
  return end;
}

/**
 * `StreamAdapter` de `PtyService.Create`/`Connect`: `data` son bytes crudos
 * de la terminal, entregados tal cual a `onData` y al iterador y
 * decodificados (UTF-8 con reemplazo) en `stdout`.
 */
export class PtyMessages implements StreamAdapter<PtyServerMessage> {
  readonly #onData: PtyDataCallback | undefined;

  constructor(onData: PtyDataCallback | undefined) {
    this.#onData = onData;
  }

  pid(first: PtyServerMessage | undefined): number {
    return pidFromPtyStarted(first);
  }

  consume(message: PtyServerMessage, accumulator: OutputAccumulator): Consumed {
    const body = message.message;
    if (body.case === "data") {
      const payload = body.value;
      accumulator.feedTerminal(Number(message.seq), payload);
      this.#onData?.(payload);
      return { kind: "chunk", seq: Number(message.seq), output: { pty: payload } };
    }
    if (body.case === "exited") {
      return consumedEnd(endEventFromPtyExited(body.value));
    }
    return NOTHING;
  }
}

/** Terminales interactivas del sandbox (`PtyService`). */
export class Pty {
  readonly core: SandboxCore;
  readonly #commands: Commands;

  constructor(core: SandboxCore, commands: Commands) {
    this.core = core;
    this.#commands = commands;
  }

  async create(options: PtyCreateOptions = {}): Promise<PtyHandle> {
    const request = buildPtyStartRequest(options);
    const deadline = streamDeadlineMs(options.timeoutMs ?? DEFAULT_PTY_TIMEOUT_MS);
    return this.attach(
      (client, callOptions) => client.create(request, withTimeout(callOptions, deadline)),
      {
        deadlineMs: deadline,
        onData: options.onData,
        requestTimeoutMs: options.requestTimeoutMs,
        signal: options.signal,
      },
    );
  }

  async connect(pid: number, options: PtyConnectOptions = {}): Promise<PtyHandle> {
    const deadline =
      options.timeoutMs === undefined ? undefined : timeoutToMs(options.timeoutMs) || undefined;
    return this.attach(this.connectStarter(pid, options.fromSeq ?? 0, deadline), {
      deadlineMs: deadline,
      onData: options.onData,
      requestTimeoutMs: options.requestTimeoutMs,
      signal: options.signal,
    });
  }

  async sendInput(
    pid: number,
    data: string | Uint8Array,
    options: RequestOptions = {},
  ): Promise<void> {
    const request = buildPtySendInputRequest(pid, data);
    await this.core.ptyCall(
      (client, callOptions) => client.sendInput(request, callOptions),
      options.requestTimeoutMs,
      options.signal,
    );
  }

  sendStdin(pid: number, data: string | Uint8Array, options: RequestOptions = {}): Promise<void> {
    return this.sendInput(pid, data, options);
  }

  async resize(pid: number, size: Partial<PtySize>, options: RequestOptions = {}): Promise<void> {
    const request = buildResizeRequest(pid, size);
    await this.core.ptyCall(
      (client, callOptions) => client.resize(request, callOptions),
      options.requestTimeoutMs,
      options.signal,
    );
  }

  async kill(pid: number, options: RequestOptions = {}): Promise<boolean> {
    const request = buildKillRequest(pid);
    try {
      await this.core.ptyCall(
        (client, callOptions) => client.kill(request, callOptions),
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

  connectStarter(
    pid: number,
    fromSeq: number,
    deadlineMs: number | undefined,
  ): StreamStarter<typeof PtyService, PtyServerMessage> {
    const request = buildPtyConnectRequest(pid, fromSeq);
    return (client, callOptions) => client.connect(request, withTimeout(callOptions, deadlineMs));
  }

  async openConnect(
    pid: number,
    fromSeq: number,
    deadlineMs: number | undefined,
    signal?: AbortSignal,
  ): Promise<OpenedStream<PtyServerMessage>> {
    const opened = await this.core.openStream(this.connectStarter(pid, fromSeq, deadlineMs), {
      service: PtyService,
      stream: true,
      signal,
    });
    if (pidFromPtyStarted(opened.first) !== pid) {
      opened.controller.abort();
      throw new SandboxError(`Pty.Connect(${pid}) respondió con otro pid`);
    }
    return opened;
  }

  async attach(
    start: StreamStarter<typeof PtyService, PtyServerMessage>,
    options: {
      readonly deadlineMs: number | undefined;
      readonly onData: PtyDataCallback | undefined;
      readonly requestTimeoutMs: number | undefined;
      readonly signal?: AbortSignal | undefined;
    },
  ): Promise<PtyHandle> {
    const opened = await this.core.openStream(start, {
      service: PtyService,
      stream: true,
      signal: options.signal,
    });
    const adapter = new PtyMessages(options.onData);
    const progress = new CommandProgress<PtyServerMessage>(
      adapter.pid(opened.first),
      new OutputAccumulator(),
      adapter,
    );
    return new PtyHandle({
      pty: this,
      commands: this.#commands,
      opened,
      progress,
      requestTimeoutMs: options.requestTimeoutMs,
      deadlineAt: deadlineAt(options.deadlineMs, this.core.now),
      foreground: false,
      signal: options.signal,
    });
  }
}

export interface PtyHandleInit {
  readonly pty: Pty;
  readonly commands: Commands;
  readonly opened: OpenedStream<PtyServerMessage>;
  readonly progress: CommandProgress<PtyServerMessage>;
  readonly requestTimeoutMs: number | undefined;
  readonly deadlineAt: number | undefined;
  readonly foreground: boolean;
  readonly signal?: AbortSignal | undefined;
}

/** Handle de una PTY: `for await` entrega `{ pty: Uint8Array }`; `sendInput`, `resize` y `kill` van por `PtyService`. */
export class PtyHandle extends CommandHandle<PtyServerMessage> {
  readonly #pty: Pty;

  constructor(init: PtyHandleInit) {
    super(init);
    this.#pty = init.pty;
  }

  override kill(): Promise<boolean> {
    return this.#pty.kill(this.pid, { requestTimeoutMs: this.requestTimeoutMs });
  }

  sendInput(data: string | Uint8Array): Promise<void> {
    return this.#pty.sendInput(this.pid, data, { requestTimeoutMs: this.requestTimeoutMs });
  }

  override sendStdin(data: string | Uint8Array): Promise<void> {
    return this.sendInput(data);
  }

  resize(size: Partial<PtySize>): Promise<void> {
    return this.#pty.resize(this.pid, size, { requestTimeoutMs: this.requestTimeoutMs });
  }

  protected override resubscribe(fromSeq: number): Promise<OpenedStream<PtyServerMessage>> {
    return this.#pty.openConnect(this.pid, fromSeq, this.remainingDeadline(), this.signal);
  }
}
