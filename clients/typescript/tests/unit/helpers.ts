/**
 * Cableado de los tests de `Sandbox`: un `rayd` falso por test, un plano de
 * control falso, un logger grabador y utilidades de espera.
 */

import { afterEach, beforeEach } from "vitest";
import { Sandbox, type SandboxCreateOptions } from "../../src/index.js";
import type { Logger } from "../../src/logger.js";
import type { OutputChunk } from "../../src/models.js";
import { encodeAccessToken } from "../../src/payload.js";
import { ReconnectPoll } from "../../src/sandbox/readiness.js";
import { FakeControlPlane, IMAGE_ARN, SANDBOX_ID } from "./fake/control-plane.js";
import { FakeRayd } from "./fake/server.js";

export const ACCESS_TOKEN_SECRET = new TextEncoder().encode("unit-test-access-token-32-bytes!");
export const ACCESS_TOKEN = encodeAccessToken(ACCESS_TOKEN_SECRET);
export const WAIT_BUDGET_MS = 10_000;
export const POLL_MS = 20;
export const SHORT_RECONNECT_TIMEOUT_MS = 5000;
export const FAST_STATE_CHECK_MS = 100;

export interface LoggedLine {
  readonly level: "debug" | "info" | "warn" | "error";
  readonly message: string;
  readonly fields: Readonly<Record<string, unknown>> | undefined;
}

export class RecordingLogger implements Logger {
  readonly lines: LoggedLine[] = [];

  debug(message: string, fields?: Readonly<Record<string, unknown>>): void {
    this.lines.push({ level: "debug", message, fields });
  }

  info(message: string, fields?: Readonly<Record<string, unknown>>): void {
    this.lines.push({ level: "info", message, fields });
  }

  warn(message: string, fields?: Readonly<Record<string, unknown>>): void {
    this.lines.push({ level: "warn", message, fields });
  }

  error(message: string, fields?: Readonly<Record<string, unknown>>): void {
    this.lines.push({ level: "error", message, fields });
  }

  at(level: LoggedLine["level"]): LoggedLine[] {
    return this.lines.filter((line) => line.level === level);
  }

  /** Todo lo logueado como una sola cadena, para buscar secretos. */
  dump(): string {
    return this.lines
      .map((line) => `${line.message} ${JSON.stringify(line.fields ?? {})}`)
      .join("\n");
  }
}

export interface TestSandbox {
  readonly sandbox: Sandbox;
  readonly rayd: FakeRayd;
  readonly plane: FakeControlPlane;
  readonly logger: RecordingLogger;
  close(): Promise<void>;
}

export interface TestSandboxOptions {
  readonly states?: readonly string[];
  readonly reconnectTimeoutMs?: number;
  readonly readyTimeoutMs?: number;
  readonly accessToken?: string;
  readonly create?: Partial<SandboxCreateOptions>;
  readonly beforeCreate?: (rayd: FakeRayd, plane: FakeControlPlane) => void;
}

const opened: TestSandbox[] = [];

afterEach(async () => {
  for (const item of opened.splice(0)) {
    await item.close();
  }
});

export async function startRayd(accessToken = ACCESS_TOKEN): Promise<FakeRayd> {
  return FakeRayd.start({ accessToken });
}

export async function createTestSandbox(options: TestSandboxOptions = {}): Promise<TestSandbox> {
  const rayd = await startRayd(ACCESS_TOKEN);
  const plane = new FakeControlPlane({
    endpoint: rayd.host,
    states: options.states ?? ["PENDING"],
  });
  const logger = new RecordingLogger();
  options.beforeCreate?.(rayd, plane);
  let sandbox: Sandbox;
  try {
    sandbox = await Sandbox.create({
      template: IMAGE_ARN,
      idle: null,
      accessToken: options.accessToken ?? ACCESS_TOKEN,
      controlPlane: plane,
      transport: rayd.transport,
      reconnectTimeoutMs: options.reconnectTimeoutMs ?? SHORT_RECONNECT_TIMEOUT_MS,
      readyTimeoutMs: options.readyTimeoutMs ?? 10_000,
      logger,
      ...(options.create ?? {}),
    });
  } catch (error) {
    await rayd.close();
    throw error;
  }
  const handle: TestSandbox = {
    sandbox,
    rayd,
    plane,
    logger,
    close: async () => {
      sandbox.close();
      await rayd.close();
    },
  };
  opened.push(handle);
  return handle;
}

/** Tests de reconexión: sondeos de 100→400 ms y `get-microvm` cada 100 ms en lugar de 0,5→4 s y 5 s. */
export function useFastStateChecks(): void {
  const previous = {
    initialDelayMs: ReconnectPoll.initialDelayMs,
    maxDelayMs: ReconnectPoll.maxDelayMs,
    stateCheckIntervalMs: ReconnectPoll.stateCheckIntervalMs,
  };
  beforeEach(() => {
    ReconnectPoll.initialDelayMs = 100;
    ReconnectPoll.maxDelayMs = 400;
    ReconnectPoll.stateCheckIntervalMs = FAST_STATE_CHECK_MS;
  });
  afterEach(() => {
    ReconnectPoll.initialDelayMs = previous.initialDelayMs;
    ReconnectPoll.maxDelayMs = previous.maxDelayMs;
    ReconnectPoll.stateCheckIntervalMs = previous.stateCheckIntervalMs;
  });
}

export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export async function waitUntil(
  predicate: () => boolean,
  budgetMs = WAIT_BUDGET_MS,
  label = "la condición no se cumplió a tiempo",
): Promise<void> {
  const deadline = performance.now() + budgetMs;
  while (!predicate()) {
    if (performance.now() >= deadline) {
      throw new Error(label);
    }
    await sleep(POLL_MS);
  }
}

export function withTimeout<T>(promise: Promise<T>, ms: number, label = "timeout"): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error(label)), ms);
  });
  return Promise.race([promise, deadline]).finally(() => {
    if (timer !== undefined) {
      clearTimeout(timer);
    }
  });
}

export async function nextChunk(
  iterator: AsyncIterator<OutputChunk>,
  timeoutMs = WAIT_BUDGET_MS,
): Promise<IteratorResult<OutputChunk>> {
  return withTimeout(iterator.next(), timeoutMs, "el iterador no entregó nada a tiempo");
}

export function chunkText(chunk: OutputChunk): string {
  if (chunk.stdout !== undefined) {
    return chunk.stdout;
  }
  if (chunk.stderr !== undefined) {
    return chunk.stderr;
  }
  return new TextDecoder().decode(chunk.pty);
}

/** Itera un handle hasta ver `needle` en el texto acumulado; devuelve ese texto. */
export async function readUntil(
  handle: AsyncIterable<OutputChunk>,
  needle: string | RegExp,
  timeoutMs = WAIT_BUDGET_MS,
): Promise<string> {
  const iterator = handle[Symbol.asyncIterator]();
  let seen = "";
  const matches = () => (typeof needle === "string" ? seen.includes(needle) : needle.test(seen));
  const deadline = performance.now() + timeoutMs;
  while (!matches()) {
    const remaining = deadline - performance.now();
    if (remaining <= 0) {
      throw new Error(`no llegó ${String(needle)} a tiempo; visto: ${JSON.stringify(seen)}`);
    }
    const result = await nextChunk(iterator, remaining);
    if (result.done) {
      throw new Error(`el stream terminó sin ${String(needle)}; visto: ${JSON.stringify(seen)}`);
    }
    seen += chunkText(result.value);
  }
  return seen;
}

/** Consume un handle en segundo plano acumulando texto y el error final. */
export class Collector {
  readonly chunks: string[] = [];
  error: Error | undefined;
  done = false;
  readonly task: Promise<void>;

  constructor(handle: AsyncIterable<OutputChunk>) {
    this.task = this.#run(handle);
  }

  get text(): string {
    return this.chunks.join("");
  }

  async #run(handle: AsyncIterable<OutputChunk>): Promise<void> {
    try {
      for await (const chunk of handle) {
        this.chunks.push(chunkText(chunk));
      }
    } catch (error) {
      this.error = error as Error;
    } finally {
      this.done = true;
    }
  }

  join(budgetMs = WAIT_BUDGET_MS): Promise<void> {
    return withTimeout(this.task, budgetMs, "el collector no terminó a tiempo");
  }
}

export { FakeControlPlane, FakeRayd, IMAGE_ARN, SANDBOX_ID, Sandbox };
