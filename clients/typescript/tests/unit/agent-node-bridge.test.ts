/**
 * El puente de Node del host (`node_bridge.mjs`, ADR-028 y 0.14 D3). Adaptador
 * `strands`: la traducción de `agent.stream()` a eventos v1 contra los eventos
 * grabados que comparte con el driver de Python
 * (`testdata/agent/strands-ts-events.jsonl` y `strands-expected-events.json`),
 * los permisos, la construcción del modelo por pasarela y una ejecución completa
 * con módulos de doble. Adaptadores `langgraph`, `vercel-ai` y `callable`: los
 * vectores de `testdata/agent/node-adapters/`, la resolución de `entrypoint`
 * (`ruta#exportación`), el historial de los marcos sin memoria y el proceso
 * real. El puente es Node: se prueba aquí y el host (Python) lo ejecuta tal cual.
 */

import { mkdirSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { describe, expect, test } from "vitest";

const REPO = join(import.meta.dirname, "..", "..", "..", "..");
const TESTDATA = join(REPO, "testdata", "agent");
const BRIDGE = join(
  REPO,
  "clients",
  "python",
  "src",
  "rayito",
  "_agent",
  "_assets",
  "node_bridge.mjs",
);

/** `Module` de Node: el gancho `_load` es su nombre, no el nuestro. */
interface ModuleLoader {
  // biome-ignore lint/style/useNamingConvention: nombre del gancho de `node:module`.
  _load: (...args: unknown[]) => unknown;
}
type Json = Record<string, unknown>;
type Emit = (event: Json) => void;
interface Bridge {
  Translator: new (options: { reasoning: boolean }) => { feed(item: Json): Json[] };
  decide(rules: Json, tool: string, args: Json): string;
  classify(error: unknown): [string, string];
  buildModel(
    config: Json,
    modelId: string,
    importModule: (name: string) => Promise<unknown>,
  ): Promise<{ options: Json }>;
  registerPermissions(agent: unknown, sdk: unknown, rules: Json): void;
  loadAgent(
    config: Json,
    model: unknown,
    sessionId: string,
    importModule: (name: string) => Promise<unknown>,
  ): Promise<unknown>;
  run(options: {
    config: Json;
    request: Json;
    emit: Emit;
    importModule: (name: string) => Promise<unknown>;
  }): Promise<void>;
  usageOf(raw: unknown): Json;
  rayitoUsage(raw: unknown): Json;
  truncateOutput(text: string): [string, boolean];
  parseEntrypoint(value: string): { path: string; exportName: string | null };
  loadTarget(
    config: Json,
    preferred: string[],
    importModule: (name: string) => Promise<unknown>,
  ): Promise<unknown>;
  checkEntry(config: Json, importModule: (name: string) => Promise<unknown>): Promise<void>;
  CHECK_ENTRY_FLAG: string;
  GraphTranslator: new () => { feed(chunk: unknown): Json[]; answer: string };
  AiStreamTranslator: new (options: {
    reasoning: boolean;
  }) => {
    feed(part: Json): Json[];
    answer: string;
    failure: unknown;
  };
  generatedParts(result: Json): Iterable<Json>;
  HistoryStore: new (
    directory: string | null,
    sessionId: string,
  ) => {
    path: string | null;
    load(): Json[];
    save(history: Json[], prompt: string, answer: string): void;
  };
  BridgeError: new (code: string) => Error & { code: string };
  forceHttp1ForAwsSdk(loader?: ModuleLoader): () => void;
  GATEWAY_ENDPOINT_ENV: string;
  DENIED_TOOL_MESSAGE: string;
  ANTHROPIC_MAX_TOKENS: number;
  DEFAULT_ADAPTER: string;
  HISTORY_MAX_MESSAGES: number;
  SESSION_FILE_MODE: number;
}

const bridge = (await import(pathToFileURL(BRIDGE).href)) as Bridge;
const EVENTS = readFileSync(join(TESTDATA, "strands-ts-events.jsonl"), "utf8")
  .split("\n")
  .filter(Boolean)
  .map((text) => JSON.parse(text) as Json);
const EXPECTED = JSON.parse(readFileSync(join(TESTDATA, "strands-expected-events.json"), "utf8"));
const GATEWAY = {
  base_url: "http://127.0.0.1:18001/v1",
  credential_placeholder: "placeholder-not-a-secret",
};

describe("translator", () => {
  test.each([
    ["plain", false],
    ["reasoning", true],
  ])("matches the recorded events (%s)", (key, reasoning) => {
    const translator = new bridge.Translator({ reasoning });
    expect(EVENTS.flatMap((item) => translator.feed(item))).toEqual(EXPECTED[key]);
  });

  test("a failed tool result is an error call and a huge output is truncated", () => {
    const translator = new bridge.Translator({ reasoning: false });
    const [call] = translator.feed({
      type: "afterToolCallEvent",
      toolUse: { name: "n", toolUseId: "t", input: {} },
      result: { toolUseId: "t", status: "error", content: [{ text: "x".repeat(70000) }] },
    });
    expect(call).toMatchObject({ type: "tool_call", status: "error", output_truncated: true });
    expect(String(call?.output).length).toBe(65536);
  });

  test("usage and truncation helpers ignore what is not a count", () => {
    expect(bridge.usageOf({ inputTokens: 3, outputTokens: -1, cacheReadInputTokens: "x" })).toEqual(
      {
        input: 3,
        output: 0,
        reasoning: 0,
        cache_read: 0,
        cache_write: 0,
      },
    );
    expect(bridge.truncateOutput("corto")).toEqual(["corto", false]);
  });
});

describe("permissions", () => {
  const rules = {
    default: "allow",
    tools: { bash: { "*": "deny", "git *": "allow" }, edit: "deny" },
  };

  test("decide applies the last matching pattern and per-tool rules", () => {
    expect(bridge.decide(rules, "bash", { command: "git status" })).toBe("allow");
    expect(bridge.decide(rules, "bash", { command: "rm -rf x" })).toBe("deny");
    expect(bridge.decide(rules, "edit", {})).toBe("deny");
    expect(bridge.decide(rules, "read", {})).toBe("allow");
    expect(bridge.decide({ default: "deny", tools: {} }, "read", {})).toBe("deny");
  });

  test("the hook cancels denied tool calls", () => {
    let hook: ((event: Json) => void) | undefined;
    const agent = {
      addHook(_type: unknown, callback: (event: Json) => void) {
        hook = callback;
      },
    };
    class BeforeToolCallEvent {}
    bridge.registerPermissions(agent, { BeforeToolCallEvent }, rules);
    const verdict = (name: string, input: Json): unknown => {
      const event: Json = { toolUse: { name, input }, cancel: false };
      hook?.(event);
      return event.cancel;
    };
    expect(verdict("bash", { command: "git log" })).toBe(false);
    expect(verdict("bash", { command: "rm x" })).toBe(bridge.DENIED_TOOL_MESSAGE);
    expect(verdict("edit", {})).toBe(bridge.DENIED_TOOL_MESSAGE);
  });
});

describe("errors", () => {
  test("model errors are classified by class name along the cause chain, never by message", () => {
    class ModelThrottledError extends Error {}
    class Wrapper extends Error {}
    const [reason, detail] = bridge.classify(
      new Wrapper("detalle con sk-123", { cause: new ModelThrottledError("x") }),
    );
    expect(reason).toBe("model_error");
    expect(detail).toBe("wrapper");
    expect(bridge.classify(new TypeError("boom"))).toEqual(["runtime_error", "typeerror"]);
  });
});

function fakeModules(created: Json[]) {
  class Recorder {
    options: Json;
    constructor(options: Json) {
      this.options = options;
      created.push(options);
    }
  }
  const modules: Record<string, unknown> = {
    "@strands-agents/sdk/models/bedrock": { BedrockModel: Recorder },
    "@strands-agents/sdk/models/anthropic": { AnthropicModel: Recorder },
    "@strands-agents/sdk/models/openai": { OpenAIModel: Recorder },
    "@strands-agents/sdk/models/google": { GoogleModel: Recorder },
  };
  return (name: string) => {
    const module = modules[name];
    return module === undefined
      ? Promise.reject(new Error(`sin módulo ${name}`))
      : Promise.resolve(module);
  };
}

describe("buildModel", () => {
  test("every provider goes through the gateway with the placeholder key", async () => {
    const created: Json[] = [];
    const importModule = fakeModules(created);
    await bridge.buildModel(
      { provider: "bedrock", region: "us-east-1", ...GATEWAY },
      "m",
      importModule,
    );
    await bridge.buildModel({ provider: "anthropic", ...GATEWAY }, "m", importModule);
    await bridge.buildModel({ provider: "openai", ...GATEWAY }, "m", importModule);
    await bridge.buildModel({ provider: "openai-compatible", ...GATEWAY }, "m", importModule);
    await bridge.buildModel({ provider: "google", ...GATEWAY }, "m", importModule);
    const [bedrock, anthropic, openai, compatible, google] = created;
    expect(bedrock).toEqual({
      modelId: "m",
      region: "us-east-1",
      clientConfig: { endpoint: GATEWAY.base_url },
    });
    expect(anthropic).toEqual({
      modelId: "m",
      maxTokens: bridge.ANTHROPIC_MAX_TOKENS,
      apiKey: GATEWAY.credential_placeholder,
      clientConfig: { baseURL: GATEWAY.base_url },
    });
    for (const options of [openai, compatible]) {
      expect(options).toEqual({
        modelId: "m",
        api: "chat",
        apiKey: GATEWAY.credential_placeholder,
        clientConfig: { baseURL: GATEWAY.base_url },
      });
    }
    expect(google).toEqual({
      modelId: "m",
      apiKey: GATEWAY.credential_placeholder,
      clientConfig: { httpOptions: { baseUrl: GATEWAY.base_url } },
    });
  });

  test("without a gateway the client keeps its endpoint and credentials", async () => {
    const created: Json[] = [];
    const importModule = fakeModules(created);
    const none = { base_url: null, credential_placeholder: null };
    await bridge.buildModel(
      { provider: "bedrock", region: "us-east-1", ...none },
      "m",
      importModule,
    );
    await bridge.buildModel({ provider: "openai", ...none }, "m", importModule);
    expect(created).toEqual([
      { modelId: "m", region: "us-east-1" },
      { modelId: "m", api: "chat" },
    ]);
  });

  test("an unknown provider is rejected", async () => {
    await expect(
      bridge.buildModel({ provider: "azure", ...GATEWAY }, "m", fakeModules([])),
    ).rejects.toThrow("unknown_provider");
  });
});

describe("run", () => {
  function fakeSdk(streamed: Json[], hooks: Json[]) {
    class BeforeToolCallEvent {}
    const agent = {
      addHook: (_type: unknown, callback: unknown) => hooks.push({ callback }),
      async *stream(prompt: string) {
        hooks.push({ prompt });
        yield* streamed;
      },
    };
    return { agent, BeforeToolCallEvent };
  }

  test("runs a factory with the wired model and translates the stream", async () => {
    const hooks: Json[] = [];
    const { agent, BeforeToolCallEvent } = fakeSdk(EVENTS, hooks);
    const contexts: Json[] = [];
    const created: Json[] = [];
    const modelModules = fakeModules(created);
    const importModule = (name: string): Promise<unknown> => {
      if (name === "@strands-agents/sdk") {
        return Promise.resolve({ BeforeToolCallEvent });
      }
      if (name.startsWith("file://")) {
        return Promise.resolve({
          createAgent: (ctx: Json) => {
            contexts.push(ctx);
            return agent;
          },
        });
      }
      return modelModules(name);
    };
    const emitted: Json[] = [];
    await bridge.run({
      config: {
        provider: "openai",
        ...GATEWAY,
        model: "modelo-a",
        instructions: "Eres útil.",
        workdir: "/home/user",
        app_dir: "/opt/agents/app",
        state_dir: "/s",
        sessions_dir: "/s/sessions",
        entrypoint: "agent.ts",
        permissions: { default: "allow", tools: {} },
      },
      request: {
        v: 1,
        prompt: "suma 2 y 3",
        session_id: `rda_${"a".repeat(32)}`,
        reasoning: false,
      },
      emit: (event) => emitted.push(event),
      importModule,
    });
    expect(emitted).toEqual(EXPECTED.plain);
    expect(hooks.some((hook) => hook.prompt === "suma 2 y 3")).toBe(true);
    expect(contexts[0]).toMatchObject({
      instructions: "Eres útil.",
      sessionId: `rda_${"a".repeat(32)}`,
      sessionsDir: "/s/sessions",
      workdir: "/home/user",
    });
    expect(created[0]).toMatchObject({ modelId: "modelo-a", api: "chat" });
  });

  test("an entrypoint that exports an agent instance is used as is", async () => {
    const hooks: Json[] = [];
    const { agent, BeforeToolCallEvent } = fakeSdk([], hooks);
    const importModule = (name: string): Promise<unknown> => {
      if (name === "@strands-agents/sdk") {
        return Promise.resolve({ BeforeToolCallEvent });
      }
      return name.startsWith("file://") ? Promise.resolve({ agent }) : fakeModules([])(name);
    };
    await bridge.run({
      config: {
        provider: "openai",
        ...GATEWAY,
        model: "m",
        app_dir: "/a",
        entrypoint: "agent.ts",
        permissions: {},
      },
      request: { v: 1, prompt: "hola", session_id: `rda_${"b".repeat(32)}` },
      emit: () => undefined,
      importModule,
    });
    expect(hooks.some((hook) => typeof hook.callback === "function")).toBe(true);
  });

  test("an entrypoint that exports nothing usable fails with invalid_entrypoint", async () => {
    const importModule = (name: string): Promise<unknown> =>
      name === "@strands-agents/sdk"
        ? Promise.resolve({})
        : name.startsWith("file://")
          ? Promise.resolve({ valor: 1 })
          : fakeModules([])(name);
    await expect(
      bridge.run({
        config: { provider: "openai", ...GATEWAY, model: "m", app_dir: "/a", entrypoint: "a.ts" },
        request: { v: 1, prompt: "x", session_id: `rda_${"c".repeat(32)}` },
        emit: () => undefined,
        importModule,
      }),
    ).rejects.toThrow("invalid_entrypoint");
  });
});

describe("the real bridge process", () => {
  test("answers an invalid request with a protocol_error line and exits cleanly", async () => {
    const { spawnSync } = await import("node:child_process");
    const config = join(TESTDATA, "strands-spike.json");
    const result = spawnSync(process.execPath, [BRIDGE, config], {
      input: '{"v":2,"prompt":"x"}',
      encoding: "utf8",
    });
    expect(result.status).toBe(0);
    expect(JSON.parse(result.stdout.trim())).toEqual({
      v: 1,
      type: "agent_failed",
      reason: "protocol_error",
      detail_code: "invalid_request",
    });
  });
});

interface AdapterVectors {
  cases: Json[];
  generate_cases: Json[];
  invalid_returns: Json[];
}

function readVectors(name: string): AdapterVectors {
  return JSON.parse(readFileSync(join(TESTDATA, "node-adapters", name), "utf8")) as AdapterVectors;
}

const SESSION = `rda_${"d".repeat(32)}`;

function moduleLoader(modules: Record<string, unknown>): (name: string) => Promise<unknown> {
  return (name: string) => {
    const entry = Object.entries(modules).find(([suffix]) => name.endsWith(suffix));
    return entry === undefined
      ? Promise.reject(new Error(`sin módulo ${name}`))
      : Promise.resolve(entry[1]);
  };
}

async function runAdapter(
  adapter: string,
  modules: Record<string, unknown>,
  options: { config?: Json; request?: Json } = {},
): Promise<Json[]> {
  const emitted: Json[] = [];
  await bridge.run({
    config: { adapter, app_dir: "/a", entrypoint: "agent.mjs", ...options.config },
    request: { v: 1, prompt: "suma", session_id: SESSION, ...options.request },
    emit: (event) => emitted.push(event),
    importModule: moduleLoader(modules),
  });
  return emitted;
}

describe("entrypoint resolution", () => {
  test("a path with and without an export name", () => {
    expect(bridge.parseEntrypoint("src/agent.ts#graph")).toEqual({
      path: "src/agent.ts",
      exportName: "graph",
    });
    expect(bridge.parseEntrypoint("agent.ts")).toEqual({ path: "agent.ts", exportName: null });
  });

  test("the named export wins, then the preferred names, then default", async () => {
    const module = { graph: "g", agent: "a", default: "d" };
    const load = (entrypoint: string, preferred: string[]) =>
      bridge.loadTarget({ app_dir: "/a", entrypoint }, preferred, () => Promise.resolve(module));
    await expect(load("x.ts#agent", ["graph"])).resolves.toBe("a");
    await expect(load("x.ts", ["graph", "agent"])).resolves.toBe("g");
    await expect(load("x.ts", ["nada"])).resolves.toBe("d");
  });

  test("a named export that does not exist is entrypoint_not_found", async () => {
    await expect(
      bridge.loadTarget({ app_dir: "/a", entrypoint: "x.ts#no" }, [], () => Promise.resolve({})),
    ).rejects.toMatchObject({ code: "entrypoint_not_found" });
  });

  test("checkEntry finds a missing file or export before any request", async () => {
    const dir = mkdtempSync(join(tmpdir(), "rayito-check-entry-"));
    writeFileSync(join(dir, "agent.mjs"), "export const agent = {};\n");
    const importModule = () => Promise.resolve({ agent: {} });
    try {
      await expect(
        bridge.checkEntry({ app_dir: dir, entrypoint: "agent.mjs#agent" }, importModule),
      ).resolves.toBeUndefined();
      await expect(
        bridge.checkEntry({ app_dir: dir, entrypoint: "agent.mjs#otro" }, importModule),
      ).rejects.toMatchObject({ code: "entrypoint_not_found" });
      await expect(
        bridge.checkEntry({ app_dir: dir, entrypoint: "falta.mjs" }, importModule),
      ).rejects.toMatchObject({ code: "entrypoint_not_found" });
      await expect(bridge.checkEntry({ app_dir: dir }, importModule)).resolves.toBeUndefined();
      expect(bridge.CHECK_ENTRY_FLAG).toBe("--check-entry");
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

describe("langgraph adapter", () => {
  const vectors = readVectors("langgraph.json");
  test.each(vectors.cases.map((c) => String(c.name)))("translates %s", (name) => {
    const vector = (vectors.cases as Json[]).find((c) => c.name === name) as Json;
    const translator = new bridge.GraphTranslator();
    const events = (vector.chunks as unknown[]).flatMap((chunk) => translator.feed(chunk));
    expect(events).toEqual(vector.events);
    expect(translator.answer).toBe(vector.answer);
  });

  function fakeGraph(chunks: Json[], calls: Json[], extra: Json = {}) {
    return {
      ...extra,
      async stream(input: Json, options: Json) {
        calls.push({ input, options });
        return (async function* () {
          yield* chunks;
        })();
      },
    };
  }

  const answerChunks = (vectors.cases as Json[])[0]?.chunks as Json[];

  test("runs the graph with the thread id and the updates stream mode", async () => {
    const calls: Json[] = [];
    const directory = mkdtempSync(join(tmpdir(), "bridge-"));
    try {
      const events = await runAdapter(
        "langgraph",
        { "agent.mjs": { graph: fakeGraph(answerChunks, calls) } },
        { config: { sessions_dir: directory }, request: { prompt: "2 + 3" } },
      );
      expect(events.at(-1)).toMatchObject({ type: "step_finished", index: 2 });
      expect(calls[0]?.options).toEqual({
        configurable: { thread_id: SESSION },
        streamMode: "updates",
      });
      expect(calls[0]?.input).toEqual({ messages: [{ role: "user", content: "2 + 3" }] });
    } finally {
      rmSync(directory, { recursive: true });
    }
  });

  test("a graph without a checkpointer keeps the conversation between runs", async () => {
    const directory = mkdtempSync(join(tmpdir(), "bridge-"));
    try {
      const calls: Json[] = [];
      const modules = { "agent.mjs": { graph: fakeGraph(answerChunks, calls) } };
      await runAdapter("langgraph", modules, { config: { sessions_dir: directory } });
      await runAdapter("langgraph", modules, {
        config: { sessions_dir: directory },
        request: { prompt: "y ahora" },
      });
      expect((calls[1]?.input as Json | undefined)?.messages).toEqual([
        { role: "user", content: "suma" },
        { role: "assistant", content: "La suma es 5" },
        { role: "user", content: "y ahora" },
      ]);
      const file = join(directory, `${SESSION}.json`);
      expect(statSync(file).mode & 0o777).toBe(bridge.SESSION_FILE_MODE);
    } finally {
      rmSync(directory, { recursive: true });
    }
  });

  test("a graph with its own checkpointer is left to keep its own state", async () => {
    const directory = mkdtempSync(join(tmpdir(), "bridge-"));
    try {
      const calls: Json[] = [];
      const graph = fakeGraph(answerChunks, calls, { checkpointer: {} });
      await runAdapter(
        "langgraph",
        { "agent.mjs": { graph } },
        { config: { sessions_dir: directory } },
      );
      await runAdapter(
        "langgraph",
        { "agent.mjs": { graph } },
        { config: { sessions_dir: directory } },
      );
      expect((calls[1]?.input as Json | undefined)?.messages).toHaveLength(1);
      expect(() => statSync(join(directory, `${SESSION}.json`))).toThrow();
    } finally {
      rmSync(directory, { recursive: true });
    }
  });

  test("a factory receives the run context and may return the graph", async () => {
    const contexts: Json[] = [];
    const factory = (ctx: Json) => {
      contexts.push(ctx);
      return fakeGraph([], []);
    };
    await runAdapter(
      "langgraph",
      { "agent.mjs": { createGraph: factory } },
      { config: { model: "m1", workdir: "/w", instructions: "Sé breve." } },
    );
    expect(contexts[0]).toMatchObject({
      modelId: "m1",
      workdir: "/w",
      instructions: "Sé breve.",
      sessionId: SESSION,
    });
  });

  test("an export that is neither a graph nor a factory is invalid_entrypoint", async () => {
    await expect(runAdapter("langgraph", { "agent.mjs": { graph: 3 } })).rejects.toMatchObject({
      code: "invalid_entrypoint",
    });
  });
});

describe("vercel-ai adapter", () => {
  const vectors = readVectors("vercel-ai.json");
  test.each(vectors.cases.map((c) => String(c.name)))("translates %s", (name) => {
    const vector = (vectors.cases as Json[]).find((c) => c.name === name) as Json;
    const translator = new bridge.AiStreamTranslator({ reasoning: vector.reasoning === true });
    const events = (vector.parts as Json[]).flatMap((part) => translator.feed(part));
    expect(events).toEqual(vector.events);
    expect(translator.answer).toBe(vector.answer);
  });

  test.each(vectors.generate_cases.map((c) => String(c.name)))(
    "translates generate() %s",
    (name) => {
      const vector = (vectors.generate_cases as Json[]).find((c) => c.name === name) as Json;
      const translator = new bridge.AiStreamTranslator({ reasoning: false });
      const events = [...bridge.generatedParts(vector.result as Json)].flatMap((part) =>
        translator.feed(part),
      );
      expect(events).toEqual(vector.events);
      expect(translator.answer).toBe(vector.answer);
    },
  );

  const streamingAgent = (parts: Json[], calls: Json[]) => ({
    async stream(input: Json) {
      calls.push(input);
      return {
        fullStream: (async function* () {
          yield* parts;
        })(),
      };
    },
  });
  const firstParts = (vectors.cases as Json[])[0]?.parts as Json[];

  test("streams the agent and keeps the conversation between runs", async () => {
    const directory = mkdtempSync(join(tmpdir(), "bridge-"));
    try {
      const calls: Json[] = [];
      const modules = { "agent.mjs": { agent: streamingAgent(firstParts, calls) } };
      const first = await runAdapter("vercel-ai", modules, { config: { sessions_dir: directory } });
      await runAdapter("vercel-ai", modules, {
        config: { sessions_dir: directory },
        request: { prompt: "otra" },
      });
      expect(first.map((event) => event.type)).toContain("text_delta");
      expect((calls[1] as Json).messages).toEqual([
        { role: "user", content: "suma" },
        { role: "assistant", content: "La suma es 5" },
        { role: "user", content: "otra" },
      ]);
    } finally {
      rmSync(directory, { recursive: true });
    }
  });

  test("an agent without stream() is run with generate()", async () => {
    const result = (vectors.generate_cases as Json[])[1]?.result;
    const agent = { generate: () => Promise.resolve(result) };
    const events = await runAdapter("vercel-ai", { "agent.mjs": { agent } });
    expect(events.map((event) => event.type)).toEqual([
      "step_started",
      "text_delta",
      "text",
      "step_finished",
    ]);
  });

  test("a stream error part fails the run with the original error", async () => {
    class APICallError extends Error {}
    const failure = new APICallError("detalle con sk-123");
    const agent = streamingAgent([{ type: "start-step" }, { type: "error", error: failure }], []);
    const error = await runAdapter("vercel-ai", { "agent.mjs": { agent } }).catch((e) => e);
    expect(error).toBe(failure);
    expect(bridge.classify(error)).toEqual(["model_error", "apicallerror"]);
  });

  test("a factory may return the agent", async () => {
    const contexts: Json[] = [];
    const createAgent = (ctx: Json) => {
      contexts.push(ctx);
      return streamingAgent([], []);
    };
    await runAdapter("vercel-ai", { "agent.mjs": { createAgent } }, { config: { model: "m2" } });
    expect(contexts[0]).toMatchObject({ modelId: "m2", sessionId: SESSION });
  });
});

describe("callable adapter", () => {
  const vectors = readVectors("callable.json");

  function returned(spec: Json): unknown {
    switch (spec.kind) {
      case "string":
      case "object":
      case "number":
        return spec.value;
      default: {
        const chunks = spec.value as unknown[];
        return spec.async === true
          ? (async function* () {
              yield* chunks;
            })()
          : chunks;
      }
    }
  }

  test.each(vectors.cases.map((c) => String(c.name)))("runs %s", async (name) => {
    const vector = (vectors.cases as Json[]).find((c) => c.name === name) as Json;
    const seen: Json[] = [];
    const handler = (request: Json) => {
      seen.push(request);
      return returned(vector.returns as Json);
    };
    const events = await runAdapter("callable", { "agent.mjs": { handler } });
    expect(events).toEqual(vector.events);
    expect(seen[0]).toEqual({
      prompt: "suma",
      session_id: SESSION,
      model: null,
      reasoning: false,
    });
  });

  test.each(vectors.invalid_returns)("rejects a return of kind %#", async (spec) => {
    const handler = () => returned(spec as Json);
    await expect(runAdapter("callable", { "agent.mjs": { handler } })).rejects.toMatchObject({
      code: "invalid_response",
    });
  });

  test("an async function and the #export form work", async () => {
    const events = await runAdapter(
      "callable",
      { "agent.mjs": { respond: async () => "hecho" } },
      { config: { entrypoint: "agent.mjs#respond" } },
    );
    expect(events[1]).toEqual({ v: 1, type: "text", text: "hecho" });
  });

  test("a non-function export is invalid_entrypoint", async () => {
    await expect(
      runAdapter("callable", { "agent.mjs": { handler: "no soy una función" } }),
    ).rejects.toMatchObject({ code: "invalid_entrypoint" });
  });
});

describe("dispatch and errors", () => {
  test("strands is the default adapter", () => {
    expect(bridge.DEFAULT_ADAPTER).toBe("strands");
  });

  test("an unknown adapter fails with a closed detail code", async () => {
    const error = await runAdapter("crewai", {}).catch((e) => e);
    expect(bridge.classify(error)).toEqual(["runtime_error", "unknown_adapter"]);
  });

  test("AWS and AI SDK error classes are model errors, by name", () => {
    class ThrottlingException extends Error {}
    expect(bridge.classify(new ThrottlingException("x"))[0]).toBe("model_error");
    const aiSdk = Object.assign(new Error("x"), { name: "AI_APICallError" });
    expect(bridge.classify(aiSdk)[0]).toBe("model_error");
    const named = Object.assign(new Error("x"), { name: "AccessDeniedException" });
    expect(bridge.classify(named)[0]).toBe("model_error");
    expect(bridge.classify(new bridge.BridgeError("Mala Señal"))).toEqual([
      "runtime_error",
      "mala_se_al",
    ]);
  });

  test("rayitoUsage keeps only Rayito counters", () => {
    expect(bridge.rayitoUsage({ input: 2, cache_read: 1, output: -1, otro: 9 })).toEqual({
      input: 2,
      output: 0,
      reasoning: 0,
      cache_read: 1,
      cache_write: 0,
    });
  });
});

describe("history store", () => {
  test("without a directory nothing is read or written", () => {
    const store = new bridge.HistoryStore(null, SESSION);
    expect(store.load()).toEqual([]);
    store.save([], "x", "y");
  });

  test("an unreadable or malformed file is an empty history", () => {
    const directory = mkdtempSync(join(tmpdir(), "bridge-"));
    try {
      const store = new bridge.HistoryStore(directory, SESSION);
      expect(store.load()).toEqual([]);
      writeFileSync(join(directory, `${SESSION}.json`), "{no es json");
      expect(store.load()).toEqual([]);
      writeFileSync(join(directory, `${SESSION}.json`), '{"a":1}');
      expect(store.load()).toEqual([]);
    } finally {
      rmSync(directory, { recursive: true });
    }
  });

  test("keeps only the last messages", () => {
    const directory = mkdtempSync(join(tmpdir(), "bridge-"));
    try {
      const store = new bridge.HistoryStore(directory, SESSION);
      const old = Array.from({ length: bridge.HISTORY_MAX_MESSAGES }, (_, index) => ({
        role: "user",
        content: String(index),
      }));
      store.save(old, "nuevo", "respuesta");
      const kept = store.load();
      expect(kept).toHaveLength(bridge.HISTORY_MAX_MESSAGES);
      expect(kept.at(-1)).toEqual({ role: "assistant", content: "respuesta" });
      expect(kept[0]).toEqual({ role: "user", content: "2" });
    } finally {
      rmSync(directory, { recursive: true });
    }
  });
});

describe("the real bridge process with a framework adapter", () => {
  test("runs a callable module and closes the run with done", async () => {
    const { spawnSync } = await import("node:child_process");
    const directory = mkdtempSync(join(tmpdir(), "bridge-"));
    try {
      writeFileSync(
        join(directory, "agent.mjs"),
        "export const handler = ({ prompt }) => `eco ${prompt}`;",
      );
      const config = join(directory, "config.json");
      writeFileSync(
        config,
        JSON.stringify({ adapter: "callable", app_dir: directory, entrypoint: "agent.mjs" }),
      );
      const result = spawnSync(process.execPath, [BRIDGE, config], {
        input: JSON.stringify({ v: 1, prompt: "hola", session_id: SESSION }),
        encoding: "utf8",
      });
      expect(result.status).toBe(0);
      const lines = result.stdout
        .trim()
        .split("\n")
        .map((line) => JSON.parse(line) as Json);
      expect(lines.map((line) => line.type)).toEqual([
        "step_started",
        "text",
        "step_finished",
        "done",
      ]);
      expect(lines[1]?.text).toBe("eco hola");
    } finally {
      rmSync(directory, { recursive: true });
    }
  });

  test("an unknown adapter is an agent_failed line, not a crash", async () => {
    const { spawnSync } = await import("node:child_process");
    const directory = mkdtempSync(join(tmpdir(), "bridge-"));
    try {
      const config = join(directory, "config.json");
      writeFileSync(config, JSON.stringify({ adapter: "crewai" }));
      const result = spawnSync(process.execPath, [BRIDGE, config], {
        input: JSON.stringify({ v: 1, prompt: "hola", session_id: SESSION }),
        encoding: "utf8",
      });
      expect(result.status).toBe(0);
      expect(JSON.parse(result.stdout.trim())).toEqual({
        v: 1,
        type: "agent_failed",
        reason: "runtime_error",
        detail_code: "unknown_adapter",
        session_id: SESSION,
      });
    } finally {
      rmSync(directory, { recursive: true });
    }
  });
});

describe("HTTP/1.1 for the AWS SDK behind the gateway", () => {
  class Http2Handler {}
  class Http1Handler {}

  test("a module exporting both smithy handlers comes out with the HTTP/1.1 one in both names", () => {
    const smithy = { NodeHttp2Handler: Http2Handler, NodeHttpHandler: Http1Handler };
    const other = { NodeHttp2Handler: 1, NodeHttpHandler: 2, value: "intacto" };
    const loader: ModuleLoader = {
      // biome-ignore lint/style/useNamingConvention: nombre del gancho de `node:module`.
      _load: (request: unknown) => (request === "smithy" ? smithy : other),
    };
    const restore = bridge.forceHttp1ForAwsSdk(loader);
    const patched = loader._load("smithy") as typeof smithy;
    expect(patched.NodeHttp2Handler).toBe(Http1Handler);
    expect(patched.NodeHttpHandler).toBe(Http1Handler);
    expect(loader._load("otro")).toEqual({
      NodeHttp2Handler: 1,
      NodeHttpHandler: 2,
      value: "intacto",
    });
    restore();
    expect(loader._load("smithy")).toBe(smithy);
  });

  function projectWithSmithy(directory: string): string {
    const module = join(directory, "node_modules", "@smithy", "node-http-handler");
    mkdirSync(module, { recursive: true });
    writeFileSync(
      join(module, "package.json"),
      JSON.stringify({ name: "@smithy/node-http-handler", version: "0.0.0", main: "index.js" }),
    );
    writeFileSync(
      join(module, "index.js"),
      "class NodeHttpHandler {}\nclass NodeHttp2Handler {}\nmodule.exports = { NodeHttpHandler, NodeHttp2Handler };\n",
    );
    writeFileSync(
      join(directory, "agent.mjs"),
      'import * as handlers from "@smithy/node-http-handler";\n' +
        "export const handler = () => String(handlers.NodeHttp2Handler === handlers.NodeHttpHandler);\n",
    );
    const config = join(directory, "config.json");
    writeFileSync(
      config,
      JSON.stringify({ adapter: "callable", app_dir: directory, entrypoint: "agent.mjs" }),
    );
    return config;
  }

  test.each([
    [true, "true"],
    [false, "false"],
  ])("the process patches only with the gateway endpoint set (%s)", async (gateway, expected) => {
    const { spawnSync } = await import("node:child_process");
    const directory = mkdtempSync(join(tmpdir(), "bridge-"));
    try {
      const config = projectWithSmithy(directory);
      const env: Record<string, string> = { ...(process.env as Record<string, string>) };
      delete env[bridge.GATEWAY_ENDPOINT_ENV];
      if (gateway) {
        env[bridge.GATEWAY_ENDPOINT_ENV] = "http://127.0.0.1:1";
      }
      const result = spawnSync(process.execPath, [BRIDGE, config], {
        input: JSON.stringify({ v: 1, prompt: "x", session_id: SESSION }),
        encoding: "utf8",
        env,
      });
      const lines = result.stdout
        .trim()
        .split("\n")
        .map((line) => JSON.parse(line) as Json);
      expect(lines.find((line) => line.type === "text")?.text).toBe(expected);
    } finally {
      rmSync(directory, { recursive: true });
    }
  });
});
