/**
 * `CloudFormationProvisioner.failureReason` (`DescribeStackEvents`) y el
 * estado terminal que `wait()` pone en el `StackError`: espejo de los tests de
 * `failure_reason` de `test_m15_cloudformation.py`.
 */

import { describe, expect, test } from "vitest";
import { StackError } from "../../src/errors.js";
import { CloudFormationProvisioner } from "../../src/stacks/cloudformation.js";
import { componentByName } from "../../src/stacks/registry.js";
import { OptionalStacks } from "../../src/stacks/service.js";
import { FakeStackProvisioner } from "./m15-fake-stacks.js";

const SECRET_SIGNATURE = "0123456789abcdef";

interface ApiOptions {
  readonly events?: unknown;
  readonly eventsError?: Error;
  readonly state?: string;
  readonly reason?: string;
}

function adapterWith(options: ApiOptions) {
  const sent: unknown[] = [];
  const api = {
    describeStacks: async () => ({
      Stacks: [
        { StackStatus: options.state ?? "ROLLBACK_COMPLETE", StackStatusReason: options.reason },
      ],
    }),
    createStack: async () => ({}),
    updateStack: async () => ({}),
    deleteStack: async () => ({}),
    describeStackEvents: async (input: unknown) => {
      sent.push(input);
      if (options.eventsError !== undefined) {
        throw options.eventsError;
      }
      return options.events ?? {};
    },
  };
  const adapter = new CloudFormationProvisioner({
    region: "us-east-1",
    cloudformationClient: api as never,
    pollIntervalMs: 1,
  });
  return { adapter, sent };
}

describe("CloudFormationProvisioner.failureReason", () => {
  test("names the earliest failed resource, sends only StackName and redacts", async () => {
    const { adapter, sent } = adapterWith({
      events: {
        StackEvents: [
          {
            LogicalResourceId: "Queue",
            ResourceStatus: "CREATE_FAILED",
            ResourceStatusReason: "Resource creation cancelled",
          },
          {
            LogicalResourceId: "Distribution",
            ResourceStatus: "CREATE_FAILED",
            ResourceStatusReason: `denied X-Amz-Signature=${SECRET_SIGNATURE}`,
          },
          { LogicalResourceId: "x", ResourceStatus: "CREATE_IN_PROGRESS" },
        ],
      },
    });
    const reason = await adapter.failureReason("x");
    expect(sent).toEqual([{ StackName: "x" }]);
    expect(reason).toContain("Distribution (CREATE_FAILED): denied");
    expect(reason).not.toContain(SECRET_SIGNATURE);
  });

  test("falls back to the stack's own reason, redacted", async () => {
    const { adapter } = adapterWith({
      events: { StackEvents: [{ LogicalResourceId: "x", ResourceStatus: "ROLLBACK_COMPLETE" }] },
      reason: `boom X-Amz-Signature=${SECRET_SIGNATURE}`,
    });
    const reason = await adapter.failureReason("x");
    expect(reason).toContain("boom");
    expect(reason).not.toContain(SECRET_SIGNATURE);
  });

  test("a missing stack has no reason", async () => {
    const { adapter } = adapterWith({
      eventsError: Object.assign(new Error("Stack [x] does not exist"), {
        name: "ValidationError",
      }),
    });
    expect(await adapter.failureReason("x")).toBeUndefined();
  });

  test("a rejected read is a StackError without state", async () => {
    const { adapter } = adapterWith({ eventsError: new Error("not allowed") });
    const error = await adapter.failureReason("x").catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(StackError);
    expect((error as StackError).code).toBe("failed");
    expect((error as StackError).state).toBeUndefined();
  });
});

describe("CloudFormationProvisioner.wait", () => {
  test("stamps the terminal state on the failure", async () => {
    const { adapter } = adapterWith({ state: "UPDATE_ROLLBACK_COMPLETE" });
    const error = await adapter.wait("x", "deployed", 1_000).catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(StackError);
    expect((error as StackError).code).toBe("failed");
    expect((error as StackError).state).toBe("UPDATE_ROLLBACK_COMPLETE");
  });
});

describe("OptionalStacks.failureReason", () => {
  test("resolves the default and the given stack name", async () => {
    const fake = new FakeStackProvisioner();
    fake.events.set("rayito-metadata-index", [
      {
        logicalResourceId: "Table",
        resourceStatus: "CREATE_FAILED",
        resourceStatusReason: "denied",
      },
    ]);
    fake.events.set("mine", [
      {
        logicalResourceId: "Queue",
        resourceStatus: "UPDATE_FAILED",
        resourceStatusReason: "invalid",
      },
    ]);
    const stacks = new OptionalStacks({ provisioner: fake });
    expect(await stacks.failureReason("metadata-index")).toBe("Table (CREATE_FAILED): denied");
    expect(await stacks.failureReason("metadata-index", { stackName: "mine" })).toBe(
      "Queue (UPDATE_FAILED): invalid",
    );
    expect(fake.calls).toEqual([
      ["failureReason", "rayito-metadata-index"],
      ["failureReason", "mine"],
    ]);
  });

  test("an unknown component is rejected before the provisioner", async () => {
    const fake = new FakeStackProvisioner();
    await expect(
      new OptionalStacks({ provisioner: fake }).failureReason("no-such-component"),
    ).rejects.toThrow();
    expect(fake.calls).toEqual([]);
    expect(componentByName("no-such-component")).toBeUndefined();
  });
});
