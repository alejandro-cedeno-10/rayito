/**
 * Modelo opcional cuando no es Bedrock (ADR-033 D2), espejo de
 * `test_agent_model_optional.py`: cuándo hace falta `[model] id`
 * (`testdata/agent-model-optional/requirement.json`), los nombres de variable
 * de `[model]`, el cableado en MicroVMs (variables, el puente de Node sin
 * modelo, telemetría) y que un proyecto existente no cambia.
 */

import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { describe, expect, test } from "vitest";
import { CustomRuntime, MODEL_ID_ENV } from "../../src/agent/custom.js";
import { buildDeepAgentsConfig, DeepAgents } from "../../src/agent/deepagents.js";
import { AgentModel, AgentSpec, MODEL_ENV_KEYS, requiredModelId } from "../../src/agent/domain.js";
import { openaiCompatibleGateway } from "../../src/agent/gateways.js";
import { HermesRuntime } from "../../src/agent/hermes.js";
import { modelEnvNames, standardModelEnvs } from "../../src/agent/host-runtime.js";
import { buildOpenCodeConfig } from "../../src/agent/opencode.js";
import {
  AgentProject,
  AgentProjectError,
  MODEL_ID_REQUIRED_PRESETS,
  modelIdRequired,
  requireModelId,
} from "../../src/agent/project.js";
import { StrandsRuntime } from "../../src/agent/strands.js";
import { REQUEST_MODEL_ATTRIBUTE, startAttributes } from "../../src/agent/telemetry.js";
import { InvalidArgumentError } from "../../src/errors.js";
import { MODEL_CREDENTIAL_PLACEHOLDER } from "../../src/limits.js";

const REPO = join(import.meta.dirname, "..", "..", "..", "..");
const TESTDATA = join(REPO, "testdata");
const PROJECT_DIR = join(TESTDATA, "agent-project");
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
const GATEWAY_URL = "https://gw.example.com";
const LLM_GATEWAY = "llm";
const URLS = { [LLM_GATEWAY]: GATEWAY_URL };

interface RequirementCase {
  readonly name: string;
  readonly input: {
    readonly target: string;
    readonly source: string;
    readonly runtime: string;
    readonly entrypoint: boolean;
    readonly gatewayPreset: string | null;
    readonly modelId: string | null;
  };
  readonly expected: "ok" | { readonly error: { readonly code: string; readonly path: string } };
}

const REQUIREMENT = JSON.parse(
  readFileSync(join(TESTDATA, "agent-model-optional", "requirement.json"), "utf8"),
) as { readonly modelIdRequiredPresets: string[]; readonly cases: RequirementCase[] };
const MICROVM_CASES = REQUIREMENT.cases.filter((item) => item.input.target === "microvm");

type Names = Partial<Record<"idEnv" | "baseUrlEnv" | "apiKeyEnv" | "basePath", string>>;

function externalModel(id?: string, names: Names = {}): AgentModel {
  return new AgentModel({
    provider: "openai-compatible",
    gateway: LLM_GATEWAY,
    ...(id === undefined ? {} : { id }),
    ...names,
  });
}

function specOf(id?: string, names: Names = {}): AgentSpec {
  return new AgentSpec({ model: externalModel(id, names) });
}

function catchError(run: () => unknown): unknown {
  try {
    run();
  } catch (error) {
    return error;
  }
  throw new Error("no lanzó");
}

describe("cuándo hace falta el id del modelo", () => {
  test("las pasarelas que lo exigen son las que filtran por modelo", () => {
    expect([...MODEL_ID_REQUIRED_PRESETS]).toEqual(REQUIREMENT.modelIdRequiredPresets);
  });

  test.each(MICROVM_CASES.map((item) => [item.name, item] as const))("%s", (_, item) => {
    const { input, expected } = item;
    const check = () =>
      requireModelId(input.modelId ?? undefined, {
        runtime: input.runtime,
        entrypoint: input.entrypoint,
        gatewayPreset: input.gatewayPreset ?? undefined,
        detected: input.source === "detection",
      });
    if (expected === "ok") {
      check();
      return;
    }
    const error = catchError(check) as AgentProjectError;
    expect(error).toBeInstanceOf(AgentProjectError);
    expect([error.code, error.path]).toEqual([expected.error.code, expected.error.path]);
  });

  test("un id conocido nunca falta", () => {
    expect(
      modelIdRequired({ runtime: "opencode", entrypoint: false, gatewayPreset: "bedrock" }),
    ).toBe(true);
    requireModelId("gpt-5-mini", {
      runtime: "opencode",
      entrypoint: false,
      gatewayPreset: "bedrock",
    });
  });
});

describe("AgentModel sin id", () => {
  test("acepta nombres de variable y los valida", () => {
    const model = externalModel(undefined, { idEnv: "LLM_MODEL", baseUrlEnv: "LLM_BASE_URL" });
    expect(model.id).toBeUndefined();
    expect(model.envNames()).toEqual({ id_env: "LLM_MODEL", base_url_env: "LLM_BASE_URL" });
    expect(new AgentSpec({ model }).effectiveSmallModel).toBeUndefined();
    for (const field of ["idEnv", "baseUrlEnv", "apiKeyEnv"] as const) {
      expect(() => externalModel(undefined, { [field]: "1-NOT-A-NAME" })).toThrow(field);
    }
    expect(MODEL_ENV_KEYS).toEqual(["id_env", "base_url_env", "api_key_env"]);
  });

  test("requiredModelId nombra el runtime", () => {
    expect(requiredModelId(externalModel("demo-model"), "opencode")).toBe("demo-model");
    expect(() => requiredModelId(externalModel(), "hermes")).toThrow("'hermes'");
  });
});

function documentOf(model: Record<string, unknown>, env?: Record<string, string>) {
  return {
    schema: "rayito.agent-project/1",
    name: "ticket-agent",
    runtime: { kind: "langgraph", entrypoint: "agent:build_graph" },
    model: { provider: "openai-compatible", gateway: LLM_GATEWAY, ...model },
    gateways: {
      [LLM_GATEWAY]: {
        preset: "openai-compatible",
        secret: "rayito/llm-key",
        upstream: "https://llm-gateway.example.com",
      },
    },
    ...(env === undefined ? {} : { env }),
  };
}

describe("el fichero de proyecto", () => {
  test("un proyecto sin id carga y se escribe sin id", async () => {
    const project = await AgentProject.load(join(PROJECT_DIR, "valid", "model-without-id.toml"));
    expect(project.spec.model.id).toBeUndefined();
    const model = project.toObject().model as Record<string, unknown>;
    expect("id" in model).toBe(false);
    expect(model.base_url_env).toBe("LLM_BASE_URL");
  });

  test("una variable de [model] que también está en [env] es conflict", () => {
    const error = catchError(() =>
      AgentProject.fromObject(documentOf({ base_url_env: "LLM_BASE_URL" }, { LLM_BASE_URL: "x" }), {
        baseDir: ".",
      }),
    ) as AgentProjectError;
    expect([error.code, error.path]).toEqual(["conflict", "model.base_url_env"]);
  });

  test("una variable de [model] tiene que ser un nombre", () => {
    const error = catchError(() =>
      AgentProject.fromObject(documentOf({ id_env: "LLM MODEL" }), { baseDir: "." }),
    ) as AgentProjectError;
    expect([error.code, error.path]).toEqual(["invalid_value", "model.id_env"]);
  });

  test("los proyectos existentes conservan su forma canónica", async () => {
    const files = readdirSync(join(PROJECT_DIR, "valid")).filter((name) => name.endsWith(".toml"));
    for (const name of files) {
      const expected = JSON.parse(
        readFileSync(join(PROJECT_DIR, "valid", name.replace(/\.toml$/, ".expected.json")), "utf8"),
      ) as Record<string, unknown>;
      const project = await AgentProject.load(join(PROJECT_DIR, "valid", name));
      expect(project.toObject().model).toEqual(expected.model);
    }
  });
});

describe("los runtimes que construyen el modelo", () => {
  test.each([
    ["opencode", () => buildOpenCodeConfig(specOf(), { gatewayUrls: URLS })],
    [
      "deepagents",
      () =>
        buildDeepAgentsConfig(specOf(), {
          gatewayUrls: URLS,
          workdir: "/w",
          entrypoint: undefined,
        }),
    ],
    ["strands", () => new StrandsRuntime().hostSetup(specOf(), URLS, "/w")],
    ["hermes", () => new HermesRuntime().hostSetup(specOf(), URLS, "/w")],
  ] as const)("%s necesita el id", (_, build) => {
    const error = catchError(build);
    expect(error).toBeInstanceOf(InvalidArgumentError);
    expect(String(error)).toContain("construye el modelo");
  });

  const gateways = () => ({
    [LLM_GATEWAY]: openaiCompatibleGateway("llm-key", { upstream: GATEWAY_URL }),
  });

  test.each([
    ["opencode", "opencode"],
    ["hermes", "hermes"],
    ["deepagents", "deepagents"],
    ["strands", "strands"],
    ["DeepAgents()", new DeepAgents()],
    ["Strands()", new StrandsRuntime()],
    ["Hermes()", new HermesRuntime()],
  ] as const)("un AgentProject con %s necesita el id al construirlo", (_, runtime) => {
    const error = catchError(
      () => new AgentProject({ name: "no-model", runtime, spec: specOf(), gateways: gateways() }),
    );
    expect(error).toBeInstanceOf(InvalidArgumentError);
    expect(String(error)).toContain("construye el modelo");
  });

  test.each([
    ["langgraph con entrypoint", new DeepAgents({ entrypoint: "agent:graph" })],
    ["strands con entrypoint", new StrandsRuntime({ entrypoint: "agent:agent" })],
    ["langgraph por nombre", "langgraph"],
  ] as const)("un AgentProject con %s no necesita el id", (_, runtime) => {
    const project = new AgentProject({
      name: "own-model",
      runtime,
      spec: specOf(),
      gateways: gateways(),
    });
    expect(project.spec.model.id).toBeUndefined();
  });

  test("con entrypoint el modelo viaja como null", () => {
    const config = buildDeepAgentsConfig(specOf(), {
      gatewayUrls: URLS,
      workdir: "/w",
      entrypoint: "agent:graph",
    });
    expect(config.model).toBeNull();
    const setup = new StrandsRuntime({ entrypoint: "agent:agent" }).hostSetup(specOf(), URLS, "/w");
    expect(setup.config?.model).toBeNull();
  });

  test("un langgraph exportado sin id no necesita endpoint", () => {
    const config = buildDeepAgentsConfig(specOf(), {
      gatewayUrls: undefined,
      workdir: "/w",
      entrypoint: "a:g",
    });
    expect(config.base_url).toBeNull();
  });
});

describe("el cableado en MicroVMs", () => {
  test("exporta las variables nombradas con el marcador", () => {
    const spec = specOf("demo-model", {
      basePath: "/v1",
      idEnv: "LLM_MODEL",
      baseUrlEnv: "LLM_BASE_URL",
      apiKeyEnv: "LLM_API_KEY",
    });
    const envs = standardModelEnvs(spec, URLS);
    expect(envs.LLM_BASE_URL).toBe(`${GATEWAY_URL}/v1`);
    expect(envs.OPENAI_BASE_URL).toBe(`${GATEWAY_URL}/v1`);
    expect(envs.LLM_API_KEY).toBe(MODEL_CREDENTIAL_PLACEHOLDER);
    expect(envs.LLM_MODEL).toBe("demo-model");
    expect(standardModelEnvs(spec, undefined)).toEqual({ LLM_MODEL: "demo-model" });
  });

  test("sin id no se exporta la variable del id", () => {
    const spec = specOf(undefined, { idEnv: "LLM_MODEL", baseUrlEnv: "LLM_BASE_URL" });
    expect("LLM_MODEL" in modelEnvNames(spec, GATEWAY_URL)).toBe(false);
    expect(modelEnvNames(spec, undefined)).toEqual({});
    expect(modelEnvNames(specOf("demo-model"), GATEWAY_URL)).toEqual({});
  });

  test("el runtime custom omite RAYITO_MODEL_ID sin id", () => {
    const runtime = new CustomRuntime({ entrypoint: "agent:run" });
    expect(MODEL_ID_ENV in runtime.wiringEnvs(specOf(), URLS)).toBe(false);
    expect(runtime.wiringEnvs(specOf("demo-model"), URLS)[MODEL_ID_ENV]).toBe("demo-model");
  });

  test("la telemetría omite gen_ai.request.model sin id", () => {
    expect(REQUEST_MODEL_ATTRIBUTE in startAttributes(specOf(), "langgraph")).toBe(false);
    expect(startAttributes(specOf("demo-model"), "langgraph")[REQUEST_MODEL_ATTRIBUTE]).toBe(
      "demo-model",
    );
  });

  test("el puente de Node da ctx.model y ctx.modelId como undefined, nunca null", async () => {
    const bridge = (await import(pathToFileURL(BRIDGE).href)) as {
      factoryContext: (
        config: Record<string, unknown>,
        request: Record<string, unknown>,
        extra?: Record<string, unknown>,
      ) => Record<string, unknown>;
    };
    const context = bridge.factoryContext(
      { workdir: "/w" },
      { session_id: "s" },
      { model: null, modelId: null },
    );
    expect(context.model).toBeUndefined();
    expect(context.modelId).toBeUndefined();
    expect(bridge.factoryContext({}, {}, { modelId: "demo-model" }).modelId).toBe("demo-model");
  });
});
