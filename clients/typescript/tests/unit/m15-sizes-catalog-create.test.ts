/**
 * `Sandbox.create({ size })` de extremo a extremo (m15-sizes-catalog):
 * espejo de `test_m15_sizes_catalog_create.py`.
 */

import { afterEach, describe, expect, test, vi } from "vitest";
import { InvalidArgumentError } from "../../src/errors.js";
import { Sandbox } from "../../src/sandbox/sandbox.js";
import { ACCOUNT_ID, FakeControlPlane, REGION } from "./fake/control-plane.js";
import { ACCESS_TOKEN, createTestSandbox } from "./helpers.js";

const BASE_TEMPLATE = "rayito-base";
const BASE_ARN = `arn:aws:lambda:${REGION}:${ACCOUNT_ID}:microvm-image:${BASE_TEMPLATE}`;
const SIZED_ARN = `arn:aws:lambda:${REGION}:${ACCOUNT_ID}:microvm-image:${BASE_TEMPLATE}-4gb`;

describe("Sandbox.create({ size })", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  test("size appends the suffix to the resolved image arn", async () => {
    const { sandbox } = await createTestSandbox({
      create: { template: BASE_TEMPLATE, size: "4gb" },
    });
    expect(sandbox.info.template).toBe(SIZED_ARN);
  });

  test("size baseline keeps the unsuffixed image name", async () => {
    const { sandbox } = await createTestSandbox({
      create: { template: BASE_TEMPLATE, size: "2gb" },
    });
    expect(sandbox.info.template).toBe(BASE_ARN);
  });

  test("without size the control plane never sees getMicrovmImageVersion", async () => {
    const { sandbox, plane } = await createTestSandbox({ create: { template: BASE_TEMPLATE } });
    const info = await sandbox.getInfo();
    expect(info.size).toBeUndefined();
    expect(info.baselineMemoryMib).toBeUndefined();
    expect(info.baselineCpu).toBeUndefined();
    expect(plane.calls.filter((call) => call.operation === "getMicrovmImageVersion")).toEqual([]);
  });

  test("getInfo confirms baseline memory with one cached call", async () => {
    const { sandbox, plane } = await createTestSandbox({
      create: { template: BASE_TEMPLATE, size: "4gb" },
    });
    plane.setImageVersionMemory(SIZED_ARN, "1.0", 4096);
    const first = await sandbox.getInfo();
    const second = await sandbox.getInfo();
    expect(first.size).toBe("4gb");
    expect(second.size).toBe("4gb");
    expect(first.baselineMemoryMib).toBe(4096);
    expect(second.baselineMemoryMib).toBe(4096);
    expect(first.baselineCpu).toBe(8); // RES-2/Q88: 4096 MiB -> 8 vCPU
    expect(second.baselineCpu).toBe(8);
    expect(plane.calls.filter((call) => call.operation === "getMicrovmImageVersion")).toHaveLength(
      1,
    );
  });

  test("a size request that rounds up warns once", async () => {
    const spy = vi.spyOn(process, "emitWarning").mockImplementation(() => {});
    await createTestSandbox({
      create: { template: BASE_TEMPLATE, size: { memoryMib: 3000 } },
    });
    const compatWarnings = spy.mock.calls.filter(
      ([, options]) => (options as { type?: string } | undefined)?.type === "RayitoCompatWarning",
    );
    expect(compatWarnings).toHaveLength(1);
  });

  test("size with an arn template is invalid argument before any AWS call", async () => {
    const plane = new FakeControlPlane({ endpoint: "example.invalid" });
    await expect(
      Sandbox.create({
        template: BASE_ARN,
        size: "4gb",
        idle: null,
        accessToken: ACCESS_TOKEN,
        controlPlane: plane,
      }),
    ).rejects.toThrow(InvalidArgumentError);
    expect(plane.calls).toEqual([]);
  });

  test("impossible size is rejected before any AWS call", async () => {
    const plane = new FakeControlPlane({ endpoint: "example.invalid" });
    await expect(
      Sandbox.create({
        template: BASE_TEMPLATE,
        size: { memoryMib: 16384 },
        idle: null,
        accessToken: ACCESS_TOKEN,
        controlPlane: plane,
      }),
    ).rejects.toThrow(InvalidArgumentError);
    expect(plane.calls).toEqual([]);
  });
});
