/**
 * Adaptador de deepagents (`ai-agent-deepagents`) contra los ficheros
 * dorados que comparte con Python (`testdata/agent/`): misma configuración y
 * sha256, mismo script y stdin, misma traducción del protocolo v1, y el
 * runner generado idéntico al de Python. Espejo de `test_agent_deepagents.py`.
 */

import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, test } from "vitest";
import {
  DEEPAGENTS_RUNNER_SHA256,
  DEEPAGENTS_RUNNER_SOURCE,
} from "../../src/agent/assets/deepagents-runner.js";
import { DEEPAGENTS_CONFIG_PATH, type DeepAgentsState } from "../../src/agent/deepagents.js";
import type { RunRequest } from "../../src/agent/runtime.js";
import { AGENT_RUNTIMES, resolveRuntime } from "../../src/agent/runtimes.js";
import {
  AgentModel,
  AgentPermissions,
  AgentSpec,
  DeepAgents,
  InvalidArgumentError,
  McpLocal,
  SubAgent,
} from "../../src/index.js";

const REPO = join(import.meta.dirname, "..", "..", "..", "..");
const TESTDATA = join(REPO, "testdata", "agent");
const GATEWAY_URLS = {
  bedrock: "http://127.0.0.1:18001",
  anthropic: "http://127.0.0.1:18002",
  openai: "http://127.0.0.1:18003/",
};
const SESSION_ID = "rda_0000000000000000000000000000000a";
const UTF8 = new TextEncoder();
const DECODER = new TextDecoder();

// biome-ignore lint/suspicious/noExplicitAny: dorados JSON compartidos, sin esquema.
function readJson(...parts: string[]): any {
  return JSON.parse(readFileSync(join(TESTDATA, ...parts), "utf8"));
}

function bedrockModel(): AgentModel {
  return new AgentModel({
    provider: "bedrock",
    id: "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    gateway: "bedrock",
    region: "us-east-1",
  });
}

function bedrockSpec(): AgentSpec {
  return new AgentSpec({ model: bedrockModel() });
}

const CONFIG_CASES: Record<string, () => AgentSpec> = {
  bedrock: bedrockSpec,
  anthropic: () =>
    new AgentSpec({
      model: new AgentModel({
        provider: "anthropic",
        id: "claude-haiku-4-5",
        gateway: "anthropic",
        promptCaching: false,
      }),
      instructions: "Responde en español.",
      permissions: new AgentPermissions({
        default: "deny",
        tools: { read: "allow", edit: "allow", bash: { "git *": "allow", "*": "deny" } },
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

describe("deepagents config", () => {
  test.each(Object.keys(CONFIG_CASES))("%s matches golden", (name) => {
    const golden = readJson("deepagents-config.json")[name];
    const spec = (CONFIG_CASES[name] as () => AgentSpec)();
    const files = new DeepAgents({ entrypoint: golden.entrypoint ?? undefined }).buildConfig(spec, {
      gatewayUrls: GATEWAY_URLS,
      workdir: "/home/user/proyecto",
    });
    expect(files.files).toHaveLength(1);
    const [file] = files.files;
    expect(file?.path).toBe(DEEPAGENTS_CONFIG_PATH);
    expect(DECODER.decode(file?.data)).toBe(golden.config_json);
    expect(file?.mode).toBe(golden.mode);
    expect(files.configSha256).toBe(golden.config_sha256);
  });

  test("registry has deepagents", () => {
    expect(AGENT_RUNTIMES.deepagents).toBeInstanceOf(DeepAgents);
    expect(resolveRuntime("deepagents").name).toBe("deepagents");
    const custom = new DeepAgents({ entrypoint: "pkg.mod:build" });
    expect(resolveRuntime(custom)).toBe(custom);
  });

  test.each(["", "pkg", "pkg.mod:", "1pkg:build", "pkg:mod:x"])(
    "entrypoint %j is rejected",
    (entrypoint) => {
      expect(() => new DeepAgents({ entrypoint })).toThrow(InvalidArgumentError);
    },
  );

  test.each([
    [
      "mcp",
      () =>
        new AgentSpec({ model: bedrockModel(), mcp: { local: new McpLocal({ command: ["x"] }) } }),
    ],
    ["rawConfig", () => new AgentSpec({ model: bedrockModel(), rawConfig: { tui: { x: 1 } } })],
    [
      "unknown tool",
      () =>
        new AgentSpec({
          model: bedrockModel(),
          permissions: new AgentPermissions({ tools: { webfetch: "allow" } }),
        }),
    ],
    [
      "patterns outside bash",
      () =>
        new AgentSpec({
          model: bedrockModel(),
          permissions: new AgentPermissions({ tools: { read: { "*.env": "deny" } } }),
        }),
    ],
  ])("rejects %s", (_name, build) => {
    expect(() =>
      new DeepAgents().buildConfig(build(), { gatewayUrls: GATEWAY_URLS, workdir: "/home/user" }),
    ).toThrow(InvalidArgumentError);
  });

  test("requires every gateway", () => {
    expect(() =>
      new DeepAgents().buildConfig(bedrockSpec(), {
        gatewayUrls: { otra: "http://x" },
        workdir: "/home/user",
      }),
    ).toThrow(InvalidArgumentError);
  });
});

describe("deepagents run command", () => {
  const cases: Record<string, Partial<RunRequest>> = {
    new: {},
    resume: {
      sessionId: SESSION_ID,
      model: "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
      reasoning: true,
      attach: false,
    },
  };
  test.each(Object.keys(cases))("%s matches golden", (name) => {
    const golden = readJson("deepagents-run-commands.json")[name];
    const command = new DeepAgents().command({
      spec: bedrockSpec(),
      prompt: "hola ñ",
      workdir: "/home/user",
      ...cases[name],
    });
    expect(command.script).toBe(golden.script);
    expect(command.envs).toEqual(golden.envs);
    expect(DECODER.decode(command.stdin)).toBe(golden.stdin);
    expect(command.script).not.toContain("hola");
  });

  test.each([
    { attach: true },
    { attach: 1 as unknown as boolean },
    { sessionId: "ses_otro" },
    { model: "con espacios" },
  ])("rejects %j", (extra) => {
    expect(() =>
      new DeepAgents().command({
        spec: bedrockSpec(),
        prompt: "x",
        workdir: "/home/user",
        ...extra,
      }),
    ).toThrow(InvalidArgumentError);
  });
});

describe("deepagents events", () => {
  test("protocol vector matches golden", () => {
    const expected = readJson("rayito-protocol-v1-expected.json");
    const runtime = new DeepAgents();
    const state = runtime.newState();
    const events: unknown[] = [];
    const lines = readFileSync(join(TESTDATA, "rayito-protocol-v1.jsonl"), "utf8").split("\n");
    for (const line of lines.filter((l) => l !== "")) {
      events.push(...runtime.parseLine(UTF8.encode(line), state).map(snake));
    }
    expect(events).toEqual(expected.events);
    const typed = state as DeepAgentsState;
    expect(typed.sessionId).toBe(expected.session_id);
    expect(typed.ignoredLines).toBe(expected.ignored_lines);
    expect(snake(runtime.finish(state, 0))).toEqual(expected.done);
  });

  test.each([
    [
      '{"v":1,"type":"agent_failed","reason":"model_error","detail_code":"ThrottlingException"}',
      "model_error",
      "ThrottlingException",
    ],
    [
      '{"v":1,"type":"agent_failed","reason":"max_steps","detail_code":"x y"}',
      "protocol_error",
      undefined,
    ],
    ['{"v":1,"type":"rayito.busy"}', "busy", undefined],
    ['{"v":1,"type":"rayito.runtime_missing"}', "runtime_missing", undefined],
  ])("%s is terminal", (line, reason, detail) => {
    const runtime = new DeepAgents();
    const state = runtime.newState();
    const [event] = runtime.parseLine(UTF8.encode(line), state);
    expect(event).toMatchObject({ type: "agent_failed", reason, detailCode: detail });
    expect(runtime.finish(state, 0)).toBe(event);
  });

  test("finish without done", () => {
    const runtime = new DeepAgents();
    const state = runtime.newState();
    runtime.parseLine(UTF8.encode(`{"v":1,"type":"session","session_id":"${SESSION_ID}"}`), state);
    expect(runtime.finish(state, 0)).toMatchObject({
      reason: "protocol_error",
      sessionId: SESSION_ID,
    });
    expect(runtime.finish(state, 137)).toMatchObject({ reason: "runtime_error", exitCode: 137 });
  });

  test("oversized tool output is truncated", () => {
    const runtime = new DeepAgents();
    const line = JSON.stringify({
      v: 1,
      type: "tool_call",
      call_id: "c",
      name: "ls",
      status: "completed",
      output: "x".repeat(70_000),
    });
    const [event] = runtime.parseLine(UTF8.encode(line), runtime.newState());
    expect(event).toMatchObject({ type: "tool_call", outputTruncated: true });
  });

  test("abort, template and warmup", () => {
    const runtime = new DeepAgents();
    expect(runtime.abortCommand(runtime.newState())).toBeUndefined();
    expect(runtime.templateSteps()).toEqual([]);
    const steps = runtime.warmupSteps({ serve: true });
    expect(steps).toHaveLength(1);
    expect(steps[0]?.cmd).toContain("import deepagents");
  });
});

describe("deepagents runner asset", () => {
  test("is the Python runner byte for byte", () => {
    const source = readFileSync(
      join(REPO, "clients", "python", "src", "rayito", "_agent", "_runner", "deepagents_runner.py"),
    );
    expect(DEEPAGENTS_RUNNER_SOURCE).toBe(source.toString("utf8"));
    expect(DEEPAGENTS_RUNNER_SHA256).toBe(createHash("sha256").update(source).digest("hex"));
  });
});
