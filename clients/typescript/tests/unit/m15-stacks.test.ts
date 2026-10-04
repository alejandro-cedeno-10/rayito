/**
 * `src/stacks/model.ts` y `src/stacks/service.ts` (M15 foundations): espejo
 * de `test_m15_stacks_model.py`/`test_m15_stacks_service.py`.
 */

import { describe, expect, test } from "vitest";
import { InvalidArgumentError, StackError, UnimplementedError } from "../../src/errors.js";
import { CloudFormationProvisioner } from "../../src/stacks/cloudformation.js";
import {
  type CostStatement,
  defaultStackName,
  planDeploy,
  type StackComponent,
  stackTags,
} from "../../src/stacks/model.js";
import { loadTemplate } from "../../src/stacks/packaging.js";
import { COMPONENTS, componentByName } from "../../src/stacks/registry.js";
import { OptionalStacks } from "../../src/stacks/service.js";
import { VERSION } from "../../src/version.js";
import { FakeStackProvisioner } from "./m15-fake-stacks.js";

const COST: CostStatement = { creates: [], idleMonthly: "$0" };

function component(name = "widget"): StackComponent {
  return { name, description: "x", cost: COST };
}

/** Every catalog component has a template now (custom-domain was the last
 * stub), so the `supported: false` path is exercised with a fake one. */
function unsupportedStub(): StackComponent {
  return { ...component("stub-component"), supported: false };
}

describe("stacks/model", () => {
  test("default stack name is prefixed", () => {
    expect(defaultStackName(component("metadata-index"))).toBe("rayito-metadata-index");
  });

  test("plan deploy: create / update / blocked", () => {
    expect(planDeploy(undefined).action).toBe("create");
    expect(planDeploy({ name: "x", state: "CREATE_COMPLETE", outputs: {} }).action).toBe("update");
    const blocked = planDeploy({ name: "x", state: "ROLLBACK_COMPLETE", outputs: {} });
    expect(blocked.action).toBe("blocked");
    expect(blocked.reason).toContain("ROLLBACK_COMPLETE");
  });

  test("fixed tags always win over user tags", () => {
    const merged = stackTags(component("metadata-index"), {
      "rayito:component": "spoofed",
      team: "platform",
    });
    expect(merged["rayito:component"]).toBe("metadata-index");
    expect(merged["rayito:managed-by"]).toBe("rayito-sdk");
    expect(merged["rayito:sdk-version"]).toBe(VERSION);
    expect(merged.team).toBe("platform");
  });
});

describe("stacks/service: OptionalStacks", () => {
  test("components() lists the full catalog with no provisioner call", () => {
    const fake = new FakeStackProvisioner();
    const stacks = new OptionalStacks({ provisioner: fake });
    const names = new Set(stacks.components().map((c) => c.name));
    expect(names).toEqual(new Set(COMPONENTS.map((c) => c.name)));
    expect(fake.calls).toEqual([]);
  });

  test("deploying an unsupported component raises before touching the provisioner", async () => {
    const fake = new FakeStackProvisioner();
    const stacks = new OptionalStacks({ provisioner: fake });
    await expect(stacks.deploy(unsupportedStub())).rejects.toThrow(UnimplementedError);
    expect(fake.calls).toEqual([]);
  });

  test("every catalog component has a template", () => {
    expect(COMPONENTS.every((entry) => entry.supported !== false)).toBe(true);
  });

  test("an unknown component name is invalid argument", async () => {
    const fake = new FakeStackProvisioner();
    const stacks = new OptionalStacks({ provisioner: fake });
    await expect(stacks.status("not-a-real-component")).rejects.toThrow(InvalidArgumentError);
    expect(fake.calls).toEqual([]);
  });

  test("deploy creates a stack that does not exist yet", async () => {
    const fake = new FakeStackProvisioner();
    const stacks = new OptionalStacks({ provisioner: fake });
    const status = await stacks.deploy("metadata-index");
    expect(status.state).toBe("CREATE_COMPLETE");
    expect(fake.calls.map((call) => call[0])).toEqual(["describe", "create", "wait", "describe"]);
  });

  test("deploy updates an existing stack", async () => {
    const fake = new FakeStackProvisioner();
    fake.stacks.set("rayito-metadata-index", {
      name: "rayito-metadata-index",
      state: "CREATE_COMPLETE",
      outputs: {},
    });
    const stacks = new OptionalStacks({ provisioner: fake });
    const status = await stacks.deploy("metadata-index");
    expect(status.state).toBe("UPDATE_COMPLETE");
    expect(fake.calls.map((call) => call[0])).toEqual(["describe", "update", "wait", "describe"]);
  });

  test("deploy is blocked on a ROLLBACK_COMPLETE stack", async () => {
    const fake = new FakeStackProvisioner();
    fake.stacks.set("rayito-metadata-index", {
      name: "rayito-metadata-index",
      state: "ROLLBACK_COMPLETE",
      outputs: {},
    });
    const stacks = new OptionalStacks({ provisioner: fake });
    await expect(stacks.deploy("metadata-index")).rejects.toThrow(StackError);
    expect(fake.calls.map((call) => call[0])).toEqual(["describe"]);
  });

  test("unknown parameters are rejected before any call", async () => {
    const fake = new FakeStackProvisioner();
    const stacks = new OptionalStacks({ provisioner: fake });
    await expect(
      stacks.deploy("metadata-index", { parameters: { TotallyMadeUp: "x" } }),
    ).rejects.toThrow(InvalidArgumentError);
    expect(fake.calls).toEqual([]);
  });

  test("destroy deletes and waits by default", async () => {
    const fake = new FakeStackProvisioner();
    const stacks = new OptionalStacks({ provisioner: fake });
    await stacks.destroy("metadata-index");
    expect(fake.calls.map((call) => call[0])).toEqual(["delete", "wait"]);
  });

  test("destroy without wait skips the wait call", async () => {
    const fake = new FakeStackProvisioner();
    const stacks = new OptionalStacks({ provisioner: fake });
    await stacks.destroy("metadata-index", { wait: false });
    expect(fake.calls.map((call) => call[0])).toEqual(["delete"]);
  });

  test("destroying an unsupported component also raises first", async () => {
    const fake = new FakeStackProvisioner();
    const stacks = new OptionalStacks({ provisioner: fake });
    await expect(stacks.destroy(unsupportedStub())).rejects.toThrow(UnimplementedError);
    expect(fake.calls).toEqual([]);
  });

  test("componentByName is undefined for an unknown name", () => {
    expect(componentByName("not-a-component")).toBeUndefined();
    expect(componentByName("metadata-index")).toBeDefined();
  });
});

/** Prefijo de tipo de todo recurso IAM de CloudFormation; una plantilla que
 * crea uno exige `CAPABILITY_IAM` (o `CAPABILITY_NAMED_IAM`) en
 * `CreateStack`/`UpdateStack`, o falla con `InsufficientCapabilitiesException`. */
const IAM_RESOURCE_TYPE_PREFIX = "AWS::IAM::";

describe("stacks/packaging", () => {
  const supported = COMPONENTS.filter((entry) => entry.supported !== false);

  test.each(supported.map((entry) => [entry.name, entry] as const))(
    "%s: packaged, and declares a capability if it creates IAM resources",
    async (_name, entry) => {
      const template = await loadTemplate(entry);
      if (template.includes(IAM_RESOURCE_TYPE_PREFIX)) {
        expect(entry.capabilities ?? []).not.toHaveLength(0);
      }
    },
  );
});

describe("OptionalStacks artifact bucket parameter", () => {
  test("events-webhooks gets ArtifactBucket from artifactBucket (0.6 AWS acceptance)", async () => {
    const provisioner = new FakeStackProvisioner();
    await new OptionalStacks({ provisioner }).deploy("events-webhooks", {
      parameters: { LogGroupName: "/rayito/x" },
      artifactBucket: "bucket-a",
    });
    expect(provisioner.sentParameters.ArtifactBucket).toBe("bucket-a");
    expect(provisioner.sentParameters.ArtifactS3Key).toBeTruthy();
  });

  test("a conflicting ArtifactBucket is rejected before any upload", async () => {
    const provisioner = new FakeStackProvisioner();
    await expect(
      new OptionalStacks({ provisioner }).deploy("events-webhooks", {
        parameters: { LogGroupName: "/rayito/x", ArtifactBucket: "bucket-b" },
        artifactBucket: "bucket-a",
      }),
    ).rejects.toThrow(/ArtifactBucket/);
    expect(provisioner.calls).toEqual([]);
  });
});

// ------------------------------- redeploy keeps the settings already deployed

async function redeploy(
  name: string,
  first: Record<string, string>,
  second: Record<string, string>,
): Promise<FakeStackProvisioner> {
  const fake = new FakeStackProvisioner();
  const stacks = new OptionalStacks({ provisioner: fake });
  await stacks.deploy(name, { parameters: first });
  await stacks.deploy(name, { parameters: second });
  return fake;
}

describe("OptionalStacks redeploy keeps deployed parameters (UsePreviousValue)", () => {
  test("s3-mounts keeps Prefixes and ReadOnly (no privilege widening)", async () => {
    const fake = await redeploy(
      "s3-mounts",
      { BucketName: "b", Prefixes: "team7/*", ReadOnly: "false" },
      { BucketName: "b" },
    );
    expect(fake.sentParameters).toEqual({ BucketName: "b" });
    expect(fake.sentKeepPrevious).toEqual(["Prefixes", "ReadOnly"]);
    expect(fake.stacks.get("rayito-s3-mounts")?.parameters).toMatchObject({
      Prefixes: "team7/*",
      ReadOnly: "false",
    });
  });

  test("s3-mounts keeps the required BucketName when not passed again", async () => {
    const fake = await redeploy("s3-mounts", { BucketName: "b" }, { Prefixes: "a/*" });
    expect(fake.sentKeepPrevious).toEqual(["BucketName", "ReadOnly"]);
    expect(fake.stacks.get("rayito-s3-mounts")?.parameters?.BucketName).toBe("b");
  });

  test("metadata-index keeps TableName and DeletionProtection (no table replacement)", async () => {
    const fake = await redeploy(
      "metadata-index",
      { TableName: "my-table", DeletionProtection: "true", PointInTimeRecovery: "true" },
      {},
    );
    expect(fake.sentParameters).toEqual({});
    expect(fake.sentKeepPrevious).toEqual([
      "DeletionProtection",
      "PointInTimeRecovery",
      "TableName",
    ]);
    expect(fake.stacks.get("rayito-metadata-index")?.parameters).toEqual({
      TableName: "my-table",
      DeletionProtection: "true",
      PointInTimeRecovery: "true",
    });
  });

  test("secrets-access keeps KmsKeyArn", async () => {
    const fake = await redeploy("secrets-access", { KmsKeyArn: "arn:aws:kms:example" }, {});
    expect(fake.sentKeepPrevious).toContain("KmsKeyArn");
    expect(fake.stacks.get("rayito-secrets-access")?.parameters?.KmsKeyArn).toBe(
      "arn:aws:kms:example",
    );
  });

  test("a parameter the existing stack lacks gets its default on update", async () => {
    const fake = new FakeStackProvisioner();
    fake.stacks.set("rayito-metadata-index", {
      name: "rayito-metadata-index",
      state: "CREATE_COMPLETE",
      outputs: {},
      parameters: { TableName: "my-table" },
    });
    await new OptionalStacks({ provisioner: fake }).deploy("metadata-index");
    expect(fake.sentKeepPrevious).toEqual(["TableName"]);
    expect(fake.sentParameters).toEqual({
      DeletionProtection: "false",
      PointInTimeRecovery: "false",
    });
  });

  test("create still fills the catalog defaults", async () => {
    const fake = new FakeStackProvisioner();
    await new OptionalStacks({ provisioner: fake }).deploy("s3-mounts", {
      parameters: { BucketName: "b" },
    });
    expect(fake.sentParameters).toEqual({ BucketName: "b", Prefixes: "*", ReadOnly: "true" });
    expect(fake.sentKeepPrevious).toEqual([]);
  });

  test("create without a required parameter is rejected before creating", async () => {
    const fake = new FakeStackProvisioner();
    await expect(new OptionalStacks({ provisioner: fake }).deploy("s3-mounts")).rejects.toThrow(
      /BucketName/,
    );
    expect(fake.calls.map((call) => call[0])).toEqual(["describe"]);
  });

  test("parameterChanges lists only what deploy would change", async () => {
    const fake = new FakeStackProvisioner();
    const stacks = new OptionalStacks({ provisioner: fake });
    await stacks.deploy("s3-mounts", { parameters: { BucketName: "b", Prefixes: "team7/*" } });
    const changes = await stacks.parameterChanges("s3-mounts", {
      parameters: { ReadOnly: "false" },
    });
    expect(changes).toEqual([{ name: "ReadOnly", before: "true", after: "false" }]);
  });
});

describe("CloudFormationProvisioner parameters", () => {
  test("update sends kept parameters with UsePreviousValue", async () => {
    const sent: unknown[] = [];
    const api = {
      describeStacks: async () => ({
        Stacks: [
          {
            StackStatus: "UPDATE_COMPLETE",
            Outputs: [],
            Parameters: [{ ParameterKey: "Prefixes", ParameterValue: "team7/*" }],
          },
        ],
      }),
      createStack: async () => ({}),
      updateStack: async (input: unknown) => {
        sent.push(input);
        return {};
      },
      deleteStack: async () => ({}),
    };
    const adapter = new CloudFormationProvisioner({
      region: "us-east-1",
      cloudformationClient: api,
    });
    await adapter.update(component("s3-mounts"), {
      stackName: "rayito-s3-mounts",
      templateBody: "x",
      parameters: { BucketName: "b" },
      tags: {},
      keepPrevious: ["Prefixes", "ReadOnly"],
    });
    expect(sent).toEqual([
      {
        StackName: "rayito-s3-mounts",
        TemplateBody: "x",
        Parameters: [
          { ParameterKey: "BucketName", ParameterValue: "b" },
          { ParameterKey: "Prefixes", UsePreviousValue: true },
          { ParameterKey: "ReadOnly", UsePreviousValue: true },
        ],
        Tags: [],
        Capabilities: undefined,
      },
    ]);
    const status = await adapter.describe("rayito-s3-mounts");
    expect(status?.parameters).toEqual({ Prefixes: "team7/*" });
  });
});
