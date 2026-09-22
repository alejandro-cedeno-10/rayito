/**
 * `sandbox.runCode` y los contextos de código (`CodeService`): validación,
 * construcción de requests, aritmética de deadlines, conversión de protos, el
 * `ExecutionBuilder` que convierte el stream de `Execute` en una `Execution`
 * y el `Reattach` tras un suspend/resume.
 */

import { create } from "@bufbuild/protobuf";
import { ConnectError } from "@connectrpc/connect";
import { parseChart } from "../charts.js";
import { InvalidArgumentError, NotFoundError, SandboxError, TimeoutError } from "../errors.js";
import {
  CodeService,
  type ContextInfo,
  type CreateContextRequest,
  CreateContextRequestSchema,
  DestroyContextRequestSchema,
  type ExecuteEvent,
  type ExecuteRequest,
  ExecuteRequestSchema,
  type ExecutionError as ExecutionErrorProto,
  type ExecutionResult,
  ListContextsRequestSchema,
  type ReattachRequest,
  ReattachRequestSchema,
  RestartContextRequestSchema,
} from "../gen/rayito/v1/code_pb.js";
import {
  type CodeContext,
  Execution,
  type ExecutionError,
  type OutputMessage,
  Result,
  type ResultFields,
} from "../models.js";
import { DEFAULT_WORKDIR, validatedEnvs } from "../payload.js";
import { deadlineAt, type RequestOptions, remainingDeadlineMs, timeoutToMs } from "./commands.js";
import { type OpenedStream, type SandboxCore, withTimeout } from "./core.js";
import { ReconnectBudget } from "./readiness.js";

export const DEFAULT_CODE_TIMEOUT_MS = 300_000;
export const CODE_STREAM_GRACE_MS = 15_000;
export const CONTEXT_REQUEST_TIMEOUT_MS = 90_000;
export const MAX_CODE_BYTES = 1_048_576;
export const DEFAULT_CONTEXT_ID = "default";
export const DEFAULT_LANGUAGE = "python";
export const SUPPORTED_LANGUAGES: ReadonlySet<string> = new Set([
  DEFAULT_LANGUAGE,
  "bash",
  "javascript",
]);
export const LANGUAGE_ALIASES: ReadonlyMap<string, string> = new Map([["js", "javascript"]]);

export const RESULT_MIME_FIELDS = [
  ["text", "text/plain"],
  ["html", "text/html"],
  ["markdown", "text/markdown"],
  ["svg", "image/svg+xml"],
  ["png", "image/png"],
  ["jpeg", "image/jpeg"],
  ["pdf", "application/pdf"],
  ["latex", "text/latex"],
  ["javascript", "application/javascript"],
  ["json", "application/json"],
  ["data", "e2b/data"],
  ["chart", "e2b/chart"],
] as const;
export type ResultFieldName = (typeof RESULT_MIME_FIELDS)[number][0];
const PARSED_JSON_FIELDS: ReadonlySet<string> = new Set(["json", "data"]);
const EXECUTION_ID_PATTERN = /^exec-[0-9a-f]{16}$/;

export type StdoutCallback = (message: OutputMessage) => void;
export type ResultCallback = (result: Result) => void;
export type ErrorCallback = (error: ExecutionError) => void;
export type ContextLike = CodeContext | string;

export interface RunCodeOptions extends RequestOptions {
  /**
   * Kernel de la celda: `python` (por defecto), `bash` o `javascript` (alias
   * `js`, sin distinguir mayúsculas). Selecciona el contexto por defecto de
   * ese lenguaje (`default-bash`), que el agente crea en la primera celda;
   * el kernel `bash` sólo existe en la variante de imagen `rayito-base-poly`
   * (`InvalidArgumentError` con `Unimplemented` en las demás) y `javascript`
   * es un nombre reservado sin kernel en ninguna imagen (`Unimplemented` en
   * todas). Excluyente con `context`.
   */
  readonly language?: string | undefined;
  readonly context?: ContextLike | undefined;
  readonly onStdout?: StdoutCallback | undefined;
  readonly onStderr?: StdoutCallback | undefined;
  readonly onResult?: ResultCallback | undefined;
  readonly onError?: ErrorCallback | undefined;
  readonly envs?: Readonly<Record<string, string>> | undefined;
  /** Timeout del agente en ms (300 000 por defecto; `0` = sin límite). */
  readonly timeoutMs?: number | undefined;
}

export interface CreateContextOptions extends RequestOptions {
  readonly cwd?: string | undefined;
  readonly language?: string | undefined;
  readonly envs?: Readonly<Record<string, string>> | undefined;
}

export function validateCode(code: unknown): string {
  if (typeof code !== "string") {
    throw new InvalidArgumentError(`code debe ser string, recibido ${typeof code}`);
  }
  const size = Buffer.byteLength(code, "utf8");
  if (size > MAX_CODE_BYTES) {
    throw new InvalidArgumentError(
      `code ocupa ${size} bytes y el máximo es ${MAX_CODE_BYTES} (1 MiB); escribe el ` +
        "código en un fichero con files.write() y ejecútalo desde allí",
    );
  }
  return code;
}

/** `undefined` es el contexto por defecto del agente (se omite en el request). */
export function resolveContextId(context: ContextLike | undefined): string | undefined {
  return context === undefined ? undefined : requireContextId(context);
}

export function requireContextId(context: ContextLike | undefined): string {
  const id = typeof context === "object" && context !== null ? context.id : context;
  if (typeof id !== "string" || id.length === 0) {
    throw new InvalidArgumentError(`context inválido: ${JSON.stringify(context)}`);
  }
  return id;
}

/**
 * El nombre canónico del kernel (`python`, `bash`, `javascript`) sin
 * distinguir mayúsculas y con el alias `js`; `undefined` o `""` es "no
 * enviar" (el contexto indicado o el de Python).
 */
export function normalizeLanguage(language: string | undefined): string | undefined {
  if (language === undefined || language === "") {
    return undefined;
  }
  if (typeof language !== "string") {
    throw new InvalidArgumentError(
      `language debe ser string o undefined, recibido ${typeof language}`,
    );
  }
  const lowered = language.toLowerCase();
  const canonical = LANGUAGE_ALIASES.get(lowered) ?? lowered;
  if (!SUPPORTED_LANGUAGES.has(canonical)) {
    throw new InvalidArgumentError(
      `language debe ser uno de ${[...SUPPORTED_LANGUAGES].sort().join(", ")} (o undefined), ` +
        `recibido ${JSON.stringify(language)}`,
    );
  }
  return canonical;
}

/** Como `normalizeLanguage`, con `python` por defecto (lo que `createCodeContext` envía). */
export function validateLanguage(language: string | undefined): string {
  return normalizeLanguage(language) ?? DEFAULT_LANGUAGE;
}

/**
 * El id del contexto por defecto que el agente elige para el `language` del
 * request (`default` para Python), o `undefined` si no viaja `language`; es
 * el `contextId` que `Reattach` necesita tras una reconexión.
 */
export function languageDefaultContextId(request: ExecuteRequest): string | undefined {
  if (request.language === undefined) {
    return undefined;
  }
  return request.language === DEFAULT_LANGUAGE
    ? DEFAULT_CONTEXT_ID
    : `${DEFAULT_CONTEXT_ID}-${request.language}`;
}

export function validateCwd(cwd: string | undefined): string | undefined {
  if (cwd === undefined || cwd === "") {
    return undefined;
  }
  if (typeof cwd !== "string" || cwd.includes("\0") || !cwd.startsWith("/")) {
    throw new InvalidArgumentError(
      `cwd debe ser una ruta absoluta, recibido ${JSON.stringify(cwd)}`,
    );
  }
  return cwd;
}

export interface ExecuteRequestInput {
  readonly contextId?: string | undefined;
  readonly language?: string | undefined;
  readonly envs?: Readonly<Record<string, string>> | undefined;
  readonly timeoutMs?: number | undefined;
}

/**
 * `timeout_ms` lo impone el agente (`interrupt` al vencer, reinicio del
 * contexto 5 s después); `0` es sin límite. `language` sólo viaja cuando se
 * pide y es excluyente con `contextId`, como en E2B.
 */
export function buildExecuteRequest(code: string, input: ExecuteRequestInput = {}): ExecuteRequest {
  const language = normalizeLanguage(input.language);
  if (input.contextId && language !== undefined) {
    throw new InvalidArgumentError("language y context son excluyentes");
  }
  const request = create(ExecuteRequestSchema, {
    code: validateCode(code),
    timeoutMs: BigInt(timeoutToMs(input.timeoutMs ?? DEFAULT_CODE_TIMEOUT_MS)),
  });
  if (input.contextId) {
    request.contextId = input.contextId;
  }
  if (language !== undefined) {
    request.language = language;
  }
  if (input.envs !== undefined && Object.keys(input.envs).length > 0) {
    request.envs = validatedEnvs(input.envs);
  }
  return request;
}

export function buildCreateContextRequest(input: CreateContextOptions = {}): CreateContextRequest {
  const request = create(CreateContextRequestSchema, {
    language: validateLanguage(input.language),
  });
  const cwd = validateCwd(input.cwd);
  if (cwd !== undefined) {
    request.cwd = cwd;
  }
  if (input.envs !== undefined && Object.keys(input.envs).length > 0) {
    request.envs = validatedEnvs(input.envs);
  }
  return request;
}

/** Deadline gRPC de `Execute`: `timeoutMs + 15 s`, para que el `end` del servidor llegue antes. */
export function executeDeadlineMs(timeoutMs: number | undefined): number | undefined {
  const ms = timeoutToMs(timeoutMs);
  return ms === 0 ? undefined : ms + CODE_STREAM_GRACE_MS;
}

/** `Reattach{context_id, execution_id, from_seq}`; `from_seq = N` reenvía los retenidos con `seq >= N`. */
export function buildReattachRequest(
  contextId: string,
  executionId: string,
  fromSeq: number,
): ReattachRequest {
  if (!EXECUTION_ID_PATTERN.test(executionId)) {
    throw new InvalidArgumentError(`executionId inválido: ${JSON.stringify(executionId)}`);
  }
  if (!Number.isInteger(fromSeq) || fromSeq < 0) {
    throw new InvalidArgumentError(`fromSeq debe ser un entero >= 0, recibido ${String(fromSeq)}`);
  }
  return create(ReattachRequestSchema, {
    contextId: requireContextId(contextId),
    executionId,
    fromSeq: BigInt(fromSeq),
  });
}

/** Un `NotFound` o un `OutOfRange` al reenganchar una celda: el resultado ya no se puede reconstruir. */
export function reattachFailure(error: Error): SandboxError {
  return new SandboxError(
    `no se pudo reenganchar la ejecución tras la reconexión: ${error.message}; la celda corrió ` +
      "pero su salida se perdió",
    { cause: error },
  );
}

export function parseJsonOrRaw(document: string): unknown {
  try {
    return JSON.parse(document);
  } catch {
    return document;
  }
}

function parsedResultField(name: ResultFieldName, value: string): unknown {
  if (name === "chart") {
    return parseChart(value);
  }
  if (PARSED_JSON_FIELDS.has(name)) {
    return parseJsonOrRaw(value);
  }
  return value;
}

export function resultFromProto(result: ExecutionResult): Result {
  const raw: Record<string, string> = {};
  const fields: Record<string, unknown> = {};
  for (const [name, mime] of RESULT_MIME_FIELDS) {
    const value = result[name];
    if (value === undefined) {
      continue;
    }
    raw[mime] = value;
    fields[name] = parsedResultField(name, value);
  }
  const extra: Record<string, string> = { ...result.extra };
  Object.assign(raw, extra);
  return new Result({ ...(fields as ResultFields), isMainResult: result.isMainResult, extra, raw });
}

export function errorFromProto(error: ExecutionErrorProto): ExecutionError {
  return Object.freeze({
    name: error.name,
    value: error.value,
    traceback: error.traceback.join("\n"),
  });
}

export function contextFromProto(info: ContextInfo): CodeContext {
  return Object.freeze({
    id: info.contextId,
    language: info.language || DEFAULT_LANGUAGE,
    cwd: info.cwd || DEFAULT_WORKDIR,
  });
}

/** Lo que `createCodeContext` devuelve si `ListContexts` no lista el contexto recién creado. */
export function fallbackContext(
  contextId: string,
  options: { readonly language?: string | undefined; readonly cwd?: string | undefined },
): CodeContext {
  return Object.freeze({
    id: contextId,
    language: validateLanguage(options.language),
    cwd: options.cwd || DEFAULT_WORKDIR,
  });
}

export interface ExecutionBuilderOptions {
  readonly contextId?: string | undefined;
  readonly onStdout?: StdoutCallback | undefined;
  readonly onStderr?: StdoutCallback | undefined;
  readonly onResult?: ResultCallback | undefined;
  readonly onError?: ErrorCallback | undefined;
}

/**
 * Consume los `ExecuteEvent` de una ejecución y construye la `Execution`.
 * `feed` devuelve `true` en el `end`. Los `keepalive` se ignoran; un error en
 * un callback se propaga al caller (como en E2B). Un segundo `started` o
 * cualquier evento después del `end` es una violación de protocolo.
 */
export class ExecutionBuilder {
  readonly execution = new Execution();
  readonly contextId: string;
  executionId: string | undefined;
  lastSeq = 0;
  reattached = 0;
  started = false;
  ended = false;
  readonly #options: ExecutionBuilderOptions;

  constructor(options: ExecutionBuilderOptions = {}) {
    this.#options = options;
    this.contextId = options.contextId || DEFAULT_CONTEXT_ID;
  }

  feed(event: ExecuteEvent): boolean {
    const kind = event.event.case;
    if (kind === "keepalive") {
      return false;
    }
    if (this.ended) {
      throw new SandboxError(
        `protocol violation: llegó ${JSON.stringify(kind)} después del end de la ejecución`,
      );
    }
    switch (event.event.case) {
      case "started":
        this.#onStarted(event.event.value.executionId, Number(event.event.value.executionCount));
        break;
      case "stdout":
        this.#onOutput(event.event.value.text, Number(event.event.value.timestampUnixNs), false);
        break;
      case "stderr":
        this.#onOutput(event.event.value.text, Number(event.event.value.timestampUnixNs), true);
        break;
      case "result":
        this.#onResult(resultFromProto(event.event.value));
        break;
      case "error":
        this.#onError(errorFromProto(event.event.value));
        break;
      case "end":
        this.#onEnd(Number(event.event.value.executionCount));
        break;
      default:
        throw new SandboxError(
          `protocol violation: evento desconocido ${JSON.stringify(kind ?? null)}`,
        );
    }
    this.lastSeq = Math.max(this.lastSeq, Number(event.seq));
    return this.ended;
  }

  /** El `Reattach` que continúa esta ejecución sin huecos; sólo tiene sentido tras `started`. */
  reattachRequest(): ReattachRequest {
    if (this.executionId === undefined) {
      throw new SandboxError("no se puede reenganchar una ejecución sin started");
    }
    return buildReattachRequest(this.contextId, this.executionId, this.lastSeq + 1);
  }

  finish(): Execution {
    if (!this.ended) {
      throw new SandboxError("el stream de Execute terminó sin ExecutionEnd");
    }
    return this.execution;
  }

  #onStarted(executionId: string, executionCount: number): void {
    if (this.started) {
      throw new SandboxError("protocol violation: segundo started en la misma ejecución");
    }
    this.started = true;
    this.executionId = executionId;
    this.execution.executionCount = executionCount;
  }

  #onOutput(line: string, timestamp: number, error: boolean): void {
    const message: OutputMessage = Object.freeze({ line, timestamp, error });
    if (error) {
      this.execution.logs.stderr.push(line);
      this.#options.onStderr?.(message);
    } else {
      this.execution.logs.stdout.push(line);
      this.#options.onStdout?.(message);
    }
  }

  #onResult(result: Result): void {
    this.execution.results.push(result);
    this.#options.onResult?.(result);
  }

  #onError(error: ExecutionError): void {
    this.execution.error = error;
    this.#options.onError?.(error);
  }

  #onEnd(executionCount: number): void {
    this.ended = true;
    if (executionCount > 0) {
      this.execution.executionCount = executionCount;
    }
  }
}

/** `CodeService` del sandbox; la superficie pública vive en `Sandbox`. */
export class CodeClient {
  readonly core: SandboxCore;

  constructor(core: SandboxCore) {
    this.core = core;
  }

  async runCode(code: string, options: RunCodeOptions = {}): Promise<Execution> {
    const contextId = resolveContextId(options.context);
    const request = buildExecuteRequest(code, {
      contextId,
      language: options.language,
      envs: options.envs,
      timeoutMs: options.timeoutMs,
    });
    const deadline =
      options.requestTimeoutMs === undefined
        ? executeDeadlineMs(options.timeoutMs ?? DEFAULT_CODE_TIMEOUT_MS)
        : options.requestTimeoutMs;
    const builder = new ExecutionBuilder({
      contextId: contextId ?? languageDefaultContextId(request),
      onStdout: options.onStdout,
      onStderr: options.onStderr,
      onResult: options.onResult,
      onError: options.onError,
    });
    const opened = await this.core.openStream(
      (client, callOptions) => client.execute(request, withTimeout(callOptions, deadline)),
      { service: CodeService, stream: false, reconnect: false },
    );
    return this.#consume(opened, builder, deadlineAt(deadline, this.core.now));
  }

  async createContext(options: CreateContextOptions = {}): Promise<CodeContext> {
    const request = buildCreateContextRequest(options);
    const response = await this.core.codeCall(
      (client, callOptions) => client.createContext(request, callOptions),
      options.requestTimeoutMs,
      CONTEXT_REQUEST_TIMEOUT_MS,
    );
    const contextId = response.contextId;
    for (const listed of await this.listContexts({ requestTimeoutMs: options.requestTimeoutMs })) {
      if (listed.id === contextId) {
        return listed;
      }
    }
    return fallbackContext(contextId, { language: options.language, cwd: options.cwd });
  }

  async listContexts(options: RequestOptions = {}): Promise<CodeContext[]> {
    const response = await this.core.codeCall(
      (client, callOptions) =>
        client.listContexts(create(ListContextsRequestSchema, {}), callOptions),
      options.requestTimeoutMs,
    );
    return response.contexts.map(contextFromProto);
  }

  async removeContext(context: ContextLike, options: RequestOptions = {}): Promise<void> {
    const request = create(DestroyContextRequestSchema, { contextId: requireContextId(context) });
    await this.core.codeCall(
      (client, callOptions) => client.destroyContext(request, callOptions),
      options.requestTimeoutMs,
    );
  }

  async restartContext(context: ContextLike, options: RequestOptions = {}): Promise<void> {
    const request = create(RestartContextRequestSchema, { contextId: requireContextId(context) });
    await this.core.codeCall(
      (client, callOptions) => client.restartContext(request, callOptions),
      options.requestTimeoutMs,
      CONTEXT_REQUEST_TIMEOUT_MS,
    );
  }

  async #consume(
    opened: OpenedStream<ExecuteEvent>,
    builder: ExecutionBuilder,
    deadlineAtMs: number | undefined,
  ): Promise<Execution> {
    const feed = new ExecutionFeed(this.core, opened, builder, deadlineAtMs);
    try {
      await feed.run();
    } finally {
      this.core.releaseStream(feed.opened.controller);
    }
    return builder.finish();
  }
}

/** Consume `Execute` y, tras un corte reconectable posterior a `started`, `Reattach`. */
export class ExecutionFeed {
  readonly #core: SandboxCore;
  opened: OpenedStream<ExecuteEvent>;
  #first: ExecuteEvent | undefined;
  readonly #builder: ExecutionBuilder;
  readonly #deadlineAt: number | undefined;
  #generation: number;
  readonly #budget = new ReconnectBudget();
  finished = false;

  constructor(
    core: SandboxCore,
    opened: OpenedStream<ExecuteEvent>,
    builder: ExecutionBuilder,
    deadlineAtMs: number | undefined,
  ) {
    this.#core = core;
    this.opened = opened;
    this.#first = opened.first;
    this.#builder = builder;
    this.#deadlineAt = deadlineAtMs;
    this.#generation = core.resumeGeneration;
    core.trackStream(opened.controller);
  }

  async run(): Promise<void> {
    while (!this.finished) {
      const cut = await this.#feedUntilCut();
      if (cut === undefined) {
        return;
      }
      await this.#reattach(cut);
    }
  }

  async #feedUntilCut(): Promise<ConnectError | undefined> {
    try {
      if (this.#first !== undefined && this.#builder.feed(this.#first)) {
        this.finished = true;
        return undefined;
      }
      this.#first = undefined;
      while (true) {
        const result = await this.opened.iterator.next();
        if (result.done) {
          return undefined;
        }
        if (this.#builder.feed(result.value)) {
          this.finished = true;
          return undefined;
        }
      }
    } catch (error) {
      if (this.#builder.started && this.#core.isReconnectable(error)) {
        return error as ConnectError;
      }
      if (error instanceof ConnectError) {
        throw await this.#core.streamFailure(error);
      }
      throw error;
    }
  }

  async #reattach(reason: ConnectError): Promise<void> {
    const core = this.#core;
    const outcome = await core.reconnect(reason, this.#generation, {
      wake: core.foregroundStreamWakes(),
    });
    if (!outcome.resumed) {
      throw core.reconnectError(outcome, reason);
    }
    this.#generation = outcome.resumeGeneration;
    if (!this.#budget.allows(outcome)) {
      throw await core.streamFailure(reason);
    }
    const request = this.#builder.reattachRequest();
    core.releaseStream(this.opened.controller);
    try {
      this.opened = await core.openStream(
        (client, callOptions) =>
          client.reattach(request, withTimeout(callOptions, this.#remainingDeadline())),
        { service: CodeService, stream: false },
      );
    } catch (error) {
      if (error instanceof NotFoundError) {
        throw reattachFailure(error);
      }
      throw error;
    }
    core.trackStream(this.opened.controller);
    this.#first = this.opened.first;
    this.#builder.reattached += 1;
    core.logger?.info?.("ejecución continuada con Reattach", {
      sandboxId: core.sandboxId,
      executionId: this.#builder.executionId,
      fromSeq: Number(request.fromSeq),
      resumeGeneration: this.#generation,
    });
  }

  #remainingDeadline(): number | undefined {
    const remaining = remainingDeadlineMs(this.#deadlineAt, this.#core.now);
    if (remaining !== undefined && remaining <= 0) {
      throw new TimeoutError("el deadline de la ejecución venció durante la reconexión");
    }
    return remaining;
  }
}
