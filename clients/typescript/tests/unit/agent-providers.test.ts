/**
 * Catálogo de proveedores del agente (`ai-agent-providers`): presets de
 * pasarela, `ModelProvider` y su traducción a OpenCode y deepagents, contra
 * `testdata/agent/provider-catalogue.json`. Espejo de
 * `test_agent_providers.py`: los args del catálogo van en snake_case y aquí
 * se pasan a camelCase. Nada aquí llama a AWS ni a la red.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, test, vi } from "vitest";
import { DeepAgents } from "../../src/agent/deepagents.js";
import { MODEL_PROVIDERS, type ModelProvider } from "../../src/agent/domain.js";
import { OPENCODE_CONFIG_PATH, OpenCodeRuntime } from "../../src/agent/opencode.js";
import { startAttributes } from "../../src/agent/telemetry.js";
import { UnimplementedError } from "../../src/errors.js";
import * as rayito from "../../src/index.js";
import {
  AgentModel,
  AgentSpec,
  InvalidArgumentError,
  type SecretGateway,
} from "../../src/index.js";
import * as optional from "../../src/optional.js";
import { isSafeRequestPath } from "../../src/secret-gateway/domain.js";

interface PresetEntry {
  readonly name: string;
  readonly typescript: string;
  readonly args: Record<string, unknown>;
  readonly model_provider: ModelProvider;
  readonly base_path: string;
  readonly upstream: string;
  readonly headers: string[];
  readonly allow: [string, string][];
  readonly model_allowlist: boolean;
}

interface InvalidEntry {
  readonly why: string;
  readonly typescript: string;
  readonly args: Record<string, unknown>;
}

interface ProviderEntry {
  readonly otel_provider: string;
  readonly opencode_provider_id: string;
  readonly opencode_base_path?: string;
  readonly deepagents: "supported" | "unimplemented";
  readonly deepagents_base_path?: string;
  readonly opencode_operation?: string;
  readonly deepagents_operation?: string;
}

/** Prefijos de `<runtime>_base_path` / `<runtime>_operation` en el catálogo. */
const RUNTIMES = ["opencode", "deepagents"] as const;

interface Catalogue {
  readonly secret: string;
  readonly presets: PresetEntry[];
  readonly invalid: InvalidEntry[];
  readonly model_providers: Record<string, ProviderEntry>;
  readonly refused: { readonly id: string }[];
}

const CATALOGUE = JSON.parse(
  readFileSync(
    join(
      import.meta.dirname,
      "..",
      "..",
      "..",
      "..",
      "testdata",
      "agent",
      "provider-catalogue.json",
    ),
    "utf8",
  ),
) as Catalogue;
const GATEWAY_URL = "http://127.0.0.1:18005";
const PLACEHOLDER = "placeholder-not-a-secret";

type Preset = (secret: string, options?: Record<string, unknown>) => SecretGateway;

function camel(key: string): string {
  return key.replace(/_([a-z])/g, (_, letter: string) => letter.toUpperCase());
}

function camelArgs(args: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(Object.entries(args).map(([key, value]) => [camel(key), value]));
}

function preset(name: string): Preset {
  const fn = (rayito as unknown as Record<string, unknown>)[name];
  if (typeof fn !== "function") {
    throw new Error(`${name} no se exporta`);
  }
  return fn as Preset;
}

function build(entry: { typescript: string; args: Record<string, unknown> }): SecretGateway {
  return preset(entry.typescript)(CATALOGUE.secret, camelArgs(entry.args));
}

function spec(provider: string): AgentSpec {
  return new AgentSpec({
    model: new AgentModel({
      provider: provider as ModelProvider,
      id: "modelo-a",
      gateway: "modelo",
    }),
  });
}

const NATIVE = Object.entries(CATALOGUE.model_providers)
  .filter(([, entry]) => entry.opencode_base_path !== undefined)
  .map(([name]) => name)
  .sort();

describe("gateway presets", () => {
  test.each(CATALOGUE.presets.map((entry) => [entry.name, entry] as const))(
    "%s matches the catalogue",
    (_, entry) => {
      const gateway = build(entry);
      expect(gateway.upstream).toBe(entry.upstream);
      expect(Object.keys(gateway.headers)).toEqual(entry.headers);
      expect(Object.values(gateway.headers).every((value) => value === CATALOGUE.secret)).toBe(
        true,
      );
      expect(gateway.allow.map((rule) => [rule.method, rule.path])).toEqual(entry.allow);
      expect(gateway.allow.every((rule) => isSafeRequestPath(rule.path))).toBe(true);
    },
  );

  test.each(CATALOGUE.presets.map((entry) => [entry.name, entry] as const))(
    "%s pairs with its AgentModel",
    (_, entry) => {
      const model = new AgentModel({
        provider: entry.model_provider,
        id: "modelo-a",
        gateway: "modelo",
        basePath: entry.base_path,
      });
      if (model.provider === "openai-compatible") {
        expect(entry.allow.some(([, path]) => path.startsWith(`${model.basePath}/`))).toBe(true);
      }
      const provider = CATALOGUE.model_providers[model.provider] as ProviderEntry;
      const modelId = (entry.args.models as string[] | undefined)?.[0] ?? model.id;
      const allowed = entry.allow.map(([, path]) => path);
      for (const runtime of RUNTIMES) {
        const operation = provider[`${runtime}_operation`];
        if (operation !== undefined) {
          const basePath = provider[`${runtime}_base_path`] ?? "";
          expect(allowed).toContain(basePath + operation.replace("{model}", modelId));
        }
      }
    },
  );

  test.each(CATALOGUE.presets.map((entry) => [entry.name, entry] as const))(
    "%s allowlists models only when they go in the path",
    (_, entry) => {
      const models = (entry.args.models as string[] | undefined) ?? [];
      expect(entry.model_allowlist).toBe(models.length > 0);
      for (const modelId of models) {
        expect(entry.allow.some(([, path]) => path.includes(`/${modelId}:`))).toBe(true);
      }
    },
  );

  test("presets carry the rate limit", () => {
    expect(rayito.openaiGateway("k", { ratePerMinute: 30 }).ratePerMinute).toBe(30);
    expect(
      rayito.litellmGateway("k", {
        upstream: "https://litellm.example.com",
        ratePerMinute: 30,
      }).ratePerMinute,
    ).toBe(30);
  });

  test.each(CATALOGUE.invalid.map((entry) => [entry.why, entry] as const))(
    "rejects %s",
    (_, entry) => {
      expect(() => build(entry)).toThrow(InvalidArgumentError);
    },
  );

  test("building a preset loads no AWS peer", () => {
    const loader = vi.spyOn(optional, "loadOptionalPeer");
    for (const entry of CATALOGUE.presets) {
      build(entry);
    }
    expect(loader).not.toHaveBeenCalled();
  });
});

describe("model providers", () => {
  test("match the catalogue and leave out the refused ones", () => {
    expect([...MODEL_PROVIDERS].sort()).toEqual(Object.keys(CATALOGUE.model_providers).sort());
    for (const refused of CATALOGUE.refused) {
      expect(MODEL_PROVIDERS as readonly string[]).not.toContain(refused.id);
    }
    expect(
      () =>
        new AgentModel({
          provider: "chatgpt-plan" as ModelProvider,
          id: "m",
          gateway: "modelo",
        }),
    ).toThrow(InvalidArgumentError);
  });

  test.each(Object.keys(CATALOGUE.model_providers).sort())("%s otel provider name", (provider) => {
    const model = new AgentModel({
      provider: provider as ModelProvider,
      id: "modelo-a",
      gateway: "modelo",
      region: provider === "bedrock" ? "us-east-1" : undefined,
    });
    const attributes = startAttributes(new AgentSpec({ model }), "opencode");
    expect(attributes["gen_ai.provider.name"]).toBe(
      CATALOGUE.model_providers[provider]?.otel_provider,
    );
  });

  test.each(NATIVE)("OpenCode %s goes through the gateway", (provider) => {
    const entry = CATALOGUE.model_providers[provider] as ProviderEntry;
    const files = new OpenCodeRuntime().buildConfig(spec(provider), {
      gatewayUrls: { modelo: GATEWAY_URL },
      workdir: "/home/user",
    });
    expect(files.files.map((file) => file.path)).toEqual([OPENCODE_CONFIG_PATH]);
    const config = JSON.parse(new TextDecoder().decode(files.files[0]?.data)) as Record<
      string,
      unknown
    >;
    const id = entry.opencode_provider_id;
    expect(config.enabled_providers).toEqual([id]);
    expect(config.model).toBe(`${id}/modelo-a`);
    expect(config.auth).toBeUndefined();
    expect(config.plugin).toBeUndefined();
    expect(config.provider).toEqual({
      [id]: {
        options: {
          baseURL: GATEWAY_URL + entry.opencode_base_path,
          apiKey: PLACEHOLDER,
        },
        models: { "modelo-a": {} },
      },
    });
  });

  test.each([
    ["provider", { openai: {} }],
    ["enabled_providers", ["openai"]],
    ["plugin", ["opencode-oauth-plugin"]],
  ] as const)("rawConfig cannot set %s", (key, value) => {
    expect(
      () =>
        new AgentSpec({
          model: new AgentModel({
            provider: "openai",
            id: "modelo-a",
            gateway: "modelo",
          }),
          rawConfig: { [key]: value },
        }),
    ).toThrow(new RegExp(key));
  });

  test.each(NATIVE)("deepagents %s", (provider) => {
    const entry = CATALOGUE.model_providers[provider] as ProviderEntry;
    const runtime = new DeepAgents();
    const options = {
      gatewayUrls: { modelo: GATEWAY_URL },
      workdir: "/home/user",
    };
    if (entry.deepagents === "unimplemented") {
      expect(() => runtime.buildConfig(spec(provider), options)).toThrow(UnimplementedError);
      return;
    }
    const files = runtime.buildConfig(spec(provider), options);
    const config = JSON.parse(new TextDecoder().decode(files.files[0]?.data)) as Record<
      string,
      unknown
    >;
    expect(config.provider).toBe(provider);
    expect(config.base_url).toBe(GATEWAY_URL + (entry.deepagents_base_path ?? ""));
    expect(config.credential_placeholder).toBe(PLACEHOLDER);
  });
});
