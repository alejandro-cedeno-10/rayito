/**
 * `secretEnv` al desplegar (ADR-033 D4) sobre dobles: en AgentCore el plan
 * lleva `RAYITO_SECRET_ENV` y los secretos unidos a `ReadableSecretArns`, el
 * export añade la herramienta y la clave de `host.json` sólo con la función
 * (igual que Python, byte a byte), `createSecretsFrom` crea el secreto antes
 * de aplicar y `checkEnv` avisa por `onStep`; en MicroVMs el SDK lee los
 * valores y el host los recibe sólo en el entorno de su proceso. Ningún
 * valor sale en un plan, un paso, un fichero ni un error. Nada toca AWS.
 */

import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, test } from "vitest";
import { planDeploy } from "../../src/agent/deploy.js";
import type { AgentDeployStep } from "../../src/agent/deploy-plan.js";
import { DOCKER_SECRET_ENV_WARNING, renderExport } from "../../src/agent/export.js";
import { AgentProject, AgentProjectError } from "../../src/agent/project.js";
import { ProjectService } from "../../src/agent/project-service.js";
import {
  envFileSecretName,
  prepareSecretEnv,
  publishEnvFileSecret,
  readEnvFileSecret,
} from "../../src/agent/secret-env-deploy.js";
import { checkRuntimeCollisions } from "../../src/agent/secret-env-project.js";
import { secretResourceArns } from "../../src/agentcore/arns.js";
import { deployAgentCoreWith } from "../../src/agentcore/facade.js";
import type { AgentCoreDeployPlan } from "../../src/agentcore/models.js";
import { planLines } from "../../src/cli/agentcore-output.js";
import { AgentError, SecretEnvRef, SecretEnvSource } from "../../src/index.js";
import { AGENT_SECRET_ENV_NAMES_VARIABLE, AGENT_SECRET_ENV_VARIABLE } from "../../src/limits.js";
import { FakeSandbox, FakeSandboxFactory, project, SANDBOX_ID } from "./agent-project-fake.js";
import { ACCOUNT, BUCKET, clockOf, newAccount, REGION } from "./agentcore-fakes.js";

const REPO = join(import.meta.dirname, "..", "..", "..", "..");
const SAMPLE = join(REPO, "testdata", "agent-secret-env", "secret-env-echo");
const DATABASE_URL = "postgres://db.internal.example/app";
const TOKEN_VALUE = "tk-value";
const SECRET_ENV = {
  DATABASE_URL: "app/db-url",
  LLM_GATEWAY_KEY: new SecretEnvRef({
    secret: "arn:aws:secretsmanager:us-east-1:123456789012:secret:llm-AbCdEf",
    key: "api_key",
  }),
};
const SOURCES = [new SecretEnvSource({ secret: "app/config", prefix: "APP_", keys: ["DB_HOST"] })];
const EXPECTED_ARNS = [
  "arn:aws:secretsmanager:us-east-1:123456789012:secret:app/db-url-??????",
  "arn:aws:secretsmanager:us-east-1:123456789012:secret:llm-AbCdEf",
  "arn:aws:secretsmanager:us-east-1:123456789012:secret:app/config-??????",
];

let directory: string;

beforeEach(async () => {
  directory = await mkdtemp(join(tmpdir(), "rayito-secret-env-"));
});

afterEach(async () => {
  await rm(directory, { recursive: true, force: true });
});

function secretProject(fields: Record<string, unknown> = {}): AgentProject {
  return project({ secretEnv: SECRET_ENV, secretEnvFrom: SOURCES, ...fields });
}

function text(tree: Map<string, string | Uint8Array>, name: string): string {
  const value = tree.get(name);
  if (value === undefined) {
    throw new Error(`falta ${name}`);
  }
  return typeof value === "string" ? value : new TextDecoder().decode(value);
}

class Store {
  readonly calls: (readonly [string, string])[] = [];
  async exists(): Promise<boolean> {
    return false;
  }
  async create(name: string): Promise<void> {
    this.calls.push(["create", name]);
  }
  async update(name: string): Promise<void> {
    this.calls.push(["update", name]);
  }
}

describe("AgentCore", () => {
  test("the plan carries the spec and unions the secrets", async () => {
    const plan = (await planDeploy(".", {
      project: secretProject(),
      region: REGION,
      target: "agentcore",
      agentcore: { readableSecrets: ["app/db-url"] },
    })) as AgentCoreDeployPlan;
    expect(JSON.parse(plan.environment[AGENT_SECRET_ENV_VARIABLE] ?? "")).toEqual({
      env: {
        DATABASE_URL: { secret: "app/db-url" },
        LLM_GATEWAY_KEY: {
          key: "api_key",
          secret: "arn:aws:secretsmanager:us-east-1:123456789012:secret:llm-AbCdEf",
        },
      },
      from: [{ keys: ["DB_HOST"], prefix: "APP_", secret: "app/config" }],
      v: 1,
    });
    expect(plan.readableSecrets).toEqual([
      "app/db-url",
      "arn:aws:secretsmanager:us-east-1:123456789012:secret:llm-AbCdEf",
      "app/config",
    ]);
    expect(secretResourceArns(plan.readableSecrets, { region: REGION, account: ACCOUNT })).toEqual(
      EXPECTED_ARNS,
    );
    expect(plan.security).toContain("secretEnv");
    expect(plan.secretEnvNames).toEqual(["APP_DB_HOST", "DATABASE_URL", "LLM_GATEWAY_KEY"]);
    expect(planLines(plan).join("\n")).toContain("APP_DB_HOST, DATABASE_URL, LLM_GATEWAY_KEY");
  });

  test("without secret env the plan is the same as before", async () => {
    const plan = (await planDeploy(".", {
      project: project(),
      region: REGION,
      target: "agentcore",
    })) as AgentCoreDeployPlan;
    expect(plan.environment).not.toHaveProperty(AGENT_SECRET_ENV_VARIABLE);
    expect(plan.security).not.toContain("secretEnv");
    expect(plan).not.toHaveProperty("secretEnvNames");
  });

  test("the export adds the helper and the host key only with secret env", async () => {
    const tree = await renderExport(secretProject(), "agentcore");
    const plain = await renderExport(project(), "agentcore");
    expect(tree.has("aws_secret_env.py")).toBe(true);
    expect(plain.has("aws_secret_env.py")).toBe(false);
    const host = JSON.parse(text(tree, "host.json"));
    expect(Object.keys(host.secret_env).sort()).toEqual(["python", "reserved", "script"]);
    expect(JSON.parse(text(plain, "host.json"))).not.toHaveProperty("secret_env");
    const dockerfile = text(tree, "Dockerfile");
    expect(dockerfile).toContain("COPY aws_secret_env.py /opt/agents/rayito/aws_secret_env.py");
    expect(dockerfile).not.toContain("aws_env_credentials.py");
  });

  test("the docker export ignores secret env and says so in the README", async () => {
    const tree = await renderExport(secretProject(), "docker");
    expect(tree.has("aws_secret_env.py")).toBe(false);
    expect(text(tree, "README.md")).toContain(DOCKER_SECRET_ENV_WARNING);
  });

  test("the sample host config is the one Python writes", async () => {
    const sample = await AgentProject.load(SAMPLE);
    const expected = JSON.parse(await readFile(join(SAMPLE, "expected", "project.json"), "utf8"));
    expect(sample.toObject()).toEqual(expected);
    const tree = await renderExport(sample, "agentcore");
    expect(text(tree, "host.json")).toBe(
      await readFile(join(SAMPLE, "expected", "agentcore-host.json"), "utf8"),
    );
  });

  test("a variable the runtime already sets is refused", () => {
    expect(() => checkRuntimeCollisions(secretProject(), { DATABASE_URL: "x" })).toThrow(
      AgentProjectError,
    );
    checkRuntimeCollisions(secretProject(), { OTHER: "x" });
  });

  test("deploying creates the env file secret and reports names only", async () => {
    const source = join(directory, "agent");
    await mkdir(source);
    await writeFile(join(source, ".env.example"), "TICKETS_API_URL=\nTICKETS_TOKEN=\nLOG_LEVEL=\n");
    const envFile = join(directory, "values.env");
    await writeFile(
      envFile,
      `TICKETS_API_URL=https://tickets.example.com\nTICKETS_TOKEN=${TOKEN_VALUE}\n`,
    );
    const account = newAccount();
    const store = new Store();
    const steps: AgentDeployStep[] = [];
    await deployAgentCoreWith(
      {
        aws: account.ports,
        environ: { RAYITO_BUCKET: BUCKET },
        settings: clockOf(account),
        secretEnvStore: store,
      },
      ".",
      {
        project: project({ source }),
        region: REGION,
        target: "agentcore",
        options: { onStep: (step) => steps.push(step), createSecretsFrom: envFile, checkEnv: true },
      },
    );
    expect(store.calls).toEqual([["create", "rayito/agents/support-bot/env"]]);
    const secretSteps = steps.filter((step) =>
      ["env_missing", "secret_env_created"].includes(step.kind),
    );
    expect(secretSteps[0]?.kind).toBe("env_missing");
    expect(secretSteps[0]?.detail.startsWith("LOG_LEVEL ")).toBe(true);
    expect(secretSteps.at(-1)).toEqual({ kind: "secret_env_created", detail: "created" });
    expect(account.stacks.state.parameters.ReadableSecretArns).toContain(
      "secret:rayito/agents/support-bot/env-??????",
    );
    const created = account.calls.find((call) => call.operation === "CreateAgentRuntime");
    if (created === undefined) {
      throw new Error("no se creó el runtime");
    }
    const environment = (created.input as { environmentVariables: Record<string, string> })
      .environmentVariables;
    expect(environment[AGENT_SECRET_ENV_VARIABLE]).toContain("rayito/agents/support-bot/env");
    expect(JSON.stringify([steps, account.calls])).not.toContain(TOKEN_VALUE);
  });
});

describe("--create-secrets-from", () => {
  test("the env file secret holds names in the clear and hides the value", async () => {
    const path = join(directory, ".env");
    await writeFile(
      path,
      `TICKETS_API_URL=https://tickets.example.com\nTICKETS_TOKEN=${TOKEN_VALUE}\n`,
    );
    const secret = readEnvFileSecret("support-bot", path);
    expect(secret.name).toBe(envFileSecretName("support-bot"));
    expect(secret.variables).toEqual(["TICKETS_API_URL", "TICKETS_TOKEN"]);
    expect(JSON.parse(secret.value).TICKETS_TOKEN).toBe(TOKEN_VALUE);
    expect(JSON.stringify(secret)).not.toContain(TOKEN_VALUE);
    const updates: string[] = [];
    expect(
      await publishEnvFileSecret(
        {
          exists: async () => true,
          create: async () => undefined,
          update: async (name) => {
            updates.push(name);
          },
        },
        secret,
      ),
    ).toBe("updated");
    expect(updates).toEqual([secret.name]);
  });

  test("a reserved name in the env file is named without its value", async () => {
    const path = join(directory, "local.env");
    await writeFile(path, `TICKETS_TOKEN=${TOKEN_VALUE}\nAWS_PROFILE=dev-profile-value\n`);
    expect(() => readEnvFileSecret("support-bot", path)).toThrowError(/AWS_PROFILE/);
    expect(() => readEnvFileSecret("support-bot", path)).not.toThrowError(/dev-profile-value/);
  });

  test("an env file inside the source is excluded from it", async () => {
    const source = join(directory, "agent");
    await mkdir(join(source, "deploy"), { recursive: true });
    const inside = join(source, "deploy", "prod.env");
    const outside = join(directory, "values.env");
    for (const path of [inside, outside]) {
      await writeFile(path, `TICKETS_TOKEN=${TOKEN_VALUE}\n`);
    }
    const theProject = project({ source, exclude: ["tests"] });
    expect(prepareSecretEnv(theProject, inside, false).project.exclude).toEqual([
      "tests",
      "deploy/prod.env",
    ]);
    expect(prepareSecretEnv(theProject, outside, false).project.exclude).toEqual(["tests"]);
  });
});

describe("MicroVMs", () => {
  function service(the: AgentProject, reads: (readonly [string, string | undefined])[]) {
    const sandbox = new FakeSandbox({ readyAfter: 0 });
    const values: Record<string, string> = {
      "app/db-url": DATABASE_URL,
      "arn:aws:secretsmanager:us-east-1:123456789012:secret:llm-AbCdEf": JSON.stringify({
        api_key: TOKEN_VALUE,
      }),
      "app/config": JSON.stringify({ DB_HOST: "db.internal.example" }),
    };
    return {
      sandbox,
      service: new ProjectService(the, {
        sandboxFactory: new FakeSandboxFactory(sandbox),
        now: () => 0,
        sleep: async () => undefined,
        secretReader: async (secret, stage) => {
          reads.push([secret, stage]);
          const value = values[secret];
          if (value === undefined) {
            throw new Error("secreto desconocido");
          }
          return value;
        },
      }),
    };
  }

  test("the host process receives the values and no file holds them", async () => {
    const reads: (readonly [string, string | undefined])[] = [];
    const { sandbox, service: the } = service(secretProject(), reads);
    const deployment = await the.deploy();
    expect(deployment.sandboxId).toBe(SANDBOX_ID);
    const serve = sandbox.commandCalls.at(-1);
    const envs = serve?.options.envs as Record<string, string>;
    expect(envs.DATABASE_URL).toBe(DATABASE_URL);
    expect(envs.LLM_GATEWAY_KEY).toBe(TOKEN_VALUE);
    expect(envs.APP_DB_HOST).toBe("db.internal.example");
    expect(envs).not.toHaveProperty(AGENT_SECRET_ENV_VARIABLE);
    expect(envs[AGENT_SECRET_ENV_NAMES_VARIABLE]).toBe("APP_DB_HOST,DATABASE_URL,LLM_GATEWAY_KEY");
    expect(reads.map(([secret]) => secret)).toEqual([
      "app/db-url",
      "arn:aws:secretsmanager:us-east-1:123456789012:secret:llm-AbCdEf",
      "app/config",
    ]);
    const written = sandbox.written.map((file) => new TextDecoder().decode(file.data)).join("\n");
    expect(written).not.toContain(DATABASE_URL);
    expect(written).not.toContain(TOKEN_VALUE);
  });

  test("without secret env nothing is read", async () => {
    const reads: (readonly [string, string | undefined])[] = [];
    const { service: the } = service(project(), reads);
    await the.deploy();
    expect(reads).toEqual([]);
  });

  test("a bad secret fails before the host starts and names nothing", async () => {
    const reads: (readonly [string, string | undefined])[] = [];
    const broken = project({
      secretEnvFrom: [new SecretEnvSource({ secret: "app/db-url" })],
    });
    const { sandbox, service: the } = service(broken, reads);
    const error = await the.deploy().then(
      () => undefined,
      (thrown: unknown) => thrown as AgentError,
    );
    expect(error).toBeInstanceOf(AgentError);
    expect(error?.detailCode).toBe("secret_env_invalid");
    expect(error?.message).not.toContain("app/db-url");
    expect(sandbox.commandCalls).toEqual([]);
    expect(sandbox.kills).toBe(1);
  });
});
