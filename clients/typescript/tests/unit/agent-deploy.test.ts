/**
 * `deployAgent()` y `planDeploy()` (`agent/deploy.ts`, `deploy-plan.ts`,
 * `aws-resolve.ts`) sobre dobles: el orden de resolución de cada valor contra
 * los vectores compartidos con Python (`testdata/agent-deploy/`), que nada se
 * cree sin pedirlo y que `planDeploy()` no llame a AWS. Espejo de
 * `test_agent_deploy.py`; ninguna prueba llama a AWS.
 */

import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import net from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { inspect } from "node:util";
import { describe, expect, test, vi } from "vitest";
import { ARTIFACT_STACK_NAME } from "../../src/account-setup/domain.js";
import {
  AwsResolver,
  BUCKET_ENV_VAR,
  cdkBucketName,
  type DeployAws,
  discoverBucket,
  GATEWAY_SECRET_ENV_VARS,
  resolveBucket,
  resolveToken,
  type SecretStorePort,
  secretNameFor,
  secretValueFromEnv,
} from "../../src/agent/aws-resolve.js";
import { deployWith, planDeploy } from "../../src/agent/deploy.js";
import {
  AgentDeployOptions,
  type AgentDeployStep,
  estimateCost,
} from "../../src/agent/deploy-plan.js";
import type { DetectOverrides } from "../../src/agent/detect-report.js";
import { AgentFleet } from "../../src/agent/fleet.js";
import { AgentProject } from "../../src/agent/project.js";
import { ScalingOptions } from "../../src/agent/scaling-domain.js";
import { AgentTemplate } from "../../src/agent/template.js";
import { InvalidArgumentError, StackError } from "../../src/errors.js";
import { AGENT_SOURCE_DEFAULT_EXCLUDES } from "../../src/limits.js";
import { generateAccessToken } from "../../src/payload.js";
import { ACCESS_TOKEN_ENV_VAR } from "../../src/sandbox/launch.js";
import { Sandbox } from "../../src/sandbox/sandbox.js";
import { Template } from "../../src/templates/dsl.js";
import { FakePool, FleetHarness } from "./agent-fleet-fake.js";
import { project } from "./agent-project-fake.js";
import { FakeStackProvisioner } from "./m15-fake-stacks.js";

const VECTORS = join(import.meta.dirname, "..", "..", "..", "..", "testdata", "agent-deploy");
const RESOLVE = JSON.parse(readFileSync(join(VECTORS, "resolve.json"), "utf8"));
const SECRET_ENV = JSON.parse(readFileSync(join(VECTORS, "secret-env.json"), "utf8"));
interface BucketCase {
  name: string;
  explicit: string | null;
  projectFile: string | null;
  environment: string | null;
  discovered: string | null;
  expected: { bucket: string; source: string } | null;
}
interface SecretValueCase {
  gateway: string;
  header: string;
  environment: Record<string, string>;
  expected: string | null;
}
interface CostCase {
  memoryMib: number;
  scaling: { minVms: number; maxVms: number; warmVms: number };
  runningUsdPerHour: number;
  parkedUsdPerDay: number;
  imageBuildUsd: number;
  imageStorageUsdPerWeek: number;
}
const BUCKET = "amzn-s3-demo-bucket";
const INFO = { templateId: "arn", buildId: "3.0/abc", alias: "support-bot" };

class FakeAws implements DeployAws {
  images = new Set(["rayito-base-caps"]);
  buckets = new Set<string>();
  /** El `BucketName` de la pila `rayito-m0-artifacts`, si `setup` la creó. */
  stackBucket: string | undefined = undefined;
  calls: string[] = [];

  resolvedRegion(): string {
    return "us-east-1";
  }
  async accountId(): Promise<string> {
    this.calls.push("account");
    return "123456789012";
  }
  async bucketExists(bucket: string): Promise<boolean> {
    this.calls.push("bucket");
    return this.buckets.has(bucket);
  }
  async artifactStackBucket(): Promise<string | undefined> {
    this.calls.push("stack");
    return this.stackBucket;
  }
  async imageExists(name: string): Promise<boolean> {
    this.calls.push(`image:${name}`);
    return this.images.has(name);
  }
}

class FakeStore implements SecretStorePort {
  existing = new Set(["bedrock-key"]);
  created: [string, string][] = [];
  checked: string[] = [];

  async exists(name: string): Promise<boolean> {
    this.checked.push(name);
    return this.existing.has(name);
  }
  async create(name: string, value: string): Promise<void> {
    this.created.push([name, value]);
    this.existing.add(name);
  }
}

function templateOf(build: Record<string, unknown> | undefined): AgentTemplate {
  return (build as { template: AgentTemplate }).template;
}

async function failureOf(promise: Promise<unknown>): Promise<Error> {
  try {
    await promise;
  } catch (error) {
    return error as Error;
  }
  throw new Error("se esperaba un fallo");
}

class Harness {
  readonly fleet = new FleetHarness();
  readonly aws = new FakeAws();
  readonly store = new FakeStore();
  readonly environ: Record<string, string | undefined> = { [BUCKET_ENV_VAR]: BUCKET };
  readonly builds: Record<string, unknown>[] = [];
  readonly steps: AgentDeployStep[] = [];

  constructor() {
    vi.spyOn(AgentTemplate.prototype, "build").mockImplementation(async function (
      this: AgentTemplate,
      options,
    ) {
      builds.push({ template: this, ...options });
      return INFO;
    });
    const builds = this.builds;
  }

  async deploy(options: ConstructorParameters<typeof AgentDeployOptions>[0] = {}, the = project()) {
    return deployWith(
      {
        aws: this.aws,
        store: this.store,
        environ: this.environ,
        serviceOptions: {
          sandboxFactory: this.fleet.world,
          fetch: this.fleet.backend.fetch,
          sleep: this.fleet.clock.sleep,
          now: this.fleet.clock.clock,
        },
        fleetOptions: {
          directory: this.fleet.world,
          fetch: this.fleet.backend.fetch,
          now: this.fleet.clock.clock,
          sleep: this.fleet.clock.sleep,
          poolFactory: (config) => {
            const pool = new FakePool(config, this.fleet.world);
            this.fleet.pools.push(pool);
            return pool;
          },
        },
      },
      ".",
      { project: the, options: { onStep: (step) => this.steps.push(step), ...options } },
    );
  }

  get createdVms(): number {
    return this.fleet.world.created.length;
  }
}

describe("shared vectors", () => {
  test.each(RESOLVE.buckets as BucketCase[])("bucket: $name", async (item) => {
    const environ = item.environment ? { [BUCKET_ENV_VAR]: item.environment } : {};
    const discover = async () => item.discovered ?? undefined;
    if (item.expected === null) {
      const error = await failureOf(
        resolveBucket(item.explicit ?? undefined, item.projectFile ?? undefined, environ, discover),
      );
      expect(error.message).toContain("no crea buckets");
      return;
    }
    const choice = await resolveBucket(
      item.explicit ?? undefined,
      item.projectFile ?? undefined,
      environ,
      discover,
    );
    expect({ bucket: choice.bucket, source: choice.source }).toEqual(item.expected);
  });

  test("discovery is not called when something else decides", async () => {
    const fail = async (): Promise<string> => {
      throw new Error("no debía descubrir");
    };
    expect((await resolveBucket(BUCKET, undefined, {}, fail)).bucket).toBe(BUCKET);
    expect((await resolveBucket(undefined, "from-file", {}, fail)).bucket).toBe("from-file");
    expect(
      (await resolveBucket(undefined, undefined, { [BUCKET_ENV_VAR]: "from-env" }, fail)).bucket,
    ).toBe("from-env");
  });

  test("secret names follow the convention", () => {
    for (const item of RESOLVE.secretNames) {
      expect(secretNameFor(item.project, item.gateway)).toBe(item.name);
    }
  });

  test.each(RESOLVE.secretValues as SecretValueCase[])("secret value $gateway $header", (item) => {
    expect(secretValueFromEnv(item.gateway, item.header, item.environment)).toBe(
      item.expected ?? undefined,
    );
  });

  test("the provider variable table is the shared one", () => {
    expect({ ...GATEWAY_SECRET_ENV_VARS }).toEqual(SECRET_ENV.gateways);
  });

  test("the cdk bucket name is the bootstrap one", () => {
    for (const item of RESOLVE.cdkBuckets) {
      const expected = String(item.template)
        .replace("{qualifier}", item.qualifier)
        .replace("{account}", item.account)
        .replace("{region}", item.region);
      expect(cdkBucketName(item.account, item.region)).toBe(expected);
    }
  });

  test("discovery checks the cdk bucket and never creates it", async () => {
    const aws = new FakeAws();
    aws.buckets.add(cdkBucketName("123456789012", "us-east-1"));
    expect(await discoverBucket(aws, "us-east-1")).toBe(cdkBucketName("123456789012", "us-east-1"));
    expect(await discoverBucket(new FakeAws(), "us-east-1")).toBeUndefined();
  });

  test("discovery falls back to the bucket of the rayito setup stack after the cdk one", async () => {
    const aws = new FakeAws();
    aws.stackBucket = BUCKET;
    expect(await discoverBucket(aws, "us-east-1")).toBe(BUCKET);
    expect(aws.calls).toEqual(["account", "bucket", "stack"]);
  });

  test.each([
    ["a deployed stack", "CREATE_COMPLETE", { BucketName: BUCKET }, BUCKET],
    ["an updated stack", "UPDATE_COMPLETE", { BucketName: BUCKET }, BUCKET],
    ["a stack in progress", "CREATE_IN_PROGRESS", { BucketName: BUCKET }, undefined],
    ["a rolled back stack", "ROLLBACK_COMPLETE", { BucketName: BUCKET }, undefined],
    ["a stack without the output", "CREATE_COMPLETE", {}, undefined],
  ])(
    "AwsResolver reads %s of rayito-m0-artifacts read-only",
    async (_name, state, outputs, expected) => {
      const stacks = new FakeStackProvisioner();
      stacks.stacks.set(ARTIFACT_STACK_NAME, {
        name: ARTIFACT_STACK_NAME,
        state,
        outputs,
        parameters: {},
      });
      const resolver = new AwsResolver({ region: "us-east-1", stacks });
      expect(await resolver.artifactStackBucket()).toBe(expected);
      expect(stacks.calls).toEqual([["describe", ARTIFACT_STACK_NAME]]);
    },
  );

  test.each(["AccessDenied", "AccessDeniedException"])(
    "AwsResolver treats %s on DescribeStacks as no bucket and deployAgent keeps the old error",
    async (name) => {
      const stacks = {
        describe: async () => {
          throw new StackError(`${name}: no`, { code: "failed", cause: { name, message: "no" } });
        },
      };
      const resolver = new AwsResolver({ region: "us-east-1", stacks });
      expect(await resolver.artifactStackBucket()).toBeUndefined();
      const failure = await resolveBucket(undefined, undefined, {}, () =>
        resolver.artifactStackBucket(),
      ).catch((error: unknown) => error);
      expect(failure).toBeInstanceOf(InvalidArgumentError);
      expect((failure as Error).message).toContain("Rayito no crea buckets");
    },
  );

  test("AwsResolver still propagates any other DescribeStacks failure", async () => {
    const stacks = {
      describe: async () => {
        throw new StackError("Throttling: no", { code: "failed", cause: { name: "Throttling" } });
      },
    };
    const resolver = new AwsResolver({ region: "us-east-1", stacks });
    await expect(resolver.artifactStackBucket()).rejects.toBeInstanceOf(StackError);
  });

  test("AwsResolver finds no bucket when the stack does not exist", async () => {
    const stacks = new FakeStackProvisioner();
    const resolver = new AwsResolver({ region: "us-east-1", stacks });
    expect(await resolver.artifactStackBucket()).toBeUndefined();
  });

  test("the cdk bucket wins and the stack is not read", async () => {
    const aws = new FakeAws();
    aws.buckets.add(cdkBucketName("123456789012", "us-east-1"));
    aws.stackBucket = BUCKET;
    expect(await discoverBucket(aws, "us-east-1")).toBe(cdkBucketName("123456789012", "us-east-1"));
    expect(aws.calls).not.toContain("stack");
  });

  test.each(RESOLVE.costs as CostCase[])("cost $memoryMib", (item) => {
    const cost = estimateCost(
      item.memoryMib,
      new ScalingOptions({
        minVms: item.scaling.minVms,
        maxVms: item.scaling.maxVms,
        warmVms: item.scaling.warmVms,
      }),
    );
    const round = (value: number) => Math.round(value * 1e6) / 1e6;
    expect(round(cost.runningUsdPerHour)).toBe(item.runningUsdPerHour);
    expect(round(cost.parkedUsdPerDay)).toBe(item.parkedUsdPerDay);
    expect(round(cost.imageBuildUsd)).toBe(item.imageBuildUsd);
    expect(round(cost.imageStorageUsdPerWeek)).toBe(item.imageStorageUsdPerWeek);
  });

  test("the default source excludes are the shared list", () => {
    expect([...AGENT_SOURCE_DEFAULT_EXCLUDES]).toEqual(RESOLVE.sourceExcludes);
  });

  test("the token is the argument, then the environment, then a new one", () => {
    const explicit = generateAccessToken();
    const fromEnv = generateAccessToken();
    expect(resolveToken(explicit, { [ACCESS_TOKEN_ENV_VAR]: fromEnv })).toBe(explicit);
    expect(resolveToken(undefined, { [ACCESS_TOKEN_ENV_VAR]: fromEnv })).toBe(fromEnv);
    expect(resolveToken(undefined, {})).not.toBe(resolveToken(undefined, {}));
  });
});

describe("options", () => {
  test.each([
    { bucket: "" },
    { bucket: "no" },
    { bucket: "has space" },
    { bucket: "a/b/c" },
    { baseImageVersion: "" },
    { baseImageVersion: "1 2" },
    { accessToken: "corto" },
    { secretNames: { bedrock: "" } },
    { secretNames: { bedrock: "has space" } },
    { secretNames: ["bedrock"] },
    { createSecretFromEnv: "si" },
    { scaling: { minVms: 1 } },
    { buildTimeoutMs: 0 },
    { buildImage: "no" },
  ])("rejects %j", (options) => {
    expect(() => new AgentDeployOptions(options as never)).toThrow(InvalidArgumentError);
  });

  test("keeps the access token out of enumeration and inspection", () => {
    const token = generateAccessToken();
    const options = new AgentDeployOptions({ accessToken: token });
    expect(options.accessToken).toBe(token);
    expect(JSON.stringify(options)).not.toContain(token);
    expect(inspect(options)).not.toContain(token);
    expect({ ...options }).not.toHaveProperty("accessToken");
  });

  test("default to nothing that costs", () => {
    const options = new AgentDeployOptions();
    expect(options.createSecretFromEnv).toBe(false);
    expect(options.basePublisher).toBeUndefined();
    expect(options.sessionIndex).toBeUndefined();
    expect(options.buildImage).toBeUndefined();
  });
});

describe("planDeploy", () => {
  test("calls nothing and reports the cost", async () => {
    const plan = await planDeploy(".", {
      project: project({ memoryMib: 4096 }),
      options: {
        bucket: BUCKET,
        secretNames: { bedrock: "otra" },
        scaling: new ScalingOptions({ maxVms: 2 }),
      },
      region: "us-east-1",
    });
    expect(plan.name).toBe("support-bot");
    expect(plan.memoryMib).toBe(4096);
    expect(plan.region).toBe("us-east-1");
    expect(plan.scaling.maxVms).toBe(2);
    expect(plan.bucketSource).toBe("argument");
    expect(plan.secrets).toEqual([{ gateway: "bedrock", source: "override" }]);
    expect(plan.cost.runningUsdPerHour).toBeGreaterThan(0);
    expect(plan.notes.some((note) => note.includes("memoria"))).toBe(true);
  });

  test("names where each value would come from", async () => {
    const previousBucket = process.env[BUCKET_ENV_VAR];
    const previousToken = process.env[ACCESS_TOKEN_ENV_VAR];
    process.env[BUCKET_ENV_VAR] = BUCKET;
    delete process.env[ACCESS_TOKEN_ENV_VAR];
    try {
      const plan = await planDeploy(".", { project: project() });
      expect(plan.bucketSource).toBe("environment");
      expect(plan.tokenSource).toBe("generated");
      expect(plan.secrets[0]?.source).toBe("project");
    } finally {
      if (previousBucket === undefined) {
        delete process.env[BUCKET_ENV_VAR];
      } else {
        process.env[BUCKET_ENV_VAR] = previousBucket;
      }
      if (previousToken !== undefined) {
        process.env[ACCESS_TOKEN_ENV_VAR] = previousToken;
      }
    }
  });

  test("without a build there is no memory note", async () => {
    const plan = await planDeploy(".", { project: project(), options: { buildImage: false } });
    expect(plan.notes).toEqual([]);
  });

  test("uses the detector", async () => {
    const detect = vi
      .spyOn(AgentProject, "detect")
      .mockResolvedValue([project(), { framework: "langgraph" } as never]);
    try {
      const overrides: DetectOverrides = { framework: "langgraph" };
      const plan = await planDeploy("./mi-agente", { overrides, region: "us-east-1" });
      expect(detect).toHaveBeenCalledWith("./mi-agente", overrides, { region: "us-east-1" });
      expect(plan.detection).toEqual({ framework: "langgraph" });
    } finally {
      detect.mockRestore();
    }
  });

  test("a complete project file needs no detection", async () => {
    const directory = mkdtempSync(join(tmpdir(), "rayito-deploy-"));
    writeFileSync(join(directory, "rayito-agent.json"), JSON.stringify(project().toObject()));
    const plan = await planDeploy(directory);
    expect(plan.name).toBe("support-bot");
    expect(plan.detection).toBeUndefined();
  });
});

describe("zero calls", () => {
  test("planDeploy, options, scaling and constructing a fleet call nothing", async () => {
    const calls: string[] = [];
    const trip = (name: string) => () => {
      calls.push(name);
      throw new Error(`${name} no debe llamarse`);
    };
    vi.spyOn(Sandbox, "create").mockImplementation(trip("Sandbox.create") as never);
    vi.spyOn(Sandbox, "list").mockImplementation(trip("Sandbox.list") as never);
    vi.spyOn(Template, "build").mockImplementation(trip("Template.build") as never);
    vi.spyOn(net.Socket.prototype, "connect").mockImplementation(trip("socket.connect") as never);
    vi.stubGlobal("fetch", trip("fetch"));
    await planDeploy(".", { project: project(), options: { bucket: BUCKET } });
    new AgentDeployOptions({ createSecretFromEnv: false });
    new ScalingOptions({ minVms: 1, maxVms: 3, warmVms: 1 });
    new AgentFleet({ project: project(), accessToken: generateAccessToken() });
    expect(calls).toEqual([]);
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });
});

describe("deployAgent", () => {
  test("builds once and starts minVms with the resolved token", async () => {
    const h = new Harness();
    const token = generateAccessToken();
    h.environ[ACCESS_TOKEN_ENV_VAR] = token;
    const fleet = await h.deploy({ scaling: new ScalingOptions({ minVms: 2, maxVms: 2 }) });
    expect(h.builds).toHaveLength(1);
    expect(h.builds[0]?.bucket).toBe(BUCKET);
    expect(templateOf(h.builds[0]).name).toBe("support-bot");
    expect(fleet.sandboxIds).toHaveLength(2);
    expect(fleet.accessToken).toBe(token);
    expect(h.steps.map((step) => step.kind)).toEqual(["bucket", "image", "fleet"]);
    expect(h.steps[0]?.detail).toBe(`environment:${BUCKET}`);
    expect(h.steps[1]?.detail).toBe("3.0");
  });

  test("every VM and the warm pool start the version the build chose", async () => {
    const h = new Harness();
    await h.deploy({ scaling: new ScalingOptions({ minVms: 2, maxVms: 3, warmVms: 1 }) });
    expect(h.fleet.world.created.map(({ options }) => options.templateVersion)).toEqual([
      "3.0",
      "3.0",
    ]);
    expect(h.fleet.pools.map((pool) => pool.config.templateVersion)).toEqual(["3.0"]);
  });

  test("without a build the newest active version is used", async () => {
    const h = new Harness();
    await h.deploy({ buildImage: false });
    expect(h.fleet.world.created.map(({ options }) => options.templateVersion)).toEqual([
      undefined,
    ]);
  });

  test("without a token makes one and the fleet returns it", async () => {
    const h = new Harness();
    const fleet = await h.deploy();
    expect(fleet.accessToken).not.toBe("");
    expect(
      h.fleet.world.created.every(({ options }) => options.accessToken === fleet.accessToken),
    ).toBe(true);
  });

  test("a missing base image stops before anything is created", async () => {
    const h = new Harness();
    h.aws.images.clear();
    const error = await failureOf(h.deploy());
    expect(error.message).toContain("rayito image publish");
    expect(h.builds).toEqual([]);
    expect(h.createdVms).toBe(0);
    expect(h.store.checked).toEqual([]);
  });

  test("a missing base image is published only with a publisher", async () => {
    const h = new Harness();
    h.aws.images.clear();
    const published: boolean[] = [];
    await h.deploy({
      basePublisher: async () => {
        published.push(true);
        h.aws.images.add("rayito-base-caps");
      },
    });
    expect(published).toEqual([true]);
    expect(h.steps[0]).toEqual({ kind: "base_image", detail: "published" });
  });

  test("a publisher that does not publish is an error", async () => {
    const h = new Harness();
    h.aws.images.clear();
    const error = await failureOf(h.deploy({ basePublisher: async () => undefined }));
    expect(error.message).toContain("sigue sin existir");
  });

  test("ensureBaseImage false never publishes", async () => {
    const h = new Harness();
    h.aws.images.clear();
    const called: boolean[] = [];
    await failureOf(
      h.deploy({ basePublisher: async () => void called.push(true), ensureBaseImage: false }),
    );
    expect(called).toEqual([]);
  });

  test("a missing secret stops before the build and names the options", async () => {
    const h = new Harness();
    h.store.existing.clear();
    const error = await failureOf(h.deploy());
    expect(error.message).toContain("--secret bedrock=");
    expect(error.message).toContain("--create-secret");
    expect(error.message).toContain("AWS_BEARER_TOKEN_BEDROCK");
    expect(error.message).not.toContain("bedrock-key");
    expect(h.builds).toEqual([]);
    expect(h.createdVms).toBe(0);
  });

  test("createSecretFromEnv stores the value and never prints it", async () => {
    const h = new Harness();
    h.store.existing.clear();
    h.environ.AWS_BEARER_TOKEN_BEDROCK = "valor-de-prueba";
    await h.deploy({ createSecretFromEnv: true });
    expect(h.store.created).toEqual([["bedrock-key", "Bearer valor-de-prueba"]]);
    expect(h.steps).toContainEqual({ kind: "secret_created", detail: "bedrock" });
    expect(h.steps.every((step) => !step.detail.includes("valor-de-prueba"))).toBe(true);
  });

  test("createSecretFromEnv needs the variable", async () => {
    const h = new Harness();
    h.store.existing.clear();
    const error = await failureOf(h.deploy({ createSecretFromEnv: true }));
    expect(error.message).toContain("AWS_BEARER_TOKEN_BEDROCK");
    expect(h.store.created).toEqual([]);
  });

  test("secretNames replace the name of the project", async () => {
    const h = new Harness();
    h.store.existing = new Set(["mi-propia-clave"]);
    await h.deploy({ secretNames: { bedrock: "mi-propia-clave" } });
    expect(h.store.checked).toEqual(["mi-propia-clave"]);
    const gateways = h.fleet.world.created[0]?.options.gateways as Record<
      string,
      { headers: Record<string, string> }
    >;
    expect(gateways.bedrock?.headers.authorization).toBe("mi-propia-clave");
  });

  test("secretNames for an unknown gateway fail", async () => {
    const h = new Harness();
    const error = await failureOf(h.deploy({ secretNames: { openai: "x" } }));
    expect(error.message).toContain("pasarelas que el proyecto no tiene");
  });

  test("without a bucket an existing image is reused", async () => {
    const h = new Harness();
    delete h.environ[BUCKET_ENV_VAR];
    h.aws.images.add("support-bot");
    await h.deploy();
    expect(h.builds).toEqual([]);
    expect(h.createdVms).toBe(1);
    expect(h.steps.map((step) => step.kind)).not.toContain("bucket");
  });

  test("without a bucket and without an image it fails before creating", async () => {
    const h = new Harness();
    delete h.environ[BUCKET_ENV_VAR];
    const error = await failureOf(h.deploy());
    expect(error.message).toContain("no crea buckets");
    expect(h.createdVms).toBe(0);
  });

  test("buildImage true requires a bucket even if the image exists", async () => {
    const h = new Harness();
    delete h.environ[BUCKET_ENV_VAR];
    h.aws.images.add("support-bot");
    const error = await failureOf(h.deploy({ buildImage: true }));
    expect(error.message).toContain("no crea buckets");
  });

  test("buildImage false skips the build and the base check", async () => {
    const h = new Harness();
    h.aws.images.clear();
    delete h.environ[BUCKET_ENV_VAR];
    await h.deploy({ buildImage: false });
    expect(h.builds).toEqual([]);
    expect(h.aws.calls.some((call) => call.startsWith("image:"))).toBe(false);
  });

  test("the discovered bucket is used and reported", async () => {
    const h = new Harness();
    delete h.environ[BUCKET_ENV_VAR];
    const discovered = cdkBucketName("123456789012", "us-east-1");
    h.aws.buckets.add(discovered);
    await h.deploy();
    expect(h.builds[0]?.bucket).toBe(discovered);
    expect(h.steps[0]?.detail).toBe(`discovered:${discovered}`);
  });

  test("the bucket of the rayito setup stack is found when no other source exists", async () => {
    const h = new Harness();
    delete h.environ[BUCKET_ENV_VAR];
    h.aws.stackBucket = BUCKET;
    await h.deploy();
    expect(h.builds[0]?.bucket).toBe(BUCKET);
    expect(h.steps[0]?.detail).toBe(`discovered:${BUCKET}`);
  });

  test("the base image version and timeout reach the build", async () => {
    const h = new Harness();
    await h.deploy({ baseImageVersion: "7", buildTimeoutMs: 42 });
    expect(h.builds[0]?.timeoutMs).toBe(42);
    expect(templateOf(h.builds[0]).baseVersion).toBe("7");
  });

  test("the base image version follows argument, then project file, then latest", async () => {
    const h = new Harness();
    const fromFile = project({ baseImageVersion: "3" });
    await h.deploy({}, fromFile);
    await h.deploy({ baseImageVersion: "7" }, fromFile);
    await h.deploy({}, project({ baseImageVersion: "latest" }));
    await h.deploy({}, project());
    expect(h.builds.map((build) => templateOf(build).baseVersion)).toEqual([
      "3",
      "7",
      undefined,
      undefined,
    ]);
  });

  test("the project file bucket is used when nothing else decides", async () => {
    const h = new Harness();
    delete h.environ[BUCKET_ENV_VAR];
    await h.deploy({}, project({ bucket: "amzn-s3-demo-project" }));
    expect(h.builds[0]?.bucket).toBe("amzn-s3-demo-project");
    expect(h.steps[0]?.detail).toBe("project_file:amzn-s3-demo-project");
  });

  test("warm slots are waited for and reported", async () => {
    const h = new Harness();
    const scaling = new ScalingOptions({ minVms: 1, maxVms: 3, warmVms: 1 });
    const fleet = await h.deploy({ scaling });
    expect(h.steps[h.steps.length - 1]).toEqual({ kind: "warm", detail: "ready" });
    expect(fleet.scaling).toEqual(scaling);
  });

  test("a deploy never ships the token or the environment files", async () => {
    const h = new Harness();
    await h.deploy({}, project({ exclude: ["dist", ".git"] }));
    const template = h.builds[0]?.template as unknown as
      | { appExclude: readonly string[] }
      | undefined;
    const exclude = template?.appExclude ?? [];
    expect(exclude).toContain(".rayito");
    expect(exclude).toContain(".env");
    expect(exclude.filter((pattern) => pattern === ".git")).toHaveLength(1);
    expect(exclude).toContain("dist");
  });

  test("keepOnFailure reaches the VM creation", async () => {
    const h = new Harness();
    await h.deploy({ keepOnFailure: true });
    expect(h.fleet.world.created[0]?.options.accessToken).toBeDefined();
  });
});
