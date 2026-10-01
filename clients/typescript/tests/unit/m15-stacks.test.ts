/**
 * `src/stacks/model.ts` y `src/stacks/service.ts` (M15 foundations): espejo
 * de `test_m15_stacks_model.py`/`test_m15_stacks_service.py`.
 */

import { describe, expect, test } from "vitest";
import { InvalidArgumentError, StackError, UnimplementedError } from "../../src/errors.js";
import {
  type CostStatement,
  defaultStackName,
  planDeploy,
  type StackComponent,
  stackTags,
} from "../../src/stacks/model.js";
import { COMPONENTS, componentByName } from "../../src/stacks/registry.js";
import { OptionalStacks } from "../../src/stacks/service.js";
import { VERSION } from "../../src/version.js";
import { FakeStackProvisioner } from "./m15-fake-stacks.js";

const COST: CostStatement = { creates: [], idleMonthly: "$0" };

function component(name = "widget"): StackComponent {
  return { name, description: "x", cost: COST };
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
    await expect(stacks.deploy("s3-mounts")).rejects.toThrow(UnimplementedError);
    expect(fake.calls).toEqual([]);
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
    // sizes-guard, not efs-volumes: m15-efs-volumes made that component
    // real (infra/efs-volumes.yaml), so it is supported: true now.
    const fake = new FakeStackProvisioner();
    const stacks = new OptionalStacks({ provisioner: fake });
    await expect(stacks.destroy("sizes-guard")).rejects.toThrow(UnimplementedError);
    expect(fake.calls).toEqual([]);
  });

  test("componentByName is undefined for an unknown name", () => {
    expect(componentByName("not-a-component")).toBeUndefined();
    expect(componentByName("metadata-index")).toBeDefined();
  });
});
