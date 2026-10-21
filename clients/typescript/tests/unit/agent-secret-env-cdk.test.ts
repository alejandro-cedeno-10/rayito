/**
 * `RayitoAgent` con un proyecto que usa `secretEnv` (ADR-033 D4): el runtime
 * lleva `RAYITO_SECRET_ENV` (sólo nombres y ARNs) y la pila anidada pasa los
 * secretos referenciados en `ReadableSecretArns`, sin ninguna sentencia nueva
 * de IAM. Sintetizado sin AWS, como el test de Python.
 */

import { cpSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { App, NestedStack, Stack } from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { afterEach, describe, expect, it } from "vitest";
import { AgentProject } from "../../src/agent/project.js";
import { RayitoAgent } from "../../src/cdk/index.js";
import { AGENT_SECRET_ENV_VARIABLE } from "../../src/limits.js";
import { loadContract, TESTDATA_ROOT } from "./cdk-contract-harness.js";

const ACCOUNT = "123456789012";
const REGION = "us-east-1";
const STRANDS = join(TESTDATA_ROOT, "agent-samples", "python", "strands");
const HAIKU = "us.anthropic.claude-haiku-4-5-20251001-v1:0";
const SECRET_ENV = { DATABASE_URL: "app/db-url" };
const SECRET_ENV_FROM = [{ secret: "app/config", prefix: "APP_", keys: ["DB_HOST"] }];
const scratches: string[] = [];

afterEach(() => {
  for (const directory of scratches.splice(0)) {
    rmSync(directory, { recursive: true, force: true });
  }
});

async function projectWithSecretEnv(): Promise<string> {
  const directory = join(mkdtempSync(join(tmpdir(), "rayito-cdk-secret-env-")), "strands");
  scratches.push(directory);
  cpSync(STRANDS, directory, { recursive: true });
  const [detected] = await AgentProject.detect(directory, undefined, { region: REGION });
  const document = {
    ...detected.toObject(),
    secret_env: SECRET_ENV,
    secret_env_from: SECRET_ENV_FROM,
  };
  writeFileSync(join(directory, "rayito-agent.json"), JSON.stringify(document));
  return directory;
}

function synth(project: string) {
  const app = new App({
    context: JSON.parse(loadContract().caseFormat.appContext) as Record<string, unknown>,
  });
  const stack = new Stack(app, "AgentTest", { env: { account: ACCOUNT, region: REGION } });
  new RayitoAgent(stack, "Agent", { project, bedrockModels: [HAIKU] });
  const nested = stack.node.findAll().find((child) => child instanceof NestedStack) as NestedStack;
  return { root: Template.fromStack(stack).toJSON(), nested: Template.fromStack(nested).toJSON() };
}

function runtimeEnvironment(root: Record<string, unknown>): Record<string, string> {
  const resources = root.Resources as Record<string, { Type: string; Properties: never }>;
  const runtime = Object.values(resources).find(
    (resource) => resource.Type === "AWS::BedrockAgentCore::Runtime",
  ) as { Properties: { EnvironmentVariables: Record<string, string> } };
  return runtime.Properties.EnvironmentVariables;
}

describe("RayitoAgent con secretEnv", () => {
  it("lleva la especificación y los secretos legibles", async () => {
    const { root, nested } = synth(await projectWithSecretEnv());
    const spec = JSON.parse(runtimeEnvironment(root)[AGENT_SECRET_ENV_VARIABLE] ?? "");
    expect(spec.env).toEqual({ DATABASE_URL: { secret: "app/db-url" } });
    expect(spec.from).toEqual(SECRET_ENV_FROM);
    const rendered = JSON.stringify(
      (nested.Resources as Record<string, { Properties?: unknown }>).RuntimeRole?.Properties,
    );
    expect(rendered).toContain("secret:app/db-url-??????");
    expect(rendered).toContain("secret:app/config-??????");
    expect(rendered).not.toContain("secretsmanager:*");
    expect(rendered).not.toContain("kms:");
  }, 60_000);

  it("sin secretEnv el entorno del runtime no cambia", () => {
    const { root } = synth(STRANDS);
    expect(runtimeEnvironment(root)).not.toHaveProperty(AGENT_SECRET_ENV_VARIABLE);
  }, 60_000);
});
