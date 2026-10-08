/**
 * Adaptador de OpenCode (`ai-agent-core`) contra los ficheros dorados que
 * comparte con Python (`testdata/agent/`): misma configuración y sha256,
 * mismo script y misma traducción de eventos. Espejo de
 * `test_agent_opencode.py`; las claves de los dorados van en snake_case.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, test } from "vitest";
import type { ModelProvider } from "../../src/agent/domain.js";
import {
  OPENCODE_CONFIG_PATH,
  OPENCODE_INSTRUCTIONS_PATH,
  OPENCODE_NATIVE_BASE_PATHS,
  OPENCODE_NATIVE_NPM,
  OpenCodeRuntime,
  type OpenCodeState,
} from "../../src/agent/opencode.js";
import type { RunRequest } from "../../src/agent/runtime.js";
import { resolveRuntime } from "../../src/agent/runtimes.js";
import {
  AgentModel,
  AgentPermissions,
  AgentSpec,
  InvalidArgumentError,
  McpLocal,
  McpRemote,
  SubAgent,
} from "../../src/index.js";

const TESTDATA = join(import.meta.dirname, "..", "..", "..", "..", "testdata", "agent");
const GATEWAY_URLS = {
  bedrock: "http://127.0.0.1:18001",
  anthropic: "http://127.0.0.1:18002",
  openai: "http://127.0.0.1:18003/",
  docs: "http://127.0.0.1:18004",
};
const UTF8 = new TextEncoder();
const DECODER = new TextDecoder();

// biome-ignore lint/suspicious/noExplicitAny: dorados JSON compartidos, sin esquema.
function readJson(...parts: string[]): any {
  return JSON.parse(readFileSync(join(TESTDATA, ...parts), "utf8"));
}

function bedrockSpec(): AgentSpec {
  return new AgentSpec({
    model: new AgentModel({
      provider: "bedrock",
      id: "us.anthropic.claude-haiku-4-5-20251001-v1:0",
      gateway: "bedrock",
      region: "us-east-1",
    }),
  });
}

function nativeSpec(provider: ModelProvider, id: string, smallModel: string): AgentSpec {
  return new AgentSpec({
    model: new AgentModel({ provider, id, gateway: "openai" }),
    smallModel,
    agents: {
      revisor: new SubAgent({ description: "Revisa cambios", instructions: "Revisa el diff." }),
    },
  });
}

const CONFIG_CASES: Record<string, () => AgentSpec> = {
  openai: () => nativeSpec("openai", "gpt-5", "gpt-5-mini"),
  google: () => nativeSpec("google", "gemini-2.5-pro", "gemini-2.5-flash"),
  azure: () => nativeSpec("azure", "mi-despliegue", "mi-despliegue-mini"),
  litellm: () =>
    new AgentSpec({
      model: new AgentModel({
        provider: "openai-compatible",
        id: "modelo-litellm",
        gateway: "openai",
        basePath: "/v1",
      }),
    }),
  bedrock: bedrockSpec,
  anthropic: () =>
    new AgentSpec({
      model: new AgentModel({
        provider: "anthropic",
        id: "claude-haiku-4-5",
        gateway: "anthropic",
      }),
      smallModel: "claude-haiku-4-5-mini",
      instructions: "Responde en español.",
      permissions: new AgentPermissions({
        default: "deny",
        tools: { read: "allow", bash: { "git *": "allow", "*": "deny" } },
      }),
    }),
  "openai-compatible": () =>
    new AgentSpec({
      model: new AgentModel({
        provider: "openai-compatible",
        id: "modelo-a",
        gateway: "openai",
        basePath: "/v1",
      }),
      agents: {
        revisor: new SubAgent({
          description: "Revisa cambios",
          instructions: "Revisa el diff.",
          model: "modelo-b",
          permissions: new AgentPermissions({ tools: { edit: "deny" } }),
        }),
      },
      mcp: {
        local: new McpLocal({
          command: ["mcp-server", "--stdio"],
          envs: { MODO: "x" },
          timeoutMs: 2500,
        }),
        docs: new McpRemote({ gateway: "docs", path: "/mcp" }),
      },
      rawConfig: { tui: { scroll_speed: 3 } },
    }),
};

function snake(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map(snake);
  }
  if (typeof value === "object" && value !== null) {
    const out: Record<string, unknown> = {};
    for (const [key, item] of Object.entries(value)) {
      out[key.replace(/[A-Z]/g, (c) => `_${c.toLowerCase()}`)] =
        item === undefined ? null : key === "input" ? item : snake(item);
    }
    return out;
  }
  return value;
}

describe("OpenCode config", () => {
  test.each(Object.keys(CONFIG_CASES))("%s matches golden", (name) => {
    const golden = readJson("opencode-config", `${name}.json`);
    const spec = (CONFIG_CASES[name] as () => AgentSpec)();
    const files = new OpenCodeRuntime().buildConfig(spec, {
      gatewayUrls: GATEWAY_URLS,
      workdir: "/home/user",
    });
    const byPath = Object.fromEntries(files.files.map((f) => [f.path, DECODER.decode(f.data)]));
    expect(byPath[OPENCODE_CONFIG_PATH]).toBe(golden.opencode_json);
    expect(byPath[OPENCODE_INSTRUCTIONS_PATH] ?? null).toBe(golden.agents_md);
    expect(files.configSha256).toBe(golden.config_sha256);
  });

  test("requires every gateway", () => {
    expect(() =>
      new OpenCodeRuntime().buildConfig(bedrockSpec(), {
        gatewayUrls: { otra: "http://x" },
        workdir: "/home/user",
      }),
    ).toThrow(InvalidArgumentError);
  });
});

describe("OpenCode run command", () => {
  const cases: Record<string, Partial<RunRequest>> = {
    default: {},
    resume: {
      sessionId: "ses_0000000000000000000000000a",
      model: "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
      reasoning: true,
      workdir: "/home/user/mi proyecto's",
    },
  };
  test.each(Object.keys(cases))("%s matches golden", (name) => {
    const golden = readJson("opencode-run-commands.json")[name];
    const command = new OpenCodeRuntime().command({
      spec: bedrockSpec(),
      prompt: "hola ñ",
      workdir: "/home/user",
      ...cases[name],
    });
    expect(command.script).toBe(golden.script);
    expect(command.envs).toEqual(golden.envs);
    expect(DECODER.decode(command.stdin)).toBe("hola ñ");
    expect(command.script).not.toContain("hola");
  });

  test("rejects a session id that is not OpenCode's", () => {
    expect(() =>
      new OpenCodeRuntime().command({
        spec: bedrockSpec(),
        prompt: "x",
        workdir: "/home/user",
        sessionId: 'a"b',
      }),
    ).toThrow(InvalidArgumentError);
  });

  test("the script execs opencode directly", () => {
    const { script } = new OpenCodeRuntime().command({
      spec: bedrockSpec(),
      prompt: "x",
      workdir: "/home/user",
    });
    expect(script.trimEnd().split("\n").at(-1)).toBe(
      "exec 'opencode' 'run' '--format' 'json' '--auto' '--title' 'rayito' '--dir' '/home/user'",
    );
    expect(script).not.toContain("--attach");
    expect(script).not.toContain("curl");
  });
});

describe("OpenCode events", () => {
  test("captured run matches golden", () => {
    const expected = readJson("expected-events.json");
    const runtime = new OpenCodeRuntime();
    const state = runtime.newState();
    const events: unknown[] = [];
    const lines = readFileSync(join(TESTDATA, "opencode-v1.18.34-events.jsonl"), "utf8").split(
      "\n",
    );
    for (const line of lines.filter((l) => l !== "")) {
      events.push(...runtime.parseLine(UTF8.encode(line), state).map(snake));
    }
    expect(events).toEqual(expected.events);
    const typed = state as OpenCodeState;
    expect(typed.ignoredLines).toBe(expected.ignored_lines);
    expect(snake(runtime.finish(state, 0))).toEqual(expected.done);
  });

  test.each([
    [
      '{"type":"error","sessionID":"ses_1","error":{"name":"APIError","data":{"message":"x"}}}',
      "model_error",
      "APIError",
    ],
    ['{"type":"rayito.busy"}', "busy", undefined],
    ['{"type":"rayito.runtime_missing"}', "runtime_missing", undefined],
  ])("%s is terminal", (line, reason, detail) => {
    const runtime = new OpenCodeRuntime();
    const state = runtime.newState();
    const [event] = runtime.parseLine(UTF8.encode(line), state);
    expect(event).toMatchObject({ type: "agent_failed", reason, detailCode: detail });
    expect(runtime.finish(state, 0)).toBe(event);
  });

  test("finish without error events", () => {
    const runtime = new OpenCodeRuntime();
    const state = runtime.newState();
    expect(runtime.finish(state, 0)).toMatchObject({ reason: "protocol_error" });
    runtime.parseLine(UTF8.encode('{"type":"step_start","sessionID":"ses_1","part":{}}'), state);
    expect(runtime.finish(state, 3)).toMatchObject({
      reason: "runtime_error",
      exitCode: 3,
      sessionId: "ses_1",
    });
  });

  test("warmup steps and registry", () => {
    const runtime = new OpenCodeRuntime();
    expect(runtime.warmupSteps()).toEqual([{ cmd: "opencode --version >/dev/null" }]);
    expect(runtime.templateSteps()).toEqual([]);
    expect(resolveRuntime("opencode")).toBeInstanceOf(OpenCodeRuntime);
  });
});

test("los mapas de proveedores nativos comparten claves", () => {
  expect(Object.keys(OPENCODE_NATIVE_NPM).sort()).toEqual(
    Object.keys(OPENCODE_NATIVE_BASE_PATHS).sort(),
  );
});
