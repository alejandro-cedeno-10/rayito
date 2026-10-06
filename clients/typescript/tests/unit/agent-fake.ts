/**
 * Dobles de prueba para `sbx.agent` (`ai-agent-core`, design.md §11): nunca
 * tocan Connect/gRPC ni AWS. `Agent`/`AgentStream` sólo necesitan de un
 * `Sandbox` los cuatro campos de `AgentSandbox` (`commands`, `files`,
 * `gateways`, `instrumentation`), así que estos dobles sólo implementan eso.
 *
 * `FakeAgentRuntime` traduce líneas JSON (`{"event": "step_started", ...}`)
 * a los eventos de `src/agent/events.ts`: lo que un test escribe en
 * `lines` es ya el protocolo, no el JSONL real de OpenCode.
 */

import { type AgentEvent, agentFailed, type Done, TokenUsage } from "../../src/agent/events.js";
import type {
  AgentRuntime,
  RunCommand,
  RunRequest,
  RuntimeFiles,
  RuntimeState,
  WarmupStep,
} from "../../src/agent/runtime.js";
import type { AgentCommandHandle, AgentSandbox } from "../../src/agent/stream.js";
import { type Instrumentation, NOOP } from "../../src/otel.js";

export const FAKE_SESSION_ID = "fake-session-1";

function eventFromJson(payload: Record<string, unknown>): AgentEvent {
  const kind = payload.event as string;
  switch (kind) {
    case "text_delta":
      return { type: "text_delta", text: payload.text as string };
    case "text":
      return { type: "text", text: payload.text as string };
    case "reasoning":
      return { type: "reasoning", text: payload.text as string };
    case "tool_call":
      return {
        type: "tool_call",
        callId: payload.callId as string,
        name: payload.name as string,
        status: payload.status as "completed" | "error",
        input: payload.input as Record<string, unknown> | undefined,
        output: payload.output as string | undefined,
        outputTruncated: (payload.outputTruncated as boolean) ?? false,
      };
    case "step_started":
      return { type: "step_started", index: payload.index as number };
    case "step_finished":
      return {
        type: "step_finished",
        index: payload.index as number,
        usage: new TokenUsage((payload.usage as Record<string, number>) ?? {}),
        finishReason: payload.finishReason as string | undefined,
      };
    case "agent_failed":
      return agentFailed(payload.reason as string, {
        exitCode: payload.exitCode as number | undefined,
        detailCode: payload.detailCode as string | undefined,
        sessionId: payload.sessionId as string | undefined,
      });
    case "done":
      return {
        type: "done",
        sessionId: (payload.sessionId as string) ?? FAKE_SESSION_ID,
        exitCode: payload.exitCode as number,
        usage: new TokenUsage((payload.usage as Record<string, number>) ?? {}),
      };
    default:
      throw new Error(`evento de prueba desconocido: ${kind}`);
  }
}

export function line(payload: Record<string, unknown>): Uint8Array {
  return new TextEncoder().encode(JSON.stringify(payload));
}

interface FakeState extends RuntimeState {
  sessionId: string;
}

export interface FakeAgentRuntimeOptions {
  readonly configSha?: string;
  readonly abortCmd?: string;
  readonly finishResult?: Done | import("../../src/agent/events.js").AgentFailed;
  readonly warmup?: readonly WarmupStep[];
}

export class FakeAgentRuntime implements AgentRuntime {
  readonly name: string;
  readonly #configSha: string;
  readonly #abortCmd: string | undefined;
  readonly #finishResult: Done | import("../../src/agent/events.js").AgentFailed | undefined;
  readonly #warmup: readonly WarmupStep[];
  readonly buildConfigCalls: Readonly<Record<string, string>>[] = [];

  constructor(name = "fake", options: FakeAgentRuntimeOptions = {}) {
    this.name = name;
    this.#configSha = options.configSha ?? "sha-1";
    this.#abortCmd = options.abortCmd;
    this.#finishResult = options.finishResult;
    this.#warmup = options.warmup ?? [];
  }

  buildConfig(
    _spec: unknown,
    options: { gatewayUrls: Readonly<Record<string, string>>; workdir: string },
  ): RuntimeFiles {
    this.buildConfigCalls.push(options.gatewayUrls);
    return {
      files: [{ path: `${options.workdir}/.rayito/agent/fake.json`, data: new Uint8Array() }],
      configSha256: this.#configSha,
    };
  }

  command(request: RunRequest): RunCommand {
    return {
      script: "exec rayito-agent-fake-run",
      envs: { RAYITO_AGENT_FAKE: "1" },
      stdin: new TextEncoder().encode(request.prompt),
    };
  }

  newState(): RuntimeState {
    return { sessionId: FAKE_SESSION_ID } satisfies FakeState;
  }

  parseLine(lineBytes: Uint8Array, state: RuntimeState): readonly AgentEvent[] {
    const payload = JSON.parse(new TextDecoder().decode(lineBytes)) as Record<string, unknown>;
    const event = eventFromJson(payload);
    if (event.type === "done") {
      (state as FakeState).sessionId = event.sessionId;
    }
    return [event];
  }

  finish(
    state: RuntimeState,
    exitCode: number,
  ): Done | import("../../src/agent/events.js").AgentFailed {
    if (this.#finishResult !== undefined) {
      return this.#finishResult;
    }
    const sessionId = (state as FakeState).sessionId;
    if (exitCode === 0) {
      return { type: "done", sessionId, exitCode, usage: new TokenUsage() };
    }
    return agentFailed("runtime_error", { exitCode, sessionId });
  }

  abortCommand(): string | undefined {
    return this.#abortCmd;
  }

  templateSteps() {
    return [];
  }

  warmupSteps(): readonly WarmupStep[] {
    return this.#warmup;
  }
}

export const FAKE_PID = 4242;

export class FakeCommandHandle implements AgentCommandHandle {
  readonly pid = FAKE_PID;
  readonly #chunks: string[];
  #index = 0;
  #exitCode: number | undefined;
  readonly #raiseOnIterate: Error | undefined;
  readonly #raiseOnWait: Error | undefined;
  killed = false;
  disconnected = false;
  stdin: Uint8Array | undefined;
  stdinClosed = false;

  constructor(options: {
    readonly lines: readonly Uint8Array[];
    readonly exitCode?: number | undefined;
    readonly raiseOnIterate?: Error | undefined;
    readonly raiseOnWait?: Error | undefined;
  }) {
    this.#raiseOnWait = options.raiseOnWait;
    this.#chunks = options.lines.map((l) => `${new TextDecoder().decode(l)}\n`);
    this.#exitCode = options.exitCode ?? 0;
    this.#raiseOnIterate = options.raiseOnIterate;
  }

  get exitCode(): number | undefined {
    return this.#exitCode;
  }

  async sendStdin(data: Uint8Array): Promise<void> {
    this.stdin = data;
  }

  async closeStdin(): Promise<void> {
    this.stdinClosed = true;
  }

  async kill(): Promise<boolean> {
    this.killed = true;
    return true;
  }

  disconnect(): void {
    this.disconnected = true;
  }

  /** Como `CommandHandle.wait()` con el stream ya consumido: lanza el error
   * del `EndEvent` (`raiseOnWait`), si lo hay. */
  async wait(): Promise<void> {
    if (this.#raiseOnWait !== undefined) {
      throw this.#raiseOnWait;
    }
  }

  [Symbol.asyncIterator](): AsyncIterator<{ readonly stdout?: string }> {
    return {
      next: async () => {
        if (this.killed || this.disconnected) {
          return { done: true, value: undefined };
        }
        if (this.#index >= this.#chunks.length) {
          if (this.#raiseOnIterate !== undefined) {
            throw this.#raiseOnIterate;
          }
          return { done: true, value: undefined };
        }
        const chunk = this.#chunks[this.#index] as string;
        this.#index += 1;
        return { done: false, value: { stdout: chunk } };
      },
    };
  }
}

interface RunCall {
  readonly cmd: string;
  readonly options: Record<string, unknown> | undefined;
}

export class FakeCommands {
  readonly calls: RunCall[] = [];
  readonly #handles: FakeCommandHandle[];
  readonly #foregroundResults: (Error | undefined)[];

  constructor(
    options: {
      readonly handles?: readonly FakeCommandHandle[];
      readonly foregroundResults?: readonly (Error | undefined)[];
    } = {},
  ) {
    this.#handles = [...(options.handles ?? [])];
    this.#foregroundResults = [...(options.foregroundResults ?? [])];
  }

  async run(cmd: string, options?: Record<string, unknown>): Promise<unknown> {
    this.calls.push({ cmd, options });
    if (options?.background !== true) {
      const failure = this.#foregroundResults.shift();
      if (failure !== undefined) {
        throw failure;
      }
      return undefined;
    }
    const handle = this.#handles.shift();
    if (handle === undefined) {
      throw new Error("FakeCommands: no hay más handles precargados");
    }
    return handle;
  }
}

export class FakeFilesystem {
  readonly writeFilesCalls: unknown[][] = [];

  async writeFiles(files: readonly unknown[]): Promise<unknown> {
    this.writeFilesCalls.push([...files]);
    return [];
  }
}

export function gatewayStatus(port = 18080): { url: string } {
  return { url: `http://127.0.0.1:${port}` };
}

export class FakeGateways {
  readonly #entries: Readonly<Record<string, { readonly url: string }>>;

  constructor(entries: Readonly<Record<string, { readonly url: string }>> = {}) {
    this.#entries = entries;
  }

  get(name: string): { readonly url: string } | undefined {
    return this.#entries[name];
  }

  entries(): readonly (readonly [string, { readonly url: string }])[] {
    return Object.entries(this.#entries);
  }
}

export class FakeSandbox implements AgentSandbox {
  readonly commands: FakeCommands;
  readonly files: FakeFilesystem;
  #gateways: FakeGateways;
  #instrumentation: Instrumentation;

  constructor(options: {
    readonly commands: FakeCommands;
    readonly files: FakeFilesystem;
    readonly gateways?: Readonly<Record<string, { readonly url: string }>>;
  }) {
    this.commands = options.commands;
    this.files = options.files;
    this.#gateways = new FakeGateways(options.gateways ?? {});
    this.#instrumentation = NOOP;
  }

  gateways(): FakeGateways {
    return this.#gateways;
  }

  instrumentation(): Instrumentation {
    return this.#instrumentation;
  }

  setInstrumentation(instrumentation: Instrumentation): void {
    this.#instrumentation = instrumentation;
  }
}
