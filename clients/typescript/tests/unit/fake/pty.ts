/**
 * `PtyService` falso en proceso, con el contrato de `rayd` en M5. Un "shell
 * de eco": cada `SendInput` se devuelve como `data` con `\n` convertido en
 * `\r\n` y cada línea completa se interpreta con una tabla mínima (`echo`,
 * `stty size`, `exit`, `id -u`, `tty`, `sleep`; `$VAR` se expande con el
 * entorno; lo demás es `command not found`). `started{pid}` primero, `seq`
 * desde 1 en `data`, ring con `Connect(from_seq)` y `OutOfRange`, `Resize`
 * que el siguiente `stty size` refleja, `Kill` → `exited{signaled, 137}`,
 * `FailedPrecondition` para un pid que no es PTY, el phase gate y `suspend()`.
 */

import { create } from "@bufbuild/protobuf";
import { Code, ConnectError, type HandlerContext } from "@connectrpc/connect";
import { KeepAliveSchema, StreamErrorSchema } from "../../../src/gen/rayito/v1/common_pb.js";
import {
  type ConnectRequest,
  type SendInputRequest,
  SendInputResponseSchema,
} from "../../../src/gen/rayito/v1/process_pb.js";
import {
  type KillPtyRequest,
  KillPtyResponseSchema,
  type PtyExited,
  PtyExitedSchema,
  type PtyServerMessage,
  PtyServerMessageSchema,
  type PtyStart,
  PtyStartedSchema,
  type ResizeRequest,
  ResizeResponseSchema,
} from "../../../src/gen/rayito/v1/pty_pb.js";
import {
  AsyncQueue,
  assertProxyHeaders,
  bytes,
  chunks,
  DEFAULT_CWD,
  deadlineFromHeaders,
  type HeaderMap,
  headerMap,
  KNOWN_DIRECTORIES,
  ReplayOutOfRange,
  record,
  requireAccessToken,
  Signal,
} from "./common.js";
import type { FakeProcessService } from "./process.js";
import { MAX_LIVE_PROCESSES, MAX_SUBSCRIBERS_PER_PID, SIGTERM } from "./process.js";

export const DEFAULT_COLS = 80;
export const DEFAULT_ROWS = 24;
export const MAX_DIMENSION = 4096;
export const DEFAULT_SHELL = "/bin/bash";
export const FALSE_SHELL = "/bin/false";
export const DEFAULT_UID = "1000";
export const PTS_DEVICE = "/dev/pts/0";
export const PTY_CHUNK_SIZE = 16 * 1024;
const BASE_ENV: Readonly<Record<string, string>> = {
  TERM: "xterm-256color",
  LANG: "C.UTF-8",
  LC_ALL: "C.UTF-8",
};

export function ptyExited(exitCode: number): PtyExited {
  return create(PtyExitedSchema, { exitCode, exited: true, status: "exited" });
}

export function ptySignaled(signal: number): PtyExited {
  return create(PtyExitedSchema, {
    exitCode: 128 + signal,
    exited: true,
    status: "signaled",
    signal,
  });
}

export function ptyTimedOut(): PtyExited {
  return create(PtyExitedSchema, {
    exitCode: 128 + SIGTERM,
    exited: false,
    status: "timeout",
    signal: SIGTERM,
    error: create(StreamErrorSchema, { code: "deadline_exceeded", message: "timeout_ms expired" }),
  });
}

export function ptySuspending(): PtyExited {
  return create(PtyExitedSchema, {
    exitCode: 0,
    exited: false,
    status: "suspending",
    error: create(StreamErrorSchema, {
      code: "suspending",
      message: "sandbox suspending; reconnect with Connect(pid, from_seq)",
    }),
  });
}

function startedMessage(pid: number): PtyServerMessage {
  return create(PtyServerMessageSchema, {
    message: { case: "started", value: create(PtyStartedSchema, { pid }) },
  });
}

function dataMessage(seq: number, payload: Uint8Array): PtyServerMessage {
  return create(PtyServerMessageSchema, {
    message: { case: "data", value: payload },
    seq: BigInt(seq),
  });
}

function exitedMessage(exited: PtyExited): PtyServerMessage {
  return create(PtyServerMessageSchema, { message: { case: "exited", value: exited } });
}

function keepaliveMessage(): PtyServerMessage {
  return create(PtyServerMessageSchema, {
    message: { case: "keepalive", value: create(KeepAliveSchema, {}) },
  });
}

/** Una PTY del `rayd` falso: ring, suscriptores, tamaño, shell de eco. */
export class FakePty {
  readonly pid: number;
  readonly request: PtyStart;
  readonly shell: string;
  cols: number;
  rows: number;
  readonly ring: PtyServerMessage[] = [];
  nextSeq = 1;
  subscribers: AsyncQueue<PtyServerMessage>[] = [];
  end: PtyExited | undefined;
  endedAt: number | undefined;
  readonly signal = new Signal();
  signalNumber: number | undefined;
  readonly inputs: Uint8Array[] = [];
  pendingLine = "";
  readonly env: Record<string, string>;

  constructor(pid: number, request: PtyStart, shell: string) {
    this.pid = pid;
    this.request = request;
    this.shell = shell;
    this.cols = request.size?.cols ?? DEFAULT_COLS;
    this.rows = request.size?.rows ?? DEFAULT_ROWS;
    this.env = { ...BASE_ENV, SHELL: shell, ...request.envs };
  }

  get alive(): boolean {
    return this.end === undefined;
  }

  get cwd(): string {
    return this.request.cwd ?? DEFAULT_CWD;
  }

  deadlineMs(): number | undefined {
    const timeoutMs = Number(this.request.timeoutMs);
    return timeoutMs > 0 ? timeoutMs : undefined;
  }

  publish(payload: Uint8Array): void {
    for (const chunk of chunks(payload, PTY_CHUNK_SIZE)) {
      if (this.end !== undefined) {
        return;
      }
      const message = dataMessage(this.nextSeq, chunk);
      this.nextSeq += 1;
      this.ring.push(message);
      this.#fanOut(message);
    }
  }

  finish(end: PtyExited): void {
    if (this.end !== undefined) {
      return;
    }
    this.end = end;
    this.endedAt = performance.now();
    this.#fanOut(exitedMessage(end));
    this.subscribers = [];
    this.signal.set();
  }

  suspend(): void {
    this.#fanOut(exitedMessage(ptySuspending()));
    this.subscribers = [];
  }

  subscribe(fromSeq: number): {
    queue: AsyncQueue<PtyServerMessage> | undefined;
    replay: PtyServerMessage[];
  } {
    const oldest =
      this.ring.length > 0 ? Number((this.ring[0] as PtyServerMessage).seq) : this.nextSeq;
    if (fromSeq > 0 && (fromSeq < oldest || fromSeq > this.nextSeq)) {
      throw new ReplayOutOfRange(oldest, this.nextSeq);
    }
    const replay = fromSeq > 0 ? this.ring.filter((message) => Number(message.seq) >= fromSeq) : [];
    if (this.end !== undefined) {
      return { queue: undefined, replay: [...replay, exitedMessage(this.end)] };
    }
    const queue = new AsyncQueue<PtyServerMessage>();
    this.subscribers.push(queue);
    return { queue, replay };
  }

  unsubscribe(queue: AsyncQueue<PtyServerMessage>): void {
    this.subscribers = this.subscribers.filter((item) => item !== queue);
  }

  /** El eco de la terminal y la interpretación de cada línea completa. */
  feedInput(data: Uint8Array): void {
    this.inputs.push(data);
    const text = new TextDecoder().decode(data);
    this.publish(bytes(text.replaceAll("\n", "\r\n")));
    this.pendingLine += text;
    while (this.pendingLine.includes("\n")) {
      const index = this.pendingLine.indexOf("\n");
      const line = this.pendingLine.slice(0, index);
      this.pendingLine = this.pendingLine.slice(index + 1);
      this.runLine(line.trim());
      if (this.end !== undefined) {
        return;
      }
    }
  }

  runLine(line: string): void {
    for (const command of line.split(";").map((part) => part.trim())) {
      if (command) {
        this.runCommand(command);
      }
      if (this.end !== undefined) {
        return;
      }
    }
  }

  runCommand(command: string): void {
    const space = command.indexOf(" ");
    const name = space < 0 ? command : command.slice(0, space);
    const argument = space < 0 ? "" : command.slice(space + 1);
    if (name === "echo") {
      this.publish(bytes(`${this.expand(argument)}\r\n`));
    } else if (command === "stty size") {
      this.publish(bytes(`${this.rows} ${this.cols}\r\n`));
    } else if (command === "id -u") {
      this.publish(bytes(`${DEFAULT_UID}\r\n`));
    } else if (command === "tty") {
      this.publish(bytes(`${PTS_DEVICE}\r\n`));
    } else if (name === "exit") {
      this.finish(ptyExited(Number(argument || "0")));
    } else if (name === "sleep") {
      void this.signal.wait(Number(argument || "0") * 1000);
    } else {
      this.publish(bytes(`bash: ${name}: command not found\r\n`));
    }
  }

  expand(text: string): string {
    return text
      .split(/\s+/)
      .filter((word) => word.length > 0)
      .map((word) => (word.startsWith("$") ? (this.env[word.slice(1)] ?? "") : word))
      .join(" ");
  }

  /** Como `rayd`: SIGTERM al grupo al vencer `timeout_ms`. */
  async waitForTimeout(): Promise<void> {
    const deadline = this.deadlineMs();
    if (deadline === undefined) {
      return;
    }
    if (!(await this.signal.wait(deadline))) {
      this.finish(ptyTimedOut());
    }
  }

  kill(signal: number): void {
    this.signalNumber = signal;
    this.finish(ptySignaled(signal));
  }

  #fanOut(message: PtyServerMessage): void {
    for (const subscriber of this.subscribers) {
      subscriber.push(message);
    }
  }
}

async function* streamMessages(
  pty: FakePty,
  queue: AsyncQueue<PtyServerMessage> | undefined,
  prelude: PtyServerMessage[],
  context: HandlerContext,
): AsyncGenerator<PtyServerMessage, void, undefined> {
  yield startedMessage(pty.pid);
  yield keepaliveMessage();
  yield* prelude;
  if (queue === undefined) {
    return;
  }
  try {
    while (!context.signal.aborted) {
      const message = await queue.next(context.signal);
      if (message === undefined) {
        return;
      }
      yield message;
      if (message.message.case === "exited") {
        return;
      }
    }
  } finally {
    pty.unsubscribe(queue);
  }
}

function validDimensions(size: { cols: number; rows: number } | undefined): boolean {
  return (
    size !== undefined &&
    size.cols >= 1 &&
    size.cols <= MAX_DIMENSION &&
    size.rows >= 1 &&
    size.rows <= MAX_DIMENSION
  );
}

/** `PtyService` como lo implementa `rayd` en M5 (ver módulo). */
export class FakePtyService {
  readonly tokenSha256: string;
  readonly processes: FakeProcessService;
  allowRoot = false;
  ptyDevices = true;
  readonly createRequests: PtyStart[] = [];
  readonly createHeaders: HeaderMap[] = [];
  readonly connectRequests: ConnectRequest[] = [];
  readonly resizeRequests: ResizeRequest[] = [];
  readonly killRequests: number[] = [];
  readonly deadlines: Record<string, Array<number | undefined>> = {};

  constructor(tokenSha256: string, processes: FakeProcessService) {
    this.tokenSha256 = tokenSha256;
    this.processes = processes;
  }

  get ptys(): Map<number, FakePty> {
    return this.processes.ptys;
  }

  get phase(): string | undefined {
    return this.processes.phase;
  }

  get connectCalls(): Array<[number, number]> {
    return this.connectRequests.map((request) => [request.pid, Number(request.fromSeq)]);
  }

  create(request: PtyStart, context: HandlerContext): AsyncIterable<PtyServerMessage> {
    this.createHeaders.push(this.#authenticate(context, "Create"));
    this.createRequests.push(request);
    this.#gatePhase();
    const shell = this.#validateCreate(request);
    const pty = new FakePty(this.processes.allocatePid(), request, shell);
    this.ptys.set(pty.pid, pty);
    const { queue, replay } = pty.subscribe(0);
    if (shell === FALSE_SHELL) {
      pty.finish(ptyExited(1));
    }
    void pty.waitForTimeout();
    return streamMessages(pty, queue, replay, context);
  }

  connect(request: ConnectRequest, context: HandlerContext): AsyncIterable<PtyServerMessage> {
    this.#authenticate(context, "Connect");
    this.connectRequests.push(request);
    this.#gatePhase();
    const pty = this.#retained(request.pid);
    if (pty.subscribers.length >= MAX_SUBSCRIBERS_PER_PID) {
      throw new ConnectError(
        `max ${MAX_SUBSCRIBERS_PER_PID} subscribers per pid`,
        Code.ResourceExhausted,
      );
    }
    let subscription: ReturnType<FakePty["subscribe"]>;
    try {
      subscription = pty.subscribe(Number(request.fromSeq));
    } catch (error) {
      if (error instanceof ReplayOutOfRange) {
        throw new ConnectError(error.message, Code.OutOfRange);
      }
      throw error;
    }
    return streamMessages(pty, subscription.queue, subscription.replay, context);
  }

  sendInput(request: SendInputRequest, context: HandlerContext) {
    this.#authenticate(context, "SendInput");
    const pty = this.#live(request.pid);
    pty.feedInput(request.data);
    return create(SendInputResponseSchema, {});
  }

  resize(request: ResizeRequest, context: HandlerContext) {
    this.#authenticate(context, "Resize");
    this.resizeRequests.push(request);
    if (!validDimensions(request.size)) {
      throw new ConnectError("invalid pty size", Code.InvalidArgument);
    }
    const pty = this.#live(request.pid);
    pty.cols = request.size?.cols ?? pty.cols;
    pty.rows = request.size?.rows ?? pty.rows;
    return create(ResizeResponseSchema, {});
  }

  kill(request: KillPtyRequest, context: HandlerContext) {
    this.#authenticate(context, "Kill");
    this.killRequests.push(request.pid);
    const pty = this.#live(request.pid);
    pty.kill(9);
    return create(KillPtyResponseSchema, {});
  }

  /** Lo que hace `/suspend` con las PTY: cierra los streams vivos con `exited{suspending}`. */
  suspend(): void {
    for (const pty of this.ptys.values()) {
      if (pty.alive) {
        pty.suspend();
      }
    }
  }

  livePtys(): FakePty[] {
    return [...this.ptys.values()].filter((pty) => pty.alive);
  }

  #authenticate(context: HandlerContext, rpc: string): HeaderMap {
    const headers = headerMap(context);
    assertProxyHeaders(headers);
    record(this.deadlines, rpc, deadlineFromHeaders(headers));
    requireAccessToken(headers, this.tokenSha256);
    return headers;
  }

  #gatePhase(): void {
    if (this.phase !== undefined) {
      throw new ConnectError(this.phase, Code.Unavailable);
    }
  }

  #validateCreate(request: PtyStart): string {
    if (request.size !== undefined && !validDimensions(request.size)) {
      throw new ConnectError("invalid pty size", Code.InvalidArgument);
    }
    const shell = request.shell ?? DEFAULT_SHELL;
    if (!shell.startsWith("/")) {
      throw new ConnectError("shell must be an absolute path", Code.InvalidArgument);
    }
    if (request.user?.username === "root" && !this.allowRoot) {
      throw new ConnectError("root is not allowed", Code.PermissionDenied);
    }
    if (request.cwd !== undefined && !KNOWN_DIRECTORIES.has(request.cwd)) {
      throw new ConnectError("cwd is not a directory", Code.InvalidArgument);
    }
    if (!this.ptyDevices) {
      throw new ConnectError("pty devices unavailable", Code.FailedPrecondition);
    }
    if (this.processes.liveCount() >= MAX_LIVE_PROCESSES) {
      throw new ConnectError(`max ${MAX_LIVE_PROCESSES} live processes`, Code.ResourceExhausted);
    }
    return shell;
  }

  #live(pid: number): FakePty {
    this.#refuseProcessPid(pid);
    const pty = this.ptys.get(pid);
    if (pty === undefined || !pty.alive) {
      throw new ConnectError(`pid ${pid} not found`, Code.NotFound);
    }
    return pty;
  }

  #retained(pid: number): FakePty {
    this.#refuseProcessPid(pid);
    const pty = this.ptys.get(pid);
    if (pty === undefined || !(pty.alive || this.#withinRetention(pty))) {
      throw new ConnectError(`pid ${pid} not found`, Code.NotFound);
    }
    return pty;
  }

  #refuseProcessPid(pid: number): void {
    if (this.processes.processes.has(pid)) {
      throw new ConnectError(`pid ${pid} is not a PTY`, Code.FailedPrecondition);
    }
  }

  #withinRetention(pty: FakePty): boolean {
    return (
      pty.endedAt !== undefined && performance.now() - pty.endedAt < this.processes.retentionMs
    );
  }
}
