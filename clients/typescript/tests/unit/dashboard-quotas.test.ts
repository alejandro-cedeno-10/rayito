/**
 * `Quotas`: la memoria asignada sale de `ListMicrovms` por la memoria de
 * cada imagen, las cuotas de Service Quotas (adaptador con un cliente
 * falso), los avisos y el mapeo de errores por código.
 */

import { describe, expect, it } from "vitest";
import { listMicrovmQuotas, type QuotaPage, serviceQuotasSource } from "../../src/dashboard/aws.js";
import { Quotas } from "../../src/dashboard/quotas.js";
import {
  AuthenticationError,
  DashboardError,
  InvalidArgumentError,
  RateLimitError,
} from "../../src/errors.js";
import { QUOTA_NAMES } from "../../src/limits.js";
import { ConventionCatalog } from "../../src/sizing/catalog.js";
import { IMAGE_ARN, REGION } from "./fake/control-plane.js";
import {
  appliedDefaults,
  awsSdkError,
  FakePlane,
  FakeQuotaSource,
  listItem,
  OTHER_IMAGE_ARN,
} from "./fake/dashboard.js";

function fleetPlane(): FakePlane {
  return new FakePlane(
    [
      listItem(1, { state: "RUNNING" }),
      listItem(2, { state: "SUSPENDED" }),
      listItem(3, { state: "SUSPENDING", imageArn: OTHER_IMAGE_ARN }),
      listItem(4, { state: "PENDING" }),
      listItem(5, { state: "TERMINATED" }),
    ],
    { [`${IMAGE_ARN}@1.0`]: 2048, [`${OTHER_IMAGE_ARN}@1.0`]: 8192 },
  );
}

function quotas(plane: FakePlane, source: FakeQuotaSource): Quotas {
  return new Quotas({ controlPlane: plane, source, catalog: new ConventionCatalog() });
}

describe("Quotas.report", () => {
  it("counts running and suspended memory against the quota", async () => {
    const source = new FakeQuotaSource(appliedDefaults(REGION, { memoryGb: 20 }));
    const report = await quotas(fleetPlane(), source).report();

    expect(report.region).toBe(REGION);
    expect(report.memory.sandboxes).toBe(3);
    expect(report.memory.allocatedGb).toBe(12);
    expect(report.memory.quotaGb).toBe(20);
    expect(report.memory.level).toBe("ok");
    expect(source.calls).toBe(1);
  });

  it("warns and links the increase near the limit", async () => {
    const plane = new FakePlane(
      Array.from({ length: 8 }, (_, index) => listItem(index + 1)),
      { [`${IMAGE_ARN}@1.0`]: 2048 },
    );
    const report = await quotas(
      plane,
      new FakeQuotaSource(appliedDefaults(REGION, { memoryGb: 20 })),
    ).report();

    expect(report.memory.fraction).toBe(0.8);
    expect(report.memory.level).toBe("warning");
    const warning = report.warnings.find((w) => w.kind === "memory_high");
    expect(warning?.increaseUrl.endsWith("/services/lambda/quotas/L-CD1C0CC4")).toBe(true);
  });

  it("asks the inventory only for states that hold memory", async () => {
    const plane = fleetPlane();
    await quotas(plane, new FakeQuotaSource(appliedDefaults())).report();
    expect(plane.calls).toContainEqual(["listMicrovms", ["RUNNING", "SUSPENDED", "SUSPENDING"]]);
  });

  it("counts an unreadable image version apart instead of failing", async () => {
    const plane = new FakePlane([listItem(1), listItem(2, { version: "9.9" })], {
      [`${IMAGE_ARN}@1.0`]: 1024,
    });
    const report = await quotas(plane, new FakeQuotaSource(appliedDefaults())).report();
    expect([report.memory.sandboxes, report.memory.unresolved, report.memory.allocatedGb]).toEqual([
      2, 1, 1,
    ]);
  });

  it("resolves each image version once per report", async () => {
    const plane = new FakePlane(Array.from({ length: 5 }, (_, index) => listItem(index + 1)));
    await quotas(plane, new FakeQuotaSource(appliedDefaults())).report();
    expect(plane.operations().filter((name) => name === "getMicrovmImageVersion")).toHaveLength(1);
  });
});

function quotaPager(pages: QuotaPage[]) {
  const inputs: Array<{ ServiceCode: string; NextToken?: string }> = [];
  const pager = async (input: { ServiceCode: string; NextToken?: string }): Promise<QuotaPage> => {
    inputs.push(input);
    const page = pages[inputs.length - 1];
    if (page === undefined) {
      throw new Error("sin más páginas");
    }
    return page;
  };
  return { inputs, pager };
}

describe("Service Quotas adapter", () => {
  it("keeps only microvm quotas and follows NextToken", async () => {
    const { inputs, pager } = quotaPager([
      {
        Quotas: [
          {
            QuotaCode: "L-CD1C0CC4",
            QuotaName: "Total memory for MicroVMs",
            Value: 1024,
            Adjustable: true,
          },
          { QuotaCode: "L-B99A9384", QuotaName: "Concurrent executions", Value: 1000 },
        ],
        NextToken: "t1",
      },
      { Quotas: [{ QuotaCode: "L-535CA9B6", QuotaName: "RunMicrovm rate", Value: 5 }] },
    ]);
    const applied = await listMicrovmQuotas(pager);
    expect(applied).toEqual([
      { code: "L-CD1C0CC4", name: "Total memory for MicroVMs", value: 1024, adjustable: true },
      { code: "L-535CA9B6", name: "RunMicrovm rate", value: 5, adjustable: false },
    ]);
    expect(inputs).toEqual([{ ServiceCode: "lambda" }, { ServiceCode: "lambda", NextToken: "t1" }]);
  });

  it("builds no client until the first call", async () => {
    let built = 0;
    const source = serviceQuotasSource(async () => {
      built += 1;
      return quotaPager([{ Quotas: [] }]).pager;
    });
    expect(built).toBe(0);
    await source.listAppliedQuotas();
    expect(built).toBe(1);
  });

  it.each([
    ["AccessDeniedException", AuthenticationError],
    ["TooManyRequestsException", RateLimitError],
    ["ServiceException", DashboardError],
  ] as const)("maps %s by code and never repeats the AWS message", async (code, expected) => {
    const failure = awsSdkError("arn:aws:... secreto", code);
    const source = serviceQuotasSource(async () => async () => {
      throw failure;
    });
    const error = await source.listAppliedQuotas().catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(expected);
    expect((error as Error).message).toContain(code);
    expect((error as Error).message).not.toContain("secreto");
    expect((error as { awsCode?: string }).awsCode).toBe(code);
  });

  it("a missing peer keeps its install hint and a non-AWS bug is not mislabelled", async () => {
    const missing = new InvalidArgumentError("instala @aws-sdk/client-service-quotas");
    const noPeer = serviceQuotasSource(async () => {
      throw missing;
    });
    await expect(noPeer.listAppliedQuotas()).rejects.toBe(missing);
    const bug = new TypeError("bug");
    const buggy = serviceQuotasSource(async () => async () => {
      throw bug;
    });
    await expect(buggy.listAppliedQuotas()).rejects.toBe(bug);
  });

  it("the catalog names carry no account data", () => {
    for (const name of Object.values(QUOTA_NAMES)) {
      expect(name).not.toContain("arn:");
    }
  });
});
