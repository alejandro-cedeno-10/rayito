/**
 * `ProcessService` falso en proceso, con el contrato de `rayd` en M2/M5.
 *
 * Interpreta el wrapper `/bin/bash -l -c <cmd>` con una tabla mínima de
 * scripts (`echo`, `err`, `exit`, `sleep`, `cat`, `seq`, `big`, `split`,
 * `truncate`, `slow`, `pwd`, `whoami`, `env`; lo demás es `command not
 * found`, 127) y reproduce lo que el SDK necesita observar: `StartEvent`
 * primero, un `KeepAlive` que el cliente debe ignorar, ring por pid con `seq`
 * desde 1, `Connect(from_seq)` con `OutOfRange`, retención de terminados,
 * `timeout_ms`, `SendSignal`, stdin por pipe, `x-access-token` en cada RPC,
 * el phase gate y `suspend()` (cierra los streams vivos con
 * `EndEvent{suspending}` sin tocar los procesos). Comparte el registro de
 * pids con `FakePty`.
 */

import { create } from "@bufbuild/protobuf";
import { Code, ConnectError, type HandlerContext } from "@connectrpc/connect";
import { KeepAliveSchema, StreamErrorSchema } from "../../../src/gen/rayito/v1/common_pb.js";
import {
  type CloseStdinRequest,
  CloseStdinResponseSchema,
  type ConnectRequest,
  DataEventSchema,
  type EndEvent,
  EndEventSchema,
  type ListResponse,
  ListResponseSchema,
  ProcessConfigSchema,
  type ProcessEvent,
  ProcessEventSchema,
  ProcessInfoSchema,
  ProcessKind,
  type SendInputRequest,
  SendInputResponseSchema,
  type SendSignalRequest,
  SendSignalResponseSchema,
  StartEventSchema,
  type StartRequest,
} from "../../../src/gen/rayito/v1/process_pb.js";
import {
  AsyncQueue,
  abortWith,
  assertProxyHeaders,
  bytes,
  chunks,
  DEFAULT_CWD,
  DEFAULT_USERNAME,
  deadlineFromHeaders,
  type HeaderMap,
  headerMap,
  KNOWN_DIRECTORIES,
  ReplayOutOfRange,
  requireAccessToken,
  Signal,
  STEP_MS,
  StreamEnd,
  sleep,
} from "./common.js";
import type { FakePty } from "./pty.js";

export const CHUNK_SIZE = 32 * 1024;
export const FIRST_PID = 1000;
export const MAX_LIVE_PROCESSES = 256;
export const MAX_SUBSCRIBERS_PER_PID = 8;
export const SIGTERM = 15;
const SEQ_PAUSE_MS = 50;

export function exited(exitCode: number): EndEvent {
  return create(EndEventSchema, { exitCode, exited: true, status: "exited" });
}

export function signaled(signal: number): EndEvent {
  return create(EndEventSchema, {
    exitCode: 128 + signal,
    exited: true,
    status: "signaled",
    signal,
  });
}

export function timedOut(): EndEvent {
  return create(EndEventSchema, {
    exitCode: 128 + SIGTERM,
    exited: false,
    status: "timeout",
    signal: SIGTERM,
    error: create(StreamErrorSchema, { code: "deadline_exceeded", message: "timeout_ms expired" }),
  });
}

export function truncated(lastSeq: number): EndEvent {
  return create(EndEventSchema, {
    exited: false,
    status: "output_truncated",
    error: create(StreamErrorSchema, {
      code: "output_truncated",
      message: `subscriber stalled for 30 s at seq ${lastSeq}`,
    }),
  });
}

export function suspending(): EndEvent {
  return create(EndEventSchema, {
    exitCode: 0,
    exited: false,
    status: "suspending",
    error: create(StreamErrorSchema, {
      code: "suspending",
      message: "sandbox suspending; reconnect with Connect(pid, from_seq)",
    }),
  });
}

function startEvent(pid: number): ProcessEvent {
  return create(ProcessEventSchema, {
    event: { case: "start", value: create(StartEventSchema, { pid }) },
  });
}

function keepalive(): ProcessEvent {
  return create(ProcessEventSchema, {
    event: { case: "keepalive", value: create(KeepAliveSchema, {}) },
  });
}

function endEvent(end: EndEvent): ProcessEvent {
  return create(ProcessEventSchema, { event: { case: "end", value: end } });
}

/** Un proceso del `rayd` falso: ring, suscriptores, stdin y señal. */
export class FakeProcess {
  readonly pid: number;
  readonly request: StartRequest;
  readonly stdinEnabled: boolean;
  readonly stdin = new AsyncQueue<Uint8Array | null>();
  stdinClosed = false;
  readonly ring: ProcessEvent[] = [];
  nextSeq = 1;
  subscribers: AsyncQueue<ProcessEvent | StreamEnd>[] = [];
  readonly signal = new Signal();
  signalNumber: number | undefined;
  end: EndEvent | undefined;
  endedAt: number | undefined;
  readonly stdinAbort = new AbortController();

  constructor(pid: number, request: StartRequest) {
    this.pid = pid;
    this.request = request;
    this.stdinEnabled = request.stdin;
  }

  get alive(): boolean {
    return this.end === undefined;
  }

  get username(): string {
    return this.request.user?.username || DEFAULT_USERNAME;
  }

  get cwd(): string {
    return this.request.process?.cwd ?? DEFAULT_CWD;
  }

  get command(): string {
    const args = this.request.process?.args ?? [];
    return args[args.length - 1] ?? "";
  }

  deadlineMs(): number | undefined {
    const timeoutMs = Number(this.request.timeoutMs);
    return timeoutMs > 0 ? timeoutMs : undefined;
  }

  publish(stream: "stdout" | "stderr", payload: Uint8Array): void {
    const data = create(DataEventSchema, {
      seq: BigInt(this.nextSeq),
      output: { case: stream, value: payload },
    });
    this.nextSeq += 1;
    const event = create(ProcessEventSchema, { event: { case: "data", value: data } });
    this.ring.push(event);
    this.#fanOut(event);
  }

  finish(end: EndEvent): void {
    if (this.end !== undefined) {
      return;
    }
    this.end = end;
    this.endedAt = performance.now();
    this.#fanOut(endEvent(end));
    this.subscribers = [];
    this.stdinAbort.abort();
  }

  truncateSubscribers(): void {
    this.#fanOut(endEvent(truncated(this.nextSeq - 1)));
    this.subscribers = [];
  }

  /** Lo que hace `/suspend`: el final `suspending` a cada suscriptor y el stream cerrado; el proceso sigue. */
  suspend(): void {
    this.#fanOut(endEvent(suspending()));
    this.subscribers = [];
  }

  /** Un corte por debajo de gRPC (proxy 502, sesión caída): el stream aborta con ese status. */
  cut(code: Code, message: string): void {
    for (const subscriber of this.subscribers) {
      subscriber.push(new StreamEnd(code, message));
    }
    this.subscribers = [];
  }

  /** Registra el suscriptor y toma la foto del replay (ni huecos ni duplicados). Sin cola si ya terminó. */
  subscribe(fromSeq: number): {
    queue: AsyncQueue<ProcessEvent | StreamEnd> | undefined;
    replay: ProcessEvent[];
  } {
    const oldest = this.ring.length > 0 ? seqOf(this.ring[0] as ProcessEvent) : this.nextSeq;
    if (fromSeq > 0 && (fromSeq < oldest || fromSeq > this.nextSeq)) {
      throw new ReplayOutOfRange(oldest, this.nextSeq);
    }
    const replay = fromSeq > 0 ? this.ring.filter((event) => seqOf(event) >= fromSeq) : [];
    if (this.end !== undefined) {
      return { queue: undefined, replay: [...replay, endEvent(this.end)] };
    }
    const queue = new AsyncQueue<ProcessEvent | StreamEnd>();
    this.subscribers.push(queue);
    return { queue, replay };
  }

  unsubscribe(queue: AsyncQueue<ProcessEvent | StreamEnd>): void {
    this.subscribers = this.subscribers.filter((item) => item !== queue);
  }

  /** Espera `ms` salvo señal o `timeout_ms`: "signaled" | "timeout" | "done". */
  async wait(ms: number): Promise<"signaled" | "timeout" | "done"> {
    const deadline = this.deadlineMs();
    if (deadline !== undefined && deadline < ms) {
      return (await this.signal.wait(deadline)) ? "signaled" : "timeout";
    }
    return (await this.signal.wait(ms)) ? "signaled" : "done";
  }

  endAfter(outcome: "signaled" | "timeout" | "done"): EndEvent {
    if (outcome === "signaled") {
      return signaled(this.signalNumber ?? SIGTERM);
    }
    if (outcome === "timeout") {
      return timedOut();
    }
    return exited(0);
  }

  #fanOut(event: ProcessEvent): void {
    for (const subscriber of this.subscribers) {
      subscriber.push(event);
    }
  }
}

function seqOf(event: ProcessEvent): number {
  return event.event.case === "data" ? Number(event.event.value.seq) : 0;
}

type Script = (process: FakeProcess, argument: string) => Promise<void>;

const SCRIPTS: Readonly<Record<string, Script>> = {
  async echo(process, argument) {
    process.publish("stdout", bytes(`${argument}\n`));
    process.finish(exited(0));
  },
  async err(process, argument) {
    process.publish("stderr", bytes(`${argument}\n`));
    process.finish(exited(0));
  },
  async exit(process, argument) {
    process.finish(exited(Number(argument)));
  },
  async sleep(process, argument) {
    process.finish(process.endAfter(await process.wait(Number(argument) * 1000)));
  },
  async cat(process) {
    if (!process.stdinEnabled) {
      process.finish(exited(0));
      return;
    }
    while (!process.signal.isSet) {
      const item = await process.stdin.next(process.stdinAbort.signal);
      if (item === undefined) {
        break;
      }
      if (item === null) {
        process.finish(exited(0));
        return;
      }
      process.publish("stdout", item);
    }
    process.finish(process.endAfter("signaled"));
  },
  async seq(process, argument) {
    const count = Number(argument);
    for (let number = 1; number <= count; number += 1) {
      process.publish("stdout", bytes(`${number}\n`));
      if (await process.signal.wait(SEQ_PAUSE_MS)) {
        process.finish(process.endAfter("signaled"));
        return;
      }
    }
    process.finish(exited(0));
  },
  async big(process, argument) {
    for (const chunk of chunks(new Uint8Array(Number(argument)).fill(97), CHUNK_SIZE)) {
      process.publish("stdout", chunk);
    }
    process.finish(exited(0));
  },
  async split(process) {
    const payload = new Uint8Array([...new Uint8Array(CHUNK_SIZE - 1).fill(97), ...bytes("é\n")]);
    for (const chunk of chunks(payload, CHUNK_SIZE)) {
      process.publish("stdout", chunk);
    }
    process.finish(exited(0));
  },
  async truncate(process) {
    process.publish("stdout", bytes("partial\n"));
    process.truncateSubscribers();
    process.finish(process.endAfter(await process.wait(30_000)));
  },
  async slow(process, argument) {
    process.publish("stdout", bytes("slow start\n"));
    const outcome = await process.wait(Number(argument) * 1000);
    if (outcome !== "done") {
      process.finish(process.endAfter(outcome));
      return;
    }
    process.publish("stdout", bytes("slow end\n"));
    process.finish(exited(0));
  },
  async pwd(process) {
    process.publish("stdout", bytes(`${process.cwd}\n`));
    process.finish(exited(0));
  },
  async whoami(process) {
    process.publish("stdout", bytes(`${process.username}\n`));
    process.finish(exited(0));
  },
  async env(process) {
    const envs = process.request.process?.envs ?? {};
    const lines = Object.keys(envs)
      .sort()
      .map((key) => `${key}=${envs[key]}\n`)
      .join("");
    process.publish("stdout", bytes(lines));
    process.finish(exited(0));
  },
};

async function runUnknown(process: FakeProcess, name: string): Promise<void> {
  process.publish("stderr", bytes(`bash: ${name}: command not found\n`));
  process.finish(exited(127));
}

export function runScript(process: FakeProcess): Promise<void> {
  const command = process.command;
  const space = command.indexOf(" ");
  const name = space < 0 ? command : command.slice(0, space);
  const argument = space < 0 ? "" : command.slice(space + 1);
  const script = SCRIPTS[name];
  return script === undefined ? runUnknown(process, name) : script(process, argument);
}

async function* streamEvents(
  process: FakeProcess,
  queue: AsyncQueue<ProcessEvent | StreamEnd> | undefined,
  prelude: ProcessEvent[],
  context: HandlerContext,
): AsyncGenerator<ProcessEvent, void, undefined> {
  yield startEvent(process.pid);
  yield keepalive();
  yield* prelude;
  if (queue === undefined) {
    return;
  }
  try {
    while (!context.signal.aborted) {
      const event = await queue.next(context.signal);
      if (event === undefined) {
        return;
      }
      if (event instanceof StreamEnd) {
        abortWith(event);
      }
      yield event;
      if (event.event.case === "end") {
        return;
      }
    }
  } finally {
    process.unsubscribe(queue);
  }
}

export interface FakeProcessServiceOptions {
  readonly allowRoot?: boolean;
  readonly retentionMs?: number;
}

/** `ProcessService` como lo implementa `rayd` (ver módulo). */
export class FakeProcessService {
  tokenSha256: string;
  readonly allowRoot: boolean;
  readonly retentionMs: number;
  phase: string | undefined;
  readonly startRequests: StartRequest[] = [];
  readonly startHeaders: HeaderMap[] = [];
  readonly startDeadlines: Array<number | undefined> = [];
  readonly connectRequests: ConnectRequest[] = [];
  readonly processes = new Map<number, FakeProcess>();
  readonly ptys = new Map<number, FakePty>();
  readonly signalRequests: SendSignalRequest[] = [];
  readonly listHeaders: HeaderMap[] = [];
  signalUnavailableCalls = 0;
  nextPid = FIRST_PID;

  constructor(tokenSha256: string, options: FakeProcessServiceOptions = {}) {
    this.tokenSha256 = tokenSha256;
    this.allowRoot = options.allowRoot ?? false;
    this.retentionMs = options.retentionMs ?? 30_000;
  }

  get connectCalls(): Array<[number, number]> {
    return this.connectRequests.map((request) => [request.pid, Number(request.fromSeq)]);
  }

  start(request: StartRequest, context: HandlerContext): AsyncIterable<ProcessEvent> {
    const headers = this.#authenticate(context);
    this.startHeaders.push(headers);
    this.startRequests.push(request);
    this.startDeadlines.push(deadlineFromHeaders(headers));
    this.#gatePhase();
    this.#validateStart(request);
    const process = this.#spawn(request);
    const { queue, replay } = process.subscribe(0);
    void runScript(process);
    return streamEvents(process, queue, replay, context);
  }

  connect(request: ConnectRequest, context: HandlerContext): AsyncIterable<ProcessEvent> {
    this.#authenticate(context);
    this.connectRequests.push(request);
    this.#gatePhase();
    this.#refusePtyPid(request.pid);
    const process = this.#retained(request.pid);
    if (process.subscribers.length >= MAX_SUBSCRIBERS_PER_PID) {
      throw new ConnectError(
        `max ${MAX_SUBSCRIBERS_PER_PID} subscribers per pid`,
        Code.ResourceExhausted,
      );
    }
    let subscription: ReturnType<FakeProcess["subscribe"]>;
    try {
      subscription = process.subscribe(Number(request.fromSeq));
    } catch (error) {
      if (error instanceof ReplayOutOfRange) {
        throw new ConnectError(error.message, Code.OutOfRange);
      }
      throw error;
    }
    return streamEvents(process, subscription.queue, subscription.replay, context);
  }

  sendInput(request: SendInputRequest, context: HandlerContext) {
    this.#authenticate(context);
    const process = this.#live(request.pid);
    if (!process.stdinEnabled || process.stdinClosed) {
      throw new ConnectError("stdin is not open", Code.FailedPrecondition);
    }
    process.stdin.push(request.data);
    return create(SendInputResponseSchema, {});
  }

  closeStdin(request: CloseStdinRequest, context: HandlerContext) {
    this.#authenticate(context);
    const process = this.#live(request.pid);
    if (!process.stdinEnabled) {
      throw new ConnectError("stdin is not open", Code.FailedPrecondition);
    }
    if (!process.stdinClosed) {
      process.stdinClosed = true;
      process.stdin.push(null);
    }
    return create(CloseStdinResponseSchema, {});
  }

  sendSignal(request: SendSignalRequest, context: HandlerContext) {
    this.#authenticate(context);
    this.signalRequests.push(request);
    if (this.signalUnavailableCalls > 0) {
      this.signalUnavailableCalls -= 1;
      throw new ConnectError("suspending", Code.Unavailable);
    }
    this.#gatePhase();
    if (request.signal < 1 || request.signal > 64) {
      throw new ConnectError("signal must be in 1..=64", Code.InvalidArgument);
    }
    const pty = this.ptys.get(request.pid);
    if (pty !== undefined) {
      if (!pty.alive) {
        throw new ConnectError(`pid ${request.pid} not found`, Code.NotFound);
      }
      pty.kill(request.signal);
      return create(SendSignalResponseSchema, {});
    }
    const process = this.#live(request.pid);
    process.signalNumber = request.signal;
    process.signal.set();
    return create(SendSignalResponseSchema, {});
  }

  list(_request: unknown, context: HandlerContext): ListResponse {
    this.listHeaders.push(this.#authenticate(context));
    return create(ListResponseSchema, {
      processes: [
        ...this.liveProcesses().map((process) =>
          create(ProcessInfoSchema, {
            pid: process.pid,
            config: process.request.process,
            tag: process.request.tag,
            kind: ProcessKind.PROCESS,
          }),
        ),
        ...this.livePtys().map((pty) =>
          create(ProcessInfoSchema, {
            pid: pty.pid,
            config: create(ProcessConfigSchema, {
              cmd: pty.shell,
              args: ["-i", "-l"],
              cwd: pty.cwd,
              envs: { ...pty.request.envs },
            }),
            kind: ProcessKind.PTY,
          }),
        ),
      ],
    });
  }

  // --------------------------------------------------------- test controls

  /** Lo que hace `/suspend`: los streams vivos terminan con `EndEvent{suspending}` y los nuevos se rechazan. */
  suspend(): void {
    for (const process of this.liveProcesses()) {
      process.suspend();
    }
    this.phase = "suspending";
  }

  resume(): void {
    this.phase = undefined;
  }

  allocatePid(): number {
    const pid = this.nextPid;
    this.nextPid += 1;
    return pid;
  }

  liveProcesses(): FakeProcess[] {
    return [...this.processes.values()].filter((process) => process.alive);
  }

  livePtys(): FakePty[] {
    return [...this.ptys.values()].filter((pty) => pty.alive);
  }

  liveCount(): number {
    return this.liveProcesses().length + this.livePtys().length;
  }

  #authenticate(context: HandlerContext): HeaderMap {
    const headers = headerMap(context);
    assertProxyHeaders(headers);
    requireAccessToken(headers, this.tokenSha256);
    return headers;
  }

  #gatePhase(): void {
    if (this.phase !== undefined) {
      throw new ConnectError(this.phase, Code.Unavailable);
    }
  }

  #refusePtyPid(pid: number): void {
    if (this.ptys.has(pid)) {
      throw new ConnectError(`pid ${pid} is a PTY; use PtyService`, Code.FailedPrecondition);
    }
  }

  #validateStart(request: StartRequest): void {
    if (!request.process?.cmd) {
      throw new ConnectError("empty command", Code.InvalidArgument);
    }
    if (request.user?.username === "root" && !this.allowRoot) {
      throw new ConnectError("root is not allowed", Code.PermissionDenied);
    }
    if (request.process.cwd !== undefined && !KNOWN_DIRECTORIES.has(request.process.cwd)) {
      throw new ConnectError("cwd is not a directory", Code.InvalidArgument);
    }
    if (this.liveCount() >= MAX_LIVE_PROCESSES) {
      throw new ConnectError(`max ${MAX_LIVE_PROCESSES} live processes`, Code.ResourceExhausted);
    }
  }

  #spawn(request: StartRequest): FakeProcess {
    const process = new FakeProcess(this.allocatePid(), request);
    this.processes.set(process.pid, process);
    return process;
  }

  #live(pid: number): FakeProcess {
    this.#refusePtyPid(pid);
    const process = this.processes.get(pid);
    if (process === undefined || !process.alive) {
      throw new ConnectError(`pid ${pid} not found`, Code.NotFound);
    }
    return process;
  }

  #retained(pid: number): FakeProcess {
    const process = this.processes.get(pid);
    if (process === undefined || !(process.alive || this.#withinRetention(process))) {
      throw new ConnectError(`pid ${pid} not found`, Code.NotFound);
    }
    return process;
  }

  #withinRetention(process: FakeProcess): boolean {
    return process.endedAt !== undefined && performance.now() - process.endedAt < this.retentionMs;
  }
}

export { STEP_MS, sleep };
