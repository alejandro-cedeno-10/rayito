/**
 * `CodeService` falso en proceso, con el contrato de `rayd` en M4 y M5.
 *
 * Interpreta una tabla mínima de celdas (`x = 42`, `x`, `print(x)`, `1/0`,
 * `plot`, `df`, `stderr`, `many`, `sleep`, `slow <s>`, `print-then-sleep`,
 * `kernel-die`, `bad-json`, `omitted`, `envs-echo`, `1+1`,
 * `raise ValueError('boom')`, `echo <texto>`, `console.log('<texto>')`; lo
 * demás es `ScriptError`): `started` primero,
 * `seq` desde 1 con `keepalive` a 0, `end` con `execution_count` (0 en los
 * finales sintéticos), contextos `default` + `ctx-<12 hex>` con tope de 8, el
 * kernel gate, el phase gate, `timeout_ms`/`envs` grabados por ejecución y
 * `x-access-token` en cada RPC. Cada ejecución la corre un "recorder" que
 * graba los eventos en un ring: `Reattach(context_id, execution_id, from_seq)`
 * reenvía desde el ring (`NotFound`/`OutOfRange`/`InvalidArgument`),
 * `suspend()` corta los streams vivos con `Unavailable suspending` sin
 * interrumpir la celda, y un cliente que cancela el `Execute` original sigue
 * interrumpiéndola. Desde M7 honra `ExecuteRequest.language` como `rayd`:
 * `language` + `context_id` es `InvalidArgument`; un lenguaje que el fake no
 * "instala" (`languages`) es `Unimplemented` con `rayito-base-poly` en el
 * mensaje; los contextos `default-bash`/`default-javascript` nacen en la
 * primera celda de ese lenguaje y `listContexts` los lista con su lenguaje;
 * `envs` por ejecución en un contexto no Python es `InvalidArgument`.
 */

import { randomBytes } from "node:crypto";
import { create } from "@bufbuild/protobuf";
import { Code, ConnectError, type HandlerContext } from "@connectrpc/connect";
import {
  ContextInfoSchema,
  type CreateContextRequest,
  CreateContextResponseSchema,
  type DestroyContextRequest,
  DestroyContextResponseSchema,
  type ExecuteEvent,
  ExecuteEventSchema,
  type ExecuteRequest,
  ExecutionEndSchema,
  ExecutionErrorSchema,
  ExecutionResultSchema,
  ExecutionStartedSchema,
  ListContextsResponseSchema,
  OutputChunkSchema,
  type ReattachRequest,
  type RestartContextRequest,
  RestartContextResponseSchema,
} from "../../../src/gen/rayito/v1/code_pb.js";
import { KeepAliveSchema } from "../../../src/gen/rayito/v1/common_pb.js";
import {
  AsyncQueue,
  abortWith,
  assertProxyHeaders,
  DEFAULT_CWD,
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

export const DEFAULT_CONTEXT_ID = "default";
export const DEFAULT_LANGUAGE = "python";
export const KNOWN_LANGUAGES: ReadonlySet<string> = new Set(["python", "bash", "javascript"]);
export const POLY_IMAGE = "rayito-base-poly";
const MAX_CONTEXTS = 8;
const MAX_CODE_BYTES = 1_048_576;
const MAX_SUBSCRIBERS = 8;
const TIMEOUT_CAP_MS = 200;
const SLEEP_MAX_MS = 30_000;
const KERNEL_GATE_PREFIX = "kernel not ready";
const EXECUTION_ID = /^exec-[0-9a-f]{16}$/;

export const ONE_PIXEL_PNG_BASE64 = Buffer.from(
  "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c63f8ffff3f0005fe02fe0d58d2fe0000000049454e44ae426082",
  "hex",
).toString("base64");
export const LINE_CHART = {
  type: "line",
  title: "plot",
  x_label: null,
  y_label: null,
  x_unit: null,
  y_unit: null,
  x_ticks: [0.0, 1.0, 2.0],
  x_tick_labels: ["0.0", "1.0", "2.0"],
  x_scale: "linear",
  y_ticks: [1.0, 2.0, 3.0],
  y_tick_labels: ["1.0", "2.0", "3.0"],
  y_scale: "linear",
  elements: [
    {
      label: "_child0",
      points: [
        [0.0, 1.0],
        [1.0, 2.0],
        [2.0, 3.0],
      ],
    },
  ],
};
export const DATAFRAME_TEXT = "   a    b\n0  1  3.5\n1  2  4.5";
export const DATAFRAME_HTML =
  "<table><tr><th>a</th><th>b</th></tr><tr><td>1</td><td>3.5</td></tr></table>";
export const DATAFRAME_DATA = { a: [1, 2], b: [3.5, 4.5] };
export const OMITTED_NOTE = "image/png: 9000000 bytes";
const ASSIGNMENT = /^(\w+) = (.+)$/;
const NAME = /^\w+$/;
const PRINT = /^print\((\w+)\)$/;
const ARITHMETIC = /^(\d+) ?([+*]) ?(\d+)$/;
const RAISE = /^raise (\w+)\('(.*)'\)$/;
const SLOW = /^slow (\d+(?:\.\d+)?)$/;
const ECHO = /^echo (.+)$/;
const CONSOLE_LOG = /^console\.log\('(.*)'\)$/;

export class FakeContext {
  readonly contextId: string;
  cwd: string;
  language: string;
  envs: Record<string, string>;
  namespace = new Map<string, string>();
  executionCount = 0;
  kernelGeneration = 0;

  constructor(
    contextId: string,
    cwd = DEFAULT_CWD,
    envs: Record<string, string> = {},
    language = DEFAULT_LANGUAGE,
  ) {
    this.contextId = contextId;
    this.cwd = cwd;
    this.language = language;
    this.envs = envs;
  }

  reset(): void {
    this.namespace.clear();
    this.executionCount = 0;
    this.kernelGeneration += 1;
  }
}

/** Lo que el `rayd` falso recuerda de cada `Execute`, para las aserciones. */
export interface FakeExecution {
  readonly contextId: string;
  readonly executionId: string;
  readonly code: string;
  readonly timeoutMs: number;
  readonly envs: Record<string, string>;
  interrupted: boolean;
}

type Item = ExecuteEvent | StreamEnd;

/** El "recorder" de una ejecución: ring con `seq`, suscriptores y final. */
export class FakeRun {
  readonly record: FakeExecution;
  readonly ring: ExecuteEvent[] = [];
  nextSeq = 1;
  subscribers: AsyncQueue<Item>[] = [];
  ended = false;
  endedAt: number | undefined;
  detachedOrigin = false;
  readonly interruptSignal = new Signal();

  constructor(record: FakeExecution) {
    this.record = record;
  }

  publish(event: ExecuteEvent): void {
    if (event.event.case !== "keepalive") {
      event.seq = BigInt(this.nextSeq);
      this.nextSeq += 1;
      this.ring.push(event);
    }
    for (const subscriber of this.subscribers) {
      subscriber.push(event);
    }
    if (event.event.case === "end") {
      this.ended = true;
      this.endedAt = performance.now();
      this.subscribers = [];
    }
  }

  subscribe(fromSeq: number): { queue: AsyncQueue<Item> | undefined; replay: Item[] } {
    const oldest = this.ring.length > 0 ? Number((this.ring[0] as ExecuteEvent).seq) : this.nextSeq;
    if (fromSeq > 0 && (fromSeq < oldest || fromSeq > this.nextSeq)) {
      throw new ReplayOutOfRange(oldest, this.nextSeq);
    }
    const replay: Item[] =
      fromSeq > 0 ? this.ring.filter((event) => Number(event.seq) >= fromSeq) : [];
    if (this.ended) {
      return { queue: undefined, replay };
    }
    const queue = new AsyncQueue<Item>();
    this.subscribers.push(queue);
    return { queue, replay };
  }

  unsubscribe(queue: AsyncQueue<Item>): void {
    this.subscribers = this.subscribers.filter((item) => item !== queue);
  }

  /** `/suspend`: los streams se cortan con `Unavailable suspending`, el origen queda desacoplado y la celda sigue. */
  suspend(): void {
    this.detachedOrigin = true;
    for (const subscriber of this.subscribers) {
      subscriber.push(new StreamEnd(Code.Unavailable, "suspending"));
    }
    this.subscribers = [];
  }
}

interface Cell {
  readonly context: FakeContext;
  readonly request: ExecuteRequest;
  readonly record: FakeExecution;
  readonly run: FakeRun;
}

function event(payload: ExecuteEvent["event"]): ExecuteEvent {
  return create(ExecuteEventSchema, { event: payload });
}

function nowNs(): bigint {
  return BigInt(Date.now()) * 1_000_000n;
}

export function stdoutEvent(text: string): ExecuteEvent {
  return event({
    case: "stdout",
    value: create(OutputChunkSchema, { text, timestampUnixNs: nowNs() }),
  });
}

export function stderrEvent(text: string): ExecuteEvent {
  return event({
    case: "stderr",
    value: create(OutputChunkSchema, { text, timestampUnixNs: nowNs() }),
  });
}

export function resultEvent(
  mime: Record<string, string>,
  options: { isMainResult?: boolean; extra?: Record<string, string> } = {},
): ExecuteEvent {
  return event({
    case: "result",
    value: create(ExecutionResultSchema, {
      ...mime,
      isMainResult: options.isMainResult ?? false,
      extra: options.extra ?? {},
    }),
  });
}

export function errorEvent(name: string, value: string, traceback: string[] = []): ExecuteEvent {
  return event({ case: "error", value: create(ExecutionErrorSchema, { name, value, traceback }) });
}

export function endEvent(executionCount: number): ExecuteEvent {
  return event({
    case: "end",
    value: create(ExecutionEndSchema, { executionCount: BigInt(executionCount) }),
  });
}

export function keepaliveEvent(): ExecuteEvent {
  return event({ case: "keepalive", value: create(KeepAliveSchema, {}) });
}

function kernelTraceback(name: string, value: string): string[] {
  return [
    "---------------------------------------------------------------------------",
    `${name}                                Traceback (most recent call last)`,
    `${name}: ${value}`,
  ];
}

function nameError(name: string): ExecuteEvent {
  const value = `name '${name}' is not defined`;
  return errorEvent("NameError", value, kernelTraceback("NameError", value));
}

/** Espera `ms` en pasos cortos; `true` si el cliente interrumpió. */
async function waitOrInterrupt(cell: Cell, ms: number): Promise<boolean> {
  const deadline = performance.now() + ms;
  while (performance.now() < deadline) {
    if (cell.record.interrupted) {
      return true;
    }
    await sleep(Math.min(STEP_MS, Math.max(0, deadline - performance.now())));
  }
  return cell.record.interrupted;
}

type CellScript = (cell: Cell) => AsyncGenerator<ExecuteEvent, void, undefined>;
type PatternScript = (
  cell: Cell,
  match: RegExpMatchArray,
) => AsyncGenerator<ExecuteEvent, void, undefined>;

async function* cellSleep(cell: Cell): AsyncGenerator<ExecuteEvent, void, undefined> {
  const timeoutMs = Number(cell.request.timeoutMs);
  const wait = timeoutMs > 0 ? Math.min(timeoutMs, TIMEOUT_CAP_MS) : SLEEP_MAX_MS;
  if (await waitOrInterrupt(cell, wait)) {
    return;
  }
  if (timeoutMs > 0) {
    yield errorEvent("ExecutionTimeout", `execution exceeded ${timeoutMs} ms`);
    yield endEvent(0);
  }
}

const SCRIPTS: Readonly<Record<string, CellScript>> = {
  "1/0": async function* () {
    yield errorEvent(
      "ZeroDivisionError",
      "division by zero",
      kernelTraceback("ZeroDivisionError", "division by zero"),
    );
  },
  plot: async function* () {
    yield keepaliveEvent();
    yield resultEvent({ png: ONE_PIXEL_PNG_BASE64, chart: JSON.stringify(LINE_CHART) });
  },
  df: async function* () {
    yield resultEvent(
      { text: DATAFRAME_TEXT, html: DATAFRAME_HTML, data: JSON.stringify(DATAFRAME_DATA) },
      { isMainResult: true },
    );
  },
  stderr: async function* () {
    yield stderrEvent("warn\n");
  },
  many: async function* () {
    yield resultEvent({ text: "one" });
    yield resultEvent({ text: "two" }, { isMainResult: true });
    yield resultEvent({ text: "three" });
  },
  sleep: cellSleep,
  "print-then-sleep": async function* (cell) {
    yield stdoutEvent("tick\n");
    yield* cellSleep(cell);
  },
  "kernel-die": async function* (cell) {
    cell.context.reset();
    yield errorEvent("KernelDied", "kernel process exited (code 3)");
    yield endEvent(0);
  },
  "bad-json": async function* () {
    yield resultEvent({ json: "{not json" });
  },
  omitted: async function* () {
    yield resultEvent({}, { extra: { "rayito/omitted": OMITTED_NOTE } });
  },
  "envs-echo": async function* (cell) {
    const envs = cell.request.envs;
    const sorted = Object.fromEntries(
      Object.keys(envs)
        .sort()
        .map((key) => [key, envs[key]]),
    );
    yield stdoutEvent(`${JSON.stringify(sorted)}\n`);
  },
};

const PATTERN_SCRIPTS: ReadonlyArray<[RegExp, PatternScript]> = [
  [
    ASSIGNMENT,
    async function* (cell, match) {
      cell.context.namespace.set(match[1] as string, match[2] as string);
      yield* [];
    },
  ],
  [
    PRINT,
    async function* (cell, match) {
      const value = cell.context.namespace.get(match[1] as string);
      yield value === undefined ? nameError(match[1] as string) : stdoutEvent(`${value}\n`);
    },
  ],
  [
    ARITHMETIC,
    async function* (_cell, match) {
      const left = Number(match[1]);
      const right = Number(match[3]);
      const value = match[2] === "+" ? left + right : left * right;
      yield resultEvent({ text: String(value) }, { isMainResult: true });
    },
  ],
  [
    RAISE,
    async function* (_cell, match) {
      const name = match[1] as string;
      const value = match[2] as string;
      yield errorEvent(name, value, kernelTraceback(name, value));
    },
  ],
  [
    SLOW,
    async function* (cell, match) {
      yield stdoutEvent("slow start\n");
      const wantedMs = Number(match[1]) * 1000;
      const timeoutMs = Number(cell.request.timeoutMs);
      if (timeoutMs > 0 && timeoutMs < wantedMs) {
        if (await waitOrInterrupt(cell, Math.min(timeoutMs, TIMEOUT_CAP_MS))) {
          return;
        }
        yield errorEvent("ExecutionTimeout", `execution exceeded ${timeoutMs} ms`);
        yield endEvent(0);
        return;
      }
      if (await waitOrInterrupt(cell, wantedMs)) {
        return;
      }
      yield resultEvent({ text: "'slow'" }, { isMainResult: true });
    },
  ],
  [
    ECHO,
    async function* (_cell, match) {
      yield stdoutEvent(`${match[1] as string}\n`);
    },
  ],
  [
    CONSOLE_LOG,
    async function* (_cell, match) {
      yield stdoutEvent(`${match[1] as string}\n`);
    },
  ],
  [
    NAME,
    async function* (cell, match) {
      const value = cell.context.namespace.get(match[0]);
      yield value === undefined
        ? nameError(match[0])
        : resultEvent({ text: value }, { isMainResult: true });
    },
  ],
];

async function* cellUnknown(): AsyncGenerator<ExecuteEvent, void, undefined> {
  yield errorEvent(
    "ScriptError",
    "unscripted cell",
    kernelTraceback("ScriptError", "unscripted cell"),
  );
}

function runCell(cell: Cell): AsyncGenerator<ExecuteEvent, void, undefined> {
  const code = cell.request.code;
  const script = SCRIPTS[code];
  if (script !== undefined) {
    return script(cell);
  }
  for (const [pattern, patternScript] of PATTERN_SCRIPTS) {
    const match = code.match(pattern);
    if (match !== null) {
      return patternScript(cell, match);
    }
  }
  return cellUnknown();
}

/** El recorder: `started`, la celda y un `end` con el `execution_count` salvo que la celda emita el suyo. */
async function recordCell(cell: Cell): Promise<void> {
  const context = cell.context;
  context.executionCount += 1;
  const count = context.executionCount;
  cell.run.publish(
    event({
      case: "started",
      value: create(ExecutionStartedSchema, {
        executionId: cell.record.executionId,
        executionCount: BigInt(count),
      }),
    }),
  );
  for await (const item of runCell(cell)) {
    cell.run.publish(item);
    if (item.event.case === "end") {
      return;
    }
  }
  if (cell.record.interrupted) {
    cell.run.publish(errorEvent("KeyboardInterrupt", ""));
  }
  cell.run.publish(endEvent(count));
}

async function* streamSubscriber(
  run: FakeRun,
  queue: AsyncQueue<Item> | undefined,
  prelude: Item[],
  context: HandlerContext,
  origin: boolean,
): AsyncGenerator<ExecuteEvent, void, undefined> {
  try {
    for (const item of prelude) {
      if (item instanceof StreamEnd) {
        abortWith(item);
      }
      yield item;
    }
    if (queue === undefined) {
      return;
    }
    while (!context.signal.aborted) {
      const item = await queue.next(context.signal);
      if (item === undefined) {
        return;
      }
      if (item instanceof StreamEnd) {
        abortWith(item);
      }
      yield item;
      if (item.event.case === "end") {
        return;
      }
    }
  } finally {
    if (queue !== undefined) {
      run.unsubscribe(queue);
    }
    if (origin && !run.ended && !run.detachedOrigin && context.signal.aborted) {
      run.record.interrupted = true;
    }
  }
}

/** `CodeService` como lo implementa `rayd` en M4/M5 (ver módulo). */
export class FakeCodeService {
  readonly tokenSha256: string;
  phase: string | undefined;
  kernelGate: string | undefined;
  retentionMs = 30_000;
  readonly contexts = new Map<string, FakeContext>([
    [DEFAULT_CONTEXT_ID, new FakeContext(DEFAULT_CONTEXT_ID)],
  ]);
  readonly executions: FakeExecution[] = [];
  readonly runs = new Map<string, FakeRun>();
  readonly executeRequests: ExecuteRequest[] = [];
  readonly executeHeaders: HeaderMap[] = [];
  readonly executeDeadlines: Array<number | undefined> = [];
  readonly reattachRequests: ReattachRequest[] = [];
  readonly createRequests: CreateContextRequest[] = [];
  readonly createDeadlines: Array<number | undefined> = [];
  readonly destroyRequests: string[] = [];
  readonly restartRequests: string[] = [];
  readonly lazyContexts: string[] = [];
  languages: ReadonlySet<string> = KNOWN_LANGUAGES;
  nextContext = 1;

  constructor(tokenSha256: string) {
    this.tokenSha256 = tokenSha256;
  }

  get reattachCalls(): Array<[string, string, number]> {
    return this.reattachRequests.map((request) => [
      request.contextId,
      request.executionId,
      Number(request.fromSeq),
    ]);
  }

  createContext(request: CreateContextRequest, context: HandlerContext) {
    const headers = this.#authenticate(context);
    this.createRequests.push(request);
    this.createDeadlines.push(deadlineFromHeaders(headers));
    this.#gateKernel();
    const language = this.#requireLanguage(request.language || DEFAULT_LANGUAGE);
    if (request.cwd !== undefined && !KNOWN_DIRECTORIES.has(request.cwd)) {
      throw new ConnectError("cwd does not exist", Code.InvalidArgument);
    }
    if (this.contexts.size >= MAX_CONTEXTS) {
      throw new ConnectError(`max ${MAX_CONTEXTS} contexts`, Code.ResourceExhausted);
    }
    const contextId = `ctx-${this.nextContext.toString(16).padStart(12, "0")}`;
    this.nextContext += 1;
    this.contexts.set(
      contextId,
      new FakeContext(contextId, request.cwd ?? DEFAULT_CWD, { ...request.envs }, language),
    );
    return create(CreateContextResponseSchema, { contextId });
  }

  execute(request: ExecuteRequest, context: HandlerContext): AsyncIterable<ExecuteEvent> {
    const headers = this.#authenticate(context);
    this.executeHeaders.push(headers);
    this.executeRequests.push(request);
    this.executeDeadlines.push(deadlineFromHeaders(headers));
    this.#gatePhase();
    this.#gateKernel();
    if (Buffer.byteLength(request.code, "utf8") > MAX_CODE_BYTES) {
      throw new ConnectError("code exceeds 1 MiB", Code.InvalidArgument);
    }
    const fakeContext = this.#executeContext(request);
    if (Object.keys(request.envs).length > 0 && fakeContext.language !== DEFAULT_LANGUAGE) {
      throw new ConnectError(
        "envs per execution are only supported on python contexts",
        Code.InvalidArgument,
      );
    }
    const record: FakeExecution = {
      contextId: fakeContext.contextId,
      executionId: `exec-${randomBytes(8).toString("hex")}`,
      code: request.code,
      timeoutMs: Number(request.timeoutMs),
      envs: { ...request.envs },
      interrupted: false,
    };
    const run = new FakeRun(record);
    this.executions.push(record);
    this.runs.set(record.executionId, run);
    const { queue, replay } = run.subscribe(0);
    void recordCell({ context: fakeContext, request, record, run });
    return streamSubscriber(run, queue, replay, context, true);
  }

  reattach(request: ReattachRequest, context: HandlerContext): AsyncIterable<ExecuteEvent> {
    this.#authenticate(context);
    this.reattachRequests.push(request);
    this.#gatePhase();
    if (!EXECUTION_ID.test(request.executionId)) {
      throw new ConnectError("malformed execution_id", Code.InvalidArgument);
    }
    const run = this.#retainedRun(request);
    if (run.subscribers.length >= MAX_SUBSCRIBERS) {
      throw new ConnectError(`max ${MAX_SUBSCRIBERS} subscribers`, Code.ResourceExhausted);
    }
    let subscription: ReturnType<FakeRun["subscribe"]>;
    try {
      subscription = run.subscribe(Number(request.fromSeq));
    } catch (error) {
      if (error instanceof ReplayOutOfRange) {
        throw new ConnectError(error.message, Code.OutOfRange);
      }
      throw error;
    }
    return streamSubscriber(run, subscription.queue, subscription.replay, context, false);
  }

  listContexts(_request: unknown, context: HandlerContext) {
    this.#authenticate(context);
    return create(ListContextsResponseSchema, {
      contexts: [...this.contexts.values()].map((item) =>
        create(ContextInfoSchema, {
          contextId: item.contextId,
          language: item.language,
          cwd: item.cwd,
        }),
      ),
    });
  }

  destroyContext(request: DestroyContextRequest, context: HandlerContext) {
    this.#authenticate(context);
    this.destroyRequests.push(request.contextId);
    if (request.contextId === DEFAULT_CONTEXT_ID) {
      throw new ConnectError("the default context is protected", Code.FailedPrecondition);
    }
    this.#context(request.contextId);
    this.contexts.delete(request.contextId);
    return create(DestroyContextResponseSchema, {});
  }

  restartContext(request: RestartContextRequest, context: HandlerContext) {
    this.#authenticate(context);
    this.restartRequests.push(request.contextId);
    this.#gateKernel();
    this.#context(request.contextId).reset();
    return create(RestartContextResponseSchema, {});
  }

  /** `/suspend` con `CodeService`: los `Execute`/`Reattach` vivos terminan con `Unavailable suspending`. */
  suspend(): void {
    for (const run of this.runs.values()) {
      if (!run.ended) {
        run.suspend();
      }
    }
    this.phase = "suspending";
  }

  resume(): void {
    this.phase = undefined;
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

  #gateKernel(): void {
    if (this.kernelGate !== undefined) {
      throw new ConnectError(`${KERNEL_GATE_PREFIX}: ${this.kernelGate}`, Code.Unavailable);
    }
  }

  #context(contextId: string): FakeContext {
    const found = this.contexts.get(contextId);
    if (found === undefined) {
      throw new ConnectError("context not found", Code.NotFound);
    }
    return found;
  }

  #requireLanguage(language: string): string {
    if (!KNOWN_LANGUAGES.has(language)) {
      throw new ConnectError(
        "language must be one of python, bash, javascript",
        Code.InvalidArgument,
      );
    }
    if (!this.languages.has(language)) {
      throw new ConnectError(
        `language ${language} is not installed in this image; use ${POLY_IMAGE}`,
        Code.Unimplemented,
      );
    }
    return language;
  }

  /** La tabla de rutas de `rayd`: `context_id` explícito, `default` para Python o el contexto por defecto del lenguaje. */
  #executeContext(request: ExecuteRequest): FakeContext {
    if (request.language !== undefined && request.contextId) {
      throw new ConnectError("language cannot be combined with context_id", Code.InvalidArgument);
    }
    if (request.language === undefined) {
      return this.#context(request.contextId || DEFAULT_CONTEXT_ID);
    }
    const language = this.#requireLanguage(request.language);
    if (language === DEFAULT_LANGUAGE) {
      return this.#context(DEFAULT_CONTEXT_ID);
    }
    const contextId = `${DEFAULT_CONTEXT_ID}-${language}`;
    const existing = this.contexts.get(contextId);
    if (existing !== undefined) {
      return existing;
    }
    if (this.contexts.size >= MAX_CONTEXTS) {
      throw new ConnectError(`max ${MAX_CONTEXTS} contexts`, Code.ResourceExhausted);
    }
    const created = new FakeContext(contextId, DEFAULT_CWD, {}, language);
    this.contexts.set(contextId, created);
    this.lazyContexts.push(contextId);
    return created;
  }

  #retainedRun(request: ReattachRequest): FakeRun {
    const run = this.runs.get(request.executionId);
    const contextId = request.contextId || DEFAULT_CONTEXT_ID;
    if (run === undefined || run.record.contextId !== contextId || this.#expired(run)) {
      throw new ConnectError("execution not found", Code.NotFound);
    }
    return run;
  }

  #expired(run: FakeRun): boolean {
    return run.endedAt !== undefined && performance.now() - run.endedAt >= this.retentionMs;
  }
}
