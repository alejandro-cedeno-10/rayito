/**
 * Modelo fuera de Bedrock en AgentCore (ADR-033 D2), espejo de
 * `test_agent_model_optional_agentcore.py`: el opt-in `externalModel`
 * (`testdata/agent-model-optional/plan-vectors.json`), las filas de AgentCore
 * de `requirement.json`, `model_required`, la bandera `--external-model` y la
 * prop de CDK. Nada toca AWS.
 */

import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { App, NestedStack, Stack } from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { afterEach, describe, expect, test } from "vitest";
import { planDeploy } from "../../src/agent/deploy.js";
import { AgentDeployOptions } from "../../src/agent/deploy-plan.js";
import { AgentProject } from "../../src/agent/project.js";
import { SECURITY_MESSAGES } from "../../src/agentcore/messages.js";
import type { AgentCoreDeployPlan, AgentCoreOptions } from "../../src/agentcore/models.js";
import { RayitoAgent } from "../../src/cdk/index.js";
import { SdkAgentCoreTools } from "../../src/cli/agentcore-adapter.js";
import { planDocument } from "../../src/cli/agentcore-output.js";
import { AgentCoreError } from "../../src/errors.js";
import { loadContract } from "./cdk-contract-harness.js";
import { buildWorld } from "./cli/case-world.js";
import { runWithServices } from "./cli/run-case.js";

const TESTDATA = join(import.meta.dirname, "..", "..", "..", "..", "testdata");
const DATA = join(TESTDATA, "agent-model-optional");
const REGION = "us-east-1";
const BUCKET = "amzn-s3-demo-bucket";
const UNCOMPARED_KEYS = ["imageTag", "cost"];
const CONTRACT = JSON.parse(
  readFileSync(join(TESTDATA, "agentcore-deploy", "contract.json"), "utf8"),
) as {
  messages: { security: Record<string, string>; errors: Record<string, string> };
  errorExitCodes: Record<string, number>;
};

interface RawAgentCore {
  readonly bedrockModels?: string[];
  readonly readableSecrets?: string[];
  readonly externalModel?: boolean;
}

interface PlanCase {
  readonly name: string;
  readonly project: Record<string, unknown>;
  readonly call: { readonly agentcore: RawAgentCore };
  readonly environ: Record<string, string>;
  readonly expected: Record<string, unknown> & { readonly error?: Record<string, unknown> };
}

interface RequirementCase {
  readonly name: string;
  readonly input: RawAgentCore & {
    readonly target: string;
    readonly runtime: string;
    readonly provider: string;
    readonly modelId: string | null;
  };
  readonly expected: "ok" | { readonly error: { readonly code: string } };
}

const PLAN_CASES = (
  JSON.parse(readFileSync(join(DATA, "plan-vectors.json"), "utf8")) as { cases: PlanCase[] }
).cases;
const AGENTCORE_CASES = (
  JSON.parse(readFileSync(join(DATA, "requirement.json"), "utf8")) as {
    cases: RequirementCase[];
  }
).cases.filter((item) => item.input.target === "agentcore");
const NO_AWS = new SdkAgentCoreTools(async () => {
  throw new Error("la CLI no debía construir la sesión de AWS");
});
const scratches: string[] = [];

afterEach(() => {
  for (const directory of scratches.splice(0)) {
    rmSync(directory, { recursive: true, force: true });
  }
});

function agentcoreOf(raw: RawAgentCore): AgentCoreOptions {
  return {
    bedrockModels: raw.bedrockModels ?? [],
    readableSecrets: raw.readableSecrets ?? [],
    externalModel: raw.externalModel ?? false,
  };
}

async function planOf(
  project: AgentProject,
  agentcore: AgentCoreOptions,
): Promise<AgentCoreDeployPlan> {
  const saved = process.env.RAYITO_BUCKET;
  process.env.RAYITO_BUCKET = BUCKET;
  try {
    return await planDeploy(".", {
      project,
      region: REGION,
      options: new AgentDeployOptions({}),
      target: "agentcore",
      agentcore,
    });
  } finally {
    if (saved === undefined) {
      delete process.env.RAYITO_BUCKET;
    } else {
      process.env.RAYITO_BUCKET = saved;
    }
  }
}

async function failureOf(run: () => Promise<unknown>): Promise<AgentCoreError> {
  const thrown = await run().then(
    () => undefined,
    (error: unknown) => error,
  );
  expect(thrown).toBeInstanceOf(AgentCoreError);
  return thrown as AgentCoreError;
}

describe("plan con un modelo fuera de Bedrock", () => {
  test.each(PLAN_CASES.map((item) => [item.name, item] as const))("%s", async (_, item) => {
    const project = AgentProject.fromObject(item.project, { baseDir: DATA });
    const options = agentcoreOf(item.call.agentcore);
    const { error } = item.expected;
    if (error !== undefined) {
      const raised = await failureOf(() => planOf(project, options));
      expect(raised.code).toBe(error.code);
      return;
    }
    const document = planDocument(await planOf(project, options));
    for (const key of UNCOMPARED_KEYS) {
      delete document[key];
    }
    expect(document).toEqual(item.expected);
  });
});

function requirementProject(input: RequirementCase["input"]): AgentProject {
  const model: Record<string, unknown> = { provider: input.provider, gateway: "llm" };
  if (input.provider === "bedrock") {
    model.region = REGION;
  }
  if (input.modelId !== null) {
    model.id = input.modelId;
  }
  return AgentProject.fromObject(
    {
      schema: "rayito.agent-project/1",
      name: "ticket-agent",
      runtime: { kind: input.runtime, entrypoint: "agent:build_graph" },
      model,
      gateways: {
        llm: {
          preset: "custom",
          upstream: "https://llm-gateway.example.com",
          headers: { authorization: "rayito/llm-key" },
          allow: [["POST", "/v1/chat/completions"]],
        },
      },
    },
    { baseDir: DATA },
  );
}

describe("cuándo hace falta el id en AgentCore", () => {
  test.each(AGENTCORE_CASES.map((item) => [item.name, item] as const))("%s", async (_, item) => {
    const project = requirementProject(item.input);
    const options = agentcoreOf(item.input);
    if (item.expected === "ok") {
      const plan = await planOf(project, options);
      expect(plan.externalModel).toBe(item.input.externalModel === true);
      return;
    }
    const raised = await failureOf(() => planOf(project, options));
    expect(raised.code).toBe(item.expected.error.code);
  });

  test("model_required conserva su código de salida y el texto del contrato", () => {
    const error = new AgentCoreError("model_required");
    expect(error.exitCode).toBe(CONTRACT.errorExitCodes.model_required);
    expect(error.message).toBe(CONTRACT.messages.errors.model_required);
  });

  test("la línea externalModel es la del contrato", () => {
    expect(SECURITY_MESSAGES.externalModel).toBe(CONTRACT.messages.security.externalModel);
    expect(Object.keys(SECURITY_MESSAGES)).toEqual(Object.keys(CONTRACT.messages.security));
  });

  test("la clave nunca llega al entorno del runtime", async () => {
    const item = PLAN_CASES[0] as PlanCase;
    const project = AgentProject.fromObject(item.project, { baseDir: DATA });
    const plan = await planOf(project, { externalModel: true });
    const keyName = project.spec.model.apiKeyEnv as string;
    expect(keyName in plan.environment).toBe(false);
    expect(JSON.stringify(plan.environment)).not.toContain("rayito/agents/ticket-agent/llm");
  });
});

function projectDirectory(): string {
  const directory = mkdtempSync(join(tmpdir(), "rayito-external-"));
  scratches.push(directory);
  writeFileSync(join(directory, "rayito-agent.json"), JSON.stringify(PLAN_CASES[0]?.project));
  writeFileSync(join(directory, "agent.py"), "def build_graph(ctx):\n    return None\n");
  return directory;
}

async function deploy(...extra: string[]) {
  return runWithServices({
    args: ["--json", "agent", "deploy", projectDirectory(), "--dry-run", ...extra],
    services: buildWorld({}).services,
    agentcore: NO_AWS,
    env: { AWS_REGION: REGION, RAYITO_BUCKET: BUCKET },
  });
}

describe("rayito agent deploy --external-model", () => {
  test("con la bandera el plan no da permisos de Bedrock", async () => {
    const outcome = await deploy("--target", "agentcore", "--external-model");
    expect(outcome.exitCode, outcome.stderr).toBe(0);
    const plan = (JSON.parse(outcome.stdout) as { plan: Record<string, unknown> }).plan;
    expect(plan.externalModel).toBe(true);
    expect(plan.bedrockModels).toEqual([]);
    expect(plan.security).toContain("externalModel");
  });

  test("sin la bandera es provider_unsupported", async () => {
    const outcome = await deploy("--target", "agentcore");
    expect(outcome.exitCode).toBe(CONTRACT.errorExitCodes.provider_unsupported);
    expect(outcome.stderr).toContain("openai-compatible");
  });

  test("sin --target agentcore es option_requires_agentcore", async () => {
    const outcome = await deploy("--external-model");
    expect(outcome.exitCode).toBe(CONTRACT.errorExitCodes.option_requires_agentcore);
    expect(outcome.stderr).toContain("--external-model sólo aplica a --target agentcore");
  });
});

describe("RayitoAgent con externalModel", () => {
  test("no concede Bedrock y pone la URL de la pasarela en el entorno", () => {
    const app = new App({
      context: JSON.parse(loadContract().caseFormat.appContext) as Record<string, unknown>,
    });
    const stack = new Stack(app, "AgentTest", { env: { account: "123456789012", region: REGION } });
    new RayitoAgent(stack, "Agent", { project: projectDirectory(), externalModel: true });
    const nested = stack.node
      .findAll()
      .find((child) => child instanceof NestedStack) as NestedStack;
    const conditions = Template.fromStack(nested).toJSON().Conditions as Record<string, unknown>;
    expect(conditions.HasBedrockModels).toEqual({
      "Fn::Not": [{ "Fn::Equals": [{ "Fn::Join": ["", []] }, ""] }],
    });
    const resources = Template.fromStack(stack).toJSON().Resources as Record<
      string,
      { Type: string; Properties: Record<string, unknown> }
    >;
    const runtime = Object.values(resources).find(
      (resource) => resource.Type === "AWS::BedrockAgentCore::Runtime",
    );
    const environment = runtime?.Properties.EnvironmentVariables as Record<string, string>;
    expect(environment.LLM_BASE_URL).toBe("https://llm-gateway.example.com/v1");
    expect("LLM_API_KEY" in environment).toBe(false);
  });
});
