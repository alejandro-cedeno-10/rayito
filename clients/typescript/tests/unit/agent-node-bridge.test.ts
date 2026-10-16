/**
 * El puente de Node de Strands (`strands_bridge.mjs`, ADR-028): la traducción
 * de `agent.stream()` a eventos v1 contra los eventos grabados que comparte con
 * el driver de Python (`testdata/agent/strands-ts-events.jsonl` y
 * `strands-expected-events.json`), los permisos, la construcción del modelo por
 * pasarela y una ejecución completa con módulos de doble. El puente es Node: se
 * prueba aquí, con el SDK de TypeScript, y el host (Python) lo ejecuta tal cual.
 */

import { readFileSync } from "node:fs";
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
  "strands_bridge.mjs",
);

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
  truncateOutput(text: string): [string, boolean];
  DENIED_TOOL_MESSAGE: string;
  ANTHROPIC_MAX_TOKENS: number;
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
