/**
 * Dobles de prueba de `AgentProject.deploy()` y `AgentDeployment`
 * (ADR-028): un sandbox que anota todo lo que se le pide y un `fetch` con
 * respuestas guiadas. Espejo de `fake_agent_project.py`. Nunca tocan red,
 * Connect ni AWS.
 */

import { AgentModel, AgentSpec } from "../../src/agent/domain.js";
import { bedrockGateway } from "../../src/agent/gateways.js";
import { AgentProject } from "../../src/agent/project.js";
import type { ServiceSandbox } from "../../src/agent/project-service.js";
import type { AgentRuntime } from "../../src/agent/runtime.js";
import { AGENT_SERVICE_READY_MARKER_PATH } from "../../src/limits.js";
import { HostAccess } from "../../src/models.js";
import type { SandboxCreateOptions } from "../../src/sandbox/sandbox.js";
import { gatewayStatus } from "./agent-fake.js";

export const SANDBOX_ID = "microvm-00000000-0000-0000-0000-000000000001";
export const ENDPOINT = "abc.microvm.example.com";
export const MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0";
export const GATEWAY_PORT = 8180;
export const FAILED_MARKER = `${AGENT_SERVICE_READY_MARKER_PATH}.failed`;

export function project(
  overrides: Partial<ConstructorParameters<typeof AgentProject>[0]> = {},
  runtime: string | AgentRuntime = "deepagents",
): AgentProject {
  return new AgentProject({
    name: "support-bot",
    runtime,
    spec: new AgentSpec({
      model: new AgentModel({
        provider: "bedrock",
        id: MODEL_ID,
        gateway: "bedrock",
        region: "us-east-1",
      }),
    }),
    gateways: {
      bedrock: bedrockGateway("bedrock-key", { region: "us-east-1", models: [MODEL_ID] }),
    },
    ...overrides,
  });
}

export class FakeToken {
  generation = 1;
  get jwe(): string {
    return `jwe-${this.generation}`;
  }
}

export interface FakeSandboxOptions {
  readonly metadata?: Record<string, string>;
  readonly readyAfter?: number | null;
  readonly failedCode?: string;
  readonly agentResult?: unknown;
  readonly agentError?: Error;
  readonly agentEvents?: readonly unknown[];
  readonly killError?: Error;
  readonly writeError?: Error;
}

export class FakeSandbox {
  readonly sandboxId = SANDBOX_ID;
  readonly metadata: Record<string, string>;
  readonly gateways = {
    get: (name: string) => (name === "bedrock" ? gatewayStatus(GATEWAY_PORT) : undefined),
    entries: () => [["bedrock", gatewayStatus(GATEWAY_PORT)]] as const,
  };
  readonly token = new FakeToken();
  readonly written: { path: string; data: Uint8Array; mode?: number | undefined }[] = [];
  readonly commandCalls: { cmd: string; options: Record<string, unknown> }[] = [];
  readonly agentCalls: { prompt: string; options: Record<string, unknown> }[] = [];
  readonly hostPorts: number[] = [];
  readonly handle = {
    disconnected: false,
    disconnect() {
      this.disconnected = true;
    },
  };
  kills = 0;
  readonly streamCloses = { count: 0 };
  refreshed = 0;
  readyPolls = 0;
  readonly #options: FakeSandboxOptions;

  constructor(options: FakeSandboxOptions = {}) {
    this.#options = options;
    this.metadata = options.metadata ?? {};
  }

  readonly agent = {
    stream: async (prompt: string, options: Record<string, unknown>) => {
      this.agentCalls.push({ prompt, options });
      const { agentError, agentEvents = [], agentResult = "resultado" } = this.#options;
      const closes = this.streamCloses;
      return {
        async *[Symbol.asyncIterator]() {
          yield* agentEvents as never[];
        },
        result: async () => {
          if (agentError !== undefined) {
            throw agentError;
          }
          return agentResult as never;
        },
        close: async () => {
          closes.count += 1;
        },
      };
    },
  };

  readonly commands = {
    run: async (cmd: string, options: Record<string, unknown>) => {
      this.commandCalls.push({ cmd, options });
      return this.handle;
    },
  };

  readonly files = {
    writeFiles: async (
      files: readonly { path: string; data: Uint8Array; mode?: number | undefined }[],
    ) => {
      if (this.#options.writeError !== undefined) {
        throw this.#options.writeError;
      }
      this.written.push(...files);
    },
    exists: async (path: string): Promise<boolean> => {
      if (path === AGENT_SERVICE_READY_MARKER_PATH) {
        this.readyPolls += 1;
        const after = this.#options.readyAfter === undefined ? 0 : this.#options.readyAfter;
        return after !== null && this.readyPolls > after;
      }
      return path === FAILED_MARKER && this.#options.failedCode !== undefined;
    },
    read: async (): Promise<string> => `${this.#options.failedCode}\n`,
  };

  async getHost(port: number): Promise<HostAccess> {
    this.hostPorts.push(port);
    return new HostAccess(ENDPOINT, port, () => this.token.jwe);
  }

  async kill(): Promise<boolean> {
    this.kills += 1;
    if (this.#options.killError !== undefined) {
      throw this.#options.killError;
    }
    return true;
  }

  async refreshProxyAccess(): Promise<void> {
    this.refreshed += 1;
    this.token.generation += 1;
  }

  asService(): ServiceSandbox {
    return this as unknown as ServiceSandbox;
  }
}

export class FakeSandboxFactory {
  readonly created: SandboxCreateOptions[] = [];
  error: Error | undefined;
  readonly #sandbox: FakeSandbox;

  constructor(sandbox: FakeSandbox) {
    this.#sandbox = sandbox;
  }

  async create(options: SandboxCreateOptions): Promise<ServiceSandbox> {
    this.created.push(options);
    if (this.error !== undefined) {
      throw this.error;
    }
    return this.#sandbox.asService();
  }
}

export interface Scripted {
  readonly status?: number;
  readonly body?: string | Uint8Array;
  readonly chunks?: readonly (string | Uint8Array)[];
}

export interface RecordedRequest {
  readonly url: string;
  readonly method: string;
  readonly headers: Record<string, string>;
  readonly body: string | undefined;
}

const encoder = new TextEncoder();

function bytes(value: string | Uint8Array): Uint8Array {
  return typeof value === "string" ? encoder.encode(value) : value;
}

/** Un `fetch` con una respuesta guiada por petición y el registro de lo
 * enviado. Una respuesta que es un `Error` rechaza la promesa. */
export function scriptedFetch(...responses: (Scripted | Error)[]): {
  readonly fetch: typeof fetch;
  readonly requests: RecordedRequest[];
  readonly opened: Response[];
} {
  const queue = [...responses];
  const requests: RecordedRequest[] = [];
  const opened: Response[] = [];
  const doFetch = async (input: string | URL | Request, init?: RequestInit): Promise<Response> => {
    requests.push({
      url: String(input),
      method: init?.method ?? "GET",
      headers: { ...(init?.headers as Record<string, string>) },
      body: init?.body === undefined ? undefined : String(init.body),
    });
    const scripted = queue.shift();
    if (scripted === undefined || scripted instanceof Error) {
      throw scripted ?? new Error("sin respuesta guiada");
    }
    const parts = scripted.chunks ?? (scripted.body === undefined ? [] : [scripted.body]);
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        for (const part of parts) {
          controller.enqueue(bytes(part));
        }
        controller.close();
      },
    });
    const response = new Response(stream, { status: scripted.status ?? 200 });
    opened.push(response);
    return response;
  };
  return { fetch: doFetch as typeof fetch, requests, opened };
}

export function sse(...messages: [string, Record<string, unknown>][]): string {
  return messages
    .map(
      ([kind, data]) =>
        `event: ${kind}\ndata: ${JSON.stringify({ v: 1, type: kind, ...data })}\n\n`,
    )
    .join("");
}

export const RESULT_BODY = {
  v: 1,
  text: "hola mundo",
  session_id: "ses_0000000000000000000000000a",
  usage: { input: 10, output: 5, reasoning: 0, cache_read: 0, cache_write: 0 },
  steps: 2,
  failed: null,
};
