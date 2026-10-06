/**
 * Dominio del agente de IA (`ai-agent-core`) contra los vectores
 * compartidos con el SDK Python (`testdata/agent/domain-vectors.json`).
 * Espejo de `test_agent_domain.py` y `test_agent_events.py`: las claves de
 * los vectores van en snake_case y aquí se pasan a camelCase (y los
 * segundos a milisegundos).
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, test } from "vitest";
import {
  AGENT_EVENT_TYPES,
  AGENT_FAILURE_MESSAGES,
  AGENT_FAILURE_REASONS,
  agentFailedToError,
  failureMessage,
  truncateToolOutput,
} from "../../src/agent/events.js";
import {
  AgentError,
  AgentLimits,
  AgentModel,
  type AgentModelOptions,
  AgentPermissions,
  type AgentPermissionsOptions,
  AgentSpec,
  agentFailed,
  anthropicGateway,
  bedrockGateway,
  InvalidArgumentError,
  McpLocal,
  type McpLocalOptions,
  McpRemote,
  type McpRemoteOptions,
  type McpServer,
  openaiCompatibleGateway,
  SandboxError,
  type SecretGateway,
  SubAgent,
  type SubAgentOptions,
  TokenUsage,
} from "../../src/index.js";
import {
  DEFAULT_AGENT_MAX_OUTPUT_BYTES,
  DEFAULT_AGENT_MAX_STEPS,
  DEFAULT_AGENT_MAX_TOTAL_TOKENS,
  DEFAULT_AGENT_TIMEOUT_SECONDS,
  MAX_TOOL_OUTPUT_PREVIEW_BYTES,
} from "../../src/limits.js";
import { isSafeRequestPath } from "../../src/secret-gateway/domain.js";

// biome-ignore lint/suspicious/noExplicitAny: vectores JSON sin tipo fijo.
type Json = any;

const VECTORS: Json = JSON.parse(
  readFileSync(
    join(import.meta.dirname, "..", "..", "..", "..", "testdata", "agent", "domain-vectors.json"),
    "utf8",
  ),
);

const MS_PER_SECOND = 1000;

function camel(key: string): string {
  return key.replace(/_([a-z])/g, (_, char: string) => char.toUpperCase());
}

/** Pasa las claves de primer nivel a camelCase y `*_seconds` a `*Ms`. */
function options(args: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(args)) {
    if (key.endsWith("_seconds") && typeof value === "number") {
      out[camel(key.replace(/_seconds$/, "_ms"))] = value * MS_PER_SECOND;
    } else {
      out[camel(key)] = value;
    }
  }
  return out;
}

function mcp(raw: Record<string, Json>): Record<string, McpServer> {
  const servers: Record<string, McpServer> = {};
  for (const [name, entry] of Object.entries(raw)) {
    servers[name] =
      "remote" in entry
        ? new McpRemote(options(entry.remote) as unknown as McpRemoteOptions)
        : new McpLocal(options(entry.local) as unknown as McpLocalOptions);
  }
  return servers;
}

const BEDROCK = new AgentModel({
  provider: "bedrock",
  id: "us.anthropic.claude-haiku-4-5-20251001-v1:0",
  gateway: "bedrock",
  region: "us-east-1",
});

function assertGateway(gateway: SecretGateway, vector: Json): void {
  expect(gateway.upstream).toBe(vector.upstream);
  expect(Object.keys(gateway.headers)).toEqual(vector.headers);
  expect(gateway.allow.map((rule) => [rule.method, rule.path])).toEqual(vector.allow);
}

describe("AgentModel", () => {
  test.each(VECTORS.models.valid as Json[])("acepta %o", (args) => {
    expect(new AgentModel(options(args) as unknown as AgentModelOptions).id).toBe(args.id);
  });

  test.each(VECTORS.models.invalid as Json[])("rechaza: $why", ({ args }) => {
    expect(() => new AgentModel(options(args) as unknown as AgentModelOptions)).toThrow(
      InvalidArgumentError,
    );
  });
});

describe("AgentPermissions", () => {
  test.each(VECTORS.permissions.valid as Json[])("permisos efectivos de %o", (vector) => {
    const permissions = new AgentPermissions(vector.args as AgentPermissionsOptions);
    expect(permissions.effectiveTools()).toEqual(vector.effective);
  });

  test.each(VECTORS.permissions.invalid as Json[])("rechaza: $why", ({ args }) => {
    expect(() => new AgentPermissions(args as AgentPermissionsOptions)).toThrow(
      InvalidArgumentError,
    );
  });

  test("'ask' se rechaza con un motivo", () => {
    expect(
      () => new AgentPermissions({ tools: { edit: "ask" } } as unknown as AgentPermissionsOptions),
    ).toThrow(/'ask' no se admite/);
  });

  test("copia congelada de tools", () => {
    const tools: Record<string, "allow" | "deny"> = { edit: "deny" };
    const permissions = new AgentPermissions({ tools });
    tools.edit = "allow";
    expect(permissions.tools.edit).toBe("deny");
    expect(Object.isFrozen(permissions.tools)).toBe(true);
  });
});

describe("AgentSpec", () => {
  test.each(VECTORS.raw_config.reserved as string[])("rawConfig rechaza '%s' por nombre", (key) => {
    expect(() => new AgentSpec({ model: BEDROCK, rawConfig: { [key]: {} } })).toThrow(
      new RegExp(`'${key}'`),
    );
  });

  test.each(VECTORS.raw_config.allowed as Json[])("rawConfig acepta %o", (rawConfig) => {
    expect(new AgentSpec({ model: BEDROCK, rawConfig }).rawConfig).toEqual(rawConfig);
  });

  test.each(VECTORS.spec.invalid as Json[])("rechaza: $why", (vector) => {
    expect(() => {
      const agents: Record<string, SubAgent> = {};
      for (const [name, args] of Object.entries((vector.agents ?? {}) as Record<string, Json>)) {
        agents[name] = new SubAgent(args as SubAgentOptions);
      }
      return new AgentSpec({
        model: BEDROCK,
        agents,
        mcp: mcp(vector.mcp ?? {}),
        instructions: vector.instructions,
      });
    }).toThrow(InvalidArgumentError);
  });

  test("smallModel es por defecto el modelo", () => {
    expect(new AgentSpec({ model: BEDROCK }).effectiveSmallModel).toBe(BEDROCK.id);
    expect(new AgentSpec({ model: BEDROCK, smallModel: "other" }).effectiveSmallModel).toBe(
      "other",
    );
  });

  test("pasarelas necesarias y pasarela ausente", () => {
    const vector = VECTORS.spec.gateways;
    const spec = new AgentSpec({ model: BEDROCK, mcp: mcp(vector.mcp) });
    expect([...spec.gatewayNames()].sort()).toEqual(vector.needed);
    spec.requireGateways(["bedrock", "docs-mcp", "unused"]);
    expect(() => spec.requireGateways(["bedrock"])).toThrow(/'docs-mcp'/);
  });

  test("McpLocal no muestra envs al inspeccionar", () => {
    const server = new McpLocal({ command: ["mcp-fs"], envs: { TOKEN_FILE: "stays-out" } });
    expect(JSON.stringify(server)).not.toContain("stays-out");
    expect(server.envs.TOKEN_FILE).toBe("stays-out");
  });
});

describe("AgentLimits", () => {
  test("valores por defecto de limits.json", () => {
    const limits = new AgentLimits();
    const defaults = VECTORS.limits.defaults;
    expect(limits.maxSteps).toBe(DEFAULT_AGENT_MAX_STEPS);
    expect(limits.maxSteps).toBe(defaults.max_steps);
    expect(limits.timeoutMs).toBe(DEFAULT_AGENT_TIMEOUT_SECONDS * MS_PER_SECOND);
    expect(limits.timeoutMs).toBe(defaults.timeout_seconds * MS_PER_SECOND);
    expect(limits.maxOutputBytes).toBe(DEFAULT_AGENT_MAX_OUTPUT_BYTES);
    expect(limits.maxOutputBytes).toBe(defaults.max_output_bytes);
    expect(limits.maxTotalTokens).toBe(DEFAULT_AGENT_MAX_TOTAL_TOKENS);
    expect(limits.maxTotalTokens).toBe(defaults.max_total_tokens);
    expect(new AgentLimits({ maxTotalTokens: null }).maxTotalTokens).toBeNull();
  });

  test.each(VECTORS.limits.invalid as Json[])("rechaza %o", (args) => {
    expect(() => new AgentLimits(options(args))).toThrow(InvalidArgumentError);
  });
});

describe("pasarelas ya hechas", () => {
  test("bedrockGateway", () => {
    const vector = VECTORS.gateway_presets.bedrock;
    const gateway = bedrockGateway("bedrock-key", {
      region: vector.region,
      models: vector.models,
    });
    assertGateway(gateway, vector);
    expect(gateway.allow.every((rule) => isSafeRequestPath(rule.path))).toBe(true);
    expect(bedrockGateway("k", { region: "us-east-1", models: ["m", "m"] }).allow).toHaveLength(2);
  });

  test.each(VECTORS.gateway_presets.bedrock_invalid as Json[])("bedrock rechaza: $why", (c) => {
    expect(() => bedrockGateway("k", { region: c.region, models: c.models })).toThrow(
      InvalidArgumentError,
    );
  });

  test("anthropicGateway", () => {
    assertGateway(anthropicGateway("anthropic"), VECTORS.gateway_presets.anthropic);
    expect(anthropicGateway("a", { ratePerMinute: 60 }).ratePerMinute).toBe(60);
  });

  test("openaiCompatibleGateway", () => {
    const vector = VECTORS.gateway_presets.openai_compatible;
    assertGateway(
      openaiCompatibleGateway("openai", { upstream: vector.upstream, basePath: vector.base_path }),
      vector,
    );
    expect(() =>
      openaiCompatibleGateway("openai", { upstream: vector.upstream, basePath: "v1" }),
    ).toThrow(InvalidArgumentError);
  });
});

describe("eventos y fallos", () => {
  test("discriminadores en el orden del protocolo", () => {
    expect([...AGENT_EVENT_TYPES]).toEqual(VECTORS.event_types);
  });

  test("tabla de fallos cerrada y compartida", () => {
    expect({ ...AGENT_FAILURE_MESSAGES }).toEqual(VECTORS.failures.messages);
    expect([...AGENT_FAILURE_REASONS].sort()).toEqual(
      Object.keys(VECTORS.failures.messages).sort(),
    );
  });

  test.each(VECTORS.failures.detail_codes as Json[])("detailCode seguro: %o", (c) => {
    expect(failureMessage(c.reason, c.detail_code)).toBe(c.message);
    const event = agentFailed(c.reason, { detailCode: c.detail_code });
    expect(agentFailedToError(event).message).toBe(c.message);
  });

  test("reason desconocido", () => {
    expect(() => agentFailed("exploded")).toThrow(InvalidArgumentError);
    expect(() => failureMessage("exploded")).toThrow(InvalidArgumentError);
  });

  test("AgentFailed se convierte en AgentError", () => {
    const usage = new TokenUsage({ input: 10, output: 2 });
    const error = agentFailedToError(
      agentFailed("token_budget", { exitCode: 137, detailCode: "x", sessionId: "ses_1" }),
      usage,
    );
    expect(error).toBeInstanceOf(AgentError);
    expect(error).toBeInstanceOf(SandboxError);
    expect(error.name).toBe("AgentError");
    expect([error.reason, error.sessionId, error.exitCode, error.detailCode]).toEqual([
      "token_budget",
      "ses_1",
      137,
      "x",
    ]);
    expect(error.usage).toBe(usage);
    expect(error.message).toBe("el agente superó su presupuesto de tokens (x)");
  });

  test.each(VECTORS.usage as Json[])("TokenUsage.total %o", (c) => {
    expect(new TokenUsage(options(c.tokens)).total).toBe(c.total);
  });

  test("TokenUsage suma y rechaza negativos", () => {
    const total = new TokenUsage({ input: 1, cacheRead: 2 }).plus(
      new TokenUsage({ output: 3, cacheWrite: 4, reasoning: 5 }),
    );
    expect(total).toEqual(
      new TokenUsage({ input: 1, output: 3, reasoning: 5, cacheRead: 2, cacheWrite: 4 }),
    );
    expect(() => new TokenUsage({ input: -1 })).toThrow(InvalidArgumentError);
  });

  test("la vista previa de una herramienta se corta entre caracteres", () => {
    expect(MAX_TOOL_OUTPUT_PREVIEW_BYTES).toBe(VECTORS.tool_output.preview_bytes);
    expect(truncateToolOutput("ok")).toEqual({ text: "ok", truncated: false });
    const text = `${"a".repeat(MAX_TOOL_OUTPUT_PREVIEW_BYTES - 1)}ñtail`;
    expect(truncateToolOutput(text)).toEqual({
      text: "a".repeat(MAX_TOOL_OUTPUT_PREVIEW_BYTES - 1),
      truncated: true,
    });
  });
});
